"""The web session through the real app: cookies, CSRF, refresh, lockout, headers.

These go through HTTP rather than the services, because every property under
test lives in the transport — which cookie is set, which header is demanded,
which status comes back.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import get_session
from app.main import app
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from tests.integration.conftest import requires_db

pytestmark = requires_db

PASSWORD = "Correct-horse-9"
WEB = {"X-Auth-Transport": "cookie"}


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def person(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        # A real-looking domain: `.test` is a reserved TLD the email validator refuses.
        email="meera@acme-demo.com",
        password_hash=hash_password(PASSWORD),
        full_name="Meera Nair",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _sign_in(client: httpx.AsyncClient, person: User) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"email": person.email, "password": PASSWORD}, headers=WEB
    )


async def test_the_web_login_sets_httponly_cookies_and_returns_no_tokens(
    client: httpx.AsyncClient, person: User
) -> None:
    response = await _sign_in(client, person)
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "" and body["refresh_token"] == ""
    set_cookies = response.headers.get_list("set-cookie")
    access = next(c for c in set_cookies if c.startswith("askmira_access="))
    refresh = next(c for c in set_cookies if c.startswith("askmira_refresh="))
    csrf = next(c for c in set_cookies if c.startswith("askmira_csrf="))
    assert "httponly" in access.lower() and "samesite=lax" in access.lower()
    assert "httponly" in refresh.lower() and "path=/api/v1/auth" in refresh.lower()
    assert "httponly" not in csrf.lower()


async def test_a_non_web_login_still_receives_bearer_tokens(
    client: httpx.AsyncClient, person: User
) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": person.email, "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_the_session_cookie_authenticates_reads(
    client: httpx.AsyncClient, person: User
) -> None:
    await _sign_in(client, person)
    response = await client.get("/api/v1/auth/session")
    assert response.status_code == 200
    assert response.json()["user"]["email"] == person.email


async def test_a_cookie_write_without_the_csrf_header_is_refused(
    client: httpx.AsyncClient, person: User
) -> None:
    await _sign_in(client, person)
    refused = await client.post("/api/v1/auth/logout")
    assert refused.status_code == 403
    wrong = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "guess"})
    assert wrong.status_code == 403

    token = client.cookies.get("askmira_csrf")
    accepted = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": token})
    assert accepted.status_code == 204
    cleared = accepted.headers.get_list("set-cookie")
    assert any(c.startswith("askmira_access=") and "max-age=0" in c.lower() for c in cleared)


async def test_a_bearer_write_needs_no_csrf_header(client: httpx.AsyncClient, person: User) -> None:
    token = (
        await client.post("/api/v1/auth/login", json={"email": person.email, "password": PASSWORD})
    ).json()["access_token"]
    client.cookies.clear()
    response = await client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 204


async def test_refresh_from_the_cookie_needs_the_csrf_header(
    client: httpx.AsyncClient, person: User
) -> None:
    await _sign_in(client, person)
    refused = await client.post("/api/v1/auth/refresh", headers=WEB)
    assert refused.status_code == 401
    token = client.cookies.get("askmira_csrf")
    renewed = await client.post("/api/v1/auth/refresh", headers={**WEB, "X-CSRF-Token": token})
    assert renewed.status_code == 200
    assert renewed.json()["access_token"] == ""


async def test_repeated_failed_sign_ins_lock_the_account(
    client: httpx.AsyncClient, person: User
) -> None:
    for _ in range(5):
        failed = await client.post(
            "/api/v1/auth/login", json={"email": person.email, "password": "wrong-password-1"}
        )
        assert failed.status_code == 401
    locked = await _sign_in(client, person)
    assert locked.status_code == 429
    assert locked.headers.get("retry-after")


async def test_api_responses_carry_hardening_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


async def test_a_module_the_workspace_has_not_enabled_is_refused(
    client: httpx.AsyncClient, person: User
) -> None:
    # The fixture tenant enables core HR, payroll and leave - not culture.
    await _sign_in(client, person)
    response = await client.get("/api/v1/culture/clubs")
    assert response.status_code == 403
    assert "not enabled" in response.json()["detail"]
    allowed = await client.get("/api/v1/leave/types")
    assert allowed.status_code == 200
