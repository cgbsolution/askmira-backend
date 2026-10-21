"""Notification endpoints (slice 05)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.notifications import (
    Channel,
    DigestMode,
    Notification,
    NotificationPreference,
    NotificationStatus,
    WhatsAppConsent,
)
from app.models.user import UserRole
from app.services.notifications import admin as notification_admin
from app.services.notifications import dispatch as notification_service
from app.services.notifications import email_admin
from app.services.notifications.email_brand import BrandSettings

router = APIRouter(prefix="/notifications", tags=["notifications"])

HR_ROLES = (UserRole.ADMIN,)
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]


class PolicyUpdate(BaseModel):
    quiet_hours_enabled: bool
    quiet_start: time
    quiet_end: time
    timezone: str = Field(min_length=1, max_length=64)
    rate_limit_per_hour: int = Field(ge=1, le=1000)
    max_attempts: int = Field(ge=1, le=20)


class TemplateSave(BaseModel):
    event_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_.]+$")
    channel: Channel
    locale: str = Field(default="en-IN", min_length=2, max_length=16)
    subject: str | None = Field(default=None, max_length=512)
    body: str = Field(min_length=1, max_length=8000)


class SmtpSave(BaseModel):
    """`password` omitted keeps the stored one; an empty string clears it."""

    enabled: bool = True
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=512)
    use_starttls: bool = True
    use_ssl: bool = False
    from_address: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    from_name: str | None = Field(default=None, max_length=128)


# ── The caller's own tallies and the catalogue ──────────────────────────────


@router.get("/unread-count")
async def unread_count(user: CurrentUser, session: DbSession) -> dict[str, int]:
    """The number behind the header bell."""
    return {"count": await notification_admin.unread_count(session, user)}


@router.post("/read-all")
async def read_all(user: CurrentUser, session: DbSession) -> dict[str, int]:
    return {"marked": await notification_admin.mark_all_read(session, user)}


@router.get("/channels")
async def channels(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Which channels actually deliver here — push and unconnected ones say so."""
    return await notification_admin.channel_status(session, user)


@router.get("/events")
async def events(user: CurrentUser) -> list[dict[str, Any]]:
    return notification_admin.event_catalogue()


# ── Workspace administration ────────────────────────────────────────────────


@router.get("/admin/policy", dependencies=ADMIN_ONLY)
async def get_policy(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await notification_admin.get_policy(session, user)


@router.put("/admin/policy", dependencies=ADMIN_ONLY)
async def update_policy(
    payload: PolicyUpdate, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await notification_admin.update_policy(
        session, user, **payload.model_dump(), request=request
    )


@router.get("/admin/templates", dependencies=ADMIN_ONLY)
async def list_templates(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await notification_admin.list_templates(session, user)


@router.put("/admin/templates", dependencies=ADMIN_ONLY)
async def save_template(
    payload: TemplateSave, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Save the next version of a template. The previous version is kept, inactive."""
    return await notification_admin.save_template(
        session, user, **payload.model_dump(), request=request
    )


class TemplateReset(BaseModel):
    event_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_.]+$")
    channel: Channel
    locale: str = Field(default="en-IN", min_length=2, max_length=16)


class EmailDraft(BaseModel):
    """An email to preview or test-send: wording, and optionally unsaved design choices."""

    event_key: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9_.]+$")
    subject: str = Field(default="", max_length=512)
    body: str = Field(min_length=1, max_length=8000)
    branding: BrandSettings | None = None


@router.post("/admin/templates/reset", dependencies=ADMIN_ONLY)
async def reset_template(
    payload: TemplateReset, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Go back to the built-in wording; the workspace's versions are kept, inactive."""
    return await email_admin.reset_template(session, user, **payload.model_dump(), request=request)


@router.get("/admin/emails", dependencies=ADMIN_ONLY)
async def email_catalogue(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Every email the workspace can send, with its built-in and its own wording."""
    return await email_admin.catalogue(session, user)


@router.get("/admin/email-design", dependencies=ADMIN_ONLY)
async def get_email_design(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await email_admin.get_branding(session, user)


@router.put("/admin/email-design", dependencies=ADMIN_ONLY)
async def save_email_design(
    payload: BrandSettings, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await email_admin.save_branding(session, user, payload, request=request)


@router.post("/admin/emails/preview", dependencies=ADMIN_ONLY)
async def preview_email(
    payload: EmailDraft, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """The email as it will look, with sample values. Nothing is saved or sent."""
    return await email_admin.preview(
        session, user, **payload.model_dump(exclude={"branding"}), branding=payload.branding
    )


@router.post("/admin/emails/test", dependencies=ADMIN_ONLY)
async def send_test_email(
    payload: EmailDraft, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Send the previewed email to the admin who asked."""
    return await email_admin.send_test(
        session,
        user,
        **payload.model_dump(exclude={"branding"}),
        branding=payload.branding,
        request=request,
    )


@router.post("/admin/failed/{notification_id}/retry", dependencies=ADMIN_ONLY)
async def retry_failed(
    notification_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await notification_admin.retry_notification(
        session, user, notification_id=notification_id, request=request
    )


class DeviceIn(BaseModel):
    push_token: str = Field(min_length=10, max_length=255)
    platform: str = Field(default="expo", max_length=16)


@router.post("/devices")
async def register_device(
    payload: DeviceIn, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """The app on the caller's phone, for push."""
    return await notification_admin.register_device(
        session, user, push_token=payload.push_token, platform=payload.platform
    )


@router.post("/devices/unregister", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def unregister_device(payload: DeviceIn, user: CurrentUser, session: DbSession) -> None:
    """Stop pushing to this phone (sign-out)."""
    await notification_admin.unregister_device(session, user, push_token=payload.push_token)


@router.get("/admin/smtp", dependencies=ADMIN_ONLY)
async def get_smtp(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """The relay settings. The password is never returned — only whether one is set."""
    return await notification_admin.smtp_settings(session, user)


@router.put("/admin/smtp", dependencies=ADMIN_ONLY)
async def save_smtp(
    payload: SmtpSave, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await notification_admin.save_smtp(
        session, user, **payload.model_dump(), request=request
    )


class WhatsAppSave(BaseModel):
    enabled: bool
    phone_number_id: str = Field(min_length=1, max_length=64)
    #: Null keeps the stored token; an empty string clears it.
    access_token: str | None = Field(default=None, max_length=1024)


@router.get("/admin/whatsapp", dependencies=ADMIN_ONLY)
async def get_whatsapp(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """This workspace's WhatsApp Business account. The token is never returned."""
    return await notification_admin.whatsapp_settings(session, user)


@router.put("/admin/whatsapp", dependencies=ADMIN_ONLY)
async def save_whatsapp(
    payload: WhatsAppSave, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await notification_admin.save_whatsapp(
        session, user, **payload.model_dump(), request=request
    )


@router.post("/admin/smtp/test", dependencies=ADMIN_ONLY)
async def test_smtp(user: CurrentUser, session: DbSession, request: Request) -> dict[str, Any]:
    """Send a test message to the admin who asked."""
    return await notification_admin.test_smtp(session, user, request=request)


# ── Contracts ───────────────────────────────────────────────────────────────


class PreferenceItem(BaseModel):
    event_key: str = Field(min_length=1, max_length=128)
    channel: Channel
    enabled: bool = True
    digest_mode: DigestMode = DigestMode.IMMEDIATE


class PreferenceUpdate(BaseModel):
    preferences: list[PreferenceItem] = Field(min_length=1, max_length=200)


class PreferenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_key: str
    channel: Channel
    enabled: bool
    digest_mode: DigestMode


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_key: str
    channel: Channel
    rendered_subject: str | None
    rendered_body: str
    status: NotificationStatus
    is_urgent: bool
    payload: dict
    created_at: datetime
    read_at: datetime | None


class FailedNotificationRead(NotificationRead):
    """Dead-letter visibility.

    A failed notification must be findable, not only present in a log. This is
    the shape the admin screen renders.
    """

    recipient_user_id: uuid.UUID
    failure_reason: str | None
    attempt_count: int
    next_attempt_at: datetime | None


class ConsentRequest(BaseModel):
    phone_e164: str = Field(pattern=r"^\+[1-9]\d{7,14}$", max_length=20)
    opted_in: bool
    source: str = Field(default="ess_settings", max_length=64)


class ConsentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    phone_e164: str
    opted_in: bool
    opted_in_at: datetime | None
    opted_out_at: datetime | None
    source: str | None


# ── Preferences ─────────────────────────────────────────────────────────────


@router.get("/preferences", response_model=list[PreferenceRead])
async def get_preferences(user: CurrentUser, session: DbSession) -> list[PreferenceRead]:
    """This user's preferences, with the shipped defaults filled in.

    Returning the effective set rather than only stored rows means the settings
    screen shows what will actually happen, not an empty form.
    """
    stored = (
        await session.scalars(
            tenant_scoped(
                select(NotificationPreference).where(NotificationPreference.user_id == user.id),
                NotificationPreference,
                user,
            )
        )
    ).all()
    explicit = {(row.event_key, row.channel) for row in stored}

    effective = [PreferenceRead.model_validate(row) for row in stored]
    for event_key, channels in notification_service.DEFAULT_PREFERENCES.items():
        for channel, mode in channels.items():
            if (event_key, channel) in explicit:
                continue
            effective.append(
                PreferenceRead(
                    event_key=event_key,
                    channel=channel,
                    enabled=mode is not DigestMode.OFF,
                    digest_mode=mode,
                )
            )

    return sorted(effective, key=lambda p: (p.event_key, p.channel.value))


@router.patch("/preferences", response_model=list[PreferenceRead])
async def update_preferences(
    payload: PreferenceUpdate, user: CurrentUser, session: DbSession
) -> list[PreferenceRead]:
    """Set preferences. A user may only change their own."""
    for item in payload.preferences:
        existing = await session.scalar(
            tenant_scoped(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user.id,
                    NotificationPreference.event_key == item.event_key,
                    NotificationPreference.channel == item.channel,
                ),
                NotificationPreference,
                user,
            )
        )
        if existing is None:
            session.add(
                NotificationPreference(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    event_key=item.event_key,
                    channel=item.channel,
                    enabled=item.enabled,
                    digest_mode=item.digest_mode,
                )
            )
        else:
            existing.enabled = item.enabled
            existing.digest_mode = item.digest_mode

    await session.commit()
    return await get_preferences(user, session)


# ── In-app feed ─────────────────────────────────────────────────────────────


@router.get("", response_model=list[NotificationRead])
async def list_notifications(
    user: CurrentUser,
    session: DbSession,
    unread: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[NotificationRead]:
    stmt = tenant_scoped(
        select(Notification)
        .where(
            Notification.recipient_user_id == user.id,
            Notification.channel == Channel.IN_APP,
        )
        .order_by(Notification.created_at.desc())
        .limit(limit),
        Notification,
        user,
    )
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    return [NotificationRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


@router.post("/{notification_id}/read", response_model=NotificationRead)
async def mark_read(
    notification_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> NotificationRead:
    notification = await session.scalar(
        tenant_scoped(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.recipient_user_id == user.id,
            ),
            Notification,
            user,
        )
    )
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")

    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        notification.status = NotificationStatus.READ
        await session.commit()
        await session.refresh(notification)

    return NotificationRead.model_validate(notification)


@router.get("/stream")
async def stream(user: CurrentUser, session: DbSession, request: Request) -> StreamingResponse:
    """Server-sent events for the in-app bell.

    Polls rather than listening on a Postgres channel: the poll is cheap, and
    a LISTEN connection per signed-in user does not scale on a pooled
    connection. Revisit if the notification volume ever justifies it.
    """

    async def events():
        last_seen = datetime.now(UTC)
        # A comment frame immediately, so the client knows the stream is live
        # rather than waiting for the first real event.
        yield ": connected\n\n"

        while True:
            if await request.is_disconnected():
                break

            rows = (
                await session.scalars(
                    tenant_scoped(
                        select(Notification)
                        .where(
                            Notification.recipient_user_id == user.id,
                            Notification.channel == Channel.IN_APP,
                            Notification.created_at > last_seen,
                        )
                        .order_by(Notification.created_at),
                        Notification,
                        user,
                    )
                )
            ).all()

            for row in rows:
                last_seen = max(last_seen, row.created_at)
                yield (
                    "event: notification\ndata: "
                    + json.dumps(
                        {
                            "id": str(row.id),
                            "event_key": row.event_key,
                            "subject": row.rendered_subject,
                            "body": row.rendered_body,
                            "is_urgent": row.is_urgent,
                            "created_at": row.created_at.isoformat(),
                        }
                    )
                    + "\n\n"
                )

            # Keep-alive, so proxies do not close an idle stream.
            yield ": keep-alive\n\n"
            await asyncio.sleep(15)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── WhatsApp consent ────────────────────────────────────────────────────────


@router.get("/whatsapp/consent", response_model=ConsentRead | None)
async def get_consent(user: CurrentUser, session: DbSession) -> ConsentRead | None:
    consent = await session.scalar(
        tenant_scoped(
            select(WhatsAppConsent).where(WhatsAppConsent.user_id == user.id),
            WhatsAppConsent,
            user,
        )
    )
    return ConsentRead.model_validate(consent) if consent else None


@router.put("/whatsapp/consent", response_model=ConsentRead)
async def set_consent(
    payload: ConsentRequest, user: CurrentUser, session: DbSession
) -> ConsentRead:
    """Record or withdraw WhatsApp opt-in.

    The timestamp and source are kept because "we have consent" is a claim
    that has to be evidenced — to a regulator, and to Meta if the number is
    ever reported.
    """
    consent = await session.scalar(
        tenant_scoped(
            select(WhatsAppConsent).where(WhatsAppConsent.user_id == user.id),
            WhatsAppConsent,
            user,
        )
    )
    now = datetime.now(UTC)

    if consent is None:
        consent = WhatsAppConsent(
            tenant_id=user.tenant_id, user_id=user.id, phone_e164=payload.phone_e164
        )
        session.add(consent)

    consent.phone_e164 = payload.phone_e164
    consent.source = payload.source
    if payload.opted_in:
        consent.opted_in = True
        consent.opted_in_at = now
        consent.opted_out_at = None
    else:
        consent.opted_in = False
        consent.opted_out_at = now

    await session.commit()
    await session.refresh(consent)
    return ConsentRead.model_validate(consent)


# ── Dead-letter visibility ──────────────────────────────────────────────────


@router.get(
    "/failed",
    response_model=list[FailedNotificationRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_failed(
    user: CurrentUser, session: DbSession, limit: int = Query(default=100, ge=1, le=500)
) -> list[FailedNotificationRead]:
    """Notifications that did not arrive.

    Queryable rather than only logged. "Nobody got the payroll alert" should be
    noticeable in an hour, not a month.
    """
    stmt = tenant_scoped(
        select(Notification)
        .where(Notification.status.in_([NotificationStatus.FAILED, NotificationStatus.DEAD_LETTER]))
        .order_by(Notification.created_at.desc())
        .limit(limit),
        Notification,
        user,
    )
    return [
        FailedNotificationRead.model_validate(row) for row in (await session.scalars(stmt)).all()
    ]


@router.post(
    "/process-due",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def process_due(user: CurrentUser, session: DbSession) -> dict[str, int]:
    """Send deferred and retryable notifications whose time has come.

    Called by the scheduler; exposed so an admin can flush a backlog manually
    after fixing whatever caused the deferral.
    """
    sent = await notification_service.process_due_notifications(session, user.tenant_id)
    digests = await notification_service.send_digests(session, user.tenant_id)
    return {"sent": sent, "digests_sent": digests}


@router.post(
    "/templates/seed",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def seed_templates(user: CurrentUser, session: DbSession) -> dict[str, int]:
    created = await notification_service.seed_default_templates(session, user.tenant_id)
    return {"created": len(created)}


# ── Provider webhooks ───────────────────────────────────────────────────────


@router.post("/webhooks/{provider}", include_in_schema=False)
async def delivery_receipt(provider: str, request: Request, session: DbSession) -> dict[str, Any]:
    """Delivery receipts, bounces and complaints.

    A bounce or complaint is a *permanent* signal: the notification is marked
    failed and not retried. Continuing to send to a bouncing address is how a
    sending domain's reputation is destroyed.

    Signature verification per provider lands with the credentials — until
    then this endpoint only ever moves a notification from SENT to DELIVERED
    or FAILED, and never grants anything.
    """
    payload = await request.json()

    message_id = payload.get("providerMessageId") or payload.get("message_id") or payload.get("id")
    if not message_id:
        return {"status": "ignored", "reason": "no message id in payload"}

    notification = await session.scalar(
        select(Notification).where(Notification.provider_message_id == str(message_id))
    )
    if notification is None:
        # 200 rather than 404: providers retry non-2xx, and retrying a receipt
        # for a message we do not have will never succeed.
        return {"status": "unknown_message"}

    event = str(payload.get("event") or payload.get("status") or "").lower()

    if event in {"delivered", "delivery", "read"}:
        notification.status = (
            NotificationStatus.READ if event == "read" else NotificationStatus.DELIVERED
        )
        notification.delivered_at = notification.delivered_at or datetime.now(UTC)
    elif event in {"bounce", "bounced", "complaint", "failed", "undelivered"}:
        notification.status = NotificationStatus.FAILED
        notification.failure_reason = (
            f"{provider} reported '{event}': {payload.get('reason') or 'no reason supplied'}"
        )
        # Permanent by nature — no next attempt.
        notification.next_attempt_at = None

    await session.commit()
    return {"status": "ok"}
