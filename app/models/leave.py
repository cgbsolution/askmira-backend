"""Leave and time-off (FRD 5.1).

Policies vary by location and grade, so a leave type is a definition and a
policy is the assignment of that definition to a population.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class LeaveRequestStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class LeaveType(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A kind of leave: Earned, Casual, Sick, Maternity, Comp-off, and so on."""

    __tablename__ = "leave_types"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_leave_types_tenant_code"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_paid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allows_half_day: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires_document: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Statutory types (maternity under the Maternity Benefit Act, for example)
    # cannot be edited away by a tenant admin.
    is_statutory: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LeavePolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Accrual and carry-forward rules, scoped to a population.

    A null `location_id` or `grade` means "applies to everyone" — the most
    specific matching policy wins at assignment time.
    """

    __tablename__ = "leave_policies"

    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leave_types.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=True
    )
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True)

    annual_quota_days: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    # monthly | quarterly | annual | on_joining
    accrual_frequency: Mapped[str] = mapped_column(String(16), default="monthly", nullable=False)
    max_carry_forward_days: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    max_consecutive_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_notice_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    allows_negative_balance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LeaveBalance(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee's balance for one leave type in one leave year."""

    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "leave_type_id", "year", name="uq_leave_balance_slot"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leave_types.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)

    opening_balance: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    accrued: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    used: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    encashed: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    carried_forward: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)


class LeaveRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "leave_requests"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leave_types.id", ondelete="RESTRICT"), nullable=False
    )

    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Fractional to carry half-days.
    total_days: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    is_half_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    status: Mapped[LeaveRequestStatus] = mapped_column(
        Enum(LeaveRequestStatus, name="leave_request_status"),
        default=LeaveRequestStatus.PENDING,
        nullable=False,
        index=True,
    )
    # Snapshot of the approval chain and each step's outcome. Kept on the
    # request so a later change to the org chart cannot rewrite who approved
    # what.
    approval_chain: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Holiday(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Holiday calendar entry.

    Scoped by state because Indian holiday calendars differ by state, and a
    location's `state_code` is what selects the right list (FRD 5.1).
    """

    __tablename__ = "holidays"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # Null state_code = applies nationally across the tenant.
    state_code: Mapped[str | None] = mapped_column(String(4), nullable=True, index=True)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=True
    )

    # Optional holidays the employee chooses from a floating allowance.
    is_optional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
