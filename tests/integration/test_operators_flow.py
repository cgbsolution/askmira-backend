"""Operator access: the client grants, the grant is the only way in, and it can be taken back."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.user import User
from app.services import operators
from tests.integration.conftest import requires_db

pytestmark = requires_db


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


async def _grant(
    session: AsyncSession, client_admin: User, bureau: Tenant, operator: User, *, days: int = 30
) -> dict:
    return await operators.create_grant(
        session,
        client_admin,
        operators.GrantIn(
            operator_workspace=bureau.slug,
            operator_email=operator.email,
            expires_on=date.today() + timedelta(days=days),
        ),
        confirm=None,
        request=None,
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_an_operator_works_payroll_in_a_client_only_under_a_live_grant(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_tenant: Tenant,
    other_hr_user: User,
    client: httpx.AsyncClient,
) -> None:
    # `tenant` is the client; `other_tenant` is the payroll bureau.
    grant = await _grant(session, hr_user, other_tenant, other_hr_user)
    assert grant["is_live"]

    own_token = create_access_token(
        user_id=str(other_hr_user.id), tenant_id=str(other_tenant.id), role=other_hr_user.role.value
    )
    clients = await client.get("/api/v1/operator/clients", headers=_bearer(own_token))
    assert clients.status_code == 200
    assert [c["client"] for c in clients.json()] == [tenant.name]
    assert clients.json()[0]["work"]["no_run_yet"] is True

    entered = await client.post(
        f"/api/v1/operator/clients/{grant['id']}/enter", headers=_bearer(own_token)
    )
    assert entered.status_code == 200
    operator_token = entered.json()["access_token"]

    state = await client.get("/api/v1/operator/session", headers=_bearer(operator_token))
    assert state.json()["active"] is True
    assert state.json()["client"] == tenant.name

    # Inside: payroll reads, the employee directory reads, settings do not.
    runs = await client.get("/api/v1/payroll/runs", headers=_bearer(operator_token))
    assert runs.status_code == 200
    people = await client.get("/api/v1/employees", headers=_bearer(operator_token))
    assert people.status_code == 200
    refused = await client.get("/api/v1/admin/operators", headers=_bearer(operator_token))
    assert refused.status_code == 403
    refused_write = await client.post("/api/v1/employees", json={}, headers=_bearer(operator_token))
    assert refused_write.status_code == 403

    # The client's audit trail shows the bureau as an operator.
    entries = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id, AuditLog.entity_type == "operator_grant"
            )
        )
    ).all()
    assert {e.actor_type for e in entries} >= {"operator"}

    # Revoked: the very next request is refused.
    await operators.revoke_grant(session, hr_user, grant_id=uuid.UUID(grant["id"]), request=None)
    after = await client.get("/api/v1/payroll/runs", headers=_bearer(operator_token))
    assert after.status_code == 401


async def test_a_grant_cannot_be_used_by_anyone_else_or_after_it_ends(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_tenant: Tenant,
    other_hr_user: User,
    client: httpx.AsyncClient,
) -> None:
    grant = await _grant(session, hr_user, other_tenant, other_hr_user)

    # A token naming the grant but signed for somebody else is refused.
    from app.core.security import create_operator_token

    forged = create_operator_token(
        user_id=str(hr_user.id), tenant_id=str(tenant.id), grant_id=grant["id"]
    )
    response = await client.get("/api/v1/payroll/runs", headers=_bearer(forged))
    assert response.status_code == 401

    # Expired: the grant row says so, and the token stops working.
    from app.models.operators import OperatorGrant

    row = await session.get(OperatorGrant, uuid.UUID(grant["id"]))
    token = create_operator_token(
        user_id=str(other_hr_user.id), tenant_id=str(tenant.id), grant_id=grant["id"]
    )
    row.created_at = datetime.now(UTC) - timedelta(days=1)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()
    expired = await client.get("/api/v1/payroll/runs", headers=_bearer(token))
    assert expired.status_code == 401


async def test_only_the_clients_admin_grants_and_only_to_an_outside_admin(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_tenant: Tenant,
    other_hr_user: User,
) -> None:
    with pytest.raises(HTTPException) as own:
        await operators.create_grant(
            session,
            hr_user,
            operators.GrantIn(
                operator_workspace=tenant.slug,
                operator_email=hr_user.email,
                expires_on=date.today() + timedelta(days=10),
            ),
            confirm=None,
            request=None,
        )
    assert own.value.status_code == 422

    with pytest.raises(HTTPException) as unknown:
        await operators.create_grant(
            session,
            hr_user,
            operators.GrantIn(
                operator_workspace=other_tenant.slug,
                operator_email="nobody@nowhere.test",
                expires_on=date.today() + timedelta(days=10),
            ),
            confirm=None,
            request=None,
        )
    assert unknown.value.status_code == 404

    with pytest.raises(HTTPException) as too_long:
        await _grant(session, hr_user, other_tenant, other_hr_user, days=400)
    assert too_long.value.status_code == 422

    await _grant(session, hr_user, other_tenant, other_hr_user)
    with pytest.raises(HTTPException) as twice:
        await _grant(session, hr_user, other_tenant, other_hr_user)
    assert twice.value.status_code == 409


def test_the_operator_path_policy() -> None:
    assert operators.operator_may("POST", "/api/v1/payroll/runs")
    assert operators.operator_may("POST", "/api/v1/employees/abc/salary")
    assert operators.operator_may("GET", "/api/v1/employees")
    assert operators.operator_may("GET", "/api/v1/attendance/period")
    assert operators.operator_may("POST", "/api/v1/operator/exit")
    assert not operators.operator_may("POST", "/api/v1/employees")
    assert not operators.operator_may("POST", "/api/v1/leave/requests")
    assert not operators.operator_may("GET", "/api/v1/admin/users")
    assert not operators.operator_may("POST", "/api/v1/auth/change-password")
    assert not operators.operator_may("GET", "/api/v1/hiring/candidates")
