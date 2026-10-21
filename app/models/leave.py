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
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
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


class CompOffStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


#: Which half of the day a half-day request covers. A first-half and a
#: second-half request on the same date are two halves, not an overlap.
HALF_DAY_SESSIONS = ("first_half", "second_half")


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
    # The one type an approved comp-off claim is credited to. At most one per
    # tenant; marking another type moves the flag.
    is_comp_off: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LeavePolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Accrual and carry-forward rules, scoped to a population.

    A null `location_id` or `grade` means "applies to everyone" — the most
    specific matching policy wins at assignment time.
    """

    __tablename__ = "leave_policies"
    __table_args__ = (
        CheckConstraint(
            "applies_to_gender IS NULL OR applies_to_gender IN ('female', 'male')",
            name="ck_leave_policies_applies_to_gender",
        ),
    )

    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leave_types.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=True
    )
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # female | male | null (everyone). Excludes only somebody whose recorded
    # gender is the other one: an employee with no gender on file, or who has
    # not disclosed it, is never shut out of maternity leave by a data gap.
    applies_to_gender: Mapped[str | None] = mapped_column(String(8), nullable=True)

    annual_quota_days: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    # monthly | quarterly | annual | on_joining
    accrual_frequency: Mapped[str] = mapped_column(String(16), default="monthly", nullable=False)
    max_carry_forward_days: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    max_consecutive_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_notice_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    allows_negative_balance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Sandwich rule: weekly offs and holidays that fall between two days of
    # the same request are charged as leave (Friday to Monday costs four days).
    sandwich_weekends_holidays: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

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
    # Net of HR credits and debits, comp-off credits and expiries, and the
    # year-end carry-out and lapse. Every movement behind it is a row in
    # `leave_balance_adjustments`; this is the running total for fast reads.
    adjusted: Mapped[float] = mapped_column(
        Numeric(6, 2), default=0, server_default=text("0"), nullable=False
    )


class LeaveBalanceAdjustment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One movement on a balance that is not accrual or leave taken.

    Insert-only. `days` is the signed effect on what is available: an HR credit
    is +2, an encashment of 5 days is -5. `kind` says which ledger column moved
    (see `app.services.leave.ledger.apply_to_row`), so the balance row can always
    be rebuilt from these rows plus accrual and approved leave.
    """

    __tablename__ = "leave_balance_adjustments"
    __table_args__ = (
        Index("ix_leave_adjustments_slot", "tenant_id", "employee_id", "leave_type_id", "year"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leave_types.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    # opening | credit | debit | comp_off | comp_off_expiry | encashment |
    # carry_forward_in | carry_forward_out | lapse
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    days: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    effective_on: Mapped[date] = mapped_column(Date, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # What caused it, when something did: a comp-off claim, an encashment.
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    # Comp-off credits only: the day they lapse, and when the expiry run did it.
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LeaveRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint(
            "half_day_session IS NULL OR half_day_session IN ('first_half', 'second_half')",
            name="ck_leave_requests_half_day_session",
        ),
    )

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
    # first_half | second_half. Null on a full-day request, and on half days
    # raised before sessions existed (those overlap any other half day).
    half_day_session: Mapped[str | None] = mapped_column(String(16), nullable=True)

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


class OptionalHolidaySelection(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An employee opting in to an optional (restricted) holiday.

    Counted against the tenant's yearly quota. Once chosen, the holiday resolver
    treats the date as a holiday for this person only, so attendance marks it
    HOLIDAY and leave does not charge it.
    """

    __tablename__ = "optional_holiday_selections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "holiday_id", name="uq_optional_holiday_selection"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    holiday_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("holidays.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    selected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class CompOffRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A claim for compensatory time off for working a holiday or weekly off.

    Approved by the line manager or HR, never the claimant. Approval credits the
    tenant's comp-off leave type through a `LeaveBalanceAdjustment`, which
    lapses `expires_on` if it has not been used.
    """

    __tablename__ = "comp_off_requests"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    worked_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # 0.5 or 1.
    days: Mapped[float] = mapped_column(Numeric(3, 1), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[CompOffStatus] = mapped_column(
        Enum(CompOffStatus, name="comp_off_status"),
        default=CompOffStatus.PENDING,
        nullable=False,
        index=True,
    )
    # Snapshotted at submission, like a leave request's chain. Null means HR.
    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approver_name: Mapped[str] = mapped_column(String(255), nullable=False)

    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set on approval: where the credit went and when it lapses.
    leave_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("leave_types.id", ondelete="SET NULL"), nullable=True
    )
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
