"""Attendance that works for a company that has not configured everything.

The default shift, per-state holidays, applied missed-punch times, multi-day
work-from-home, and forgot-password — each against a real database.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import (
    AttendanceDay,
    AttendanceStatus,
    PunchDirection,
    PunchSource,
    RegularisationKind,
    Shift,
)
from app.models.employee import Employee
from app.models.leave import Holiday
from app.models.tenant import Tenant
from app.models.user import PasswordResetToken, User, UserRole
from app.schemas.attendance import ShiftCreate
from app.services import auth as auth_service
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from tests.integration.conftest import requires_db

pytestmark = requires_db

MONDAY = date(2026, 4, 6)
SATURDAY = date(2026, 4, 11)


async def _general_shift(session: AsyncSession, hr_user: User) -> Shift:
    """Created through the service, so the first shift becomes the default."""
    return await attendance_setup.create_shift(
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


def _ist(day: date, clock: str) -> datetime:
    hours, minutes = (int(part) for part in clock.split(":"))
    # 05:30 ahead of UTC.
    return datetime.combine(day, time(hours, minutes), tzinfo=UTC) - timedelta(hours=5, minutes=30)


@pytest.mark.asyncio
async def test_the_first_shift_becomes_the_default_and_judges_unassigned_people(
    session: AsyncSession, hr_user: User, employee: Employee
):
    shift = await _general_shift(session, hr_user)
    assert shift.is_default

    for clock, direction in (("09:35", PunchDirection.IN), ("18:40", PunchDirection.OUT)):
        await attendance_service.record_punch(
            session, hr_user, employee_id=employee.id, punched_at=_ist(MONDAY, clock),
            direction=direction, source=PunchSource.WEB,
        )
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=MONDAY, end=SATURDAY
    )
    days = {
        d.attendance_date: d
        for d in (await session.scalars(select(AttendanceDay).where(
            AttendanceDay.employee_id == employee.id
        ))).all()
    }
    assert days[MONDAY].status is AttendanceStatus.PRESENT
    assert days[MONDAY].shift_id == shift.id
    assert days[MONDAY].late_by_minutes == 0  # 5 minutes inside a 15-minute grace
    assert days[SATURDAY].status is AttendanceStatus.WEEK_OFF
    # No punches on a weekday with a shift in force is an absence, not "cannot be judged".
    assert days[MONDAY + timedelta(days=1)].status is AttendanceStatus.ABSENT


@pytest.mark.asyncio
async def test_future_days_are_not_written_and_days_before_joining_are_skipped(
    session: AsyncSession, hr_user: User, employee: Employee
):
    await _general_shift(session, hr_user)
    employee.date_of_joining = MONDAY
    await session.commit()

    future = datetime.now(UTC).date() + timedelta(days=5)
    written = await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=MONDAY - timedelta(days=3),
        end=MONDAY,
    )
    assert written == 1  # only the joining day itself
    written = await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=future, end=future
    )
    assert written == 0


@pytest.mark.asyncio
async def test_a_holiday_applies_only_in_its_own_state(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _general_shift(session, hr_user)
    session.add_all(
        [
            Holiday(tenant_id=tenant.id, name="Karnataka day", holiday_date=MONDAY,
                    year=2026, state_code="KA"),
            Holiday(tenant_id=tenant.id, name="Maharashtra day",
                    holiday_date=MONDAY + timedelta(days=1), year=2026, state_code="MH"),
        ]
    )
    await session.commit()
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=MONDAY,
        end=MONDAY + timedelta(days=1),
    )
    days = {
        d.attendance_date: d.status
        for d in (await session.scalars(select(AttendanceDay).where(
            AttendanceDay.employee_id == employee.id
        ))).all()
    }
    # The employee works in Bengaluru (KA).
    assert days[MONDAY] is AttendanceStatus.HOLIDAY
    assert days[MONDAY + timedelta(days=1)] is AttendanceStatus.ABSENT


@pytest.mark.asyncio
async def test_an_approved_missed_punch_applies_the_corrected_time(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _general_shift(session, hr_user)
    await attendance_service.record_punch(
        session, hr_user, employee_id=employee.id, punched_at=_ist(MONDAY, "09:40"),
        direction=PunchDirection.IN, source=PunchSource.WEB,
    )
    employee_login = User(
        tenant_id=tenant.id, email="anita.login@acme.test", password_hash="x",
        full_name="Anita Sharma", role=UserRole.EMPLOYEE, is_active=True,
        employee_id=employee.id,
    )
    session.add(employee_login)
    await session.commit()

    request = await attendance_service.raise_regularisation(
        session, employee_login, employee_id=employee.id, attendance_date=MONDAY,
        requested_status=AttendanceStatus.PRESENT, reason="Forgot to punch out",
        kind=RegularisationKind.MISSED_PUNCH, requested_last_out=_ist(MONDAY, "18:45"),
    )
    await attendance_service.decide_regularisation(session, hr_user, request.id, approve=True)

    day = await session.scalar(select(AttendanceDay).where(
        AttendanceDay.employee_id == employee.id, AttendanceDay.attendance_date == MONDAY
    ))
    assert day is not None
    assert day.status is AttendanceStatus.PRESENT
    assert day.first_in == _ist(MONDAY, "09:40")
    assert day.last_out == _ist(MONDAY, "18:45")
    assert day.worked_minutes == 485  # 09:40-18:45 less the hour's break
    assert day.is_regularised


@pytest.mark.asyncio
async def test_work_from_home_over_a_week_skips_the_weekend(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _general_shift(session, hr_user)
    login = User(
        tenant_id=tenant.id, email="anita.wfh@acme.test", password_hash="x",
        full_name="Anita Sharma", role=UserRole.EMPLOYEE, is_active=True,
        employee_id=employee.id,
    )
    session.add(login)
    await session.commit()
    start = datetime.now(UTC).date() + timedelta(days=7)
    start -= timedelta(days=start.weekday())  # a Monday a week or two ahead
    created = await attendance_service.raise_regularisation_range(
        session, login, employee_id=employee.id, start=start, end=start + timedelta(days=6),
        kind=RegularisationKind.WORK_FROM_HOME, reason="Home internet install",
    )
    assert [row.attendance_date.weekday() for row in created] == [0, 1, 2, 3, 4]

    with pytest.raises(HTTPException) as refused:
        await attendance_service.raise_regularisation(
            session, login, employee_id=employee.id, attendance_date=start,
            requested_status=AttendanceStatus.PRESENT, reason="again",
            kind=RegularisationKind.STATUS_CHANGE,
        )
    assert refused.value.status_code == 422  # a correction cannot be for the future


@pytest.mark.asyncio
async def test_a_reset_link_works_once(session: AsyncSession, tenant: Tenant, hr_user: User):
    await auth_service.request_password_reset(session, hr_user.email)
    token_row = await session.scalar(select(PasswordResetToken))
    assert token_row is not None and token_row.used_at is None

    # The token itself is never stored; mint a known one onto the row to use it.
    known = "known-test-token-value"
    token_row.token_hash = auth_service._token_hash(known)
    await session.commit()

    await auth_service.reset_password(session, known, "BrandNewPass123")
    await session.refresh(hr_user)
    assert hr_user.must_change_password is False
    assert hr_user.tokens_valid_from is not None

    with pytest.raises(HTTPException):
        await auth_service.reset_password(session, known, "AnotherPass1234")
