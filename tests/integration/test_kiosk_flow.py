"""Punching at a shared kiosk with an employee code and PIN."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendancePunch, PunchDirection, PunchSource
from app.models.employee import Employee
from app.models.identity import DeviceVendor
from app.models.kiosk import KioskPin
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.attendance import kiosk
from app.services.identity import devices as device_service
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

MORNING = datetime(2026, 10, 12, 3, 30, tzinfo=UTC)


async def _login(session: AsyncSession, tenant: Tenant, employee: Employee) -> User:
    user = User(
        tenant_id=tenant.id,
        email="anita.kiosk@acme-demo.com",
        password_hash="x",
        full_name="Anita",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    return await as_caller(session, user)


async def _tablet(session: AsyncSession, hr_user: User) -> str:
    _, token = await device_service.create_device(
        session,
        hr_user,
        device_id="KIOSK-RECEPTION",
        name="Reception tablet",
        vendor=DeviceVendor.KIOSK,
    )
    return token


async def test_a_person_punches_in_then_out_with_their_pin(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _login(session, tenant, employee)
    with pytest.raises(HTTPException):
        await kiosk.set_my_pin(session, me, "1234")
    with pytest.raises(HTTPException):
        await kiosk.set_my_pin(session, me, "7777")
    status_now = await kiosk.set_my_pin(session, me, "2580")
    assert status_now["is_set"] is True
    stored = await session.scalar(select(KioskPin).where(KioskPin.employee_id == employee.id))
    assert stored is not None and "2580" not in stored.pin_hash

    token = await _tablet(session, hr_user)
    info = await kiosk.kiosk_info(session, token)
    assert info["kiosk_name"] == "Reception tablet"

    first = await kiosk.kiosk_punch(
        session, token, employee_code=employee.employee_code, pin="2580", now=MORNING
    )
    assert first["direction"] == "in" and first["first_name"] == "Anita"
    second = await kiosk.kiosk_punch(
        session,
        token,
        employee_code=employee.employee_code,
        pin="2580",
        now=MORNING + timedelta(hours=9),
    )
    assert second["direction"] == "out"
    punches = (
        await session.scalars(
            select(AttendancePunch)
            .where(AttendancePunch.employee_id == employee.id)
            .order_by(AttendancePunch.punched_at)
        )
    ).all()
    assert [(p.direction, p.source) for p in punches] == [
        (PunchDirection.IN, PunchSource.KIOSK),
        (PunchDirection.OUT, PunchSource.KIOSK),
    ]


async def test_wrong_pins_lock_the_person_and_a_wrong_code_reads_the_same(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _login(session, tenant, employee)
    await kiosk.set_my_pin(session, me, "2580")
    token = await _tablet(session, hr_user)

    with pytest.raises(HTTPException) as unknown:
        await kiosk.kiosk_punch(session, token, employee_code="NOBODY", pin="2580", now=MORNING)
    with pytest.raises(HTTPException) as wrong:
        await kiosk.kiosk_punch(
            session, token, employee_code=employee.employee_code, pin="0000", now=MORNING
        )
    assert unknown.value.detail == wrong.value.detail
    assert unknown.value.status_code == wrong.value.status_code == 401

    for _ in range(kiosk.PERSON_ATTEMPTS - 1):
        with pytest.raises(HTTPException):
            await kiosk.kiosk_punch(
                session, token, employee_code=employee.employee_code, pin="0000", now=MORNING
            )
    with pytest.raises(HTTPException) as locked:
        await kiosk.kiosk_punch(
            session, token, employee_code=employee.employee_code, pin="2580", now=MORNING
        )
    assert locked.value.status_code == 429

    # Setting the PIN again clears the pause.
    await kiosk.set_my_pin(session, me, "3691")
    done = await kiosk.kiosk_punch(
        session, token, employee_code=employee.employee_code, pin="3691", now=MORNING
    )
    assert done["direction"] == "in"


async def test_a_tablet_pauses_after_a_run_of_wrong_entries(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    token = await _tablet(session, hr_user)
    for n in range(kiosk.DEVICE_ATTEMPTS):
        with pytest.raises(HTTPException):
            await kiosk.kiosk_punch(
                session, token, employee_code=f"GUESS{n}", pin="2580", now=MORNING
            )
    with pytest.raises(HTTPException) as paused:
        await kiosk.kiosk_punch(session, token, employee_code="GUESS", pin="2580", now=MORNING)
    assert paused.value.status_code == 429


async def test_only_a_kiosk_device_takes_kiosk_punches(
    session: AsyncSession, hr_user: User
) -> None:
    _, token = await device_service.create_device(
        session, hr_user, device_id="GATE-1", name="Gate", vendor=DeviceVendor.ESSL
    )
    with pytest.raises(HTTPException) as refused:
        await kiosk.kiosk_info(session, token)
    assert refused.value.status_code == 401


async def test_a_badge_punches_until_it_is_revoked(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    from app.models.kiosk import EmployeeBadge

    token = await _tablet(session, hr_user)
    card = await kiosk.issue_badge(session, hr_user, employee.id, "04 A1 B2 C3 9F")
    assert card["last_four"] == "C39F"
    stored = await session.scalar(select(EmployeeBadge))
    assert "04A1B2C39F" not in stored.badge_hash
    with pytest.raises(HTTPException) as twice:
        await kiosk.issue_badge(session, hr_user, employee.id, "04a1b2c39f")
    assert twice.value.status_code == 409

    punched = await kiosk.kiosk_punch(session, token, badge="04A1B2C39F", now=MORNING)
    assert punched["direction"] == "in" and punched["first_name"] == "Anita"

    await kiosk.revoke_badge(session, hr_user, uuid.UUID(card["id"]))
    with pytest.raises(HTTPException) as refused:
        await kiosk.kiosk_punch(
            session, token, badge="04A1B2C39F", now=MORNING + timedelta(hours=1)
        )
    assert refused.value.status_code == 401
