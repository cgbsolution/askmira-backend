"""Settings → Connectors: a workspace's admin connects AnytimeHire or ExpenseGenie.

Like "Sign in with…", started here and allowed there:

1. start     The admin presses Connect. AskMira makes a PKCE verifier and a
             state (encrypted: workspace, admin, product, verifier, addresses,
             expiry) and sends the browser to the product's /connect/askmira.
2.           An admin of the organisation in the product signs in there and
             presses Allow; the product sends the browser back to
             /settings/connectors/callback with a one-time code.
3. complete  AskMira's server swaps the code and the verifier — which never left
             this server — for a secret the product issued for this workspace
             alone, keeps it encrypted on the workspace's product link, and
             sends the workspace's people across.

The product checks every later call with that workspace's secret, so it reaches
only the one organisation that allowed it. Either side can disconnect. Nobody
outside the workspace — no platform owner, no shared server-wide key — is
involved.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.crypto import EncryptionError, decrypt_field, encrypt_field
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.auth import web_app_url
from app.services.products import client
from app.services.products import service as links
from app.services.products.catalogue import PRODUCTS, Product
from app.services.products.settings import Endpoint, addresses

__all__ = ["CALLBACK_PATH", "complete", "disconnect", "overview", "start", "sync_now"]

CALLBACK_PATH = "/settings/connectors/callback"
#: How long the admin has to sign in to the product and press Allow.
STATE_LIFETIME = timedelta(minutes=15)

DESCRIPTIONS = {
    "interview": (
        "Hiring and interviews. Your HR and hiring managers open it without a second "
        "login; jobs, candidates and interviews show under Hiring and in Ask Mira."
    ),
    "expense": (
        "Expense claims. Everyone opens it without a second login; their claims, "
        "approvals and totals show under Expenses and in Ask Mira."
    ),
}


def _require_admin(user: User) -> None:
    if not permissions.is_admin(user) or getattr(user, "operator_grant_id", None):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only your workspace's admin connects products.",
        )


def _product(key: str) -> Product:
    product = PRODUCTS.get(key)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such product.")
    return product


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def _link(session: AsyncSession, user: User, product: str) -> ProductLink | None:
    return await session.scalar(
        tenant_scoped(select(ProductLink), ProductLink, user).where(ProductLink.product == product)
    )


def _read(product: Product, row: ProductLink | None) -> dict[str, Any]:
    connected = row is not None and row.status == links.ACTIVE
    via = None
    if connected:
        via = "connectors" if row.secret_encrypted else "platform"
    return {
        "key": product.key,
        "name": product.name,
        "brand": product.brand,
        "description": DESCRIPTIONS.get(product.key, ""),
        "available": addresses(product.key) is not None,
        "connected": connected,
        "via": via,
        "organization": row.organization_name if connected else None,
        "last_synced_at": (
            row.last_synced_at.isoformat() if connected and row.last_synced_at else None
        ),
        "last_sync": row.last_sync if connected else None,
        "last_error": row.last_error if connected else None,
    }


async def overview(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Each product: whether this workspace is connected, to what, and how the last sync went."""
    _require_admin(user)
    return [_read(p, await _link(session, user, p.key)) for p in PRODUCTS.values()]


async def start(session: AsyncSession, user: User, product_key: str) -> dict[str, str]:
    """Where to send the admin's browser: the product's Allow page."""
    _require_admin(user)
    product = _product(product_key)
    where = addresses(product.key)
    if where is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This AskMira server does not know where {product.brand} is yet.",
        )
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Your workspace is not active."
        )
    api_url, app_url = where
    verifier = secrets.token_urlsafe(48)
    state = encrypt_field(
        json.dumps(
            {
                "tid": str(user.tenant_id),
                "uid": str(user.id),
                "p": product.key,
                "v": verifier,
                "api": api_url,
                "app": app_url,
                "exp": int((datetime.now(UTC) + STATE_LIFETIME).timestamp()),
            }
        )
    )
    query = urlencode(
        {
            "tenant_id": str(user.tenant_id),
            "tenant_name": tenant.name,
            "redirect_uri": f"{web_app_url()}{CALLBACK_PATH}",
            "state": state,
            "challenge": _challenge(verifier),
        }
    )
    return {"authorize_url": f"{app_url}/connect/askmira?{query}"}


def _open_state(state: str, user: User, product: Product) -> dict[str, Any]:
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="This connect link is not valid any more. Press Connect again.",
    )
    if not state.startswith("encf:"):
        raise invalid
    try:
        data = json.loads(decrypt_field(state) or "")
    except (EncryptionError, ValueError):
        raise invalid from None
    if (
        not isinstance(data, dict)
        or data.get("tid") != str(user.tenant_id)
        or data.get("uid") != str(user.id)
        or data.get("p") != product.key
        or int(data.get("exp") or 0) < int(datetime.now(UTC).timestamp())
    ):
        raise invalid
    return data


def _refused(brand: str, exc: client.ProductError) -> HTTPException:
    if exc.status == 400:
        detail = "That connect code expired or was already used. Press Connect again."
    elif exc.status == 409 and exc.detail:
        detail = exc.detail
    elif exc.status is None:
        detail = f"AskMira could not reach {brand}. Try again in a minute."
    else:
        detail = f"{brand} refused the connection: {exc.detail or exc}"
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail[:300])


async def complete(
    session: AsyncSession,
    user: User,
    product_key: str,
    *,
    code: str,
    state: str,
    request: Request | None,
) -> dict[str, Any]:
    """The admin came back from Allow: swap the code for this workspace's secret, then sync."""
    _require_admin(user)
    product = _product(product_key)
    data = _open_state(state, user, product)
    try:
        answer = await client.connect_token(
            data["api"], product.key, code=code, verifier=data["v"], tenant_id=user.tenant_id
        )
    except client.ProductError as exc:
        raise _refused(product.brand, exc) from exc

    row = await _link(session, user, product.key)
    before = (
        {
            "status": row.status,
            "external_ref": row.external_ref,
            "via_connectors": bool(row.secret_encrypted),
        }
        if row
        else None
    )
    if row is None:
        row = ProductLink(tenant_id=user.tenant_id, product=product.key)
        session.add(row)
    row.status = links.ACTIVE
    row.external_ref = str(answer.get("external_ref") or "")[:120] or None
    row.organization_name = str(answer.get("organization") or "")[:200] or None
    row.api_url = data["api"]
    row.app_url = data["app"]
    row.secret_encrypted = answer["secret"]
    row.linked_by_user_id = user.id
    row.last_error = None
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is not None:
        links.mirror(tenant, product.key, True)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="product_link",
        entity_id=row.id,
        actor=user,
        changes={
            "before": before,
            "after": {
                "status": links.ACTIVE,
                "external_ref": row.external_ref,
                "organization": row.organization_name,
                "via_connectors": True,
            },
        },
        summary=(
            f"{product.brand} connected from Connectors to its organisation "
            f"“{row.organization_name or row.external_ref}”"
        ),
        request=request,
    )
    await session.commit()

    await links.sync(session, user.tenant_id, product.key, actor=user, request=request)
    return _read(product, await _link(session, user, product.key))


async def sync_now(
    session: AsyncSession, user: User, product_key: str, *, request: Request | None
) -> dict[str, Any]:
    _require_admin(user)
    product = _product(product_key)
    row = await _link(session, user, product.key)
    if row is None or row.status != links.ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not connected.")
    await links.sync(session, user.tenant_id, product.key, actor=user, request=request)
    return _read(product, await _link(session, user, product.key))


async def disconnect(
    session: AsyncSession, user: User, product_key: str, *, request: Request | None
) -> dict[str, Any]:
    """Disconnect: the product forgets this workspace's secret, and AskMira drops it too."""
    _require_admin(user)
    product = _product(product_key)
    row = await _link(session, user, product.key)
    if row is None or row.status != links.ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not connected.")
    told = True
    if row.secret_encrypted and row.api_url and row.app_url:
        target = Endpoint(api_url=row.api_url, app_url=row.app_url, secret=row.secret_encrypted)
        try:
            await client.revoke(target, product.key, user.tenant_id)
        except client.ProductError:
            # AskMira drops the secret below, and only AskMira held it: nothing can use it.
            told = False
    row.status = links.DISABLED
    row.secret_encrypted = None
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is not None:
        links.mirror(tenant, product.key, False)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="product_link",
        entity_id=row.id,
        actor=user,
        changes={"before": {"status": links.ACTIVE}, "after": {"status": links.DISABLED}},
        summary=(
            f"{product.brand} disconnected from Connectors"
            + ("" if told else f"; {product.brand} could not be reached to be told")
        ),
        request=request,
    )
    await session.commit()
    return _read(product, row)
