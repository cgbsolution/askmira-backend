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
        tenant_id=tenant.id, employee_code=code, first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com", legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE, **extra,
    )
    session.add(person)
    await session.flush()
    login = User(
        tenant_id=tenant.id, email=f"{code.lower()}.ts@acme-demo.com", password_hash="x",
        full_name=code.title(), role=UserRole.EMPLOYEE, is_active=True, employee_id=person.id,
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
        session, hr_user, RateIn(employee_id=dev.id, rate=Decimal("500"),
                                 effective_from=date(2026, 1, 1)), request=None,
    )
    # A raise from Wednesday: Monday and Tuesday at 500, the rest at 600.
    await timesheets.add_rate(
        session, hr_user, RateIn(employee_id=dev.id, rate=Decimal("600"),
                                 effective_from=date(2026, 9, 9)), request=None,
    )

    with pytest.raises(HTTPException):
        await timesheets.save_week(
            session, dev_login, MONDAY,
            [EntryIn(work_date=date(2026, 9, 20), project_id=project_id, hours=Decimal("2"))],
        )
    week = await timesheets.save_week(
        session, dev_login, MONDAY,
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
