"""HR's own requests go to their manager; employees see no company-wide figures.

- An admin or sub-admin who has a manager raises a missed punch like anyone:
  it waits, their manager decides it, and the day changes only then. They
  cannot decide it themselves.
- With nobody above them, HR's own correction is still applied at once, and
  HR's correction to anybody else's day always is.
- The home dashboard gives headcount and new hires to HR only; anybody else
  sees their own team on leave, not the company's.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.attendance import (
    AttendanceDay,
    AttendanceStatus,
    RegularisationKind,
    RegularisationStatus,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.attendance import ShiftCreate
from app.services.approvals import service as approvals_service
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

MONDAY = date(2026, 4, 6)


def _ist(day: date, clock: str) -> datetime:
    hours, minutes = (int(part) for part in clock.split(":"))
    return datetime.combine(day, time(hours, minutes), tzinfo=UTC) - timedelta(hours=5, minutes=30)


async def _person(
    session: AsyncSession, tenant: Tenant, code: str, *, manager: Employee | None = None
) -> Employee:
    row = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code,
        last_name="Test",
        work_email=f"{code.lower()}@acme.test",
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2026, 1, 1),
        manager_id=manager.id if manager else None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _login(session: AsyncSession, tenant: Tenant, person: Employee, role: UserRole) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{person.employee_code.lower()}@login.test",
        password_hash="x",
        full_name=f"{person.first_name} Test",
        role=role,
        is_active=True,
        employee_id=person.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return await as_caller(session, user)


@pytest.fixture
async def shift(session: AsyncSession, hr_user: User) -> None:
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


async def _missed_punch(session: AsyncSession, user: User, day: date = MONDAY):
    return await attendance_service.raise_regularisation(
        session,
        user,
        employee_id=user.employee_id,
        attendance_date=day,
        requested_status=AttendanceStatus.PRESENT,
        reason="Forgot to punch out",
        kind=RegularisationKind.MISSED_PUNCH,
        requested_first_in=_ist(day, "09:30"),
        requested_last_out=_ist(day, "18:30"),
    )


async def _day(session: AsyncSession, employee_id, day: date = MONDAY) -> AttendanceDay | None:
    return await session.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee_id, AttendanceDay.attendance_date == day
        )
    )


# ── HR's own missed punch ───────────────────────────────────────────────────


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SUB_ADMIN])
async def test_hr_with_a_manager_asks_and_the_manager_decides(
    session: AsyncSession, tenant: Tenant, shift, role: UserRole
) -> None:
    boss = await _person(session, tenant, "BOSS1")
    boss_login = await _login(session, tenant, boss, UserRole.EMPLOYEE)
    hr_person = await _person(session, tenant, "HRME1", manager=boss)
    hr = await _login(session, tenant, hr_person, role)

    asked = await _missed_punch(session, hr)

    assert asked.status is RegularisationStatus.PENDING
    assert not asked.applied_directly
    day = await _day(session, hr_person.id)
    assert day is None or day.status is not AttendanceStatus.PRESENT
    # It waits in the manager's approvals, and HR cannot wave it through.
    inbox = await approvals_service.inbox(session, await as_caller(session, boss_login))
    assert inbox.total >= 1
    with pytest.raises(HTTPException) as refused:
        await attendance_service.decide_regularisation(session, hr, asked.id, approve=True)
    assert refused.value.status_code == 403

    decided = await attendance_service.decide_regularisation(
        session, boss_login, asked.id, approve=True
    )
    assert decided.status is RegularisationStatus.APPROVED
    assert decided.approver_user_id == boss_login.id
    day = await _day(session, hr_person.id)
    assert day is not None and day.status is AttendanceStatus.PRESENT


async def test_hr_with_a_manager_cannot_mark_a_stretch_of_their_own_days(
    session: AsyncSession, tenant: Tenant, shift
) -> None:
    boss = await _person(session, tenant, "BOSS2")
    hr_person = await _person(session, tenant, "HRME2", manager=boss)
    hr = await _login(session, tenant, hr_person, UserRole.ADMIN)

    with pytest.raises(HTTPException) as refused:
        await attendance_service.raise_regularisation_range(
            session,
            hr,
            employee_id=hr_person.id,
            start=MONDAY,
            end=MONDAY + timedelta(days=2),
            kind=RegularisationKind.STATUS_CHANGE,
            reason="Was in",
        )
    assert refused.value.status_code == 403
    assert "manager" in refused.value.detail


async def test_hr_with_nobody_above_them_still_applies_their_own_at_once(
    session: AsyncSession, tenant: Tenant, shift
) -> None:
    hr_person = await _person(session, tenant, "TOP01")
    hr = await _login(session, tenant, hr_person, UserRole.ADMIN)

    applied = await _missed_punch(session, hr)

    assert applied.status is RegularisationStatus.APPROVED
    assert applied.applied_directly
    day = await _day(session, hr_person.id)
    assert day is not None and day.status is AttendanceStatus.PRESENT


async def test_hr_with_a_manager_still_marks_somebody_else_at_once(
    session: AsyncSession, tenant: Tenant, shift
) -> None:
    boss = await _person(session, tenant, "BOSS3")
    hr_person = await _person(session, tenant, "HRME3", manager=boss)
    hr = await _login(session, tenant, hr_person, UserRole.SUB_ADMIN)
    worker = await _person(session, tenant, "WORK3")

    marked = await attendance_service.raise_regularisation(
        session,
        hr,
        employee_id=worker.id,
        attendance_date=MONDAY,
        requested_status=AttendanceStatus.PRESENT,
        reason="Was at a client site",
        kind=RegularisationKind.MISSED_PUNCH,
        requested_first_in=_ist(MONDAY, "09:30"),
        requested_last_out=_ist(MONDAY, "18:30"),
    )

    assert marked.status is RegularisationStatus.APPROVED
    assert marked.applied_directly


# ── The home dashboard ──────────────────────────────────────────────────────


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def test_only_hr_sees_company_figures_on_the_dashboard(
    client, session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    boss = await _person(session, tenant, "LEAD1")
    me = await _person(session, tenant, "MINE1", manager=boss)
    mate = await _person(session, tenant, "MATE1", manager=boss)
    stranger = await _person(session, tenant, "FAR01")
    kind = LeaveType(tenant_id=tenant.id, name="Casual Leave", code="CL")
    session.add(kind)
    await session.flush()
    around_today = date.today()
    for person in (mate, stranger):
        session.add(
            LeaveRequest(
                tenant_id=tenant.id,
                employee_id=person.id,
                leave_type_id=kind.id,
                start_date=around_today - timedelta(days=1),
                end_date=around_today + timedelta(days=1),
                total_days=Decimal("3"),
                status=LeaveRequestStatus.APPROVED,
            )
        )
    await session.commit()
    worker = await _login(session, tenant, me, UserRole.EMPLOYEE)

    mine = await client.get("/api/v1/dashboard/summary", headers=_auth(worker))
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["headcount"] is None
    assert body["new_hires_30d"] is None
    assert body["on_leave_today"] == 1  # the teammate, not the stranger

    company = (await client.get("/api/v1/dashboard/summary", headers=_auth(hr_user))).json()
    assert company["headcount"] >= 4
    assert company["new_hires_30d"] is not None
    assert company["on_leave_today"] == 2
