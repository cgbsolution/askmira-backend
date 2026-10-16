"""Link a workspace to a product, keep its people in step, and open it for someone.

link / unlink           platform console (super admin)
sync                    the console's "Sync now", and every night (scheduler)
launch                  anyone in a linked workspace the product admits
list_for_workspace      the console's view of one workspace's products
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import audit
from app.services.products.catalogue import PRODUCTS, Product
from app.services.products.client import ProductError, provision_organisation, sync_people
from app.services.products.settings import endpoint
from app.services.products.tokens import launch_token

ACTIVE = "active"
DISABLED = "disabled"

# Never offered to a product as "the company's domain": anyone can have an address there.
_PUBLIC_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "rediffmail.com", "proton.me",
    "protonmail.com", "zoho.com", "zohomail.in", "yandex.com", "gmx.com", "mail.com",
}  # fmt: skip


def _product(key: str) -> Product:
    product = PRODUCTS.get(key)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such product.")
    return product


def _require_super(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN or getattr(user, "operator_grant_id", None):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the platform's super admin links products.",
        )


async def _workspace(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such workspace.")
    return tenant


async def _link_row(
    session: AsyncSession, tenant_id: uuid.UUID, product: str
) -> ProductLink | None:
    return await session.scalar(
        select(ProductLink).where(
            tenant_filter(ProductLink, tenant_id), ProductLink.product == product
        )
    )


def _read(product: Product, row: ProductLink | None) -> dict[str, Any]:
    return {
        "key": product.key,
        "name": product.name,
        "replaces": product.replaces,
        "configured": endpoint(product.key) is not None,
        "status": row.status if row else None,
        "external_ref": row.external_ref if row else None,
        "last_synced_at": row.last_synced_at.isoformat() if row and row.last_synced_at else None,
        "last_sync": row.last_sync if row else None,
        "last_error": row.last_error if row else None,
    }


def _mirror(tenant: Tenant, product: str, on: bool) -> None:
    """Keep Tenant.linked_products — what the portal's nav reads — in step."""
    settings = dict(tenant.settings or {})
    linked = dict(settings.get("linked_products") or {})
    if on:
        linked[product] = True
    else:
        linked.pop(product, None)
    settings["linked_products"] = linked
    tenant.settings = settings


def _domain_of(value: str | None) -> str | None:
    if not value:
        return None
    if "@" in value:
        host = value.split("@", 1)[1]
    else:
        host = urlparse(value if "//" in value else f"//{value}").hostname or ""
    host = host.strip().lower().removeprefix("www.")
    return host if host and "." in host and host not in _PUBLIC_MAIL else None


async def _company(session: AsyncSession, tenant: Tenant) -> dict[str, Any]:
    """What a product needs to create (or recognise) the company."""
    profile = (tenant.settings or {}).get("profile") or {}
    first_admin = await session.scalar(
        select(User.email)
        .where(tenant_filter(User, tenant.id), User.role == UserRole.ADMIN)
        .order_by(User.created_at)
        .limit(1)
    )
    domain = (
        _domain_of(profile.get("contact_email"))
        or _domain_of(first_admin)
        or _domain_of(profile.get("website"))
    )
    return {"tenant_id": str(tenant.id), "name": tenant.name, "slug": tenant.slug, "domain": domain}


async def _roster(
    session: AsyncSession, tenant_id: uuid.UUID, product: Product
) -> list[dict[str, Any]]:
    """Everyone in the workspace the product should admit, with what it needs about them."""
    rows = (
        await session.execute(
            select(User, Employee)
            .outerjoin(Employee, Employee.id == User.employee_id)
            .where(tenant_filter(User, tenant_id), User.is_active.is_(True))
        )
    ).all()
    employees = (
        await session.execute(
            select(Employee.id, Employee.work_email, Employee.manager_id).where(
                tenant_filter(Employee, tenant_id),
                Employee.deleted_at.is_(None),
                Employee.status != EmploymentStatus.EXITED,
            )
        )
    ).all()
    managers = {manager_id for _, _, manager_id in employees if manager_id}
    emails = {emp_id: email for emp_id, email, _ in employees}
    login_email = {emp.id: user.email for user, emp in rows if emp is not None}
    departments = dict(
        (
            await session.execute(
                select(Department.id, Department.name).where(tenant_filter(Department, tenant_id))
            )
        ).all()
    )

    people: list[dict[str, Any]] = []
    for user, emp in rows:
        if emp is not None and (
            emp.deleted_at is not None or emp.status is EmploymentStatus.EXITED
        ):
            continue
        role = product.role_for(user.role, is_manager=bool(emp and emp.id in managers))
        if role is None:
            continue
        manager_email = None
        if emp is not None and emp.manager_id:
            manager_email = login_email.get(emp.manager_id) or emails.get(emp.manager_id)
        people.append(
            {
                "id": str(user.id),
                "email": user.email.lower(),
                "name": user.full_name,
                "role": role,
                "manager_email": manager_email.lower() if manager_email else None,
                "department": departments.get(emp.department_id) if emp else None,
                "employee_code": emp.employee_code if emp else None,
                "designation": emp.designation if emp else None,
                "grade": emp.grade if emp else None,
            }
        )
    return people


# ── Platform console ─────────────────────────────────────────────────────────


async def list_for_workspace(
    session: AsyncSession, user: User, tenant_id: uuid.UUID
) -> list[dict[str, Any]]:
    _require_super(user)
    await _workspace(session, tenant_id)
    return [_read(p, await _link_row(session, tenant_id, p.key)) for p in PRODUCTS.values()]


async def link(
    session: AsyncSession,
    user: User,
    tenant_id: uuid.UUID,
    product_key: str,
    *,
    request: Request | None,
) -> dict[str, Any]:
    """Create or adopt the company in the product, switch the module over, push its people."""
    _require_super(user)
    product = _product(product_key)
    target = endpoint(product.key)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{product.name} is not configured on this server.",
        )
    tenant = await _workspace(session, tenant_id)
    if not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Reactivate the workspace first."
        )

    company = await _company(session, tenant)
    try:
        external_ref = await provision_organisation(target, product.key, tenant.id, company)
    except ProductError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    row = await _link_row(session, tenant.id, product.key)
    before = {"status": row.status, "external_ref": row.external_ref} if row else None
    if row is None:
        row = ProductLink(tenant_id=tenant.id, product=product.key)
        session.add(row)
    row.status = ACTIVE
    row.external_ref = external_ref
    row.linked_by_user_id = user.id
    row.last_error = None
    _mirror(tenant, product.key, True)
    await session.flush()
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="product_link",
        entity_id=row.id,
        actor_user_id=user.id,
        actor_type="platform",
        changes={"before": before, "after": {"status": ACTIVE, "external_ref": external_ref}},
        summary=f"{product.name} linked by the platform; it replaces the built-in module",
        request=request,
    )
    await session.commit()

    await sync(session, tenant.id, product.key, actor=user, request=request)
    return _read(product, await _link_row(session, tenant.id, product.key))


async def unlink(
    session: AsyncSession,
    user: User,
    tenant_id: uuid.UUID,
    product_key: str,
    *,
    request: Request | None,
) -> dict[str, Any]:
    """Bring the built-in module back. The product keeps its data; nobody launches it from here."""
    _require_super(user)
    product = _product(product_key)
    tenant = await _workspace(session, tenant_id)
    row = await _link_row(session, tenant.id, product.key)
    if row is None or row.status != ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not linked.")
    row.status = DISABLED
    _mirror(tenant, product.key, False)
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="product_link",
        entity_id=row.id,
        actor_user_id=user.id,
        actor_type="platform",
        changes={"before": {"status": ACTIVE}, "after": {"status": DISABLED}},
        summary=f"{product.name} unlinked by the platform; the built-in module is back",
        request=request,
    )
    await session.commit()
    return _read(product, row)


async def sync_now(
    session: AsyncSession,
    user: User,
    tenant_id: uuid.UUID,
    product_key: str,
    *,
    request: Request | None,
) -> dict[str, Any]:
    _require_super(user)
    product = _product(product_key)
    await _workspace(session, tenant_id)
    row = await _link_row(session, tenant_id, product.key)
    if row is None or row.status != ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not linked.")
    await sync(session, tenant_id, product.key, actor=user, request=request)
    return _read(product, await _link_row(session, tenant_id, product.key))


# ── People sync ──────────────────────────────────────────────────────────────


async def sync(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_key: str,
    *,
    actor: User,
    request: Request | None = None,
) -> dict[str, Any]:
    """Push the workspace's roster to the product and record how it went.

    A failure is recorded on the link (and shown in the console), not raised:
    the link stays, and the next sync tries again.
    """
    product = _product(product_key)
    row = await _link_row(session, tenant_id, product.key)
    target = endpoint(product.key)
    if row is None or row.status != ACTIVE:
        return {"skipped": "not linked"}
    if target is None:
        outcome: dict[str, Any] = {"error": f"{product.name} is not configured on this server."}
    else:
        people = await _roster(session, tenant_id, product)
        try:
            result = await sync_people(target, product.key, tenant_id, people)
            outcome = {
                "sent": len(people),
                "created": int(result.get("created", 0)),
                "updated": int(result.get("updated", 0)),
                "deactivated": int(result.get("deactivated", 0)),
                "skipped": [
                    {"email": str(s.get("email", "")), "reason": str(s.get("reason", ""))[:200]}
                    for s in (result.get("skipped") or [])[:50]
                    if isinstance(s, dict)
                ],
            }
        except ProductError as exc:
            outcome = {"error": str(exc)[:500]}

    row = await _link_row(session, tenant_id, product.key)
    if row is None:  # unlinked while the product was answering
        return outcome
    if "error" in outcome:
        row.last_error = outcome["error"]
    else:
        row.last_error = None
        row.last_sync = outcome
        row.last_synced_at = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.UPDATE,
        entity_type="product_link",
        entity_id=row.id,
        actor_user_id=actor.id if actor.id else None,
        actor_type="platform" if actor.role is UserRole.SUPER_ADMIN else "system",
        changes={"sync": {k: v for k, v in outcome.items() if k != "skipped"}},
        summary=(
            f"{product.name} people sync failed"
            if "error" in outcome
            else f"{product.name} people sync: {outcome['sent']} sent"
        ),
        request=request,
    )
    await session.commit()
    return outcome


async def sync_workspace(session: AsyncSession, actor: User) -> dict[str, Any]:
    """Every active link in the actor's workspace. The nightly job."""
    rows = (
        await session.scalars(
            tenant_scoped(select(ProductLink), ProductLink, actor).where(
                ProductLink.status == ACTIVE
            )
        )
    ).all()
    keys = [row.product for row in rows]
    results = {}
    for key in keys:
        results[key] = await sync(session, actor.tenant_id, key, actor=actor)
    return {"products": len(keys), "results": results}


# ── Launch ───────────────────────────────────────────────────────────────────


async def launch(session: AsyncSession, user: User, product_key: str) -> dict[str, str]:
    """A one-time, 60-second link that signs this person in to the product."""
    product = _product(product_key)
    row = await session.scalar(
        tenant_scoped(select(ProductLink), ProductLink, user).where(
            ProductLink.product == product.key
        )
    )
    if row is None or row.status != ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{product.name} is not set up for your workspace.",
        )
    role = product.role_for(user.role, is_manager=bool(getattr(user, "has_reports", False)))
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{product.name} is for HR and hiring managers.",
        )
    target = endpoint(product.key)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{product.name} is not configured on this server.",
        )
    token = launch_token(
        target.secret,
        product.key,
        tenant_id=user.tenant_id,
        external_ref=row.external_ref,
        user_id=user.id,
        email=user.email,
        name=user.full_name,
        role=role,
    )
    # The fragment never reaches a server log or a Referer header.
    return {"url": f"{target.app_url}/sso/askmira#token={token}"}
