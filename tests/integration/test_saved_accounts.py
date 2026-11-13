"""Switching between accounts signed in on one browser, through the real app.

A person who runs HR for two workspaces holds a login in each. Both stay
signed in on the browser, and the account menu switches between them without
the password — but never around either workspace's rules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Tenant, TenantSessionPolicy, User, UserRole, UserSession
from app.models.audit import AuditAction
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import PASSWORD, make_person

pytestmark = requires_db

client = platform_support.client

WEB = {"X-Auth-Transport": "cookie"}


async def _sign_in(client: httpx.AsyncClient, user: User) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}, headers=WEB
    )
    assert response.status_code == 200, response.text


def _csrf(client: httpx.AsyncClient) -> dict[str, str]:
    return {**WEB, "X-CSRF-Token": client.cookies.get("askmira_csrf") or ""}


async def _accounts(client: httpx.AsyncClient) -> list[dict]:
    response = await client.get("/api/v1/auth/accounts", headers=WEB)
    assert response.status_code == 200, response.text
    return response.json()["accounts"]


async def _who(client: httpx.AsyncClient) -> str | None:
    response = await client.get("/api/v1/auth/session", headers=WEB)
    return response.json()["user"]["email"] if response.status_code == 200 else None


async def _switch(client: httpx.AsyncClient, user: User) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/accounts/switch", json={"user_id": str(user.id)}, headers=_csrf(client)
    )


async def _two_workspaces(
    session: AsyncSession, tenant: Tenant, other_tenant: Tenant
) -> tuple[User, User]:
    first = await make_person(
        session, tenant, role=UserRole.ADMIN, name="Sachin Rao", password=PASSWORD
    )
    second = await make_person(
        session, other_tenant, role=UserRole.ADMIN, name="Sachin Rao", password=PASSWORD
    )
    return first, second


async def test_both_accounts_are_listed_and_the_last_sign_in_is_current(
    client, session, tenant, other_tenant
):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    await _sign_in(client, first)
    await _sign_in(client, second)

    accounts = await _accounts(client)
    assert [a["email"] for a in accounts] == [first.email, second.email]
    assert [a["current"] for a in accounts] == [False, True]
    assert [a["workspace"] for a in accounts] == [tenant.slug, other_tenant.slug]
    assert accounts[0]["workspace_name"] == tenant.name
    # Tokens never reach the page.
    assert "token" not in str(accounts)


async def test_the_saved_accounts_cookie_is_httponly_and_only_for_auth_routes(
    client, session, tenant
):
    person = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair", password=PASSWORD
    )
    response = await client.post(
        "/api/v1/auth/login", json={"email": person.email, "password": PASSWORD}, headers=WEB
    )
    saved = next(
        c for c in response.headers.get_list("set-cookie") if c.startswith("askmira_accounts=")
    )
    assert "httponly" in saved.lower()
    assert "path=/api/v1/auth" in saved.lower()
    assert "samesite=lax" in saved.lower()


async def test_switching_changes_the_session_without_a_password_and_is_audited(
    client, session, tenant, other_tenant
):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    await _sign_in(client, first)
    await _sign_in(client, second)
    assert await _who(client) == second.email

    switched = await _switch(client, first)
    assert switched.status_code == 200, switched.text
    assert switched.json()["access_token"] == ""
    assert switched.json()["tenant"]["id"] == str(tenant.id)
    assert await _who(client) == first.email
    assert [a["current"] for a in await _accounts(client)] == [True, False]

    # And back again: the first switch must not have dropped the second account.
    back = await _switch(client, second)
    assert back.status_code == 200
    assert await _who(client) == second.email

    rows = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == first.id, AuditLog.action == AuditAction.LOGIN
            )
        )
    ).all()
    assert any("Switched" in (row.summary or "") for row in rows)


async def test_a_switch_without_the_csrf_header_is_refused(client, session, tenant, other_tenant):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    await _sign_in(client, first)
    await _sign_in(client, second)
    refused = await client.post(
        "/api/v1/auth/accounts/switch", json={"user_id": str(first.id)}, headers=WEB
    )
    assert refused.status_code == 403
    assert await _who(client) == second.email


async def test_an_account_never_signed_in_here_cannot_be_switched_to(
    client, session, tenant, other_tenant
):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    await _sign_in(client, second)
    refused = await _switch(client, first)
    assert refused.status_code == 404
    assert await _who(client) == second.email


async def test_a_password_change_ends_the_saved_account_too(client, session, tenant, other_tenant):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    await _sign_in(client, first)
    await _sign_in(client, second)
    # Changed elsewhere (or reset by HR): every token issued before is void.
    await session.execute(
        update(User)
        .where(User.id == first.id)
        .values(tokens_valid_from=datetime.now(UTC) + timedelta(seconds=2))
    )
    await session.commit()

    ended = await _switch(client, first)
    assert ended.status_code == 401
    assert await _who(client) == second.email
    assert [a["email"] for a in await _accounts(client)] == [second.email]


async def test_a_switched_off_workspace_drops_out_of_the_list(
    client, session, tenant, other_tenant
):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    await _sign_in(client, first)
    await _sign_in(client, second)
    await session.execute(update(Tenant).where(Tenant.id == tenant.id).values(is_active=False))
    await session.commit()
    assert [a["email"] for a in await _accounts(client)] == [second.email]
    refused = await _switch(client, first)
    assert refused.status_code in (403, 404)
    assert await _who(client) == second.email


async def test_the_target_workspaces_idle_timeout_still_applies(
    client, session, tenant, other_tenant
):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    session.add(
        TenantSessionPolicy(
            tenant_id=tenant.id,
            idle_timeout_minutes=30,
            absolute_lifetime_hours=12,
            max_concurrent_sessions=0,
            enforce_ip_allowlist_for_privileged=False,
            ip_allowlist=[],
        )
    )
    await session.commit()
    await _sign_in(client, first)
    assert await _who(client) == first.email  # starts the device session
    await _sign_in(client, second)
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == first.id)
        .values(last_seen_at=datetime.now(UTC) - timedelta(minutes=40))
    )
    await session.commit()

    ended = await _switch(client, first)
    assert ended.status_code == 401
    assert "without activity" in ended.json()["detail"]
    assert await _who(client) == second.email
    assert [a["email"] for a in await _accounts(client)] == [second.email]


async def test_signing_out_forgets_only_that_account(client, session, tenant, other_tenant):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    await _sign_in(client, first)
    await _sign_in(client, second)

    out = await client.post("/api/v1/auth/logout", headers=_csrf(client))
    assert out.status_code == 204
    assert await _who(client) is None
    remaining = await _accounts(client)
    assert [(a["email"], a["current"]) for a in remaining] == [(first.email, False)]

    # The sign-in page's chooser: no session, yet the listing handed it a CSRF value.
    back = await _switch(client, first)
    assert back.status_code == 200, back.text
    assert await _who(client) == first.email


async def test_signing_out_of_every_account_forgets_them_all(client, session, tenant, other_tenant):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    await _sign_in(client, first)
    await _sign_in(client, second)
    out = await client.post("/api/v1/auth/logout?every_account=true", headers=_csrf(client))
    assert out.status_code == 204
    assert await _who(client) is None
    assert await _accounts(client) == []


async def test_removing_accounts_from_the_browser(client, session, tenant, other_tenant):
    first, second = await _two_workspaces(session, tenant, other_tenant)
    await _sign_in(client, first)
    await _sign_in(client, second)

    removed = await client.post(
        "/api/v1/auth/accounts/forget", json={"user_id": str(first.id)}, headers=_csrf(client)
    )
    assert removed.status_code == 204
    assert await _who(client) == second.email  # removing another account leaves this one
    assert [a["email"] for a in await _accounts(client)] == [second.email]

    current = await client.post(
        "/api/v1/auth/accounts/forget", json={"user_id": str(second.id)}, headers=_csrf(client)
    )
    assert current.status_code == 204
    assert await _who(client) is None  # removing the active one signs it out
    assert await _accounts(client) == []


async def test_a_browser_signed_in_before_the_list_existed_lists_its_account(
    client, session, tenant
):
    person = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair", password=PASSWORD
    )
    await _sign_in(client, person)
    client.cookies.delete("askmira_accounts", path="/api/v1/auth")
    accounts = await _accounts(client)
    assert [(a["email"], a["current"]) for a in accounts] == [(person.email, True)]
