"""Back-dated salary revision and the arrears it generates.

═══════════════════════════════════════════════════════════════════════════════
THE ONE PRINCIPLE THIS SCHEMA ENFORCES
═══════════════════════════════════════════════════════════════════════════════

**A closed payroll run is never reopened.** Arrears are the *difference* between
what was paid and what should have been paid, held in their own rows, and paid
in a future run.

An auditor asking what was disbursed in March needs March's answer, not a
retroactively corrected version of it. A system that rewrites a closed period to
make the numbers "right" has destroyed the evidence that they were ever
different — and with it the ability to explain a bank statement, a filed return,
or a payslip the employee still has.

So `ArrearLine` holds `paid_amount` and `should_have_been_amount` side by side.
Storing only the difference would lose the same evidence one level down.
═══════════════════════════════════════════════════════════════════════════════

`ArrearLine` is one row per component per period, never a lump sum. Basic and a
travel allowance are taxed differently, count differently towards PF, and appear
on different lines of a return; collapsing them into one figure makes the
arrears untaxable and unfilable without re-deriving what they were made of.
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


class RevisionType(str, enum.Enum):
    INCREMENT = "increment"
    PROMOTION = "promotion"
    # Fixing a mistake in what was recorded, rather than a change of terms.
    CORRECTION = "correction"
    # A one-off payment owed for past periods with no change to the standing
    # salary — a delayed allowance, a settled dispute.
    ARREAR_ONLY = "arrear_only"


class ArrearStatus(str, enum.Enum):
    # Computed but not written to any payroll run. This is what "preview"
    # produces, and it is discardable.
    PREVIEW = "preview"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    # Attached to a future payroll run and paid.
    PAID = "paid"
    CANCELLED = "cancelled"


class SalaryRevision(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee's salary changing, possibly from a date in the past."""

    __tablename__ = "salary_revisions"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    previous_salary_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employee_salaries.id", ondelete="SET NULL"),
        nullable=True,
    )
    new_salary_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employee_salaries.id", ondelete="RESTRICT"),
        nullable=False,
    )

    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    revision_type: Mapped[RevisionType] = mapped_column(
        Enum(RevisionType, name="revision_type"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    arrear_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("arrear_batches.id", ondelete="SET NULL"), nullable=True
    )


class ArrearBatch(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One arrear computation, covering one or many employees.

    An increment cycle revises hundreds of people from a common date. That is
    one batch, not hundreds — the approval, the liability total and the payment
    are all per cycle, and a payroll lead approving three hundred separate
    batches will approve them without reading any.
    """

    __tablename__ = "arrear_batches"

    # A batch and a revision point at each other: the revision names the batch
    # it generated, and a single-employee batch names the revision that caused
    # it. `use_alter` breaks the cycle for DDL — without it neither table can be
    # created first, and neither can be dropped first either.
    #
    # For a bulk increment cycle this is null: three hundred revisions have no
    # single trigger, and `SalaryRevision.arrear_batch_id` is the link that
    # always holds.
    trigger_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "salary_revisions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_arrear_batch_trigger_revision",
        ),
        nullable=True,
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("legal_entities.id", ondelete="RESTRICT"), nullable=False
    )

    # "2026-04" to "2026-08" — the closed periods recomputed.
    from_period: Mapped[str] = mapped_column(String(7), nullable=False)
    to_period: Mapped[str] = mapped_column(String(7), nullable=False)

    status: Mapped[ArrearStatus] = mapped_column(
        Enum(ArrearStatus, name="arrear_status"),
        default=ArrearStatus.PREVIEW,
        nullable=False,
        index=True,
    )

    employee_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_earnings_arrear: Mapped[float] = mapped_column(
        Numeric(16, 2), default=0, nullable=False
    )
    # Employee-side statutory arrears reduce the net; employer-side ones do not
    # but are still owed, so they are counted separately rather than netted.
    total_employee_deduction_arrear: Mapped[float] = mapped_column(
        Numeric(16, 2), default=0, nullable=False
    )
    total_employer_contribution_arrear: Mapped[float] = mapped_column(
        Numeric(16, 2), default=0, nullable=False
    )
    total_net_payable: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)

    # Which already-filed returns this changes. Surfaced rather than acted on:
    # a revised ECR is a decision for the payroll team, not something to
    # attempt silently.
    affected_filings: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The future run the arrears were actually paid in.
    paid_in_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
    )

    # True when the total crossed the tenant's threshold, so a second approval
    # is required beyond the ordinary one.
    requires_additional_approval: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    lines: Mapped[list[ArrearLine]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ArrearLine(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One component, one period, what was paid and what should have been.

    Both figures are kept, not just the difference. The difference alone cannot
    answer "what did March's payslip say", which is the question that comes up
    when an employee disputes an arrear.
    """

    __tablename__ = "arrear_lines"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "batch_id", "employee_id", "period", "component_code",
            name="uq_arrear_line",
        ),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("arrear_batches.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    # The closed run this line recomputes. Read-only — nothing here writes to it.
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
    )

    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    component_code: Mapped[str] = mapped_column(String(64), nullable=False)
    component_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # earning | deduction | employer_contribution — the arrear of an employer
    # contribution is owed but does not reach the employee's net.
    component_type: Mapped[str] = mapped_column(String(32), nullable=False)

    paid_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    should_have_been_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    difference: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    # Which rule version the recomputation used. Not today's — the one that was
    # in force in that period. Recorded because it is the thing most likely to
    # be wrong and the hardest to notice.
    rule_set_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_rule_sets.id", ondelete="SET NULL"),
        nullable=True,
    )

    batch: Mapped[ArrearBatch] = relationship(back_populates="lines")


class SectionRelief(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Section 89(1) relief on arrears, and the Form 10E behind it.

    Arrears are taxable in the year of receipt, which can push an employee into
    a higher slab for money they earned across several years. Relief spreads
    the arrear back over the years it relates to and refunds the difference —
    but only where that is beneficial, so the computation produces both figures
    and takes the better one.

    `working` holds the full year-by-year comparison, because Form 10E is a
    declaration the employee signs and they are entitled to see how it was
    arrived at.
    """

    __tablename__ = "section_reliefs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "financial_year", "arrear_batch_id",
            name="uq_section_relief",
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    arrear_batch_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("arrear_batches.id", ondelete="CASCADE"), nullable=False
    )
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)

    arrear_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    tax_without_relief: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    tax_with_spreading: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    relief_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    # False where spreading back is not beneficial, which is common and is not
    # an error — the employee simply pays at the current year's rates.
    is_beneficial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    working: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    form_10e_letter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("issued_letters.id", ondelete="SET NULL"), nullable=True
    )
