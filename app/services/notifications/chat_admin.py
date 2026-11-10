"""Settings → Notifications → Slack and Microsoft Teams, for the workspace admin.

The admin creates AskMira's app in their own Slack workspace or Microsoft 365
tenant from the manifest given here, pastes its credentials, and chooses what
goes there: approval requests only, or everything people are told in AskMira.
Credentials are checked against Slack or Microsoft before they are kept, are
encrypted with the tenant key, and are never returned.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.notifications import (
    CHAT_CHANNELS,
    Channel,
    ChatEvents,
    ChatRecipient,
    TenantChatApp,
)
from app.models.user import User
from app.services import audit
from app.services.notifications import dispatch, email_brand
from app.services.notifications.adapters import (
    MessageAction,
    OutboundMessage,
    PermanentFailure,
    TransientFailure,
)

__all__ = [
    "LABEL",
    "chat_apps",
    "manifest",
    "provider_of",
    "remove",
    "save",
    "send_test",
]

LABEL = {Channel.SLACK: "Slack", Channel.TEAMS: "Microsoft Teams"}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def provider_of(value: str) -> Channel:
    try:
        channel = Channel(value)
    except ValueError:
        channel = None
    if channel not in CHAT_CHANNELS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown chat app.")
    return channel


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def _row(session: AsyncSession, user: User, provider: Channel) -> TenantChatApp | None:
    return await session.scalar(
        tenant_scoped(
            select(TenantChatApp).where(TenantChatApp.provider == provider), TenantChatApp, user
        )
    )


def inbound_url(provider: Channel, tenant_id: Any) -> str:
    """Where Slack sends events, or Teams its messages, for Ask Mira in this workspace."""
    from app.core.config import settings

    base = (settings.PUBLIC_API_URL or settings.WEB_APP_URL or "").rstrip("/")
    return f"{base}/api/v1/companion/inbound/{provider.value}/{tenant_id}"


def _read(provider: Channel, row: TenantChatApp | None, tenant_id: Any = None) -> dict[str, Any]:
    base = {
        "provider": provider.value,
        "name": LABEL[provider],
        "inbound_url": inbound_url(provider, tenant_id) if tenant_id else None,
    }
    if row is None:
        return {
            **base,
            "configured": False,
            "enabled": False,
            "events": "approvals",
            "ask_mira": False,
            "has_signing_secret": False,
        }
    return {
        **base,
        "configured": True,
        "enabled": row.enabled,
        "events": row.events.value,
        "ask_mira": row.ask_mira,
        "has_signing_secret": bool(row.signing_secret_encrypted),
        "has_secret": bool(row.secret_encrypted),
        "app_id": row.app_id,
        "directory_tenant_id": row.directory_tenant_id,
        "connected_as": row.connected_as,
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "last_error": row.last_error,
    }


async def chat_apps(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    return [_read(p, await _row(session, user, p), user.tenant_id) for p in CHAT_CHANNELS]


async def save(
    session: AsyncSession,
    user: User,
    provider: Channel,
    *,
    enabled: bool,
    events: str,
    secret: str | None,
    app_id: str | None,
    directory_tenant_id: str | None,
    ask_mira: bool = False,
    signing_secret: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Save the app. `secret=None` keeps the stored one. Checked with the provider first.

    `ask_mira` lets people message the app to ask Ask Mira. Slack then needs the
    app's signing secret (`signing_secret`, None keeps it), to tell its events
    from anyone else's.
    """
    try:
        chosen_events = ChatEvents(events)
    except ValueError as exc:
        raise _unprocessable("Choose approvals only, or everything.") from exc
    row = await _row(session, user, provider)
    secret = secret.strip() if secret is not None else None
    if provider is Channel.SLACK:
        if secret and not secret.startswith("xoxb-"):
            raise _unprocessable(
                "Paste the app's Bot User OAuth Token: it starts with xoxb- (Slack → your app → "
                "OAuth & Permissions)."
            )
        app_id = directory_tenant_id = None
    else:
        app_id = (app_id or "").strip()
        directory_tenant_id = (directory_tenant_id or "").strip()
        if not _GUID.match(app_id):
            raise _unprocessable(
                "The bot's Microsoft App ID is a GUID like 0f8fad5b-d9cb-469f-a165-70867728950e."
            )
        if not _GUID.match(directory_tenant_id):
            raise _unprocessable(
                "Your Microsoft 365 tenant id (Directory ID) is a GUID, under Microsoft Entra "
                "ID → Overview."
            )
    signing_secret = signing_secret.strip() if signing_secret is not None else None
    if (
        provider is Channel.SLACK
        and signing_secret
        and not re.fullmatch(r"[0-9a-f]{32}", signing_secret)
    ):
        raise _unprocessable(
            "Paste the app's Signing Secret: 32 letters and digits, under Slack → your app → "
            "Basic Information → App Credentials."
        )
    has_signing = bool(signing_secret) or bool(
        row is not None and row.signing_secret_encrypted and signing_secret is None
    )
    if provider is Channel.SLACK and ask_mira and not has_signing:
        raise _unprocessable(
            "Add the app's signing secret before people can ask Mira in Slack: it is how "
            "AskMira knows a message really came from Slack."
        )
    has_secret = bool(secret) or bool(row is not None and row.secret_encrypted and secret is None)
    if enabled and not has_secret:
        raise _unprocessable(
            "Add the bot token before turning Slack on."
            if provider is Channel.SLACK
            else "Add the bot's client secret before turning Teams on."
        )

    if row is None:
        row = TenantChatApp(tenant_id=user.tenant_id, provider=provider)
        session.add(row)
    changed_identity = (
        bool(secret) or row.app_id != app_id or row.directory_tenant_id != directory_tenant_id
    )
    row.app_id = app_id
    row.directory_tenant_id = directory_tenant_id
    if secret:
        row.secret_encrypted = encrypt_secret(secret, user.tenant_id)
    if provider is Channel.SLACK and signing_secret:
        row.signing_secret_encrypted = encrypt_secret(signing_secret, user.tenant_id)
    row.ask_mira = ask_mira
    row.enabled = enabled
    row.events = chosen_events
    row.configured_by_user_id = user.id

    if has_secret and (changed_identity or enabled):
        # Checked now, so a typo is caught here rather than on the first approval.
        try:
            adapter = dispatch.get_adapter(provider) or dispatch.chat_adapter(row)
            checked = await adapter.check()  # type: ignore[attr-defined]
        except (PermanentFailure, TransientFailure) as exc:
            await session.rollback()
            raise _unprocessable(f"{LABEL[provider]} did not accept these details: {exc}") from exc
        row.connected_as = str(checked.get("connected_as") or "")[:255] or None
        row.last_checked_at = datetime.now(UTC)
        row.last_error = None
    if changed_identity:
        # Another app or tenant: people are looked up there afresh.
        await session.execute(
            delete(ChatRecipient).where(
                ChatRecipient.tenant_id == user.tenant_id, ChatRecipient.provider == provider
            )
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tenant_chat_app",
        entity_id=row.id,
        actor=user,
        changes={
            "provider": provider.value,
            "enabled": enabled,
            "events": chosen_events.value,
            "secret_changed": bool(secret),
            "ask_mira": ask_mira,
            "signing_secret_changed": bool(signing_secret),
            "app_id": app_id,
            "directory_tenant_id": directory_tenant_id,
        },
        summary=f"{LABEL[provider]} {'turned on' if enabled else 'saved, off'}",
        request=request,
    )
    await session.commit()
    return _read(provider, row, user.tenant_id)


async def send_test(
    session: AsyncSession, user: User, provider: Channel, *, request: Request | None = None
) -> dict[str, Any]:
    """Message the admin who asked, the way a real approval would look."""
    row = await _row(session, user, provider)
    if row is None or not row.secret_encrypted:
        raise _unprocessable(f"Save {LABEL[provider]}'s details before sending a test.")
    adapter = dispatch.get_adapter(provider) or dispatch.chat_adapter(row)
    base = email_brand.portal_url()
    actions = (MessageAction(label="Open AskMira", url=base.rstrip("/")),) if base else ()
    ok, error = True, None
    try:
        result = await adapter.send(
            OutboundMessage(
                recipient_name=user.full_name,
                recipient_email=user.email,
                recipient_phone=None,
                subject="AskMira is connected",
                body=(
                    f"This is a test from AskMira. Approval requests will reach people here in "
                    f"{LABEL[provider]}, with Approve and Reject buttons."
                ),
                event_key="system.chat_test",
                actions=actions,
            )
        )
    except (PermanentFailure, TransientFailure) as exc:
        ok, error, result = False, str(exc)[:1000], None
    row.last_checked_at = datetime.now(UTC)
    row.last_error = error
    if result is not None and result.recipient_address:
        kept = await session.scalar(
            tenant_scoped(
                select(ChatRecipient).where(
                    ChatRecipient.provider == provider, ChatRecipient.user_id == user.id
                ),
                ChatRecipient,
                user,
            )
        )
        if kept is None:
            session.add(
                ChatRecipient(
                    tenant_id=user.tenant_id,
                    provider=provider,
                    user_id=user.id,
                    email=user.email.lower(),
                    address=result.recipient_address[:512],
                )
            )
        else:
            kept.email, kept.address = user.email.lower(), result.recipient_address[:512]
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tenant_chat_app",
        entity_id=row.id,
        actor=user,
        changes={"test": "sent" if ok else "failed", "error": error},
        summary=f"{LABEL[provider]} test message {'sent' if ok else 'failed'}",
        request=request,
    )
    await session.commit()
    return {"sent": ok, "error": error}


async def remove(
    session: AsyncSession, user: User, provider: Channel, *, request: Request | None = None
) -> None:
    row = await _row(session, user, provider)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not connected.")
    await session.execute(
        delete(ChatRecipient).where(
            ChatRecipient.tenant_id == user.tenant_id, ChatRecipient.provider == provider
        )
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="tenant_chat_app",
        entity_id=row.id,
        actor=user,
        changes={"provider": provider.value},
        summary=f"{LABEL[provider]} disconnected",
        request=request,
    )
    await session.delete(row)
    await session.commit()


async def manifest(session: AsyncSession, user: User, provider: Channel) -> tuple[str, str, bytes]:
    """The app to install: (filename, media type, bytes)."""
    import json

    from app.services.notifications.chat import slack_manifest, teams_package

    portal = email_brand.portal_url()
    if provider is Channel.SLACK:
        row = await _row(session, user, provider)
        events = inbound_url(provider, user.tenant_id) if row is not None and row.ask_mira else None
        body = json.dumps(slack_manifest(portal, events), indent=2).encode("utf-8")
        return "askmira-slack-manifest.json", "application/json", body
    row = await _row(session, user, provider)
    if row is None or not row.app_id:
        raise _unprocessable(
            "Save the bot's Microsoft App ID first: the Teams app package is built for it."
        )
    return "askmira-teams-app.zip", "application/zip", teams_package(row.app_id, portal)
