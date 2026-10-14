"""Pay that is not in a salary structure: ad-hoc adjustments and overtime.

Two kinds of thing reach a payslip that no structure can describe:

- **Adjustments** — an incentive, a one-off bonus, a canteen recovery, a court
  attachment. Somebody in payroll keys them in; somebody else approves them
  (maker-checker, like every other money-moving record here); a run pays them.
  A one-time adjustment is paid once. A monthly one is paid every period in its
  window until its end or its total cap — a court decree for a fixed sum stops
  when the sum is recovered, not when somebody remembers to cancel it.

- **Overtime** — minutes worked beyond the shift, taken from locked attendance,
  priced by the tenant's overtime policy and approved by the line manager or
  HR. The policy holds every number (the multiplier, the wage components that
  form the ordinary rate, the divisors, the caps). None is written into code:
  the Factories Act's double rate and a state's quarterly limit are the
  tenant's configuration, the same way PF rates are a rule set.

Every payment is recorded against the run that made it
(`payroll_adjustment_applications`, `overtime_entries.paid_in_run_id`,
`shift_allowance_payments`), so nothing is paid twice and every payslip line
says which record it came from.
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = [
    "AdjustmentCategory",
    "AdjustmentKind",
    "AdjustmentRecurrence",
    "AdjustmentStatus",
    "OvertimeEntry",
    "OvertimePolicy",
    "OvertimeStatus",
    "PayrollAdjustment",
    "PayrollAdjustmentApplication",
    "ShiftAllowancePayment",
]


# ── Ad-hoc adjustments ──────────────────────────────────────────────────────


class AdjustmentKind(str, enum.Enum):
    EARNING = "earning"
    DEDUCTION = "deduction"


class AdjustmentCategory(str, enum.Enum):
    # Earnings
    INCENTIVE = "incentive"
    BONUS = "bonus"
    COMMISSION = "commission"
    OVERTIME = "overtime"
    SHIFT_ALLOWANCE = "shift_allowance"
    ARREAR = "arrear"
    OTHER_EARNING = "other_earning"
    # Deductions
    COURT_ATTACHMENT = "court_attachment"
    CANTEEN = "canteen"
    TRANSPORT = "transport"
    DAMAGE_RECOVERY = "damage_recovery"
    OTHER_DEDUCTION = "other_deduction"


class AdjustmentRecurrence(str, enum.Enum):
    ONE_TIME = "one_time"
    MONTHLY = "monthly"


class AdjustmentStatus(str, enum.Enum):
    # Keyed in, waiting for a second person.
    PENDING = "pending"
    # Approved and due in its window.
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    # Paid (one-time), or its cap was reached.
    COMPLETED = "completed"


class PayrollAdjustment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One earning or deduction outside the salary structure."""

    __tablename__ = "payroll_adjustments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payroll_adjustment_amount_positive"),
        CheckConstraint(
            "total_cap IS NULL OR total_cap > 0", name="ck_payroll_adjustment_cap_positive"
        ),
        CheckConstraint(
            "start_month BETWEEN 1 AND 12", name="ck_payroll_adjustment_start_month"
        ),
        CheckConstraint(
            "end_month IS NULL OR end_month BETWEEN 1 AND 12",
            name="ck_payroll_adjustment_end_month",
        ),
        Index("ix_payroll_adjustment_due", "tenant_id", "status", "start_year", "start_month"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    kind: Mapped[AdjustmentKind] = mapped_column(
        Enum(AdjustmentKind, name="payroll_adjustment_kind"), nullable=False
    )
    category: Mapped[AdjustmentCategory] = mapped_column(
        Enum(AdjustmentCategory, name="payroll_adjustment_category"), nullable=False
    )
    #: What the payslip line says, e.g. "Q2 sales incentive".
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Per occurrence: the whole amount for a one-time, each month's for a monthly.
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    #: Earnings only. A deduction is never taxable income.
    taxable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Earnings only: whether this is a payment *for work* — overtime, a shift
    #: allowance — that counts toward the ESI contribution and the PT slab. It
    #: never counts toward PF wages, which are basic and DA.
    counts_as_wages: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Deductions only: taken only when it fits inside what is left of net pay,
    #: and carried to the next run otherwise — never driving net pay negative.
    within_net_pay: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    recurrence: Mapped[AdjustmentRecurrence] = mapped_column(
        Enum(AdjustmentRecurrence, name="payroll_adjustment_recurrence"), nullable=False
    )
    start_year: Mapped[int] = mapped_column(Integer, nullable=False)
    start_month: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Monthly only. Null runs until the cap or until cancelled.
    end_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The most this adjustment will ever pay or recover in total.
    total_cap: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    amount_applied: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    #: A court order number, an incentive scheme reference.
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[AdjustmentStatus] = mapped_column(
        Enum(AdjustmentStatus, name="payroll_adjustment_status"),
        default=AdjustmentStatus.PENDING,
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PayrollAdjustmentApplication(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One payment of an adjustment, by one approved run, for one period."""

    __tablename__ = "payroll_adjustment_applications"
    __table_args__ = (
        UniqueConstraint(
            "adjustment_id", "period_year", "period_month",
            name="uq_adjustment_application_period",
        ),
    )

    adjustment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("payroll_adjustments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="RESTRICT"), nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)


# ── Overtime ────────────────────────────────────────────────────────────────


class OvertimePolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """How overtime is counted and priced, for a location or the whole tenant.

    The ordinary hourly rate is the monthly amount of `wage_component_codes`
    divided by `days_divisor` and `hours_per_day`; overtime is paid at
    `rate_multiplier` times that. Every one of those is the tenant's input.
    """

    __tablename__ = "overtime_policies"
    __table_args__ = (
        CheckConstraint("rate_multiplier > 0", name="ck_overtime_multiplier_positive"),
        CheckConstraint("days_divisor > 0", name="ck_overtime_days_divisor_positive"),
        CheckConstraint("hours_per_day > 0", name="ck_overtime_hours_positive"),
        CheckConstraint("minimum_minutes >= 0", name="ck_overtime_minimum_nonneg"),
        CheckConstraint("rounding_minutes >= 0", name="ck_overtime_rounding_nonneg"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Null applies to every location without a policy of its own.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=True,
        index=True,
    )
    rate_multiplier: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    wage_component_codes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    days_divisor: Mapped[int] = mapped_column(Integer, nullable=False)
    hours_per_day: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)
    #: A day's overtime below this is not counted at all.
    minimum_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Counted in whole blocks of this many minutes, rounded down. 0 = exact.
    rounding_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_cap_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The statutory quarterly limit where the state sets one.
    quarterly_cap_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Whether hours worked on a weekly off or a holiday count as overtime.
    count_rest_days: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class OvertimeStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"


class OvertimeEntry(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee-day of overtime, from attendance to payslip."""

    __tablename__ = "overtime_entries"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "attendance_date", name="uq_overtime_entry_day"
        ),
        Index("ix_overtime_entry_status", "tenant_id", "status", "attendance_date"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    attendance_date: Mapped[date] = mapped_column(Date, nullable=False)
    policy_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("overtime_policies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: Beyond the shift, as attendance recorded it.
    recorded_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: After the policy's minimum, rounding and daily cap.
    eligible_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: What the approver accepted, after the quarterly cap.
    approved_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_rest_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hourly_rate: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    rate_multiplier: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    #: How the amount was reached, in words, for the payslip and the reviewer.
    basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[OvertimeStatus] = mapped_column(
        Enum(OvertimeStatus, name="overtime_status"),
        default=OvertimeStatus.PENDING,
        nullable=False,
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_in_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
    )


class ShiftAllowancePayment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A shift allowance paid for one employee's period, by one run."""

    __tablename__ = "shift_allowance_payments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "shift_id", "period_year", "period_month",
            name="uq_shift_allowance_period",
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    shift_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("shifts.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="RESTRICT"), nullable=False
    )
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    days: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
