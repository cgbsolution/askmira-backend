"""Settings → Integrations → Digio: the workspace's own Aadhaar eSign account.

The admin pastes the client id and secret from their Digio dashboard (sandbox or
production) and, optionally, the webhook secret. The credentials are checked
against Digio before they are kept. Letters sent for signature then go out on
this account, and Digio tells AskMiraa when one is signed at this workspace's own
webhook address — or, without a webhook, the letter's status is checked on
request and every hour.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import TenantConnection
from app.models.user import User
from app.services.connections import store
from app.services.documents.signature import DigioProvider, SignatureError

__all__ = ["connect", "disconnect", "provider_for_tenant", "status_for", "webhook_path"]

LABEL = "Digio eSign"


def webhook_path(row: TenantConnection) -> str:
    """This workspace's webhook address on the portal. The id names the workspace;
    the webhook secret proves the call is Digio's."""
    return f"/api/v1/webhooks/signature/digio/w/{row.id}"


def _provider(row: TenantConnection, *, transport: Any = None) -> DigioProvider | None:
    secret = store.secrets_of(row)
    client_id = str((row.settings or {}).get("client_id") or "")
    if not client_id or not secret.get("client_secret"):
        return None
    environment = str((row.settings or {}).get("environment") or "production")
    return DigioProvider(
        client_id,
        str(secret["client_secret"]),
        str(secret.get("webhook_secret") or ""),
        base_url=DigioProvider.ENVIRONMENTS.get(environment, DigioProvider.BASE_URL),
        transport=transport,
    )


async def provider_for_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, *, transport: Any = None
) -> DigioProvider | None:
    """The workspace's own Digio account, or None when it has not connected one."""
    row = await store.get_for_tenant(session, tenant_id, store.KIND_DIGIO)
    if row is None or not row.enabled:
        return None
    return _provider(row, transport=transport)


def _view(row: TenantConnection | None) -> dict[str, Any]:
    out = store.read(row)
    out["webhook_path"] = webhook_path(row) if row is not None else None
    stored = store.secrets_of(row) if row is not None else {}
    out["has_webhook_secret"] = bool(stored.get("webhook_secret"))
    out["environments"] = sorted(DigioProvider.ENVIRONMENTS)
    return out


async def status_for(session: AsyncSession, user: User) -> dict[str, Any]:
    return _view(await store.get(session, user, store.KIND_DIGIO))


async def connect(
    session: AsyncSession,
    user: User,
    *,
    environment: str,
    client_id: str,
    client_secret: str | None,
    webhook_secret: str | None,
    request: Request | None = None,
    transport: Any = None,
) -> dict[str, Any]:
    """Check the credentials at Digio, then keep them. Nothing is kept if Digio refuses."""
    environment = (environment or "").strip().lower()
    if environment not in DigioProvider.ENVIRONMENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose the Digio environment: sandbox or production.",
        )
    client_id = (client_id or "").strip()
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Enter the Digio client id."
        )

    existing = await store.get(session, user, store.KIND_DIGIO)
    stored = store.secrets_of(existing) if existing is not None else {}
    # A blank secret on an existing connection keeps the stored one, so the
    # environment or the webhook secret can change without retyping it.
    secret = (client_secret or "").strip() or str(stored.get("client_secret") or "")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter the Digio client secret.",
        )
    hook = (webhook_secret or "").strip() or str(stored.get("webhook_secret") or "")

    provider = DigioProvider(
        client_id,
        secret,
        hook,
        base_url=DigioProvider.ENVIRONMENTS[environment],
        transport=transport,
    )
    try:
        await provider.check_credentials()
    except SignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    row = await store.save(
        session,
        user,
        store.KIND_DIGIO,
        settings={"environment": environment, "client_id": client_id},
        # Without a webhook secret Digio's calls cannot be verified, so they are
        # refused; the letter's status is then checked at Digio instead.
        secrets={"client_secret": secret, "webhook_secret": hook},
        account_label=f"Digio {environment}",
        label=LABEL,
        request=request,
    )
    return _view(row)


async def disconnect(session: AsyncSession, user: User, *, request: Request | None = None) -> None:
    if not await store.remove(session, user, store.KIND_DIGIO, label=LABEL, request=request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Digio is not connected.")
