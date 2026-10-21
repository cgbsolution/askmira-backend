"""Asset management contracts.

Every amount is `Decimal` and serialises as a string: a purchase cost or a
recovery figure is never a float on the way to the browser.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.assets import (
    AssetCondition,
    AssetRequestStatus,
    AssetStatus,
    AssignmentOutcome,
)

_Money = Field(default=None, ge=0, max_digits=14, decimal_places=2)


def _strip(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# ══════════════════════════════════════════════════════════════════════════════
# Categories
# ══════════════════════════════════════════════════════════════════════════════


class AssetCategoryCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    custodian_user_id: uuid.UUID | None = None
    depreciation_months: int | None = Field(default=None, ge=1, le=600)
    requires_acknowledgement: bool = True

    @field_validator("code")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class AssetCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    custodian_user_id: uuid.UUID | None = None
    depreciation_months: int | None = Field(default=None, ge=1, le=600)
    requires_acknowledgement: bool | None = None
    active: bool | None = None


class AssetCategoryRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    custodian_user_id: uuid.UUID | None
    custodian_name: str | None
    depreciation_months: int | None
    requires_acknowledgement: bool
    active: bool
    asset_count: int
    in_stock_count: int


class CustodianRead(BaseModel):
    user_id: uuid.UUID
    full_name: str
    role: str


# ══════════════════════════════════════════════════════════════════════════════
# Assets
# ══════════════════════════════════════════════════════════════════════════════


class AssetCreate(BaseModel):
    category_id: uuid.UUID
    asset_tag: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    serial_number: str | None = Field(default=None, max_length=128)
    make: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=120)
    specs: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    purchase_date: date | None = None
    purchase_cost: Decimal | None = _Money
    vendor: str | None = Field(default=None, max_length=160)
    invoice_number: str | None = Field(default=None, max_length=64)
    warranty_until: date | None = None
    location_id: uuid.UUID | None = None
    condition: AssetCondition = AssetCondition.NEW

    @field_validator("asset_tag")
    @classmethod
    def _tag(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("serial_number", "make", "model", "specs", "notes", "vendor", "invoice_number")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return _strip(value)


class AssetUpdate(BaseModel):
    """Register details only. Status and holder change through their own actions."""

    category_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    serial_number: str | None = Field(default=None, max_length=128)
    make: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=120)
    specs: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    purchase_date: date | None = None
    purchase_cost: Decimal | None = _Money
    vendor: str | None = Field(default=None, max_length=160)
    invoice_number: str | None = Field(default=None, max_length=64)
    warranty_until: date | None = None
    location_id: uuid.UUID | None = None
    condition: AssetCondition | None = None

    @field_validator("serial_number", "make", "model", "specs", "notes", "vendor", "invoice_number")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return _strip(value)


class AssetStatusChange(BaseModel):
    status: Literal["in_stock", "in_repair", "lost", "retired", "disposed"]
    reason: str = Field(min_length=1, max_length=2000)
    #: Only when an assigned asset is reported lost: what its holder owes.
    recovery_amount: Decimal | None = _Money


class AssignmentRead(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    asset_tag: str
    asset_name: str
    serial_number: str | None
    category_name: str
    requires_acknowledgement: bool
    employee_id: uuid.UUID
    employee_code: str | None
    employee_name: str | None
    assigned_at: datetime
    assigned_by_name: str | None
    expected_return_date: date | None
    condition_out: AssetCondition
    assignment_notes: str | None
    acknowledged_at: datetime | None
    returned_at: datetime | None
    returned_to_name: str | None
    condition_in: AssetCondition | None
    return_notes: str | None
    recovery_amount: Decimal | None
    exit_record_id: uuid.UUID | None
    #: An exit clearance item is waiting on this hand-over; returning it signs that off.
    exit_clearance_pending: bool
    closed_at: datetime | None
    outcome: AssignmentOutcome | None
    is_open: bool
    is_overdue: bool
    return_reminder_sent_at: datetime | None


class AssetRead(BaseModel):
    id: uuid.UUID
    asset_tag: str
    name: str
    category_id: uuid.UUID
    category_code: str
    category_name: str
    serial_number: str | None
    make: str | None
    model: str | None
    specs: str | None
    notes: str | None
    purchase_date: date | None
    purchase_cost: Decimal | None
    vendor: str | None
    invoice_number: str | None
    warranty_until: date | None
    location_id: uuid.UUID | None
    location_name: str | None
    status: AssetStatus
    condition: AssetCondition
    status_reason: str | None
    status_changed_at: datetime | None
    depreciation_months: int | None
    #: Straight-line value today, or None when no purchase cost is recorded.
    book_value: Decimal | None
    book_value_basis: str | None
    current_assignment: AssignmentRead | None
    created_at: datetime
    updated_at: datetime


class AssetDetailRead(AssetRead):
    history: list[AssignmentRead]


class AssetPage(BaseModel):
    items: list[AssetRead]
    total: int
    page: int
    page_size: int


class AssetSummary(BaseModel):
    total: int
    by_status: dict[str, int]
    assigned: int
    in_stock: int
    in_repair: int
    lost: int
    overdue_returns: int
    unacknowledged: int
    pending_requests: int
    approved_awaiting_fulfilment: int
    #: Over assets with a recorded purchase cost, excluding disposed ones.
    purchase_cost_total: Decimal
    book_value_total: Decimal
    assets_without_cost: int


# ══════════════════════════════════════════════════════════════════════════════
# Assignment actions
# ══════════════════════════════════════════════════════════════════════════════


class AssignIn(BaseModel):
    employee_id: uuid.UUID
    expected_return_date: date | None = None
    condition_out: AssetCondition | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ReturnIn(BaseModel):
    condition_in: AssetCondition
    notes: str | None = Field(default=None, max_length=2000)
    #: What the holder owes for damage. Only with a damaged return.
    recovery_amount: Decimal | None = _Money
    next_status: Literal["in_stock", "in_repair"] = "in_stock"


class MyAssetsRead(BaseModel):
    current: list[AssignmentRead]
    history: list[AssignmentRead]


# ══════════════════════════════════════════════════════════════════════════════
# Requests
# ══════════════════════════════════════════════════════════════════════════════


class AssetRequestCreate(BaseModel):
    category_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=2000)
    needed_by: date | None = None


class DecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class RejectIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class FulfilIn(BaseModel):
    asset_id: uuid.UUID
    expected_return_date: date | None = None
    condition_out: AssetCondition | None = None
    notes: str | None = Field(default=None, max_length=2000)


class AssetRequestRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None
    employee_name: str | None
    category_id: uuid.UUID
    category_name: str
    reason: str
    needed_by: date | None
    status: AssetRequestStatus
    created_at: datetime
    decided_by_name: str | None
    decided_at: datetime | None
    decision_note: str | None
    fulfilled_asset_id: uuid.UUID | None
    fulfilled_asset_tag: str | None
    fulfilled_at: datetime | None
    #: Whether the caller may approve or reject it now.
    can_decide: bool
    #: Whether the caller may hand over an asset against it now.
    can_fulfil: bool
    #: Whether the caller may withdraw it (the requester, while it is open).
    can_cancel: bool


class ReminderResult(BaseModel):
    reminded: int
