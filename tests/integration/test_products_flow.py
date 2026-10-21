"""Linking a workspace to our separate products (interview tool, expense tool).

The products are faked with an httpx MockTransport that checks every call the
way a real product must: the HS256 signature with that product's secret, the
audience, and that the token is bound to the exact body it came with.
"""

from __future__ import annotations

import hashlib
import json
import types
from collections.abc import AsyncGenerator, Iterator
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.products import client as product_client
from app.services.products import service as products
from app.services.products import settings as product_settings
from app.services.scheduler.jobs import job_by_key, system_actor
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

SECRETS = {
    "interview": "interview-secret-0123456789abcdef0123456789",
    "expense": "expense-secret-0123456789abcdef0123456789ab",
}


class FakeProduct:
    """Both products behind one transport, checking tokens as a product would."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_sync = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        product = "interview" if request.url.host == "interview-api.test" else "expense"
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token,
            SECRETS[product],
            algorithms=["HS256"],
            audience=f"askmira:{product}",
            issuer="askmira",
        )
        assert claims["use"] == "provision"
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        payload = json.loads(request.content)
        self.calls.append(
            {"product": product, "path": request.url.path, "body": payload, "claims": claims}
        )
        if request.url.path.endswith("/organizations"):
            return httpx.Response(200, json={"external_ref": f"{product}-org-42"})
        if self.fail_sync:
            return httpx.Response(500, json={"detail": "database is down"})
        return httpx.Response(
            200,
            json={
                "created": len(payload["users"]),
                "updated": 0,
                "deactivated": 0,
                "skipped": [],
            },
        )

    def synced(self, product: str) -> list[dict[str, Any]]:
        bodies = [c["body"] for c in self.calls if c["product"] == product and "sync" in c["path"]]
        return bodies[-1]["users"]


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProduct]:
    for product, prefix in (("interview", "INTERVIEW_TOOL"), ("expense", "EXPENSE_TOOL")):
        monkeypatch.setenv(f"{prefix}_API_URL", f"https://{product}-api.test")
        monkeypatch.setenv(f"{prefix}_APP_URL", f"https://{product}.test")
        monkeypatch.setenv(f"{prefix}_SECRET", SECRETS[product])
    product_settings.reset_cache()
    fake = FakeProduct()
    real = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        product_client,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=client_factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout
        ),
    )
    yield fake
    product_settings.reset_cache()


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for prefix in ("INTERVIEW_TOOL", "EXPENSE_TOOL"):
        for suffix in ("API_URL", "APP_URL", "SECRET"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    product_settings.reset_cache()
    yield
    product_settings.reset_cache()


@pytest.fixture
async def super_admin(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email="platform@acme.test",
        password_hash="not-a-real-hash",
        full_name="Platform",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def people(session: AsyncSession, tenant: Tenant) -> dict[str, User]:
    """A manager, their report, and someone who has left — each with a login."""
    sales = Department(tenant_id=tenant.id, name="Sales", code="SAL")
    session.add(sales)
    await session.flush()

    def person(code: str, email: str, **extra: Any) -> Employee:
        return Employee(
            tenant_id=tenant.id,
            employee_code=code,
            first_name=code,
            work_email=email,
            status=extra.pop("status", EmploymentStatus.ACTIVE),
            **extra,
        )

    mgr = person("M1", "mgr@acme.test", department_id=sales.id, grade="L5")
    session.add(mgr)
    await session.flush()
    rep = person("E1", "rep@acme.test", manager_id=mgr.id, department_id=sales.id)
    gone = person("X1", "gone@acme.test", status=EmploymentStatus.EXITED)
    session.add_all([rep, gone])
    await session.flush()

    users = {}
    for key, emp in (("mgr", mgr), ("rep", rep), ("gone", gone)):
        users[key] = User(
            tenant_id=tenant.id,
            email=emp.work_email,
            password_hash="x",
            full_name=f"{key.title()} Person",
            role=UserRole.EMPLOYEE,
            is_active=True,
            employee_id=emp.id,
        )
    session.add_all(users.values())
    await session.commit()
    return users


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


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


def _launch_claims(url: str, product: str) -> dict[str, Any]:
    parsed = urlparse(url)
    assert parsed.netloc == f"{product}.test" and parsed.path == "/sso/askmira"
    assert not parsed.query, "the token travels in the fragment, never the query"
    token = parsed.fragment.removeprefix("token=")
    return jwt.decode(token, SECRETS[product], algorithms=["HS256"], audience=f"askmira:{product}")


async def test_unconfigured_product_cannot_be_linked(
    session: AsyncSession, super_admin: User, tenant: Tenant, unconfigured: None
) -> None:
    listed = await products.list_for_workspace(session, super_admin, tenant.id)
    assert {p["key"]: p["configured"] for p in listed} == {"interview": False, "expense": False}
    with pytest.raises(HTTPException) as refused:
        await products.link(session, super_admin, tenant.id, "interview", request=None)
    assert refused.value.status_code == 409


async def test_only_the_super_admin_links(
    session: AsyncSession, hr_user: User, tenant: Tenant, configured: FakeProduct
) -> None:
    with pytest.raises(HTTPException) as refused:
        await products.link(session, hr_user, tenant.id, "interview", request=None)
    assert refused.value.status_code == 403
    assert configured.calls == []


async def test_link_provisions_syncs_and_replaces_the_module(
    session: AsyncSession,
    super_admin: User,
    hr_user: User,
    tenant: Tenant,
    people: dict[str, User],
    configured: FakeProduct,
    client: httpx.AsyncClient,
) -> None:
    result = await products.link(session, super_admin, tenant.id, "interview", request=None)
    assert result["status"] == "active" and result["external_ref"] == "interview-org-42"

    org_call = configured.calls[0]
    assert org_call["path"] == "/integrations/askmira/organizations"
    assert org_call["claims"]["sub"] == str(tenant.id)
    assert org_call["body"]["slug"] == tenant.slug
    assert org_call["body"]["domain"] == "acme.test"

    # Interview: HR as admin, the manager as recruiter; the report and the leaver not at all.
    roster = {p["email"]: p for p in configured.synced("interview")}
    assert roster["hr@acme.test"]["role"] == "admin"
    assert roster["platform@acme.test"]["role"] == "admin"
    assert roster["mgr@acme.test"]["role"] == "recruiter"
    assert "rep@acme.test" not in roster and "gone@acme.test" not in roster

    await products.link(session, super_admin, tenant.id, "expense", request=None)
    staff = {p["email"]: p for p in configured.synced("expense")}
    assert staff["rep@acme.test"]["role"] == "employee"
    assert staff["rep@acme.test"]["manager_email"] == "mgr@acme.test"
    assert staff["rep@acme.test"]["department"] == "Sales"
    assert staff["mgr@acme.test"]["grade"] == "L5"
    assert staff["hr@acme.test"]["role"] == "admin"
    assert "gone@acme.test" not in staff

    link = await session.scalar(select(ProductLink).where(ProductLink.product == "expense"))
    assert link is not None and link.last_sync["created"] == len(staff)

    # The portal's session payload tells the nav which modules are replaced.
    response = await client.get("/api/v1/auth/session", headers=_bearer(hr_user))
    assert response.status_code == 200
    assert response.json()["tenant"]["linked_products"] == {"interview": True, "expense": True}

    audits = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "product_link"))
    ).all()
    assert len(audits) >= 4  # two links, two syncs


async def test_launch_signs_the_person_in_with_their_role(
    session: AsyncSession,
    super_admin: User,
    hr_user: User,
    tenant: Tenant,
    people: dict[str, User],
    configured: FakeProduct,
    client: httpx.AsyncClient,
) -> None:
    await products.link(session, super_admin, tenant.id, "interview", request=None)
    await products.link(session, super_admin, tenant.id, "expense", request=None)

    response = await client.post("/api/v1/products/interview/launch", headers=_bearer(hr_user))
    assert response.status_code == 200, response.text
    claims = _launch_claims(response.json()["url"], "interview")
    assert claims["use"] == "launch" and claims["role"] == "admin"
    assert claims["email"] == "hr@acme.test" and claims["tid"] == str(tenant.id)
    assert claims["ext"] == "interview-org-42" and claims["exp"] - claims["iat"] == 60

    # The manager gets in as a recruiter; the report may not use the interview tool.
    manager = await as_caller(session, people["mgr"])
    assert (await products.launch(session, manager, "interview"))["url"]
    response = await client.post(
        "/api/v1/products/interview/launch", headers=_bearer(people["rep"])
    )
    assert response.status_code == 403

    # Everyone files expenses.
    response = await client.post("/api/v1/products/expense/launch", headers=_bearer(people["rep"]))
    assert response.status_code == 200
    assert _launch_claims(response.json()["url"], "expense")["role"] == "employee"

    # Each launch is a fresh one-time token.
    a = _launch_claims((await products.launch(session, hr_user, "expense"))["url"], "expense")
    b = _launch_claims((await products.launch(session, hr_user, "expense"))["url"], "expense")
    assert a["jti"] != b["jti"]


async def test_another_workspace_cannot_launch_this_ones_product(
    session: AsyncSession,
    super_admin: User,
    tenant: Tenant,
    other_hr_user: User,
    configured: FakeProduct,
    client: httpx.AsyncClient,
) -> None:
    await products.link(session, super_admin, tenant.id, "interview", request=None)
    response = await client.post(
        "/api/v1/products/interview/launch", headers=_bearer(other_hr_user)
    )
    assert response.status_code == 404


async def test_unlink_brings_the_module_back(
    session: AsyncSession,
    super_admin: User,
    hr_user: User,
    tenant: Tenant,
    configured: FakeProduct,
    client: httpx.AsyncClient,
) -> None:
    await products.link(session, super_admin, tenant.id, "interview", request=None)
    await products.unlink(session, super_admin, tenant.id, "interview", request=None)
    await session.refresh(tenant)
    assert tenant.linked_products == {}
    response = await client.post("/api/v1/products/interview/launch", headers=_bearer(hr_user))
    assert response.status_code == 404

    # Relinking reuses the row.
    await products.link(session, super_admin, tenant.id, "interview", request=None)
    rows = (await session.scalars(select(ProductLink))).all()
    assert len([r for r in rows if r.tenant_id == tenant.id]) == 1


async def test_a_failed_sync_is_recorded_not_raised(
    session: AsyncSession, super_admin: User, tenant: Tenant, configured: FakeProduct
) -> None:
    configured.fail_sync = True
    result = await products.link(session, super_admin, tenant.id, "expense", request=None)
    assert result["status"] == "active"
    assert "500" in result["last_error"] and "database is down" in result["last_error"]

    configured.fail_sync = False
    result = await products.sync_now(session, super_admin, tenant.id, "expense", request=None)
    assert result["last_error"] is None and result["last_synced_at"]


async def test_the_nightly_job_syncs_every_active_link(
    session: AsyncSession, super_admin: User, tenant: Tenant, configured: FakeProduct
) -> None:
    job = job_by_key("products.people_sync")
    assert job is not None
    await products.link(session, super_admin, tenant.id, "interview", request=None)
    before = len(configured.calls)
    outcome = await products.sync_workspace(session, system_actor(tenant.id))
    assert outcome["products"] == 1
    assert len(configured.calls) == before + 1


async def test_public_mail_is_never_offered_as_the_company_domain(
    session: AsyncSession, super_admin: User, tenant: Tenant, configured: FakeProduct
) -> None:
    tenant.settings = {**tenant.settings, "profile": {"contact_email": "acme.hr@gmail.com"}}
    await session.commit()
    await products.link(session, super_admin, tenant.id, "interview", request=None)
    # No admin and no website on file either, so no domain is offered at all.
    assert configured.calls[0]["body"]["domain"] is None


async def test_unknown_product_is_404(
    session: AsyncSession, super_admin: User, tenant: Tenant, configured: FakeProduct
) -> None:
    with pytest.raises(HTTPException) as missing:
        await products.link(session, super_admin, tenant.id, "payroll-x", request=None)
    assert missing.value.status_code == 404


async def test_a_launch_can_land_on_a_page_inside_the_product_only(
    session: AsyncSession,
    super_admin: User,
    hr_user: User,
    tenant: Tenant,
    configured: FakeProduct,
) -> None:
    await products.link(session, super_admin, tenant.id, "interview", request=None)
    hr = await as_caller(session, hr_user)
    url = (await products.launch(session, hr, "interview", to="/job-openings/aB3_x-9/candidates"))[
        "url"
    ]
    fragment = dict(part.split("=", 1) for part in urlparse(url).fragment.split("&"))
    assert fragment["next"] == "/job-openings/aB3_x-9/candidates"
    jwt.decode(
        fragment["token"], SECRETS["interview"], algorithms=["HS256"], audience="askmira:interview"
    )
    # Never another site, a protocol-relative path or anything with a query.
    for bad in ("https://evil.test", "//evil.test", "/a?b=c", "/a#b", "jobs", "/a b"):
        with pytest.raises(HTTPException) as refused:
            await products.launch(session, hr, "interview", to=bad)
        assert refused.value.status_code == 422, bad
