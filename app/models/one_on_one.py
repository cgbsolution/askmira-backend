"""1:1 meetings between a manager and somebody who reports to them.

Three decisions worth stating.

**Two participants, and nobody else reads it.** A 1:1 is the conversation a
report has with their manager about how things are going, and it only works if
it stays between them. HR does not read talking points; the manager's manager
does not either. The rows are visible to the two people in the meeting.

**Private notes are a separate table, keyed by author.** "Shared" and "private"
in one text column with a flag is a leak waiting for a serialiser bug. A note in
`one_on_one_private_notes` is returned to its author and to nobody else.

**Action items outlive the meeting.** An open action item from last month's 1:1
is still open, and the next meeting between the same two people shows it. The
item keeps its meeting id; "carried over" is a query, not a copy.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class OneOnOneStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class OneOnOneItemKind(str, enum.Enum):
    #: Something either side wants to talk about. Agenda items are these.
    TALKING_POINT = "talking_point"
    #: Something somebody agreed to do, with an owner and usually a date.
    ACTION_ITEM = "action_item"


class OneOnOne(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One scheduled conversation between a manager and a report."""

    __tablename__ = "one_on_ones"
    __table_args__ = (
        CheckConstraint(
            "manager_employee_id <> report_employee_id", name="ck_one_on_one_two_people"
        ),
        CheckConstraint(
            "duration_minutes > 0 AND duration_minutes <= 480",
            name="ck_one_on_one_duration_range",
        ),
        Index("ix_one_on_ones_manager", "tenant_id", "manager_employee_id", "scheduled_on"),
        Index("ix_one_on_ones_report", "tenant_id", "report_employee_id", "scheduled_on"),
    )

    manager_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    report_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_on: Mapped[date] = mapped_column(Date, nullable=False)
    #: Local time of day, as the two people agreed it. Optional: plenty of
    #: teams fix the day and find the slot on the day.
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    #: A recurring 1:1: every this many weeks. When one is held or cancelled,
    #: the next is scheduled. Null: a one-off.
    repeat_every_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[OneOnOneStatus] = mapped_column(
        Enum(OneOnOneStatus, name="one_on_one_status"),
        default=OneOnOneStatus.SCHEDULED,
        nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: What both people agreed the meeting covered. Written when it is marked
    #: complete; visible to both.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class OneOnOneItem(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A shared talking point or an action item. Both participants see these."""

    __tablename__ = "one_on_one_items"
    __table_args__ = (
        Index("ix_one_on_one_items_meeting", "tenant_id", "one_on_one_id", "display_order"),
        Index("ix_one_on_one_items_owner", "tenant_id", "owner_employee_id", "done_at"),
    )

    one_on_one_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("one_on_ones.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[OneOnOneItemKind] = mapped_column(
        Enum(OneOnOneItemKind, name="one_on_one_item_kind"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Who does it. Action items only; one of the two participants.
    owner_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class OneOnOneNote(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One participant's private notes on one meeting. Read by its author only."""

    __tablename__ = "one_on_one_private_notes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "one_on_one_id", "author_user_id", name="uq_one_on_one_note_author"
        ),
    )

    one_on_one_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("one_on_ones.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
