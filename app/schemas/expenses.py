"""Expense policy, claims, receipts, reports, travel and advance contracts (slice 31).

Every amount is `Decimal` and serialises as a string. The claim form never adds
two floats together.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.expenses import (
    AdvanceStatusEnum,
    CityTierEnum,
    ClaimKindEnum,
    ConversionConventionEnum,
    ExpenseClaimStatusEnum,
    ExpenseReportStatusEnum,
    LimitTypeEnum,
    PolicyVerdictEnum,
    TravelRequestStatusEnum,
    ViolationActionEnum,
)

# ══════════════════════════════════════════════════════════════════════════════
# Inputs
# ══════════════════════════════════════════════════════════════════════════════


class RuleIn(BaseModel):
    category_key: str = Field(min_length=1, max_length=80)
    city_tier: CityTierEnum
    limit_type: LimitTypeEnum
    limit_amount: Decimal = Field(ge=0)
    violation_action: ViolationActionEnum
    requires_receipt: bool = True
    #: Below this, no receipt is asked for.
    receipt_threshold: Decimal = Field(default=Decimal("0"), ge=0)
    requires_pre_approval: bool = False
    is_per_diem: bool = False


class PolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    effective_from: date
    rules: list[RuleIn] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=2000)
    #: Empty means every grade.
    applicable_grades: list[str] = Field(default_factory=list)
    applicable_locations: list[uuid.UUID] = Field(default_factory=list)


class SettingsIn(BaseModel):
    conversion_convention: ConversionConventionEnum | None = None
    rate_source: str | None = Field(default=None, max_length=120)
    advance_block_after_days: int | None = Field(default=None, ge=1, le=365)


class CityTierIn(BaseModel):
    location_id: uuid.UUID
    city_tier: CityTierEnum
    note: str | None = Field(default=None, max_length=500)


class MileageRateIn(BaseModel):
    vehicle_type: str = Field(min_length=1, max_length=40)
    rate_per_km: Decimal = Field(gt=0)
    effective_from: date
    tolerance_percent: Decimal | None = Field(default=None, ge=0, le=200)


class PerDiemRateIn(BaseModel):
    city_tier: CityTierEnum
    grade: str = Field(min_length=1, max_length=40)
    amount: Decimal = Field(gt=0)
    effective_from: date
    includes_meals: bool = True
    includes_accommodation: bool = False


class ExchangeRateIn(BaseModel):
    from_currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    rate: Decimal = Field(gt=0)
    quote_date: date
    source: str = Field(min_length=1, max_length=120)
    to_currency: str = Field(default="INR", min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")


def _latest_date_anywhere() -> date:
    """Today in the timezone furthest ahead (UTC+14).

    A cheap first check that needs no database: a date after this is in the
    future everywhere. The service repeats the check against the workspace's
    own date, which is the one that decides.
    """
    return datetime.now(timezone(timedelta(hours=14))).date()


class ClaimIn(BaseModel):
    #: Omit to claim for yourself. Only finance may file for somebody else.
    employee_id: uuid.UUID | None = None
    category_key: str = Field(min_length=1, max_length=80)
    city_tier: CityTierEnum
    amount: Decimal = Field(gt=0)
    incurred_on: date
    kind: ClaimKindEnum = ClaimKindEnum.RECEIPTED
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    settlement_date: date | None = None
    #: Finance only: a paper receipt seen in person. Everybody else attaches one.
    has_receipt: bool = False
    justification: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=2000)
    travel_request_id: uuid.UUID | None = None
    report_id: uuid.UUID | None = None
    receipt_document_id: uuid.UUID | None = None
    distance_km: Decimal | None = Field(default=None, ge=0)
    route_km: Decimal | None = Field(default=None, ge=0)
    vehicle_type: str | None = Field(default=None, max_length=40)
    per_diem_days: int | None = Field(default=None, ge=1, le=366)

    @field_validator("incurred_on")
    @classmethod
    def _not_in_the_future(cls, value: date) -> date:
        if value > _latest_date_anywhere():
            raise ValueError("An expense cannot be claimed before it is incurred.")
        return value


class SubmitIn(BaseModel):
    justification: str | None = Field(default=None, max_length=2000)


class DecisionIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_on_reject(self) -> DecisionIn:
        if not self.approve and not (self.note or "").strip():
            raise ValueError("Say why this is rejected. The employee sees the reason.")
        return self


class TravelRequestIn(BaseModel):
    employee_id: uuid.UUID | None = None
    purpose: str = Field(min_length=1, max_length=2000)
    from_date: date
    to_date: date
    destinations: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    estimated_cost: Decimal = Field(default=Decimal("0"), ge=0)
    advance_requested: Decimal = Field(default=Decimal("0"), ge=0)


class AdvanceIn(BaseModel):
    travel_request_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    issued_on: date
    disbursement_batch_id: uuid.UUID | None = None


class SettleIn(BaseModel):
    settled_on: date


class ReportCreate(BaseModel):
    employee_id: uuid.UUID | None = None
    title: str = Field(min_length=1, max_length=160)
    travel_request_id: uuid.UUID | None = None


class ReportSubmitIn(BaseModel):
    #: A written reason per line, for lines whose verdict needs one.
    justifications: dict[uuid.UUID, str] = Field(default_factory=dict)

    @field_validator("justifications")
    @classmethod
    def _bounded(cls, value: dict[uuid.UUID, str]) -> dict[uuid.UUID, str]:
        if any(len(text) > 2000 for text in value.values()):
            raise ValueError("A justification is at most 2000 characters.")
        return value


# ══════════════════════════════════════════════════════════════════════════════
# Reads
# ══════════════════════════════════════════════════════════════════════════════


class ClaimRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None = None
    employee_name: str | None = None
    report_id: uuid.UUID | None = None
    travel_request_id: uuid.UUID | None
    category_key: str
    city_tier: CityTierEnum
    kind: ClaimKindEnum
    amount: Decimal
    original_amount: Decimal
    original_currency: str
    exchange_rate: Decimal | None
    exchange_rate_date: date | None
    conversion_convention: ConversionConventionEnum | None
    exchange_rate_source: str | None
    incurred_on: date
    description: str | None
    has_receipt: bool
    receipt_document_id: uuid.UUID | None
    distance_km: Decimal | None
    route_km: Decimal | None
    vehicle_type: str | None
    per_diem_days: int | None
    status: ExpenseClaimStatusEnum
    policy_verdict: PolicyVerdictEnum
    verdict_message: str
    limit_amount: Decimal | None
    already_claimed: Decimal
    overage: Decimal
    justification: str | None
    flagged_for_approver: bool
    exceeds_route_tolerance: bool
    evaluated_at: datetime
    submitted_at: datetime | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime | None = None
    #: The line manager's stage.
    manager_decided_at: datetime | None = None
    manager_decision_note: str | None = None
    manager_decided_by_name: str | None = None
    #: How the claim was paid, once it was.
    reimbursed_in_run_id: uuid.UUID | None = None
    reimbursed_at: datetime | None = None
    advance_settlement_id: uuid.UUID | None = None
    #: "October 2026" — the payroll month that paid it.
    reimbursed_in_period: str | None = None
    #: The paying run's status: approved (money not yet sent) or paid.
    reimbursed_run_status: str | None = None
    #: One sentence for the claimant: where the money is.
    payment_note: str | None = None


class ReceiptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    content_type: str
    size_bytes: int
    sha256: str
    display_name: str
    created_at: datetime | None = None


class ReportRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str | None
    title: str
    travel_request_id: uuid.UUID | None
    status: ExpenseReportStatusEnum
    submitted_at: datetime | None
    decided_at: datetime | None
    created_at: datetime | None
    line_count: int
    total_amount: Decimal
    blocked_lines: int
    lines: list[ClaimRead] = Field(default_factory=list)


class TravelRequestRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None
    employee_name: str | None
    purpose: str
    from_date: date
    to_date: date
    destinations: list[dict[str, Any]]
    estimated_cost: Decimal
    advance_requested: Decimal
    advance_issued: Decimal
    status: TravelRequestStatusEnum
    approvals: list[dict[str, Any]]
    decided_at: datetime | None
    created_at: datetime | None


class AdvanceRead(BaseModel):
    id: uuid.UUID
    travel_request_id: uuid.UUID
    trip_purpose: str | None
    trip_to_date: date | None
    employee_id: uuid.UUID
    employee_code: str | None
    employee_name: str | None
    amount: Decimal
    issued_on: date
    status: AdvanceStatusEnum
    claimed_against: Decimal
    settled_on: date | None
    recovered_amount: Decimal
    refunded_amount: Decimal
    outstanding: Decimal
    age_days: int
    is_blocking: bool
    #: Null until a payroll run's approval takes the recovery.
    payroll_recovery_ref: str | None = None
    #: Null until a payroll run's approval pays a balance owed to the employee.
    payroll_refund_ref: str | None = None


class BlockStatus(BaseModel):
    employee_id: uuid.UUID
    permitted: bool
    message: str
    block_after_days: int
    blocking_count: int
    total_outstanding: Decimal


class ReviewQueue(BaseModel):
    claims: list[ClaimRead]
    travel_requests: list[TravelRequestRead]
    advances_to_settle: list[AdvanceRead]
    #: Things excluded because the caller is the person they are about.
    excluded_own: int


class TeamQueue(BaseModel):
    """Claims and trips waiting for a line manager's decision."""

    claims: list[ClaimRead]
    travel_requests: list[TravelRequestRead]
    #: True when the caller sees everyone's (HR standing in), not only their team's.
    covers_everyone: bool
    excluded_own: int


class RuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_key: str
    city_tier: CityTierEnum
    limit_type: LimitTypeEnum
    limit_amount: Decimal
    violation_action: ViolationActionEnum
    requires_receipt: bool
    receipt_threshold: Decimal
    requires_pre_approval: bool
    is_per_diem: bool


class PolicyRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    effective_from: date
    applicable_grades: list[str]
    applicable_locations: list[str]
    is_active: bool
    created_at: datetime | None
    rules: list[RuleRead]


class CityTierRead(BaseModel):
    location_id: uuid.UUID
    location_name: str
    city: str | None
    city_tier: CityTierEnum | None
    note: str | None


class MileageRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vehicle_type: str
    rate_per_km: Decimal
    effective_from: date
    tolerance_percent: Decimal


class PerDiemRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    city_tier: CityTierEnum
    grade: str
    amount: Decimal
    effective_from: date
    includes_meals: bool
    includes_accommodation: bool


class ExchangeRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_currency: str
    to_currency: str
    rate: Decimal
    quote_date: date
    source: str


class SettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    conversion_convention: ConversionConventionEnum
    rate_source: str
    reporting_currency: str
    advance_block_after_days: int


class ExpenseMeta(BaseModel):
    """What the claim form needs to offer — all of it from configuration."""

    reporting_currency: str
    conversion_convention: ConversionConventionEnum
    rate_source: str
    #: Categories that some active policy has a rule for.
    categories: list[str]
    vehicle_types: list[str]
    #: Currencies with at least one recorded quote into the reporting currency.
    currencies: list[str]
    city_tiers: list[CityTierEnum]
    claim_kinds: list[ClaimKindEnum]
    receipt_max_bytes: int
    receipt_types: list[str]
