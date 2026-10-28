"""HR editing somebody's contact details from the profile's Contact details card.

The card sends `PATCH /employees/{id}` with only the fields that changed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.user import User
from tests.integration.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override() -> AsyncGenerator[AsyncSession, None]:
        yield session

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


async def _person(session: AsyncSession, tenant) -> Employee:
    person = Employee(
        tenant_id=tenant.id, employee_code="E1", first_name="Deepak", work_email="e1@acme.test"
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def test_hr_saves_phone_email_and_gender(client, session, tenant, hr_user) -> None:
    person = await _person(session, tenant)
    saved = await client.patch(
        f"/api/v1/employees/{person.id}",
        headers=_auth(hr_user),
        json={"phone": "+91 82876 68958", "personal_email": "d@example.com", "gender": "male"},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert (body["phone"], body["personal_email"], body["gender"]) == (
        "+91 82876 68958",
        "d@example.com",
        "male",
    )
    audit = (
        await session.execute(select(AuditLog).where(AuditLog.entity_id == person.id))
    ).scalars().all()
    assert any("phone" in (row.changes or {}) for row in audit), "the edit is audited"


@pytest.mark.parametrize("phone", ["abc", "98765e4321", "12;34"])
async def test_a_phone_with_letters_is_refused(client, session, tenant, hr_user, phone) -> None:
    person = await _person(session, tenant)
    refused = await client.patch(
        f"/api/v1/employees/{person.id}", headers=_auth(hr_user), json={"phone": phone}
    )
    assert refused.status_code == 422, refused.text
