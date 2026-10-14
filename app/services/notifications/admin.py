"""Notification settings a workspace admin changes, and the person's own inbox tallies.

Templates are versioned rather than edited: saving a template deactivates the
active version and inserts the next one, so "what did the payslip email say in
March" has an answer.

The SMTP password is write-only. It is encrypted with the tenant's derived key
on the way in, and nothing here returns it — the screen is told whether one is
set, and that is all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.notifications import (
    Channel,
    Notification,
    NotificationPolicy,
    NotificationStatus,
    NotificationTemplate,
    TenantSmtpSettings,
)
from app.models.user import User
from app.services import audit
from app.services.notifications import dispatch
from app.services.notifications.adapters import OutboundMessage, PermanentFailure, TransientFailure

__all__ = [
    "channel_status",
    "event_catalogue",
    "get_policy",
    "list_templates",
    "mark_all_read",
    "retry_notification",
    "save_smtp",
    "save_template",
    "smtp_settings",
    "test_smtp",
    "unread_count",
    "update_policy",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


# ── The person's own inbox ──────────────────────────────────────────────────


async def unread_count(session: AsyncSession, user: User) -> int:
    """Unread in-app notifications addressed to the caller."""
    return int(
        await session.scalar(
            tenant_scoped(
                select(func.count(Notification.id)).where(
                    Notification.recipient_user_id == user.id,
                    Notification.channel == Channel.IN_APP,
                    Notification.read_at.is_(None),
                ),
                Notification,
                user,
            )
        )
        or 0
    )


async def mark_all_read(session: AsyncSession, user: User) -> int:
    """Mark every unread in-app notification addressed to the caller as read."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(Notification)
        .where(
            tenant_filter(Notification, user.tenant_id),
            Notification.recipient_user_id == user.id,
            Notification.channel == Channel.IN_APP,
            Notification.read_at.is_(None),
        )
        .values(read_at=now, status=NotificationStatus.READ)
    )
    await session.commit()
    return int(result.rowcount or 0)


def event_catalogue() -> list[dict[str, Any]]:
    """The events a person can set preferences for, with the shipped defaults."""
    return [
        {
            "event_key": key,
            "urgent": key in dispatch.URGENT_EVENT_KEYS,
            "defaults": {channel.value: mode.value for channel, mode in channels.items()},
        }
        for key, channels in sorted(dispatch.DEFAULT_PREFERENCES.items())
    ]


# ── Channels ────────────────────────────────────────────────────────────────


async def channel_status(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Which channels actually deliver in this workspace. Stated, not implied."""
    email_row = await session.scalar(
        tenant_scoped(
            select(TenantSmtpSettings).where(TenantSmtpSettings.enabled.is_(True)),
            TenantSmtpSettings,
            user,
        )
    )
    if email_row is not None:
        email = {
            "state": "live",
            "summary": f"Sent through this workspace's mail relay ({email_row.host}).",
        }
    elif settings.SMTP_HOST and settings.SMTP_FROM:
        email = {"state": "live", "summary": "Sent through the platform mail relay."}
    else:
        email = {
            "state": "not_delivered",
            "summary": (
                "No mail relay is configured, so email is written to the server log and "
                "not delivered. An admin can add one under Settings → Notifications."
            ),
        }
    whatsapp_live = dispatch.get_adapter(Channel.WHATSAPP) is not None
    return [
        {
            "channel": Channel.IN_APP.value,
            "state": "live",
            "summary": "Shown in the bell and on the Notifications page.",
        },
        {"channel": Channel.EMAIL.value, **email},
        {
            "channel": Channel.PUSH.value,
            "state": "not_delivered",
            "summary": (
                "Push is not connected: there is no mobile app registration and no FCM or "
                "APNs account, so a push preference is saved but nothing is sent."
            ),
        },
        {
            "channel": Channel.WHATSAPP.value,
            "state": "live" if whatsapp_live else "not_delivered",
            "summary": (
                "Sent through the WhatsApp Cloud API for people who have opted in."
                if whatsapp_live
                else (
                    "WhatsApp is not connected to a verified business account, so nothing "
                    "is sent. Your opt-in is still recorded for when it is."
                )
            ),
        },
    ]


# ── Quiet hours and rate caps ───────────────────────────────────────────────


def _policy_dict(row: NotificationPolicy, *, configured: bool) -> dict[str, Any]:
    return {
        "configured": configured,
        "quiet_hours_enabled": row.quiet_hours_enabled,
        "quiet_start": row.quiet_start.strftime("%H:%M"),
        "quiet_end": row.quiet_end.strftime("%H:%M"),
        "timezone": row.timezone,
        "rate_limit_per_hour": row.rate_limit_per_hour,
        "max_attempts": row.max_attempts,
        "urgent_events": sorted(dispatch.URGENT_EVENT_KEYS),
    }


async def get_policy(session: AsyncSession, user: User) -> dict[str, Any]:
    row = await session.scalar(tenant_scoped(select(NotificationPolicy), NotificationPolicy, user))
    if row is None:
        # The defaults the pipeline would create on first send, shown unsaved.
        return _policy_dict(
            NotificationPolicy(
                tenant_id=user.tenant_id,
                quiet_hours_enabled=True,
                quiet_start=time(21, 0),
                quiet_end=time(8, 0),
                timezone="Asia/Kolkata",
                rate_limit_per_hour=20,
                max_attempts=5,
            ),
            configured=False,
        )
    return _policy_dict(row, configured=True)


async def update_policy(
    session: AsyncSession,
    user: User,
    *,
    quiet_hours_enabled: bool,
    quiet_start: time,
    quiet_end: time,
    timezone: str,
    rate_limit_per_hour: int,
    max_attempts: int,
    request: Request | None = None,
) -> dict[str, Any]:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise _bad_request(f"{timezone!r} is not a timezone this server recognises.") from exc
    if quiet_hours_enabled and quiet_start == quiet_end:
        raise _bad_request(
            "Quiet hours that start and end at the same minute cover nothing. Turn quiet "
            "hours off instead."
        )

    row = await session.scalar(tenant_scoped(select(NotificationPolicy), NotificationPolicy, user))
    if row is None:
        row = NotificationPolicy(tenant_id=user.tenant_id)
        session.add(row)
    row.quiet_hours_enabled = quiet_hours_enabled
    row.quiet_start = quiet_start
    row.quiet_end = quiet_end
    row.timezone = timezone
    row.rate_limit_per_hour = rate_limit_per_hour
    row.max_attempts = max_attempts
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="notification_policy",
        entity_id=row.id,
        actor=user,
        changes={
            "quiet_hours_enabled": quiet_hours_enabled,
            "quiet_start": quiet_start.strftime("%H:%M"),
            "quiet_end": quiet_end.strftime("%H:%M"),
            "timezone": timezone,
            "rate_limit_per_hour": rate_limit_per_hour,
            "max_attempts": max_attempts,
        },
        summary="Notification quiet hours and rate cap updated",
        request=request,
    )
    await session.commit()
    return _policy_dict(row, configured=True)


# ── Templates ───────────────────────────────────────────────────────────────


def _template_dict(row: NotificationTemplate) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "event_key": row.event_key,
        "channel": row.channel.value,
        "locale": row.locale,
        "subject": row.subject,
        "body": row.body,
        "version": row.version,
        "active": row.active,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def list_templates(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(NotificationTemplate).where(NotificationTemplate.active.is_(True)),
                NotificationTemplate,
                user,
            ).order_by(NotificationTemplate.event_key, NotificationTemplate.channel)
        )
    ).all()
    return [_template_dict(row) for row in rows]


async def save_template(
    session: AsyncSession,
    user: User,
    *,
    event_key: str,
    channel: Channel,
    locale: str,
    subject: str | None,
    body: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Write the next version of a template and retire the current one."""
    if channel is Channel.WHATSAPP:
        raise _bad_request(
            "WhatsApp copy is fixed by the Meta-approved template it names and cannot be "
            "edited here."
        )
    if not body.strip():
        raise _bad_request("A template needs a body.")
    try:
        dispatch.render(body, {})
    except Exception as exc:
        raise _bad_request(f"The template does not render: {exc}") from exc

    existing = (
        await session.scalars(
            tenant_scoped(
                select(NotificationTemplate).where(
                    NotificationTemplate.event_key == event_key,
                    NotificationTemplate.channel == channel,
                    NotificationTemplate.locale == locale,
                ),
                NotificationTemplate,
                user,
            )
        )
    ).all()
    next_version = max((row.version for row in existing), default=0) + 1
    for row in existing:
        row.active = False

    template = NotificationTemplate(
        tenant_id=user.tenant_id,
        event_key=event_key,
        channel=channel,
        locale=locale,
        subject=subject,
        body=body,
        version=next_version,
        active=True,
    )
    session.add(template)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="notification_template",
        entity_id=template.id,
        actor=user,
        changes={"event_key": event_key, "channel": channel.value, "version": next_version},
        summary=f"Template {event_key}/{channel.value} saved as version {next_version}",
        request=request,
    )
    await session.commit()
    await session.refresh(template)
    return _template_dict(template)


# ── Failed deliveries ───────────────────────────────────────────────────────


async def retry_notification(
    session: AsyncSession,
    user: User,
    *,
    notification_id: uuid.UUID,
    request: Request | None = None,
) -> dict[str, Any]:
    """Queue a failed notification for another attempt on the next sweep."""
    row = await session.scalar(
        tenant_scoped(
            select(Notification).where(Notification.id == notification_id), Notification, user
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    if row.status not in (NotificationStatus.FAILED, NotificationStatus.DEAD_LETTER):
        raise _bad_request(f"This notification is {row.status.value}, not failed.")

    previous = row.failure_reason
    row.status = NotificationStatus.QUEUED
    row.next_attempt_at = datetime.now(UTC)
    row.attempt_count = 0
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="notification",
        entity_id=row.id,
        actor=user,
        changes={"previous_failure": (previous or "")[:500]},
        summary="Failed notification queued for another attempt",
        request=request,
    )
    await session.commit()
    return {"id": str(row.id), "status": row.status.value}


# ── SMTP relay ──────────────────────────────────────────────────────────────


def _smtp_dict(row: TenantSmtpSettings | None) -> dict[str, Any]:
    platform = bool(settings.SMTP_HOST and settings.SMTP_FROM)
    if row is None:
        return {"configured": False, "platform_relay_configured": platform}
    return {
        "configured": True,
        "platform_relay_configured": platform,
        "enabled": row.enabled,
        "host": row.host,
        "port": row.port,
        "username": row.username,
        "has_password": bool(row.password_encrypted),
        "use_starttls": row.use_starttls,
        "use_ssl": row.use_ssl,
        "from_address": row.from_address,
        "from_name": row.from_name,
        "last_test_at": row.last_test_at.isoformat() if row.last_test_at else None,
        "last_test_ok": row.last_test_ok,
        "last_test_error": row.last_test_error,
    }


async def smtp_settings(session: AsyncSession, user: User) -> dict[str, Any]:
    row = await session.scalar(tenant_scoped(select(TenantSmtpSettings), TenantSmtpSettings, user))
    return _smtp_dict(row)


async def save_smtp(
    session: AsyncSession,
    user: User,
    *,
    enabled: bool,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    use_starttls: bool,
    use_ssl: bool,
    from_address: str,
    from_name: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Save the relay. `password=None` keeps the stored one; an empty string clears it."""
    if use_ssl and use_starttls:
        raise _bad_request("Choose implicit TLS (SSL) or STARTTLS, not both.")
    row = await session.scalar(tenant_scoped(select(TenantSmtpSettings), TenantSmtpSettings, user))
    if row is None:
        row = TenantSmtpSettings(tenant_id=user.tenant_id, host=host, from_address=from_address)
        session.add(row)
    row.enabled = enabled
    row.host = host.strip()
    row.port = port
    row.username = (username or "").strip() or None
    if password is not None:
        row.password_encrypted = encrypt_secret(password, user.tenant_id) if password else None
    row.use_starttls = use_starttls
    row.use_ssl = use_ssl
    row.from_address = from_address.strip()
    row.from_name = (from_name or "").strip() or None
    row.configured_by_user_id = user.id
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tenant_smtp_settings",
        entity_id=row.id,
        actor=user,
        changes={
            "enabled": enabled,
            "host": row.host,
            "port": port,
            "from_address": row.from_address,
            "password_changed": password is not None,
        },
        summary="Mail relay settings saved",
        request=request,
    )
    await session.commit()
    return _smtp_dict(row)


async def test_smtp(
    session: AsyncSession, user: User, *, request: Request | None = None
) -> dict[str, Any]:
    """Send a test message to the admin who asked, through the configured relay."""
    row = await session.scalar(tenant_scoped(select(TenantSmtpSettings), TenantSmtpSettings, user))
    if row is None or not row.enabled:
        raise _bad_request("Save and enable this workspace's mail relay before testing it.")

    adapter, source = await dispatch.email_adapter_for(session, user.tenant_id)
    ok, error = True, None
    try:
        await adapter.send(
            OutboundMessage(
                recipient_name=user.full_name,
                recipient_email=user.email,
                recipient_phone=None,
                subject="AskMira mail relay test",
                body=(
                    "This is a test message from AskMira. If you can read it, this "
                    "workspace's mail relay delivers."
                ),
                event_key="system.smtp_test",
            )
        )
    except (PermanentFailure, TransientFailure) as exc:
        ok, error = False, str(exc)[:1000]

    row.last_test_at = datetime.now(UTC)
    row.last_test_ok = ok
    row.last_test_error = error
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tenant_smtp_settings",
        entity_id=row.id,
        actor=user,
        changes={"test_ok": ok, "relay": source},
        summary=f"Mail relay test {'succeeded' if ok else 'failed'}",
        request=request,
    )
    await session.commit()
    return {"ok": ok, "error": error, "sent_to": user.email if ok else None}
