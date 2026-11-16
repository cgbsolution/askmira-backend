"""Consultant timesheets: filled on their behalf, routed, and kept out of payroll."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval_rules import ApprovalProcess, ApprovalRule
from app.models.employee import ApproverRoute, Employee, EmploymentStatus, EmploymentType
from app.models.org import LegalEntity
from app.models.payroll_inputs import PayrollAdjustment
from app.models.tenant import Tenant
from app.models.timesheets import Timesheet, TimesheetStatus
from app.models.user import User, UserRole
from app.services import timesheets
from app.services.timesheets import EntryIn, RateIn
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
        email=f"{code.lower()}.cts@acme-demo.com",
        password_hash="x",
        full_name=code.title(),
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=person.id,
    )
    session.add(login)
    await session.commit()
    return person, await as_caller(session, login)


async def _consultant(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    code: str,
    approver: User | None = None,
    **extra,
) -> tuple[Employee, User]:
    return await _person(
        session,
        tenant,
        entity,
        code,
        employment_type=EmploymentType.CONSULTANT,
        timesheet_approver_route=extra.pop("route", ApproverRoute.INTERNAL),
        internal_approver_user_id=approver.id if approver else None,
        **extra,
    )


def _hours(*pairs: tuple[int, str]) -> list[EntryIn]:
    return [EntryIn(work_date=date(2026, 9, day), hours=Decimal(hours)) for day, hours in pairs]


async def _rate(session: AsyncSession, hr_user: User, person: Employee, rate: str) -> None:
    await timesheets.add_rate(
        session,
        hr_user,
        RateIn(employee_id=person.id, rate=Decimal(rate), effective_from=date(2026, 1, 1)),
        request=None,
    )


async def test_internal_admin_fills_and_submits_for_consultant(
    session, tenant, hr_user, legal_entity
) -> None:
    admin_person, admin = await _person(session, tenant, legal_entity, "ADMN")
    consultant, _ = await _consultant(session, tenant, legal_entity, "CON1", approver=admin)
    _, stranger = await _person(session, tenant, legal_entity, "STRN")

    week = await timesheets.save_week(
        session, admin, MONDAY, _hours((7, "8")), employee_id=consultant.id
    )
    assert week["employee_id"] == str(consultant.id)
    submitted = await timesheets.submit_week(session, admin, MONDAY, employee_id=consultant.id)
    assert submitted["status"] == "submitted"
    assert submitted["approval_route"] == "internal"

    with pytest.raises(HTTPException) as refused:
        await timesheets.save_week(
            session, stranger, MONDAY, _hours((8, "4")), employee_id=consultant.id
        )
    assert refused.value.status_code == 403
    with pytest.raises(HTTPException) as refused:
        await timesheets.submit_week(session, stranger, MONDAY, employee_id=consultant.id)
    assert refused.value.status_code == 403


async def test_cannot_fill_for_a_non_consultant(session, tenant, hr_user, legal_entity) -> None:
    full_timer, _ = await _person(session, tenant, legal_entity, "FTIM")
    with pytest.raises(HTTPException) as refused:
        await timesheets.save_week(
            session, hr_user, MONDAY, _hours((7, "8")), employee_id=full_timer.id
        )
    assert refused.value.status_code == 403


async def test_consultant_week_skips_approval_rules(session, tenant, hr_user, legal_entity) -> None:
    session.add(
        ApprovalRule(
            tenant_id=tenant.id,
            process=ApprovalProcess.TIMESHEET,
            name="Timesheets via HR",
            steps=[{"kind": "hr"}],
        )
    )
    await session.commit()
    _, admin = await _person(session, tenant, legal_entity, "ADMN")
    consultant, _ = await _consultant(session, tenant, legal_entity, "CON1", approver=admin)

    await timesheets.save_week(session, admin, MONDAY, _hours((7, "8")), employee_id=consultant.id)
    submitted = await timesheets.submit_week(session, admin, MONDAY, employee_id=consultant.id)
    assert submitted["approval_chain"] == []
    assert submitted["approval_route"] == "internal"
    assert submitted["consultant"] is True


async def test_approved_consultant_week_creates_no_payroll_adjustment(
    session, tenant, hr_user, legal_entity
) -> None:
    _, admin = await _person(session, tenant, legal_entity, "ADMN")
    consultant, consultant_login = await _consultant(
        session, tenant, legal_entity, "CON1", approver=admin
    )
    await _rate(session, hr_user, consultant, "1200")
    await timesheets.save_week(
        session, consultant_login, MONDAY, _hours((7, "8"), (8, "8"), (9, "8"), (10, "8"))
    )
    submitted = await timesheets.submit_week(session, consultant_login, MONDAY)
    sheet_id = uuid.UUID(submitted["id"])
    # The consultant never sees what the week pays; neither may anyone else.
    assert "amount" not in submitted

    with pytest.raises(HTTPException) as refused:
        await timesheets.decide(session, consultant_login, sheet_id, approve=True, note=None)
    assert refused.value.status_code == 403

    done = await timesheets.decide(session, admin, sheet_id, approve=True, note=None)
    assert done["status"] == "approved"
    assert done["amount"] == "38400.00"
    assert done["pay"] is None
    assert await session.scalar(select(func.count()).select_from(PayrollAdjustment)) == 0
    sheet = await session.scalar(select(Timesheet).where(Timesheet.id == sheet_id))
    assert sheet is not None and sheet.payroll_adjustment_id is None


async def test_amount_is_only_for_hr_and_the_internal_approver(
    session, tenant, hr_user, legal_entity
) -> None:
    _, admin = await _person(session, tenant, legal_entity, "ADMN")
    _, stranger = await _person(session, tenant, legal_entity, "STRN")
    consultant, consultant_login = await _consultant(
        session, tenant, legal_entity, "CON1", approver=admin
    )
    await _rate(session, hr_user, consultant, "1000")
    await timesheets.save_week(
        session, hr_user, MONDAY, _hours((7, "2")), employee_id=consultant.id
    )

    as_hr = await timesheets.person_week(session, hr_user, consultant.id, MONDAY)
    as_approver = await timesheets.person_week(session, admin, consultant.id, MONDAY)
    own = await timesheets.my_week(session, consultant_login, MONDAY)
    assert as_hr["amount"] == as_approver["amount"] == "2000.00"
    assert "amount" not in own
    assert own["consultant"] is True and own["approval_route"] is None
    with pytest.raises(HTTPException):
        await timesheets.person_week(session, stranger, consultant.id, MONDAY)


async def test_internal_route_without_an_approver_tells_hr(
    session, tenant, hr_user, legal_entity
) -> None:
    from app.models.notifications import Notification

    consultant, consultant_login = await _consultant(session, tenant, legal_entity, "CON1")
    await timesheets.save_week(session, consultant_login, MONDAY, _hours((7, "8")))
    await timesheets.submit_week(session, consultant_login, MONDAY)
    told = (
        await session.scalars(
            select(Notification.recipient_user_id).where(
                Notification.event_key == "timesheets.submitted"
            )
        )
    ).all()
    assert list(told) == [hr_user.id]


async def test_customer_route_only_records_the_route(
    session, tenant, hr_user, legal_entity
) -> None:
    consultant, consultant_login = await _consultant(
        session, tenant, legal_entity, "CON1", route=ApproverRoute.CUSTOMER
    )
    await timesheets.save_week(session, consultant_login, MONDAY, _hours((7, "8")))
    submitted = await timesheets.submit_week(session, consultant_login, MONDAY)
    assert submitted["status"] == "submitted"
    assert submitted["approval_route"] == "customer"


async def test_hourly_employee_unchanged(session, tenant, hr_user, legal_entity) -> None:
    lead, lead_login = await _person(session, tenant, legal_entity, "LEAD")
    dev, dev_login = await _person(session, tenant, legal_entity, "DEV", manager_id=lead.id)
    await _rate(session, hr_user, dev, "500")
    await timesheets.save_week(session, dev_login, MONDAY, _hours((7, "8")))
    submitted = await timesheets.submit_week(session, dev_login, MONDAY)
    assert submitted["consultant"] is False
    assert submitted["approval_route"] is None
    assert "amount" not in submitted
    done = await timesheets.decide(
        session, lead_login, uuid.UUID(submitted["id"]), approve=True, note=None
    )
    assert done["pay"] == "4000.00"
    assert await session.scalar(select(func.count()).select_from(PayrollAdjustment)) == 1


async def test_pending_consultant_only_filter(session, tenant, hr_user, legal_entity) -> None:
    _, admin = await _person(session, tenant, legal_entity, "ADMN")
    _, other_admin = await _person(session, tenant, legal_entity, "OTHR")
    lead, _ = await _person(session, tenant, legal_entity, "LEAD")
    consultant, consultant_login = await _consultant(
        session, tenant, legal_entity, "CON1", approver=admin
    )
    dev, dev_login = await _person(session, tenant, legal_entity, "DEV", manager_id=lead.id)
    await timesheets.save_week(session, consultant_login, MONDAY, _hours((7, "8")))
    await timesheets.submit_week(session, consultant_login, MONDAY)
    await timesheets.save_week(session, dev_login, MONDAY, _hours((7, "8")))
    await timesheets.submit_week(session, dev_login, MONDAY)

    everything = await timesheets.pending_for_approver(session, hr_user)
    assert {p["employee_code"] for p in everything} == {"CON1", "DEV"}
    only = await timesheets.pending_for_approver(session, hr_user, consultant_only=True)
    assert [p["employee_code"] for p in only] == ["CON1"]
    assert only[0]["consultant"] is True
    mine = await timesheets.pending_for_approver(session, admin)
    assert [p["employee_code"] for p in mine] == ["CON1"]
    assert await timesheets.pending_for_approver(session, other_admin) == []


async def test_reopen_returns_a_decided_week_to_draft(
    session, tenant, hr_user, legal_entity
) -> None:
    _, admin = await _person(session, tenant, legal_entity, "ADMN")
    consultant, consultant_login = await _consultant(
        session, tenant, legal_entity, "CON1", approver=admin
    )
    await timesheets.save_week(session, consultant_login, MONDAY, _hours((7, "8")))
    submitted = await timesheets.submit_week(session, consultant_login, MONDAY)
    sheet_id = uuid.UUID(submitted["id"])

    with pytest.raises(HTTPException) as refused:  # not decided yet
        await timesheets.reopen_week(session, admin, sheet_id)
    assert refused.value.status_code == 409
    await timesheets.decide(session, admin, sheet_id, approve=True, note="ok")
    with pytest.raises(HTTPException) as refused:
        await timesheets.reopen_week(session, consultant_login, sheet_id)
    assert refused.value.status_code == 403

    reopened = await timesheets.reopen_week(session, admin, sheet_id)
    assert reopened["status"] == "draft"
    assert reopened["decision_note"] is None
    assert len(reopened["entries"]) == 1
    sheet = await session.scalar(select(Timesheet).where(Timesheet.id == sheet_id))
    assert sheet is not None
    assert sheet.status is TimesheetStatus.DRAFT
    assert sheet.decided_by_user_id is None and sheet.decided_at is None


async def test_pending_reminder_goes_to_internal_approver_not_line_manager(
    session, tenant, hr_user, legal_entity
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.models.notifications import Notification

    manager, _ = await _person(session, tenant, legal_entity, "MNGR")
    _, admin = await _person(session, tenant, legal_entity, "ADMN")
    consultant, consultant_login = await _consultant(
        session, tenant, legal_entity, "CON1", approver=admin, manager_id=manager.id
    )
    await timesheets.save_week(session, consultant_login, MONDAY, _hours((7, "8")))
    await timesheets.submit_week(session, consultant_login, MONDAY)

    sent = await timesheets.send_timesheet_reminders(
        session, hr_user, today=date(2026, 9, 30), now=datetime.now(UTC) + timedelta(days=3)
    )
    assert sent["approvers_reminded"] == 1
    told = (
        await session.scalars(
            select(Notification.recipient_user_id).where(
                Notification.event_key == "timesheets.reminder.pending"
            )
        )
    ).all()
    assert list(told) == [admin.id]


async def test_timesheet_agent_leaves_consultant_weeks_to_people(
    session, tenant, hr_user, legal_entity
) -> None:
    from datetime import UTC, datetime

    from app.models.attendance import AttendanceDay, AttendanceStatus
    from app.models.autonomy import AgentProposal
    from app.services.autonomy import agents

    consultant, consultant_login = await _consultant(session, tenant, legal_entity, "CON1")
    session.add(
        AttendanceDay(
            tenant_id=tenant.id,
            employee_id=consultant.id,
            attendance_date=date(2026, 9, 7),
            worked_minutes=8 * 60,
            status=AttendanceStatus.PRESENT,
            payable_fraction=Decimal("1"),
            computed_at=datetime(2026, 9, 8, tzinfo=UTC),
        )
    )
    await session.commit()
    await timesheets.save_week(session, consultant_login, MONDAY, _hours((7, "8")))
    await timesheets.submit_week(session, consultant_login, MONDAY)

    outcome = await agents.timesheet_agent(session, hr_user, today=date(2026, 9, 14))
    assert outcome["proposed"] == 0 and outcome["held"] == []
    assert await session.scalar(select(func.count()).select_from(AgentProposal)) == 0
