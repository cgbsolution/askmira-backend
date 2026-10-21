"""Connecting this server to a product, from the platform console — no .env edits.

The product's own super admin generates a connection key there. It carries the
product's addresses and a fresh secret, and is good for 30 minutes:

    amk1.<base64url JSON {"p": product, "api": url, "app": url, "s": secret, "exp": unix}>

The platform's super admin pastes it here. AskMira proves it holds the secret
with a signed handshake (client.handshake); the product then accepts that
secret for AskMira's calls, and AskMira keeps the connection, encrypted, in
`product_connections`. Disconnecting drops the row.

`endpoint()` is where every caller learns where a product lives: the console's
connection first, then the INTERVIEW_TOOL_… / EXPENSE_TOOL_… settings, so a
server set up the old way keeps working.

Platform-wide rows, one per product, read by primary key: there is nothing to
scope by tenant. Only the platform's own super admin (never an operator acting
in a workspace) may see or change them.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction
from app.models.product_link import ProductConnection
from app.models.user import User, UserRole
from app.services import audit
from app.services.products import client
from app.services.products.catalogue import PRODUCTS, Product
from app.services.products.settings import MIN_SECRET_LENGTH, Endpoint
from app.services.products.settings import endpoint as env_endpoint

__all__ = [
    "KEY_PREFIX",
    "connect",
    "decode_key",
    "disconnect",
    "endpoint",
    "require_super",
    "status_all",
    "verify",
]

KEY_PREFIX = "amk1."


def require_super(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN or getattr(user, "operator_grant_id", None):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the platform's super admin links products.",
        )


def _product(key: str) -> Product:
    product = PRODUCTS.get(key)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such product.")
    return product


async def endpoint(session: AsyncSession, product: str) -> Endpoint | None:
    """Where the product lives and the secret it shares, or None when it is not connected."""
    row = await session.get(ProductConnection, product)
    if row is not None:
        return Endpoint(api_url=row.api_url, app_url=row.app_url, secret=row.secret_encrypted)
    return env_endpoint(product)


# ── The key ──────────────────────────────────────────────────────────────────


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _url(value: Any, what: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _bad(f"The key's {what} is not a web address.")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise _bad(f"The key's {what} must use https.")
    return text


def decode_key(key: str) -> dict[str, Any]:
    """The product, addresses and secret in a connection key; refuses a bad or stale one."""
    text = "".join(key.split())
    if not text.startswith(KEY_PREFIX):
        raise _bad("That is not an AskMira connection key. It starts with “amk1.”.")
    body = text[len(KEY_PREFIX) :]
    try:
        raw = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        raise _bad("That connection key is damaged. Copy it again from the product.") from None
    if not isinstance(raw, dict):
        raise _bad("That connection key is damaged. Copy it again from the product.")
    product = str(raw.get("p") or "")
    if product not in PRODUCTS:
        raise _bad("That key is for a product AskMira does not know.")
    secret = str(raw.get("s") or "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise _bad("That connection key is damaged. Copy it again from the product.")
    try:
        expires = int(raw.get("exp") or 0)
    except (TypeError, ValueError):
        expires = 0
    if expires < int(datetime.now(UTC).timestamp()):
        raise _bad("That connection key has expired. Generate a new one in the product.")
    return {
        "product": product,
        "api_url": _url(raw.get("api"), "server address"),
        "app_url": _url(raw.get("app"), "app address"),
        "secret": secret,
    }


# ── The console ──────────────────────────────────────────────────────────────


def _read(product: Product, row: ProductConnection | None) -> dict[str, Any]:
    env = env_endpoint(product.key)
    source = "console" if row else "environment" if env else None
    return {
        "key": product.key,
        "name": product.name,
        "brand": product.brand,
        "connected": source is not None,
        "source": source,
        "api_url": row.api_url if row else env.api_url if env else None,
        "app_url": row.app_url if row else env.app_url if env else None,
        "connected_at": row.created_at.isoformat() if row and row.created_at else None,
        "verified_at": row.verified_at.isoformat() if row and row.verified_at else None,
    }


async def status_all(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    require_super(user)
    return [
        _read(product, await session.get(ProductConnection, product.key))
        for product in PRODUCTS.values()
    ]


def _refused(brand: str, exc: client.ProductError) -> HTTPException:
    if exc.status == 401:
        detail = (
            f"{brand} did not accept this key. It may have been replaced by a newer one; "
            "generate a new key there and paste it again."
        )
    elif exc.status == 404:
        detail = (
            f"{brand} at that address has no AskMira connection endpoint. Check that it is "
            "up to date, and that the key came from the right server."
        )
    elif exc.status is None:
        detail = f"AskMira could not reach {brand} at the key's server address."
    else:
        detail = f"{brand} refused the connection: {exc.detail or exc}"
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail[:300])


async def _handshake(product: Product, target: Endpoint) -> None:
    try:
        answer = await client.handshake(target, product.key)
    except client.ProductError as exc:
        raise _refused(product.brand, exc) from exc
    if answer.get("product") != product.key:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"The server at that address is not {product.brand}.",
        )


async def connect(
    session: AsyncSession, user: User, product_key: str, key: str, *, request: Request | None
) -> dict[str, Any]:
    """Check the key with the product, then keep it as this server's connection."""
    require_super(user)
    product = _product(product_key)
    decoded = decode_key(key)
    if decoded["product"] != product.key:
        theirs = PRODUCTS[decoded["product"]].brand
        raise _bad(f"That key is for {theirs}, not {product.brand}.")
    target = Endpoint(
        api_url=decoded["api_url"], app_url=decoded["app_url"], secret=decoded["secret"]
    )
    await _handshake(product, target)

    row = await session.get(ProductConnection, product.key)
    before = {"api_url": row.api_url, "app_url": row.app_url} if row else None
    if row is None:
        row = ProductConnection(product=product.key)
        session.add(row)
    row.api_url = target.api_url
    row.app_url = target.app_url
    row.secret_encrypted = target.secret
    row.connected_by_user_id = user.id
    row.verified_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="product_connection",
        actor_user_id=user.id,
        actor_type="platform",
        changes={
            "product": product.key,
            "before": before,
            "after": {"api_url": target.api_url, "app_url": target.app_url},
        },
        summary=f"{product.brand} connected to this server from the console",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _read(product, row)


async def verify(session: AsyncSession, user: User, product_key: str) -> dict[str, Any]:
    """Ask the product whether it still accepts this server's secret."""
    require_super(user)
    product = _product(product_key)
    target = await endpoint(session, product.key)
    if target is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not connected.")
    await _handshake(product, target)
    row = await session.get(ProductConnection, product.key)
    if row is not None:
        row.verified_at = datetime.now(UTC)
        await session.commit()
    return _read(product, row)


async def disconnect(
    session: AsyncSession, user: User, product_key: str, *, request: Request | None
) -> dict[str, Any]:
    """Forget the console's connection. Environment settings, if any, take over again."""
    require_super(user)
    product = _product(product_key)
    row = await session.get(ProductConnection, product.key)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="There is no connection made from the console to remove.",
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="product_connection",
        actor_user_id=user.id,
        actor_type="platform",
        changes={"product": product.key, "before": {"api_url": row.api_url}},
        summary=f"{product.brand} disconnected from this server in the console",
        request=request,
    )
    await session.delete(row)
    await session.commit()
    return _read(product, None)
