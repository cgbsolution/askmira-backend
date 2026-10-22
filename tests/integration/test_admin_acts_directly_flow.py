"""An admin acts directly: on logins, on their own profile, on anyone's attendance.

- Deactivating somebody ends their login with them, and deleting a login frees
  its email for a login anywhere on the platform.
- HR edits their own sensitive profile sections directly; an employee still
  raises a request.
- HR's attendance correction is applied at once, nobody asked; an employee's
  still waits for a decision.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.attendance import (
    AttendanceDay,
    AttendanceStatus,
    RegularisationKind,
    RegularisationStatus,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.attendance import ShiftCreate
from app.schemas.auth import LoginRequest
from app.schemas.employee_profile import IdentityDetails, IdentityWrite
from app.services import auth as auth_service
from app.services import employee as employee_service
from app.services import employee_profile as profile_service
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from app.services.identity import users as user_admin
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer

pytestmark = requires_db

client = platform_support.client

PASSWORD = "Correct-horse-9"
MONDAY = date(2026, 4, 6)


def _ist(day: date, clock: str) -> datetime:
    hours, minutes = (int(part) for part in clock.split(":"))
    return datetime.combine(day, time(hours, minutes), tzinfo=UTC) - timedelta(hours=5, minutes=30)


async def _login(session: AsyncSession, tenant: Tenant, employee: Employee, email: str) -> User:
    login = User(
        tenant_id=tenant.id,
        email=email,
        password_hash=hash_password(PASSWORD),
        full_name=f"{employee.first_name} {employee.last_name}",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(login)
    await session.commit()
    return login


async def _admin_with_record(session: AsyncSession, tenant: Tenant, hr_user: User) -> Employee:
    record = Employee(
        tenant_id=tenant.id,
        employee_code="ACM900",
        first_name="Deepk",
        last_name="Rawat",
        work_email=hr_user.email,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2026, 1, 1),
    )
    session.add(record)
    await session.flush()
    hr_user.employee_id = record.id
    await session.commit()
    return record


async def test_deactivating_someone_ends_their_login_and_deleting_it_frees_the_email(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    client: httpx.AsyncClient,
) -> None:
    login = await _login(session, tenant, employee, "deepak.rawat@cgbindia-demo.com")

    await employee_service.deactivate_employee(session, hr_user, employee.id)
    await session.refresh(login)
    assert login.is_active is False
    with pytest.raises(HTTPException):
        await auth_service.login(session, LoginRequest(email=login.email, password=PASSWORD))

    # Switched off, the address is still taken; deleted, it is free again.
    assert await auth_service.email_in_use(session, "deepak.rawat@cgbindia-demo.com")
    deleted = await client.request(
        "DELETE",
        f"/api/v1/admin/users/{login.id}",
        json={"reason": "Added to the wrong workspace"},
        headers=bearer(hr_user),
    )
    assert deleted.status_code == 204
    assert not await auth_service.email_in_use(session, "deepak.rawat@cgbindia-demo.com")

    await session.refresh(login)
    assert login.employee_id is None
    listed = await user_admin.list_users(session, hr_user)
    assert login.id not in {row.id for row in listed}
    with pytest.raises(HTTPException) as gone:
        await user_admin.set_active(session, hr_user, user_id=login.id, active=True, reason="Undo")
    assert gone.value.status_code == 404
    with pytest.raises(HTTPException) as own:
        await user_admin.delete_login(session, hr_user, user_id=hr_user.id, reason="Test")
    assert own.value.status_code == 403


async def test_hr_edits_their_own_legal_name_directly_and_an_employee_still_asks(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    own = await _admin_with_record(session, tenant, hr_user)
    fix = IdentityWrite(
        section="identity", data=IdentityDetails(first_name="Deepak", last_name="Rawat")
    )

    profile = await profile_service.write_section(session, hr_user, own.id, fix)
    assert "identity" in {s.value for s in profile.direct_sections}
    assert profile.request_sections == []
    await session.refresh(own)
    assert own.first_name == "Deepak"

    worker = await _login(session, tenant, employee, "anita.login@acme-demo.com")
    with pytest.raises(HTTPException) as asks:
        await profile_service.write_section(session, worker, employee.id, fix)
    assert asks.value.status_code == 403


async def test_hr_marks_a_day_present_at_once_and_an_employee_s_correction_waits(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await attendance_setup.create_shift(
        session,
        hr_user,
        ShiftCreate(
            code="GEN",
            name="General",
            start_time=time(9, 30),
            end_time=time(18, 30),
            break_minutes=60,
            grace_in_minutes=15,
            half_day_threshold_minutes=240,
            full_day_threshold_minutes=420,
            weekly_off_days=[6, 7],
        ),
    )

    marked = await attendance_service.raise_regularisation(
        session,
        hr_user,
        employee_id=employee.id,
        attendance_date=MONDAY,
        requested_status=AttendanceStatus.PRESENT,
        reason="Was at a client site",
        kind=RegularisationKind.MISSED_PUNCH,
        requested_first_in=_ist(MONDAY, "09:30"),
        requested_last_out=_ist(MONDAY, "18:30"),
    )
    assert marked.status is RegularisationStatus.APPROVED
    assert marked.approver_user_id == hr_user.id
    day = await session.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee.id, AttendanceDay.attendance_date == MONDAY
        )
    )
    assert day is not None and day.status is AttendanceStatus.PRESENT

    worker = await _login(session, tenant, employee, "anita.login@acme-demo.com")
    asked = await attendance_service.raise_regularisation(
        session,
        worker,
        employee_id=employee.id,
        attendance_date=MONDAY + timedelta(days=1),
        requested_status=AttendanceStatus.PRESENT,
        reason="Forgot to punch",
        kind=RegularisationKind.MISSED_PUNCH,
        requested_first_in=_ist(MONDAY + timedelta(days=1), "09:30"),
    )
    assert asked.status is RegularisationStatus.PENDING
