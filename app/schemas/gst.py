"""GST input credit contracts (slice 34)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.gst import (
    GstEligibilityEnum,
    GstinPortalStatusEnum,
    Gstr2bMatchEnum,
    GstTaxSplitEnum,
)


class EntityGstinIn(BaseModel):
    legal_entity_id: uuid.UUID
    gstin: str = Field(min_length=15, max_length=15)


class BlockedCategoryIn(BaseModel):
    category_key: str = Field(min_length=1, max_length=64)
    #: Required. A line marked ineligible with no explanation is one finance
    #: cannot challenge.
    reason: str = Field(min_length=1, max_length=4000)
    reference: str = Field(default="Section 17(5), CGST Act", max_length=120)
    has_exceptions: bool = False
    exception_note: str | None = Field(default=None, max_length=4000)
    reviewed: bool = False
    reviewed_by: str | None = Field(default=None, max_length=160)
    reviewed_on: date | None = None


class CaptureIn(BaseModel):
    vendor_gstin: str = Field(default="", max_length=20)
    invoice_number: str = Field(min_length=1, max_length=120)
    invoice_date: date
    category_key: str = Field(min_length=1, max_length=64)
    taxable_value: Decimal = Field(ge=0)
    legal_entity_id: uuid.UUID | None = None
    expense_claim_id: uuid.UUID | None = None
    employee_id: uuid.UUID | None = None
    cgst: Decimal = Field(default=Decimal("0"), ge=0)
    sgst: Decimal = Field(default=Decimal("0"), ge=0)
    igst: Decimal = Field(default=Decimal("0"), ge=0)
    cess: Decimal = Field(default=Decimal("0"), ge=0)
    supplier_state_code: str | None = Field(default=None, max_length=2)
    place_of_supply_state_code: str | None = Field(default=None, max_length=2)
    hsn_sac: str | None = Field(default=None, max_length=16)


class ReviewIn(BaseModel):
    #: `uncertain` is refused — a review has to reach a decision.
    decision: GstEligibilityEnum
    rationale: str = Field(min_length=1, max_length=4000)
    decided_on: date


class Gstr2bLineIn(BaseModel):
    vendor_gstin: str = Field(min_length=1, max_length=20)
    vendor_name: str | None = Field(default=None, max_length=240)
    invoice_number: str = Field(min_length=1, max_length=120)
    invoice_date: date
    taxable_value: Decimal = Field(ge=0)
    cgst: Decimal = Field(default=Decimal("0"), ge=0)
    sgst: Decimal = Field(default=Decimal("0"), ge=0)
    igst: Decimal = Field(default=Decimal("0"), ge=0)
    cess: Decimal = Field(default=Decimal("0"), ge=0)


class Gstr2bImportIn(BaseModel):
    period_label: str = Field(min_length=1, max_length=16)
    imported_on: date
    lines: list[Gstr2bLineIn] = Field(default_factory=list)
    legal_entity_id: uuid.UUID | None = None
    source_note: str | None = Field(default=None, max_length=2000)


class ReconcileIn(BaseModel):
    period_label: str = Field(min_length=1, max_length=16)
    period_start: date
    period_end: date
    legal_entity_id: uuid.UUID | None = None


# ── Reads ───────────────────────────────────────────────────────────────────


class GstCreditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID | None
    expense_claim_id: uuid.UUID | None
    employee_id: uuid.UUID | None
    vendor_gstin: str
    gstin_is_valid: bool
    gstin_problem: str | None
    gstin_portal_status: GstinPortalStatusEnum
    invoice_number: str
    invoice_date: date
    category_key: str
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    cess: Decimal
    hsn_sac: str | None
    supplier_state_code: str | None
    place_of_supply_state_code: str | None
    recipient_state_code: str | None
    tax_split: GstTaxSplitEnum | None
    eligibility: GstEligibilityEnum
    eligibility_reason: str
    eligibility_reference: str
    claimable_amount: Decimal
    amount_at_stake: Decimal
    reconciliation_status: Gstr2bMatchEnum
    gstr2b_matched_at: date | None
    created_at: datetime | None = None


class EntityRegistrationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID
    gstin: str
    state_code: str
    state_name: str | None
    is_active: bool


class BlockedCategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_key: str
    reason: str
    reference: str
    has_exceptions: bool
    exception_note: str | None
    reviewed: bool
    reviewed_by: str | None
    reviewed_on: date | None


class Gstr2bImportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID | None
    period_label: str
    line_count: int
    total_tax: Decimal
    imported_on: date
    source_note: str | None
    created_at: datetime | None = None


class ReconciliationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID | None
    period_label: str
    captured_lines: int
    portal_lines: int
    matched_tax: Decimal
    missing_in_2b_tax: Decimal
    missing_in_register_tax: Decimal
    at_risk_tax: Decimal
    counts: dict[str, Any]
    run_on: date
    created_at: datetime | None = None


class CsvRowError(BaseModel):
    row: int
    error: str


class Gstr2bCsvResult(BaseModel):
    imported: dict[str, Any] | None
    rows_read: int
    errors: list[CsvRowError]
