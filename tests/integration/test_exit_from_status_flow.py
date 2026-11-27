"""HR marks somebody Exited or Absconding straight from the record's Status (2026-11-19)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import ExitType
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.employee import EmployeeSummary, EmployeeUpdate
from app.services import employee as employee_service
from app.services.attendance.service import local_today
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _login(session: AsyncSession, tenant: Tenant, person: Employee) -> User:
    account = User(
        tenant_id=tenant.id,
        email=person.work_email,
        password_hash="not-a-real-hash",
        full_name=person.first_name,
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=person.id,
    )
    session.add(account)
    await session.commit()
    return account


async def test_absconding_marks_them_left_and_switches_off_the_login(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    account = await _login(session, tenant, employee)
    last_day = await local_today(session, hr_user) - timedelta(days=3)

    saved = await employee_service.update_employee(
        session,
        hr_user,
        employee.id,
        EmployeeUpdate(
            status=EmploymentStatus.EXITED, exit_type=ExitType.ABSCONDING, date_of_exit=last_day
        ),
    )

    assert saved.status is EmploymentStatus.EXITED
    assert saved.exit_type == "absconding" and saved.date_of_exit == last_day
    assert saved.is_active is False
    await session.refresh(account)
    assert account.is_active is False
    row = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "employee", AuditLog.entity_id == employee.id
            )
        )
    ).one()
    assert row.summary == f"Marked ACM001 as absconding (last working day {last_day})"
    assert row.changes["status"]["to"] == "EmploymentStatus.EXITED"
    assert row.changes["logins_deactivated"]["to"] == "1"
    # Lists say how they left.
    rows, _ = await employee_service.list_employees(session, hr_user, search="ACM001")
    assert EmployeeSummary.model_validate(rows[0]).exit_type == "absconding"


async def test_exited_needs_a_last_working_day_that_has_passed(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    with pytest.raises(HTTPException) as missing:
        await employee_service.update_employee(
            session, hr_user, employee.id, EmployeeUpdate(status=EmploymentStatus.EXITED)
        )
    assert missing.value.status_code == 422 and "last working day" in missing.value.detail

    tomorrow = await local_today(session, hr_user) + timedelta(days=1)
    with pytest.raises(HTTPException) as ahead:
        await employee_service.update_employee(
            session,
            hr_user,
            employee.id,
            EmployeeUpdate(status=EmploymentStatus.EXITED, date_of_exit=tomorrow),
        )
    assert ahead.value.status_code == 422 and "On notice" in ahead.value.detail
    await session.refresh(employee)
    assert employee.status is EmploymentStatus.ACTIVE and employee.is_active is True


async def test_how_they_left_goes_only_with_exited(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, hr_user, employee.id, EmployeeUpdate(exit_type=ExitType.ABSCONDING)
        )
    assert refused.value.status_code == 422


async def test_back_from_absconding_clears_how_they_left(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    today = await local_today(session, hr_user)
    await employee_service.update_employee(
        session,
        hr_user,
        employee.id,
        EmployeeUpdate(
            status=EmploymentStatus.EXITED, exit_type=ExitType.ABSCONDING, date_of_exit=today
        ),
    )
    # A plain Exited keeps the record left but drops "absconding".
    plain = await employee_service.update_employee(
        session, hr_user, employee.id, EmployeeUpdate(exit_type=None)
    )
    assert plain.status is EmploymentStatus.EXITED
    back = await employee_service.update_employee(
        session, hr_user, employee.id, EmployeeUpdate(status=EmploymentStatus.ACTIVE)
    )
    assert back.status is EmploymentStatus.ACTIVE
    assert back.exit_type is None and back.is_active is True


async def test_nobody_marks_themselves_as_left(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    hr_user.employee_id = employee.id
    await session.commit()
    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session,
            hr_user,
            employee.id,
            EmployeeUpdate(
                status=EmploymentStatus.EXITED,
                date_of_exit=await local_today(session, hr_user),
            ),
        )
    assert refused.value.status_code == 403
