"""A fixed-monthly consultant's monthly fee, from a date.

Like hourly rates, a fee is never updated in place: a raise is a new row from
its day. A payout prices each day worked at the fee in force that day divided
by the days in that day's month.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Numeric, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ConsultantMonthlyFee(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What a fixed-monthly consultant is paid a month, from a date. Never updated in place."""

    __tablename__ = "consultant_monthly_fees"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "effective_from", name="uq_consultant_monthly_fee_from"
        ),
        CheckConstraint("amount > 0", name="ck_consultant_monthly_fee_positive"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_consultant_monthly_fees_employee"),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
