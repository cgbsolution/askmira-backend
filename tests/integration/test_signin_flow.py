"""Two-step sign-in: find the company from an email or mobile, then its own page."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models.employee import Employee, EmploymentStatus
from app.models.identity import IdentityProvider, IdpType
from app.models.org import LegalEntity
from app.models.social_login import SocialLoginAttempt
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.auth import AuthResponse, LoginRequest
from app.services import auth as auth_service
from app.services.hiring.public_guard import LIMITER
from app.services.identity import oidc, social
from tests.integration.conftest import requires_db

pytestmark = requires_db

PASSWORD = "Correct-Horse-9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    LIMITER.reset()
    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    email: str,
    *,
    phone: str | None = None,
    employee_phone: str | None = None,
    role: UserRole = UserRole.EMPLOYEE,
) -> User:
    employee = None
    if employee_phone:
        entity = await session.scalar(select(LegalEntity).where(LegalEntity.tenant_id == tenant.id))
        employee = Employee(
            tenant_id=tenant.id,
            employee_code=f"E{email[:4]}",
            first_name="Person",
            work_email=email,
            phone=employee_phone,
            status=EmploymentStatus.ACTIVE,
            date_of_joining=date(2025, 1, 1),
            legal_entity_id=entity.id if entity else None,
        )
        session.add(employee)
        await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=email,
        password_hash=hash_password(PASSWORD),
        full_name="Person",
        role=role,
        is_active=True,
        phone=phone,
        employee_id=employee.id if employee else None,
    )
    session.add(user)
    await session.commit()
    return user


# ── Step one: which company ─────────────────────────────────────────────────


async def test_an_email_or_a_mobile_finds_the_company(
    session: AsyncSession, tenant: Tenant, client: httpx.AsyncClient
) -> None:
    await _person(session, tenant, "deepak@acme-demo.com", phone="+91 98765 43210")

    by_email = await client.post("/api/v1/auth/identify", json={"login": "Deepak@acme-demo.com"})
    assert by_email.status_code == 200
    body = by_email.json()
    assert body["name"] == tenant.name
    assert body["workspace"] == tenant.slug
    assert body["login_kind"] == "email"
    assert "deepak" not in str(body).lower()  # nothing about the person comes back

    by_mobile = await client.post("/api/v1/auth/identify", json={"login": "09876543210"})
    assert by_mobile.status_code == 200
    assert by_mobile.json()["workspace"] == tenant.slug
    assert by_mobile.json()["login_kind"] == "mobile"

    unknown = await client.post("/api/v1/auth/identify", json={"login": "nobody@acme-demo.com"})
    assert unknown.status_code == 404


async def test_a_mobile_on_the_employee_record_also_counts(
    session: AsyncSession, tenant: Tenant, client: httpx.AsyncClient
) -> None:
    await _person(session, tenant, "ravi@acme-demo.com", employee_phone="9123456780")
    found = await client.post("/api/v1/auth/identify", json={"login": "+919123456780"})
    assert found.status_code == 200


async def test_a_mobile_shared_by_two_logins_is_refused_not_guessed(
    session: AsyncSession, tenant: Tenant, other_tenant: Tenant, client: httpx.AsyncClient
) -> None:
    await _person(session, tenant, "a@acme-demo.com", phone="9000000001")
    await _person(session, other_tenant, "b@globex-demo.com", phone="+91-9000000001")
    response = await client.post("/api/v1/auth/identify", json={"login": "9000000001"})
    assert response.status_code == 409
    assert "work email" in response.json()["detail"]


async def test_a_suspended_company_is_not_offered(
    session: AsyncSession, tenant: Tenant, client: httpx.AsyncClient
) -> None:
    await _person(session, tenant, "s@acme-demo.com")
    tenant.is_active = False
    await session.commit()
    response = await client.post("/api/v1/auth/identify", json={"login": "s@acme-demo.com"})
    assert response.status_code == 403
    page = await client.get(f"/api/v1/public/workspaces/{tenant.slug}")
    assert page.status_code == 403


async def test_identify_is_rate_limited(
    session: AsyncSession, tenant: Tenant, client: httpx.AsyncClient
) -> None:
    codes = [
        (
            await client.post("/api/v1/auth/identify", json={"login": f"x{i}@acme-demo.com"})
        ).status_code
        for i in range(22)
    ]
    assert codes[-1] == 429
    assert 404 in codes[:20]


# ── Step two: the company's own page ────────────────────────────────────────


async def test_the_company_page_shows_its_name_logo_and_sso(
    session: AsyncSession, tenant: Tenant, hr_user: User, client: httpx.AsyncClient
) -> None:
    page = await client.get(f"/api/v1/public/workspaces/{tenant.slug}")
    assert page.status_code == 200
    assert page.json()["logo_url"] is None
    assert page.json()["sso"] is None

    admin_token = create_access_token(
        user_id=str(hr_user.id), tenant_id=str(tenant.id), role=hr_user.role.value
    )
    uploaded = await client.post(
        "/api/v1/org/logo",
        files={"file": ("logo.png", PNG, "image/png")},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert uploaded.status_code == 200, uploaded.text
    logo_url = uploaded.json()["logo_url"]
    served = await client.get(logo_url)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.content == PNG

    svg = await client.post(
        "/api/v1/org/logo",
        files={"file": ("logo.svg", b"<svg onload='alert(1)'/>", "image/svg+xml")},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert svg.status_code == 422

    session.add(
        IdentityProvider(
            tenant_id=tenant.id,
            name="Acme Okta",
            idp_type=IdpType.OIDC,
            enabled=True,
            discovery_url="https://acme.okta.example/.well-known/openid-configuration",
            client_id="acme",
            enforce_sso=False,
        )
    )
    await session.commit()
    with_sso = await client.get(f"/api/v1/public/workspaces/{tenant.slug}")
    assert with_sso.json()["sso"]["name"] == "Acme Okta"
    assert with_sso.json()["logo_url"] == logo_url

    missing = await client.get("/api/v1/public/workspaces/no-such-company")
    assert missing.status_code == 404


async def test_sign_in_with_a_mobile_number_on_the_company_page(
    session: AsyncSession, tenant: Tenant
) -> None:
    await _person(session, tenant, "m@acme-demo.com", phone="9876500000")
    result = await auth_service.login(
        session, LoginRequest(login="+91 98765 00000", password=PASSWORD, workspace=tenant.slug)
    )
    assert isinstance(result, AuthResponse)
    assert result.user.email == "m@acme-demo.com"

    # The older shape the mobile app sends still works.
    old = await auth_service.login(
        session, LoginRequest(email="m@acme-demo.com", password=PASSWORD)
    )
    assert isinstance(old, AuthResponse)


async def test_a_company_that_enforces_sso_takes_no_passwords(
    session: AsyncSession, tenant: Tenant
) -> None:
    from fastapi import HTTPException

    await _person(session, tenant, "e@acme-demo.com")
    session.add(
        IdentityProvider(
            tenant_id=tenant.id,
            name="Acme Okta",
            idp_type=IdpType.OIDC,
            enabled=True,
            discovery_url="https://acme.okta.example/.well-known/openid-configuration",
            client_id="acme",
            enforce_sso=True,
        )
    )
    await session.commit()
    with pytest.raises(HTTPException) as refused:
        await auth_service.login(session, LoginRequest(login="e@acme-demo.com", password=PASSWORD))
    assert refused.value.status_code == 403


# ── Continue with Google / Microsoft ────────────────────────────────────────


@pytest.fixture
def fake_providers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(social.social_settings, "google_oauth_client_id", "g-client")
    monkeypatch.setattr(social.social_settings, "google_oauth_client_secret", "g-secret")
    monkeypatch.setattr(social.social_settings, "microsoft_oauth_client_id", "m-client")
    monkeypatch.setattr(social.social_settings, "microsoft_oauth_client_secret", "m-secret")

    async def discover(url: str) -> oidc.OidcDiscovery:
        return oidc.OidcDiscovery(
            issuer="https://issuer.example",
            authorization_endpoint="https://issuer.example/authorize",
            token_endpoint="https://issuer.example/token",
            jwks_uri="https://issuer.example/jwks",
        )

    claims: dict = {}

    async def exchange_code(**kwargs) -> oidc.OidcIdentity:
        return oidc.OidcIdentity(
            subject="sub-1", issuer="https://issuer.example", claims=dict(claims)
        )

    monkeypatch.setattr(oidc, "discover", discover)
    monkeypatch.setattr(oidc, "exchange_code", exchange_code)
    return claims


async def _begin(client: httpx.AsyncClient, session: AsyncSession, provider: str) -> str:
    started = await client.post(
        f"/api/v1/auth/social/{provider}/start",
        json={"redirect_uri": f"https://portal.example/signin/social/{provider}"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["authorization_url"].startswith("https://issuer.example/authorize")
    attempt = await session.scalar(
        select(SocialLoginAttempt).order_by(SocialLoginAttempt.created_at.desc()).limit(1)
    )
    return attempt.state


async def test_continue_with_google_signs_in_an_existing_login_only(
    session: AsyncSession, tenant: Tenant, client: httpx.AsyncClient, fake_providers: dict
) -> None:
    await _person(session, tenant, "g@acme-demo.com")
    listed = await client.get("/api/v1/auth/social/providers")
    assert set(listed.json()["providers"]) == {"google", "microsoft"}

    fake_providers.update({"email": "g@acme-demo.com", "email_verified": True})
    state = await _begin(client, session, "google")
    done = await client.post(
        "/api/v1/auth/social/google/callback", json={"code": "c", "state": state}
    )
    assert done.status_code == 200, done.text
    assert done.json()["user"]["email"] == "g@acme-demo.com"

    again = await client.post(
        "/api/v1/auth/social/google/callback", json={"code": "c", "state": state}
    )
    assert again.status_code == 401

    fake_providers.update({"email": "stranger@gmail.com", "email_verified": True})
    state = await _begin(client, session, "google")
    nobody = await client.post(
        "/api/v1/auth/social/google/callback", json={"code": "c", "state": state}
    )
    assert nobody.status_code == 404


async def test_an_unverified_provider_email_is_not_trusted(
    session: AsyncSession, tenant: Tenant, client: httpx.AsyncClient, fake_providers: dict
) -> None:
    await _person(session, tenant, "u@acme-demo.com")
    fake_providers.update({"email": "u@acme-demo.com", "email_verified": False})
    state = await _begin(client, session, "google")
    google = await client.post(
        "/api/v1/auth/social/google/callback", json={"code": "c", "state": state}
    )
    assert google.status_code == 403

    fake_providers.clear()
    fake_providers.update({"email": "u@acme-demo.com"})  # no xms_edov
    state = await _begin(client, session, "microsoft")
    microsoft = await client.post(
        "/api/v1/auth/social/microsoft/callback", json={"code": "c", "state": state}
    )
    assert microsoft.status_code == 403
    assert "owns this email" in microsoft.json()["detail"]

    fake_providers["xms_edov"] = True
    state = await _begin(client, session, "microsoft")
    ok = await client.post(
        "/api/v1/auth/social/microsoft/callback", json={"code": "c", "state": state}
    )
    assert ok.status_code == 200, ok.text


async def test_a_foreign_return_address_is_refused(
    client: httpx.AsyncClient, fake_providers: dict
) -> None:
    response = await client.post(
        "/api/v1/auth/social/google/start", json={"redirect_uri": "https://evil.example/steal"}
    )
    assert response.status_code == 422


async def test_without_an_oauth_app_the_buttons_do_not_exist(client: httpx.AsyncClient) -> None:
    listed = await client.get("/api/v1/auth/social/providers")
    assert listed.json()["providers"] == []
    started = await client.post(
        "/api/v1/auth/social/google/start",
        json={"redirect_uri": "https://portal.example/signin/social/google"},
    )
    assert started.status_code == 404
