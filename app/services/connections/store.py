"""Saving, reading and removing a workspace's own connection to an outside service.

Every connection is one `TenantConnection` row: non-secret settings in the open,
credentials as JSON encrypted with the tenant key. `read` never carries a
secret — the page is told only that one is stored. Each save and removal is
audited in the same transaction, without the secret.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.integrations import TenantConnection
from app.models.user import User
from app.services import audit

__all__ = [
    "KIND_DIGIO",
    "KIND_ZOHO_BOOKS",
    "get",
    "get_for_tenant",
    "read",
    "remove",
    "save",
    "secrets_of",
]

KIND_DIGIO = "esign_digio"
KIND_ZOHO_BOOKS = "books_zoho"


async def get(session: AsyncSession, user: User, kind: str) -> TenantConnection | None:
    return await session.scalar(
        tenant_scoped(
            select(TenantConnection).where(TenantConnection.kind == kind), TenantConnection, user
        )
    )


async def get_for_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str
) -> TenantConnection | None:
    """For work with no signed-in user (a job, a letter being sent by the system)."""
    return await session.scalar(
        select(TenantConnection).where(
            tenant_filter(TenantConnection, tenant_id), TenantConnection.kind == kind
        )
    )


def secrets_of(row: TenantConnection) -> dict[str, Any]:
    """The stored credentials, decrypted at the point of use. Empty when unreadable."""
    if not row.secret_encrypted:
        return {}
    raw = decrypt_secret(row.secret_encrypted, row.tenant_id)
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def read(row: TenantConnection | None) -> dict[str, Any]:
    if row is None:
        return {"connected": False}
    return {
        "connected": bool(row.enabled and row.secret_encrypted),
        "enabled": row.enabled,
        "settings": dict(row.settings or {}),
        "account_label": row.account_label,
        "has_secret": bool(row.secret_encrypted),
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "last_error": row.last_error,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def save(
    session: AsyncSession,
    user: User,
    kind: str,
    *,
    settings: dict[str, Any],
    secrets: dict[str, Any] | None,
    account_label: str | None,
    label: str,
    request: Request | None = None,
) -> TenantConnection:
    """Create or replace the workspace's connection. `secrets=None` keeps what is stored."""
    row = await get(session, user, kind)
    created = row is None
    if row is None:
        row = TenantConnection(tenant_id=user.tenant_id, kind=kind)
        session.add(row)
    row.settings = settings
    if secrets is not None:
        row.secret_encrypted = encrypt_secret(json.dumps(secrets), user.tenant_id)
    row.account_label = account_label
    row.enabled = True
    row.last_checked_at = datetime.now(UTC)
    row.last_error = None
    row.configured_by_user_id = user.id
    await session.flush()
    # updated_at is set by the database on update; read it back now rather than
    # lazily (which an async session cannot do).
    await session.refresh(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE if created else AuditAction.UPDATE,
        entity_type="tenant_connection",
        entity_id=row.id,
        # Which settings changed, never the credentials.
        changes={"kind": kind, "settings": settings},
        summary=f"{label} {'connected' if created else 'connection updated'}",
        request=request,
    )
    return row


async def remove(
    session: AsyncSession, user: User, kind: str, *, label: str, request: Request | None = None
) -> bool:
    row = await get(session, user, kind)
    if row is None:
        return False
    row_id = row.id
    await session.delete(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.DELETE,
        entity_type="tenant_connection",
        entity_id=row_id,
        changes={"kind": kind},
        summary=f"{label} disconnected",
        request=request,
    )
    return True
