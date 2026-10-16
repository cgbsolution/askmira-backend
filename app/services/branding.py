"""A company's logo: stored once, shown on its own sign-in page and in the portal.

PNG or JPEG only, read from the file's own bytes rather than its name. SVG is
refused: it is a document that can carry script, and this file is served on a
public URL with no sign-in in front of it.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.identity import directory
from app.services.storage import StorageError, get_storage
from app.services.storage.sniff import FileRejectedError, validate_upload

__all__ = ["LOGO_MAX_BYTES", "read_logo", "remove_logo", "save_logo"]

LOGO_MAX_BYTES = 1024 * 1024
_IMAGE_TYPES = {"image/png", "image/jpeg"}


async def save_logo(
    session: AsyncSession, actor: User, tenant: Tenant, data: bytes, *, request: Request | None
) -> dict[str, Any]:
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
    key = storage.new_key(tenant.id, "branding", kind.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The logo could not be stored just now. Nothing was changed; try again.",
        ) from exc
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
    if previous and previous != key:
        # An orphaned old logo is harmless; the record points at the new one.
        with contextlib.suppress(StorageError):
            await storage.delete(previous)
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
    try:
        data = await get_storage().get(tenant.logo_key)
    except StorageError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No logo.") from exc
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No logo.")
    media_type = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return data, media_type
