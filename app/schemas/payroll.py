"""Payroll contracts.

Money crosses the wire as `Decimal`. Pydantic serialises it as a JSON number by
default, which is a float on the other side — so every money field is annotated
to serialise as a string. A payroll figure must survive the round trip exactly.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

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


# ── Employee salary ─────────────────────────────────────────────────────────


class EmployeeSalaryCreate(BaseModel):
    structure_id: uuid.UUID
    ctc_annual: Decimal = Field(gt=0)
    effective_from: date
    revision_reason: str | None = Field(default=None, max_length=255)


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
