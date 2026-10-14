"""Group benefits."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.benefits import BenefitKind, LifeEventKind

Relation = Literal["spouse", "child", "parent", "parent_in_law"]
MONEY = {"max_digits": 14, "decimal_places": 2, "ge": 0}


class TopUp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=120)
    sum_insured: Decimal = Field(**MONEY)
    employee_premium_annual: Decimal = Field(**MONEY)


class PlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=160)
    kind: BenefitKind
    insurer: str = Field(min_length=2, max_length=160)
    tpa: str | None = Field(default=None, max_length=160)
    policy_number: str | None = Field(default=None, max_length=64)
    policy_start: date
    policy_end: date
    sum_insured: Decimal = Field(**MONEY)
    covers: list[Relation] = Field(default_factory=list)
    max_children: int | None = Field(default=None, ge=0, le=10)
    employer_premium_per_member: Decimal = Field(default=Decimal("0"), **MONEY)
    top_ups: list[TopUp] = Field(default_factory=list, max_length=10)
    enrolment_opens: date | None = None
    enrolment_closes: date | None = None
    life_event_window_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def _dates(self) -> PlanCreate:
        if self.policy_end < self.policy_start:
            raise ValueError("The policy ends before it starts.")
        if (self.enrolment_opens is None) != (self.enrolment_closes is None):
            raise ValueError("Give both enrolment dates, or neither.")
        if self.enrolment_opens and self.enrolment_closes < self.enrolment_opens:
            raise ValueError("Enrolment closes before it opens.")
        if len({t.code for t in self.top_ups}) != len(self.top_ups):
            raise ValueError("Each top-up needs its own code.")
        return self


class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enrolment_opens: date | None = None
    enrolment_closes: date | None = None
    employer_premium_per_member: Decimal | None = Field(default=None, **MONEY)
    active: bool | None = None


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    relationship: Relation
    date_of_birth: date | None = None
    gender: Literal["female", "male", "non_binary", "undisclosed"] | None = None


class EnrolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    waive: bool = False
    members: list[Member] = Field(default_factory=list, max_length=12)
    top_up_code: str | None = Field(default=None, max_length=32)


class LifeEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: uuid.UUID
    kind: LifeEventKind
    event_date: date
    add_members: list[Member] = Field(default_factory=list, max_length=6)
    remove_members: list[str] = Field(default_factory=list, max_length=6)
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _something(self) -> LifeEventIn:
        if not self.add_members and not self.remove_members:
            raise ValueError("Say who is added to or removed from cover.")
        return self


class LifeEventDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    note: str | None = Field(default=None, max_length=1000)
