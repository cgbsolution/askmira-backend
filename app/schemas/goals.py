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

from pydantic import BaseModel, Field

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


class GoalCheckInCreate(BaseModel):
    key_result_id: uuid.UUID
    value: Decimal = Field(max_digits=18, decimal_places=4)
    confidence: CheckInConfidence | None = None
    note: str | None = Field(default=None, max_length=2000)


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


class GoalScoreRead(BaseModel):
    employee_id: uuid.UUID
    cycle_id: uuid.UUID
    score: Decimal | None
    measured_weight_percent: Decimal
    contributing: list[dict[str, Any]]
    excluded: list[dict[str, str]]
    notes: list[str]
