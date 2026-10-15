"""Lifecycle event and probation contracts (slice 24)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, Field, PlainSerializer

from app.models.lifecycle import LifecycleEventType, LifecycleStatus, SilencePolicy

Dec = Annotated[Decimal, PlainSerializer(lambda v: str(v), return_type=str)]


class EventRaise(BaseModel):
    employee_id: uuid.UUID
    event_type: LifecycleEventType
    effective_from: date
    #: Only the fields this event type is allowed to change. Anything else is
    #: refused — an event that can change any field is a field edit with a nicer
    #: name, and the point of these is that they are not field edits.
    new_values: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)
    new_ctc_annual: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)


class EventDecision(BaseModel):
    approve: bool = True
    note: str = Field(default="", max_length=2000)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class EventRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str
    event_type: LifecycleEventType
    effective_from: date
    status: LifecycleStatus
    previous_values: dict
    new_values: dict
    reason: str
    new_ctc_annual: Dec | None
    consequences_shown: list
    approved_at: datetime | None
    effected_at: datetime | None
    cancelled_reason: str | None
    failure_reason: str | None
    letter_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    created_at: datetime
    #: The approvers in order, each with its status; empty on older events.
    approval_chain: list = []
    #: Whether the caller decides the current step (on the single-event read).
    caller_may_decide: bool = False


class BulkRow(BaseModel):
    employee_id: uuid.UUID
    new_values: dict[str, Any] = Field(default_factory=dict)
    new_ctc_annual: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    reason: str | None = Field(default=None, max_length=2000)


class BulkRaise(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    event_type: LifecycleEventType
    effective_from: date
    reason: str = Field(min_length=1, max_length=2000)
    rows: list[BulkRow] = Field(min_length=1, max_length=2000)
    #: Approving four hundred promotions individually is theatre; approving a
    #: mass termination in one click is not. Per tenant, per batch.
    approve_individually: bool = True


class ProbationPolicyIn(BaseModel):
    """No default on `silence_policy`.

    Auto-confirming and requiring a decision lead to opposite outcomes for a real
    person, so the tenant chooses. A missing value is a 422, not a fallback.
    """

    silence_policy: SilencePolicy
    grace_days: int = Field(default=3, ge=0, le=90)
    confirmation_raises_salary: bool = False
    hr_escalation_user_id: uuid.UUID | None = None


class ProbationPolicyRead(BaseModel):
    configured: bool
    silence_policy: SilencePolicy | None
    grace_days: int | None
    confirmation_raises_salary: bool | None
    hr_escalation_user_id: uuid.UUID | None
    note: str
