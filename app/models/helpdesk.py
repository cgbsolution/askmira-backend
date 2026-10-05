"""HR helpdesk: categories, tickets, the conversation on them and its files.

An employee raises a ticket — a salary query, an address-proof letter, a lost
ID card, a grievance — and HR (or whoever owns that category) works it to a
resolution while both sides talk on the ticket instead of over email.

Three decisions in this schema are worth stating up front.

**Ticket numbers are a per-tenant counter, not a max()+1.** `HD-00042` is what
people quote on the phone. Allocation takes a row lock on `helpdesk_sequences`
so two tickets raised in the same instant cannot share a number. Gaps (a
rolled-back create) are fine; duplicates are not.

**Confidentiality is copied onto the ticket.** A grievance raised while its
category was confidential stays confidential if an admin later edits the
category. Who may read a ticket is decided from the ticket, not from a setting
that can move under it.

**Internal notes live in the same thread, flagged.** An agent's note to another
agent is a comment with `is_internal`. The requester never receives one — not
in the thread, not as an attachment, not in a notification.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class HelpdeskRequestKind(str, enum.Enum):
    """What sort of request a category collects. Drives nothing but labelling
    and reporting today; the category name is what people see."""

    GENERAL = "general"
    LETTER = "letter"
    ID_CARD = "id_card"
    ASSET = "asset"
    IT = "it"
    PAYROLL = "payroll"
    POLICY = "policy"
    GRIEVANCE = "grievance"


class HelpdeskPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class HelpdeskTicketStatus(str, enum.Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    # The agent has asked the employee something and is waiting for the answer.
    WAITING_ON_EMPLOYEE = "waiting_on_employee"
    RESOLVED = "resolved"
    # Final. Reached by rating, by an agent, or automatically seven days after
    # resolution.
    CLOSED = "closed"
    # Withdrawn by the employee before it was resolved.
    CANCELLED = "cancelled"


class HelpdeskCategory(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A kind of request, who owns it and how fast it should be answered."""

    __tablename__ = "helpdesk_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_helpdesk_category_name"),
        CheckConstraint("sla_hours > 0", name="ck_helpdesk_category_sla_positive"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The person who works this category's tickets. None means HR's shared
    # queue: every HR role (admin, sub-admin) sees them.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Calendar hours from raising to the resolution being due.
    sla_hours: Mapped[int] = mapped_column(Integer, default=48, nullable=False)
    # Grievance / POSH-adjacent: visible only to the requester, the category
    # owner and admins. A sub-admin does not see these.
    is_confidential: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    request_kind: Mapped[HelpdeskRequestKind] = mapped_column(
        Enum(HelpdeskRequestKind, name="helpdesk_request_kind"),
        default=HelpdeskRequestKind.GENERAL,
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class HelpdeskSequence(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The counter behind `HD-00001`. One row per tenant, locked to allocate."""

    __tablename__ = "helpdesk_sequences"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_helpdesk_sequence_tenant"),)

    last_value: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class HelpdeskTicket(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One request from one employee."""

    __tablename__ = "helpdesk_tickets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "ticket_seq", name="uq_helpdesk_ticket_seq"),
        CheckConstraint(
            "satisfaction_rating IS NULL OR (satisfaction_rating BETWEEN 1 AND 5)",
            name="ck_helpdesk_ticket_rating_range",
        ),
        Index("ix_helpdesk_tickets_tenant_status", "tenant_id", "status"),
    )

    ticket_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_number: Mapped[str] = mapped_column(String(16), nullable=False)

    # Whose request it is. HR may raise one on somebody's behalf; then
    # `raised_by_user_id` is HR's login and this is still the employee.
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    raised_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("helpdesk_categories.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[HelpdeskPriority] = mapped_column(
        Enum(HelpdeskPriority, name="helpdesk_priority"),
        default=HelpdeskPriority.MEDIUM,
        nullable=False,
    )
    status: Mapped[HelpdeskTicketStatus] = mapped_column(
        Enum(HelpdeskTicketStatus, name="helpdesk_ticket_status"),
        default=HelpdeskTicketStatus.OPEN,
        nullable=False,
    )
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Copied from the category at creation — see the module docstring.
    is_confidential: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sla_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    first_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Bumped by every comment and status change; the queue sorts on it.
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    satisfaction_rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    satisfaction_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HelpdeskComment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One message on a ticket: a reply, an internal note, or a system line
    ("Assigned to Rahul") the thread shows so nobody has to ask what happened."""

    __tablename__ = "helpdesk_comments"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("helpdesk_tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Agent-to-agent. Never shown to the requester.
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Written by the service on a status change or an assignment, not typed.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class HelpdeskAttachment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A file on a ticket, stored through the shared storage service.

    `comment_id` is None for the file attached when the ticket was raised. A
    file on an internal note is as internal as the note.
    """

    __tablename__ = "helpdesk_attachments"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_helpdesk_attachment_storage_key"),
        CheckConstraint("size_bytes > 0", name="ck_helpdesk_attachment_size_positive"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("helpdesk_tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    comment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("helpdesk_comments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Generated by the storage service; the client's filename never becomes a key.
    storage_key: Mapped[str] = mapped_column(String(200), nullable=False)
    file_name: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
