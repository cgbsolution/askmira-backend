"""Payroll: salary structure, statutory rule sets, runs and payslips.

Slice 01 introduced the salary structure engine. The shape here is what every
later payroll capability reads from — arrears, gratuity, IT declarations, full
and final — so the invariants matter more than any screen built on top.

Invariants, all release-blocking:

1. `PayrollRuleSet` rows are never updated in place. A rate change writes a new
   effective-dated row. A historical run recomputes against the version active
   on its own run date (FS-PAY-02).
2. `SalaryStructure` is effective-dated for the same reason: editing a structure
   today must never alter a payroll run that already used an earlier version.
3. `EmployeeSalary` is a series, never an updated row. A revision inserts.
4. `EmployeeSalaryComponent` amounts are materialised at assignment time, so a
   run never re-derives historical amounts from current configuration.
5. A run marked PAID is immutable. Corrections are a new `arrear` or
   `off_cycle` run, never an edit.
6. All money is `Numeric`. No float anywhere in payroll.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

# ── Enums ───────────────────────────────────────────────────────────────────


class ComponentType(str, enum.Enum):
    EARNING = "earning"
    DEDUCTION = "deduction"
    EMPLOYER_CONTRIBUTION = "employer_contribution"
    REIMBURSEMENT = "reimbursement"


class WageGroup(str, enum.Enum):
    """Where a wage type sits on the Salary master, finer than its `ComponentType`.

    Display and totals only: the payroll engine works from `ComponentType`.
    Statutory deductions (PF, ESI, PT, LWF, TDS) are not a group here because
    no tenant component holds them; the statutory engine works them out.
    """

    REGULAR_EARNING = "regular_earning"
    VARIABLE_EARNING = "variable_earning"
    REIMBURSEMENT = "reimbursement"
    NON_STATUTORY_DEDUCTION = "non_statutory_deduction"
    COMPANY_CONTRIBUTION = "company_contribution"
    PROVISION = "provision"


#: The kind of component each group holds.
WAGE_GROUP_TYPE: dict[WageGroup, ComponentType] = {
    WageGroup.REGULAR_EARNING: ComponentType.EARNING,
    WageGroup.VARIABLE_EARNING: ComponentType.EARNING,
    WageGroup.REIMBURSEMENT: ComponentType.REIMBURSEMENT,
    WageGroup.NON_STATUTORY_DEDUCTION: ComponentType.DEDUCTION,
    WageGroup.COMPANY_CONTRIBUTION: ComponentType.EMPLOYER_CONTRIBUTION,
    WageGroup.PROVISION: ComponentType.EMPLOYER_CONTRIBUTION,
}

#: A component with no group set sits in its kind's usual group.
DEFAULT_WAGE_GROUP: dict[ComponentType, WageGroup] = {
    ComponentType.EARNING: WageGroup.REGULAR_EARNING,
    ComponentType.REIMBURSEMENT: WageGroup.REIMBURSEMENT,
    ComponentType.DEDUCTION: WageGroup.NON_STATUTORY_DEDUCTION,
    ComponentType.EMPLOYER_CONTRIBUTION: WageGroup.COMPANY_CONTRIBUTION,
}


def wage_group_of(component_type: ComponentType | str, stored: str | None) -> WageGroup:
    """A component's group: the one set on it if it fits its kind, else the kind's default."""
    kind = ComponentType(component_type)
    if stored:
        try:
            group = WageGroup(stored)
        except ValueError:
            group = None
        if group is not None and WAGE_GROUP_TYPE[group] is kind:
            return group
    return DEFAULT_WAGE_GROUP[kind]


class CalculationType(str, enum.Enum):
    """How a component's amount is arrived at.

    BALANCE is the residual: whatever of the CTC is left once every other
    in-CTC component has resolved. Exactly one per structure — zero means the
    breakdown cannot sum to CTC, more than one is ambiguous.
    """

    FIXED = "fixed"
    PERCENTAGE_OF = "percentage_of"
    FORMULA = "formula"
    BALANCE = "balance"


class RoundingMode(str, enum.Enum):
    """Mirrors Python's `decimal` rounding modes.

    HALF_UP is the default because it is what Indian payroll conventionally
    does and what an employee expects when they check the arithmetic by hand.
    """

    HALF_UP = "half_up"
    HALF_EVEN = "half_even"
    CEILING = "ceiling"
    FLOOR = "floor"


class PayrollRunStatus(str, enum.Enum):
    DRAFT = "draft"
    PROCESSING = "processing"
    # Computed, variances flagged, waiting on HR Ops sign-off before money moves.
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PayrollRunType(str, enum.Enum):
    REGULAR = "regular"
    OFF_CYCLE = "off_cycle"
    ARREAR = "arrear"
    FULL_AND_FINAL = "full_and_final"
    BONUS = "bonus"


# ── Salary structure ────────────────────────────────────────────────────────


class SalaryComponent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A named line on a payslip: Basic, HRA, Special Allowance, PF, PT, TDS.

    `formula` holds a restricted arithmetic expression over other component
    codes. It is evaluated by an AST walker in
    `app/services/payroll/formula.py` — never by `eval`.
    """

    __tablename__ = "salary_components"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_salary_components_tenant_code"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    component_type: Mapped[ComponentType] = mapped_column(
        Enum(ComponentType, name="component_type"), nullable=False
    )

    calculation_type: Mapped[CalculationType] = mapped_column(
        Enum(CalculationType, name="calculation_type"),
        default=CalculationType.FIXED,
        nullable=False,
    )
    # What a PERCENTAGE_OF is a percentage of. Null for every other type.
    base_component_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The fixed amount, or the percentage, depending on calculation_type.
    value: Mapped[float | None] = mapped_column(Numeric(14, 4), nullable=True)
    formula: Mapped[str | None] = mapped_column(String(512), nullable=True)

    taxable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Whether this component consumes part of the CTC. Employer PF usually
    # does in India; a reimbursement usually does not.
    part_of_ctc: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Whether loss-of-pay days reduce this component. Most employer
    # contributions are not prorated; earnings are.
    pro_rate_on_lop: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Statutory wage-base membership. PF is computed on BASIC + DA, not on
    # gross — these flags are what make that correct rather than assumed.
    counts_toward_pf: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    counts_toward_esi: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: A `WageGroup` value, or null for the kind's usual group. Display only.
    wage_group: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # System components (PF, ESI, PT, TDS) belong to the statutory engine and
    # cannot be edited or removed by a tenant admin.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<SalaryComponent {self.code}>"


class SalaryStructure(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A reusable CTC template, effective-dated and scoped to a population.

    Empty `applicable_grades` or `applicable_locations` means "applies to
    everyone" — the most specific matching structure wins at assignment time.
    """

    __tablename__ = "salary_structures"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    applicable_grades: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, nullable=False
    )
    applicable_locations: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, nullable=False
    )

    # ── Rounding policy ─────────────────────────────────────────────────────
    # Made explicit rather than implicit because rounding is a real source of
    # payroll disputes. See the policy comment block in
    # `app/services/payroll/salary_structure.py`.
    rounding_mode: Mapped[RoundingMode] = mapped_column(
        Enum(RoundingMode, name="rounding_mode"), default=RoundingMode.HALF_UP, nullable=False
    )
    # Decimal places. 0 = whole rupees (the common Indian choice), 2 = paise.
    rounding_precision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    components: Mapped[list[SalaryStructureComponent]] = relationship(
        back_populates="structure", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<SalaryStructure {self.name} from {self.effective_from}>"


class SalaryStructureComponent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Membership of a component in a structure, with per-structure overrides.

    The override columns let one component behave differently across
    structures — HRA at 50% of Basic in a metro structure, 40% elsewhere —
    without duplicating the component definition.
    """

    __tablename__ = "salary_structure_components"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "structure_id", "component_id", name="uq_structure_component_once"
        ),
    )

    structure_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("salary_structures.id", ondelete="CASCADE"), nullable=False
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("salary_components.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Null means "use the component's own definition".
    override_value: Mapped[float | None] = mapped_column(Numeric(14, 4), nullable=True)
    override_formula: Mapped[str | None] = mapped_column(String(512), nullable=True)
    override_base_component_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    structure: Mapped[SalaryStructure] = relationship(back_populates="components")
    component: Mapped[SalaryComponent] = relationship(lazy="selectin")


class EmployeeSalary(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee's CTC on one structure, effective-dated.

    A revision inserts a new row and closes the previous one's `effective_to`.
    An employee's salary history is the series.

    A revision is history once payroll has used it: once an approved or paid
    run has a payslip on it, or an arrears revision points at it, it is never
    changed or removed. Until then HR may correct or delete it from the Salary
    master, audited, since nothing has been paid on it yet.
    """

    __tablename__ = "employee_salaries"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    structure_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("salary_structures.id", ondelete="RESTRICT"),
        nullable=False,
    )

    ctc_annual: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    revision_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # One supporting file per revision (an increment letter, an approval mail).
    # The key is server-generated; the name is only what is shown.
    attachment_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    attachment_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attachment_mime: Mapped[str | None] = mapped_column(String(128), nullable=True)

    components: Mapped[list[EmployeeSalaryComponent]] = relationship(
        back_populates="employee_salary", cascade="all, delete-orphan", lazy="selectin"
    )
    structure: Mapped[SalaryStructure] = relationship(lazy="selectin")

    def __repr__(self) -> str:
        return f"<EmployeeSalary {self.employee_id} {self.ctc_annual} from {self.effective_from}>"


class EmployeeSalaryComponent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A resolved component amount, materialised at assignment time.

    These rows are the payroll engine's input. Editing the structure later
    leaves them byte-identical, which is what makes a historical run
    reproducible. They are only ever replaced while their revision is still
    unpaid (see `EmployeeSalary`).

    The statutory flags are snapshotted here too, so a later change to a
    component definition cannot retroactively move a past run's PF wage base.
    """

    __tablename__ = "employee_salary_components"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "employee_salary_id",
            "component_code",
            name="uq_employee_salary_component_once",
        ),
    )

    employee_salary_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employee_salaries.id", ondelete="CASCADE"), nullable=False
    )

    component_code: Mapped[str] = mapped_column(String(32), nullable=False)
    component_name: Mapped[str] = mapped_column(String(128), nullable=False)
    component_type: Mapped[ComponentType] = mapped_column(
        Enum(ComponentType, name="component_type"), nullable=False
    )

    annual_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    monthly_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    # Snapshotted definition — see the class docstring.
    taxable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    part_of_ctc: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pro_rate_on_lop: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    counts_toward_pf: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    counts_toward_esi: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Typed in for this employee on the Salary master, not worked out by the
    #: structure. A later edit keeps it typed unless HR resets it.
    is_manual: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    remarks: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Whether a typed amount was entered as a month's or a year's figure, so
    #: the editor shows it back exactly as typed ("monthly" or "annual").
    pay_basis: Mapped[str] = mapped_column(
        String(8), default="monthly", server_default=text("'monthly'"), nullable=False
    )

    employee_salary: Mapped[EmployeeSalary] = relationship(back_populates="components")


# ── Statutory rule sets ─────────────────────────────────────────────────────


class PayrollRuleSet(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A versioned, effective-dated statutory rule set (FS-PAY-02).

    One row per (rule_type, jurisdiction, effective_from). Superseding a rule
    means inserting a new row and closing the old one's `effective_to` — never
    an UPDATE of `rules`.

    `approved_by_user_id` is the Compliance SME sign-off gate from FRD 9.3: the
    payroll engine refuses to compute against an unapproved rule set. No
    statutory rate, slab or threshold is ever hardcoded in application code —
    it lives here, supplied and approved by the SME.
    """

    __tablename__ = "payroll_rule_sets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "rule_type",
            "jurisdiction",
            "effective_from",
            name="uq_rule_set_version",
        ),
    )

    # pf | esi | professional_tax | tds | gratuity | bonus | labour_welfare_fund
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # "IN" for central rules, or an ISO 3166-2:IN state code for state rules.
    jurisdiction: Mapped[str] = mapped_column(String(8), nullable=False, index=True)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Slabs, rates, ceilings and thresholds. Shape varies per rule_type and is
    # validated by the Pydantic schema for that type before it is written.
    rules: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Gazette notification, circular number, or the source URL the rule came
    # from. Required for audit; the V4 statutory monitor populates it too.
    source_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Who entered this version. The person who typed the rates in is not the
    # person who attests to them, so approval refuses the creator.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None


# ── Runs and payslips ───────────────────────────────────────────────────────


class PayrollRun(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "payroll_runs"
    __table_args__ = (
        # One regular run per entity and month. Off-cycle, bonus and arrear runs
        # are supplementary and may be as many as the month needs. A cancelled
        # run (opened in error, never approved) does not hold the month.
        Index(
            "uq_payroll_run_regular_period",
            "tenant_id",
            "legal_entity_id",
            "period_year",
            "period_month",
            unique=True,
            postgresql_where=text("run_type = 'REGULAR' AND status <> 'CANCELLED'"),
        ),
    )

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("legal_entities.id", ondelete="RESTRICT"), nullable=False
    )

    period_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # The date whose rule and structure versions this run resolves against.
    # Fixed at creation so a re-run months later produces an identical result.
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    run_type: Mapped[PayrollRunType] = mapped_column(
        Enum(PayrollRunType, name="payroll_run_type"),
        default=PayrollRunType.REGULAR,
        nullable=False,
    )
    status: Mapped[PayrollRunStatus] = mapped_column(
        Enum(PayrollRunStatus, name="payroll_run_status"),
        default=PayrollRunStatus.DRAFT,
        nullable=False,
        index=True,
    )

    employee_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_gross: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)
    total_deductions: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)
    total_net: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)
    total_employer_cost: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)

    # Employees whose computed value moved beyond the configured threshold
    # versus the prior cycle. Non-empty means the run cannot be approved
    # without an explicit acknowledgement (FS-PAY-01).
    variance_flags: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # Statutory questions payroll declined to answer alone — a work location
    # that changed mid-period, say. Unlike a variance, these are not "check
    # this number is right"; they are "this number could not be determined",
    # and the run cannot be approved while any remain unresolved.
    review_flags: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # Snapshot of {rule_type: rule_set_id} used by this run, so the exact
    # versions stay provable even if rule rows are later superseded.
    rule_set_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Maker-checker: whoever computed the run cannot be the one who approves it.
    computed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # A run opened in error and discarded before approval. Its payslips are
    # removed; the row stays, with who discarded it and why.
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the input providers (loans, reimbursements, arrears, …) said about
    # this run that is not a line on anybody's payslip: a source that is not
    # available yet, or an item left unprocessed and why. Informational — a
    # notice never blocks approval; a review flag does.
    #: `server_default` as well as `default`: the column is NOT NULL and was added to a
    #: table that already had rows, so the database needs its own answer for anything
    #: inserted outside the ORM. Declared here too, or `alembic check` reports drift.
    input_notices: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )

    # ── Off-cycle runs ──────────────────────────────────────────────────────
    #: Who an off-cycle, bonus or arrear run pays. Empty on a regular run, which
    #: pays everyone employed in the period.
    employee_scope: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    #: Which input sources a supplementary run draws on (provider keys). Null
    #: means every source, which is what a regular run always uses.
    included_sources: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    #: Why a supplementary run was opened — shown on the run and in the audit.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    payslips: Mapped[list[Payslip]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def is_locked(self) -> bool:
        """A paid run is history. It is never edited, only superseded."""
        return self.status is PayrollRunStatus.PAID


class Payslip(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One employee's computed pay for one run.

    `line_items` is a full computation trace, not just totals: each line records
    the component, the amount, and the rule that produced it. That is what makes
    a payslip explainable to an employee and auditable to a statutory inspector.
    """

    __tablename__ = "payslips"
    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id", "employee_id", name="uq_payslip_run_employee"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    # Which materialised salary this run computed against — the audit link back
    # to the exact amounts used.
    employee_salary_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employee_salaries.id", ondelete="SET NULL"), nullable=True
    )

    working_days: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    paid_days: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    lop_days: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    # Days of the period before the employee joined or after they left. Unpaid,
    # like loss of pay, but kept apart from it: it is not an absence.
    not_employed_days: Mapped[float] = mapped_column(
        Numeric(5, 2), default=0, server_default=text("0"), nullable=False
    )

    gross_earnings: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total_deductions: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_pay: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    employer_cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    # The statutory wage bases this run used, kept so a dispute can be settled
    # without re-deriving them.
    pf_wage_base: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    esi_wage_base: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    pf_employee: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    pf_employer: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    esi_employee: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    esi_employer: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    professional_tax: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    tds: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    # Slice 09 heads. LWF is zero in every month that is not a deduction month
    # for the state — zero, never a twelfth of the annual levy.
    lwf_employee: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    lwf_employer: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    vpf: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    # Anything payroll declined to decide on its own — a mid-period work
    # location change, say. A payslip carrying flags has been computed, but
    # something on it needs a human before the run is approved.
    review_flags: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # [{"code": "BASIC", "type": "earning", "amount": "50000.00",
    #   "rule_set_id": "...", "basis": "structure"}, ...]
    line_items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # Lines that came from other modules through `services/payroll/inputs.py` —
    # a loan EMI, a reimbursement, arrears, a referral bonus — each with the
    # source record it consumed. Kept apart from `line_items` on purpose: the
    # arrears engine diffs `line_items` against a structure recomputation, and a
    # one-off payment there would read as a component to recover.
    input_lines: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    # The income tax projection behind `tds`: regime, projected income, slabs,
    # deductions, months remaining, what was already deducted.
    tds_working: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: An off-cycle payslip for somebody the month's regular run already paid:
    #: input lines only, no structure, no days. Registers and returns merge it
    #: into the month rather than counting the person twice.
    is_supplementary: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    pdf_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[PayrollRun] = relationship(back_populates="payslips")


# ── Slice 09: the remaining statutory heads ─────────────────────────────────


class BonusStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    PAID = "paid"


class BonusComputation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Statutory bonus for one employee for one financial year.

    Bonus is annual, computed from a full year of attendance, and paid in a
    single month — so it does not belong on a payslip row. It gets its own
    record, with the working kept, because the Payment of Bonus register has to
    show how each figure was arrived at and a query eight months later must not
    have to re-derive it from configuration that may since have changed.

    `rule_set_id` pins the version the figure was computed against, exactly as
    a payroll run does.
    """

    __tablename__ = "bonus_computations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "financial_year", name="uq_bonus_employee_year"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    # "2025-2026"
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)

    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("payroll_rule_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )

    is_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ineligibility_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The wage the bonus was computed on: the lower of actual wages and the
    # calculation ceiling. Distinct from the wage that decided eligibility.
    eligible_wage: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    days_worked: Mapped[float] = mapped_column(Numeric(8, 2), default=0, nullable=False)
    total_working_days: Mapped[float] = mapped_column(Numeric(8, 2), default=0, nullable=False)
    percentage: Mapped[float] = mapped_column(Numeric(6, 3), default=0, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    working: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    status: Mapped[BonusStatus] = mapped_column(
        Enum(BonusStatus, name="bonus_status"), default=BonusStatus.DRAFT, nullable=False
    )

    # Bonus is usually paid once a year, before a festival, as its own
    # disbursement — so `paid_in_run_id` is nullable. It is set only where the
    # employer chose to ride a payroll run, and the register reads the payment
    # date from `paid_on` either way.
    paid_in_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payroll_runs.id", ondelete="SET NULL"), nullable=True
    )
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EsiCoveragePeriod(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Whether an employee was contributing to ESI at the start of a period.

    This table exists because of one rule: an employee whose wages cross the
    ESI ceiling mid-period keeps contributing until the end of that
    contribution period. Deciding that from the current month's wages alone is
    impossible — the answer depends on what was true in April (or October), and
    a payroll run in February cannot reconstruct it from the current salary.

    So the state is recorded when a period opens and read for every month in
    it. One row per employee per contribution period.
    """

    __tablename__ = "esi_coverage_periods"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", "period_label", name="uq_esi_coverage_period"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    # "2025-APR-SEP" — the label from the rule set's configured periods, so a
    # tenant on non-standard periods is representable.
    period_label: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    period_start_month: Mapped[int] = mapped_column(Integer, nullable=False)
    period_end_month: Mapped[int] = mapped_column(Integer, nullable=False)

    was_contributing_at_start: Mapped[bool] = mapped_column(Boolean, nullable=False)
    opening_wage: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    # Set when wages cross the ceiling part-way through; contributions
    # nonetheless continue to the period end.
    ceiling_crossed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class EmployeePfElection(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An employee's PF ceiling election and voluntary contribution.

    Effective-dated like every other payroll input: an employee raising their
    VPF in October must not alter what September's payslip said.
    """

    __tablename__ = "employee_pf_elections"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # PF on full wages rather than capped at the statutory ceiling. Raises the
    # employer's contribution too, so it needs employer agreement.
    pf_on_actual_wage: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Exactly one of these, or neither. An additional *employee* contribution;
    # it never moves the employer's.
    vpf_percentage: Mapped[float | None] = mapped_column(Numeric(6, 3), nullable=True)
    vpf_fixed_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    declared_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
