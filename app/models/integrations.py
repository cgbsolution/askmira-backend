"""Customer API keys and outbound webhooks.

**API keys** let a customer's own systems read their workspace: the directory,
leave and attendance. A key is shown once; only its SHA-256 is kept. Each key
carries the scopes it may read, and is revoked rather than deleted so the
audit trail still names it.

**Webhooks** tell a customer's endpoint when something changes. Events come
from the audit log — insert-only and written in the same transaction as the
change — so an event is sent only for a change that was committed, and every
change that was committed can be sent. Each delivery is a row: retried with
backoff, signed with the endpoint's secret, and kept as a record of what was
sent where.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.types import EncryptedText


class ApiKey(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: SHA-256 of the key. Never the key.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    key_hint: Mapped[str] = mapped_column(String(32), nullable=False)
    #: e.g. ["employees:read", "leave:read", "attendance:read"].
    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_api_keys_created_by"),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookFormat(str, enum.Enum):
    #: The event as JSON, signed.
    ASKMIRA = "askmira"
    #: {"text": "..."} for a Slack or Microsoft Teams incoming-webhook URL.
    CHAT = "chat"


class WebhookEndpoint(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "webhook_endpoints"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: Signs each delivery (HMAC-SHA256). Encrypted at rest; shown once.
    secret_encrypted: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    #: Entity types to send, e.g. ["employee", "leave_request"]; empty sends all.
    entity_types: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    format: Mapped[WebhookFormat] = mapped_column(
        Enum(WebhookFormat, name="webhook_format"), default=WebhookFormat.ASKMIRA, nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Audit entries up to here have been turned into deliveries.
    cursor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_webhook_endpoints_created_by"),
        nullable=True,
    )


class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class WebhookDelivery(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "audit_log_id", name="uq_webhook_delivery_event"),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
    )

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "webhook_endpoints.id", ondelete="CASCADE", name="fk_webhook_deliveries_endpoint"
        ),
        nullable=False,
        index=True,
    )
    audit_log_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="webhook_delivery_status"),
        default=DeliveryStatus.PENDING,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
