"""Goal and key result schemas.

Values, weights and progress are `Decimal` and serialise as strings: a key
result of 12.5 accounts that passes through a float on the way to the browser
is no longer the number somebody checked in.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.appraisal import CycleStatus, CycleType
from app.models.goals import (
    CheckInConfidence,
    GoalLevel,
    GoalStatus,
    GoalVisibility,
    KeyResultKind,
)

GoalScope = Literal["mine", "team", "company", "all"]


class KeyResultCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    kind: KeyResultKind = KeyResultKind.NUMBER
    unit: str | None = Field(default=None, max_length=32)
    start_value: Decimal = Field(default=Decimal(0), max_digits=18, decimal_places=4)
    target_value: Decimal = Field(max_digits=18, decimal_places=4)
    weight: Decimal = Field(default=Decimal(1), gt=0, le=100, decimal_places=2)


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    level: GoalLevel = GoalLevel.INDIVIDUAL
    #: A manager or HR setting a goal for somebody. Omitted means "mine".
    owner_employee_id: uuid.UUID | None = None
    weight: Decimal = Field(default=Decimal(0), ge=0, le=100, decimal_places=2)
    start_date: date | None = None
    due_date: date
    visibility: GoalVisibility = GoalVisibility.MANAGER_LINE
    parent_goal_id: uuid.UUID | None = None
    appraisal_cycle_id: uuid.UUID | None = None
    key_results: list[KeyResultCreate] = Field(default_factory=list, max_length=20)


class GoalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    weight: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    due_date: date | None = None
    visibility: GoalVisibility | None = None
    status: GoalStatus | None = None
    parent_goal_id: uuid.UUID | None = None
    #: Removes the alignment. Separate from `parent_goal_id` so that omitting a
    #: field never silently clears it.
    clear_parent: bool = False


class KeyResultRead(BaseModel):
    id: uuid.UUID
    title: str
    kind: KeyResultKind
    unit: str | None
    start_value: Decimal
    target_value: Decimal
    current_value: Decimal
    weight: Decimal
    progress: Decimal


class LatestCheckInRead(BaseModel):
    """The most recent check-in on a goal, for the goal list."""

    confidence: CheckInConfidence | None
    note: str | None
    progress_after: Decimal
    recorded_by_name: str | None
    recorded_at: datetime


class GoalRead(BaseModel):
    id: uuid.UUID
    owner_employee_id: uuid.UUID | None
    owner_name: str | None
    level: GoalLevel
    title: str
    description: str | None
    weight: Decimal
    start_date: date | None
    due_date: date
    status: GoalStatus
    visibility: GoalVisibility
    parent_goal_id: uuid.UUID | None
    parent_title: str | None
    appraisal_cycle_id: uuid.UUID | None
    progress_percent: Decimal
    key_results: list[KeyResultRead]
    created_at: datetime
    updated_at: datetime
    #: True when the caller may edit the goal and record progress on it.
    can_edit: bool
    latest_check_in: LatestCheckInRead | None = None


class GoalCheckInCreate(BaseModel):
    """A progress update. A new value for one key result, a status, a note — or
    any combination, as long as it says something."""

    key_result_id: uuid.UUID | None = None
    value: Decimal | None = Field(default=None, max_digits=18, decimal_places=4)
    #: `done` completes the goal in the same transaction.
    confidence: CheckInConfidence | None = None
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _says_something(self) -> GoalCheckInCreate:
        if (self.key_result_id is None) != (self.value is None):
            raise ValueError("A new value needs the key result it belongs to, and vice versa.")
        if self.value is None and self.confidence is None and not (self.note or "").strip():
            raise ValueError("A check-in needs a new value, a status or a note.")
        return self


class GoalCheckInCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class GoalCheckInCommentRead(BaseModel):
    id: uuid.UUID
    author_name: str | None
    body: str
    created_at: datetime


class GoalCheckInRead(BaseModel):
    id: uuid.UUID
    key_result_id: uuid.UUID | None
    key_result_title: str | None
    previous_value: Decimal | None
    new_value: Decimal | None
    progress_after: Decimal
    confidence: CheckInConfidence | None
    note: str | None
    recorded_by_name: str | None
    recorded_at: datetime
    comments: list[GoalCheckInCommentRead] = Field(default_factory=list)
    #: True when the caller may reply to this check-in.
    can_comment: bool = False


class GoalCycleRead(BaseModel):
    """A review cycle a goal may count towards. Name and period only."""

    id: uuid.UUID
    name: str
    cycle_type: CycleType
    status: CycleStatus
    period_start: date
    period_end: date


class GoalScoreRead(BaseModel):
    employee_id: uuid.UUID
    cycle_id: uuid.UUID
    score: Decimal | None
    measured_weight_percent: Decimal
    contributing: list[dict[str, Any]]
    excluded: list[dict[str, str]]
    notes: list[str]
