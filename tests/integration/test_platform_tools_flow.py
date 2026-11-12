"""Platform → Tools: the super admin sets a workspace up in our other products.

The product is faked behind an httpx MockTransport that behaves as the real
ones must: platform calls are checked with the platform key (use=platform,
bound to the body), workspace calls with the secret the product issued for that
workspace alone, and it refuses to make a second organisation for an admin who
already has one unless told to.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import types
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import httpx
import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.products import client as product_client
from app.services.products import connections
from app.services.products import platform as tools
from app.services.products import settings as product_settings
from tests.integration.conftest import requires_db

pytestmark = requires_db

PLATFORM_KEY = "platform-key-0123456789abcdef0123456789abcdef"


class FakeProduct:
    """One product (Saleinx-like) as the platform contract describes it."""

    def __init__(self, product: str = "saleinx") -> None:
        self.product = product
        self.platform_key = PLATFORM_KEY
        #: org ref -> {"name", "admins": [emails], "askmira": tenant id or None, "secret"}
        self.orgs: dict[str, dict[str, Any]] = {
            "org-old": {"name": "Acme Sales (old)", "admins": ["owner@elsewhere.test"]}
        }
        self.calls: list[str] = []
        self.revoked: list[str] = []
        self.synced: list[list[dict[str, Any]]] = []

    def _claims(self, request: httpx.Request, secret: str) -> dict[str, Any]:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=f"askmira:{self.product}",
            issuer="askmira",
        )
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        return claims

    def _by_tenant(self, tenant_id: str) -> tuple[str, dict[str, Any]] | None:
        for ref, org in self.orgs.items():
            if org.get("askmira") == tenant_id:
                return ref, org
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        body = json.loads(request.content)
        if "/platform/" in path:
            try:
                claims = self._claims(request, self.platform_key)
            except jwt.PyJWTError:
                return httpx.Response(401, json={"error": "bad key"})
            assert claims["use"] == "platform" and claims["sub"] == "platform"
            return self._platform(path.rsplit("/", 1)[1], body)
        found = self._by_tenant(body["tenant_id"])
        if found is None:
            return httpx.Response(404, json={"error": "Not found"})
        ref, org = found
        claims = self._claims(request, org["secret"])
        assert claims["sub"] == body["tenant_id"]
        if path.endswith("/users/sync"):
            self.synced.append(body["users"])
            emails = {u["email"] for u in body["users"]}
            members = [{"email": e, "role": "owner"} for e in org["admins"] if e in emails]
            return httpx.Response(
                200, json={"created": 0, "updated": len(members), "deactivated": 0,
                           "skipped": [], "members": members},
            )  # fmt: skip
        if path.endswith("/connect/revoke"):
            org["askmira"] = None
            org["secret"] = None
            self.revoked.append(ref)
            return httpx.Response(200, json={"disconnected": True})
        return httpx.Response(404, json={"error": "Not found"})

    def _platform(self, what: str, body: dict[str, Any]) -> httpx.Response:
        if what == "ping":
            return httpx.Response(200, json={"product": self.product, "ready": True})
        if what == "organizations":
            q = body["q"].lower()
            hits = [
                {"ref": ref, "name": o["name"], "admins": o["admins"],
                 "connected_to": o.get("askmira")}
                for ref, o in self.orgs.items()
                if q in o["name"].lower() or any(q in a for a in o["admins"])
            ]  # fmt: skip
            return httpx.Response(200, json={"organizations": hits})
        assert what == "provision"
        tenant_id = body["tenant_id"]
        admin = body["admin"]["email"]
        ref = body.get("organization_ref")
        created = False
        if ref:
            org = self.orgs.get(ref)
            if org is None:
                return httpx.Response(404, json={"error": "No such organisation."})
            if org.get("askmira") not in (None, tenant_id):
                return httpx.Response(
                    409, json={"error": "Connected to another workspace.", "code": "taken"}
                )
        else:
            mine = [r for r, o in self.orgs.items() if admin in o["admins"]]
            if mine and not body.get("create_new"):
                return httpx.Response(
                    409,
                    json={
                        "error": f"{admin} already has an organisation here.",
                        "code": "existing",
                        "organizations": [
                            {"ref": r, "name": self.orgs[r]["name"], "admins": [admin]}
                            for r in mine
                        ],
                    },
                )
            ref = f"org-{len(self.orgs) + 1}"
            self.orgs[ref] = {"name": body["company"]["name"], "admins": [admin]}
            org = self.orgs[ref]
            created = True
        org["askmira"] = tenant_id
        org["secret"] = secrets.token_hex(32)
        return httpx.Response(
            200,
            json={
                "product": self.product,
                "secret": org["secret"],
                "external_ref": ref,
                "organization": org["name"],
                "created": created,
                "admin_created": created,
            },
        )


@pytest.fixture
def product(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProduct]:
    monkeypatch.setenv("SALEINX_TOOL_API_URL", "https://saleinx-api.test")
    monkeypatch.setenv("SALEINX_TOOL_APP_URL", "https://saleinx.test")
    monkeypatch.delenv("SALEINX_TOOL_SECRET", raising=False)
    monkeypatch.delenv("SALEINX_TOOL_PLATFORM_SECRET", raising=False)
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
async def super_admin(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email="platform@acme.test",
        password_hash="x",
        full_name="Platform",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def admin(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email="Admin@Acme.test",
        password_hash="x",
        full_name="Asha Admin",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def http(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def _with_key(session: AsyncSession, super_admin: User) -> None:
    await tools.set_key(session, super_admin, "saleinx", secret=PLATFORM_KEY, request=None)


# ── The platform's key ───────────────────────────────────────────────────────


async def test_generated_key_is_shown_once_and_kept_encrypted(
    session: AsyncSession, super_admin: User, product: FakeProduct
) -> None:
    out = await tools.set_key(session, super_admin, "saleinx", secret=None, request=None)
    assert out["generated"] and len(out["generated"]) >= 60
    assert out["key_set"] and out["key_source"] == "platform"
    listed = {t["key"]: t for t in await tools.tools(session, super_admin)}
    assert "generated" not in listed["saleinx"]
    assert all(out["generated"] not in json.dumps(t) for t in listed.values())
    raw = (
        await session.execute(
            text("SELECT secret_encrypted FROM platform_product_keys WHERE product = 'saleinx'")
        )
    ).scalar_one()
    assert out["generated"] not in raw, "the key is encrypted at rest"


async def test_a_short_or_spaced_key_is_refused(
    session: AsyncSession, super_admin: User, product: FakeProduct
) -> None:
    for bad in ("short", "x" * 20 + " " + "y" * 20):
        with pytest.raises(HTTPException) as refused:
            await tools.set_key(session, super_admin, "saleinx", secret=bad, request=None)
        assert refused.value.status_code == 422


async def test_check_says_whether_the_product_takes_the_key(
    session: AsyncSession, super_admin: User, product: FakeProduct
) -> None:
    await _with_key(session, super_admin)
    assert (await tools.check(session, super_admin, "saleinx"))["last_check_ok"] is True
    product.platform_key = "another-key-0123456789abcdef0123456789abcd"
    out = await tools.check(session, super_admin, "saleinx")
    assert out["last_check_ok"] is False
    assert "ASKMIRA_PLATFORM_SECRET" in out["last_check_message"]


async def test_only_the_super_admin_manages_tools(
    http: httpx.AsyncClient, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    for method, url in (
        ("GET", "/api/v1/platform/tools"),
        ("PUT", "/api/v1/platform/tools/saleinx/key"),
        ("POST", f"/api/v1/platform/workspaces/{tenant.id}/products/saleinx"),
        ("GET", "/api/v1/platform/connections"),
    ):
        response = await http.request(method, url, json={}, headers=_bearer(admin))
        assert response.status_code == 403, (method, url, response.text)
    assert product.calls == []


# ── A workspace in a product ─────────────────────────────────────────────────


async def test_provision_creates_the_organisation_and_connects(
    session: AsyncSession,
    super_admin: User,
    admin: User,
    tenant: Tenant,
    product: FakeProduct,
) -> None:
    await _with_key(session, super_admin)
    out = await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    assert out["outcome"] == "connected" and out["created"] and out["admin_created"]
    assert out["admin_email"] == "admin@acme.test"
    assert out["admin_note"] == "admin@acme.test is its admin."
    assert out["tool"]["connected"] and out["tool"]["via"] == "platform"

    link = await session.scalar(select(ProductLink).where(ProductLink.tenant_id == tenant.id))
    assert link is not None and link.via == "platform" and link.secret_encrypted
    assert link.api_url is None, "follows the product's standard address"
    # Every later call for the workspace uses its own secret, not the platform key.
    target = await connections.endpoint(session, "saleinx", tenant.id)
    assert target is not None and target.secret == link.secret_encrypted != PLATFORM_KEY
    assert product.synced and "admin@acme.test" in {p["email"] for p in product.synced[-1]}
    assert link.member_roles == {"admin@acme.test": "owner"}
    refreshed = await session.get(Tenant, tenant.id)
    assert (refreshed.settings or {}).get("linked_products", {}).get("saleinx") is True
    entries = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id, AuditLog.entity_type == "product_link"
            )
        )
    ).all()
    assert any("organisation created by the platform" in (e.summary or "") for e in entries)


async def test_a_second_provision_is_refused_while_connected(
    session: AsyncSession, super_admin: User, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    await _with_key(session, super_admin)
    await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    with pytest.raises(HTTPException) as refused:
        await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    assert refused.value.status_code == 409


async def test_an_admin_with_an_organisation_there_is_asked_first(
    session: AsyncSession, super_admin: User, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    await _with_key(session, super_admin)
    product.orgs["org-old"]["admins"].append("admin@acme.test")
    out = await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    assert out["outcome"] == "existing"
    assert [o["ref"] for o in out["organizations"]] == ["org-old"]
    nothing = select(ProductLink).where(ProductLink.tenant_id == tenant.id)
    assert await session.scalar(nothing) is None

    # Link the one they already have: nothing new is made there.
    linked = await tools.provision(
        session, super_admin, tenant.id, "saleinx", organization_ref="org-old", request=None
    )
    assert linked["outcome"] == "connected" and not linked["created"]
    assert linked["tool"]["organization"] == "Acme Sales (old)"
    assert "keeps the role they have there" in linked["admin_note"]
    assert len(product.orgs) == 1


async def test_create_new_anyway_makes_a_second_organisation(
    session: AsyncSession, super_admin: User, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    await _with_key(session, super_admin)
    product.orgs["org-old"]["admins"].append("admin@acme.test")
    out = await tools.provision(
        session, super_admin, tenant.id, "saleinx", create_new=True, request=None
    )
    assert out["outcome"] == "connected" and out["created"]
    assert len(product.orgs) == 2


async def test_search_finds_organisations_by_name_or_admin(
    session: AsyncSession, super_admin: User, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    await _with_key(session, super_admin)
    hits = await tools.organizations(session, super_admin, tenant.id, "saleinx", q="elsewhere")
    assert hits == [
        {
            "ref": "org-old",
            "name": "Acme Sales (old)",
            "admins": ["owner@elsewhere.test"],
            "connected_to": None,
        }
    ]
    with pytest.raises(HTTPException) as refused:
        await tools.organizations(session, super_admin, tenant.id, "saleinx", q=" a ")
    assert refused.value.status_code == 422


async def test_without_a_key_nothing_is_sent(
    session: AsyncSession, super_admin: User, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    with pytest.raises(HTTPException) as refused:
        await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    assert refused.value.status_code == 409
    assert "Platform → Tools" in refused.value.detail
    assert product.calls == []


async def test_a_wrong_key_says_where_to_set_it(
    session: AsyncSession, super_admin: User, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    await _with_key(session, super_admin)
    product.platform_key = "another-key-0123456789abcdef0123456789abcd"
    with pytest.raises(HTTPException) as refused:
        await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    assert refused.value.status_code == 409
    assert "ASKMIRA_PLATFORM_SECRET" in refused.value.detail


async def test_a_workspace_without_an_admin_is_refused(
    session: AsyncSession, super_admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    await _with_key(session, super_admin)
    with pytest.raises(HTTPException) as refused:
        await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    assert refused.value.status_code == 409
    assert product.calls == []


async def test_disconnect_tells_the_product_and_can_connect_again(
    session: AsyncSession, super_admin: User, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    await _with_key(session, super_admin)
    first = await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    ref = first["tool"]["external_ref"]
    out = await tools.disconnect(session, super_admin, tenant.id, "saleinx", request=None)
    assert out["connected"] is False and product.revoked == [ref]
    assert await connections.endpoint(session, "saleinx", tenant.id) is None

    # The organisation is still there: link it again rather than making another.
    again = await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    assert again["outcome"] == "existing"
    relinked = await tools.provision(
        session, super_admin, tenant.id, "saleinx", organization_ref=ref, request=None
    )
    assert relinked["outcome"] == "connected" and not relinked["created"]


async def test_the_workspace_admin_sees_the_platform_connection_in_connectors(
    http: httpx.AsyncClient,
    session: AsyncSession,
    super_admin: User,
    admin: User,
    tenant: Tenant,
    product: FakeProduct,
) -> None:
    await _with_key(session, super_admin)
    await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    response = await http.get("/api/v1/connectors", headers=_bearer(admin))
    assert response.status_code == 200, response.text
    saleinx = next(c for c in response.json() if c["key"] == "saleinx")
    assert saleinx["connected"] and saleinx["via"] == "platform"


async def test_the_console_round_trip_over_http(
    http: httpx.AsyncClient,
    super_admin: User,
    admin: User,
    tenant: Tenant,
    product: FakeProduct,
) -> None:
    headers = _bearer(super_admin)
    made = await http.put("/api/v1/platform/tools/saleinx/key", json={}, headers=headers)
    assert made.status_code == 200, made.text
    product.platform_key = made.json()["generated"]
    checked = await http.post("/api/v1/platform/tools/saleinx/check", headers=headers)
    assert checked.json()["last_check_ok"] is True

    base = f"/api/v1/platform/workspaces/{tenant.id}/products"
    listed = (await http.get(base, headers=headers)).json()
    assert {t["key"]: t["ready"] for t in listed}["saleinx"] is True
    connected = await http.post(f"{base}/saleinx", headers=headers)
    assert connected.status_code == 200, connected.text
    assert connected.json()["outcome"] == "connected"
    synced = await http.post(f"{base}/saleinx/sync", headers=headers)
    assert synced.status_code == 200 and synced.json()["last_error"] is None
    found = await http.post(f"{base}/saleinx/organizations", json={"q": "acme"}, headers=headers)
    assert found.status_code == 200 and any(o["ref"] == "org-2" for o in found.json())
    gone = await http.delete(f"{base}/saleinx", headers=headers)
    assert gone.status_code == 200 and gone.json()["connected"] is False


# ── The console's overview grid ──────────────────────────────────────────────


async def test_connections_grid_shows_every_workspace_in_every_tool(
    session: AsyncSession,
    http: httpx.AsyncClient,
    super_admin: User,
    admin: User,
    tenant: Tenant,
    product: FakeProduct,
) -> None:
    other = Tenant(name="Beta Works", slug="beta-works", is_active=False)
    session.add(other)
    await session.commit()

    before = (await http.get("/api/v1/platform/connections", headers=_bearer(super_admin))).json()
    rows = {row["workspace"]: row for row in before}
    assert {tenant.slug, "beta-works"} <= set(rows)
    assert rows["beta-works"]["is_active"] is False
    sale = {t["key"]: t for t in rows[tenant.slug]["tools"]}["saleinx"]
    assert sale["connected"] is False and sale["ready"] is False, "no platform key yet"

    await _with_key(session, super_admin)
    await tools.provision(session, super_admin, tenant.id, "saleinx", request=None)
    after = {
        row["workspace"]: row
        for row in (
            await http.get("/api/v1/platform/connections", headers=_bearer(super_admin))
        ).json()
    }
    sale = {t["key"]: t for t in after[tenant.slug]["tools"]}["saleinx"]
    assert sale["connected"] and sale["ready"] and sale["via"] == "platform"
    beta = {t["key"]: t for t in after["beta-works"]["tools"]}["saleinx"]
    assert beta["connected"] is False and beta["ready"] is True
    # Nothing secret leaves in the grid.
    assert "secret" not in json.dumps(after).lower()
