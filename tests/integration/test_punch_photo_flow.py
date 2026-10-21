"""A photo with a self-service punch, where the workspace asks for one.

What this holds, over HTTP: a workspace that requires a photo refuses a punch
without one; a photo that is not an image is refused; the stored photo is shown
to the person and to HR, and to no colleague.
"""

from __future__ import annotations

import base64
from datetime import date

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.employee import Employee, EmploymentStatus
from app.models.user import User, UserRole
from tests.integration.conftest import requires_db

pytestmark = requires_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
async def client(session: AsyncSession, tmp_path, monkeypatch):
    async def override():
        yield session

    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path))
    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def _login(session, tenant, entity, location, code) -> User:
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        last_name="Test",
        work_email=f"{code.lower()}@acme-demo.com",
        legal_entity_id=entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2024, 1, 1),
    )
    session.add(employee)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}@acme-demo.com",
        password_hash="x",
        full_name=code.title(),
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    return user


async def test_a_required_photo_is_asked_for_kept_and_shown_to_few(
    client, session, tenant, hr_user, legal_entity, location
) -> None:
    tenant.enabled_modules = {**(tenant.enabled_modules or {}), "leave": True}
    tenant.settings = {**(tenant.settings or {}), "attendance": {"photo_mode": "required"}}
    await session.commit()
    me = await _login(session, tenant, legal_entity, location, "SELFIE")
    colleague = await _login(session, tenant, legal_entity, location, "OTHER")

    refused = await client.post(
        "/api/v1/attendance/punch", json={"direction": "in"}, headers=_auth(me)
    )
    assert refused.status_code == 422 and "photo" in refused.json()["detail"]

    not_an_image = await client.post(
        "/api/v1/attendance/punch",
        json={"direction": "in", "photo": base64.b64encode(b"MZ\x90 not an image").decode()},
        headers=_auth(me),
    )
    assert not_an_image.status_code == 422

    punched = await client.post(
        "/api/v1/attendance/punch",
        json={
            "direction": "in",
            "photo": "data:image/png;base64," + base64.b64encode(PNG).decode(),
        },
        headers=_auth(me),
    )
    assert punched.status_code == 201, punched.text
    assert punched.json()["has_photo"] is True
    url = f"/api/v1/attendance/punches/{punched.json()['id']}/photo"

    mine = await client.get(url, headers=_auth(me))
    assert mine.status_code == 200 and mine.content == PNG
    assert (await client.get(url, headers=_auth(hr_user))).status_code == 200
    assert (await client.get(url, headers=_auth(colleague))).status_code in (403, 404)
