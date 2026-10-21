"""Messages to candidates: an outbox, because a candidate has no login.

Every other notification in AskMira goes to a user. A candidate is not one, so
the hiring emails — application received, interview booked, the offer is
ready, and the decision when it is no — are queued here against the
candidate and delivered by a job through the tenant's own mail relay.

**Links that act as the candidate are never stored readable.** A portal or
self-schedule link is a bearer token: whoever holds it can accept an offer in
the candidate's name. It is kept encrypted until the message is sent, then
erased; the body anybody can read in the log says only that a link was sent.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["CandidateMessage", "CandidateMessageStatus"]


class CandidateMessageStatus(str, enum.Enum):
    QUEUED = "queued"
    SENT = "sent"
    #: Not retried: the address was refused, or the relay could not be used.
    FAILED = "failed"
    #: Retried until the attempts ran out.
    DEAD_LETTER = "dead_letter"
    #: Withdrawn before it went: cancelled by a recruiter, or no longer true
    #: (a rejection for a candidate who has since been moved back).
    CANCELLED = "cancelled"


class CandidateMessage(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One email to one candidate, from queued to sent."""

    __tablename__ = "candidate_messages"
    __table_args__ = (
        Index("ix_candidate_messages_candidate", "tenant_id", "candidate_id", "created_at"),
        Index("ix_candidate_messages_due", "status", "next_attempt_at"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: email | whatsapp. A WhatsApp message goes to the phone in `to_address`.
    channel: Mapped[str] = mapped_column(
        String(16), default="email", server_default="email", nullable=False
    )
    to_address: Mapped[str] = mapped_column(String(320), nullable=False)
    #: WhatsApp only: the approved Meta template and its parameters, in order.
    whatsapp_template_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    whatsapp_parameters: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    #: As the log shows it: a secret link appears as a placeholder, never itself.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: The secret link, encrypted with the tenant key; erased once sent.
    secret_link_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[CandidateMessageStatus] = mapped_column(
        Enum(CandidateMessageStatus, name="candidate_message_status"),
        default=CandidateMessageStatus.QUEUED,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Who wrote it; None for one AskMira sent on a hiring event.
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
