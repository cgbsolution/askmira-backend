"""Changing a reporting line, as the org chart's drag-and-drop does it.

The chart sends `PATCH /employees/{id}` with only `manager_id`, and its Undo
sends the previous manager back (or null). These pin the rules the chart relies
on the server to enforce: HR only, never your own line, never under your own
report, and every move audited.
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
from app.models.tenant import Tenant
from app.models.user import User, UserRole
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


async def _person(
    session: AsyncSession, tenant: Tenant, code: str, manager: Employee | None = None
) -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code,
        work_email=f"{code.lower()}@acme.test",
        manager_id=manager.id if manager else None,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _login(
    session: AsyncSession,
    tenant: Tenant,
    role: UserRole,
    tag: str,
    employee: Employee | None = None,
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{tag}@acme.test",
        password_hash="x",
        full_name=tag,
        role=role,
        is_active=True,
        employee_id=employee.id if employee else None,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _manager_of(session: AsyncSession, person: Employee):
    # A column select reads the row, not the cached object.
    return (
        await session.execute(select(Employee.manager_id).where(Employee.id == person.id))
    ).scalar_one()


async def test_hr_moves_a_person_and_undoes_it(client, session, tenant, hr_user) -> None:
    ceo = await _person(session, tenant, "CEO")
    lead = await _person(session, tenant, "LEAD", ceo)
    other = await _person(session, tenant, "OTHER", ceo)
    dev = await _person(session, tenant, "DEV", lead)

    moved = await client.patch(
        f"/api/v1/employees/{dev.id}", headers=_auth(hr_user), json={"manager_id": str(other.id)}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["manager_id"] == str(other.id)
    assert await _manager_of(session, dev) == other.id

    audit = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "employee", AuditLog.entity_id == dev.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert any("manager_id" in (row.changes or {}) for row in audit), "the move is audited"

    # Undo puts the old manager back.
    undone = await client.patch(
        f"/api/v1/employees/{dev.id}", headers=_auth(hr_user), json={"manager_id": str(lead.id)}
    )
    assert undone.status_code == 200, undone.text
    assert await _manager_of(session, dev) == lead.id


async def test_undo_back_to_nobody_clears_the_manager(client, session, tenant, hr_user) -> None:
    ceo = await _person(session, tenant, "CEO")
    loose = await _person(session, tenant, "LOOSE")

    await client.patch(
        f"/api/v1/employees/{loose.id}", headers=_auth(hr_user), json={"manager_id": str(ceo.id)}
    )
    cleared = await client.patch(
        f"/api/v1/employees/{loose.id}", headers=_auth(hr_user), json={"manager_id": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert await _manager_of(session, loose) is None


async def test_nobody_moves_under_their_own_report(client, session, tenant, hr_user) -> None:
    ceo = await _person(session, tenant, "CEO")
    lead = await _person(session, tenant, "LEAD", ceo)
    dev = await _person(session, tenant, "DEV", lead)

    for target in (dev, lead):
        refused = await client.patch(
            f"/api/v1/employees/{ceo.id}",
            headers=_auth(hr_user),
            json={"manager_id": str(target.id)},
        )
        assert refused.status_code == 422, refused.text
    assert await _manager_of(session, ceo) is None

    own = await client.patch(
        f"/api/v1/employees/{lead.id}", headers=_auth(hr_user), json={"manager_id": str(lead.id)}
    )
    assert own.status_code == 422


async def test_only_hr_can_move_and_not_themselves(client, session, tenant) -> None:
    ceo = await _person(session, tenant, "CEO")
    lead = await _person(session, tenant, "LEAD", ceo)
    dev = await _person(session, tenant, "DEV", lead)

    # A manager sees the chart but cannot drag: the API refuses as well.
    manager = await _login(session, tenant, UserRole.EMPLOYEE, "lead", lead)
    refused = await client.patch(
        f"/api/v1/employees/{dev.id}", headers=_auth(manager), json={"manager_id": str(ceo.id)}
    )
    assert refused.status_code == 403
    assert await _manager_of(session, dev) == lead.id

    # HR's own line is changed by somebody else.
    hr_person = await _person(session, tenant, "HR", ceo)
    sub_admin = await _login(session, tenant, UserRole.SUB_ADMIN, "ops", hr_person)
    self_move = await client.patch(
        f"/api/v1/employees/{hr_person.id}",
        headers=_auth(sub_admin),
        json={"manager_id": str(lead.id)},
    )
    assert self_move.status_code == 403
    allowed = await client.patch(
        f"/api/v1/employees/{dev.id}", headers=_auth(sub_admin), json={"manager_id": str(ceo.id)}
    )
    assert allowed.status_code == 200, allowed.text


async def test_a_manager_from_another_workspace_is_refused(
    client, session, tenant, other_tenant, hr_user
) -> None:
    dev = await _person(session, tenant, "DEV")
    stranger = await _person(session, other_tenant, "STRANGER")
    refused = await client.patch(
        f"/api/v1/employees/{dev.id}", headers=_auth(hr_user), json={"manager_id": str(stranger.id)}
    )
    assert refused.status_code == 422
    assert await _manager_of(session, dev) is None
