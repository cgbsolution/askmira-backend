"""The tenant session policy is enforced on every request, through the real app.

Signs in through `/auth/login` so the session starts where production's does.
The test client's address is 127.0.0.1.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tenant, TenantSessionPolicy, UserRole, UserSession
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import PASSWORD, bearer, make_person, now

pytestmark = requires_db

client = platform_support.client


async def _login(client, user) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _policy(session: AsyncSession, tenant: Tenant, **values) -> None:
    session.add(
        TenantSessionPolicy(
            tenant_id=tenant.id,
            idle_timeout_minutes=values.get("idle", 30),
            absolute_lifetime_hours=values.get("lifetime", 12),
            max_concurrent_sessions=values.get("limit", 0),
            enforce_ip_allowlist_for_privileged=values.get("enforce", False),
            ip_allowlist=values.get("allowlist", []),
        )
    )
    await session.commit()


async def test_without_a_policy_nothing_is_tracked(client, session, tenant):
    person = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair", password=PASSWORD
    )
    headers = await _login(client, person)
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    assert (await session.scalars(select(UserSession))).all() == []


async def test_an_idle_session_is_ended_and_cannot_be_refreshed_back(client, session, tenant):
    await _policy(session, tenant, idle=30)
    person = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair", password=PASSWORD
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": person.email, "password": PASSWORD}
    )
    access = {"Authorization": f"Bearer {login.json()['access_token']}"}
    refresh_token = login.json()["refresh_token"]

    assert (await client.get("/api/v1/auth/me", headers=access)).status_code == 200
    tracked = (await session.scalars(select(UserSession))).all()
    assert len(tracked) == 1

    # Forty minutes of silence.
    await session.execute(
        update(UserSession).values(last_seen_at=now() - timedelta(minutes=40))
    )
    await session.commit()
    ended = await client.get("/api/v1/auth/me", headers=access)
    assert ended.status_code == 401
    assert "without activity" in ended.json()["detail"]

    # A refresh token mints a new access token, and the device session stays ended.
    renewed = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert renewed.status_code == 200
    again = {"Authorization": f"Bearer {renewed.json()['access_token']}"}
    assert (await client.get("/api/v1/auth/me", headers=again)).status_code == 401

    # Signing in again starts a fresh session.
    fresh = await _login(client, person)
    assert (await client.get("/api/v1/auth/me", headers=fresh)).status_code == 200


async def test_the_absolute_lifetime_is_enforced(client, session, tenant):
    await _policy(session, tenant, idle=30, lifetime=1)
    person = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair", password=PASSWORD
    )
    headers = await _login(client, person)
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    await session.execute(
        update(UserSession).values(started_at=now() - timedelta(hours=2), last_seen_at=now())
    )
    await session.commit()
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401
    assert "at most 1 hours" in response.json()["detail"]


async def test_revoking_one_session_ends_it_immediately(client, session, tenant):
    await _policy(session, tenant)
    person = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair", password=PASSWORD
    )
    headers = await _login(client, person)
    listing = await client.get("/api/v1/opsec/sessions", headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["enforced"] is True
    session_id = body["sessions"][0]["id"]

    revoked = await client.post(
        "/api/v1/opsec/sessions/revoke", json={"session_id": session_id}, headers=headers
    )
    assert revoked.status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_the_ip_allowlist_blocks_privileged_roles_only(client, session, tenant):
    await _policy(session, tenant, enforce=True, allowlist=["10.20.0.0/16"])
    admin = await make_person(session, tenant, role=UserRole.HR_ADMIN, name="Asha Admin")
    employee = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Emp One")

    blocked = await client.get("/api/v1/auth/me", headers=bearer(admin))
    assert blocked.status_code == 403
    assert "allowed networks" in blocked.json()["detail"]
    assert (await client.get("/api/v1/auth/me", headers=bearer(employee))).status_code == 200


async def test_the_break_glass_account_passes_the_allowlist(client, session, tenant):
    await _policy(session, tenant, enforce=True, allowlist=["10.20.0.0/16"])
    admin = await make_person(session, tenant, role=UserRole.TENANT_ADMIN, name="Glass Owner")
    tenant.settings = {**(tenant.settings or {}), "break_glass_email": admin.email}
    await session.commit()
    assert (await client.get("/api/v1/auth/me", headers=bearer(admin))).status_code == 200


async def test_an_allowlisted_address_passes(client, session, tenant):
    await _policy(session, tenant, enforce=True, allowlist=["127.0.0.0/8"])
    admin = await make_person(session, tenant, role=UserRole.HR_ADMIN, name="Asha Admin")
    assert (await client.get("/api/v1/auth/me", headers=bearer(admin))).status_code == 200
