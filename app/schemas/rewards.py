"""Rewards contracts: points, redemptions, budgets, valuation, expiry, tax and
reconciliation (slice 33).

Points are integers and rupees are `Decimal`, serialised as strings. No
contract takes a date that decides valuation, expiry or financial year from the
client: an award or a redemption happens today, on the server's clock, because a
back-dated award is a revaluation and a back-dated redemption is a move into
another tax year.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.rewards import (
    BudgetScopeEnum,
    EnforcementModeEnum,
    ReconciliationStatusEnum,
    RedemptionStatusEnum,
    TaxTreatmentEnum,
)

# ── Configuration ───────────────────────────────────────────────────────────


class ValuationCreate(BaseModel):
    points_per_rupee: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    effective_from: date
    note: str | None = Field(default=None, max_length=2000)


class ValuationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    points_per_rupee: Decimal
    effective_from: date
    note: str | None
    created_at: datetime


class ExpiryPolicyUpdate(BaseModel):
    enabled: bool
    #: A floor of 90 days in the engine and a check constraint in the database.
    expire_after_days: int = Field(default=365, ge=90, le=3650)
    notify_days_before: int = Field(default=30, ge=7, le=365)
    #: Required. Points that quietly vanish teach people the recognition was
    #: not real.
    disclosure: str = Field(min_length=1, max_length=2000)


class ExpiryPolicyRead(BaseModel):
    configured: bool
    enabled: bool
    expire_after_days: int | None
    notify_days_before: int | None
    disclosure: str


class TaxRuleCreate(BaseModel):
    reward_type: str = Field(min_length=1, max_length=64)
    treatment: TaxTreatmentEnum
    exemption_threshold_per_year: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    effective_from: date
    basis: str | None = Field(default=None, max_length=4000)
    #: False unless a tax advisor is named.
    reviewed: bool = False
    reviewed_by: str | None = Field(default=None, max_length=160)
    reviewed_on: date | None = None


class TaxRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reward_type: str
    treatment: TaxTreatmentEnum
    exemption_threshold_per_year: Decimal
    effective_from: date
    basis: str | None
    reviewed: bool
    reviewed_by: str | None
    reviewed_on: date | None


class BudgetCreate(BaseModel):
    scope: BudgetScopeEnum
    #: Required for a department budget.
    department_id: uuid.UUID | None = None
    #: Required for a manager budget. Resolved to that person's login.
    manager_employee_id: uuid.UUID | None = None
    period_start: date
    period_end: date
    allocated_points: int = Field(ge=0, le=100_000_000)
    enforcement: EnforcementModeEnum = EnforcementModeEnum.SOFT_WARNING

    @model_validator(mode="after")
    def _shape(self) -> BudgetCreate:
        if self.period_end < self.period_start:
            raise ValueError("A budget period cannot end before it starts.")
        if self.scope is BudgetScopeEnum.DEPARTMENT and self.department_id is None:
            raise ValueError("A department budget needs a department.")
        if self.scope is BudgetScopeEnum.MANAGER and self.manager_employee_id is None:
            raise ValueError("A manager budget needs a manager.")
        return self


# ── The ledger ──────────────────────────────────────────────────────────────


class PointsAwardCreate(BaseModel):
    employee_id: uuid.UUID
    points: int = Field(gt=0, le=1_000_000)
    source_type: str = Field(default="recognition", max_length=48)
    source_id: uuid.UUID | None = None


class RedemptionCreate(BaseModel):
    #: Omitted means your own points. HR may redeem on somebody's behalf.
    employee_id: uuid.UUID | None = None
    points: int = Field(gt=0, le=1_000_000)
    item_sku: str | None = Field(default=None, max_length=64)
    item_description: str = Field(min_length=1, max_length=2000)


class ReversalCreate(BaseModel):
    entry_id: uuid.UUID
    #: Shown on the employee's statement.
    reason: str = Field(min_length=1, max_length=2000)


class RedemptionRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    points: int
    rupee_value: Decimal
    item_sku: str | None
    item_description: str | None
    status: RedemptionStatusEnum
    requested_on: date


# ── Reconciliation ──────────────────────────────────────────────────────────


class ReconciliationCreate(BaseModel):
    period_start: date
    period_end: date
    period_label: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")

    @model_validator(mode="after")
    def _ordered(self) -> ReconciliationCreate:
        if self.period_end < self.period_start:
            raise ValueError("A reconciliation period cannot end before it starts.")
        return self


class FulfilmentImportRow(BaseModel):
    redemption_id: uuid.UUID | None = None
    external_redemption_ref: str | None = Field(default=None, max_length=120)
    order_id: str = Field(min_length=1, max_length=120)
    item_sku: str | None = Field(default=None, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    fulfilled_on: date | None = None


class InvoiceLineImportRow(BaseModel):
    order_id: str = Field(min_length=1, max_length=120)
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    item_sku: str | None = Field(default=None, max_length=64)
    invoice_ref: str | None = Field(default=None, max_length=120)
    invoiced_on: date


class RewardsImportCreate(BaseModel):
    """What the merchandise partner sent, as rows. Parsed from their file on
    the client and validated here, row by row."""

    fulfilments: list[FulfilmentImportRow] = Field(default_factory=list, max_length=5000)
    invoice_lines: list[InvoiceLineImportRow] = Field(default_factory=list, max_length=5000)


class RewardsImportRead(BaseModel):
    fulfilments_imported: int
    fulfilments_skipped: list[str]
    invoice_lines_imported: int
    note: str


class ReconciliationRunRead(BaseModel):
    id: uuid.UUID
    period_label: str
    status: ReconciliationStatusEnum
    redemption_count: int
    fulfilment_count: int
    invoice_line_count: int
    redeemed_value: Decimal
    invoiced_value: Decimal
    gap_count: int
    gaps: list[dict[str, Any]]
    run_on: date
    created_at: datetime


# ── Scheduler results ───────────────────────────────────────────────────────


class ExpiryWarningRunRead(BaseModel):
    employees_warned: int
    lots_warned: int
    already_warned: int
    note: str
