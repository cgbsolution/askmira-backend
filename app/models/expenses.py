"""Expense policy, claims, travel requests and advances.

═══════════════════════════════════════════════════════════════════════════════
THE VERDICT IS STORED ON THE CLAIM, NOT RECOMPUTED
═══════════════════════════════════════════════════════════════════════════════

`expense_claims` records the policy verdict, the limit it was measured against
and the rule that produced it, at the moment of capture. It is not recomputed on
read, and for the same reason `PayrollRuleSet` is never updated in place: the
limit changes in April, and a March claim that was inside policy must not start
reading as a breach because somebody tightened a number afterwards. The approver
who let it through would then be looking at a violation they never saw.

═══════════════════════════════════════════════════════════════════════════════
A RATE CHANGE IS A NEW ROW
═══════════════════════════════════════════════════════════════════════════════

`mileage_rates`, `per_diem_rates` and `exchange_rate_quotes` are all
effective-dated and resolved by the claim's own date. An invariant checks no
service updates one in place.

═══════════════════════════════════════════════════════════════════════════════
BOTH CURRENCY AMOUNTS ARE COLUMNS
═══════════════════════════════════════════════════════════════════════════════

`original_amount` and `original_currency` sit beside `amount`. Keeping only the
converted figure loses the number on the employee's card statement — the one
they will query — and keeping only the original leaves the ledger unable to say
what it paid.

═══════════════════════════════════════════════════════════════════════════════
AN ADVANCE BELONGS TO A TRIP
═══════════════════════════════════════════════════════════════════════════════

`travel_advances.travel_request_id` is not nullable. An advance with no trip is
an advance that can only ever be settled by netting it against whatever claims
happen to be lying around, which is the thing `services.expenses.advances`
refuses to do.
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
    LargeBinary,
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


class CityTierEnum(str, enum.Enum):
    """Indian travel policy is almost universally tier-based."""

    A = "a"
    B = "b"
    C = "c"
    INTERNATIONAL = "international"


class LimitTypeEnum(str, enum.Enum):
    PER_CLAIM = "per_claim"
    PER_DAY = "per_day"
    PER_MONTH = "per_month"
    PER_TRIP = "per_trip"


class ViolationActionEnum(str, enum.Enum):
    BLOCK = "block"
    FLAG = "flag"
    REQUIRE_JUSTIFICATION = "require_justification"


class PolicyVerdictEnum(str, enum.Enum):
    """Mirrors `services.expenses.policy.VerdictKind`.

    Stored so the claim carries what the employee was told at capture, rather
    than what today's policy would say.
    """

    WITHIN_POLICY = "within_policy"
    NEEDS_JUSTIFICATION = "needs_justification"
    FLAGGED = "flagged"
    BLOCKED = "blocked"
    RECEIPT_REQUIRED = "receipt_required"
    PRE_APPROVAL_REQUIRED = "pre_approval_required"
    NO_RULE = "no_rule"


class ExpenseClaimStatusEnum(str, enum.Enum):
    """Where a claim is. Submission routes it to the line manager first.

    ``SUBMITTED`` is the finance stage: a claim sits there once its line
    manager has approved it, or straight after submission when the employee
    has no line manager with a login to decide it. Existing rows keep meaning
    what they meant, which is why the finance stage kept the old name.
    """

    #: Captured and evaluated. A blocked claim never leaves this state.
    DRAFT = "draft"
    #: Waiting for the line manager.
    PENDING_MANAGER = "pending_manager"
    #: Waiting for finance.
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    #: Paid — in a payroll run (`reimbursed_in_run_id`), or out of a travel
    #: advance it was settled against (`advance_settlement_id`).
    REIMBURSED = "reimbursed"
    CANCELLED = "cancelled"


class TravelRequestStatusEnum(str, enum.Enum):
    DRAFT = "draft"
    #: Waiting for the line manager. ``SUBMITTED`` is the finance stage.
    PENDING_MANAGER = "pending_manager"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class AdvanceStatusEnum(str, enum.Enum):
    ISSUED = "issued"
    SETTLED = "settled"
    CANCELLED = "cancelled"


class SettlementOutcomeEnum(str, enum.Enum):
    SQUARE = "square"
    REFUND_DUE_TO_EMPLOYEE = "refund_due_to_employee"
    RECOVERY_DUE_FROM_EMPLOYEE = "recovery_due_from_employee"


class ConversionConventionEnum(str, enum.Enum):
    """Both are in use, so it is a tenant setting rather than an assumption."""

    TRANSACTION_DATE = "transaction_date"
    SETTLEMENT_DATE = "settlement_date"


class ClaimKindEnum(str, enum.Enum):
    """What produced the amount, which decides what evidence means.

    A per-diem needs no receipt by definition and a mileage claim's evidence is
    a distance, not a bill. Storing the kind is what lets the receipt rule be
    applied to the claims it actually applies to.
    """

    RECEIPTED = "receipted"
    MILEAGE = "mileage"
    PER_DIEM = "per_diem"


# ══════════════════════════════════════════════════════════════════════════════
# Policy
# ══════════════════════════════════════════════════════════════════════════════


class ExpensePolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One named policy, applying to some grades and locations, from a date."""

    __tablename__ = "expense_policies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "name", "effective_from", name="uq_expense_policy_version"
        ),
        Index("ix_expense_policies_effective", "tenant_id", "effective_from"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Empty means every grade. Unconfigured and none-of-them have to be
    #: different states, or a policy saved before grades were set applies to
    #: nobody and silently governs nothing.
    applicable_grades: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    #: Location ids. Empty means every location, for the same reason.
    applicable_locations: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: A superseded policy is kept, not deleted: claims evaluated under it
    #: reference its rules.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ExpensePolicyRule(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One limit, for one category, in one city tier."""

    __tablename__ = "expense_policy_rules"
    __table_args__ = (
        UniqueConstraint(
            "policy_id",
            "category_key",
            "city_tier",
            name="uq_policy_rule_category_tier",
        ),
        CheckConstraint("limit_amount >= 0", name="ck_policy_rule_limit_nonneg"),
        CheckConstraint(
            "receipt_threshold >= 0", name="ck_policy_rule_threshold_nonneg"
        ),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("expense_policies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_key: Mapped[str] = mapped_column(String(80), nullable=False)
    city_tier: Mapped[CityTierEnum] = mapped_column(
        Enum(CityTierEnum, name="expense_city_tier"), nullable=False
    )
    limit_type: Mapped[LimitTypeEnum] = mapped_column(
        Enum(LimitTypeEnum, name="expense_limit_type"), nullable=False
    )
    limit_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    violation_action: Mapped[ViolationActionEnum] = mapped_column(
        Enum(ViolationActionEnum, name="expense_violation_action"), nullable=False
    )
    requires_receipt: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    #: Below this, no receipt. A ₹40 auto fare with a receipt demand is a policy
    #: that produces fake receipts.
    receipt_threshold: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    requires_pre_approval: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: A per-diem rule never demands a receipt, whatever `requires_receipt`
    #: says. The flag exists so a policy author cannot accidentally require one.
    is_per_diem: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class LocationCityTier(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Which tier a location sits in. Configurable, because the lists differ.

    There is no statutory tier list for travel policy — every company has its
    own, and a hard-coded one would be wrong for most of them the first time
    somebody opened a Coimbatore office.
    """

    __tablename__ = "location_city_tiers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "location_id", name="uq_city_tier_per_location"),
    )

    location_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    city_tier: Mapped[CityTierEnum] = mapped_column(
        Enum(CityTierEnum, name="expense_city_tier"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ══════════════════════════════════════════════════════════════════════════════
# Rates
# ══════════════════════════════════════════════════════════════════════════════


class MileageRate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What one kilometre is worth in one vehicle, from one date."""

    __tablename__ = "mileage_rates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "vehicle_type",
            "effective_from",
            name="uq_mileage_rate_version",
        ),
        CheckConstraint("rate_per_km > 0", name="ck_mileage_rate_positive"),
        CheckConstraint(
            "tolerance_percent >= 0", name="ck_mileage_tolerance_nonneg"
        ),
    )

    vehicle_type: Mapped[str] = mapped_column(String(40), nullable=False)
    rate_per_km: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: How far beyond the declared route before the claim is flagged. A route is
    #: a straight line and a road is not.
    tolerance_percent: Mapped[float] = mapped_column(
        Numeric(6, 2), default=15, nullable=False
    )


class PerDiemRate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A fixed daily amount for one grade in one tier, from one date."""

    __tablename__ = "per_diem_rates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "city_tier",
            "grade",
            "effective_from",
            name="uq_per_diem_rate_version",
        ),
        CheckConstraint("amount > 0", name="ck_per_diem_amount_positive"),
    )

    city_tier: Mapped[CityTierEnum] = mapped_column(
        Enum(CityTierEnum, name="expense_city_tier"), nullable=False
    )
    grade: Mapped[str] = mapped_column(String(40), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    includes_meals: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    includes_accommodation: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )


class ExchangeRateQuote(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One currency's rate on one date, and where it came from.

    `source` is stored because "converted at 94.20" is not an answer an auditor
    accepts. It has to say whose 94.20.
    """

    __tablename__ = "exchange_rate_quotes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "from_currency",
            "to_currency",
            "quote_date",
            name="uq_exchange_rate_per_day",
        ),
        CheckConstraint("rate > 0", name="ck_exchange_rate_positive"),
        CheckConstraint(
            "from_currency <> to_currency", name="ck_exchange_rate_differs"
        ),
    )

    from_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    to_currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    rate: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    quote_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(120), nullable=False)


class TenantExpenseSettings(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The handful of expense choices that are the tenant's to make."""

    __tablename__ = "tenant_expense_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_expense_settings_per_tenant"),
        CheckConstraint(
            "advance_block_after_days >= 1", name="ck_advance_block_positive"
        ),
    )

    conversion_convention: Mapped[ConversionConventionEnum] = mapped_column(
        Enum(ConversionConventionEnum, name="expense_conversion_convention"),
        default=ConversionConventionEnum.TRANSACTION_DATE,
        nullable=False,
    )
    rate_source: Mapped[str] = mapped_column(
        String(120), default="RBI reference rate", nullable=False
    )
    reporting_currency: Mapped[str] = mapped_column(
        String(3), default="INR", nullable=False
    )
    #: How long an advance may sit unsettled before it blocks a new one.
    advance_block_after_days: Mapped[int] = mapped_column(
        Integer, default=45, nullable=False
    )


# ══════════════════════════════════════════════════════════════════════════════
# Travel and advances
# ══════════════════════════════════════════════════════════════════════════════


class TravelRequest(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A trip, its estimate, and the advance asked for against it."""

    __tablename__ = "travel_requests"
    __table_args__ = (
        CheckConstraint("to_date >= from_date", name="ck_travel_dates_ordered"),
        CheckConstraint("estimated_cost >= 0", name="ck_travel_estimate_nonneg"),
        CheckConstraint(
            "advance_requested >= 0", name="ck_travel_advance_nonneg"
        ),
        Index("ix_travel_requests_employee", "tenant_id", "employee_id", "from_date"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: Free-form destination list; each entry carries a city and a tier so the
    #: policy engine has a tier without a second lookup at claim time.
    destinations: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    advance_requested: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    status: Mapped[TravelRequestStatusEnum] = mapped_column(
        Enum(TravelRequestStatusEnum, name="travel_request_status"),
        default=TravelRequestStatusEnum.DRAFT,
        nullable=False,
    )
    #: Append-only list of {actor, decision, at, note}. The approval history is
    #: evidence for a disbursement and is never rewritten in place.
    approvals: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TravelAdvance(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Money issued against a trip, and what became of it.

    `travel_request_id` is not nullable. An advance with no trip can only be
    settled by netting it against whatever claims are lying around, which is
    exactly what `services.expenses.advances.settle` refuses to do.
    """

    __tablename__ = "travel_advances"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_advance_amount_positive"),
        CheckConstraint("claimed_against >= 0", name="ck_advance_claimed_nonneg"),
        CheckConstraint("recovered_amount >= 0", name="ck_advance_recovered_nonneg"),
        CheckConstraint("refunded_amount >= 0", name="ck_advance_refunded_nonneg"),
        Index("ix_travel_advances_open", "tenant_id", "employee_id", "status"),
    )

    travel_request_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("travel_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Set when the advance goes out in a payment file (slice 11).
    disbursement_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("disbursement_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[AdvanceStatusEnum] = mapped_column(
        Enum(AdvanceStatusEnum, name="travel_advance_status"),
        default=AdvanceStatusEnum.ISSUED,
        nullable=False,
    )
    claimed_against: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    settled_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    recovered_amount: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    refunded_amount: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    issued_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AdvanceSettlement(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The settlement record, kept even when the outcome is `square`.

    A settlement that produced nothing is still the evidence that the trip was
    reconciled, and without a row there is no way to tell it apart from a trip
    nobody ever looked at.
    """

    __tablename__ = "advance_settlements"
    __table_args__ = (
        UniqueConstraint("advance_id", name="uq_settlement_per_advance"),
        CheckConstraint("claims_total >= 0", name="ck_settlement_claims_nonneg"),
        CheckConstraint("recovery_amount >= 0", name="ck_settlement_recovery_nonneg"),
        CheckConstraint("refund_amount >= 0", name="ck_settlement_refund_nonneg"),
    )

    advance_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("travel_advances.id", ondelete="CASCADE"),
        nullable=False,
    )
    travel_request_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("travel_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    advance_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    claims_total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    outcome: Mapped[SettlementOutcomeEnum] = mapped_column(
        Enum(SettlementOutcomeEnum, name="advance_settlement_outcome"), nullable=False
    )
    recovery_amount: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    refund_amount: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    #: The recovery is handed to payroll (slice 14) rather than netted here.
    #: Null until payroll has picked it up, which is what makes an unrecovered
    #: balance visible instead of assumed.
    payroll_recovery_ref: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    #: The same for a balance owed *to* the employee: paid in the next payroll
    #: run, and null until a run approved that payment.
    payroll_refund_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    settled_on: Mapped[date] = mapped_column(Date, nullable=False)
    settled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# Claims
# ══════════════════════════════════════════════════════════════════════════════


class ExpenseClaim(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One claim, with the verdict it was given at capture.

    The verdict columns are written once, at capture, and not recomputed. A
    limit tightened in April must not turn a March claim into a breach the
    approver never saw.
    """

    __tablename__ = "expense_claims"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_claim_amount_positive"),
        CheckConstraint("original_amount > 0", name="ck_claim_original_positive"),
        Index("ix_expense_claims_employee", "tenant_id", "employee_id", "incurred_on"),
        Index("ix_expense_claims_trip", "tenant_id", "travel_request_id"),
        Index("ix_expense_claims_category", "tenant_id", "category_key"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Denormalised at capture so violation rates can be grouped without a join
    #: through a reporting line that may since have changed. The rate for
    #: "Sales, March" must not move when somebody transfers in June.
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    travel_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("travel_requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: The report this claim is a line of. Null for a stand-alone claim.
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("expense_reports.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    category_key: Mapped[str] = mapped_column(String(80), nullable=False)
    city_tier: Mapped[CityTierEnum] = mapped_column(
        Enum(CityTierEnum, name="expense_city_tier"), nullable=False
    )
    kind: Mapped[ClaimKindEnum] = mapped_column(
        Enum(ClaimKindEnum, name="expense_claim_kind"),
        default=ClaimKindEnum.RECEIPTED,
        nullable=False,
    )
    #: In the reporting currency. `original_amount` is what was spent.
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    original_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    original_currency: Mapped[str] = mapped_column(
        String(3), default="INR", nullable=False
    )
    exchange_rate: Mapped[float | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    exchange_rate_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    conversion_convention: Mapped[ConversionConventionEnum | None] = mapped_column(
        Enum(ConversionConventionEnum, name="expense_conversion_convention"),
        nullable=True,
    )
    exchange_rate_source: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    incurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_receipt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    receipt_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    #: Mileage detail, present only on a mileage claim.
    distance_km: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    route_km: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    vehicle_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Per-diem detail.
    per_diem_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[ExpenseClaimStatusEnum] = mapped_column(
        Enum(ExpenseClaimStatusEnum, name="expense_claim_status"),
        default=ExpenseClaimStatusEnum.DRAFT,
        nullable=False,
    )
    #: What the employee was told at capture, and the numbers behind it.
    policy_verdict: Mapped[PolicyVerdictEnum] = mapped_column(
        Enum(PolicyVerdictEnum, name="expense_policy_verdict"), nullable=False
    )
    verdict_message: Mapped[str] = mapped_column(Text, nullable=False)
    applied_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("expense_policy_rules.id", ondelete="SET NULL"),
        nullable=True,
    )
    limit_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    already_claimed: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    overage: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: A flagged claim shows the flag to the approver. Hiding it would make the
    #: flag a metric nobody acts on.
    #: The approvers before finance, resolved at submission from the tenant's
    #: approval rules (the line manager when none applies). Empty for a claim
    #: submitted before chains existed, which keeps the line-manager stage.
    approval_chain: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    flagged_for_approver: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    exceeds_route_tolerance: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The line manager's stage, kept apart from finance's final decision so
    #: both approvals stay on the record.
    manager_decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    manager_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    manager_decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The payroll run that paid this claim. Set when the run is approved,
    #: never at compute, so a claim is paid by exactly one approved run.
    reimbursed_in_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
    )
    reimbursed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The travel-advance settlement this claim was offset against. A claim
    #: covered by an advance is not paid again through payroll.
    advance_settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("advance_settlements.id", ondelete="SET NULL"),
        nullable=True,
    )


class ExpensePolicyViolation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A breach, recorded once, with the rule as it stood.

    Kept separately from the claim so a violation rate survives the claim being
    cancelled or superseded, and so the limit that was breached is legible
    afterwards without resolving a policy version.
    """

    __tablename__ = "expense_policy_violations"
    __table_args__ = (
        UniqueConstraint("claim_id", name="uq_violation_per_claim"),
        Index(
            "ix_expense_violations_scope",
            "tenant_id",
            "department_id",
            "category_key",
        ),
    )

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("expense_claims.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    category_key: Mapped[str] = mapped_column(String(80), nullable=False)
    city_tier: Mapped[CityTierEnum] = mapped_column(
        Enum(CityTierEnum, name="expense_city_tier"), nullable=False
    )
    verdict: Mapped[PolicyVerdictEnum] = mapped_column(
        Enum(PolicyVerdictEnum, name="expense_policy_verdict"), nullable=False
    )
    violation_action: Mapped[ViolationActionEnum] = mapped_column(
        Enum(ViolationActionEnum, name="expense_violation_action"), nullable=False
    )
    limit_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    claim_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    overage: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    incurred_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)


# ══════════════════════════════════════════════════════════════════════════════
# Reports (line items) and receipts
# ══════════════════════════════════════════════════════════════════════════════


class ExpenseReportStatusEnum(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ExpenseReport(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A claim with line items: one trip or one month, several expenses.

    Each line is an `ExpenseClaim` with its own verdict, captured and judged the
    moment it is added. The report is submitted as a whole, so a blocked line
    stops the report rather than the rest going through quietly. Finance decides
    line by line, because approving the taxi and rejecting the minibar is the
    normal case, and the report status is derived from its lines.
    """

    __tablename__ = "expense_reports"
    __table_args__ = (
        Index("ix_expense_reports_employee", "tenant_id", "employee_id", "status"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    travel_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("travel_requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[ExpenseReportStatusEnum] = mapped_column(
        Enum(ExpenseReportStatusEnum, name="expense_report_status"),
        default=ExpenseReportStatusEnum.DRAFT,
        nullable=False,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ExpenseReceipt(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An uploaded receipt, validated by its bytes rather than its name.

    Held in the database until object storage exists. The client's filename and
    content type are never trusted: the type is read from the file's own
    signature, the size is capped, and it is served back only as an attachment.
    `storage_key` is generated; `display_name` is a sanitised label, never a path.
    """

    __tablename__ = "expense_receipts"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_expense_receipt_storage_key"),
        CheckConstraint("size_bytes > 0", name="ck_expense_receipt_size_positive"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    storage_key: Mapped[str] = mapped_column(String(120), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
