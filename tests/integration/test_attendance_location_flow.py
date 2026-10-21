"""Where a punch was made: the nearest work location, and the required-location rule."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import PunchDirection, PunchSource, WorkLocation
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.attendance import AttendanceSettings
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from tests.integration.conftest import requires_db

pytestmark = requires_db

HQ = (12.9716, 77.5946)


async def _hq(session: AsyncSession, tenant: Tenant) -> WorkLocation:
    location = WorkLocation(
        tenant_id=tenant.id,
        name="Bengaluru HQ",
        latitude=Decimal(str(HQ[0])),
        longitude=Decimal(str(HQ[1])),
        radius_metres=250,
        active=True,
    )
    session.add(location)
    await session.commit()
    return location


@pytest.mark.asyncio
async def test_a_punch_names_the_nearest_work_location_inside_and_out(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await _hq(session, tenant)
    near = await attendance_service.record_punch(
        session,
        hr_user,
        employee_id=employee.id,
        punched_at=datetime.now(UTC),
        direction=PunchDirection.IN,
        source=PunchSource.WEB,
        latitude=12.9720,
        longitude=77.5950,
    )
    assert near.location_label == "Bengaluru HQ"
    assert near.outside_geofence is False
    assert near.nearest_location_metres is not None and near.nearest_location_metres < 0

    far = await attendance_service.record_punch(
        session,
        hr_user,
        employee_id=employee.id,
        punched_at=datetime.now(UTC),
        direction=PunchDirection.OUT,
        source=PunchSource.WEB,
        latitude=13.0350,
        longitude=77.5970,
    )
    assert far.location_label == "Bengaluru HQ"
    assert far.outside_geofence is True
    assert far.nearest_location_metres > 5000  # about 7 km north


@pytest.mark.asyncio
async def test_required_location_refuses_a_self_punch_without_one(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
):
    await attendance_setup.update_attendance_settings(
        session, hr_user, AttendanceSettings(location_mode="required")
    )
    current = await attendance_setup.attendance_settings(session, hr_user)
    assert current.location_mode == "required"
    login = User(
        tenant_id=tenant.id,
        email="anita.loc@acme.test",
        password_hash="x",
        full_name="Anita Sharma",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(login)
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await attendance_service.require_location_if_configured(
            session, login, source=PunchSource.WEB, has_coordinates=False
        )
    assert refused.value.status_code == 422
    # With a location it goes through; a device punch never needs one.
    await attendance_service.require_location_if_configured(
        session, login, source=PunchSource.WEB, has_coordinates=True
    )
    await attendance_service.require_location_if_configured(
        session, login, source=PunchSource.BIOMETRIC, has_coordinates=False
    )
