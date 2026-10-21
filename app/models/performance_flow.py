"""PIPs, increment cycles and promotions (slice 35).

═══════════════════════════════════════════════════════════════════════════════
`pip_check_ins` HAS A COLUMN FOR THE EMPLOYEE'S OWN WORDS, AND A STATE
═══════════════════════════════════════════════════════════════════════════════

Not a nullable free-text field that nobody fills in. `employee_input_state`
distinguishes *given*, *declined* and *not asked*, because a bare empty comment
is indistinguishable from never having asked — and the tribunal's first question
is what the employee said at the time.

═══════════════════════════════════════════════════════════════════════════════
`promotion_records` STORES THE IDS OF ALL FOUR THINGS IT PRODUCED
═══════════════════════════════════════════════════════════════════════════════

`salary_revision_id`, `lifecycle_event_id`, `issued_letter_id` and the grade
change together. That is what makes a half-applied promotion **detectable**: a
row with three of four filled is a promotion that did not complete, and without
the row nobody would find out until somebody reconciled headcount or the
employee read their payslip.

The transaction is what makes it not happen. The columns are what make it
visible if it ever does.

═══════════════════════════════════════════════════════════════════════════════
LETTERS CARRY A CYCLE-LEVEL RELEASE DATE
═══════════════════════════════════════════════════════════════════════════════

`increment_cycles.letter_release_on`, not a per-recommendation date. Released as
each approval lands, the first person to hear is whoever has the fastest
approver and the last spends a fortnight watching colleagues get letters.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class PipStatusEnum(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    EXTENDED = "extended"
    SUCCESSFUL = "successful"
    UNSUCCESSFUL = "unsuccessful"
    #: Stopped without an outcome. Honest, and distinct from a failure.
    ABANDONED = "abandoned"


class MilestoneStatusEnum(str, enum.Enum):
    PENDING = "pending"
    MET = "met"
    NOT_MET = "not_met"
    WAIVED = "waived"


class EmployeeInputStateEnum(str, enum.Enum):
    """Whether the employee's own account was captured, and if not, why.

    `DECLINED` is a state people genuinely occupy and has to be representable.
    Otherwise a manager faced with a required field types something on the
    employee's behalf, which is worse than a recorded refusal.
    """

    GIVEN = "given"
    DECLINED = "declined"
    NOT_ASKED = "not_asked"


class IncrementCycleStatusEnum(str, enum.Enum):
    DRAFT = "draft"
    OPEN = "open"
    APPROVALS = "approvals"
    APPROVED = "approved"
    RELEASED = "released"
    CANCELLED = "cancelled"


class OverageModeEnum(str, enum.Enum):
    BLOCK = "block"
    ESCALATE = "escalate"


class RecommendationStatusEnum(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    ESCALATED = "escalated"
    APPROVED = "approved"
    REJECTED = "rejected"


# ══════════════════════════════════════════════════════════════════════════════
# PIP
# ══════════════════════════════════════════════════════════════════════════════


class Pip(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One performance improvement plan.

    `expectations` and `support_provided` are both **not nullable**. A plan with
    no written expectations sets no standard anybody can meet or fail, and one
    with no support recorded is a notice period with paperwork — which is how it
    reads to anybody reviewing it later.
    """

    __tablename__ = "pips"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_pip_dates_ordered"),
        Index("ix_pips_employee", "tenant_id", "employee_id", "start_date"),
        Index("ix_pips_status", "tenant_id", "status"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    initiated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Nullable: PIPs also arise outside appraisal cycles.
    appraisal_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("appraisals.id", ondelete="SET NULL"),
        nullable=True,
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expectations: Mapped[str] = mapped_column(Text, nullable=False)
    support_provided: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PipStatusEnum] = mapped_column(
        Enum(PipStatusEnum, name="pip_status"),
        default=PipStatusEnum.DRAFT,
        nullable=False,
    )
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_recorded_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Set where somebody proceeded to an unsuccessful outcome knowing the trail
    #: had gaps. Recording that decision is itself the useful fact.
    weak_trail_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: The gaps as they stood when the outcome was recorded, frozen. Recomputing
    #: them later would describe a plan that has since been edited.
    trail_gaps_at_outcome: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    exit_record_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("exit_records.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Extensions append here rather than overwriting the dates, so the original
    #: period stays visible.
    extensions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)


class PipMilestone(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One measurable expectation, with a date.

    `measurable_criteria` is not nullable. "Improve communication" is not a
    standard anybody can meet or fail, and a plan built from them cannot be
    defended.
    """

    __tablename__ = "pip_milestones"
    __table_args__ = (Index("ix_pip_milestones_pip", "tenant_id", "pip_id", "target_date"),)

    pip_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("pips.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    measurable_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[MilestoneStatusEnum] = mapped_column(
        Enum(MilestoneStatusEnum, name="pip_milestone_status"),
        default=MilestoneStatusEnum.PENDING,
        nullable=False,
    )
    assessed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    assessment_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class PipCheckIn(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One scheduled conversation, with both sides of it.

    `employee_input_state` is the column that matters. A nullable comment field
    that nobody fills in cannot be distinguished from an employee who was never
    asked, and that distinction is the whole evidential value of the record.
    """

    __tablename__ = "pip_check_ins"
    __table_args__ = (Index("ix_pip_check_ins_pip", "tenant_id", "pip_id", "scheduled_for"),)

    pip_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("pips.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("pip_milestones.id", ondelete="SET NULL"),
        nullable=True,
    )
    scheduled_for: Mapped[date] = mapped_column(Date, nullable=False)
    held_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    manager_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    employee_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    employee_input_state: Mapped[EmployeeInputStateEnum] = mapped_column(
        Enum(EmployeeInputStateEnum, name="pip_employee_input_state"),
        default=EmployeeInputStateEnum.NOT_ASKED,
        nullable=False,
    )
    #: In the employee's words, or the manager's account of what they said.
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: When the employee's own text was saved, which is the thing that makes it
    #: contemporaneous rather than reconstructed.
    employee_comment_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PipEmployeeStatement(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The employee's own account, in their own words, written by them.

    A check-in's `employee_comments` is what the manager recorded the employee
    saying. This is different: text the person on the plan typed themselves,
    timestamped when they saved it. Insert-only - a later statement is a new
    row, so what they said at the time is never overwritten by what they said
    afterwards.
    """

    __tablename__ = "pip_employee_statements"
    __table_args__ = (
        Index("ix_pip_employee_statements_pip", "tenant_id", "pip_id", "recorded_at"),
    )

    pip_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("pips.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The check-in this responds to, where it responds to one.
    check_in_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("pip_check_ins.id", ondelete="SET NULL"),
        nullable=True,
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# Increments
# ══════════════════════════════════════════════════════════════════════════════


class IncrementCycle(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One increment round, its budget and its letter release date."""

    __tablename__ = "increment_cycles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_increment_cycle_name"),
        CheckConstraint("budget_total >= 0", name="ck_increment_budget_nonneg"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: The annual cost of the *rises*, not the payroll. Charging whole salaries
    #: against an increment budget only shows up when a department appears 900%
    #: over.
    budget_total: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)
    #: `{department_id: amount}`.
    budget_by_department: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[IncrementCycleStatusEnum] = mapped_column(
        Enum(IncrementCycleStatusEnum, name="increment_cycle_status"),
        default=IncrementCycleStatusEnum.DRAFT,
        nullable=False,
    )
    overage_mode: Mapped[OverageModeEnum] = mapped_column(
        Enum(OverageModeEnum, name="increment_overage_mode"),
        default=OverageModeEnum.ESCALATE,
        nullable=False,
    )
    linked_appraisal_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("appraisal_cycles.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Cycle-level, not per recommendation. All letters go together.
    letter_release_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    letters_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    partial_release_acknowledged: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )


class IncrementMatrixRow(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The percentage band one rating earns.

    The rating drives the band; the manager positions within it. A single
    percentage per rating removes the manager's judgement, and a band with no
    recommendation leaves every manager inventing their own anchor.
    """

    __tablename__ = "increment_matrix_rows"
    __table_args__ = (
        UniqueConstraint("cycle_id", "rating_label", name="uq_increment_band_per_rating"),
        CheckConstraint("max_percentage >= min_percentage", name="ck_increment_band_ordered"),
        CheckConstraint("min_percentage >= 0", name="ck_increment_band_nonneg"),
        CheckConstraint(
            "recommended_percentage BETWEEN min_percentage AND max_percentage",
            name="ck_increment_recommendation_inside_band",
        ),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("increment_cycles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rating_label: Mapped[str] = mapped_column(String(64), nullable=False)
    min_percentage: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    max_percentage: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    recommended_percentage: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)


class IncrementRecommendation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One proposed increment, with the equity view captured at the time.

    `equity_snapshot` is stored rather than recomputed: the comparison group
    changes as people join and leave, and the useful record is what the manager
    was actually shown when they chose the number.
    """

    __tablename__ = "increment_recommendations"
    __table_args__ = (
        UniqueConstraint("cycle_id", "employee_id", name="uq_one_recommendation_per_cycle"),
        CheckConstraint("current_ctc > 0", name="ck_recommendation_ctc_positive"),
        Index("ix_recommendations_dept", "tenant_id", "cycle_id", "department_id"),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("increment_cycles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
    current_ctc: Mapped[float] = mapped_column(Numeric(16, 2), nullable=False)
    rating_label: Mapped[str] = mapped_column(String(64), nullable=False)
    recommended_percentage: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    recommended_ctc: Mapped[float] = mapped_column(Numeric(16, 2), nullable=False)
    #: The annual cost of the rise. This is what consumes the budget.
    increase_amount: Mapped[float] = mapped_column(Numeric(16, 2), nullable=False)
    within_band: Mapped[bool] = mapped_column(Boolean, nullable=False)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[RecommendationStatusEnum] = mapped_column(
        Enum(RecommendationStatusEnum, name="increment_recommendation_status"),
        default=RecommendationStatusEnum.DRAFT,
        nullable=False,
    )
    #: Append-only list of {actor, decision, at, note}.
    approvals: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    requires_escalation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    overage_amount: Mapped[float] = mapped_column(Numeric(16, 2), default=0, nullable=False)
    promotion_to_grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: What the manager was shown at the moment they chose the number.
    equity_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What releasing the cycle produced for this person: the new salary record
    # and the increment letter. Set once; a second release finds them and skips.
    salary_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employee_salaries.id", ondelete="SET NULL"),
        nullable=True,
    )
    letter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("issued_letters.id", ondelete="SET NULL"),
        nullable=True,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PromotionRecord(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A promotion, with the id of each of the four things it produced.

    A row with three of four filled is a promotion that did not complete. The
    transaction is what makes that not happen; these columns are what make it
    visible if it ever does.
    """

    __tablename__ = "promotion_records"
    __table_args__ = (
        Index("ix_promotions_employee", "tenant_id", "employee_id", "effective_from"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("increment_recommendations.id", ondelete="SET NULL"),
        nullable=True,
    )
    from_grade: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_grade: Mapped[str] = mapped_column(String(32), nullable=False)
    from_position_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
    )
    to_position_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
    )
    current_ctc: Mapped[float] = mapped_column(Numeric(16, 2), nullable=False)
    new_ctc: Mapped[float] = mapped_column(Numeric(16, 2), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)

    # The four things. All four or the promotion did not complete.
    #
    # Named for what it holds. The lifecycle cascade's own column is called
    # `salary_revision_id` and points at `employee_salaries` - the *new
    # assignment*, not a `salary_revisions` row - and copying that name here
    # would propagate a misleading one into a second table.
    employee_salary_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employee_salaries.id", ondelete="SET NULL"),
        nullable=True,
    )
    lifecycle_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("lifecycle_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    issued_letter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("issued_letters.id", ondelete="SET NULL"),
        nullable=True,
    )
    grade_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    is_backdated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    arrears_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
