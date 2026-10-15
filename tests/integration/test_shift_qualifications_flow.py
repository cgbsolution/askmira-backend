"""A shift that requires a course: only people holding it, valid on the day, are rostered."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.learning import AssignmentStatus, CourseAssignment, CourseKind
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.attendance import ShiftAssignmentCreate, ShiftCreate
from app.services import learning
from app.services.attendance import setup as attendance_setup
from app.services.learning import CourseIn, ModuleIn
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def test_a_forklift_shift_needs_a_valid_certificate(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    course = await learning.create_course(
        session, hr_user,
        CourseIn(title="Forklift safety", kind=CourseKind.SAFETY, validity_months=12,
                 modules=[ModuleIn(title="Operating rules", body="As the trainer wrote them.")]),
        request=None,
    )
    shift = await attendance_setup.create_shift(
        session, hr_user,
        ShiftCreate(code="FLT", name="Forklift", start_time=time(6), end_time=time(14),
                    half_day_threshold_minutes=240, full_day_threshold_minutes=420,
                    required_course_ids=[uuid.UUID(course["id"])]),
    )
    with pytest.raises(HTTPException) as refused:
        await attendance_setup.assign_shift(
            session, hr_user,
            ShiftAssignmentCreate(employee_id=employee.id, shift_id=shift.id,
                                  effective_from=date(2026, 11, 2)),
        )
    assert "Forklift safety" in refused.value.detail

    # An expired certificate is not enough either.
    session.add(
        CourseAssignment(
            tenant_id=tenant.id, course_id=uuid.UUID(course["id"]), employee_id=employee.id,
            due_on=date(2025, 1, 1), status=AssignmentStatus.COMPLETED,
            completed_at=datetime(2025, 1, 1, tzinfo=UTC), expires_on=date(2026, 1, 1),
            score=100, certificate_number="C-OLD",
        )
    )
    await session.commit()
    with pytest.raises(HTTPException):
        await attendance_setup.assign_shift(
            session, hr_user,
            ShiftAssignmentCreate(employee_id=employee.id, shift_id=shift.id,
                                  effective_from=date(2026, 11, 2)),
        )

    session.add(
        CourseAssignment(
            tenant_id=tenant.id, course_id=uuid.UUID(course["id"]), employee_id=employee.id,
            due_on=date(2026, 10, 1), status=AssignmentStatus.COMPLETED,
            completed_at=datetime(2026, 10, 1, tzinfo=UTC), expires_on=date(2027, 10, 1),
            score=100, certificate_number="C-NEW",
        )
    )
    await session.commit()
    assigned = await attendance_setup.assign_shift(
        session, hr_user,
        ShiftAssignmentCreate(employee_id=employee.id, shift_id=shift.id,
                              effective_from=date(2026, 11, 2)),
    )
    assert assigned is not None

    with pytest.raises(HTTPException):
        await attendance_setup.create_shift(
            session, hr_user,
            ShiftCreate(code="BAD", name="Bad", start_time=time(6), end_time=time(14),
                        half_day_threshold_minutes=240, full_day_threshold_minutes=420,
                        required_course_ids=[uuid.uuid4()]),
        )


async def test_a_plant_shift_is_for_people_based_at_that_plant(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    from app.models.org import Location
    from app.services.attendance import shift_swaps

    plant = Location(tenant_id=tenant.id, legal_entity_id=employee.legal_entity_id,
                     name="Pune plant", code="PUN", state_code="MH")
    other = Location(tenant_id=tenant.id, legal_entity_id=employee.legal_entity_id,
                     name="Chennai plant", code="CHN", state_code="TN")
    session.add_all([plant, other])
    await session.flush()
    employee.location_id = other.id
    await session.commit()

    line = await attendance_setup.create_shift(
        session, hr_user,
        ShiftCreate(code="L2A", name="Line 2, A shift", start_time=time(6), end_time=time(14),
                    half_day_threshold_minutes=240, full_day_threshold_minutes=420,
                    location_id=plant.id, work_area=" Line 2 "),
    )
    assert line.work_area == "Line 2"
    with pytest.raises(HTTPException) as refused:
        await attendance_setup.assign_shift(
            session, hr_user,
            ShiftAssignmentCreate(employee_id=employee.id, shift_id=line.id,
                                  effective_from=date(2026, 11, 2)),
        )
    assert "Pune plant" in refused.value.detail and "Chennai plant" in refused.value.detail

    employee.location_id = plant.id
    await session.commit()
    await attendance_setup.assign_shift(
        session, hr_user,
        ShiftAssignmentCreate(employee_id=employee.id, shift_id=line.id,
                              effective_from=date(2026, 11, 2)),
    )
    view = await shift_swaps.coverage(
        session, hr_user, start=date(2026, 11, 2), end=date(2026, 11, 3), work_area="line 2"
    )
    assert [s["work_area"] for s in view["shifts"]] == ["Line 2"]
    assert view["shifts"][0]["location_name"] == "Pune plant"
    none = await shift_swaps.coverage(
        session, hr_user, start=date(2026, 11, 2), end=date(2026, 11, 3), work_area="Line 9"
    )
    assert none["shifts"] == []
