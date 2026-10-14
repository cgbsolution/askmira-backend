"""The notification dispatch pipeline.

One entry point: `notify(...)`. Callers name an event and some recipients. What
happens next — which channels, whether it sends now or joins a digest, whether
quiet hours defer it — is the recipient's preference and the tenant's policy,
never the caller's concern.

The pipeline, in order:

  1. Resolve each recipient's preference for this event key
  2. OFF drops it; DAILY_DIGEST appends to their digest; IMMEDIATE proceeds
  3. Render the template for the channel and locale
  4. Gate: WhatsApp consent and template approval, quiet hours, rate cap
  5. Create the Notification row, then dispatch
  6. Record the status transition

Two things this deliberately does *not* do:

- It does not let one channel's failure affect another. Each channel is
  dispatched independently, and a WhatsApp outage leaves the email delivered.
- It does not fail the whole call because one recipient is misconfigured. A
  500-person announcement should not be lost because one of them has a
  malformed address.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.core.deps import tenant_filter
from app.models.notifications import (
    Channel,
    DigestMode,
    Notification,
    NotificationDigest,
    NotificationPolicy,
    NotificationPreference,
    NotificationStatus,
    NotificationTemplate,
    TenantSmtpSettings,
    WhatsAppConsent,
    WhatsAppTemplate,
)
from app.models.user import User
from app.services.notifications.adapters import (
    ChannelAdapter,
    ConsoleEmailAdapter,
    InAppAdapter,
    NullPushAdapter,
    OutboundMessage,
    PermanentFailure,
    SmtpEmailAdapter,
    TransientFailure,
)

__all__ = [
    "DEFAULT_PREFERENCES",
    "DEFAULT_TEMPLATES",
    "URGENT_EVENT_KEYS",
    "DispatchOutcome",
    "NotificationError",
    "email_adapter_for",
    "email_wording",
    "notify",
    "process_due_notifications",
    "register_event",
    "render",
    "send_digests",
]


class NotificationError(RuntimeError):
    """A caller-visible problem: no template, unknown event."""


# Events that bypass quiet hours and the rate cap. Deliberately short: the
# value of an urgent channel is that it is rare.
URGENT_EVENT_KEYS: frozenset[str] = frozenset(
    {
        "payroll.run.failed",
        "payroll.rule_set.expired",
        "security.break_glass_used",
        "security.suspicious_login",
        "attendance.device.silent",
        "system.announcement.critical",
        # Somebody is locked out and waiting on this email.
        "auth.password.reset",
    }
)

# Shipped defaults. Approvals reach people immediately because someone is
# blocked waiting; social traffic is digested because it is pleasant, not
# urgent, and an immediate shoutout notification is how a channel gets muted.
DEFAULT_PREFERENCES: dict[str, dict[Channel, DigestMode]] = {
    "leave.approval.requested": {
        Channel.EMAIL: DigestMode.IMMEDIATE,
        Channel.IN_APP: DigestMode.IMMEDIATE,
        Channel.PUSH: DigestMode.IMMEDIATE,
    },
    "leave.approval.decided": {
        Channel.EMAIL: DigestMode.IMMEDIATE,
        Channel.IN_APP: DigestMode.IMMEDIATE,
    },
    "attendance.regularisation.requested": {
        Channel.IN_APP: DigestMode.IMMEDIATE,
        Channel.EMAIL: DigestMode.IMMEDIATE,
    },
    "payroll.payslip.published": {
        Channel.EMAIL: DigestMode.IMMEDIATE,
        Channel.IN_APP: DigestMode.IMMEDIATE,
        Channel.WHATSAPP: DigestMode.IMMEDIATE,
    },
    "payroll.run.failed": {
        Channel.EMAIL: DigestMode.IMMEDIATE,
        Channel.IN_APP: DigestMode.IMMEDIATE,
    },
    "culture.shoutout.received": {
        Channel.IN_APP: DigestMode.IMMEDIATE,
        Channel.EMAIL: DigestMode.DAILY_DIGEST,
    },
    "culture.shoutout.posted": {
        Channel.IN_APP: DigestMode.DAILY_DIGEST,
        Channel.EMAIL: DigestMode.OFF,
    },
    "documents.letter.issued": {
        Channel.EMAIL: DigestMode.IMMEDIATE,
        Channel.IN_APP: DigestMode.IMMEDIATE,
    },
    "system.announcement": {
        Channel.EMAIL: DigestMode.IMMEDIATE,
        Channel.IN_APP: DigestMode.IMMEDIATE,
    },
}


# Built-in wording per event: `(subject, body)`, rendered with the payload.
# A tenant's own template (Settings → Notifications) wins; without one, these
# are used, so a new workspace notifies people from day one instead of
# silently dropping every event for want of a seeded template.
DEFAULT_TEMPLATES: dict[str, tuple[str, str]] = {
    "leave.approval.requested": (
        "Leave approval needed",
        "{employee_name} has requested {leave_type} from {start_date} to {end_date}.",
    ),
    "leave.approval.decided": (
        "Your leave request was {decision}",
        "Your {leave_type} request for {start_date} to {end_date} was {decision}.",
    ),
    "attendance.regularisation.requested": (
        "Attendance regularisation needed",
        "{employee_name} has raised a regularisation for {attendance_date}.",
    ),
    "payroll.payslip.published": (
        "Your payslip for {period} is ready",
        "Your payslip for {period} is available in AskMira.",
    ),
    "payroll.run.failed": (
        "Payroll run failed for {period}",
        "The payroll run for {period} could not complete: {reason}",
    ),
    "culture.shoutout.received": (
        "{from_name} gave you a shoutout",
        "{from_name} said: {message}",
    ),
    "documents.letter.issued": (
        "A document is ready for you",
        "Your {category} letter ({reference_number}) is available in AskMira.",
    ),
    "system.announcement": ("{title}", "{message}"),
}


def register_event(
    event_key: str,
    subject: str,
    body: str,
    *,
    preferences: dict[Channel, DigestMode] | None = None,
) -> None:
    """Declare an event's built-in wording, and optionally its default channels.

    Modules call this at import time for the events they raise, next to the
    code that raises them. Without a registered default an event reaches only
    tenants that wrote their own template.
    """
    DEFAULT_TEMPLATES[event_key] = (subject, body)
    if preferences is not None:
        DEFAULT_PREFERENCES[event_key] = dict(preferences)


@dataclass(slots=True)
class DispatchOutcome:
    """What happened, per recipient and channel.

    Returned rather than raised so a caller can surface partial failure without
    the whole notification call becoming an exception path.
    """

    queued: int = 0
    sent: int = 0
    digested: int = 0
    deferred: int = 0
    suppressed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


# ── Rendering ───────────────────────────────────────────────────────────────


def render(template_body: str, payload: dict[str, Any]) -> str:
    """Substitute `{placeholder}` tokens.

    Deliberately not a general template language. A notification body is a
    sentence with a few values in it; anything that needs logic belongs in the
    caller's payload, not in a template a non-engineer edits.

    An unknown placeholder is left as-is rather than raising: a notification
    that arrives slightly wrong beats one that never arrives because a payload
    key was renamed.
    """
    result = template_body
    for key, value in payload.items():
        result = result.replace("{" + str(key) + "}", str(value))
    return result


async def _template_for(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    event_key: str,
    channel: Channel,
    locale: str = "en-IN",
) -> NotificationTemplate | None:
    stmt = (
        select(NotificationTemplate)
        .where(
            tenant_filter(NotificationTemplate, tenant_id),
            NotificationTemplate.event_key == event_key,
            NotificationTemplate.channel == channel,
            NotificationTemplate.active.is_(True),
        )
        .order_by(NotificationTemplate.version.desc())
    )
    exact = await session.scalar(stmt.where(NotificationTemplate.locale == locale))
    found = exact or await session.scalar(stmt)
    if found is not None or channel is Channel.WHATSAPP:
        # WhatsApp needs a tenant template naming an approved Meta template, so
        # it never falls back to built-in wording.
        return found
    default = DEFAULT_TEMPLATES.get(event_key)
    if default is None:
        return None
    # Transient: rendered from, never added to the session.
    return NotificationTemplate(
        tenant_id=tenant_id,
        event_key=event_key,
        channel=channel,
        locale=locale,
        subject=default[0],
        body=default[1],
        version=0,
        active=True,
    )


async def email_wording(
    session: AsyncSession, tenant_id: uuid.UUID, event_key: str, locale: str = "en-IN"
) -> tuple[str, str] | None:
    """The subject and body a tenant sends by email for an event.

    Its own active template if it wrote one, else the built-in wording. For
    messages to people who are not users — candidates — which still deserve
    the tenant's own words.
    """
    template = await _template_for(session, tenant_id, event_key, Channel.EMAIL, locale)
    if template is None:
        return None
    return template.subject or "", template.body


# ── Policy gates ────────────────────────────────────────────────────────────


async def _policy(session: AsyncSession, tenant_id: uuid.UUID) -> NotificationPolicy:
    policy = await session.scalar(
        select(NotificationPolicy).where(tenant_filter(NotificationPolicy, tenant_id))
    )
    if policy is None:
        policy = NotificationPolicy(tenant_id=tenant_id)
        session.add(policy)
        await session.flush()
    return policy


def _in_quiet_hours(policy: NotificationPolicy, moment: datetime) -> bool:
    """Whether `moment` falls inside the tenant's quiet window.

    Handles a window that wraps midnight — 21:00 to 08:00 is the common case
    and a naive `start <= now <= end` gets it exactly backwards.
    """
    if not policy.quiet_hours_enabled:
        return False

    try:
        zone = ZoneInfo(policy.timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Kolkata")

    local = moment.astimezone(zone).time()
    start, end = policy.quiet_start, policy.quiet_end

    if start == end:
        return False
    if start < end:
        return start <= local < end
    # Wraps midnight.
    return local >= start or local < end


def _next_window_open(policy: NotificationPolicy, moment: datetime) -> datetime:
    """When the quiet window next ends."""
    try:
        zone = ZoneInfo(policy.timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Kolkata")

    local = moment.astimezone(zone)
    candidate = local.replace(
        hour=policy.quiet_end.hour, minute=policy.quiet_end.minute, second=0, microsecond=0
    )
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


async def _over_rate_limit(
    session: AsyncSession,
    policy: NotificationPolicy,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    channel: Channel,
) -> bool:
    """Whether this user has already had their hourly allowance on this channel.

    A bulk operation touching five hundred people must not send any one of them
    two hundred messages.
    """
    since = datetime.now(UTC) - timedelta(hours=1)
    count = await session.scalar(
        select(func.count(Notification.id)).where(
            # Scoped explicitly. User ids are UUIDs so a collision is not
            # realistic, but the isolation rule holds without needing anyone to
            # reason that through — and it is the better index path.
            tenant_filter(Notification, tenant_id),
            Notification.recipient_user_id == user_id,
            Notification.channel == channel,
            Notification.created_at >= since,
            Notification.status.in_(
                [NotificationStatus.SENT, NotificationStatus.DELIVERED,
                 NotificationStatus.QUEUED]
            ),
        )
    )
    return (count or 0) >= policy.rate_limit_per_hour


async def _whatsapp_gate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    template: NotificationTemplate,
) -> tuple[WhatsAppConsent, WhatsAppTemplate]:
    """Consent and template approval, checked at enqueue.

    At enqueue rather than at send, deliberately. A failure at send is
    invisible until someone reads a log; a failure here is returned to the
    caller and surfaced. And sending without consent is the fastest way to get
    a business number blocked, so it must not be possible to queue one.

    Raises:
        NotificationError: naming which gate failed.
    """
    consent = await session.scalar(
        select(WhatsAppConsent).where(
            tenant_filter(WhatsAppConsent, tenant_id), WhatsAppConsent.user_id == user_id
        )
    )
    if consent is None or not consent.is_active:
        raise NotificationError(
            "This recipient has not opted in to WhatsApp. Sending without recorded "
            "consent is a policy violation and risks the business number being blocked."
        )

    if template.whatsapp_template_id is None:
        raise NotificationError(
            f"The WhatsApp notification template for '{template.event_key}' names no "
            "Meta template. A business-initiated message needs an approved one."
        )

    meta_template = await session.get(WhatsAppTemplate, template.whatsapp_template_id)
    if meta_template is None:
        raise NotificationError(
            f"The Meta template referenced by '{template.event_key}' no longer exists"
        )
    if not meta_template.is_sendable:
        raise NotificationError(
            f"The Meta template '{meta_template.meta_name}' is "
            f"{meta_template.status.value}, not approved. It cannot be sent."
            + (
                f" Reason: {meta_template.rejection_reason}"
                if meta_template.rejection_reason
                else ""
            )
        )

    return consent, meta_template


# ── Preferences ─────────────────────────────────────────────────────────────


async def _preferences_for(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, event_key: str
) -> dict[Channel, DigestMode]:
    """This user's channels for this event, falling back to the shipped default."""
    rows = (
        await session.scalars(
            select(NotificationPreference).where(
                tenant_filter(NotificationPreference, tenant_id),
                NotificationPreference.user_id == user_id,
                NotificationPreference.event_key == event_key,
            )
        )
    ).all()

    if rows:
        return {
            row.channel: (row.digest_mode if row.enabled else DigestMode.OFF)
            for row in rows
        }

    return dict(
        DEFAULT_PREFERENCES.get(event_key, {Channel.IN_APP: DigestMode.IMMEDIATE})
    )


# ── Adapters ────────────────────────────────────────────────────────────────

# One instance per channel. Replaced wholesale in tests, which is why the
# registry is a plain dict rather than a container framework.
_ADAPTERS: dict[Channel, ChannelAdapter] = {
    Channel.EMAIL: ConsoleEmailAdapter(),
    Channel.IN_APP: InAppAdapter(),
    Channel.PUSH: NullPushAdapter(),
}


def register_adapter(channel: Channel, adapter: ChannelAdapter) -> None:
    _ADAPTERS[channel] = adapter


def get_adapter(channel: Channel) -> ChannelAdapter | None:
    return _ADAPTERS.get(channel)


#: The email adapter shipped in the registry. While nothing has replaced it (a
#: test registers its own), each send looks for a real relay: the tenant's own,
#: then the platform's, and only then falls back to logging.
_DEFAULT_EMAIL_ADAPTER = _ADAPTERS[Channel.EMAIL]


async def email_adapter_for(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[ChannelAdapter, str]:
    """The adapter email for this tenant goes through, and which relay it is."""
    row = await session.scalar(
        select(TenantSmtpSettings).where(
            tenant_filter(TenantSmtpSettings, tenant_id),
            TenantSmtpSettings.enabled.is_(True),
        )
    )
    if row is not None:
        return (
            SmtpEmailAdapter(
                host=row.host,
                port=row.port,
                username=row.username,
                password=decrypt_secret(row.password_encrypted, tenant_id),
                use_starttls=row.use_starttls,
                use_ssl=row.use_ssl,
                from_address=row.from_address,
                from_name=row.from_name,
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            ),
            "tenant_smtp",
        )
    if settings.SMTP_HOST and settings.SMTP_FROM:
        return (
            SmtpEmailAdapter(
                host=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME,
                password=settings.SMTP_PASSWORD,
                use_starttls=settings.SMTP_USE_STARTTLS,
                use_ssl=settings.SMTP_USE_SSL,
                from_address=settings.SMTP_FROM,
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            ),
            "platform_smtp",
        )
    return _DEFAULT_EMAIL_ADAPTER, "console"


# ── The entry point ─────────────────────────────────────────────────────────


async def notify(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_key: str,
    recipient_user_ids: list[uuid.UUID],
    payload: dict[str, Any] | None = None,
    locale: str = "en-IN",
    commit: bool = True,
) -> DispatchOutcome:
    """Raise a notification event.

    The only function other modules call. They pass an event key and
    recipients; everything about delivery is resolved here.
    """
    payload = payload or {}
    outcome = DispatchOutcome()
    policy = await _policy(session, tenant_id)
    urgent = event_key in URGENT_EVENT_KEYS
    now = datetime.now(UTC)

    for user_id in recipient_user_ids:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            outcome.suppressed += 1
            continue

        preferences = await _preferences_for(session, tenant_id, user_id, event_key)

        for channel, mode in preferences.items():
            if mode is DigestMode.OFF:
                outcome.suppressed += 1
                continue

            template = await _template_for(session, tenant_id, event_key, channel, locale)
            if template is None:
                outcome.suppressed += 1
                outcome.errors.append(
                    f"No {channel.value} template for '{event_key}'; nothing sent on that "
                    "channel."
                )
                continue

            if mode is DigestMode.DAILY_DIGEST and not urgent:
                await _append_to_digest(
                    session, tenant_id, user, event_key, template, payload, policy
                )
                outcome.digested += 1
                continue

            # ── WhatsApp gates, before anything is queued ───────────────────
            meta_template: WhatsAppTemplate | None = None
            if channel is Channel.WHATSAPP:
                try:
                    _, meta_template = await _whatsapp_gate(
                        session, tenant_id, user_id, template
                    )
                except NotificationError as exc:
                    outcome.suppressed += 1
                    outcome.errors.append(str(exc))
                    continue

            notification = Notification(
                tenant_id=tenant_id,
                recipient_user_id=user_id,
                event_key=event_key,
                channel=channel,
                payload=payload,
                rendered_subject=render(template.subject or "", payload) or None,
                rendered_body=render(template.body, payload),
                is_urgent=urgent,
                status=NotificationStatus.QUEUED,
            )

            # ── Quiet hours and rate cap, which urgent bypasses ─────────────
            if not urgent:
                if _in_quiet_hours(policy, now):
                    notification.status = NotificationStatus.DEFERRED
                    notification.next_attempt_at = _next_window_open(policy, now)
                    session.add(notification)
                    outcome.deferred += 1
                    continue
                if await _over_rate_limit(session, policy, tenant_id, user_id, channel):
                    notification.status = NotificationStatus.DEFERRED
                    notification.next_attempt_at = now + timedelta(hours=1)
                    notification.failure_reason = (
                        "Held: this recipient has reached their hourly limit on this "
                        "channel."
                    )
                    session.add(notification)
                    outcome.deferred += 1
                    continue

            session.add(notification)
            await session.flush()
            outcome.queued += 1

            # Each channel dispatches on its own. A failure here is recorded on
            # the row and does not touch the next channel or the next
            # recipient.
            delivered = await _dispatch_one(
                session, notification, user, policy, meta_template
            )
            if delivered:
                outcome.sent += 1
            else:
                outcome.failed += 1

    if commit:
        await session.commit()
    return outcome


async def _dispatch_one(
    session: AsyncSession,
    notification: Notification,
    user: User,
    policy: NotificationPolicy,
    meta_template: WhatsAppTemplate | None = None,
) -> bool:
    """Send one notification. Returns whether it went.

    Never raises. A failing channel must not stop the others, so every failure
    lands on the row instead of propagating.
    """
    adapter = get_adapter(notification.channel)
    if notification.channel is Channel.EMAIL and adapter is _DEFAULT_EMAIL_ADAPTER:
        try:
            adapter, _relay = await email_adapter_for(session, notification.tenant_id)
        except Exception as exc:
            notification.status = NotificationStatus.FAILED
            notification.failure_reason = f"The configured mail relay could not be used: {exc}"
            return False
    if adapter is None:
        notification.status = NotificationStatus.FAILED
        notification.failure_reason = (
            f"No adapter is configured for {notification.channel.value}."
        )
        return False

    phone = None
    if notification.channel is Channel.WHATSAPP:
        consent = await session.scalar(
            select(WhatsAppConsent).where(
                tenant_filter(WhatsAppConsent, notification.tenant_id),
                WhatsAppConsent.user_id == user.id,
            )
        )
        phone = consent.phone_e164 if consent else None

    message = OutboundMessage(
        recipient_name=user.full_name,
        recipient_email=user.email,
        recipient_phone=phone or user.phone,
        subject=notification.rendered_subject,
        body=notification.rendered_body,
        event_key=notification.event_key,
        whatsapp_template_name=meta_template.meta_name if meta_template else None,
        whatsapp_parameters=[
            str(notification.payload.get(name, ""))
            for name in (meta_template.parameter_names if meta_template else [])
        ],
    )

    notification.attempt_count += 1

    try:
        result = await adapter.send(message)
    except PermanentFailure as exc:
        # No retry: the address is wrong, or they unsubscribed. Retrying an
        # unsubscribe is worse than not sending.
        notification.status = NotificationStatus.FAILED
        notification.failure_reason = f"Permanent: {exc}"
        notification.next_attempt_at = None
        return False
    except TransientFailure as exc:
        if notification.attempt_count >= policy.max_attempts:
            notification.status = NotificationStatus.DEAD_LETTER
            notification.failure_reason = (
                f"Gave up after {notification.attempt_count} attempts: {exc}"
            )
            notification.next_attempt_at = None
        else:
            notification.status = NotificationStatus.QUEUED
            notification.failure_reason = f"Transient: {exc}"
            notification.next_attempt_at = datetime.now(UTC) + _backoff(
                notification.attempt_count
            )
        return False
    except Exception as exc:
        notification.status = NotificationStatus.FAILED
        notification.failure_reason = f"Adapter error: {exc}"
        return False

    notification.status = (
        NotificationStatus.DELIVERED if result.delivered else NotificationStatus.SENT
    )
    notification.provider_message_id = result.provider_message_id
    notification.sent_at = datetime.now(UTC)
    if result.delivered:
        notification.delivered_at = notification.sent_at
    notification.failure_reason = None
    notification.next_attempt_at = None
    return True


def _backoff(attempt: int) -> timedelta:
    """Exponential backoff, capped.

    1, 2, 4, 8, 16 minutes. Capped so a long outage does not push the next
    attempt days out, by which point the notification is not worth sending.
    """
    return timedelta(minutes=min(2 ** (attempt - 1), 60))


# ── Digests ─────────────────────────────────────────────────────────────────


async def _append_to_digest(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user: User,
    event_key: str,
    template: NotificationTemplate,
    payload: dict[str, Any],
    policy: NotificationPolicy,
) -> None:
    """Add an item to the user's pending digest, creating it if needed."""
    scheduled = _next_digest_time(policy)

    digest = await session.scalar(
        select(NotificationDigest).where(
            tenant_filter(NotificationDigest, tenant_id),
            NotificationDigest.recipient_user_id == user.id,
            NotificationDigest.status == NotificationStatus.QUEUED,
        )
    )
    if digest is None:
        digest = NotificationDigest(
            tenant_id=tenant_id,
            recipient_user_id=user.id,
            scheduled_for=scheduled,
            items=[],
        )
        session.add(digest)
        await session.flush()

    digest.items = [
        *digest.items,
        {
            "event_key": event_key,
            "subject": render(template.subject or "", payload),
            "body": render(template.body, payload),
            "at": datetime.now(UTC).isoformat(),
        },
    ]


def _next_digest_time(policy: NotificationPolicy) -> datetime:
    """The next digest slot, in the tenant's timezone.

    Sent at the end of quiet hours: it lands as someone starts their day
    rather than in the middle of the night.
    """
    try:
        zone = ZoneInfo(policy.timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Kolkata")

    local = datetime.now(UTC).astimezone(zone)
    slot = local.replace(
        hour=policy.quiet_end.hour, minute=policy.quiet_end.minute, second=0, microsecond=0
    )
    if slot <= local:
        slot += timedelta(days=1)
    return slot.astimezone(UTC)


async def send_digests(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Send every digest whose time has come. Returns how many went.

    An empty digest sends nothing — a daily email saying "nothing happened"
    trains people to ignore the channel.
    """
    due = (
        await session.scalars(
            select(NotificationDigest).where(
                tenant_filter(NotificationDigest, tenant_id),
                NotificationDigest.status == NotificationStatus.QUEUED,
                NotificationDigest.scheduled_for <= datetime.now(UTC),
            )
        )
    ).all()

    policy = await _policy(session, tenant_id)
    sent = 0

    for digest in due:
        if not digest.items:
            digest.status = NotificationStatus.SENT
            digest.sent_at = datetime.now(UTC)
            continue

        user = await session.get(User, digest.recipient_user_id)
        if user is None or not user.is_active:
            digest.status = NotificationStatus.FAILED
            continue

        lines = "\n".join(f"- {item['subject'] or item['body']}" for item in digest.items)
        notification = Notification(
            tenant_id=tenant_id,
            recipient_user_id=user.id,
            event_key="system.digest.daily",
            channel=Channel.EMAIL,
            payload={"count": len(digest.items)},
            rendered_subject=f"Your AskMira digest — {len(digest.items)} update(s)",
            rendered_body=f"Here is what happened:\n\n{lines}",
            status=NotificationStatus.QUEUED,
        )
        session.add(notification)
        await session.flush()

        if await _dispatch_one(session, notification, user, policy):
            sent += 1

        digest.status = NotificationStatus.SENT
        digest.sent_at = datetime.now(UTC)

    await session.commit()
    return sent


# ── Retry / deferral worker ─────────────────────────────────────────────────


async def process_due_notifications(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Send whatever is due: retries and deferred messages.

    The scheduled worker. Runs frequently and does nothing when nothing is due.
    """
    now = datetime.now(UTC)
    policy = await _policy(session, tenant_id)

    due = (
        await session.scalars(
            select(Notification).where(
                tenant_filter(Notification, tenant_id),
                Notification.status.in_(
                    [NotificationStatus.QUEUED, NotificationStatus.DEFERRED]
                ),
                Notification.next_attempt_at.isnot(None),
                Notification.next_attempt_at <= now,
            )
        )
    ).all()

    processed = 0
    for notification in due:
        user = await session.get(User, notification.recipient_user_id)
        if user is None:
            notification.status = NotificationStatus.FAILED
            notification.failure_reason = "The recipient no longer exists"
            continue

        meta_template = None
        if notification.channel is Channel.WHATSAPP:
            template = await _template_for(
                session, tenant_id, notification.event_key, Channel.WHATSAPP
            )
            if template and template.whatsapp_template_id:
                meta_template = await session.get(
                    WhatsAppTemplate, template.whatsapp_template_id
                )

        if await _dispatch_one(session, notification, user, policy, meta_template):
            processed += 1

    await session.commit()
    return processed


async def seed_default_templates(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[NotificationTemplate]:
    """Copy the built-in templates into the tenant, so HR can edit its own copy.

    Not required for delivery: `notify` falls back to `DEFAULT_TEMPLATES` for
    any event the tenant has no template for.
    """
    created: list[NotificationTemplate] = []
    for event_key, (subject, body) in DEFAULT_TEMPLATES.items():
        for channel in (Channel.EMAIL, Channel.IN_APP):
            existing = await session.scalar(
                select(NotificationTemplate).where(
                    tenant_filter(NotificationTemplate, tenant_id),
                    NotificationTemplate.event_key == event_key,
                    NotificationTemplate.channel == channel,
                )
            )
            if existing is not None:
                continue
            template = NotificationTemplate(
                tenant_id=tenant_id,
                event_key=event_key,
                channel=channel,
                subject=subject,
                body=body,
            )
            session.add(template)
            created.append(template)

    await session.commit()
    return created
