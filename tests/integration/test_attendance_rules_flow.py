"""The admin's attendance rules: late marks cost a half day, corrections are limited,
and HR marks any days picked on the calendar at once.

- Every Nth late mark in a month is a half day (the Nth, 2Nth…, never the
  first); the count follows corrections, and switching the rule off undoes it.
- An employee may ask for N corrections a month; HR is not limited, and
  neither are on-duty and work-from-home requests.
- HR picks days on the calendar and marks them Full day, Half day or Absent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import (
    AttendanceDay,
    AttendancePeriodLock,
    AttendanceStatus,
    PunchDirection,
    PunchSource,
    RegularisationKind,
    RegularisationStatus,
)
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.attendance import AttendanceSettings, ShiftCreate
from app.services.attendance import review
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from tests.integration.conftest import requires_db

pytestmark = requires_db

IST = timedelta(hours=5, minutes=30)


async def _shift(session: AsyncSession, hr_user: User) -> None:
    await attendance_setup.create_shift(
        session,
        hr_user,
        ShiftCreate(
            code="GEN",
            name="General",
            start_time=time(9, 30),
            end_time=time(18, 30),
            break_minutes=60,
            grace_in_minutes=10,
            half_day_threshold_minutes=240,
            full_day_threshold_minutes=420,
            weekly_off_days=[6, 7],
        ),
    )


def _last_month() -> tuple[date, date]:
    today = datetime.now(UTC).date()
    last = today.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def _weekdays(first: date, last: date) -> list[date]:
    return [
        first + timedelta(days=n)
        for n in range((last - first).days + 1)
        if (first + timedelta(days=n)).weekday() < 5
    ]


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time in India as UTC."""
    return datetime.combine(day, time(hour, minute), tzinfo=UTC) - IST


async def _punch_day(
    session: AsyncSession, hr_user: User, employee: Employee, day: date, *, late: bool
) -> None:
    for at, direction in (
        (_at(day, 10, 0) if late else _at(day, 9, 30), PunchDirection.IN),
        (_at(day, 19, 0), PunchDirection.OUT),
    ):
        await attendance_service.record_punch(
            session,
            hr_user,
            employee_id=employee.id,
            punched_at=at,
            direction=direction,
            source=PunchSource.MANUAL,
            commit=False,
        )
    await session.commit()


async def _rules(session: AsyncSession, tenant: Tenant, **values) -> None:
    settings = dict(tenant.settings or {})
    settings["attendance"] = {**(settings.get("attendance") or {}), **values}
    tenant.settings = settings
    await session.commit()


async def _days(session: AsyncSession, employee: Employee, first: date, last: date):
    rows = (
        await session.scalars(
            select(AttendanceDay)
            .where(
                AttendanceDay.employee_id == employee.id,
                AttendanceDay.attendance_date.between(first, last),
            )
            .order_by(AttendanceDay.attendance_date)
            .execution_options(populate_existing=True)
        )
    ).all()
    return {row.attendance_date: row for row in rows}


@pytest.mark.asyncio
async def test_every_third_late_mark_in_a_month_is_a_half_day(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _shift(session, hr_user)
    first, last = _last_month()
    workdays = _weekdays(first, last)
    late_days = workdays[:7]
    for day in late_days:
        await _punch_day(session, hr_user, employee, day, late=True)
    await _punch_day(session, hr_user, employee, workdays[7], late=False)
    await _rules(session, tenant, late_marks_per_half_day=3, late_marks_rule_from=first.isoformat())

    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=first, end=last
    )
    days = await _days(session, employee, first, last)
    half = [d for d in late_days if days[d].status is AttendanceStatus.HALF_DAY]
    assert half == [late_days[2], late_days[5]]  # the 3rd and the 6th, never the 1st
    third = days[late_days[2]]
    assert third.late_mark_half_day is True
    assert float(third.payable_fraction) == 0.5
    assert "late for the 3rd time" in third.computation_note
    assert days[late_days[0]].status is AttendanceStatus.PRESENT
    assert days[late_days[6]].status is AttendanceStatus.PRESENT  # the 7th of 7
    assert days[workdays[7]].status is AttendanceStatus.PRESENT
    assert days[workdays[7]].late_by_minutes == 0

    # Recomputing changes nothing: the rule is idempotent.
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=first, end=last
    )
    again = await _days(session, employee, first, last)
    assert [d for d in late_days if again[d].status is AttendanceStatus.HALF_DAY] == half

    # HR corrects the first late arrival: the half day moves to the next 3rd mark.
    await attendance_service.raise_regularisation(
        session,
        hr_user,
        employee_id=employee.id,
        attendance_date=late_days[0],
        requested_status=AttendanceStatus.PRESENT,
        kind=RegularisationKind.MISSED_PUNCH,
        requested_first_in=_at(late_days[0], 9, 25),
        reason="Was at the client from 9:25",
    )
    moved = await _days(session, employee, first, last)
    assert moved[late_days[0]].late_by_minutes == 0
    # Six late marks remain (the 2nd to the 7th day): their 3rd and 6th are half days.
    assert [d for d in late_days if moved[d].status is AttendanceStatus.HALF_DAY] == [
        late_days[3],
        late_days[6],
    ]
    reverted = moved[late_days[2]]
    assert reverted.status is AttendanceStatus.PRESENT
    assert reverted.late_mark_half_day is False
    assert float(reverted.payable_fraction) == 1
    assert not (reverted.computation_note or "").startswith("Half day: late")


@pytest.mark.asyncio
async def test_the_rule_skips_locked_days_and_months_before_it_started(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _shift(session, hr_user)
    first, last = _last_month()
    late_days = _weekdays(first, last)[:3]
    for day in late_days:
        await _punch_day(session, hr_user, employee, day, late=True)

    # Switched on from this month: last month is not changed.
    this_month = (last + timedelta(days=1)).isoformat()
    await _rules(session, tenant, late_marks_per_half_day=3, late_marks_rule_from=this_month)
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=first, end=last
    )
    days = await _days(session, employee, first, last)
    assert all(days[d].status is AttendanceStatus.PRESENT for d in late_days)

    # From last month, but the 3rd late day is in a locked (paid) period.
    await _rules(session, tenant, late_marks_rule_from=first.isoformat())
    session.add(
        AttendancePeriodLock(
            tenant_id=tenant.id,
            period_start=late_days[2],
            period_end=late_days[2],
            locked_by_user_id=hr_user.id,
            locked_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await attendance_service.recompute_range(
        session,
        hr_user,
        employee_ids=[employee.id],
        start=first,
        end=late_days[1],
    )
    days = await _days(session, employee, first, last)
    assert days[late_days[2]].status is AttendanceStatus.PRESENT


@pytest.mark.asyncio
async def test_only_an_admin_sets_the_rules_and_switching_off_undoes_this_month(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    sub_admin = User(
        tenant_id=tenant.id,
        email="sub@acme.test",
        password_hash="x",
        full_name="Sub Admin",
        role=UserRole.SUB_ADMIN,
        is_active=True,
    )
    session.add(sub_admin)
    await session.commit()
    with pytest.raises(HTTPException) as refused:
        await attendance_setup.update_attendance_settings(
            session, sub_admin, AttendanceSettings(late_marks_per_half_day=3)
        )
    assert refused.value.status_code == 403
    # A sub-admin still changes where punches are recorded.
    saved = await attendance_setup.update_attendance_settings(
        session, sub_admin, AttendanceSettings(location_mode="required")
    )
    assert saved.location_mode == "required"

    saved = await attendance_setup.update_attendance_settings(
        session,
        hr_user,
        AttendanceSettings(late_marks_per_half_day=3, monthly_correction_limit=2),
    )
    today = await attendance_service.local_today(session, hr_user)
    assert saved.late_marks_rule_from == today.replace(day=1)
    assert saved.monthly_correction_limit == 2
    assert saved.location_mode == "required"  # left as it was

    # A half day the rule made this month goes back when the rule is switched off.
    await _shift(session, hr_user)
    day = today.replace(day=1)
    session.add(
        AttendanceDay(
            tenant_id=tenant.id,
            employee_id=employee.id,
            attendance_date=day,
            status=AttendanceStatus.HALF_DAY,
            payable_fraction=0.5,
            late_by_minutes=30,
            late_mark_half_day=True,
            computation_note=(
                "Half day: late for the 3rd time in X. Without the late-mark rule: Late by 30m"
            ),
            computed_at=datetime.now(UTC),
        )
    )
    await session.commit()
    off = await attendance_setup.update_attendance_settings(
        session, hr_user, AttendanceSettings(late_marks_per_half_day=None)
    )
    assert off.late_marks_per_half_day is None
    assert off.late_marks_rule_from is None
    row = (await _days(session, employee, day, day))[day]
    assert row.status is AttendanceStatus.PRESENT
    assert row.late_mark_half_day is False
    assert row.computation_note == "Late by 30m"


@pytest.mark.asyncio
async def test_an_employee_may_ask_for_n_corrections_a_month(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _shift(session, hr_user)
    person = User(
        tenant_id=tenant.id,
        email="anita@acme.test",
        password_hash="x",
        full_name="Anita Sharma",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(person)
    await session.commit()
    await _rules(session, tenant, monthly_correction_limit=2)
    first, last = _last_month()
    days = _weekdays(first, last)

    async def ask(day: date, kind=RegularisationKind.MISSED_PUNCH):
        return await attendance_service.raise_regularisation(
            session,
            person,
            employee_id=employee.id,
            attendance_date=day,
            requested_status=AttendanceStatus.PRESENT,
            kind=kind,
            requested_first_in=_at(day, 9, 30) if kind is RegularisationKind.MISSED_PUNCH else None,
            reason="Forgot to punch in",
        )

    one = await ask(days[0])
    await ask(days[1])
    with pytest.raises(HTTPException) as refused:
        await ask(days[2])
    assert refused.value.status_code == 422
    assert "all 2 correction requests" in refused.value.detail
    # On duty and work from home are not corrections: not limited.
    await ask(days[3], RegularisationKind.ON_DUTY)

    # A withdrawn request gives its place back.
    await attendance_service.cancel_regularisation(session, person, one.id)
    await ask(days[2])

    # HR's own corrections are not limited, and do not use the person's allowance.
    await attendance_service.raise_regularisation(
        session,
        hr_user,
        employee_id=employee.id,
        attendance_date=days[4],
        requested_status=AttendanceStatus.HALF_DAY,
        reason="Marked by HR",
    )
    period = await review.my_period(session, person, employee_id=employee.id, start=first, end=last)
    assert period.corrections_limit == 2
    assert period.corrections_used == 2

    # Next month starts again.
    next_month = last + timedelta(days=1)
    assert await attendance_service.corrections_used(session, person, employee.id, next_month) == 0
    assert one.status is RegularisationStatus.CANCELLED


@pytest.mark.asyncio
async def test_hr_marks_the_days_picked_on_the_calendar(
    session: AsyncSession, hr_user: User, employee: Employee
):
    await _shift(session, hr_user)
    first, last = _last_month()
    workdays = _weekdays(first, last)
    saturday = next(
        first + timedelta(days=n) for n in range(7) if (first + timedelta(days=n)).weekday() == 5
    )
    picked = [workdays[0], workdays[2], workdays[4], saturday]

    created = await attendance_service.raise_regularisation_range(
        session,
        hr_user,
        employee_id=employee.id,
        start=min(picked),
        end=max(picked),
        days=picked,
        kind=RegularisationKind.STATUS_CHANGE,
        requested_status=AttendanceStatus.HALF_DAY,
        reason="Marked half day by HR",
    )
    marked = sorted(row.attendance_date for row in created)
    assert marked == [workdays[0], workdays[2], workdays[4]]  # the Saturday is a week off
    days = await _days(session, employee, first, last)
    for day in marked:
        assert days[day].status is AttendanceStatus.HALF_DAY
    # Days between the picks are not touched.
    assert days.get(workdays[1]) is None or days[workdays[1]].status is not (
        AttendanceStatus.HALF_DAY
    )
