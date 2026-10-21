"""Settings → Connectors: a workspace admin connects a product, like "Sign in with…".

The product is faked with an httpx MockTransport that behaves as a real one
must: it hands out a one-time code only against a PKCE challenge, swaps it for a
secret only with the matching verifier, and from then on checks every call for
this workspace with that workspace's own secret.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import types
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.products import client as product_client
from app.services.products import connections, connectors
from app.services.products import settings as product_settings
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

API = "https://api.anytimehire.test"
APP = "https://app.anytimehire.test"


class FakeProduct:
    """AnytimeHire's side of Connect: Allow, the code swap, then signed calls."""

    def __init__(self) -> None:
        self.codes: dict[str, dict[str, str]] = {}
        self.secrets: dict[str, str] = {}  # tenant id → the secret issued to it
        self.calls: list[dict[str, Any]] = []

    def allow(self, authorize_url: str) -> tuple[str, str]:
        """What /connect/askmira does when the organisation's admin presses Allow."""
        query = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        code = secrets.token_urlsafe(32)
        self.codes[code] = {"tenant_id": query["tenant_id"], "challenge": query["challenge"]}
        return code, query["state"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content)
        if path.endswith("/connect/token"):
            issued = self.codes.pop(body["code"], None)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(body["verifier"].encode()).digest())
                .decode()
                .rstrip("=")
            )
            if issued is None or issued["challenge"] != challenge:
                return httpx.Response(400, json={"detail": "This connect code is not valid."})
            secret = secrets.token_hex(32)
            self.secrets[issued["tenant_id"]] = secret
            return httpx.Response(
                200,
                json={
                    "product": "interview",
                    "brand": "AnytimeHire",
                    "secret": secret,
                    "external_ref": "42",
                    "organization": "CGB",
                },
            )
        token = request.headers["Authorization"].removeprefix("Bearer ")
        tenant = jwt.decode(token, options={"verify_signature": False})["sub"]
        claims = jwt.decode(
            token,
            self.secrets.get(tenant, "x" * 40),
            algorithms=["HS256"],
            audience="askmira:interview",
            issuer="askmira",
        )
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        self.calls.append({"path": path, "use": claims["use"], "tenant": tenant})
        if path.endswith("/connect/revoke"):
            self.secrets.pop(tenant, None)
            return httpx.Response(200, json={"disconnected": True})
        return httpx.Response(
            200, json={"created": len(body["users"]), "updated": 0, "deactivated": 0, "skipped": []}
        )


@pytest.fixture
def product(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProduct]:
    for prefix in ("INTERVIEW_TOOL", "EXPENSE_TOOL"):
        for suffix in ("API_URL", "APP_URL", "SECRET"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    monkeypatch.setenv("INTERVIEW_TOOL_API_URL", API)
    monkeypatch.setenv("INTERVIEW_TOOL_APP_URL", APP)
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
async def admin(session: AsyncSession, hr_user: User) -> User:
    return await as_caller(session, hr_user)


async def test_a_workspace_admin_connects_with_one_click_and_its_own_secret(
    session: AsyncSession, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    before = {row["key"]: row for row in await connectors.overview(session, admin)}
    assert before["interview"]["available"] is True and before["interview"]["connected"] is False
    assert before["expense"]["available"] is False  # this server does not know where it is

    started = await connectors.start(session, admin, "interview")
    url = urlparse(started["authorize_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{APP}/connect/askmira"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert query["tenant_id"] == str(tenant.id) and query["tenant_name"] == "Acme Technologies"
    assert query["redirect_uri"].endswith("/settings/connectors/callback")
    # The verifier stays on this server: the browser only ever sees its hash.
    assert "v" not in query and len(query["challenge"]) == 43

    code, state = product.allow(started["authorize_url"])
    row = await connectors.complete(
        session, admin, "interview", code=code, state=state, request=None
    )
    assert row["connected"] is True and row["via"] == "connectors"
    assert row["organization"] == "CGB" and row["last_sync"]["created"] >= 1

    # Every later call is signed with the secret issued to this workspace alone.
    target = await connections.endpoint(session, "interview", tenant.id)
    assert target is not None and target.secret == product.secrets[str(tenant.id)]
    assert target.api_url == API and target.app_url == APP
    assert product.calls[-1] == {
        "path": "/integrations/askmira/users/sync",
        "use": "provision",
        "tenant": str(tenant.id),
    }
    stored = await session.scalar(text("SELECT secret_encrypted FROM product_links"))
    assert stored.startswith("encf:v1:") and target.secret not in stored
    rows = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "product_link"))
    ).all()
    assert rows and all(target.secret not in json.dumps(r.changes) for r in rows)
    refreshed = await session.get(Tenant, tenant.id)
    assert refreshed.settings["linked_products"] == {"interview": True}

    gone = await connectors.disconnect(session, admin, "interview", request=None)
    assert gone["connected"] is False
    assert product.calls[-1]["use"] == "revoke" and str(tenant.id) not in product.secrets
    link = await session.scalar(select(ProductLink))
    assert link.secret_encrypted is None and link.status == "disabled"
    assert await connections.endpoint(session, "interview", tenant.id) is None


async def test_the_state_belongs_to_the_admin_who_started_and_the_code_to_this_server(
    session: AsyncSession, admin: User, tenant: Tenant, product: FakeProduct
) -> None:
    started = await connectors.start(session, admin, "interview")
    code, state = product.allow(started["authorize_url"])

    other = User(
        tenant_id=tenant.id,
        email=f"other-{uuid.uuid4().hex[:6]}@acme.test",
        password_hash="x",
        full_name="Other Admin",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(other)
    await session.commit()
    other = await as_caller(session, other)
    for who, bad_state in ((other, state), (admin, "encf:v1:not-ours"), (admin, "plain")):
        with pytest.raises(HTTPException) as refused:
            await connectors.complete(
                session, who, "interview", code=code, state=bad_state, request=None
            )
        assert refused.value.status_code == 400

    # A code the product did not issue for our challenge is refused by the product.
    with pytest.raises(HTTPException) as refused:
        await connectors.complete(
            session, admin, "interview", code="a-code-nobody-issued-123", state=state, request=None
        )
    assert refused.value.status_code == 502
    assert await connections.endpoint(session, "interview", tenant.id) is None


async def test_only_the_workspace_admin_connects(
    session: AsyncSession, tenant: Tenant, product: FakeProduct
) -> None:
    person = User(
        tenant_id=tenant.id,
        email=f"emp-{uuid.uuid4().hex[:6]}@acme.test",
        password_hash="x",
        full_name="Employee",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    session.add(person)
    await session.commit()
    person = await as_caller(session, person)
    with pytest.raises(HTTPException) as refused:
        await connectors.overview(session, person)
    assert refused.value.status_code == 403
    with pytest.raises(HTTPException) as refused:
        await connectors.start(session, person, "interview")
    assert refused.value.status_code == 403


async def test_without_a_server_encryption_key_connect_says_why_instead_of_failing(
    session: AsyncSession, admin: User, product: FakeProduct, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Production with no SECRET_ENCRYPTION_KEY: the product's key could not be stored safely.
    monkeypatch.setattr(connectors, "can_encrypt", lambda: False)
    row = {r["key"]: r for r in await connectors.overview(session, admin)}["interview"]
    assert row["available"] is False and row["unavailable_reason"] == "server_key"
    with pytest.raises(HTTPException) as refused:
        await connectors.start(session, admin, "interview")
    assert refused.value.status_code == 503
    assert "SECRET_ENCRYPTION_KEY" in refused.value.detail
