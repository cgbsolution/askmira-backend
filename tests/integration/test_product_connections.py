"""Connecting this server to a product from the console, with the product's connection key.

The product is faked with an httpx MockTransport that checks the handshake as a
real product must: signed with the key's secret, for that product, use
"handshake", bound to the body.
"""

from __future__ import annotations

import base64
import hashlib
import json
import types
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.products import client as product_client
from app.services.products import connections
from app.services.products import settings as product_settings
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

SECRET = "fresh-secret-from-anytimehire-0123456789abcdef"


def make_key(
    product: str = "interview",
    *,
    secret: str = SECRET,
    api: str = "https://api.anytimehire.test",
    expires_in: int = 1800,
) -> str:
    body = {
        "p": product,
        "api": api,
        "app": "https://app.anytimehire.test",
        "s": secret,
        "exp": int((datetime.now(UTC) + timedelta(seconds=expires_in)).timestamp()),
    }
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return f"amk1.{raw}"


class FakeProduct:
    """Accepts a handshake signed with the secret it handed out, as the product does."""

    def __init__(self) -> None:
        self.accepts = SECRET
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        try:
            claims = jwt.decode(
                token,
                self.accepts,
                algorithms=["HS256"],
                audience="askmira:interview",
                issuer="askmira",
            )
        except jwt.InvalidTokenError:
            return httpx.Response(401, json={"detail": "This AskMira link is not valid."})
        assert claims["use"] == "handshake"
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        self.calls.append({"path": request.url.path, "claims": claims})
        return httpx.Response(200, json={"product": "interview", "brand": "AnytimeHire"})


@pytest.fixture
def product(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProduct]:
    for prefix in ("INTERVIEW_TOOL", "EXPENSE_TOOL"):
        for suffix in ("API_URL", "APP_URL", "SECRET"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
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
async def platform(session: AsyncSession, tenant: Tenant) -> User:
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
    return await as_caller(session, user)


async def test_a_pasted_key_connects_the_product_without_env_settings(
    session: AsyncSession, platform: User, product: FakeProduct
) -> None:
    before = {row["key"]: row for row in await connections.status_all(session, platform)}
    assert before["interview"]["connected"] is False
    assert await connections.endpoint(session, "interview") is None

    row = await connections.connect(session, platform, "interview", make_key(), request=None)
    assert row["connected"] is True and row["source"] == "console"
    assert row["api_url"] == "https://api.anytimehire.test" and row["verified_at"]
    assert product.calls[0]["path"] == "/integrations/askmira/handshake"

    target = await connections.endpoint(session, "interview")
    assert target is not None and target.secret == SECRET
    # Encrypted at rest, and never written to the audit trail.
    stored = await session.scalar(text("SELECT secret_encrypted FROM product_connections"))
    assert stored.startswith("encf:v1:") and SECRET not in stored
    audit = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "product_connection"))
    ).all()
    assert len(audit) == 1 and SECRET not in json.dumps(audit[0].changes)

    assert (await connections.verify(session, platform, "interview"))["connected"] is True

    gone = await connections.disconnect(session, platform, "interview", request=None)
    assert gone["connected"] is False
    assert await connections.endpoint(session, "interview") is None


async def test_a_bad_stale_or_foreign_key_is_refused(
    session: AsyncSession, platform: User, product: FakeProduct
) -> None:
    cases = [
        ("not a key", 422),
        (make_key(expires_in=-5), 422),
        (make_key("expense"), 422),  # an ExpenseGenie key pasted against AnytimeHire
        (make_key(api="http://api.anytimehire.test"), 422),  # plain http off localhost
        (make_key(secret="short"), 422),
        (make_key(secret="a-secret-the-product-never-issued-0123456789"), 502),
    ]
    for key, code in cases:
        with pytest.raises(HTTPException) as refused:
            await connections.connect(session, platform, "interview", key, request=None)
        assert refused.value.status_code == code, key
    assert await connections.endpoint(session, "interview") is None


async def test_only_the_platform_super_admin_connects(
    session: AsyncSession, hr_user: User, product: FakeProduct
) -> None:
    with pytest.raises(HTTPException) as refused:
        await connections.connect(session, hr_user, "interview", make_key(), request=None)
    assert refused.value.status_code == 403
    with pytest.raises(HTTPException):
        await connections.status_all(session, hr_user)
