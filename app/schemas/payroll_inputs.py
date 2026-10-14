"""Contracts for ad-hoc payroll adjustments and overtime.

Money crosses the wire as strings (`Money`), never as JSON numbers.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.payroll_inputs import (
    AdjustmentCategory,
    AdjustmentKind,
    AdjustmentRecurrence,
    AdjustmentStatus,
    OvertimeStatus,
)
from app.schemas.payroll import Money

__all__ = [
    "AdjustmentApplicationRead",
    "AdjustmentCreate",
    "AdjustmentDecision",
    "AdjustmentImportResult",
    "AdjustmentRead",
    "LedgerMappingRead",
    "LedgerMappingSave",
    "LedgerMappingWrite",
    "OvertimeBulkDecision",
    "OvertimeDecision",
    "OvertimeEntryRead",
    "OvertimePolicyCreate",
    "OvertimePolicyRead",
    "OvertimePolicyUpdate",
    "OvertimeSyncRequest",
    "OvertimeSyncResult",
]

_MONEY = {"max_digits": 14, "decimal_places": 2}


# ── Adjustments ─────────────────────────────────────────────────────────────


class AdjustmentCreate(BaseModel):
    employee_id: uuid.UUID
    category: AdjustmentCategory
    name: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0, **_MONEY)
    taxable: bool = True
    counts_as_wages: bool = False
    within_net_pay: bool = True
    recurrence: AdjustmentRecurrence = AdjustmentRecurrence.ONE_TIME
    start_year: int = Field(ge=2000, le=2100)
    start_month: int = Field(ge=1, le=12)
    end_year: int | None = Field(default=None, ge=2000, le=2100)
    end_month: int | None = Field(default=None, ge=1, le=12)
    total_cap: Decimal | None = Field(default=None, gt=0, **_MONEY)
    reference: str | None = Field(default=None, max_length=128)
    reason: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def _window(self) -> AdjustmentCreate:
        if (self.end_year is None) != (self.end_month is None):
            raise ValueError("Give both the end year and the end month, or neither.")
        return self


class AdjustmentDecision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class AdjustmentApplicationRead(BaseModel):
    run_id: uuid.UUID
    period_year: int
    period_month: int
    amount: Money
    created_at: datetime


class AdjustmentRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None
    employee_name: str | None
    kind: AdjustmentKind
    category: AdjustmentCategory
    name: str
    amount: Money
    taxable: bool
    counts_as_wages: bool
    within_net_pay: bool
    recurrence: AdjustmentRecurrence
    start_year: int
    start_month: int
    end_year: int | None
    end_month: int | None
    total_cap: Money | None
    amount_applied: Money
    reference: str | None
    reason: str
    status: AdjustmentStatus
    created_by_user_id: uuid.UUID | None
    created_by_name: str | None
    decided_by_name: str | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime
    caller_may_decide: bool
    applications: list[AdjustmentApplicationRead] = Field(default_factory=list)


class AdjustmentImportResult(BaseModel):
    created: int
    #: Row-numbered problems. A file with any problem creates nothing.
    errors: list[str]


# ── Overtime ────────────────────────────────────────────────────────────────


class OvertimePolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    location_id: uuid.UUID | None = None
    rate_multiplier: Decimal = Field(gt=0, max_digits=5, decimal_places=2)
    wage_component_codes: list[str] = Field(min_length=1, max_length=20)
    days_divisor: int = Field(gt=0, le=31)
    hours_per_day: Decimal = Field(gt=0, le=24, max_digits=4, decimal_places=2)
    minimum_minutes: int = Field(default=0, ge=0, le=1440)
    rounding_minutes: int = Field(default=0, ge=0, le=240)
    daily_cap_minutes: int | None = Field(default=None, gt=0, le=1440)
    quarterly_cap_minutes: int | None = Field(default=None, gt=0)
    count_rest_days: bool = True
    requires_approval: bool = True
    effective_from: date


class OvertimePolicyUpdate(BaseModel):
    """What may change on a policy in use. Pricing changes are a new policy."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    effective_to: date | None = None
    is_active: bool | None = None
    requires_approval: bool | None = None


class OvertimePolicyRead(BaseModel):
    id: uuid.UUID
    name: str
    location_id: uuid.UUID | None
    location_name: str | None
    rate_multiplier: Money
    wage_component_codes: list[str]
    days_divisor: int
    hours_per_day: Money
    minimum_minutes: int
    rounding_minutes: int
    daily_cap_minutes: int | None
    quarterly_cap_minutes: int | None
    count_rest_days: bool
    requires_approval: bool
    effective_from: date
    effective_to: date | None
    is_active: bool


class OvertimeEntryRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None
    employee_name: str | None
    attendance_date: date
    policy_id: uuid.UUID
    policy_name: str | None
    recorded_minutes: int
    eligible_minutes: int
    approved_minutes: int | None
    is_rest_day: bool
    hourly_rate: Money | None
    rate_multiplier: Money | None
    amount: Money | None
    basis: str | None
    status: OvertimeStatus
    decided_by_name: str | None
    decided_at: datetime | None
    decision_note: str | None
    paid_in_run_id: uuid.UUID | None
    caller_may_decide: bool


class OvertimeDecision(BaseModel):
    approve: bool
    #: Approve fewer minutes than eligible. Never more.
    minutes: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=1000)


class OvertimeBulkDecision(BaseModel):
    entry_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    approve: bool
    note: str | None = Field(default=None, max_length=1000)


class OvertimeSyncRequest(BaseModel):
    start: date
    end: date


class OvertimeSyncResult(BaseModel):
    created: int
    updated: int
    removed: int
    auto_approved: int
    #: Days with overtime that no policy covers, as "CODE on date".
    without_policy: list[str]


# ── Ledger mapping ──────────────────────────────────────────────────────────


class LedgerMappingRead(BaseModel):
    line_code: str
    name: str
    #: earning | deduction | employer_contribution | net_pay
    nature: str
    debit_ledger: str | None
    credit_ledger: str | None
    #: Whether the ledgers this line's nature needs are all filled in.
    complete: bool
    #: Whether the run asked about posts this line.
    used_by_run: bool


class LedgerMappingWrite(BaseModel):
    line_code: str = Field(min_length=1, max_length=64)
    nature: str = Field(pattern=r"^(earning|deduction|employer_contribution|net_pay)$")
    debit_ledger: str | None = Field(default=None, max_length=160)
    credit_ledger: str | None = Field(default=None, max_length=160)


class LedgerMappingSave(BaseModel):
    mappings: list[LedgerMappingWrite] = Field(min_length=1, max_length=500)
