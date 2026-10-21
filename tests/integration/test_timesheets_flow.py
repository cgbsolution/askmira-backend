"""Weekly timesheets: filled, submitted, approved by the manager, priced at hourly rates."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
from app.models.payroll_inputs import AdjustmentStatus, PayrollAdjustment
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import timesheets
from app.services.timesheets import EntryIn, ProjectIn, RateIn
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

MONDAY = date(2026, 9, 7)


async def _person(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, code: str, **extra
) -> tuple[Employee, User]:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com",
        legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE,
        **extra,
    )
    session.add(person)
    await session.flush()
    login = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}.ts@acme-demo.com",
        password_hash="x",
        full_name=code.title(),
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=person.id,
    )
    session.add(login)
    await session.commit()
    return person, await as_caller(session, login)


async def test_an_approved_week_is_priced_at_the_rate_in_force_each_day(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    lead, lead_login = await _person(session, tenant, legal_entity, "LEAD")
    dev, dev_login = await _person(session, tenant, legal_entity, "DEV", manager_id=lead.id)
    lead_login = await as_caller(session, lead_login)
    project = await timesheets.create_project(
        session, hr_user, ProjectIn(code="ACME-WEB", name="Acme website"), request=None
    )
    project_id = uuid.UUID(project["id"])
    await timesheets.add_rate(
        session,
        hr_user,
        RateIn(employee_id=dev.id, rate=Decimal("500"), effective_from=date(2026, 1, 1)),
        request=None,
    )
    # A raise from Wednesday: Monday and Tuesday at 500, the rest at 600.
    await timesheets.add_rate(
        session,
        hr_user,
        RateIn(employee_id=dev.id, rate=Decimal("600"), effective_from=date(2026, 9, 9)),
        request=None,
    )

    with pytest.raises(HTTPException):
        await timesheets.save_week(
            session,
            dev_login,
            MONDAY,
            [EntryIn(work_date=date(2026, 9, 20), project_id=project_id, hours=Decimal("2"))],
        )
    week = await timesheets.save_week(
        session,
        dev_login,
        MONDAY,
        [
            EntryIn(work_date=date(2026, 9, 7), project_id=project_id, hours=Decimal("8")),
            EntryIn(work_date=date(2026, 9, 8), project_id=project_id, hours=Decimal("8")),
            EntryIn(work_date=date(2026, 9, 9), project_id=project_id, hours=Decimal("4")),
        ],
    )
    assert week["total_hours"] == "20.00"
    submitted = await timesheets.submit_week(session, dev_login, MONDAY)
    assert submitted["status"] == "submitted"
    with pytest.raises(HTTPException):
        await timesheets.save_week(session, dev_login, MONDAY, [])

    pending = await timesheets.pending_for_approver(session, lead_login)
    assert [p["employee_code"] for p in pending] == ["DEV"]
    with pytest.raises(HTTPException):
        await timesheets.decide(
            session, dev_login, uuid.UUID(submitted["id"]), approve=True, note=None
        )
    done = await timesheets.decide(
        session, lead_login, uuid.UUID(submitted["id"]), approve=True, note=None
    )
    # 16 hours at 500 and 4 at 600.
    assert done["pay"] == "10400.00"
    adjustment = await session.scalar(select(PayrollAdjustment))
    assert adjustment is not None and adjustment.status is AdjustmentStatus.PENDING
    assert (adjustment.start_year, adjustment.start_month) == (2026, 9)

    report = await timesheets.hours_report(
        session, hr_user, start=date(2026, 9, 1), end=date(2026, 9, 30)
    )
    assert report["projects"][0]["code"] == "ACME-WEB"
    assert report["projects"][0]["hours"] == "20.00"


async def test_a_rejected_week_goes_back_for_changes(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    dev, dev_login = await _person(session, tenant, legal_entity, "DEV")
    await timesheets.save_week(
        session, dev_login, MONDAY, [EntryIn(work_date=MONDAY, hours=Decimal("3"))]
    )
    sent = await timesheets.submit_week(session, dev_login, MONDAY)
    with pytest.raises(HTTPException):
        await timesheets.decide(session, hr_user, uuid.UUID(sent["id"]), approve=False, note=None)
    back = await timesheets.decide(
        session, hr_user, uuid.UUID(sent["id"]), approve=False, note="Split by project, please"
    )
    assert back["status"] == "rejected" and back["pay"] is None
    again = await timesheets.save_week(
        session, dev_login, MONDAY, [EntryIn(work_date=MONDAY, hours=Decimal("4"))]
    )
    assert again["status"] == "draft"


async def test_tasks_project_rates_notifications_and_reminders(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.models.notifications import Notification
    from app.services.timesheets import TaskIn

    lead, lead_login = await _person(session, tenant, legal_entity, "PM")
    dev, dev_login = await _person(session, tenant, legal_entity, "ENG", manager_id=lead.id)
    lead_login = await as_caller(session, lead_login)
    client_work = await timesheets.create_project(
        session, hr_user, ProjectIn(code="CLIENT", name="Client build"), request=None
    )
    internal = await timesheets.create_project(
        session, hr_user, ProjectIn(code="INT", name="Internal", billable=False), request=None
    )
    client_id, internal_id = uuid.UUID(client_work["id"]), uuid.UUID(internal["id"])
    design = await timesheets.create_task(
        session, hr_user, client_id, TaskIn(name="Design"), request=None
    )
    projects = {p["code"]: p for p in await timesheets.list_projects(session, dev_login)}
    assert [t["name"] for t in projects["CLIENT"]["tasks"]] == ["Design"]

    # General rate 400; on the client project the person is paid 700.
    await timesheets.add_rate(
        session,
        hr_user,
        RateIn(employee_id=dev.id, rate=Decimal("400"), effective_from=date(2026, 1, 1)),
        request=None,
    )
    await timesheets.add_rate(
        session,
        hr_user,
        RateIn(
            employee_id=dev.id,
            project_id=client_id,
            rate=Decimal("700"),
            effective_from=date(2026, 1, 1),
        ),
        request=None,
    )

    # A project split into tasks needs one on every entry.
    with pytest.raises(HTTPException) as exc:
        await timesheets.save_week(
            session,
            dev_login,
            MONDAY,
            [EntryIn(work_date=MONDAY, project_id=client_id, hours=Decimal("5"))],
        )
    assert exc.value.status_code == 422
    week = await timesheets.save_week(
        session,
        dev_login,
        MONDAY,
        [
            EntryIn(
                work_date=MONDAY,
                project_id=client_id,
                task_id=uuid.UUID(design["id"]),
                hours=Decimal("5"),
            ),
            EntryIn(work_date=MONDAY, project_id=internal_id, hours=Decimal("3")),
        ],
    )
    assert week["entries"][0]["task"] == "Design"

    # Saved but not submitted after the week ended: the person is reminded.
    later = MONDAY + timedelta(days=9)
    sent = await timesheets.send_timesheet_reminders(session, hr_user, today=later)
    assert sent["people_reminded"] == 1

    await timesheets.submit_week(session, dev_login, MONDAY)
    told = (
        await session.scalars(
            select(Notification).where(Notification.event_key == "timesheets.submitted")
        )
    ).all()
    assert [n.recipient_user_id for n in told] == [lead_login.id]

    # Waiting more than two days: the approver is reminded.
    sent = await timesheets.send_timesheet_reminders(
        session, hr_user, today=later, now=datetime.now(UTC) + timedelta(days=3)
    )
    assert sent["approvers_reminded"] == 1

    decided = await timesheets.decide(
        session, lead_login, uuid.UUID(week["id"]), approve=True, note=None
    )
    assert decided["pay"] == "4700.00"  # 5 h at 700 on the client project + 3 h at 400
    decision = (
        await session.scalars(
            select(Notification).where(Notification.event_key == "timesheets.decided")
        )
    ).all()
    assert [n.recipient_user_id for n in decision] == [dev_login.id]

    report = await timesheets.hours_report(
        session, hr_user, start=MONDAY, end=MONDAY + timedelta(days=6)
    )
    client_row = next(p for p in report["projects"] if p["code"] == "CLIENT")
    assert client_row["tasks"] == [{"name": "Design", "hours": "5.00"}]


async def test_the_timesheet_agent_proposes_only_weeks_attendance_supports(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    from datetime import UTC, datetime

    from app.models.attendance import AttendanceDay, AttendanceStatus
    from app.models.autonomy import AgentProposal
    from app.models.timesheets import Timesheet, TimesheetStatus
    from app.services.autonomy import agents
    from app.services.autonomy import service as autonomy

    lead, lead_login = await _person(session, tenant, legal_entity, "LEAD")
    good, good_login = await _person(session, tenant, legal_entity, "GOOD", manager_id=lead.id)
    padded, padded_login = await _person(session, tenant, legal_entity, "PAD", manager_id=lead.id)
    lead_login = await as_caller(session, lead_login)
    project = await timesheets.create_project(
        session, hr_user, ProjectIn(code="AGENT", name="Agent check"), request=None
    )
    project_id = uuid.UUID(project["id"])
    for person in (good, padded):
        for day, minutes in ((7, 8 * 60), (8, 6 * 60)):
            session.add(
                AttendanceDay(
                    tenant_id=tenant.id,
                    employee_id=person.id,
                    attendance_date=date(2026, 9, day),
                    worked_minutes=minutes,
                    status=AttendanceStatus.PRESENT,
                    payable_fraction=Decimal("1"),
                    computed_at=datetime(2026, 9, day + 1, tzinfo=UTC),
                )
            )
    await session.commit()
    for login, second_day in ((good_login, "6"), (padded_login, "8")):
        await timesheets.save_week(
            session,
            login,
            MONDAY,
            [
                EntryIn(work_date=date(2026, 9, 7), project_id=project_id, hours=Decimal("8")),
                EntryIn(
                    work_date=date(2026, 9, 8), project_id=project_id, hours=Decimal(second_day)
                ),
            ],
        )
        await timesheets.submit_week(session, login, MONDAY)

    outcome = await agents.timesheet_agent(session, hr_user, today=date(2026, 9, 14))
    assert outcome["proposed"] == 1
    [held] = outcome["held"]
    assert "Tue 08 Sep: 8.00 h logged, 6.00 h in attendance" in held
    again = await agents.timesheet_agent(session, hr_user, today=date(2026, 9, 14))
    assert again["proposed"] == 0, "one proposal per timesheet"

    proposal = await session.scalar(select(AgentProposal))
    good_sheet = await session.scalar(select(Timesheet).where(Timesheet.employee_id == good.id))
    assert proposal.subject_id == good_sheet.id and proposal.citations[0].startswith("timesheets:")

    await autonomy.decide_proposal(session, lead_login, proposal_id=proposal.id, approve=True)
    await session.commit()
    await session.refresh(good_sheet)
    assert good_sheet.status is TimesheetStatus.APPROVED
    assert good_sheet.decided_by_user_id == lead_login.id
