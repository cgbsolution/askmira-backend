"""Gratuity and leave encashment (slice 08).

Gratuity is a statutory obligation under the Payment of Gratuity Act; leave
encashment is a contractual one. Both are computed at exit, both feed Full and
Final settlement, and both carry tax treatment that differs by circumstance.

Invariants:

1. **No ceiling, divisor or eligibility period is hardcoded.** The statutory
   gratuity exemption has been raised several times and will be again. Every
   number lives in `GratuityConfig` or an SME-approved rule set.
2. **The "4 years 240 days" question is configuration, not a decision this
   codebase makes.** Whether a fifth year of continuous service is completed by
   240 days of the fifth year is contested ground — High Courts differ, and a
   tenant's own counsel may take a view. Baking in one interpretation makes the
   product wrong for half its customers.
3. **The taxable excess above the exemption flows into TDS.** A gratuity payout
   that skips the tax computation under-deducts, and the employee discovers it
   at filing.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
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


class ServiceRounding(str, enum.Enum):
    """How a part-year of service is treated.

    `NEAREST_YEAR_SIX_MONTH_RULE` is the conventional Indian reading — more
    than six months rounds up — but it is an option rather than an assumption.
    """

    NEAREST_YEAR_SIX_MONTH_RULE = "nearest_year_six_month_rule"
    ALWAYS_ROUND_DOWN = "always_round_down"
    ALWAYS_ROUND_UP = "always_round_up"
    EXACT_FRACTION = "exact_fraction"


class ExitReason(str, enum.Enum):
    """Why employment ended.

    Death and disablement waive the eligibility period entirely, which is why
    the reason is a first-class field rather than free text.
    """

    RESIGNATION = "resignation"
    RETIREMENT = "retirement"
    TERMINATION = "termination"
    DEATH = "death"
    DISABLEMENT = "disablement"
    END_OF_CONTRACT = "end_of_contract"


class EncashmentStatus(str, enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"
    CANCELLED = "cancelled"


class GratuityConfig(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A tenant's gratuity rules.

    One row per tenant. Everything here is a number the Compliance SME sets and
    a Budget or a judgment can change.
    """

    __tablename__ = "gratuity_configs"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_gratuity_config_tenant"),)

    # Completed years of continuous service before gratuity is payable.
    eligibility_years: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── The contested one ───────────────────────────────────────────────────
    # Whether a partial final year counts as complete once this many days are
    # served. Null disables the treatment entirely. High Courts have differed
    # on whether 240 days of a fifth year completes it, so this is a setting a
    # tenant's counsel decides, not a position this codebase takes.
    eligibility_days_in_final_year: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    # Component codes that constitute "wages" — conventionally BASIC + DA, but
    # the mapping is SME-signed rather than assumed.
    formula_basis: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, nullable=False
    )

    # The divisor in (wages / divisor) x days_per_year x service_years.
    # 26 for the covered-establishment formula; different elsewhere.
    days_per_year_divisor: Mapped[int] = mapped_column(Integer, nullable=False)
    days_per_year: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)

    # The statutory exemption ceiling. Raised repeatedly; never a constant.
    max_exempt_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    # A contractual ceiling on the payout itself, where the tenant applies one.
    max_payable_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    service_rounding: Mapped[ServiceRounding] = mapped_column(
        Enum(ServiceRounding, name="service_rounding"),
        default=ServiceRounding.NEAREST_YEAR_SIX_MONTH_RULE,
        nullable=False,
    )
    # Months beyond which unpaid leave breaks continuous service.
    unpaid_leave_break_threshold_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    include_notice_period_in_service: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # Exit reasons that waive the eligibility period.
    eligibility_waived_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)),
        default=lambda: [ExitReason.DEATH.value, ExitReason.DISABLEMENT.value],
        nullable=False,
    )

    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None


class GratuityAccrual(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee's accrued gratuity as of a date.

    Recomputed monthly so finance can see the liability at any point rather
    than discovering it when someone resigns. The accrual is not a payment —
    it is what the company would owe if the employee left that day.
    """

    __tablename__ = "gratuity_accruals"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "as_of_date", name="uq_gratuity_accrual_date"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    service_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # After the configured rounding rule. What the formula multiplies by.
    service_years: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    is_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    monthly_wages: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    eligible_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    # The full working, so a figure on a liability report is explainable.
    working: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<GratuityAccrual {self.employee_id} {self.as_of_date} {self.eligible_amount}>"


class EncashmentPolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Which leave may be encashed, when, and how much.

    Per leave type, because earned leave is usually encashable and casual leave
    usually is not.
    """

    __tablename__ = "encashment_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "leave_type_code", name="uq_encashment_policy_type"),
    )

    leave_type_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    encashable_on_exit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    encashable_in_service: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    max_days_per_year: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    # A lifetime or per-event cap on encashable days.
    cap_days: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    # Minimum balance that must remain after an in-service encashment.
    min_balance_after_encashment: Mapped[float] = mapped_column(
        Numeric(6, 2), default=0, nullable=False
    )

    # Component codes that make up the per-day rate.
    formula_basis: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, nullable=False
    )
    # Divisor turning monthly wages into a daily rate — 26 or 30 by convention,
    # and the convention differs by employer.
    days_per_month_divisor: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Tax treatment ───────────────────────────────────────────────────────
    # Differs between government and non-government employees, and between
    # in-service and retirement encashment. Configured rather than inferred:
    # inferring it from an employee attribute would be wrong for the cases
    # that matter.
    exempt_on_retirement: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retirement_exemption_ceiling: Mapped[float | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    in_service_fully_taxable: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class EncashmentRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An in-service or exit encashment.

    An exit encashment is created by Full and Final rather than requested; the
    same record carries both so the tax treatment and the payroll component
    have one shape.
    """

    __tablename__ = "encashment_requests"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    leave_type_code: Mapped[str] = mapped_column(String(32), nullable=False)
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)

    days_requested: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    # After the policy cap and the available balance are applied.
    days_approved: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)

    daily_rate: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    gross_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    exempt_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    taxable_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    is_exit_encashment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[EncashmentStatus] = mapped_column(
        Enum(EncashmentStatus, name="encashment_status"),
        default=EncashmentStatus.REQUESTED,
        nullable=False,
        index=True,
    )
    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The payroll run that paid it, so an encashment cannot be paid twice.
    paid_in_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
    )
    working: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class GratuityPayout(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A gratuity actually paid at exit.

    Separate from the accrual: an accrual is an estimate of a liability, a
    payout is a transaction. Conflating them makes the liability report wrong
    the moment anyone leaves.
    """

    __tablename__ = "gratuity_payouts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", name="uq_gratuity_payout_employee"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    exit_date: Mapped[date] = mapped_column(Date, nullable=False)
    exit_reason: Mapped[ExitReason] = mapped_column(
        Enum(ExitReason, name="exit_reason"), nullable=False
    )

    service_days: Mapped[int] = mapped_column(Integer, nullable=False)
    service_years: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    monthly_wages: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    gross_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    exempt_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    # The excess above the exemption. Flows into the TDS computation.
    taxable_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    eligibility_waived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    working: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    config_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("gratuity_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    paid_in_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
    )
