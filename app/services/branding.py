"""Logos: the workspace's and each legal entity's.

The workspace logo is on the company's own sign-in page. Each legal entity (a
sister company, a client the group staffs) may have its own: after sign-in a
person sees their own company's logo at the top of the portal and on the app's
Home, and the workspace's when their company has none.

PNG or JPEG only, read from the file's own bytes rather than its name. SVG is
refused: it is a document that can carry script, and these files are served on
a public URL with no sign-in in front of them.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import CompanyBrand
from app.services import audit
from app.services.identity import directory
from app.services.storage import StorageError, get_storage
from app.services.storage.sniff import FileRejectedError, validate_upload

__all__ = [
    "LOGO_MAX_BYTES",
    "company_for",
    "entity_logo_url",
    "read_entity_logo",
    "read_logo",
    "remove_entity_logo",
    "remove_logo",
    "save_entity_logo",
    "save_logo",
]

LOGO_MAX_BYTES = 1024 * 1024
_IMAGE_TYPES = {"image/png", "image/jpeg"}


async def _store_image(tenant_id: uuid.UUID, data: bytes) -> str:
    """Check the bytes are a PNG or JPEG of at most 1 MB, store them, return the key."""
    try:
        kind = validate_upload(data, max_bytes=LOGO_MAX_BYTES)
    except FileRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if kind.mime_type not in _IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A logo has to be a PNG or JPEG image.",
        )
    storage = get_storage()
    key = storage.new_key(tenant_id, "branding", kind.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        # The storage service's own reason ("Bucket not found", "not
        # configured"): a misconfiguration is not fixed by trying again.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The logo could not be stored. Nothing was changed. {exc}",
        ) from exc
    return key


async def _drop_old(previous: str | None, current: str | None) -> None:
    # An orphaned old logo is harmless; the record points at the new one.
    if previous and previous != current:
        with contextlib.suppress(StorageError):
            await get_storage().delete(previous)


async def _read_image(key: str | None) -> tuple[bytes, str]:
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No logo.")
    try:
        data = await get_storage().get(key)
    except StorageError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No logo.") from exc
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No logo.")
    media_type = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return data, media_type


# ── The workspace's logo ────────────────────────────────────────────────────


async def save_logo(
    session: AsyncSession, actor: User, tenant: Tenant, data: bytes, *, request: Request | None
) -> dict[str, Any]:
    key = await _store_image(tenant.id, data)
    previous = tenant.logo_key
    tenant.logo_key = key
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor=actor if actor.tenant_id == tenant.id else None,
        actor_user_id=None if actor.tenant_id == tenant.id else actor.id,
        actor_type="user" if actor.tenant_id == tenant.id else "platform",
        summary="Company logo changed",
        request=request,
    )
    await session.commit()
    await _drop_old(previous, key)
    return {"logo_url": directory.logo_url(tenant)}


async def remove_logo(
    session: AsyncSession, actor: User, tenant: Tenant, *, request: Request | None
) -> None:
    if not tenant.logo_key:
        return
    tenant.logo_key = None
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor=actor,
        summary="Company logo removed",
        request=request,
    )
    await session.commit()


async def read_logo(session: AsyncSession, slug: str) -> tuple[bytes, str]:
    """The logo bytes for a company's public sign-in page."""
    tenant = await directory.workspace_by_slug(session, slug)
    if tenant is None or not tenant.is_active or not tenant.logo_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No logo.")
    return await _read_image(tenant.logo_key)


# ── A legal entity's logo ───────────────────────────────────────────────────


def entity_logo_url(entity: LegalEntity) -> str | None:
    """Where the portal and the app fetch an entity's logo; None without one."""
    if not entity.logo_key:
        return None
    # The key's hash busts a cached logo when a new one is uploaded.
    version = hashlib.sha256(entity.logo_key.encode()).hexdigest()[:10]
    return f"{settings.API_V1_PREFIX}/public/workspaces/entities/{entity.id}/logo?v={version}"


async def _entity(session: AsyncSession, user: User, entity_id: uuid.UUID) -> LegalEntity:
    entity = await session.scalar(
        tenant_scoped(select(LegalEntity).where(LegalEntity.id == entity_id), LegalEntity, user)
    )
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Legal entity not found")
    return entity


async def save_entity_logo(
    session: AsyncSession,
    actor: User,
    entity_id: uuid.UUID,
    data: bytes,
    *,
    request: Request | None,
) -> dict[str, Any]:
    entity = await _entity(session, actor, entity_id)
    key = await _store_image(actor.tenant_id, data)
    previous = entity.logo_key
    entity.logo_key = key
    await audit.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="legal_entity",
        entity_id=entity.id,
        actor=actor,
        changes={"logo": {"from": "set" if previous else "none", "to": "set"}},
        summary=f"Logo of legal entity {entity.code} changed",
        request=request,
    )
    await session.commit()
    await _drop_old(previous, key)
    return {"logo_url": entity_logo_url(entity)}


async def remove_entity_logo(
    session: AsyncSession, actor: User, entity_id: uuid.UUID, *, request: Request | None
) -> None:
    entity = await _entity(session, actor, entity_id)
    if not entity.logo_key:
        return
    previous = entity.logo_key
    entity.logo_key = None
    await audit.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="legal_entity",
        entity_id=entity.id,
        actor=actor,
        changes={"logo": {"from": "set", "to": "none"}},
        summary=f"Logo of legal entity {entity.code} removed",
        request=request,
    )
    await session.commit()
    await _drop_old(previous, None)


async def read_entity_logo(session: AsyncSession, entity_id: uuid.UUID) -> tuple[bytes, str]:
    """An entity's logo bytes. Public, like the workspace logo: no sign-in in front.

    Looked up by its own id, across workspaces on purpose — there is nobody
    signed in to scope by — and refused for a suspended or deleted workspace.
    """
    row = (
        await session.execute(
            select(LegalEntity.logo_key, Tenant.is_active)
            .join(Tenant, Tenant.id == LegalEntity.tenant_id)
            .where(LegalEntity.id == entity_id)
        )
    ).first()
    if row is None or not row.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No logo.")
    return await _read_image(row.logo_key)


# ── The company a signed-in person belongs to ───────────────────────────────


async def company_for(session: AsyncSession, user: User, tenant: Tenant) -> CompanyBrand:
    """The company a signed-in person belongs to, as the portal and app draw it.

    Their own legal entity's name and logo; the workspace's logo when their
    company has none of its own; the workspace's name and logo for somebody
    with no employee record or no company (a platform admin, say).
    """
    entity: LegalEntity | None = None
    if user.employee_id is not None:
        entity = await session.scalar(
            tenant_scoped(
                select(LegalEntity)
                .join(Employee, Employee.legal_entity_id == LegalEntity.id)
                .where(Employee.id == user.employee_id),
                LegalEntity,
                user,
            )
        )
    workspace_logo = directory.logo_url(tenant)
    if entity is None:
        return CompanyBrand(name=tenant.name, logo_url=workspace_logo, legal_entity_id=None)
    return CompanyBrand(
        name=entity.name,
        logo_url=entity_logo_url(entity) or workspace_logo,
        legal_entity_id=entity.id,
    )
