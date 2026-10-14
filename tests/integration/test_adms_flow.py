"""eSSL / ZKTeco devices pushing through the ADMS (iclock) protocol, over HTTP."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.main import app
from app.models.attendance import AttendancePunch, PunchDirection
from app.models.employee import Employee
from app.models.identity import DevicePunchException, DeviceVendor
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.identity import adms
from app.services.identity import devices as device_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

SERIAL = "CQZ7232560123"


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


def test_attlog_lines_become_punches_with_the_right_direction() -> None:
    body = (
        "1001\t2026-10-12 09:01:22\t0\t1\t0\t0\n"
        "1001\t2026-10-12 18:05:00\t1\t1\t0\t0\r\n\ngarbage\n"
    )
    rows = adms.parse_attlog(body)
    assert [r.get("state") for r in rows] == ["in", "out", None]
    assert rows[0]["pin"] == "1001" and rows[0]["timestamp"] == "2026-10-12 09:01:22"
    assert rows[2] == {"raw_line": "garbage"}


async def test_a_device_pushes_through_its_token_address(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee,
    client: httpx.AsyncClient,
) -> None:
    device, token = await device_service.create_device(
        session, hr_user, device_id=SERIAL, name="Main gate", vendor=DeviceVendor.ZKTECO
    )
    base = f"/api/v1/adms/{token}/iclock"

    hello = await client.get(f"{base}/cdata", params={"SN": SERIAL, "options": "all"})
    assert hello.status_code == 200
    assert hello.text.startswith(f"GET OPTION FROM: {SERIAL}")
    assert "TransFlag=TransData AttLog" in hello.text

    code = employee.employee_code
    body = (
        f"{code}\t2026-10-12 09:01:22\t0\t1\n"
        f"{code}\t2026-10-12 18:05:00\t1\t1\n"
        "UNKNOWN\t2026-10-12 09:00:00\t0\t1\n"
    )
    sent = await client.post(
        f"{base}/cdata", params={"SN": SERIAL, "table": "ATTLOG", "Stamp": "1"}, content=body
    )
    assert sent.status_code == 200 and sent.text == "OK: 3"

    punches = (
        await session.scalars(
            select(AttendancePunch).where(AttendancePunch.employee_id == employee.id)
            .order_by(AttendancePunch.punched_at)
        )
    ).all()
    assert [p.direction for p in punches] == [PunchDirection.IN, PunchDirection.OUT]
    # 09:01 in India is 03:31 UTC: the device's timezone was applied.
    assert punches[0].punched_at.hour == 3 and punches[0].punched_at.minute == 31
    parked = await session.scalar(
        select(DevicePunchException).where(DevicePunchException.employee_code == "UNKNOWN")
    )
    assert parked is not None

    # The device re-sends: nothing is doubled.
    again = await client.post(
        f"{base}/cdata", params={"SN": SERIAL, "table": "ATTLOG"}, content=body
    )
    assert again.status_code == 200
    count = len(
        (
            await session.scalars(
                select(AttendancePunch).where(AttendancePunch.employee_id == employee.id)
            )
        ).all()
    )
    assert count == 2

    other = await client.post(
        f"{base}/cdata", params={"SN": SERIAL, "table": "OPERLOG"}, content="OPLOG 4\t0\n"
    )
    assert other.text == "OK"
    poll = await client.get(f"{base}/getrequest", params={"SN": SERIAL})
    assert poll.text == "OK"

    wrong_serial = await client.get(f"{base}/cdata", params={"SN": "SOMEONE-ELSE"})
    assert wrong_serial.status_code == 401


async def test_serial_only_push_needs_hr_to_switch_it_on(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee,
    client: httpx.AsyncClient,
) -> None:
    device, _ = await device_service.create_device(
        session, hr_user, device_id=SERIAL, name="Main gate", vendor=DeviceVendor.ESSL
    )
    refused = await client.get("/iclock/cdata", params={"SN": SERIAL})
    assert refused.status_code == 401

    await device_service.update_device(session, hr_user, device.id, accepts_serial_push=True)
    hello = await client.get("/iclock/cdata", params={"SN": SERIAL})
    assert hello.status_code == 200

    sent = await client.post(
        "/iclock/cdata",
        params={"SN": SERIAL, "table": "ATTLOG"},
        content=f"{employee.employee_code}\t2026-10-13 09:00:00\t0\t15\n",
    )
    assert sent.text == "OK: 1"


async def test_the_same_serial_cannot_push_by_serial_into_two_workspaces(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_tenant: Tenant
) -> None:
    mine, _ = await device_service.create_device(
        session, hr_user, device_id=SERIAL, name="Gate", vendor=DeviceVendor.ZKTECO
    )
    await device_service.update_device(session, hr_user, mine.id, accepts_serial_push=True)

    other_admin = User(
        tenant_id=other_tenant.id, email="admin@globex-demo.com", password_hash="x",
        full_name="Other admin", role=UserRole.ADMIN, is_active=True,
    )
    session.add(other_admin)
    await session.commit()
    theirs, _ = await device_service.create_device(
        session, other_admin, device_id=SERIAL, name="Gate", vendor=DeviceVendor.ZKTECO
    )
    with pytest.raises(HTTPException) as refused:
        await device_service.update_device(
            session, other_admin, theirs.id, accepts_serial_push=True
        )
    assert refused.value.status_code == 409
