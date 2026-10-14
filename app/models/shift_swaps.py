"""Shift swaps: two people trade their shifts for one day.

The requester asks a named colleague; the colleague accepts; the requester's
line manager (or HR) approves. Only then does anything change, and what changes
is two one-day shift assignments — the schedule each person is judged against
on that date. Nothing is rewritten in place, so the record shows the regular
shift, the swap, and who agreed to it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ShiftSwapStatus(str, enum.Enum):
    #: Waiting for the colleague to agree.
    AWAITING_PARTNER = "awaiting_partner"
    #: Agreed between them; waiting for the requester's manager or HR.
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    #: The colleague said no.
    DECLINED = "declined"
    #: The manager or HR said no.
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ShiftSwapRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "shift_swap_requests"
    __table_args__ = (
        CheckConstraint(
            "requester_employee_id <> partner_employee_id", name="ck_shift_swap_two_people"
        ),
        Index("ix_shift_swap_date", "tenant_id", "swap_date"),
    )

    requester_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_shift_swap_requester"),
        nullable=False,
        index=True,
    )
    partner_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_shift_swap_partner"),
        nullable=False,
        index=True,
    )
    swap_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: The shifts each was due to work that day, as they stood when asked.
    requester_shift_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("shifts.id", ondelete="RESTRICT", name="fk_shift_swap_requester_shift"),
        nullable=False,
    )
    partner_shift_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("shifts.id", ondelete="RESTRICT", name="fk_shift_swap_partner_shift"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ShiftSwapStatus] = mapped_column(
        Enum(ShiftSwapStatus, name="shift_swap_status"),
        default=ShiftSwapStatus.AWAITING_PARTNER,
        nullable=False,
        index=True,
    )
    partner_responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_shift_swap_decided_by"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
