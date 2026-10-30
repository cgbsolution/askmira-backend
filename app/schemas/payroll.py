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
    WAGE_GROUP_TYPE,
    CalculationType,
    ComponentType,
    RoundingMode,
    WageGroup,
    wage_group_of,
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
    #: Where it is listed on the Salary master. Empty: the kind's usual group.
    wage_group: WageGroup | None = None

    @field_validator("code", "base_component_code")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class SalaryComponentCreate(SalaryComponentBase):
    @model_validator(mode="after")
    def _group_fits_kind(self) -> SalaryComponentCreate:
        if (
            self.wage_group is not None
            and WAGE_GROUP_TYPE[self.wage_group] is not self.component_type
        ):
            raise ValueError(
                f"A {self.wage_group.value.replace('_', ' ')} wage type has to be a "
                f"{WAGE_GROUP_TYPE[self.wage_group].value.replace('_', ' ')}"
            )
        return self


class SalaryComponentRead(SalaryComponentBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_system: bool
    active: bool

    @model_validator(mode="after")
    def _effective_group(self) -> SalaryComponentRead:
        self.wage_group = wage_group_of(self.component_type, self.wage_group)
        return self


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
    wage_group: WageGroup | None = None


class SalaryTypeTotalRead(BaseModel):
    """What one kind of component (earnings, deductions…) adds up to."""

    component_type: ComponentType
    monthly: Money
    annual: Money


class SalaryGroupTotalRead(BaseModel):
    """What one wage group (regular earnings, provisions…) adds up to."""

    wage_group: WageGroup
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
    group_totals: list[SalaryGroupTotalRead] = Field(default_factory=list)
    #: MONTHLY GROSS: as typed when it was, else the earnings' sum.
    gross_monthly: Money | None = None
    gross_annual: Money | None = None
    #: The earnings' own sum a month, to compare a typed gross with.
    earnings_monthly: Money | None = None
    #: What would stop this from saving, said while HR is still typing.
    warnings: list[str] = Field(default_factory=list)


# ── Employee salary ─────────────────────────────────────────────────────────


#: The Salary master's Pay Basis: what period a typed amount is for. Weekly,
#: daily and hourly figures become monthly ones on the server (see
#: `service.to_period_amounts`), never in the browser.
PayBasis = Literal["annual", "monthly", "weekly", "daily", "hourly"]
_TWO_PLACES = Decimal("0.01")
#: The largest single amount a person may type: ₹10 crore. Big enough for any
#: real salary in any pay basis, small enough that an hourly figure turned into
#: a year's still fits the database's money columns.
MAX_TYPED = Decimal("100000000")


class SalaryComponentAmountInput(BaseModel):
    """One wage type on a salary: an amount typed for this employee, a remark, or both.

    Leaving `amount` empty lets the structure work the component out (or, in a
    manual entry, leaves the wage type off). A wage type the structure does not
    have is paid only when it has an amount.
    """

    component_code: str = Field(min_length=1, max_length=32)
    #: Negative only on a record of what a person who has left was paid, brought
    #: in as the old system had it (Salary master bulk upload); every other
    #: save refuses it (`resolve_amounts`, strict).
    amount: Decimal | None = Field(default=None, ge=-MAX_TYPED, le=MAX_TYPED)
    #: What period `amount` is for.
    basis: PayBasis = "monthly"
    remarks: str | None = Field(default=None, max_length=255)

    @field_validator("remarks")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @field_validator("amount")
    @classmethod
    def _paise(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value != value.quantize(_TWO_PLACES):
            raise ValueError(f"{value} has more than two decimals")
        return value


class SalaryAmountsInput(BaseModel):
    """A CTC and the wage types typed against it — what a preview resolves."""

    #: "manual": every amount is taken exactly as typed, the CTC too, and a
    #: wage type left empty is not paid (the Salary master's way). "structure":
    #: the structure splits the CTC, and typed amounts override it.
    entry: Literal["structure", "manual"] = "structure"
    #: The CTC as a year, as a month, or as an amount in any Pay Basis.
    ctc_annual: Decimal | None = Field(default=None, gt=0, le=MAX_TYPED * 12)
    ctc_monthly: Decimal | None = Field(default=None, gt=0, le=MAX_TYPED)
    ctc_amount: Decimal | None = Field(default=None, gt=0, le=MAX_TYPED)
    ctc_basis: PayBasis = "monthly"
    #: MONTHLY GROSS as typed (manual entry only). Empty: the earnings' sum.
    gross_amount: Decimal | None = Field(default=None, ge=0, le=MAX_TYPED)
    gross_basis: PayBasis = "monthly"
    components: list[SalaryComponentAmountInput] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _one_ctc_and_unique_codes(self) -> SalaryAmountsInput:
        given = [v for v in (self.ctc_annual, self.ctc_monthly, self.ctc_amount) if v is not None]
        if len(given) != 1:
            raise ValueError("Give the CTC once: as a yearly, a monthly or a pay-basis amount")
        if given[0] != given[0].quantize(_TWO_PLACES):
            raise ValueError(f"CTC {given[0]} has more than two decimals")
        if self.gross_amount is not None and self.gross_amount != self.gross_amount.quantize(
            _TWO_PLACES
        ):
            raise ValueError(f"Monthly gross {self.gross_amount} has more than two decimals")
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
    wage_group: WageGroup | None = None
    #: The amount as typed, in `pay_basis` (a day's figure for a daily one).
    entered_amount: Money | None = None


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
    #: The CTC as the editor shows it back: monthly when the yearly figure is
    #: exactly twelve months of it, else yearly.
    ctc_basis: PayBasis = "annual"
    ctc_amount: Money | None = None
    #: MONTHLY GROSS as typed, in `gross_basis`; null when it was left to the earnings.
    gross_amount: Money | None = None
    gross_basis: PayBasis | None = None
    gross_annual: Money | None = None
    earnings_monthly: Money | None = None
    #: Every amount on it was typed (the Salary master's manual entry).
    manual_entry: bool = False
    gross_monthly: Money
    basic_monthly: Money | None
    currency: str
    effective_from: date
    effective_to: date | None
    revision_reason: str | None
    attachment_name: str | None
    components: list[EmployeeSalaryComponentRead]
    totals: list[SalaryTypeTotalRead]
    group_totals: list[SalaryGroupTotalRead] = Field(default_factory=list)
    #: Payroll has used this revision, so it is history and cannot change.
    locked: bool
    locked_reason: str | None
    updated_at: datetime


class SalaryDeleteRead(BaseModel):
    runs_to_recompute: list[str] = Field(default_factory=list)


class SalaryTrailEntryRead(BaseModel):
    """One change to a person's salary, from the audit log."""

    id: uuid.UUID
    at: datetime
    action: str
    actor_name: str | None
    summary: str | None
    #: The audit entry's own record of what changed: `before`/`after` on an
    #: edit, the new figures on an assignment, `deleted` on a delete.
    changes: dict


class StandardSetupRead(BaseModel):
    """What setting up the standard wage types added."""

    components_added: list[str]
    #: Wage types still under AskMira's earlier default names, now the template's.
    components_renamed: list[str] = Field(default_factory=list)
    structure_name: str | None
    #: Why the structure was not created, when the workspace's own wage types
    #: under the standard codes are worked out differently.
    structure_skipped: str | None = None


class SalaryExportInput(BaseModel):
    """Whose present salary to export; empty exports everyone."""

    employee_ids: list[uuid.UUID] | None = Field(default=None, max_length=20000)


class StandardStatusRead(BaseModel):
    """Whether the workspace has the Salary Master template's wage types."""

    complete: bool
    missing: list[str]
    outdated: list[str]
    has_structure: bool


class SalaryImportRowRead(BaseModel):
    """One row of an uploaded salary sheet, as it would be (or was) applied."""

    row: int
    employee_code: str | None
    employee_name: str | None
    effective_from: date | None
    structure_name: str | None
    ctc_annual: Money | None
    gross_monthly: Money | None
    typed: int = 0
    #: "ready", "error" or "skipped" (a row with no CTC, such as an untouched one).
    status: Literal["ready", "error", "skipped"]
    message: str | None = None
    #: On a ready row, what HR should know before saving: statutory columns not
    #: used, a CTC or gross that differs from the sheet, revisions it replaces.
    notes: list[str] = Field(default_factory=list)


class SalaryImportRead(BaseModel):
    filename: str
    rows: list[SalaryImportRowRead]
    ready: int
    errors: int
    skipped: int
    #: Ready rows that carry notes.
    noted: int = 0
    #: True once the ready rows were saved. Nothing is saved while any row has an error.
    applied: bool = False
