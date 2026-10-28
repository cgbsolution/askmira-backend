"""The attendance calendar's day tags, through the period endpoint.

Two weeks back and three weeks ahead of today, for one person on a Monday to
Friday shift: a day marked present, an absence, an absence with leave
waiting, approved leave, a chosen optional holiday, a public holiday, week
offs, and the days ahead — only what is already known about them is tagged.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceStatus
from app.models.employee import Employee
from app.models.leave import (
    Holiday,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    OptionalHolidaySelection,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.attendance import DayTag, ShiftCreate
from app.services.attendance import review
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _leave(
    session: AsyncSession,
    tenant: Tenant,
    employee: Employee,
    leave_type: LeaveType,
    on: date,
    status: LeaveRequestStatus,
) -> None:
    session.add(
        LeaveRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            leave_type_id=leave_type.id,
            start_date=on,
            end_date=on,
            total_days=Decimal(1),
            status=status,
        )
    )


@pytest.mark.asyncio
async def test_each_day_of_the_month_gets_the_tag_its_record_calls_for(
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
            half_day_threshold_minutes=240,
            full_day_threshold_minutes=420,
            weekly_off_days=[6, 7],
        ),
    )
    today = await attendance_service.local_today(session, hr_user)
    monday = today - timedelta(days=today.weekday() + 14)
    mon, tue, wed, thu, fri, sat, sun = (monday + timedelta(days=i) for i in range(7))
    next_mon = monday + timedelta(days=7)
    end = monday + timedelta(days=34)
    employee.date_of_joining = monday - timedelta(days=30)

    casual = LeaveType(tenant_id=tenant.id, name="Casual Leave", code="CL", is_paid=True)
    session.add(casual)
    await session.flush()
    await _leave(session, tenant, employee, casual, wed, LeaveRequestStatus.PENDING)
    await _leave(session, tenant, employee, casual, thu, LeaveRequestStatus.APPROVED)

    # A future weekday on approved leave, and the first future weekday after it.
    ahead = today + timedelta(days=7 - today.weekday() + 7)  # a Monday two weeks on
    await _leave(session, tenant, employee, casual, ahead, LeaveRequestStatus.APPROVED)

    optional = Holiday(
        tenant_id=tenant.id, name="Karva Chauth", holiday_date=fri, year=fri.year, is_optional=True
    )
    public = Holiday(
        tenant_id=tenant.id, name="Founders' Day", holiday_date=next_mon, year=next_mon.year
    )
    upcoming = Holiday(
        tenant_id=tenant.id,
        name="Guru Nanak Jayanti",
        holiday_date=ahead + timedelta(days=1),
        year=(ahead + timedelta(days=1)).year,
    )
    session.add_all([optional, public, upcoming])
    await session.flush()
    session.add(
        OptionalHolidaySelection(
            tenant_id=tenant.id, employee_id=employee.id, holiday_id=optional.id, year=fri.year
        )
    )
    await session.commit()

    # HR marks Monday present; Tuesday has no punches.
    await attendance_service.raise_regularisation(
        session,
        hr_user,
        employee_id=employee.id,
        attendance_date=mon,
        requested_status=AttendanceStatus.PRESENT,
        reason="Was in the office",
    )

    period = await review.my_period(
        session, hr_user, employee_id=employee.id, start=monday, end=end
    )
    tags = {entry.day: entry.tag for entry in period.day_tags}

    assert tags[mon] is DayTag.FULL_DAY
    assert tags[tue] is DayTag.ABSENT
    assert tags[wed] is DayTag.UNAPPROVED_LEAVE
    assert tags[thu] is DayTag.APPROVED_LEAVE
    assert tags[fri] is DayTag.OPTIONAL_HOLIDAY
    assert tags[sat] is DayTag.WEEK_OFF
    assert tags[sun] is DayTag.WEEK_OFF
    assert tags[next_mon] is DayTag.HOLIDAY

    # Ahead: known week offs, holidays and leave only.
    assert tags[ahead] is DayTag.APPROVED_LEAVE
    assert tags[ahead + timedelta(days=1)] is DayTag.HOLIDAY
    assert ahead + timedelta(days=2) not in tags  # an ordinary working day
    assert tags[ahead + timedelta(days=5)] is DayTag.WEEK_OFF
    assert today not in tags or tags[today] in {DayTag.WEEK_OFF, DayTag.HOLIDAY}

    # The holiday list says which one is optional.
    by_date = {h.holiday_date: h for h in period.holidays}
    assert by_date[fri].is_optional is True
    assert by_date[next_mon].is_optional is False
    assert by_date[next_mon].is_paid is True


@pytest.mark.asyncio
async def test_no_tags_before_the_person_joined(
    session: AsyncSession, hr_user: User, employee: Employee
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
            half_day_threshold_minutes=240,
            full_day_threshold_minutes=420,
            weekly_off_days=[6, 7],
        ),
    )
    today = await attendance_service.local_today(session, hr_user)
    start = today - timedelta(days=20)
    employee.date_of_joining = today - timedelta(days=6)
    await session.commit()

    period = await review.my_period(
        session, hr_user, employee_id=employee.id, start=start, end=today
    )
    assert period.day_tags, "days since joining are tagged"
    assert all(entry.day >= employee.date_of_joining for entry in period.day_tags)
