"""Consultant hours entered by finance for a period, replacing timesheet weeks as the pay input.

An entry is one consultant's billable hours for one period (inclusive dates, at
most 31 days), optionally day by day, with the customer's approved sheet
attached as proof. Someone other than whoever entered it approves it.

A consultant cannot have two live entries whose periods share even one day,
across projects: the database refuses it with an exclusion constraint. A
rejected entry frees its period.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class HoursEntryStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


def _user_fk(name: str) -> Mapped[uuid.UUID | None]:
    return mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name=name),
        nullable=True,
    )


class ConsultantHoursEntry(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "consultant_hours_entries"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="ck_consultant_hours_period_order"),
        CheckConstraint(
            "period_end - period_start <= 30", name="ck_consultant_hours_period_length"
        ),
        CheckConstraint("total_billable_hours > 0", name="ck_consultant_hours_total_positive"),
        # No two live entries for one consultant may share a day. The enum is stored by
        # name, so a rejected entry is 'REJECTED'. Needs the btree_gist extension.
        ExcludeConstraint(
            ("tenant_id", "="),
            ("employee_id", "="),
            (text("daterange(period_start, period_end, '[]')"), "&&"),
            where=text("status <> 'REJECTED'"),
            using="gist",
            name="ex_consultant_hours_no_overlap",
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_consultant_hours_employee"),
        nullable=False,
        index=True,
    )
    #: First and last day worked, both included.
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL", name="fk_consultant_hours_project"),
        nullable=True,
    )
    #: The hours that are paid.
    total_billable_hours: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    #: Recorded for the customer's sheet, never paid.
    non_billable_hours: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    #: Optional day grid: [{"date": "2026-10-01", "hours": 8, "status": "present"}, ...].
    days: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    customer_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[HoursEntryStatus] = mapped_column(
        Enum(HoursEntryStatus, name="consultant_hours_entry_status"),
        default=HoursEntryStatus.DRAFT,
        nullable=False,
        index=True,
    )
    entered_by_user_id: Mapped[uuid.UUID | None] = _user_fk("fk_consultant_hours_entered_by")
    #: Whoever last changed the figures. Like the enterer, they cannot approve.
    last_edited_by_user_id: Mapped[uuid.UUID | None] = _user_fk("fk_consultant_hours_edited_by")
    #: Every user who entered, edited, attached a file to, submitted or reopened this entry
    #: in its current cycle (user ids as text). None of them can approve it.
    maker_user_ids: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_user_id: Mapped[uuid.UUID | None] = _user_fk("fk_consultant_hours_decided_by")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConsultantHoursAttachment(Base, UUIDMixin, TenantMixin):
    __tablename__ = "consultant_hours_attachments"

    entry_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "consultant_hours_entries.id",
            ondelete="CASCADE",
            name="fk_consultant_hours_attachment_entry",
        ),
        nullable=False,
        index=True,
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = _user_fk("fk_consultant_hours_attachment_by")
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
