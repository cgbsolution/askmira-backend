"""Income tax declarations, proofs and regime election (slice 07).

In India this is an annual, high-stakes, high-support-volume workflow:
declaration in April, proof submission in January, and a correction scramble in
March. Payroll computes TDS but has had no concept of what an employee actually
declared or proved. This is that.

Invariants:

1. **No section limit, slab or threshold appears in this module or its
   service.** Every ceiling — 80C, 80D by age band, 80CCD(1B), 24B, the
   standard deduction, the HRA percentages, the landlord-PAN threshold — lives
   in an effective-dated `PayrollRuleSet` supplied and approved by the
   Compliance SME. A Budget change is a configuration change, not a release.
   A test greps this module for numeric literals.
2. **A regime switch recomputes the whole financial year**, not just the months
   ahead. TDS is an annual liability spread across months; changing the regime
   in October and only applying it forward leaves six months computed under a
   regime the employee is no longer on.
3. **`PARTIALLY_ACCEPTED` is a first-class outcome.** Someone declares 150,000
   under 80C and proves 90,000. Accepting 90,000 with a remark is correct;
   rejecting the whole line is not, and it generates a support ticket.
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


class TaxRegime(str, enum.Enum):
    OLD = "old"
    NEW = "new"


class WindowType(str, enum.Enum):
    DECLARATION = "declaration"
    PROOF = "proof"


class DeclarationStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    # Locked once the window closes; the amount is fixed for the year.
    LOCKED = "locked"


class ProofStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"
    # Declared 150,000, proved 90,000. Accept what was proved.
    PARTIALLY_ACCEPTED = "partially_accepted"


class TaxRegimeElection(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Which regime an employee has elected for a financial year.

    A salaried employee may switch once per year. `locked_at` is set once
    payroll has run against the election — after that a change is still allowed
    but triggers a full-year TDS recompute rather than applying forward.
    """

    __tablename__ = "tax_regime_elections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "financial_year", name="uq_regime_election_year"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    # Indian financial year as "2026-27".
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)

    regime: Mapped[TaxRegime] = mapped_column(Enum(TaxRegime, name="tax_regime"), nullable=False)
    elected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set when a payroll run has used this election.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # How many times the employee has changed it this year. The permitted
    # number is configuration, not a constant here.
    switch_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    previous_regime: Mapped[TaxRegime | None] = mapped_column(
        Enum(TaxRegime, name="tax_regime"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<TaxRegimeElection {self.employee_id} {self.financial_year} {self.regime.value}>"


class DeclarationWindow(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """When employees may declare, and when they must prove.

    Two windows per year. The proof window closing is the moment unproven
    declarations drop to their accepted amounts, which usually spikes March
    TDS — so the closing date is something employees are warned about, not
    something they discover on a payslip.
    """

    __tablename__ = "declaration_windows"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "financial_year", "window_type", name="uq_declaration_window"
        ),
    )

    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)
    window_type: Mapped[WindowType] = mapped_column(
        Enum(WindowType, name="declaration_window_type"), nullable=False
    )

    opens_at: Mapped[date] = mapped_column(Date, nullable=False)
    closes_at: Mapped[date] = mapped_column(Date, nullable=False)
    allow_late_submission: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Days before closing to send a reminder, in descending order.
    reminder_days_before: Mapped[list] = mapped_column(
        JSONB, default=lambda: [30, 14, 7, 1], nullable=False
    )
    reminders_sent: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    def is_open(self, on: date) -> bool:
        return self.opens_at <= on <= self.closes_at


class TaxDeclaration(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One declared amount under one section.

    `section` is a free string rather than an enum on purpose: sections come
    and go with each Budget, and a new one should be a configuration entry in
    the rule set rather than a schema migration.
    """

    __tablename__ = "tax_declarations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "employee_id",
            "financial_year",
            "section",
            "sub_category",
            name="uq_tax_declaration_line",
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)

    # "80C", "80D", "24B", "80CCD1B", "HRA", "LTA", …
    section: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # "PPF", "ELSS", "life_insurance", "self_and_family", "parents_senior", …
    sub_category: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    declared_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    status: Mapped[DeclarationStatus] = mapped_column(
        Enum(DeclarationStatus, name="declaration_status"),
        default=DeclarationStatus.DRAFT,
        nullable=False,
        index=True,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    proofs: Mapped[list[TaxProof]] = relationship(
        back_populates="declaration", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def accepted_amount(self) -> float:
        """What actually reduces taxable income.

        The sum of accepted proof amounts — not the declared amount. A
        declaration with no verified proof contributes nothing, which is
        exactly what produces the March TDS spike.
        """
        from decimal import Decimal

        return sum(
            (
                Decimal(str(proof.accepted_amount or 0))
                for proof in self.proofs
                if proof.status in {ProofStatus.VERIFIED, ProofStatus.PARTIALLY_ACCEPTED}
            ),
            start=Decimal(0),
        )

    def __repr__(self) -> str:
        return f"<TaxDeclaration {self.section} {self.declared_amount}>"


class TaxProof(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A document supporting a declaration, and the verifier's decision."""

    __tablename__ = "tax_proofs"

    declaration_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tax_declarations.id", ondelete="CASCADE"), nullable=False
    )

    document_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    document_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proof_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    status: Mapped[ProofStatus] = mapped_column(
        Enum(ProofStatus, name="proof_status"),
        default=ProofStatus.PENDING,
        nullable=False,
        index=True,
    )
    # What the verifier actually allowed. Never assumed equal to proof_amount.
    accepted_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    verifier_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Required on anything but a clean acceptance — an employee who is told
    # "rejected" with no reason raises a ticket, every time.
    verifier_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    declaration: Mapped[TaxDeclaration] = relationship(back_populates="proofs")


class PreviousEmployerIncome(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Income from a previous employer in the same financial year.

    Without this a mid-year joiner is taxed as though they earned nothing
    before joining, which under-deducts all year and lands them with a large
    liability at filing.
    """

    __tablename__ = "previous_employer_incomes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "financial_year", name="uq_previous_employer_year"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)

    employer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gross_salary: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tds_deducted: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    pf_deducted: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    professional_tax: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    exempt_allowances: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    # Form 16 Part B from the previous employer.
    document_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    status: Mapped[ProofStatus] = mapped_column(
        Enum(ProofStatus, name="proof_status"), default=ProofStatus.PENDING, nullable=False
    )
    verifier_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    verifier_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)


class HouseRentDeclaration(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Rent paid, for the HRA exemption.

    `landlord_pan` is required above an annual threshold — and the threshold is
    configuration, not a constant. The requirement is enforced at submission
    rather than discovered at assessment.
    """

    __tablename__ = "house_rent_declarations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", "financial_year", name="uq_house_rent_year"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)

    # [{"month": 4, "year": 2026, "rent": "25000.00"}, …] — monthly rows,
    # because rent changes mid-year and a single annual figure loses that.
    monthly_rent: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    landlord_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    landlord_pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    property_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Drives which HRA percentage applies. The percentages themselves are
    # configuration.
    is_metro: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[ProofStatus] = mapped_column(
        Enum(ProofStatus, name="proof_status"), default=ProofStatus.PENDING, nullable=False
    )
    document_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    verifier_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def annual_rent(self) -> float:
        from decimal import Decimal

        return sum(
            (Decimal(str(row.get("rent", 0))) for row in self.monthly_rent),
            start=Decimal(0),
        )


class TaxComputationSnapshot(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A stored projection, so an employee can see why their TDS is what it is.

    Recomputed whenever a declaration, proof, salary or regime changes. Kept
    rather than derived on demand because the *reason* matters as much as the
    number: an employee asking "why did my TDS jump" needs the previous
    projection to compare against.
    """

    __tablename__ = "tax_computation_snapshots"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)
    regime: Mapped[TaxRegime] = mapped_column(Enum(TaxRegime, name="tax_regime"), nullable=False)

    computed_for_month: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_annual_income: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    total_exemptions: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total_deductions: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    taxable_income: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    annual_tax_liability: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    tds_already_deducted: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    monthly_tds: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    # The full working: every deduction line, the HRA three-way comparison and
    # which constraint bound, the slab breakdown. This is what makes the number
    # explainable rather than asserted.
    working: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    rule_set_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("payroll_rule_sets.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<TaxComputationSnapshot {self.employee_id} {self.financial_year}>"
