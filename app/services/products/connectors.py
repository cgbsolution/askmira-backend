"""Settings → Connectors: a workspace's admin connects AnytimeHire, ExpenseGenie or CGB Helpdesk.

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

import asyncio
import base64
import hashlib
import ipaddress
import json
import secrets
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.config import settings
from app.core.crypto import EncryptionError, can_encrypt, decrypt_field, encrypt_field
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

__all__ = [
    "CALLBACK_PATH",
    "complete",
    "disconnect",
    "overview",
    "reset_address",
    "set_address",
    "start",
    "sync_now",
]

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
    "helpdesk": (
        "IT tickets and assets. Everyone raises and follows tickets and sees the assets "
        "they hold under Helpdesk and Assets, without a second login; HR sees the "
        "company's tickets and assets, the IT team what the helpdesk shows them, and "
        "Ask Mira can read them."
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


def _where(product: Product, row: ProductLink | None) -> tuple[str, str, bool] | None:
    """(api_url, app_url, custom): the workspace's own address if its admin set one,
    else the standard one; None when neither is known."""
    standard = addresses(product.key)
    if row is not None and row.api_url and row.app_url:
        custom = standard is None or (row.api_url, row.app_url) != standard
        return row.api_url, row.app_url, custom
    if standard is None:
        return None
    return standard[0], standard[1], False


def _read(product: Product, row: ProductLink | None) -> dict[str, Any]:
    connected = row is not None and row.status == links.ACTIVE
    via = None
    if connected:
        via = "connectors" if row.secret_encrypted else "platform"
    where = _where(product, row)
    return {
        "key": product.key,
        "name": product.name,
        "brand": product.brand,
        "description": DESCRIPTIONS.get(product.key, ""),
        "available": where is not None and can_encrypt(),
        # Why Connect is off, when it is: "address" (where the product is) or
        # "server_key" (no SECRET_ENCRYPTION_KEY to keep its key safe with).
        "unavailable_reason": (
            "address" if where is None else "server_key" if not can_encrypt() else None
        ),
        # Where the product is for this workspace, and whether its admin set that.
        "api_url": where[0] if where else None,
        "app_url": where[1] if where else None,
        "custom_address": bool(where and where[2]),
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
    where = _where(product, await _link(session, user, product.key))
    if where is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This AskMira server does not know where {product.brand} is yet.",
        )
    if not can_encrypt():
        # The key the product issues must be stored encrypted; without a server
        # key there is nothing safe to store it with, so don't start.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This AskMira server cannot store connection keys safely yet: its "
                "SECRET_ENCRYPTION_KEY is not set. Ask your AskMira provider to set it."
            ),
        )
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Your workspace is not active."
        )
    api_url, app_url, _ = where
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
    # Kept only when it is not the standard address, so a connection otherwise
    # follows the product if its standard address changes.
    standard = addresses(product.key)
    custom = standard is None or (data["api"], data["app"]) != standard
    row.api_url = data["api"] if custom else None
    row.app_url = data["app"] if custom else None
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
    where = _where(product, row)
    if row.secret_encrypted and where is not None:
        target = Endpoint(api_url=where[0], app_url=where[1], secret=row.secret_encrypted)
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


# ── Where the product is: the admin can change it ────────────────────────────


def _resolves_public(hostname: str) -> bool:
    infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


async def _clean_address(value: str, what: str) -> str:
    """A web address AskMira's server may call. In production: https, on the public internet.

    An admin's address is called from AskMira's own server, so it must not be a
    way to reach machines inside AskMira's network.
    """
    text = (value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The {what} must be a web address, like https://app.example.com.",
        )
    if settings.is_production:
        if parsed.scheme != "https":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"The {what} must start with https://.",
            )
        try:
            public = await asyncio.to_thread(_resolves_public, parsed.hostname)
        except (OSError, ValueError):
            public = False
        if not public:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"The {what} must be a public address AskMira can find on the internet.",
            )
    return text


async def set_address(
    session: AsyncSession,
    user: User,
    product_key: str,
    *,
    api_url: str,
    app_url: str,
    request: Request | None,
) -> dict[str, Any]:
    """Point this workspace at the product's new address (a changed domain, say).

    The API address must answer as that product's server before it is saved. A
    connection keeps working: the product's key does not depend on its address.
    """
    _require_admin(user)
    product = _product(product_key)
    api = await _clean_address(api_url, "server address")
    app = await _clean_address(app_url, "web address")
    if not await client.answers_as(api, product.key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{api} does not answer as {product.brand}'s server. Check the server address.",
        )
    row = await _link(session, user, product.key)
    if row is None:
        row = ProductLink(tenant_id=user.tenant_id, product=product.key, status=links.DISABLED)
        session.add(row)
    before = {"api_url": row.api_url, "app_url": row.app_url}
    row.api_url, row.app_url = api, app
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="product_link",
        entity_id=row.id,
        actor=user,
        changes={"before": before, "after": {"api_url": api, "app_url": app}},
        summary=f"{product.brand}'s address changed in Connectors",
        request=request,
    )
    await session.commit()
    return _read(product, row)


async def reset_address(
    session: AsyncSession, user: User, product_key: str, *, request: Request | None
) -> dict[str, Any]:
    """Go back to the product's standard address."""
    _require_admin(user)
    product = _product(product_key)
    row = await _link(session, user, product.key)
    if row is None or not (row.api_url or row.app_url):
        return _read(product, row)
    if addresses(product.key) is None and row.secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"There is no standard address for {product.brand} to go back to.",
        )
    before = {"api_url": row.api_url, "app_url": row.app_url}
    row.api_url = row.app_url = None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="product_link",
        entity_id=row.id,
        actor=user,
        changes={"before": before, "after": {"api_url": None, "app_url": None}},
        summary=f"{product.brand} back on its standard address in Connectors",
        request=request,
    )
    await session.commit()
    return _read(product, row)
