"""Payroll run, payslip, rule-set and salary-overview contracts.

Money and day quantities cross the wire as strings (`Money`), never as JSON
numbers: a payroll figure must survive the round trip to the browser exactly.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.payroll import (
    CalculationType,
    ComponentType,
    PayrollRunStatus,
    PayrollRunType,
)
from app.schemas.payroll import EmployeeSalaryComponentRead, Money

# ── Runs ────────────────────────────────────────────────────────────────────


class PayrollRunCreate(BaseModel):
    legal_entity_id: uuid.UUID
    period_year: int = Field(ge=2000, le=2100)
    period_month: int = Field(ge=1, le=12)
    payment_date: date | None = None
    run_type: PayrollRunType = PayrollRunType.REGULAR
    #: Off-cycle, bonus and arrear runs only: who the run pays.
    employee_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    #: Off-cycle, bonus and arrear runs only: which input sources it draws on.
    #: Empty uses the run type's default.
    included_sources: list[str] = Field(default_factory=list, max_length=20)
    reason: str | None = Field(default=None, max_length=1000)


class InputSourceOptionRead(BaseModel):
    """An input source a supplementary run can draw on."""

    key: str
    label: str


class VarianceAcknowledge(BaseModel):
    """Why a swing in someone's net pay is right. Required, and kept."""

    note: str = Field(min_length=3, max_length=1000)

    @field_validator("note")
    @classmethod
    def _meaningful(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 3:
            raise ValueError("Say why this change in net pay is correct")
        return stripped


class RunCancel(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class RunMarkPaid(BaseModel):
    payment_date: date | None = None


class DeductionCeilingRead(BaseModel):
    """Deductions that alone pass the legal ceiling (Payment of Wages / Code on Wages)."""

    wages: str
    deductions: str
    share_percent: str
    ceiling_percent: str
    ceiling: str


class VarianceFlagRead(BaseModel):
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str | None = None
    #: Null when the flag is only about the deductions ceiling.
    previous_net: str | None = None
    current_net: str
    change_percent: str | None = None
    #: Days of the period before joining or after leaving — the usual reason a
    #: joining or leaving month swings net pay.
    not_employed_days: str = "0"
    deduction_ceiling: DeductionCeilingRead | None = None
    acknowledged: bool = False
    acknowledged_by_user_id: uuid.UUID | None = None
    acknowledged_by_name: str | None = None
    acknowledged_at: datetime | None = None
    note: str | None = None


class InputSourceRead(BaseModel):
    key: str
    label: str
    # included | nothing_due | not_available | blocked
    status: str
    message: str
    item_count: int = 0


class InputNoticeRead(BaseModel):
    key: str
    label: str
    message: str


class PayrollRunRead(BaseModel):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    legal_entity_name: str | None
    period_year: int
    period_month: int
    effective_date: date
    payment_date: date | None
    run_type: PayrollRunType
    status: PayrollRunStatus

    employee_count: int
    total_gross: Money
    total_deductions: Money
    total_net: Money
    total_employer_cost: Money

    variance_flags: list[VarianceFlagRead]
    unacknowledged_variances: int
    review_flags: list[str]
    input_sources: list[InputSourceRead]
    input_notices: list[InputNoticeRead]
    rule_set_snapshot: dict[str, str]
    error_log: str | None

    processed_at: datetime | None
    computed_by_user_id: uuid.UUID | None
    computed_by_name: str | None
    approved_by_user_id: uuid.UUID | None
    approved_by_name: str | None
    approved_at: datetime | None
    published_at: datetime | None
    published_by_name: str | None
    paid_at: datetime | None
    paid_by_name: str | None
    #: A run discarded before approval: when, by whom and why.
    cancelled_at: datetime | None = None
    cancelled_by_name: str | None = None
    cancel_reason: str | None = None
    created_at: datetime
    #: Off-cycle, bonus and arrear runs: who and what they pay, and why.
    employee_scope: list[uuid.UUID] = Field(default_factory=list)
    included_sources: list[str] | None = None
    reason: str | None = None

    # False when the caller computed this run: maker-checker.
    caller_may_approve: bool


# ── Payslips ────────────────────────────────────────────────────────────────


class PayslipLineRead(BaseModel):
    code: str
    name: str
    type: str
    amount: Money
    basis: str
    rule_set_id: str | None = None


class PayslipInputLineRead(BaseModel):
    provider: str
    code: str
    name: str
    kind: str
    amount: Money
    requested: Money
    taxable_amount: Money
    basis: str
    source: dict[str, Any]
    skipped: bool
    recovery: dict[str, Any] | None = None


class RuleSetUsedRead(BaseModel):
    id: uuid.UUID
    rule_type: str
    jurisdiction: str
    version: int
    effective_from: date
    effective_to: date | None
    approved_at: datetime | None
    source_reference: str | None


class PayslipSalaryRead(BaseModel):
    id: uuid.UUID
    structure_name: str | None
    ctc_annual: Money
    effective_from: date


class PayslipRead(BaseModel):
    """One payslip with its full computation trace."""

    id: uuid.UUID
    run_id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    designation: str | None
    legal_entity_name: str | None
    period_year: int
    period_month: int
    run_type: PayrollRunType
    run_status: PayrollRunStatus
    effective_date: date
    payment_date: date | None
    is_published: bool
    published_at: datetime | None
    #: An off-cycle top-up for a month the regular run already paid.
    is_supplementary: bool = False

    working_days: Money
    paid_days: Money
    lop_days: Money
    #: Before joining or after leaving; unpaid, and not an absence.
    not_employed_days: Money

    gross_earnings: Money
    other_earnings: Money
    total_deductions: Money
    net_pay: Money
    employer_cost: Money

    pf_wage_base: Money
    esi_wage_base: Money
    pf_employee: Money
    pf_employer: Money
    esi_employee: Money
    esi_employer: Money
    professional_tax: Money
    lwf_employee: Money
    lwf_employer: Money
    vpf: Money
    tds: Money

    review_flags: list[str]
    lines: list[PayslipLineRead]
    input_lines: list[PayslipInputLineRead]
    tds_working: dict[str, Any] | None
    tds_note: str
    rule_sets: list[RuleSetUsedRead]
    salary: PayslipSalaryRead | None


class PayslipSummaryRead(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    period_year: int
    period_month: int
    run_type: PayrollRunType
    run_status: PayrollRunStatus
    gross_earnings: Money
    other_earnings: Money
    total_deductions: Money
    tds: Money
    net_pay: Money
    is_published: bool
    published_at: datetime | None


class RegisterRowRead(BaseModel):
    payslip_id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    working_days: Money
    paid_days: Money
    lop_days: Money
    not_employed_days: Money
    gross_earnings: Money
    other_earnings: Money
    pf_employee: Money
    esi_employee: Money
    professional_tax: Money
    lwf_employee: Money
    vpf: Money
    tds: Money
    other_deductions: Money
    total_deductions: Money
    net_pay: Money
    employer_cost: Money
    review_flags: list[str]
    skipped_recoveries: int
    has_variance: bool


class RegisterRead(BaseModel):
    run: PayrollRunRead
    rows: list[RegisterRowRead]
    totals: dict[str, Money]


# ── Rule sets ───────────────────────────────────────────────────────────────

RuleType = Literal[
    "pf",
    "esi",
    "professional_tax",
    "tds",
    "gratuity",
    "bonus",
    "labour_welfare_fund",
    "tds_non_salary",
    "wage_deductions",
]


class RuleSetCreate(BaseModel):
    """A new effective-dated version. Enters unapproved; an existing row is never edited."""

    rule_type: RuleType
    jurisdiction: str = Field(min_length=2, max_length=8, pattern=r"^[A-Z]{2,8}$")
    effective_from: date
    rules: dict[str, Any] = Field(min_length=1)
    source_reference: str = Field(min_length=3, max_length=512)
    notes: str | None = Field(default=None, max_length=4000)


class RuleSetApprove(BaseModel):
    """The approver's attestation. The screen states what it means before this is sent."""

    attest: bool


class RuleSetRead(BaseModel):
    id: uuid.UUID
    rule_type: str
    jurisdiction: str
    version: int
    effective_from: date
    effective_to: date | None
    rules: dict[str, Any]
    source_reference: str | None
    notes: str | None
    is_approved: bool
    approved_at: datetime | None
    approved_by_user_id: uuid.UUID | None
    approved_by_name: str | None
    created_by_user_id: uuid.UUID | None
    created_by_name: str | None
    created_at: datetime
    caller_may_approve: bool


# ── Salaries ────────────────────────────────────────────────────────────────


class SalaryRecordRead(BaseModel):
    """One row of an employee's effective-dated salary series."""

    id: uuid.UUID
    employee_id: uuid.UUID
    structure_id: uuid.UUID
    structure_name: str | None
    ctc_annual: Money
    currency: str
    effective_from: date
    effective_to: date | None
    revision_reason: str | None
    components: list[EmployeeSalaryComponentRead]


class SalaryOverviewRead(BaseModel):
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    designation: str | None
    status: str
    work_email: str | None = None
    photo_url: str | None = None
    current_salary: SalaryRecordRead | None


class StructureComponentDetailRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    component_code: str
    component_name: str
    component_type: ComponentType
    calculation_type: CalculationType
    value: Money | None
    base_component_code: str | None
    formula: str | None
    override_value: Money | None
    override_formula: str | None
    override_base_component_code: str | None
    counts_toward_pf: bool
    counts_toward_esi: bool
    part_of_ctc: bool
    display_order: int


class SalaryStructureDetailRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    effective_from: date
    effective_to: date | None
    applicable_grades: list[str]
    applicable_locations: list[str]
    rounding_mode: str
    rounding_precision: int
    active: bool
    components: list[StructureComponentDetailRead]


__all__ = [
    "Decimal",
    "InputNoticeRead",
    "InputSourceOptionRead",
    "InputSourceRead",
    "PayrollRunCreate",
    "PayrollRunRead",
    "PayslipInputLineRead",
    "PayslipLineRead",
    "PayslipRead",
    "PayslipSalaryRead",
    "PayslipSummaryRead",
    "RegisterRead",
    "RegisterRowRead",
    "RuleSetApprove",
    "RuleSetCreate",
    "RuleSetRead",
    "RuleSetUsedRead",
    "RunMarkPaid",
    "SalaryOverviewRead",
    "SalaryRecordRead",
    "SalaryStructureDetailRead",
    "StructureComponentDetailRead",
    "VarianceAcknowledge",
    "VarianceFlagRead",
]
