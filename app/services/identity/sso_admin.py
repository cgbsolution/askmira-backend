"""Identity-provider and SCIM-token administration.

Secrets are write-only: a client secret is encrypted with the tenant's derived
key on the way in, and a SCIM token is shown once at creation and stored only
as its hash. Nothing here returns either.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.identity import IdentityProvider, ScimToken
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.identity import scim as scim_service

__all__ = ["create_token", "list_tokens", "revoke_token", "update_provider"]

#: Provider fields an admin may change after creation. There is no role or
#: group attribute here, deliberately: roles are assigned in AskMira.
UPDATABLE = (
    "name",
    "enabled",
    "entity_id",
    "sso_url",
    "slo_url",
    "x509_cert",
    "issuer",
    "discovery_url",
    "client_id",
    "email_attribute",
    "first_name_attribute",
    "last_name_attribute",
    "employee_id_attribute",
    "enforce_sso",
    "allowed_email_domains",
    "jit_provisioning",
)


async def update_provider(
    session: AsyncSession,
    user: User,
    *,
    provider_id: uuid.UUID,
    changes: dict[str, Any],
    client_secret: str | None,
    request: Request | None = None,
) -> IdentityProvider:
    provider = await session.scalar(
        tenant_scoped(
            select(IdentityProvider).where(IdentityProvider.id == provider_id),
            IdentityProvider,
            user,
        )
    )
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found")

    will_enforce = changes.get("enforce_sso", provider.enforce_sso) and changes.get(
        "enabled", provider.enabled
    )
    if will_enforce:
        tenant = await session.get(Tenant, user.tenant_id)
        if not ((tenant.settings or {}) if tenant else {}).get("break_glass_email"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Set a break-glass admin account before enforcing SSO. Without one, a "
                    "misconfigured identity provider locks everyone out of this workspace."
                ),
            )

    recorded: dict[str, Any] = {}
    for key, value in changes.items():
        if key in UPDATABLE and getattr(provider, key) != value:
            setattr(provider, key, value)
            recorded[key] = "changed" if key == "x509_cert" else value
    if client_secret is not None:
        provider.client_secret_encrypted = (
            encrypt_secret(client_secret, user.tenant_id) if client_secret else None
        )
        recorded["client_secret"] = "replaced" if client_secret else "cleared"

    if recorded:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.PERMISSION_CHANGE,
            entity_type="identity_provider",
            entity_id=provider.id,
            actor=user,
            changes={**recorded, "severity": "high" if "enforce_sso" in recorded else "normal"},
            summary=f"Updated identity provider {provider.name}",
            request=request,
        )
        await session.commit()
        await session.refresh(provider)
    return provider


def _token_dict(row: ScimToken) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "hint": row.token_hint,
        "active": row.active and row.revoked_at is None,
        "created_at": row.created_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


async def list_tokens(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            tenant_scoped(select(ScimToken), ScimToken, user).order_by(
                ScimToken.created_at.desc()
            )
        )
    ).all()
    return [_token_dict(row) for row in rows]


async def create_token(
    session: AsyncSession, user: User, *, name: str, request: Request | None = None
) -> dict[str, Any]:
    record, plaintext = await scim_service.create_scim_token(session, user, name, request=request)
    return {**_token_dict(record), "token": plaintext}


async def revoke_token(
    session: AsyncSession,
    user: User,
    *,
    token_id: uuid.UUID,
    request: Request | None = None,
) -> dict[str, Any]:
    row = await session.scalar(
        tenant_scoped(select(ScimToken).where(ScimToken.id == token_id), ScimToken, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    if row.revoked_at is None:
        row.active = False
        row.revoked_at = datetime.now(UTC)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.PERMISSION_CHANGE,
            entity_type="scim_token",
            entity_id=row.id,
            actor=user,
            changes={"revoked": True, "hint": row.token_hint},
            summary=f"Revoked SCIM token '{row.name}'",
            request=request,
        )
        await session.commit()
    return _token_dict(row)
