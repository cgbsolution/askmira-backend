"""Points liability, valuation, budgets, tax treatment and reconciliation.

═══════════════════════════════════════════════════════════════════════════════
`points_ledger` IS INSERT-ONLY, THE SAME AS THE AUDIT LOG
═══════════════════════════════════════════════════════════════════════════════

No service updates a row and no service deletes one. A correction is a
`REVERSED` or `ADJUSTED` insert, which the employee can see and ask about; a row
that disappears leaves somebody who had 500 points and now has none with nothing
on their statement to explain it.

The table therefore has **no `updated_at`** — it does not carry `TimestampMixin`
— because a column that records when a row was last changed is an invitation to
change one. An invariant fails the build if an update or delete path appears.

`balance_after` is written once, at insert, and is a **checkpoint** rather than
the balance. Reporting derives the balance from the transactions; `verify_chain`
compares the two, so a divergence is visible instead of silently authoritative.

═══════════════════════════════════════════════════════════════════════════════
`rate_per_point` IS STAMPED ON THE ROW
═══════════════════════════════════════════════════════════════════════════════

Not looked up at read time. That is what makes a later revaluation unable to
restate a prior period's liability: the rate the award was made at is part of
the award.

═══════════════════════════════════════════════════════════════════════════════
`reward_tax_rules.reviewed` DEFAULTS TO FALSE
═══════════════════════════════════════════════════════════════════════════════

Every shipped default is unreviewed, and payroll refuses a taxable figure from
an unreviewed rule. Getting a perquisite threshold wrong creates a liability for
the client which they discover in an assessment years later.
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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class PointsTransactionTypeEnum(str, enum.Enum):
    AWARDED = "awarded"
    REDEEMED = "redeemed"
    EXPIRED = "expired"
    ADJUSTED = "adjusted"
    REVERSED = "reversed"


class BudgetScopeEnum(str, enum.Enum):
    COMPANY = "company"
    DEPARTMENT = "department"
    MANAGER = "manager"


class EnforcementModeEnum(str, enum.Enum):
    HARD_STOP = "hard_stop"
    SOFT_WARNING = "soft_warning"


class TaxTreatmentEnum(str, enum.Enum):
    PERQUISITE = "perquisite"
    EXEMPT = "exempt"
    #: Separate from PERQUISITE because they behave differently at the
    #: threshold: under the gift rule the whole value becomes taxable once the
    #: limit is crossed, not only the excess.
    GIFT_RULE = "gift_rule"


class RedemptionStatusEnum(str, enum.Enum):
    REQUESTED = "requested"
    ORDERED = "ordered"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"


class ReconciliationStatusEnum(str, enum.Enum):
    CLEAN = "clean"
    GAPS_FOUND = "gaps_found"


# ══════════════════════════════════════════════════════════════════════════════
# The ledger
# ══════════════════════════════════════════════════════════════════════════════


class PointsLedgerEntry(Base, UUIDMixin, TenantMixin):
    """One immutable points transaction.

    Deliberately **not** `TimestampMixin`: an `updated_at` column on an
    insert-only table is an invitation to update it. `created_at` is here
    explicitly and nothing else records mutation, because nothing mutates.
    """

    __tablename__ = "points_ledger"
    __table_args__ = (
        CheckConstraint("points <> 0", name="ck_points_ledger_nonzero"),
        Index("ix_points_ledger_employee", "tenant_id", "employee_id", "occurred_at"),
        Index("ix_points_ledger_department", "tenant_id", "department_key"),
        Index("ix_points_ledger_source", "tenant_id", "source_type", "source_id"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transaction_type: Mapped[PointsTransactionTypeEnum] = mapped_column(
        Enum(PointsTransactionTypeEnum, name="points_transaction_type"),
        nullable=False,
    )
    #: Positive except on an ADJUSTED row, which carries its own sign. A
    #: negative award is a reversal and has to say so.
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Rupee value at the rate in force when this row was written. Never
    #: recomputed.
    monetary_value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    #: The rate itself, kept so a statement can explain why two awards of the
    #: same size are worth different amounts.
    rate_per_point: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    #: A checkpoint written at insert, never the reported balance. Reporting
    #: derives; `verify_chain` compares the two so drift is visible.
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: Denormalised at insert so a provision allocated to Engineering in March
    #: does not move when somebody reorganises in November.
    department_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cost_centre: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: For a REVERSED row: which entry it reverses. Visible on the statement.
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class PointsValuation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """How many points one rupee buys, from one date.

    A rate change inserts a row. It never edits one, and it never restates a
    point already issued.
    """

    __tablename__ = "points_valuations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "effective_from", name="uq_points_valuation_version"),
        CheckConstraint("points_per_rupee > 0", name="ck_points_per_rupee_positive"),
    )

    points_per_rupee: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class PointsExpiryPolicy(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """When points expire, and the disclosure shown at the point of award."""

    __tablename__ = "points_expiry_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_expiry_policy_per_tenant"),
        CheckConstraint("expire_after_days >= 90", name="ck_expiry_not_punitive"),
        CheckConstraint("notify_days_before >= 7", name="ck_expiry_warning_useful"),
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expire_after_days: Mapped[int] = mapped_column(Integer, default=365, nullable=False)
    notify_days_before: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    #: Shown at award time, not buried. Points that quietly vanish teach people
    #: the recognition was not real.
    disclosure: Mapped[str] = mapped_column(Text, nullable=False)


class PointsBudget(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One allocation, for one scope, for one period."""

    __tablename__ = "points_budgets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scope",
            "scope_key",
            "period_start",
            name="uq_budget_per_scope_period",
        ),
        CheckConstraint("allocated_points >= 0", name="ck_budget_allocation_nonneg"),
        CheckConstraint("consumed_points >= 0", name="ck_budget_consumed_nonneg"),
        CheckConstraint("period_end >= period_start", name="ck_budget_period_ordered"),
    )

    scope: Mapped[BudgetScopeEnum] = mapped_column(
        Enum(BudgetScopeEnum, name="points_budget_scope"), nullable=False
    )
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    allocated_points: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enforcement: Mapped[EnforcementModeEnum] = mapped_column(
        Enum(EnforcementModeEnum, name="points_budget_enforcement"),
        default=EnforcementModeEnum.SOFT_WARNING,
        nullable=False,
    )
    #: Recorded rather than only warned about. A warning nobody counts is a
    #: warning nobody acts on.
    overspend_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# Tax
# ══════════════════════════════════════════════════════════════════════════════


class RewardTaxRule(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """How one reward type is treated, from one date.

    `reviewed` defaults to **False** and payroll refuses a taxable figure from
    an unreviewed rule. Getting a threshold wrong creates a liability the client
    discovers in an assessment years later.
    """

    __tablename__ = "reward_tax_rules"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "reward_type",
            "effective_from",
            name="uq_reward_tax_rule_version",
        ),
        CheckConstraint("exemption_threshold_per_year >= 0", name="ck_reward_threshold_nonneg"),
        CheckConstraint(
            "(reviewed = false) OR (reviewed_by IS NOT NULL)",
            name="ck_reviewed_names_reviewer",
        ),
    )

    reward_type: Mapped[str] = mapped_column(String(64), nullable=False)
    treatment: Mapped[TaxTreatmentEnum] = mapped_column(
        Enum(TaxTreatmentEnum, name="reward_tax_treatment"), nullable=False
    )
    exemption_threshold_per_year: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: What the threshold is understood to rest on, so a reviewer can disagree
    #: with the reading rather than only with the number.
    basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reviewed_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class RewardEvent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One reward given to one person, valued in rupees, for FY cumulation.

    Separate from the ledger because the taxable thing is the **reward**, not
    the points: an employee who redeems 5,000 points for headphones received
    headphones. The ledger records the points movement; this records what they
    got and what it was worth.
    """

    __tablename__ = "reward_events"
    __table_args__ = (
        CheckConstraint("value >= 0", name="ck_reward_event_value_nonneg"),
        Index("ix_reward_events_fy", "tenant_id", "employee_id", "financial_year"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reward_type: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    given_on: Mapped[date] = mapped_column(Date, nullable=False)
    #: Denormalised so a year's cumulation is a single indexed lookup rather
    #: than a date-range scan with an April boundary in every query.
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    #: Written when the perquisite has been handed to payroll. Null means it
    #: has not been, which is what keeps an unprocessed perquisite visible.
    payroll_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)


# ══════════════════════════════════════════════════════════════════════════════
# Redemption and reconciliation
# ══════════════════════════════════════════════════════════════════════════════


class PointsRedemption(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One redemption on our side."""

    __tablename__ = "points_redemptions"
    __table_args__ = (
        CheckConstraint("points > 0", name="ck_redemption_points_positive"),
        Index("ix_redemptions_period", "tenant_id", "requested_on"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    rupee_value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    item_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[RedemptionStatusEnum] = mapped_column(
        Enum(RedemptionStatusEnum, name="points_redemption_status"),
        default=RedemptionStatusEnum.REQUESTED,
        nullable=False,
    )
    requested_on: Mapped[date] = mapped_column(Date, nullable=False)
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)


class FulfilmentRecord(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What the merchandise partner says it shipped, as imported.

    Stored as received rather than merged into the redemption. Reconciliation
    compares two independent records; folding theirs into ours would leave
    nothing to compare.
    """

    __tablename__ = "reward_fulfilments"
    __table_args__ = (UniqueConstraint("tenant_id", "order_id", name="uq_fulfilment_order"),)

    redemption_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("points_redemptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Kept as the partner's own reference even when it matches nothing here -
    #: an orphan fulfilment is a finding, not a row to discard.
    external_redemption_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    order_id: Mapped[str] = mapped_column(String(120), nullable=False)
    item_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    fulfilled_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    imported_on: Mapped[date] = mapped_column(Date, nullable=False)


class RewardInvoiceLine(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One line the partner billed us for."""

    __tablename__ = "reward_invoice_lines"
    __table_args__ = (Index("ix_invoice_lines_order", "tenant_id", "order_id"),)

    order_id: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    item_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    invoiced_on: Mapped[date] = mapped_column(Date, nullable=False)


class ReconciliationRun(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One month's reconciliation and the gaps it found.

    Kept even when clean. A clean month with no record is indistinguishable
    from a month nobody reconciled, and the question an auditor asks is whether
    it runs every month.
    """

    __tablename__ = "reward_reconciliations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period_label", name="uq_reconciliation_period"),
    )

    period_label: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[ReconciliationStatusEnum] = mapped_column(
        Enum(ReconciliationStatusEnum, name="reward_reconciliation_status"),
        nullable=False,
    )
    redemption_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fulfilment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invoice_line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    redeemed_value: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    invoiced_value: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    #: The gaps as reported, by kind. Not netted - a fulfilment failure and a
    #: billing error of the same size would cancel to zero.
    gaps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    gap_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_on: Mapped[date] = mapped_column(Date, nullable=False)
    run_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
