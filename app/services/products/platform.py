"""Platform → Tools: the super admin sets a workspace up in our other products.

Two halves.

**The platform's key per product** (`tools`, `set_key`, `remove_key`, `check`).
Set once per product: the same value lives on the product as
ASKMIRA_PLATFORM_SECRET. "Generate" makes a strong one here and shows it once,
for whoever runs the product to set there. The environment's
<PRODUCT>_TOOL_PLATFORM_SECRET is used when no key is set here.

**A workspace in a product** (`workspace_tools`, `provision`, `organizations`,
`disconnect`, `sync_now`). With the key, AskMiraa asks the product:

    POST {api}/integrations/askmira/platform/ping           is the key right?
    POST {api}/integrations/askmira/platform/organizations  find an organisation there
    POST {api}/integrations/askmira/platform/provision      create or attach one

`provision` creates the company's organisation and its admin (the workspace's
first admin, by email) in the product — or attaches the organisation the
product already has, when the super admin picks it — and the product answers
with a secret for that one workspace. From there the link is exactly what
Connectors makes (products/connectors.py): the secret is kept encrypted on the
workspace's ProductLink, the people sync runs, the product's pages show in the
portal, and the workspace's admin can Sync or Disconnect from Connectors. The
platform's key is never used for a workspace's data.

When the admin's email already belongs to an organisation in the product, the
product refuses to make a second one unasked (409 "existing", naming them);
the console offers "Link this one" or "Create a new one anyway".
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptionError, can_encrypt
from app.core.deps import tenant_filter
from app.models.audit import AuditAction
from app.models.platform_product_key import PlatformProductKey
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import audit
from app.services.auth import web_app_url
from app.services.products import client
from app.services.products import service as links
from app.services.products.catalogue import PRODUCTS, Product
from app.services.products.connections import require_super
from app.services.products.settings import (
    MIN_SECRET_LENGTH,
    Endpoint,
    addresses,
    platform_secret,
)

__all__ = [
    "check",
    "disconnect",
    "organizations",
    "provision",
    "remove_key",
    "set_key",
    "sync_now",
    "tools",
    "workspace_tools",
]

VIA = "platform"
#: What the console shows for each product, beside its brand.
WHAT = {
    "interview": "Hiring and interviews",
    "expense": "Expense claims",
    "helpdesk": "IT tickets and assets",
    "saleinx": "Sales CRM",
    "procurement": "Procure-to-pay",
}


def _product(key: str) -> Product:
    product = PRODUCTS.get(key)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such product.")
    return product


async def _key_row(session: AsyncSession, product: str) -> PlatformProductKey | None:
    try:
        return await session.get(PlatformProductKey, product)
    except EncryptionError:
        return None


async def _secret(session: AsyncSession, product: str) -> tuple[str | None, str | None]:
    """(the platform's key for the product, where it came from: "platform" or "env")."""
    row = await _key_row(session, product)
    if row is not None and row.secret_encrypted:
        return row.secret_encrypted, "platform"
    env = platform_secret(product)
    return (env, "env") if env else (None, None)


def _refused(brand: str, exc: client.ProductError) -> HTTPException:
    if exc.status is None:
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{brand} could not be reached. Try again in a minute.",
        )
    if exc.status in (401, 403):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{brand} did not accept the platform key. Set the same key on {brand} as "
                "ASKMIRA_PLATFORM_SECRET (Platform → Tools), then try again."
            ),
        )
    if exc.status == 404:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{brand} does not offer platform set-up yet: deploy its latest version, or "
                "connect from the workspace's Settings → Connectors instead."
            ),
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=exc.detail or f"{brand} refused ({exc.status}).",
    )


# ── The platform's key per product ───────────────────────────────────────────


async def _tool_read(session: AsyncSession, product: Product) -> dict[str, Any]:
    row = await _key_row(session, product.key)
    secret, source = await _secret(session, product.key)
    where = addresses(product.key)
    return {
        "key": product.key,
        "brand": product.brand,
        "name": product.name,
        "what": WHAT.get(product.key, product.name),
        "api_url": where[0] if where else None,
        "app_url": where[1] if where else None,
        "key_set": secret is not None,
        "key_source": source,
        "last_checked_at": (
            row.last_checked_at.isoformat() if row and row.last_checked_at else None
        ),
        "last_check_ok": row.last_check_ok if row else None,
        "last_check_message": row.last_check_message if row else None,
        "ready": secret is not None and where is not None and can_encrypt(),
    }


async def tools(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    require_super(user)
    return [await _tool_read(session, p) for p in PRODUCTS.values()]


async def set_key(
    session: AsyncSession,
    user: User,
    product_key: str,
    *,
    secret: str | None,
    request: Request | None,
) -> dict[str, Any]:
    """Keep the platform's key for the product; with no secret given, make a strong one.

    A made key is returned once (`generated`), to be set on the product as
    ASKMIRA_PLATFORM_SECRET; it is never shown again.
    """
    require_super(user)
    product = _product(product_key)
    if not can_encrypt():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Set SECRET_ENCRYPTION_KEY on this server first: keys are kept encrypted.",
        )
    generated = None
    if secret is None:
        generated = secret = secrets.token_urlsafe(48)
    secret = secret.strip()
    if len(secret) < MIN_SECRET_LENGTH or len(secret) > 512 or any(c.isspace() for c in secret):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A key is at least {MIN_SECRET_LENGTH} characters, with no spaces.",
        )
    row = await _key_row(session, product.key)
    replaced = row is not None
    if row is None:
        row = PlatformProductKey(product=product.key, secret_encrypted=secret)
        session.add(row)
    row.secret_encrypted = secret
    row.set_by_user_id = user.id
    row.last_checked_at = None
    row.last_check_ok = None
    row.last_check_message = None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="platform_product_key",
        actor=user,
        actor_type="platform",
        changes={"product": product.key, "replaced": replaced, "generated": generated is not None},
        summary=f"Platform key for {product.brand} {'replaced' if replaced else 'set'}",
        request=request,
    )
    await session.commit()
    out = await _tool_read(session, product)
    out["generated"] = generated
    return out


async def remove_key(
    session: AsyncSession, user: User, product_key: str, *, request: Request | None
) -> dict[str, Any]:
    require_super(user)
    product = _product(product_key)
    row = await _key_row(session, product.key)
    if row is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No key is set here.")
    await session.delete(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="platform_product_key",
        actor=user,
        actor_type="platform",
        changes={"product": product.key},
        summary=(
            f"Platform key for {product.brand} removed; workspaces already connected keep their own"
        ),
        request=request,
    )
    await session.commit()
    return await _tool_read(session, product)


async def check(session: AsyncSession, user: User, product_key: str) -> dict[str, Any]:
    """Ask the product whether it takes this key. Changes nothing there."""
    require_super(user)
    product = _product(product_key)
    secret, _ = await _secret(session, product.key)
    where = addresses(product.key)
    if secret is None or where is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Set a key for {product.brand} first.",
        )
    ok, message = True, f"{product.brand} accepted the key."
    try:
        answer = await client.platform(where[0], product.key, secret, "ping", {"check": True})
        if answer.get("product") not in (None, product.key):
            ok, message = False, f"That address is not {product.brand}."
        elif answer.get("ready") is False:
            ok = False
            message = str(answer.get("message") or f"{product.brand} is not ready.")[:500]
    except client.ProductError as exc:
        ok = False
        message = _refused(product.brand, exc).detail
    row = await _key_row(session, product.key)
    if row is not None:
        row.last_checked_at = datetime.now(UTC)
        row.last_check_ok = ok
        row.last_check_message = message[:500]
        await session.commit()
    out = await _tool_read(session, product)
    out["last_check_ok"] = ok
    out["last_check_message"] = message
    return out


# ── A workspace in a product ─────────────────────────────────────────────────


async def _workspace(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or getattr(tenant, "deleted_at", None) is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such workspace.")
    return tenant


async def _link(session: AsyncSession, tenant_id: uuid.UUID, product: str) -> ProductLink | None:
    return await session.scalar(
        select(ProductLink).where(
            tenant_filter(ProductLink, tenant_id), ProductLink.product == product
        )
    )


async def _admin(session: AsyncSession, tenant_id: uuid.UUID) -> User | None:
    """The workspace's first admin: who becomes the organisation's admin in the product."""
    return await session.scalar(
        select(User)
        .where(
            tenant_filter(User, tenant_id),
            User.role == UserRole.ADMIN,
            User.is_active.is_(True),
        )
        .order_by(User.created_at)
        .limit(1)
    )


def _link_read(product: Product, row: ProductLink | None, *, ready: bool) -> dict[str, Any]:
    connected = row is not None and row.status == links.ACTIVE
    via = None
    if connected:
        via = row.via or ("connectors" if row.secret_encrypted else "server")
    return {
        "key": product.key,
        "brand": product.brand,
        "name": product.name,
        "what": WHAT.get(product.key, product.name),
        # Whether the platform can set this product up (its key and address are known).
        "ready": ready,
        "connected": connected,
        "via": via,
        "organization": (row.organization_name or row.external_ref) if connected else None,
        "external_ref": row.external_ref if connected else None,
        "last_synced_at": (
            row.last_synced_at.isoformat() if connected and row.last_synced_at else None
        ),
        "last_sync": row.last_sync if connected else None,
        "last_error": row.last_error if connected else None,
    }


async def workspace_tools(
    session: AsyncSession, user: User, tenant_id: uuid.UUID
) -> list[dict[str, Any]]:
    require_super(user)
    await _workspace(session, tenant_id)
    out = []
    for product in PRODUCTS.values():
        secret, _ = await _secret(session, product.key)
        ready = secret is not None and addresses(product.key) is not None and can_encrypt()
        out.append(_link_read(product, await _link(session, tenant_id, product.key), ready=ready))
    return out


async def connections(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Every live workspace with its standing in each tool: the console's overview grid."""
    require_super(user)
    tenants = (
        await session.scalars(
            select(Tenant).where(Tenant.deleted_at.is_(None)).order_by(Tenant.name)
        )
    ).all()
    ready: dict[str, bool] = {}
    for product in PRODUCTS.values():
        secret, _ = await _secret(session, product.key)
        ready[product.key] = (
            secret is not None and addresses(product.key) is not None and can_encrypt()
        )
    out = []
    for tenant in tenants:
        out.append(
            {
                "id": str(tenant.id),
                "name": tenant.name,
                "workspace": tenant.slug,
                "is_active": tenant.is_active,
                "tools": [
                    _link_read(
                        product,
                        await _link(session, tenant.id, product.key),
                        ready=ready[product.key],
                    )
                    for product in PRODUCTS.values()
                ],
            }
        )
    return out


async def _target(session: AsyncSession, product: Product) -> tuple[str, str, str]:
    """(api_url, app_url, platform key), or a 409 saying what is missing."""
    secret, _ = await _secret(session, product.key)
    where = addresses(product.key)
    if where is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This server does not know where {product.brand} is.",
        )
    if secret is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Set the platform key for {product.brand} under Platform → Tools first.",
        )
    if not can_encrypt():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Set SECRET_ENCRYPTION_KEY on this server first: keys are kept encrypted.",
        )
    return where[0], where[1], secret


def _orgs(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The organisations a product named, cleaned for the console."""
    out = []
    for item in (payload or {}).get("organizations") or []:
        if not isinstance(item, dict) or not item.get("ref"):
            continue
        admins = item.get("admins") if isinstance(item.get("admins"), list) else []
        out.append(
            {
                "ref": str(item["ref"])[:120],
                "name": str(item.get("name") or item["ref"])[:200],
                "admins": [str(a)[:254] for a in admins[:5]],
                "connected_to": (
                    str(item["connected_to"])[:64] if item.get("connected_to") else None
                ),
            }
        )
    return out[:25]


async def organizations(
    session: AsyncSession, user: User, tenant_id: uuid.UUID, product_key: str, *, q: str
) -> list[dict[str, Any]]:
    """Organisations in the product matching `q` (name, address or an admin's email)."""
    require_super(user)
    product = _product(product_key)
    await _workspace(session, tenant_id)
    api_url, _, secret = await _target(session, product)
    query = q.strip()[:120]
    if len(query) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Type at least two letters of the organisation's name or an admin's email.",
        )
    try:
        answer = await client.platform(api_url, product.key, secret, "organizations", {"q": query})
    except client.ProductError as exc:
        raise _refused(product.brand, exc) from exc
    return _orgs(answer)


async def provision(
    session: AsyncSession,
    user: User,
    tenant_id: uuid.UUID,
    product_key: str,
    *,
    organization_ref: str | None = None,
    create_new: bool = False,
    request: Request | None,
) -> dict[str, Any]:
    """Create (or attach) the workspace's organisation in the product, connect, sync people.

    Returns {"outcome": "connected", "tool": …, "created", "admin_created"} — or
    {"outcome": "existing", "organizations": […]} when the admin already has an
    organisation there and the super admin has not said which way to go.
    """
    require_super(user)
    product = _product(product_key)
    tenant = await _workspace(session, tenant_id)
    if not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Reactivate the workspace first."
        )
    row = await _link(session, tenant.id, product.key)
    if row is not None and row.status == links.ACTIVE and row.secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{tenant.name} is already connected to {product.brand}.",
        )
    api_url, app_url, secret = await _target(session, product)
    admin = await _admin(session, tenant.id)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{tenant.name} has no active admin to make the {product.brand} admin.",
        )
    company = await links._company(session, tenant)
    body = {
        "tenant_id": str(tenant.id),
        "tenant_name": tenant.name,
        "askmira_origin": web_app_url(),
        "company": {
            "name": tenant.name,
            "slug": tenant.slug,
            "domain": company.get("domain"),
        },
        "admin": {"email": admin.email.lower(), "name": admin.full_name},
        "organization_ref": (organization_ref or "").strip()[:120] or None,
        "create_new": bool(create_new),
    }
    try:
        answer = await client.platform(api_url, product.key, secret, "provision", body)
    except client.ProductError as exc:
        payload = exc.payload or {}
        if exc.status == 409 and payload.get("code") == "existing":
            return {
                "outcome": "existing",
                "message": exc.detail
                or f"{admin.email} already has an organisation in {product.brand}.",
                "organizations": _orgs(payload),
            }
        raise _refused(product.brand, exc) from exc
    if answer.get("product") not in (None, product.key) or not isinstance(
        answer.get("secret"), str
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{product.brand}'s answer was not a connection.",
        )
    if len(answer["secret"]) < MIN_SECRET_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{product.brand} issued a key too short to use.",
        )

    before = (
        {"status": row.status, "external_ref": row.external_ref, "via": row.via} if row else None
    )
    if row is None:
        row = ProductLink(tenant_id=tenant.id, product=product.key)
        session.add(row)
    row.status = links.ACTIVE
    row.external_ref = str(answer.get("external_ref") or "")[:120] or None
    row.organization_name = str(answer.get("organization") or "")[:200] or None
    # The standard address: the link follows the product if it moves.
    row.api_url = None
    row.app_url = None
    row.secret_encrypted = answer["secret"]
    row.linked_by_user_id = user.id
    row.via = VIA
    row.last_error = None
    links.mirror(tenant, product.key, True)
    await session.flush()
    created = bool(answer.get("created"))
    admin_created = bool(answer.get("admin_created"))
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="product_link",
        entity_id=row.id,
        actor_user_id=user.id,
        actor_type="platform",
        changes={
            "before": before,
            "after": {
                "status": links.ACTIVE,
                "external_ref": row.external_ref,
                "organization": row.organization_name,
                "via": VIA,
                "created": created,
                "admin": admin.email.lower(),
                "admin_created": admin_created,
            },
        },
        summary=(
            f"{product.brand} {'organisation created' if created else 'organisation attached'} "
            f"by the platform: “{row.organization_name or row.external_ref}”, "
            f"admin {admin.email.lower()}"
        ),
        request=request,
    )
    await session.commit()

    await links.sync(session, tenant.id, product.key, actor=user, request=request)
    secret_ok, _ = await _secret(session, product.key)
    return {
        "outcome": "connected",
        "created": created,
        "admin_created": admin_created,
        "admin_email": admin.email.lower(),
        "admin_note": _admin_note(
            product, admin.email.lower(), created=created, made=admin_created
        ),
        "app_url": app_url,
        "tool": _link_read(
            product, await _link(session, tenant.id, product.key), ready=secret_ok is not None
        ),
    }


def _admin_note(product: Product, email: str, *, created: bool, made: bool) -> str:
    """What became of the workspace's admin in the product, in a sentence."""
    if product.key == "helpdesk":
        # Its roles reach every company it serves: AskMiraa never gives one (catalogue.py).
        return "Its people are added there as users; the helpdesk's IT team gives any other role."
    if not created:
        return f"Its people are matched there by email; {email} keeps the role they have there."
    if made:
        return f"{email} is its admin."
    if product.roles_from_product:
        return f"{email} is its owner, with the login they already had there."
    return (
        f"{email} already has a login in another {product.brand} organisation, so it was not "
        "added to this one."
    )


async def sync_now(
    session: AsyncSession,
    user: User,
    tenant_id: uuid.UUID,
    product_key: str,
    *,
    request: Request | None,
) -> dict[str, Any]:
    require_super(user)
    product = _product(product_key)
    await _workspace(session, tenant_id)
    row = await _link(session, tenant_id, product.key)
    if row is None or row.status != links.ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not connected.")
    await links.sync(session, tenant_id, product.key, actor=user, request=request)
    secret, _ = await _secret(session, product.key)
    row = await _link(session, tenant_id, product.key)
    return _link_read(product, row, ready=secret is not None)


async def disconnect(
    session: AsyncSession,
    user: User,
    tenant_id: uuid.UUID,
    product_key: str,
    *,
    request: Request | None,
) -> dict[str, Any]:
    """The product forgets the workspace's secret; its organisation and data stay there."""
    require_super(user)
    product = _product(product_key)
    tenant = await _workspace(session, tenant_id)
    row = await _link(session, tenant.id, product.key)
    if row is None or row.status != links.ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not connected.")
    told = True
    where = addresses(product.key)
    api_url = row.api_url or (where[0] if where else None)
    app_url = row.app_url or (where[1] if where else None)
    if row.secret_encrypted and api_url and app_url:
        target = Endpoint(api_url=api_url, app_url=app_url, secret=row.secret_encrypted)
        try:
            await client.revoke(target, product.key, tenant.id)
        except client.ProductError:
            told = False
    row.status = links.DISABLED
    row.secret_encrypted = None
    links.mirror(tenant, product.key, False)
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="product_link",
        entity_id=row.id,
        actor_user_id=user.id,
        actor_type="platform",
        changes={"before": {"status": links.ACTIVE}, "after": {"status": links.DISABLED}},
        summary=(
            f"{product.brand} disconnected by the platform"
            + ("" if told else f"; {product.brand} could not be reached to be told")
        ),
        request=request,
    )
    await session.commit()
    secret, _ = await _secret(session, product.key)
    return _link_read(product, row, ready=secret is not None)
