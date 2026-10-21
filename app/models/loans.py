"""Loans, advances, and reimbursement salary components.

═══════════════════════════════════════════════════════════════════════════════
TWO THINGS THAT LOOK ALIKE AND ARE NOT
═══════════════════════════════════════════════════════════════════════════════

A **reimbursement component** is part of salary: an annual entitlement (fuel,
telephone, books) that is tax-exempt on production of a bill, with unclaimed
entitlement becoming taxable at year end. It is a payroll construct.

An **expense claim** is business expenditure the company reimburses at cost. It
has no entitlement, no tax exemption of its own, and no year-end consequence.

Conflating them is a common product mistake, and an expensive one: it either
taxes genuine business expenditure or gives an exemption to something that never
had one. They are separate models here, deliberately, and an invariant keeps
them that way. The employee never has to know the difference — they submit a
bill, and it is routed.
═══════════════════════════════════════════════════════════════════════════════
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class LoanType(str, enum.Enum):
    SALARY_ADVANCE = "salary_advance"
    COMPANY_LOAN = "company_loan"


class InterestType(str, enum.Enum):
    SIMPLE = "simple"
    REDUCING = "reducing"
    INTEREST_FREE = "interest_free"


class LoanStatus(str, enum.Enum):
    APPLIED = "applied"
    APPROVED = "approved"
    # Declined at approval. `decision_note` says why — "rejected" with no
    # reason is not a decision the applicant can do anything with.
    REJECTED = "rejected"
    DISBURSED = "disbursed"
    ACTIVE = "active"
    CLOSED = "closed"
    # Recovery abandoned. Kept rather than deleted — an amount the company
    # decided not to pursue is a decision somebody made, and it stays visible.
    WRITTEN_OFF = "written_off"


class ClaimStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    REJECTED = "rejected"
    # Some of the bill accepted, the rest not. Only the accepted amount is
    # paid exempt.
    PARTIAL = "partial"


class LoanScheme(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What a tenant offers, and to whom."""

    __tablename__ = "loan_schemes"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_loan_scheme_name"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    loan_type: Mapped[LoanType] = mapped_column(Enum(LoanType, name="loan_type"), nullable=False)

    max_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    # An alternative cap: three months of salary, say. Both can apply, and the
    # lower one governs.
    max_amount_as_multiple_of_salary: Mapped[float | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )

    interest_rate: Mapped[float] = mapped_column(Numeric(6, 3), default=0, nullable=False)
    interest_type: Mapped[InterestType] = mapped_column(
        Enum(InterestType, name="interest_type"), nullable=False
    )
    max_tenure_months: Mapped[int] = mapped_column(Integer, nullable=False)
    eligibility_min_service_months: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    moratorium_months: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accrue_interest_during_moratorium: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # The floor an employee must be left with after an EMI. Never zero by
    # default: a loan is not more urgent than someone's salary.
    minimum_net_pay: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    # Where a full EMI does not fit above the floor, recover what does rather
    # than skipping. Off by default, because a part-payment nobody recorded as
    # partial is how a ledger stops reconciling.
    allow_partial_recovery: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    approver_roles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LoanAccount(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One loan, from application through to closure.

    The terms are snapshotted at disbursement. A scheme's rate changing next
    year must not retroactively alter what an existing borrower owes.
    """

    __tablename__ = "loan_accounts"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    scheme_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("loan_schemes.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[LoanStatus] = mapped_column(
        Enum(LoanStatus, name="loan_status"),
        default=LoanStatus.APPLIED,
        nullable=False,
        index=True,
    )

    principal: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    interest_rate: Mapped[float] = mapped_column(Numeric(6, 3), default=0, nullable=False)
    interest_type: Mapped[InterestType] = mapped_column(
        Enum(InterestType, name="interest_type"), nullable=False
    )
    tenure_months: Mapped[int] = mapped_column(Integer, nullable=False)
    emi_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    outstanding_principal: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    # Instalments skipped because they would have breached the net-pay floor.
    # Attempted again next month, so a good month clears the backlog rather
    # than the backlog growing forever.
    carried_forward: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The approver's note, and on a rejection the required reason.
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disbursed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The schedule as built, kept so the borrower sees the same ledger the
    # payroll run recovers against.
    schedule: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    repayments: Mapped[list[LoanRepayment]] = relationship(
        back_populates="loan_account", cascade="all, delete-orphan"
    )


class LoanRepayment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One recovery, or one skipped one.

    A skipped instalment is a row too, with `is_skipped` and the reason. A
    ledger that only records what was recovered cannot explain a gap, and "why
    was nothing deducted in July" is a question the borrower will ask.
    """

    __tablename__ = "loan_repayments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "loan_account_id", "period", name="uq_loan_repayment_period"),
    )

    loan_account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("loan_accounts.id", ondelete="CASCADE"), nullable=False
    )
    payroll_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)

    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    principal_component: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    interest_component: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    closing_balance: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    is_skipped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_prepayment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Always populated, skipped or not. It is what the payslip and the borrower
    # get told.
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    loan_account: Mapped[LoanAccount] = relationship(back_populates="repayments")


# ── Reimbursement components — salary, not expenses ─────────────────────────


class ReimbursementComponent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A tax-exempt salary entitlement claimed against bills.

    Distinct from an expense claim: this is part of the employee's package with
    an annual entitlement, and unclaimed entitlement becomes taxable at year
    end. See the module docstring.
    """

    __tablename__ = "reimbursement_components"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_reimbursement_component_code"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    annual_entitlement: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    # The section or rule the exemption rests on. Recorded rather than assumed,
    # because it is what the exemption has to be defended with.
    exemption_basis: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Some components are exempt only against a bill; some are not.
    bill_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Within the financial year only. Carrying across years would defeat the
    # year-end taxation of unclaimed entitlement.
    carry_forward_within_year: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Months before year end to warn about unclaimed entitlement. The surprise
    # is avoidable, and warning afterwards is not a warning.
    reminder_months_before_year_end: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ReimbursementClaim(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One bill against one component.

    `accepted_amount` is separate from `amount`: a partially accepted bill pays
    only what was accepted, exempt, and the rest is simply not paid under this
    component.
    """

    __tablename__ = "reimbursement_claims"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    component_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)

    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    accepted_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    bill_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bill_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bill_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[ClaimStatus] = mapped_column(
        Enum(ClaimStatus, name="claim_status"),
        default=ClaimStatus.SUBMITTED,
        nullable=False,
        index=True,
    )
    verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Required on a rejection or a partial acceptance. "Rejected" with no
    # reason is not a decision the employee can act on.
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    paid_in_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
    )
    #: Paid in a leaver's full and final settlement instead of a payroll run.
    settled_in_settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "settlements.id",
            ondelete="SET NULL",
            name="fk_reimbursement_claims_settled_in_settlement_id",
        ),
        nullable=True,
    )


class FlexiDeclaration(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """How one person splits their flexi-benefit pot across reimbursement components, for a year.

    HR sets the pot — the part of the person's CTC that is flexible. The person
    declares how much goes to each component (fuel, telephone, meals…), each no
    more than the component's own limit and together no more than the pot. Once
    HR approves it, the split is that person's entitlement for the year: claims,
    payroll and the year-end taxable balance all read it instead of the
    component's default. A change after approval is a new declaration for HR.
    """

    __tablename__ = "flexi_declarations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "financial_year", name="uq_flexi_declaration_year"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False)
    pot: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    #: {component_code: amount}, as text amounts.
    allocations: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: draft | submitted | approved | rejected
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
