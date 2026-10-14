"""Onboarding contracts: checklists, plans, tasks, and the joiner's own view.

Every figure a screen shows — progress, overdue counts, which phase a task
sits in — is computed by the service and sent here, so the portal never
derives one of its own.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.onboarding import (
    OnboardingOwnerType,
    OnboardingPlanStatus,
    OnboardingTaskStatus,
    OnboardingTaskType,
)
from app.schemas.documents import DocumentCategory

#: Where a task sits relative to the joining date.
Phase = Literal["pre_joining", "week_1", "later"]

# ══════════════════════════════════════════════════════════════════════════════
# Checklists (templates)
# ══════════════════════════════════════════════════════════════════════════════


#: The statutory declarations a FORM task can generate, as letter template codes.
STATUTORY_FORM_CODES: tuple[str, ...] = ("FORM11_STD", "FORM2_STD", "FORMF_STD", "ESI_DECL_STD")


class TemplateTaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    owner_type: OnboardingOwnerType
    #: HR, IT and finance work only: a named person. Empty = whoever starts the plan.
    owner_user_id: uuid.UUID | None = None
    #: Days relative to the joining date; negative is before joining.
    due_offset_days: int = Field(default=0, ge=-180, le=365)
    task_type: OnboardingTaskType = OnboardingTaskType.GENERAL
    document_type: DocumentCategory | None = None
    #: A FORM task's statutory form (FORM11_STD, FORM2_STD, FORMF_STD, ESI_DECL_STD).
    form_code: str | None = Field(default=None, max_length=32)
    is_mandatory: bool = True

    @model_validator(mode="after")
    def _upload_needs_a_category(self) -> TemplateTaskIn:
        if self.task_type is OnboardingTaskType.FORM:
            if self.form_code is not None and self.form_code not in STATUTORY_FORM_CODES:
                raise ValueError(
                    "A form task generates one of the statutory forms: "
                    + ", ".join(STATUTORY_FORM_CODES)
                )
        else:
            self.form_code = None
        if self.task_type is OnboardingTaskType.DOCUMENT_UPLOAD and self.document_type is None:
            raise ValueError("A document upload task needs the document type it collects.")
        if self.task_type is not OnboardingTaskType.DOCUMENT_UPLOAD:
            self.document_type = None
        if self.owner_type not in (
            OnboardingOwnerType.HR,
            OnboardingOwnerType.IT,
            OnboardingOwnerType.FINANCE,
        ):
            self.owner_user_id = None
        return self


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    active: bool = True
    is_default: bool = False
    #: In order. The list position is the task's order.
    tasks: list[TemplateTaskIn] = Field(default_factory=list, max_length=100)


class TemplateUpdate(BaseModel):
    """Every field optional. `tasks`, when sent, replaces the whole ordered list;
    plans already started keep the tasks they were started with."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    active: bool | None = None
    is_default: bool | None = None
    tasks: list[TemplateTaskIn] | None = Field(default=None, max_length=100)


class TemplateTaskRead(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    owner_type: OnboardingOwnerType
    owner_user_id: uuid.UUID | None
    owner_name: str | None
    due_offset_days: int
    task_type: OnboardingTaskType
    document_type: str | None
    form_code: str | None = None
    sort_order: int
    is_mandatory: bool


class TemplateRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    department_id: uuid.UUID | None
    department_name: str | None
    location_id: uuid.UUID | None
    location_name: str | None
    active: bool
    is_default: bool
    task_count: int
    #: How many plans have been started from it (any status).
    plan_count: int
    tasks: list[TemplateTaskRead]
    created_at: datetime
    updated_at: datetime


# ══════════════════════════════════════════════════════════════════════════════
# Plans
# ══════════════════════════════════════════════════════════════════════════════


class PlanCreate(BaseModel):
    employee_id: uuid.UUID
    template_id: uuid.UUID
    buddy_employee_id: uuid.UUID | None = None
    #: Defaults to the employee's date of joining.
    joining_date: date | None = None


class PlanCancel(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class PlanProgress(BaseModel):
    total: int
    done: int
    skipped: int
    pending: int
    overdue: int
    mandatory_pending: int
    unassigned: int
    #: Done or skipped over total, 0-100, rounded down.
    percent: int


class PlanRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str
    employee_status: str
    designation: str | None
    department_id: uuid.UUID | None
    department_name: str | None
    manager_name: str | None
    template_id: uuid.UUID | None
    template_name: str
    joining_date: date
    buddy_employee_id: uuid.UUID | None
    buddy_name: str | None
    status: OnboardingPlanStatus
    started_by_name: str | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    progress: PlanProgress
    created_at: datetime


class TaskRead(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    title: str
    description: str | None
    owner_type: OnboardingOwnerType
    assignee_user_id: uuid.UUID | None
    assignee_name: str | None
    due_date: date
    phase: Phase
    is_overdue: bool
    task_type: OnboardingTaskType
    document_type: str | None
    sort_order: int
    is_mandatory: bool
    status: OnboardingTaskStatus
    completed_by_name: str | None
    completed_at: datetime | None
    note: str | None
    document_id: uuid.UUID | None
    document_title: str | None
    form_code: str | None = None
    #: The generated statutory form, once the joiner has generated it.
    form_letter_id: uuid.UUID | None = None
    #: Whether the caller may mark it done (or upload for it).
    can_complete: bool
    #: Whether the caller may skip, reassign or re-date it (HR).
    can_manage: bool


class PlanDetail(PlanRead):
    tasks: list[TaskRead]


class MyTaskRead(TaskRead):
    """A task on somebody else's plan, with whose plan it is."""

    employee_id: uuid.UUID
    employee_name: str
    employee_code: str
    designation: str | None
    joining_date: date


class OnboardingMe(BaseModel):
    """The caller's own onboarding. `plan` is null when they have none."""

    plan: PlanDetail | None


class NextTask(BaseModel):
    id: uuid.UUID
    title: str
    due_date: date
    is_overdue: bool


class OnboardingSummary(BaseModel):
    """For the Home card: my plan's progress, and onboarding work waiting on me."""

    has_plan: bool
    plan_id: uuid.UUID | None
    plan_status: OnboardingPlanStatus | None
    joining_date: date | None
    progress: PlanProgress | None
    #: My own next pending task on my plan.
    next_task: NextTask | None
    #: Tasks on other people's plans assigned to me.
    assigned_pending: int
    assigned_overdue: int
    assigned_next: list[NextTask]


# ══════════════════════════════════════════════════════════════════════════════
# Task actions
# ══════════════════════════════════════════════════════════════════════════════


class TaskComplete(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class TaskSkip(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class TaskUpdate(BaseModel):
    """HR's reassignment or re-dating of one task."""

    model_config = ConfigDict(extra="forbid")

    assignee_user_id: uuid.UUID | None = None
    due_date: date | None = None

    @model_validator(mode="after")
    def _something(self) -> TaskUpdate:
        if self.assignee_user_id is None and self.due_date is None:
            raise ValueError("Send a new assignee, a new due date, or both.")
        return self


class OwnerCandidate(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    role: str
