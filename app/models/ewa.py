"""Earned wage access: drawing part of this month's pay before payday.

A person may ask for part of what they have already earned this month — their
monthly gross, times the days paid so far over the days in the month — up to
the share the workspace allows. HR approves it; the money goes out by bank
transfer; and the same month's payroll recovers it as a deduction, so nothing
is lent beyond what has been earned. It is off until the workspace switches
it on (`tenant.settings["ewa"]`).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class EwaStatus(str, enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISBURSED = "disbursed"
    CANCELLED = "cancelled"


class EwaRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "ewa_requests"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_ewa_amount_positive"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_ewa_requests_employee_id"),
        nullable=False,
        index=True,
    )
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    #: What had been earned, and what could be drawn, when it was asked for.
    earned_to_date: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    available: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[EwaStatus] = mapped_column(
        Enum(EwaStatus, name="ewa_status"), default=EwaStatus.REQUESTED, nullable=False, index=True
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_ewa_requests_requested_by"),
        nullable=True,
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_ewa_requests_decided_by"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    disbursed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payroll_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "payroll_adjustments.id", ondelete="SET NULL", name="fk_ewa_requests_adjustment"
        ),
        nullable=True,
    )
