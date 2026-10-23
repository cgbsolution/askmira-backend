"""Payroll contracts.

Money crosses the wire as `Decimal`. Pydantic serialises it as a JSON number by
default, which is a float on the other side — so every money field is annotated
to serialise as a string. A payroll figure must survive the round trip exactly.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from app.models.payroll import (
    CalculationType,
    ComponentType,
    RoundingMode,
)

# Serialise Decimal as a string, never as a JSON float.
Money = Annotated[Decimal, PlainSerializer(lambda v: str(v), return_type=str)]


# ── Components ──────────────────────────────────────────────────────────────


class SalaryComponentBase(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z][A-Z0-9_]*$")
    name: str = Field(min_length=1, max_length=128)
    component_type: ComponentType
    calculation_type: CalculationType = CalculationType.FIXED
    base_component_code: str | None = Field(default=None, max_length=32)
    value: Decimal | None = None
    formula: str | None = Field(default=None, max_length=512)

    taxable: bool = True
    part_of_ctc: bool = True
    pro_rate_on_lop: bool = True
    counts_toward_pf: bool = False
    counts_toward_esi: bool = False
    display_order: int = 0

    @field_validator("code", "base_component_code")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class SalaryComponentCreate(SalaryComponentBase):
    pass


class SalaryComponentRead(SalaryComponentBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_system: bool
    active: bool


# ── Structures ──────────────────────────────────────────────────────────────


class StructureComponentInput(BaseModel):
    """A component's membership in a structure, with optional overrides."""

    component_code: str = Field(min_length=1, max_length=32)
    override_value: Decimal | None = None
    override_formula: str | None = Field(default=None, max_length=512)
    override_base_component_code: str | None = Field(default=None, max_length=32)
    display_order: int = 0

    @field_validator("component_code", "override_base_component_code")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class SalaryStructureCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    effective_from: date
    effective_to: date | None = None
    applicable_grades: list[str] = Field(default_factory=list)
    applicable_locations: list[str] = Field(default_factory=list)
    rounding_mode: RoundingMode = RoundingMode.HALF_UP
    rounding_precision: int = Field(default=0, ge=0, le=2)
    components: list[StructureComponentInput] = Field(min_length=1)

    @field_validator("effective_to")
    @classmethod
    def _ordered(cls, value: date | None, info) -> date | None:
        start = info.data.get("effective_from")
        if value and start and value < start:
            raise ValueError("effective_to cannot be before effective_from")
        return value


class StructureComponentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    component_code: str
    component_name: str
    component_type: ComponentType
    calculation_type: CalculationType
    override_value: Decimal | None
    display_order: int


class SalaryStructureRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    effective_from: date
    effective_to: date | None
    applicable_grades: list[str]
    applicable_locations: list[str]
    rounding_mode: RoundingMode
    rounding_precision: int
    active: bool


# ── Resolution / preview ────────────────────────────────────────────────────


class ResolvedComponentRead(BaseModel):
    code: str
    name: str
    component_type: ComponentType
    annual_amount: Money
    monthly_amount: Money
    taxable: bool
    part_of_ctc: bool
    pro_rate_on_lop: bool
    counts_toward_pf: bool
    counts_toward_esi: bool
    display_order: int
    is_manual: bool = False


class SalaryTypeTotalRead(BaseModel):
    """What one kind of component (earnings, deductions…) adds up to."""

    component_type: ComponentType
    monthly: Money
    annual: Money


class SalaryBreakdownRead(BaseModel):
    """What the CTC builder screen renders.

    `annual_total_in_ctc` is returned rather than left for the client to sum:
    the exactness of that number against `ctc_annual` is the guarantee this
    endpoint makes, and it should be visible in the response.
    """

    structure_id: uuid.UUID
    structure_name: str
    ctc_annual: Money
    annual_total_in_ctc: Money
    monthly_total_in_ctc: Money
    components: list[ResolvedComponentRead]
    totals: list[SalaryTypeTotalRead] = Field(default_factory=list)


# ── Employee salary ─────────────────────────────────────────────────────────


PayBasis = Literal["monthly", "annual"]


class SalaryComponentAmountInput(BaseModel):
    """One wage type on a salary: an amount typed for this employee, a remark, or both.

    Leaving `amount` empty lets the structure work the component out. A wage
    type the structure does not have is paid only when it has an amount.
    """

    component_code: str = Field(min_length=1, max_length=32)
    amount: Decimal | None = Field(default=None, ge=0)
    #: Whether `amount` is a month's or a year's figure. A monthly figure is
    #: multiplied by twelve on the server, never in the browser.
    basis: PayBasis = "monthly"
    remarks: str | None = Field(default=None, max_length=255)

    @field_validator("remarks")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class SalaryAmountsInput(BaseModel):
    """A CTC and the wage types typed against it — what a preview resolves."""

    #: Give the CTC as a year or as a month. A monthly CTC is multiplied by
    #: twelve and rounded to the structure's precision.
    ctc_annual: Decimal | None = Field(default=None, gt=0)
    ctc_monthly: Decimal | None = Field(default=None, gt=0)
    components: list[SalaryComponentAmountInput] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _one_ctc_and_unique_codes(self) -> SalaryAmountsInput:
        if (self.ctc_annual is None) == (self.ctc_monthly is None):
            raise ValueError("Give the CTC as either a yearly or a monthly amount")
        codes = [item.component_code for item in self.components]
        repeated = sorted({code for code in codes if codes.count(code) > 1})
        if repeated:
            raise ValueError(f"{', '.join(repeated)} appear more than once")
        return self


class SalaryPreviewInput(SalaryAmountsInput):
    pass


class EmployeeSalaryCreate(SalaryAmountsInput):
    structure_id: uuid.UUID
    effective_from: date
    revision_reason: str | None = Field(default=None, max_length=255)


class EmployeeSalaryUpdate(SalaryAmountsInput):
    """Correct a revision payroll has not used yet."""

    structure_id: uuid.UUID
    effective_from: date
    revision_reason: str | None = Field(default=None, max_length=255)
    #: Carry the typed amounts and remarks changed here into every later
    #: revision too ("Save & update later revisions").
    apply_to_later_revisions: bool = False


class EmployeeSalaryComponentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    component_code: str
    component_name: str
    component_type: ComponentType
    annual_amount: Money
    monthly_amount: Money
    taxable: bool
    part_of_ctc: bool
    counts_toward_pf: bool
    counts_toward_esi: bool
    display_order: int
    is_manual: bool = False
    remarks: str | None = None
    pay_basis: PayBasis = "monthly"


class EmployeeSalaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    structure_id: uuid.UUID
    ctc_annual: Money
    currency: str
    effective_from: date
    effective_to: date | None
    revision_reason: str | None
    approved_by_user_id: uuid.UUID | None
    components: list[EmployeeSalaryComponentRead]
    #: On a revision: months payroll already paid at the earlier salary, as
    #: "2026-08". They are trued up by raising arrears, not by this record.
    paid_months_affected: list[str] = Field(default_factory=list)
    #: Payroll runs still open (draft or in review) that were worked out on the
    #: old amounts, as "2026-08". Recompute them to pick up the change.
    runs_to_recompute: list[str] = Field(default_factory=list)
    #: On an edit saved with "update later revisions": the revisions it reached.
    carried_forward_to: list[date] = Field(default_factory=list)


class SalaryRevisionRead(BaseModel):
    """One card on the Salary master timeline."""

    id: uuid.UUID
    employee_id: uuid.UUID
    structure_id: uuid.UUID
    structure_name: str | None
    ctc_annual: Money
    ctc_monthly: Money
    gross_monthly: Money
    basic_monthly: Money | None
    currency: str
    effective_from: date
    effective_to: date | None
    revision_reason: str | None
    attachment_name: str | None
    components: list[EmployeeSalaryComponentRead]
    totals: list[SalaryTypeTotalRead]
    #: Payroll has used this revision, so it is history and cannot change.
    locked: bool
    locked_reason: str | None
    updated_at: datetime


class SalaryDeleteRead(BaseModel):
    runs_to_recompute: list[str] = Field(default_factory=list)
