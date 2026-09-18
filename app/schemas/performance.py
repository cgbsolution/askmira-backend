"""Contracts for reviews, competencies, calibration, cycle configuration, PIPs and
increments.

Ratings, weights, scores and money are `Decimal` and serialise as strings.
Nothing here trusts a person id from the request to decide whose record is
touched — the services resolve that through `app.core.permissions`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.appraisal import (
    AcknowledgementType,
    AppraisalStatus,
    CycleStatus,
    CycleType,
    StageRole,
)
from app.models.calibration import CalibrationMode, CalibrationScope, SessionStatus
from app.models.competency import CompetencyCategory, ScaleType
from app.models.cycle_config import AutoAdvanceBehaviour, ProbationOutcome
from app.models.performance_flow import (
    EmployeeInputStateEnum,
    MilestoneStatusEnum,
    OverageModeEnum,
)

# ═══════════════════════════════════════════════════════════════════════════
# Cycles and the appraisal chain
# ═══════════════════════════════════════════════════════════════════════════


class StageCreate(BaseModel):
    sequence: int = Field(ge=1, le=20)
    role: StageRole
    label: str = Field(min_length=1, max_length=128)
    due_date: date | None = None
    is_mandatory: bool = True
    can_reopen: bool = False
    sees_previous_entries: bool = True
    #: Usually false: manager comments visible mid-cycle change what managers
    #: write, which is not the same as changing what they think.
    visible_to_employee_before_release: bool = False


class CycleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    cycle_type: CycleType
    period_start: date
    period_end: date
    eligibility_rule: dict[str, Any] = Field(default_factory=dict)
    escalation_after_days: int = Field(default=7, ge=1, le=90)
    stages: list[StageCreate] = Field(min_length=1, max_length=20)


class CycleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    period_start: date | None = None
    period_end: date | None = None
    eligibility_rule: dict[str, Any] | None = None
    escalation_after_days: int | None = Field(default=None, ge=1, le=90)
    #: Replaces every stage. Only while the cycle is a draft.
    stages: list[StageCreate] | None = Field(default=None, min_length=1, max_length=20)


class CycleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    cycle_type: CycleType
    status: CycleStatus
    period_start: date
    period_end: date
    eligibility_rule: dict[str, Any]
    escalation_after_days: int
    launched_at: datetime | None
    closed_at: datetime | None


class StageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence: int
    role: StageRole
    label: str
    due_date: date | None
    open_date: date | None
    close_date: date | None
    grace_days: int
    auto_advance: AutoAdvanceBehaviour
    is_mandatory: bool
    can_reopen: bool
    sees_previous_entries: bool
    visible_to_employee_before_release: bool


class AppraisalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cycle_id: uuid.UUID
    employee_id: uuid.UUID
    current_stage_id: uuid.UUID | None
    status: AppraisalStatus
    final_rating: str | None
    final_score: Decimal | None
    completed_at: datetime | None
    released_at: datetime | None
    acknowledged_at: datetime | None
    acknowledgement_type: AcknowledgementType | None
    objection: str | None
    reopened_count: int


class CompetencyRatingIn(BaseModel):
    competency_id: uuid.UUID
    rating: Decimal | None = Field(default=None, ge=0)
    #: False removes it from the calculation entirely. Scoring it zero instead
    #: would penalise the employee for a thing they were never expected to do.
    is_applicable: bool = True
    not_applicable_reason: str | None = Field(default=None, max_length=1000)
    comment: str | None = Field(default=None, max_length=4000)


class ValueRatingIn(BaseModel):
    value_code: str = Field(min_length=1, max_length=64)
    rating: Decimal | None = Field(default=None, ge=0)
    comment: str | None = Field(default=None, max_length=4000)


class GoalCommentIn(BaseModel):
    goal_id: uuid.UUID
    comment: str = Field(max_length=4000)


class ReviewRatings(BaseModel):
    """What one step records. Goal progress is not rated here — it is measured."""

    competencies: list[CompetencyRatingIn] = Field(default_factory=list, max_length=100)
    values: list[ValueRatingIn] = Field(default_factory=list, max_length=50)
    goals: list[GoalCommentIn] = Field(default_factory=list, max_length=100)


class SubmitRequest(BaseModel):
    ratings: dict[str, Any] = Field(default_factory=dict)
    comments: str | None = Field(default=None, max_length=8000)


class ScorePreviewRequest(BaseModel):
    ratings: dict[str, Any] = Field(default_factory=dict)


class ReturnRequest(BaseModel):
    target_stage_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=4000)


class ReleaseRequest(BaseModel):
    final_rating: str | None = Field(default=None, max_length=64)


class AcknowledgeRequest(BaseModel):
    """Disagreement is a valid close."""

    acknowledgement: AcknowledgementType
    objection: str | None = Field(default=None, max_length=8000)


class ReopenRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
    downstream_impact: list[dict[str, Any]] = Field(default_factory=list)
    acknowledge_impact: bool = False


class DelegationCreate(BaseModel):
    cycle_id: uuid.UUID
    #: Omitted means "my own steps". Only HR may delegate somebody else's.
    from_user_id: uuid.UUID | None = None
    to_user_id: uuid.UUID
    effective_from: date
    #: Required, and bounded: a standing delegation with no end date is how a
    #: manager who returned from leave three months ago still is not rating.
    effective_to: date
    reason: str | None = Field(default=None, max_length=2000)


class ReminderRequest(BaseModel):
    #: One stage, or every stage with something pending.
    stage_id: uuid.UUID | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Rating scales, competencies and templates
# ═══════════════════════════════════════════════════════════════════════════


class ScaleLevelCreate(BaseModel):
    value: Decimal
    label: str = Field(min_length=1, max_length=128)
    #: Required. It is what makes one manager's 4 the same as another's.
    description: str = Field(min_length=1, max_length=2000)
    colour_hint: str | None = Field(default=None, max_length=16)
    is_default: bool = False


class ScaleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    scale_type: ScaleType
    min_value: Decimal
    max_value: Decimal
    allow_decimals: bool = False
    levels: list[ScaleLevelCreate] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def levels_sit_inside_the_scale(self) -> ScaleCreate:
        if self.max_value <= self.min_value:
            raise ValueError("The scale's maximum is not above its minimum.")
        outside = [
            str(level.value)
            for level in self.levels
            if not (self.min_value <= level.value <= self.max_value)
        ]
        if outside:
            raise ValueError(f"These levels fall outside the scale's range: {', '.join(outside)}.")
        values = [level.value for level in self.levels]
        if len(set(values)) != len(values):
            raise ValueError("Two levels share a value.")
        return self


class ScaleLevelRead(BaseModel):
    value: Decimal
    label: str
    description: str
    colour_hint: str | None
    is_default: bool


class ScaleRead(BaseModel):
    id: uuid.UUID
    name: str
    scale_type: ScaleType
    min_value: Decimal
    max_value: Decimal
    allow_decimals: bool
    active: bool
    levels: list[ScaleLevelRead] = Field(default_factory=list)


class IndicatorCreate(BaseModel):
    scale_value: Decimal
    behaviour_description: str = Field(min_length=1, max_length=2000)


class CompetencyCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=128)
    category: CompetencyCategory
    scale_id: uuid.UUID
    description: str | None = Field(default=None, max_length=4000)
    #: Required. Without indicators "Collaboration: 3" is noise.
    indicators: list[IndicatorCreate] = Field(min_length=2, max_length=20)
    linked_value_codes: list[str] = Field(default_factory=list, max_length=20)


class CompetencyUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=4000)
    active: bool | None = None
    linked_value_codes: list[str] | None = Field(default=None, max_length=20)


class IndicatorRead(BaseModel):
    scale_value: Decimal
    behaviour: str


class CompetencyRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    category: CompetencyCategory
    scale_id: uuid.UUID
    description: str | None
    active: bool
    indicators: list[IndicatorRead] = Field(default_factory=list)
    linked_value_codes: list[str] = Field(default_factory=list)


class TemplateItemCreate(BaseModel):
    competency_id: uuid.UUID
    weight: Decimal = Field(gt=0, le=100)
    is_mandatory: bool = True
    display_order: int = Field(default=0, ge=0, le=1000)


class CompetencyTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    applicable_roles: list[str] = Field(default_factory=list)
    applicable_grades: list[str] = Field(default_factory=list)
    items: list[TemplateItemCreate] = Field(min_length=1, max_length=50)


class TemplateItemRead(BaseModel):
    competency_id: uuid.UUID
    competency_code: str
    competency_name: str
    weight: Decimal
    is_mandatory: bool
    display_order: int


class CompetencyTemplateRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    applicable_roles: list[str]
    applicable_grades: list[str]
    active: bool
    items: list[TemplateItemRead]
    total_weight: Decimal


class BandSpec(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    lower: Decimal
    upper: Decimal
    description: str | None = Field(default=None, max_length=1000)


class AppraisalTemplateCreate(BaseModel):
    cycle_id: uuid.UUID
    rating_scale_id: uuid.UUID
    competency_template_id: uuid.UUID | None = None
    goal_weight: Decimal = Field(ge=0, le=100)
    competency_weight: Decimal = Field(ge=0, le=100)
    values_weight: Decimal = Field(default=Decimal(0), ge=0, le=100)
    score_precision: int = Field(default=2, ge=0, le=4)
    bands: list[BandSpec] = Field(min_length=1, max_length=20)


class AppraisalTemplateRead(BaseModel):
    id: uuid.UUID
    cycle_id: uuid.UUID
    rating_scale_id: uuid.UUID
    rating_scale_name: str | None
    competency_template_id: uuid.UUID | None
    competency_template_name: str | None
    goal_weight: Decimal
    competency_weight: Decimal
    values_weight: Decimal
    score_precision: int
    bands: list[dict[str, Any]]


class CompetencyRatingInput(BaseModel):
    code: str
    name: str
    weight: Decimal = Field(ge=0)
    rating: Decimal | None = None
    is_applicable: bool = True
    not_applicable_reason: str | None = None


class ScoreRequest(BaseModel):
    goal_score: Decimal = Field(ge=0, le=100)
    competency_ratings: list[CompetencyRatingInput] = Field(default_factory=list)
    scale_max: Decimal = Field(gt=0)
    values_score: Decimal | None = Field(default=None, ge=0, le=100)


# ═══════════════════════════════════════════════════════════════════════════
# Calibration
# ═══════════════════════════════════════════════════════════════════════════


class TargetSpec(BaseModel):
    rating_value: str = Field(min_length=1, max_length=64)
    target_percent: Decimal = Field(ge=0, le=100)
    tolerance_percent: Decimal = Field(default=Decimal(0), ge=0, le=100)


class PolicyCreate(BaseModel):
    cycle_id: uuid.UUID
    mode: CalibrationMode = CalibrationMode.ADVISORY
    scope: CalibrationScope = CalibrationScope.DEPARTMENT
    #: Below this a group is reported but not enforced.
    min_group_size: int = Field(default=8, ge=1, le=1000)
    disclose_calibration_to_employee: bool = False
    targets: list[TargetSpec] = Field(default_factory=list, max_length=20)


class TargetRead(BaseModel):
    rating_value: str
    target_percent: Decimal
    tolerance_percent: Decimal


class PolicyRead(BaseModel):
    id: uuid.UUID
    cycle_id: uuid.UUID
    mode: CalibrationMode
    scope: CalibrationScope
    min_group_size: int
    disclose_calibration_to_employee: bool
    targets: list[TargetRead] = Field(default_factory=list)


class SessionCreate(BaseModel):
    cycle_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    scope: CalibrationScope
    scope_value: str = Field(min_length=1, max_length=128)
    facilitator_user_id: uuid.UUID | None = None
    #: Who was in the room — part of the same question as "who changed it".
    participant_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    scheduled_at: datetime | None = None


class SessionRead(BaseModel):
    id: uuid.UUID
    cycle_id: uuid.UUID
    cycle_name: str | None = None
    name: str
    scope: CalibrationScope
    scope_value: str
    status: SessionStatus
    facilitator_user_id: uuid.UUID | None
    facilitator_name: str | None = None
    participant_user_ids: list[uuid.UUID] = Field(default_factory=list)
    scheduled_at: datetime | None
    locked_at: datetime | None


class ChangeRequest(BaseModel):
    appraisal_id: uuid.UUID
    new_rating: str = Field(min_length=1, max_length=64)
    new_score: Decimal | None = Field(default=None, ge=0, le=100)
    #: Mandatory here and again in the service.
    justification: str = Field(min_length=1, max_length=4000)


class ChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    appraisal_id: uuid.UUID
    employee_id: uuid.UUID
    previous_rating: str | None
    new_rating: str
    previous_score: Decimal | None
    new_score: Decimal | None
    changed_by_user_id: uuid.UUID | None
    changed_at: datetime
    justification: str
    applied_on_lock: bool


class CalibrationReopenRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
    hr_override: bool = False


class SessionReopenRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


# ═══════════════════════════════════════════════════════════════════════════
# Cycle configuration
# ═══════════════════════════════════════════════════════════════════════════


class RuleCreate(BaseModel):
    cycle_id: uuid.UUID
    priority: int = Field(ge=1, le=1000)
    template_id: uuid.UUID
    conditions: dict[str, Any] = Field(default_factory=dict)
    label: str | None = Field(default=None, max_length=128)


class RuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cycle_id: uuid.UUID
    priority: int
    template_id: uuid.UUID
    conditions: dict[str, Any]
    label: str | None


class StageWindowUpdate(BaseModel):
    open_date: date | None = None
    close_date: date | None = None
    due_date: date | None = None
    grace_days: int = Field(default=0, ge=0, le=90)
    #: No default on purpose: what happens when a window closes is a decision.
    auto_advance: AutoAdvanceBehaviour


class CloneRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    period_start: date
    period_end: date


class CloseRequest(BaseModel):
    acknowledge_incomplete: bool = False
    #: Closure publishes completed ratings to employees.
    release_completed: bool = True


class ProbationCreate(BaseModel):
    employee_id: uuid.UUID
    probation_end_date: date
    review_due_date: date


class ProbationRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str | None = None
    probation_end_date: date
    review_due_date: date
    outcome: ProbationOutcome | None
    outcome_recorded_at: datetime | None
    extended_to: date | None
    extends_review_id: uuid.UUID | None
    notes: str | None


class ProbationOutcomeRequest(BaseModel):
    outcome: ProbationOutcome
    new_probation_end_date: date | None = None
    notes: str | None = Field(default=None, max_length=4000)


class ProbationScheduleRequest(BaseModel):
    #: Schedule a review for every probation ending within this many days.
    horizon_days: int = Field(default=30, ge=1, le=180)
    #: How long before the probation end date the review falls due.
    lead_days: int = Field(default=7, ge=0, le=60)


# ═══════════════════════════════════════════════════════════════════════════
# PIPs and increments
# ═══════════════════════════════════════════════════════════════════════════


class MilestoneIn(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    target_date: date
    #: Required. "Improve communication" is not a standard anybody can meet.
    measurable_criteria: str = Field(min_length=1, max_length=2000)


class PipIn(BaseModel):
    employee_id: uuid.UUID
    start_date: date
    end_date: date
    reason: str = Field(min_length=1, max_length=4000)
    expectations: str = Field(min_length=1, max_length=8000)
    #: Required. A plan the employer contributed nothing to is a notice period.
    support_provided: str = Field(min_length=1, max_length=8000)
    milestones: list[MilestoneIn] = Field(min_length=1, max_length=20)
    appraisal_id: uuid.UUID | None = None


class CheckInIn(BaseModel):
    scheduled_for: date
    held_on: date
    manager_comments: str = Field(min_length=1, max_length=8000)
    #: `not_asked` is refused by the engine. `declined` needs a reason.
    employee_input_state: EmployeeInputStateEnum
    employee_comments: str = Field(default="", max_length=8000)
    decline_reason: str = Field(default="", max_length=2000)
    milestone_id: uuid.UUID | None = None


class ClosePipIn(BaseModel):
    outcome: str = Field(pattern="^(successful|extended|unsuccessful|abandoned)$")
    reason: str = Field(default="", max_length=4000)
    new_end_date: date | None = None
    #: Proceeding to an exit on a plan with gaps. A deliberate act, recorded.
    acknowledge_weak_trail: bool = False


class MilestoneAssessIn(BaseModel):
    status: MilestoneStatusEnum
    note: str = Field(min_length=1, max_length=4000)


class EmployeeStatementIn(BaseModel):
    statement: str = Field(min_length=1, max_length=8000)
    check_in_id: uuid.UUID | None = None


class BandIn(BaseModel):
    rating_label: str = Field(min_length=1, max_length=64)
    min_percentage: Decimal = Field(ge=0, le=200)
    max_percentage: Decimal = Field(ge=0, le=200)
    recommended_percentage: Decimal = Field(ge=0, le=200)


class IncrementCycleIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    effective_from: date
    #: The annual cost of the *rises*, not the payroll.
    budget_total: Decimal = Field(ge=0)
    bands: list[BandIn] = Field(min_length=1, max_length=20)
    budget_by_department: dict[str, Decimal] = Field(default_factory=dict)
    overage_mode: OverageModeEnum = OverageModeEnum.ESCALATE
    letter_release_on: date | None = None
    linked_appraisal_cycle_id: uuid.UUID | None = None


class RecommendationIn(BaseModel):
    cycle_id: uuid.UUID
    employee_id: uuid.UUID
    #: Taken from the linked review where there is one; required otherwise.
    rating_label: str | None = Field(default=None, min_length=1, max_length=64)
    #: Taken from the current salary assignment where there is one. Only HR may
    #: supply it for somebody with no assignment.
    current_ctc: Decimal | None = Field(default=None, gt=0)
    percentage: Decimal = Field(ge=0, le=200)
    justification: str | None = Field(default=None, max_length=4000)
    promotion_to_grade: str | None = Field(default=None, max_length=32)
    band_midpoint: Decimal | None = Field(default=None, gt=0)


class DecisionIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=2000)


class PromotionIn(BaseModel):
    recommendation_id: uuid.UUID
    to_position_id: uuid.UUID | None = None
    effective_from: date


class ReleaseIn(BaseModel):
    #: A deliberate act with the outstanding names recorded, not a default.
    force_partial: bool = False
