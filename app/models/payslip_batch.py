"""Payslips handed out in bulk: downloaded as one file, or emailed to each person.

HR picks people and a span of months (the last 1, 3, 6 or 12). Rendering a
payslip PDF takes a fraction of a second, so a whole workforce over a year is
minutes of work: it runs in the background, and the row is how the screen
follows it, how a restarted server picks it up, and what is left to read
afterwards. Each person's outcome is kept in `results` as it happens, so a
batch resumed after a restart never emails anybody twice.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["PayslipBatch"]


class PayslipBatch(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "payslip_batches"
    __table_args__ = (Index("ix_payslip_batches_tenant_created", "tenant_id", "created_at"),)

    #: `download` (one ZIP, or one PDF when there is only one) or `email`.
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 1, 3, 6 or 12: how many months back from `period_end`.
    months: Mapped[int] = mapped_column(Integer, nullable=False)
    #: First day of the first and of the last month covered.
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    #: The people asked for, as ids, in the order they were picked.
    employee_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: Email only: `work` or `personal` (falling back to the work address).
    send_to: Mapped[str | None] = mapped_column(String(16), nullable=True)

    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: queued | running | completed | failed
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    total_people: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_payslips: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: People worked through so far, and how each went.
    processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: [{"employee_id", "employee_code", "name", "status": sent|added|skipped|failed,
    #:   "payslips": n, "email": "...", "detail": "..."}]
    results: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: Why the whole batch stopped, when it did.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Download only: the prepared file, kept for a day and then removed.
    file_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Touched after every person; a running batch silent for long was cut off
    #: by a restart and is picked up again.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
