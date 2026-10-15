"""Notification and communication engine (slice 05).

Every module assumes notifications exist — approvals, shoutouts, payroll
publication, attendance regularisation — and none of them should implement
sending. This is the shared engine, built once so no module invents its own.

The contract for callers is one function: `notify(event_key, recipients,
payload)`. A module raising an event must not know or care whether the
recipient gets email, push or WhatsApp. That is the recipient's preference,
not the caller's business.

Invariants:

1. **No module outside `services/notifications/` talks to a provider SDK.**
   Enforced by a source-scanning test.
2. **WhatsApp requires recorded consent and an approved template**, and both
   are checked at enqueue rather than at send. Failing at send means the
   failure is invisible until someone reads a log, and sending without consent
   is a fast route to a blocked business number.
3. **A failing channel never blocks another.** WhatsApp being down must not
   stop the email going.
4. **A failed notification is queryable**, not only logged. Dead-letter
   visibility is the difference between "nobody got the payroll alert" being
   noticed in an hour or in a month.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, time

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.types import EncryptedText


class Channel(str, enum.Enum):
    EMAIL = "email"
    PUSH = "push"
    IN_APP = "in_app"
    WHATSAPP = "whatsapp"


class DigestMode(str, enum.Enum):
    IMMEDIATE = "immediate"
    DAILY_DIGEST = "daily_digest"
    OFF = "off"


class NotificationStatus(str, enum.Enum):
    QUEUED = "queued"
    # Held for quiet hours or a rate cap; will send later.
    DEFERRED = "deferred"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    READ = "read"
    # Rolled into a digest instead of sending on its own.
    DIGESTED = "digested"
    # Retries exhausted. Findable, which is the whole point.
    DEAD_LETTER = "dead_letter"


class WhatsAppTemplateStatus(str, enum.Enum):
    """Meta's approval states.

    A business-initiated WhatsApp message must use a template Meta has
    approved. Anything but APPROVED fails at enqueue.
    """

    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAUSED = "paused"
    DISABLED = "disabled"


class NotificationTemplate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A rendered message body per event, channel and locale.

    A null-ish tenant is not possible here because every model is tenant
    scoped; system defaults are seeded per tenant instead, which keeps the
    isolation rule uniform and lets a tenant edit its own copy.
    """

    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "event_key", "channel", "locale", "version",
            name="uq_notification_template_version",
        ),
    )

    # Dotted, e.g. "leave.approval.requested". The stable identifier a module
    # raises; everything else about delivery is configuration.
    event_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    channel: Mapped[Channel] = mapped_column(
        Enum(Channel, name="notification_channel"), nullable=False
    )
    locale: Mapped[str] = mapped_column(String(16), default="en-IN", nullable=False)

    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # WhatsApp business-initiated messages must name an approved Meta template.
    whatsapp_template_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("whatsapp_templates.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<NotificationTemplate {self.event_key}/{self.channel.value}>"


class WhatsAppTemplate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A message template registered with Meta.

    Modelled explicitly rather than assumed, because the approval state is the
    difference between a message arriving and a silent failure — and because a
    rejected template needs to be visible to whoever has to fix it.
    """

    __tablename__ = "whatsapp_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "meta_name", "locale", name="uq_whatsapp_template"),
    )

    # The exact name registered with Meta. Case and underscores matter.
    meta_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    locale: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="UTILITY", nullable=False)

    status: Mapped[WhatsAppTemplateStatus] = mapped_column(
        Enum(WhatsAppTemplateStatus, name="whatsapp_template_status"),
        default=WhatsAppTemplateStatus.DRAFT,
        nullable=False,
        index=True,
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ordered placeholder names, so a caller's payload can be mapped onto
    # Meta's positional {{1}}, {{2}} parameters.
    parameter_names: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_sendable(self) -> bool:
        return self.status is WhatsAppTemplateStatus.APPROVED


class WhatsAppConsent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Recorded opt-in for WhatsApp, per user.

    Sending without this is both a policy violation and the fastest way to get
    a business number blocked by Meta. The timestamp and source are kept
    because "we have consent" is a claim that has to be evidenced.
    """

    __tablename__ = "whatsapp_consents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_whatsapp_consent_user"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)

    opted_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opted_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # onboarding_form | ess_settings | whatsapp_reply | hr_bulk_import
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)

    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The last inbound message from this user. Meta's 24-hour customer-service
    # window is measured from it: inside the window a free-form reply is
    # allowed, outside it only an approved template.
    last_inbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_active(self) -> bool:
        return self.opted_in and self.opted_out_at is None


class NotificationPreference(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One user's choice for one event on one channel."""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "event_key", "channel", name="uq_notification_preference"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    channel: Mapped[Channel] = mapped_column(
        Enum(Channel, name="notification_channel"), nullable=False
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    digest_mode: Mapped[DigestMode] = mapped_column(
        Enum(DigestMode, name="digest_mode"), default=DigestMode.IMMEDIATE, nullable=False
    )


class Notification(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One message to one recipient on one channel."""

    __tablename__ = "notifications"

    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    event_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    channel: Mapped[Channel] = mapped_column(
        Enum(Channel, name="notification_channel"), nullable=False, index=True
    )

    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    rendered_subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    rendered_body: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status"),
        default=NotificationStatus.QUEUED,
        nullable=False,
        index=True,
    )
    # Urgent bypasses quiet hours and the per-user rate cap. Payroll failures
    # and security alerts; not shoutouts.
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    provider_message_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # When a retry or a deferral should next be attempted.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    def __repr__(self) -> str:
        return f"<Notification {self.event_key}/{self.channel.value} {self.status.value}>"


class NotificationDigest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Accumulated items for one user's daily digest.

    An empty digest sends nothing — a daily email saying "nothing happened"
    trains people to ignore the channel.
    """

    __tablename__ = "notification_digests"

    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # In the recipient's own timezone, which is why it is stored rather than
    # derived at send time.
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status"),
        default=NotificationStatus.QUEUED,
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationPolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Per-tenant quiet hours and rate limits.

    One row per tenant. Quiet hours stop a 2am approval reminder; the rate cap
    stops a bulk operation sending one person two hundred messages.
    """

    __tablename__ = "notification_policies"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_notification_policy_tenant"),)

    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    quiet_start: Mapped[time] = mapped_column(Time, default=time(21, 0), nullable=False)
    quiet_end: Mapped[time] = mapped_column(Time, default=time(8, 0), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)

    # Per user, per hour, per channel.
    rate_limit_per_hour: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)


class TenantSmtpSettings(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A tenant's own SMTP relay.

    The password is a credential for somebody else's mail server, so it is
    stored encrypted with the tenant's derived key (`app.core.crypto`) and no
    read schema carries it — the admin screen is told only whether one is set.
    """

    __tablename__ = "tenant_smtp_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_smtp_settings_tenant"),)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=587, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    use_starttls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    from_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class TenantWhatsAppSettings(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """This workspace's own WhatsApp Business account (Meta Cloud API).

    The phone number id and access token come from the customer's Meta business
    manager. With these saved and enabled, WhatsApp messages to staff who opted
    in, and to candidates who opted in, go through the customer's own number.
    The token is encrypted with the tenant key and never returned.
    """

    __tablename__ = "tenant_whatsapp_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_whatsapp_settings_tenant"),)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    phone_number_id: Mapped[str] = mapped_column(String(64), nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class PushDevice(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A phone the AskMira app is signed in on, and the push token it registered.

    One token belongs to one person at a time: a phone that changes hands
    re-registers, and the token moves to whoever signed in last. A token Expo
    reports as no longer registered is revoked and never pushed to again.
    """

    __tablename__ = "push_devices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "push_token_hash", name="uq_push_device_token"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    #: SHA-256 of the token, to find a phone again; the token itself is encrypted.
    push_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    push_token_encrypted: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), default="expo", nullable=False)
    last_registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
