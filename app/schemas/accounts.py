"""Client accounts, their billing and costs, who works on them, and their P&L."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AccountEntryCreate",
    "AccountEntryRead",
    "AccountEntryUpdate",
    "AccountImportRead",
    "AccountImportRowRead",
    "AccountPnlMonth",
    "AccountPnlPerson",
    "AccountPnlRead",
    "AccountPnlRow",
    "AllocationCreate",
    "AllocationEnd",
    "AllocationRead",
    "ClientAccountCreate",
    "ClientAccountRead",
    "ClientAccountUpdate",
    "PnlDetailRead",
]

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
#: Larger than any invoice a mid-market company raises; refuses a typo of extra digits.
MAX_AMOUNT = Decimal("999999999999.99")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def _amount(value: Decimal, *, allow_negative: bool) -> Decimal:
    if not value.is_finite():
        raise ValueError("Enter an amount.")
    if value != value.quantize(Decimal("0.01")):
        raise ValueError("Use at most two decimals.")
    if value == 0:
        raise ValueError("Enter an amount other than zero.")
    if value < 0 and not allow_negative:
        raise ValueError("The amount cannot be negative.")
    if abs(value) > MAX_AMOUNT:
        raise ValueError("That amount is too large.")
    return value


def _month(value: str) -> str:
    value = value.strip()
    if not _MONTH.match(value):
        raise ValueError("Pick a month.")
    if int(value[:4]) < 2000:
        raise ValueError("Pick a month from 2000 on.")
    return value


# ── Accounts ─────────────────────────────────────────────────────────────────


class ClientAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=160)
    client_name: str | None = Field(default=None, max_length=160)
    legal_entity_id: uuid.UUID | None = None
    owner_employee_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_\-/.]*", value):
            raise ValueError("Use letters, digits, - _ / or . (no spaces).")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = _clean(value)
        if not cleaned:
            raise ValueError("Give the account a name.")
        return cleaned

    @field_validator("client_name", "notes")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _clean(value)


class ClientAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    client_name: str | None = Field(default=None, max_length=160)
    legal_entity_id: uuid.UUID | None = None
    owner_employee_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _clean(value)
        if not cleaned:
            raise ValueError("Give the account a name.")
        return cleaned

    @field_validator("client_name", "notes")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _clean(value)


class ClientAccountRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    client_name: str | None
    legal_entity_id: uuid.UUID | None
    legal_entity_name: str | None
    owner_employee_id: uuid.UUID | None
    owner_name: str | None
    is_active: bool
    notes: str | None
    #: People mapped to it today, and their shares added up (2 people at 50% = 1.00).
    people_now: int
    mapped_fte: Decimal
    created_at: datetime | None


# ── Billing and costs ───────────────────────────────────────────────────────


class AccountEntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: uuid.UUID
    #: "billing" (an invoice; negative for a credit note) or "cost" (a direct cost).
    kind: str
    #: YYYY-MM.
    month: str
    amount: Decimal
    #: Invoice number for a billing; what it was (Travel, Software…) for a cost.
    reference: str | None = Field(default=None, max_length=120)
    entry_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("billing", "cost"):
            raise ValueError("Choose billing or cost.")
        return value

    @field_validator("month")
    @classmethod
    def _month(cls, value: str) -> str:
        return _month(value)

    @field_validator("reference", "notes")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _clean(value)

    @model_validator(mode="after")
    def _signed(self) -> AccountEntryCreate:
        self.amount = _amount(self.amount, allow_negative=self.kind == "billing")
        return self


class AccountEntryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    month: str | None = None
    amount: Decimal | None = None
    reference: str | None = Field(default=None, max_length=120)
    entry_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("month")
    @classmethod
    def _month(cls, value: str | None) -> str | None:
        return None if value is None else _month(value)

    @field_validator("reference", "notes")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _clean(value)


class AccountEntryRead(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    account_code: str
    account_name: str
    kind: str
    month: str
    amount: Decimal
    reference: str | None
    entry_date: date | None
    notes: str | None
    created_at: datetime | None


class AccountImportRowRead(BaseModel):
    row: int
    #: ready, error or skipped (a blank row).
    status: str
    account_code: str | None
    kind: str | None
    month: str | None
    amount: Decimal | None
    reference: str | None
    message: str | None


class AccountImportRead(BaseModel):
    filename: str
    rows: list[AccountImportRowRead]
    ready: int
    errors: int
    skipped: int
    saved: int = 0


# ── People on accounts ──────────────────────────────────────────────────────


class AllocationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    account_id: uuid.UUID
    percent: Decimal
    effective_from: date
    effective_to: date | None = None

    @field_validator("percent")
    @classmethod
    def _percent(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("Enter a share.")
        if value != value.quantize(Decimal("0.01")):
            raise ValueError("Use at most two decimals.")
        if value <= 0 or value > 100:
            raise ValueError("The share is more than 0 and at most 100%.")
        return value

    @model_validator(mode="after")
    def _dates(self) -> AllocationCreate:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("The last day cannot be before the first.")
        return self


class AllocationEnd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The last day the person works on the account.
    last_day: date


class AllocationRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    department_name: str | None
    account_id: uuid.UUID
    account_code: str
    account_name: str
    percent: Decimal
    effective_from: date
    effective_to: date | None
    active: bool


# ── P&L ─────────────────────────────────────────────────────────────────────


class AccountPnlRow(BaseModel):
    account_id: uuid.UUID
    code: str
    name: str
    billed: Decimal
    people_cost: Decimal
    other_cost: Decimal
    total_cost: Decimal
    margin: Decimal
    #: Margin as a share of billing; null when nothing was billed.
    margin_pct: Decimal | None
    #: People whose cost was mapped to the account in the period.
    people: int


class AccountPnlMonth(BaseModel):
    month: str
    billed: Decimal
    people_cost: Decimal
    other_cost: Decimal
    total_cost: Decimal
    margin: Decimal
    margin_pct: Decimal | None


class AccountPnlPerson(BaseModel):
    employee_id: uuid.UUID
    employee_code: str
    name: str
    #: Their average share of the period on this account, in percent.
    share_pct: Decimal
    cost: Decimal


class AccountPnlRead(BaseModel):
    start: str
    end: str
    months: list[str]
    accounts: list[AccountPnlRow]
    totals: AccountPnlRow | None
    #: What payroll paid in the period, and the part mapped to no account.
    payroll_cost: Decimal
    unmapped_cost: Decimal
    notes: list[str]


class PnlDetailRead(BaseModel):
    account: ClientAccountRead
    start: str
    end: str
    summary: AccountPnlRow
    months: list[AccountPnlMonth]
    people: list[AccountPnlPerson]
    notes: list[str]
