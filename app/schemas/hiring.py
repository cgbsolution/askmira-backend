"""Hiring contracts: requisitions, candidates, the pipeline.

Money crosses the wire as strings (`Decimal` serialises that way), so a budget
never becomes a float between the database and the screen.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.hiring import CandidateStage, RequisitionStatus

# ── Requisitions ────────────────────────────────────────────────────────────


class RequisitionCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    #: Optional: generated when omitted, so nobody has to invent a numbering scheme.
    code: str | None = Field(default=None, min_length=2, max_length=32, pattern=r"^[A-Za-z0-9-]+$")
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    #: A manager may only raise for themselves; HR may name anyone.
    hiring_manager_id: uuid.UUID | None = None
    recruiter_id: uuid.UUID | None = None
    replacement_for_employee_id: uuid.UUID | None = None
    openings: int = Field(default=1, ge=1, le=500)
    employment_type: Literal["full_time", "part_time", "contract", "intern"] = "full_time"
    grade: str | None = Field(default=None, max_length=32)
    experience_min_years: int | None = Field(default=None, ge=0, le=60)
    experience_max_years: int | None = Field(default=None, ge=0, le=60)
    budget_ctc_min: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    budget_ctc_max: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    justification: str | None = Field(default=None, max_length=4000)
    description: str | None = Field(default=None, max_length=20000)
    skills: list[str] = Field(default_factory=list, max_length=50)
    target_joining_date: date | None = None
    target_close_date: date | None = None

    @model_validator(mode="after")
    def _ranges(self) -> RequisitionCreate:
        if (
            self.experience_min_years is not None
            and self.experience_max_years is not None
            and self.experience_min_years > self.experience_max_years
        ):
            raise ValueError("Minimum experience is above the maximum.")
        if (
            self.budget_ctc_min is not None
            and self.budget_ctc_max is not None
            and self.budget_ctc_min > self.budget_ctc_max
        ):
            raise ValueError("The budget floor is above the ceiling; no offer could fit it.")
        return self

    @field_validator("skills")
    @classmethod
    def _skills(cls, value: list[str]) -> list[str]:
        cleaned = [s.strip()[:64] for s in value if s and s.strip()]
        return list(dict.fromkeys(cleaned))


class RequisitionUpdate(BaseModel):
    """Every field optional; only a draft or a rejected requisition is editable."""

    title: str | None = Field(default=None, min_length=2, max_length=255)
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    hiring_manager_id: uuid.UUID | None = None
    recruiter_id: uuid.UUID | None = None
    replacement_for_employee_id: uuid.UUID | None = None
    openings: int | None = Field(default=None, ge=1, le=500)
    employment_type: Literal["full_time", "part_time", "contract", "intern"] | None = None
    grade: str | None = Field(default=None, max_length=32)
    experience_min_years: int | None = Field(default=None, ge=0, le=60)
    experience_max_years: int | None = Field(default=None, ge=0, le=60)
    budget_ctc_min: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    budget_ctc_max: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    justification: str | None = Field(default=None, max_length=4000)
    description: str | None = Field(default=None, max_length=20000)
    #: The description being saved started from an AI draft.
    description_ai_assisted: bool | None = None
    skills: list[str] | None = Field(default=None, max_length=50)
    target_joining_date: date | None = None
    target_close_date: date | None = None


class RequisitionStatusChange(BaseModel):
    status: Literal["on_hold", "open", "closed", "cancelled"]
    reason: str = Field(min_length=3, max_length=2000)


class RequisitionSummary(BaseModel):
    """What anybody who may list a requisition sees. No budget."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    title: str
    department_id: uuid.UUID | None
    location_id: uuid.UUID | None
    hiring_manager_id: uuid.UUID | None
    recruiter_id: uuid.UUID | None
    #: Who raised it; with the two above, who may open its detail outside HR.
    raised_by_user_id: uuid.UUID | None = None
    openings: int
    open_vacancies: int
    employment_type: str
    status: RequisitionStatus
    target_close_date: date | None
    created_at: datetime


class ApprovalStepRead(BaseModel):
    sequence: int
    role: str
    label: str | None
    reason: str | None
    decision: str
    decided_by: str | None
    decided_at: datetime | None
    note: str | None
    flags_shown: list[dict[str, Any]] = Field(default_factory=list)


class RequisitionDetail(RequisitionSummary):
    grade: str | None
    experience_min_years: int | None
    experience_max_years: int | None
    budget_ctc_min: Decimal | None
    budget_ctc_max: Decimal | None
    justification: str | None
    description: str | None
    description_ai_assisted: bool = False
    #: Each published wording's version and date; the bodies stay server-side.
    jd_versions: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[str]
    replacement_for_employee_id: uuid.UUID | None
    target_joining_date: date | None
    raised_by_user_id: uuid.UUID | None
    approved_at: datetime | None
    filled_count: int
    approved_headcount: int | None
    hiring_manager_name: str | None = None
    recruiter_name: str | None = None
    department_name: str | None = None
    location_name: str | None = None
    approvals: list[ApprovalStepRead] = Field(default_factory=list)
    budget_flags: list[dict[str, Any]] = Field(default_factory=list)
    can_edit: bool = False
    can_submit: bool = False
    can_decide: bool = False
    decide_blocked_reason: str | None = None
    candidate_count: int = 0


class PendingDecision(BaseModel):
    kind: Literal["requisition", "offer"]
    id: uuid.UUID
    code: str
    title: str
    step: int
    step_role: str
    step_reason: str | None
    flags: list[dict[str, Any]] = Field(default_factory=list)
    submitted_at: datetime | None


# ── Candidates ──────────────────────────────────────────────────────────────


class CandidateCreate(BaseModel):
    """A candidate HR enters by hand. Recorded with a `manual` source."""

    requisition_id: uuid.UUID
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=32, pattern=r"^[0-9+()\-\s]{6,32}$")
    current_company: str | None = Field(default=None, max_length=255)
    total_experience_years: Decimal | None = Field(default=None, ge=0, le=60, decimal_places=1)
    current_ctc: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    expected_ctc: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    #: Where they came from, in words — an agency, an event. Kept on the application.
    source_note: str | None = Field(default=None, max_length=500)


class CandidateSummary(BaseModel):
    id: uuid.UUID
    requisition_id: uuid.UUID
    requisition_code: str | None = None
    full_name: str
    email: str
    phone: str | None
    stage: CandidateStage
    source: str | None
    total_experience_years: Decimal | None
    notice_period_days: int | None
    converted_employee_id: uuid.UUID | None
    created_at: datetime
    allowed_moves: list[str] = Field(default_factory=list)
    possible_duplicate: bool = False
    tags: list[str] = Field(default_factory=list)


class CandidateTags(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=40)


class TalentPoolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class TalentPoolUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class TalentPoolAdd(BaseModel):
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class StageMove(BaseModel):
    to_stage: CandidateStage
    reason: str | None = Field(default=None, max_length=2000)
    #: For a rejection: email the candidate (after a day's hold). Off for, say,
    #: a duplicate profile nobody should write to.
    notify_candidate: bool = True


class CandidateMessageCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    message: str = Field(min_length=1, max_length=10_000)
    #: Add a fresh portal link, so they can reply with documents or see progress.
    include_portal_link: bool = False


class StageMoveResult(BaseModel):
    candidate_id: uuid.UUID
    from_stage: CandidateStage
    to_stage: CandidateStage
    allowed_moves: list[str]
