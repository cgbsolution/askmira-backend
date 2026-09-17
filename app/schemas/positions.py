"""Position management contracts (slice 23). Decimals cross the wire as strings."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

from app.models.position import AssignmentType, PositionStatus

Dec = Annotated[Decimal, PlainSerializer(lambda v: str(v), return_type=str)]


class PositionCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-/]+$")
    title: str = Field(min_length=1, max_length=255)
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    grade: str | None = Field(default=None, max_length=32)
    reports_to_position_id: uuid.UUID | None = None
    budgeted_ctc_min: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    budgeted_ctc_max: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    #: A seat is at most one whole person. Above 1.0 is two seats sharing a code.
    fte: Decimal = Field(default=Decimal("1.0"), gt=0, le=1, decimal_places=2)
    effective_from: date

    @model_validator(mode="after")
    def _band(self) -> PositionCreate:
        if (
            self.budgeted_ctc_min is not None
            and self.budgeted_ctc_max is not None
            and self.budgeted_ctc_max < self.budgeted_ctc_min
        ):
            raise ValueError("The budgeted CTC ceiling is below its floor")
        return self


class PositionUpdate(BaseModel):
    """Edits that are not status changes. Freezing and abolishing have their own endpoint."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    grade: str | None = Field(default=None, max_length=32)
    reports_to_position_id: uuid.UUID | None = None
    budgeted_ctc_min: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    budgeted_ctc_max: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    fte: Decimal | None = Field(default=None, gt=0, le=1, decimal_places=2)


class PositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    title: str
    status: PositionStatus
    fte: Dec
    grade: str | None
    department_id: uuid.UUID | None
    location_id: uuid.UUID | None
    cost_centre_id: uuid.UUID | None
    reports_to_position_id: uuid.UUID | None
    budgeted_ctc_min: Dec | None
    budgeted_ctc_max: Dec | None
    effective_from: date
    effective_to: date | None
    vacated_on: date | None
    status_reason: str | None


class PositionStatusChange(BaseModel):
    action: str = Field(pattern="^(freeze|unfreeze|abolish)$")
    reason: str = Field(default="", max_length=2000)
    on: date | None = None


class AssignRequest(BaseModel):
    employee_id: uuid.UUID
    from_date: date
    assignment_type: AssignmentType = AssignmentType.PRIMARY
    note: str | None = Field(default=None, max_length=2000)


class VacateRequest(BaseModel):
    on: date
    reason: str = Field(default="", max_length=2000)


class EndAssignmentRequest(BaseModel):
    on: date


class AssignmentRead(BaseModel):
    id: uuid.UUID
    position_id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    from_date: date
    to_date: date | None
    assignment_type: AssignmentType
    counts_toward_headcount: bool
    note: str | None


class CostCentreCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-/]+$")
    name: str = Field(min_length=1, max_length=160)
    owner_employee_id: uuid.UUID | None = None


class CostCentreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    owner_employee_id: uuid.UUID | None
    is_active: bool


class BudgetUpsert(BaseModel):
    financial_year: str = Field(
        pattern=r"^FY \d{4}-\d{2}$", description='Indian financial year, e.g. "FY 2026-27".'
    )
    department_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    approved_positions: int = Field(ge=0, le=1_000_000)
    approved_cost: Decimal = Field(default=Decimal(0), ge=0, max_digits=16, decimal_places=2)
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _consistent_year(self) -> BudgetUpsert:
        start = int(self.financial_year[3:7])
        if int(self.financial_year[8:10]) != (start + 1) % 100:
            raise ValueError(f"'{self.financial_year}' is not a financial year")
        return self


class BudgetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    financial_year: str
    department_id: uuid.UUID | None
    cost_centre_id: uuid.UUID | None
    approved_positions: int
    approved_cost: Dec
    note: str | None


class BudgetUpsertResult(BaseModel):
    budget: BudgetRead
    #: Where the budget now sits below what already exists. Saved regardless —
    #: a budget cut is a decision — but new seats and fills are refused until
    #: the numbers agree.
    over_budget: list[dict[str, Any]]


class ChangeIn(BaseModel):
    kind: str = Field(pattern="^(create|move|freeze|unfreeze|abolish)$")
    position_id: uuid.UUID | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2000)


class ScenarioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    as_of: date
    changes: list[ChangeIn] = Field(default_factory=list)


class ScenarioSummary(BaseModel):
    scenario_id: uuid.UUID
    name: str
    description: str | None
    status: str
    as_of: date
    changes: int
