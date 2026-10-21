"""Slice 02 gates that need a real database.

Idempotent recompute, the payroll attendance lock, retrospective leave approval
and the audited unlock — none of which can be asserted against pure functions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import (
    AttendanceDay,
    AttendanceStatus,
    PunchDirection,
    PunchSource,
    Shift,
    ShiftAssignment,
    WorkLocation,
)
from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.tenant import Tenant
from app.models.user import User
from app.services.attendance import service as attendance_service
from app.services.attendance.service import AttendanceLockError
from tests.integration.conftest import requires_db

pytestmark = requires_db

PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2026, 4, 30)
WORKDAY = date(2026, 4, 6)  # Monday


async def _general_shift(session: AsyncSession, tenant: Tenant, employee: Employee) -> Shift:
    shift = Shift(
        tenant_id=tenant.id,
        code="GEN",
        name="General",
        start_time=time(9, 0),
        end_time=time(18, 0),
        break_minutes=60,
        grace_in_minutes=10,
        half_day_threshold_minutes=240,
        full_day_threshold_minutes=480,
        weekly_off_days=[6, 7],
    )
    session.add(shift)
    await session.flush()
    session.add(
        ShiftAssignment(
            tenant_id=tenant.id,
            employee_id=employee.id,
            shift_id=shift.id,
            effective_from=date(2026, 1, 1),
        )
    )
    await session.commit()
    await session.refresh(shift)
    return shift


async def _punch_full_day(session: AsyncSession, user: User, employee: Employee, day: date) -> None:
    for at, direction in ((time(9, 0), PunchDirection.IN), (time(18, 0), PunchDirection.OUT)):
        await attendance_service.record_punch(
            session,
            user,
            employee_id=employee.id,
            punched_at=datetime.combine(day, at, tzinfo=UTC),
            direction=direction,
            source=PunchSource.WEB,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Idempotent recomputation
# ═══════════════════════════════════════════════════════════════════════════


async def test_recompute_run_twice_produces_identical_rows(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate: byte-identical on a second run.

    Everything but `computed_at` must match — that column records when the
    computation ran, so it is expected to move.
    """
    await _general_shift(session, tenant, employee)
    await _punch_full_day(session, hr_user, employee, WORKDAY)

    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=WORKDAY, end=WORKDAY
    )
    first = (
        await session.scalars(select(AttendanceDay).where(AttendanceDay.employee_id == employee.id))
    ).all()
    snapshot = [
        (
            d.attendance_date,
            d.status,
            d.payable_fraction,
            d.worked_minutes,
            d.first_in,
            d.last_out,
            d.late_by_minutes,
            d.early_by_minutes,
            d.computation_note,
        )
        for d in first
    ]

    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=WORKDAY, end=WORKDAY
    )
    # Re-read through a fresh session so the assertion is against what is
    # actually stored, not against the identity map's copy of it.
    for row in first:
        await session.refresh(row)
    second = (
        await session.scalars(select(AttendanceDay).where(AttendanceDay.employee_id == employee.id))
    ).all()

    assert [
        (
            d.attendance_date,
            d.status,
            d.payable_fraction,
            d.worked_minutes,
            d.first_in,
            d.last_out,
            d.late_by_minutes,
            d.early_by_minutes,
            d.computation_note,
        )
        for d in second
    ] == snapshot


async def test_recompute_does_not_duplicate_rows(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _general_shift(session, tenant, employee)
    await _punch_full_day(session, hr_user, employee, WORKDAY)

    for _ in range(3):
        await attendance_service.recompute_range(
            session, hr_user, employee_ids=[employee.id], start=WORKDAY, end=WORKDAY
        )

    count = await session.scalar(
        select(func.count(AttendanceDay.id)).where(
            AttendanceDay.employee_id == employee.id,
            AttendanceDay.attendance_date == WORKDAY,
        )
    )
    assert count == 1


# ═══════════════════════════════════════════════════════════════════════════
# The payroll lock
# ═══════════════════════════════════════════════════════════════════════════


async def test_lop_query_refuses_an_unlocked_period(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Refusing is the point: assuming zero LOP silently overpays."""
    await _general_shift(session, tenant, employee)

    with pytest.raises(AttendanceLockError) as exc:
        await attendance_service.get_lop_days(
            session, hr_user, employee.id, PERIOD_START, PERIOD_END
        )
    assert "has not been finalised" in str(exc.value)
    assert "overpay" in str(exc.value)


async def test_lop_query_succeeds_once_the_period_is_locked(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _general_shift(session, tenant, employee)
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=PERIOD_START, end=PERIOD_END
    )
    await attendance_service.lock_period(session, hr_user, PERIOD_START, PERIOD_END)

    lop = await attendance_service.get_lop_days(
        session, hr_user, employee.id, PERIOD_START, PERIOD_END
    )
    # No punches for the whole month, so every working day is an absence.
    assert lop > 0


async def test_recompute_refuses_a_locked_period_without_an_unlock(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    from fastapi import HTTPException

    await _general_shift(session, tenant, employee)
    await attendance_service.lock_period(session, hr_user, PERIOD_START, PERIOD_END)

    with pytest.raises(HTTPException) as exc:
        await attendance_service.recompute_range(
            session, hr_user, employee_ids=[employee.id], start=WORKDAY, end=WORKDAY
        )
    assert exc.value.status_code == 409
    assert "locked" in exc.value.detail


async def test_unlock_writes_an_audit_entry_naming_the_actor(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    lock = await attendance_service.lock_period(session, hr_user, PERIOD_START, PERIOD_END)
    await attendance_service.unlock_period(
        session, hr_user, lock.id, "Missed punches reported by two employees"
    )

    entries = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "attendance_period_lock",
                AuditLog.entity_id == lock.id,
                AuditLog.action == AuditAction.UPDATE,
            )
        )
    ).all()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_user_id == hr_user.id
    assert "Unlocked" in entry.summary
    assert "Missed punches" in entry.changes["reason"]


async def test_a_punch_is_still_accepted_for_a_locked_period(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """People forget. The punch lands; it just cannot move the computed day."""
    await _general_shift(session, tenant, employee)
    await attendance_service.lock_period(session, hr_user, PERIOD_START, PERIOD_END)

    punch = await attendance_service.record_punch(
        session,
        hr_user,
        employee_id=employee.id,
        punched_at=datetime.combine(WORKDAY, time(9, 0), tzinfo=UTC),
        direction=PunchDirection.IN,
        source=PunchSource.WEB,
    )
    assert punch.id is not None


# ═══════════════════════════════════════════════════════════════════════════
# Retrospective leave approval
# ═══════════════════════════════════════════════════════════════════════════


async def test_retrospective_leave_approval_changes_lop_on_recompute(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Someone was marked absent, then their leave was approved after the fact.
    The recompute must turn that day from unpaid absence into paid leave.
    """
    await _general_shift(session, tenant, employee)

    # No punches on the 6th → absent.
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=WORKDAY, end=WORKDAY
    )
    await attendance_service.lock_period(session, hr_user, WORKDAY, WORKDAY)
    before = await attendance_service.get_lop_days(session, hr_user, employee.id, WORKDAY, WORKDAY)
    assert before == Decimal(1)

    # Leave approved retrospectively.
    leave_type = LeaveType(tenant_id=tenant.id, name="Casual Leave", code="CL", is_paid=True)
    session.add(leave_type)
    await session.flush()
    session.add(
        LeaveRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            leave_type_id=leave_type.id,
            start_date=WORKDAY,
            end_date=WORKDAY,
            total_days=Decimal(1),
            status=LeaveRequestStatus.APPROVED,
        )
    )
    await session.commit()

    # The period is locked, so the recompute needs the explicit unlock path.
    locks = await attendance_service.active_lock_covering(session, hr_user, WORKDAY, WORKDAY)
    await attendance_service.unlock_period(
        session, hr_user, locks.id, "Retrospective leave approval"
    )
    await attendance_service.recompute_range(
        session, hr_user, employee_ids=[employee.id], start=WORKDAY, end=WORKDAY
    )
    await attendance_service.lock_period(session, hr_user, WORKDAY, WORKDAY)

    after = await attendance_service.get_lop_days(session, hr_user, employee.id, WORKDAY, WORKDAY)
    assert after == Decimal(0), "approved paid leave must clear the loss of pay"

    day = await session.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee.id,
            AttendanceDay.attendance_date == WORKDAY,
        )
    )
    assert day.status is AttendanceStatus.ON_LEAVE


# ═══════════════════════════════════════════════════════════════════════════
# Geofencing
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_punch_outside_the_geofence_is_stored_and_flagged(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Never rejected. An employee who cannot punch stops trying."""
    session.add(
        WorkLocation(
            tenant_id=tenant.id,
            name="Bengaluru office",
            latitude=Decimal("12.9716000"),
            longitude=Decimal("77.5946000"),
            radius_metres=200,
        )
    )
    await session.commit()

    # Koramangala, several kilometres away.
    punch = await attendance_service.record_punch(
        session,
        hr_user,
        employee_id=employee.id,
        punched_at=datetime.now(UTC),
        direction=PunchDirection.IN,
        source=PunchSource.MOBILE_GEO,
        latitude=12.9352,
        longitude=77.6245,
    )

    assert punch.id is not None, "the punch must be stored"
    assert punch.outside_geofence is True
    assert punch.nearest_location_metres > 200


async def test_a_punch_inside_the_geofence_is_not_flagged(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    session.add(
        WorkLocation(
            tenant_id=tenant.id,
            name="Bengaluru office",
            latitude=Decimal("12.9716000"),
            longitude=Decimal("77.5946000"),
            radius_metres=200,
        )
    )
    await session.commit()

    punch = await attendance_service.record_punch(
        session,
        hr_user,
        employee_id=employee.id,
        punched_at=datetime.now(UTC),
        direction=PunchDirection.IN,
        source=PunchSource.MOBILE_GEO,
        latitude=12.9717,
        longitude=77.5947,
    )
    assert punch.outside_geofence is False


async def test_a_punch_with_no_coordinates_is_not_flagged_as_outside(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A biometric punch has no coordinates. That is not "outside"."""
    session.add(
        WorkLocation(
            tenant_id=tenant.id,
            name="Bengaluru office",
            latitude=Decimal("12.9716000"),
            longitude=Decimal("77.5946000"),
            radius_metres=200,
        )
    )
    await session.commit()

    punch = await attendance_service.record_punch(
        session,
        hr_user,
        employee_id=employee.id,
        punched_at=datetime.now(UTC),
        direction=PunchDirection.IN,
        source=PunchSource.BIOMETRIC,
        device_id="DEV-01",
    )
    assert punch.outside_geofence is False
    assert punch.nearest_location_metres is None
