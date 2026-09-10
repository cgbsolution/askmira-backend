"""Notification endpoints (slice 05)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
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
from app.services.notifications import dispatch as notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


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
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user.id
                ),
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found"
        )

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
                yield "event: notification\ndata: " + json.dumps(
                    {
                        "id": str(row.id),
                        "event_key": row.event_key,
                        "subject": row.rendered_subject,
                        "body": row.rendered_body,
                        "is_urgent": row.is_urgent,
                        "created_at": row.created_at.isoformat(),
                    }
                ) + "\n\n"

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
        .where(
            Notification.status.in_(
                [NotificationStatus.FAILED, NotificationStatus.DEAD_LETTER]
            )
        )
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
async def delivery_receipt(
    provider: str, request: Request, session: DbSession
) -> dict[str, Any]:
    """Delivery receipts, bounces and complaints.

    A bounce or complaint is a *permanent* signal: the notification is marked
    failed and not retried. Continuing to send to a bouncing address is how a
    sending domain's reputation is destroyed.

    Signature verification per provider lands with the credentials — until
    then this endpoint only ever moves a notification from SENT to DELIVERED
    or FAILED, and never grants anything.
    """
    payload = await request.json()

    message_id = (
        payload.get("providerMessageId")
        or payload.get("message_id")
        or payload.get("id")
    )
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
            f"{provider} reported '{event}': "
            f"{payload.get('reason') or 'no reason supplied'}"
        )
        # Permanent by nature — no next attempt.
        notification.next_attempt_at = None

    await session.commit()
    return {"status": "ok"}
