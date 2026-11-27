"""Holiday, Week off and Comp off in the Request time off list.

Besides its leave, every workspace's list offers Holiday and Week off — not
leave but a paid day off of that kind, asked for and approved the same way,
with no balance — and Comp off, which uses the comp-off balance like any
leave. An approved Holiday or Week off request marks its days so in
attendance, paid; a punch on such a day is not overtime.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceDay, AttendanceStatus, PunchDirection, PunchSource
from app.models.employee import Employee
from app.models.leave import LeaveBalance, LeaveRequestStatus, LeaveType
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.leave import (
    BalanceAdjustmentCreate,
    LeavePolicyCreate,
    LeaveRequestCreate,
    LeaveTypeUpdate,
)
from app.services import auth as auth_service
from app.services.attendance import service as attendance_service
from app.services.leave import balance_report, ledger
from app.services.leave import service as leave_service
from tests.integration.conftest import requires_db
from tests.integration.test_attendance_w9_flow import _general_shift

pytestmark = requires_db

MONDAY = date(2026, 4, 6)
TUESDAY = MONDAY + timedelta(days=1)
SATURDAY = date(2026, 4, 11)


def _ist(day: date, clock: str) -> datetime:
    hours, minutes = (int(part) for part in clock.split(":"))
    return datetime.combine(day, time(hours, minutes), tzinfo=UTC) - timedelta(hours=5, minutes=30)


async def _types(session: AsyncSession, hr_user: User) -> dict[str, LeaveType]:
    await leave_service.ensure_day_off_types(session, hr_user)
    await session.commit()
    rows = await leave_service.list_types(session, hr_user)
    return {(t.marks_day_as or ("comp_off" if t.is_comp_off else t.code)): t for t in rows}


async def _status(session: AsyncSession, employee: Employee, on: date) -> AttendanceDay | None:
    return await session.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee.id, AttendanceDay.attendance_date == on
        )
    )


@pytest.mark.asyncio
async def test_every_workspace_offers_holiday_week_off_and_comp_off(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    # A workspace whose own types already use "WO" and take comp-off credits.
    session.add_all(
        [
            LeaveType(tenant_id=tenant.id, name="Work outside", code="WO"),
            LeaveType(tenant_id=tenant.id, name="Comp Off", code="CMP", is_comp_off=True),
        ]
    )
    await session.commit()
    created = await leave_service.ensure_day_off_types(session, hr_user)
    await session.commit()
    assert sorted((t.name, t.code, t.marks_day_as) for t in created) == [
        ("Holiday", "HO", "holiday"),
        ("Week off", "WOFF", "week_off"),
    ]
    # Its own comp-off type is kept; twice adds nothing.
    assert await leave_service.ensure_day_off_types(session, hr_user) == []

    # A new workspace has all three from the start.
    new_tenant, admin = await auth_service.create_workspace(
        session,
        company_name="Fresh Co",
        admin_full_name="Fresh Admin",
        admin_email=f"admin-{uuid.uuid4().hex[:6]}@fresh.test",
        admin_phone=None,
        password_hash="not-a-real-hash",
    )
    await session.commit()
    names = sorted(
        (t.name, t.code, t.is_comp_off, t.marks_day_as)
        for t in (
            await session.scalars(select(LeaveType).where(LeaveType.tenant_id == new_tenant.id))
        ).all()
    )
    assert names == [
        ("Comp off", "CO", True, None),
        ("Holiday", "HO", False, "holiday"),
        ("Week off", "WO", False, "week_off"),
    ]


@pytest.mark.asyncio
async def test_a_holiday_or_week_off_request_needs_no_balance_and_marks_the_day(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _general_shift(session, hr_user)
    types = await _types(session, hr_user)
    # Worked on the Tuesday as well: a punch on an approved day off is noise.
    for clock, direction in (("09:30", PunchDirection.IN), ("18:30", PunchDirection.OUT)):
        await attendance_service.record_punch(
            session,
            hr_user,
            employee_id=employee.id,
            punched_at=_ist(TUESDAY, clock),
            direction=direction,
            source=PunchSource.WEB,
        )

    requests = {}
    for kind, on in (("holiday", MONDAY), ("week_off", TUESDAY)):
        payload = LeaveRequestCreate(
            leave_type_id=types[kind].id, start_date=on, end_date=on, employee_id=employee.id
        )
        preview = await leave_service.preview(session, hr_user, payload)
        # Nobody gave this person a policy or a balance: no matter.
        assert preview.allowed, preview.problems
        assert preview.available_before is None
        assert any("No leave balance is used" in w for w in preview.warnings)
        requests[kind] = await leave_service.apply(session, hr_user, payload)
        assert requests[kind].total_days == 1

    for kind in ("holiday", "week_off"):
        decided = await leave_service.decide(
            session, hr_user, requests[kind].id, approve=True, note=None
        )
        assert decided.status is LeaveRequestStatus.APPROVED

    monday = await _status(session, employee, MONDAY)
    tuesday = await _status(session, employee, TUESDAY)
    assert monday.status is AttendanceStatus.HOLIDAY
    assert float(monday.payable_fraction) == 1
    assert tuesday.status is AttendanceStatus.WEEK_OFF
    assert float(tuesday.payable_fraction) == 1
    assert tuesday.worked_minutes == 0 and tuesday.overtime_minutes == 0

    # No balance was created or taken, and none is shown.
    assert (
        await session.scalars(select(LeaveBalance).where(LeaveBalance.employee_id == employee.id))
    ).all() == []
    shown = await leave_service.balances(
        session, hr_user, employee_id=employee.id, year=MONDAY.year
    )
    assert shown == []
    columns = await balance_report.team_balances(session, hr_user, year=MONDAY.year)
    assert {c.name for c in columns.leave_types} == {"Comp off"}

    # HR cancels the Holiday: the day is worked out again, nothing to give back.
    await leave_service.cancel(session, hr_user, requests["holiday"].id)
    again = await _status(session, employee, MONDAY)
    assert again.status is AttendanceStatus.ABSENT


@pytest.mark.asyncio
async def test_a_day_off_request_is_refused_where_it_means_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _general_shift(session, hr_user)
    types = await _types(session, hr_user)

    # Saturday is already a weekly off.
    on_saturday = await leave_service.preview(
        session,
        hr_user,
        LeaveRequestCreate(
            leave_type_id=types["week_off"].id,
            start_date=SATURDAY,
            end_date=SATURDAY,
            employee_id=employee.id,
        ),
    )
    assert not on_saturday.allowed
    assert any("nothing to request" in p for p in on_saturday.problems)

    # Always a full day.
    half = await leave_service.preview(
        session,
        hr_user,
        LeaveRequestCreate(
            leave_type_id=types["holiday"].id,
            start_date=MONDAY,
            end_date=MONDAY,
            is_half_day=True,
            half_day_session="first_half",
            employee_id=employee.id,
        ),
    )
    assert not half.allowed

    # Comp off still needs comp-off days.
    comp_off = await leave_service.preview(
        session,
        hr_user,
        LeaveRequestCreate(
            leave_type_id=types["comp_off"].id,
            start_date=MONDAY,
            end_date=MONDAY,
            employee_id=employee.id,
        ),
    )
    assert not comp_off.allowed
    assert any("No balance" in p for p in comp_off.problems)


@pytest.mark.asyncio
async def test_holiday_and_week_off_take_no_policy_adjustment_or_unpaid_edit(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    types = await _types(session, hr_user)
    holiday = types["holiday"]

    with pytest.raises(HTTPException) as policy:
        await leave_service.create_policy(
            session,
            hr_user,
            LeavePolicyCreate(
                leave_type_id=holiday.id,
                name="Holidays",
                annual_quota_days=5,
                effective_from=MONDAY,
            ),
        )
    assert policy.value.status_code == 422

    with pytest.raises(HTTPException) as adjust:
        await ledger.adjust(
            session,
            hr_user,
            BalanceAdjustmentCreate(
                employee_id=employee.id,
                leave_type_id=holiday.id,
                year=MONDAY.year,
                kind="credit",
                days=2,
                reason="Trying",
            ),
        )
    assert adjust.value.status_code == 422

    for change in ({"is_paid": False}, {"allows_half_day": True}, {"is_comp_off": True}):
        with pytest.raises(HTTPException) as edit:
            await leave_service.update_type(session, hr_user, holiday.id, LeaveTypeUpdate(**change))
        assert edit.value.status_code == 422
    renamed = await leave_service.update_type(
        session, hr_user, holiday.id, LeaveTypeUpdate(name="Client holiday")
    )
    assert renamed.name == "Client holiday"
    assert renamed.marks_day_as == "holiday"
