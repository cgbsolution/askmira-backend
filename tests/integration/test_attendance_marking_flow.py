"""HR marking someone's days present, half day or absent, one day or a stretch at once.

The admin's correction is the decision: applied at once, audited, the day
recomputed. A stretch skips week offs, holidays, approved leave and days
already recorded that way.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import (
    AttendanceDay,
    AttendanceRegularisation,
    AttendanceStatus,
    RegularisationKind,
    RegularisationStatus,
)
from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.attendance import ShiftCreate
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _general_shift(session: AsyncSession, hr_user: User) -> None:
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


def _past_monday():
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday() + 14)
    return monday


async def _status(session: AsyncSession, employee: Employee, day) -> AttendanceStatus | None:
    row = await session.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee.id, AttendanceDay.attendance_date == day
        )
    )
    return row.status if row else None


@pytest.mark.asyncio
async def test_hr_marks_a_week_present_and_the_weekend_is_left_alone(
    session: AsyncSession, hr_user: User, employee: Employee
):
    await _general_shift(session, hr_user)
    monday = _past_monday()
    # Tuesday is already present: it is left as it is.
    await attendance_service.raise_regularisation(
        session,
        hr_user,
        employee_id=employee.id,
        attendance_date=monday + timedelta(days=1),
        requested_status=AttendanceStatus.PRESENT,
        reason="Was in the office",
    )

    created = await attendance_service.raise_regularisation_range(
        session,
        hr_user,
        employee_id=employee.id,
        start=monday,
        end=monday + timedelta(days=6),
        kind=RegularisationKind.STATUS_CHANGE,
        requested_status=AttendanceStatus.PRESENT,
        reason="Marked present by HR",
    )

    assert sorted(row.attendance_date.weekday() for row in created) == [0, 2, 3, 4]
    assert all(row.status is RegularisationStatus.APPROVED for row in created)
    assert all(row.approver_user_id == hr_user.id for row in created)
    for offset in range(5):
        assert await _status(session, employee, monday + timedelta(days=offset)) is (
            AttendanceStatus.PRESENT
        )
    for offset in (5, 6):
        assert await _status(session, employee, monday + timedelta(days=offset)) is not (
            AttendanceStatus.PRESENT
        )
    applied = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "attendance_regularisation",
                AuditLog.action == AuditAction.APPROVE,
            )
        )
    ).all()
    assert len(applied) == 5  # Tuesday's single correction and the four in the range


@pytest.mark.asyncio
async def test_hr_marks_a_half_day(session: AsyncSession, hr_user: User, employee: Employee):
    await _general_shift(session, hr_user)
    wednesday = _past_monday() + timedelta(days=2)
    created = await attendance_service.raise_regularisation_range(
        session,
        hr_user,
        employee_id=employee.id,
        start=wednesday,
        end=wednesday,
        kind=RegularisationKind.STATUS_CHANGE,
        requested_status=AttendanceStatus.HALF_DAY,
        reason="Left after lunch",
    )
    assert len(created) == 1
    assert await _status(session, employee, wednesday) is AttendanceStatus.HALF_DAY


@pytest.mark.asyncio
async def test_an_employee_cannot_mark_a_range(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _general_shift(session, hr_user)
    login = User(
        tenant_id=tenant.id,
        email="anita.marks@acme.test",
        password_hash="x",
        full_name="Anita Sharma",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(login)
    await session.commit()
    monday = _past_monday()
    with pytest.raises(HTTPException) as refused:
        await attendance_service.raise_regularisation_range(
            session,
            login,
            employee_id=employee.id,
            start=monday,
            end=monday + timedelta(days=4),
            kind=RegularisationKind.STATUS_CHANGE,
            requested_status=AttendanceStatus.PRESENT,
            reason="All present",
        )
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_days_ahead_are_not_marked(session: AsyncSession, hr_user: User, employee: Employee):
    await _general_shift(session, hr_user)
    today = datetime.now(UTC).date()
    with pytest.raises(HTTPException) as refused:
        await attendance_service.raise_regularisation_range(
            session,
            hr_user,
            employee_id=employee.id,
            start=today,
            end=today + timedelta(days=3),
            kind=RegularisationKind.STATUS_CHANGE,
            requested_status=AttendanceStatus.PRESENT,
            reason="Will be present",
        )
    assert refused.value.status_code == 422


@pytest.mark.asyncio
async def test_a_pending_request_is_decided_before_the_range_is_marked(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _general_shift(session, hr_user)
    login = User(
        tenant_id=tenant.id,
        email="anita.pending@acme.test",
        password_hash="x",
        full_name="Anita Sharma",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(login)
    await session.commit()
    monday = _past_monday()
    await attendance_service.raise_regularisation(
        session,
        login,
        employee_id=employee.id,
        attendance_date=monday + timedelta(days=3),
        requested_status=AttendanceStatus.PRESENT,
        reason="Forgot to punch",
    )
    with pytest.raises(HTTPException) as refused:
        await attendance_service.raise_regularisation_range(
            session,
            hr_user,
            employee_id=employee.id,
            start=monday,
            end=monday + timedelta(days=4),
            kind=RegularisationKind.STATUS_CHANGE,
            requested_status=AttendanceStatus.PRESENT,
            reason="Marked present by HR",
        )
    assert refused.value.status_code == 409
    assert (monday + timedelta(days=3)).strftime("%d %b") in refused.value.detail
    applied = (
        await session.scalars(
            select(AttendanceRegularisation).where(
                AttendanceRegularisation.status == RegularisationStatus.APPROVED
            )
        )
    ).all()
    assert applied == []
