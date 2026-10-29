"""Organisation structure contracts: legal entities, locations, departments."""

from __future__ import annotations

import uuid
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: How a month's salary is divided into days (see `services/payroll/salary_days`).
SalaryDaysBasis = Literal["calendar", "fixed", "working_days"]

#: ISO 3166-2:IN subdivision codes, including the older spellings still found in
#: payroll data (CT/CG, OR/OD, UT/UK, TG/TS, DN/DD merged into DH). A location's
#: state drives Professional Tax and the holiday calendar, so a typo here is a
#: payroll error weeks later — it is refused rather than stored.
INDIAN_STATE_CODES: frozenset[str] = frozenset(
    {
        "AN",
        "AP",
        "AR",
        "AS",
        "BR",
        "CH",
        "CG",
        "CT",
        "DH",
        "DN",
        "DD",
        "DL",
        "GA",
        "GJ",
        "HP",
        "HR",
        "JH",
        "JK",
        "KA",
        "KL",
        "LA",
        "LD",
        "MH",
        "ML",
        "MN",
        "MP",
        "MZ",
        "NL",
        "OD",
        "OR",
        "PB",
        "PY",
        "RJ",
        "SK",
        "TG",
        "TS",
        "TN",
        "TR",
        "UK",
        "UT",
        "UP",
        "WB",
    }
)

CODE = r"^[A-Za-z0-9_\-]+$"
PAN = r"^[A-Z]{5}[0-9]{4}[A-Z]$"
TAN = r"^[A-Z]{4}[0-9]{5}[A-Z]$"
GSTIN = r"^[0-9]{2}[A-Z0-9]{13}$"
PINCODE = r"^[1-9][0-9]{5}$"


def _upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _state(value: object) -> object:
    if value is None:
        return value
    code = str(value).strip().upper()
    if code not in INDIAN_STATE_CODES:
        raise ValueError(f"'{value}' is not an Indian state or union territory code")
    return code


def _timezone(value: object) -> object:
    if value is None:
        return value
    try:
        ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"'{value}' is not a known time zone") from exc
    return value


# ── Legal entities ──────────────────────────────────────────────────────────


class LegalEntityFields(BaseModel):
    pan: str | None = Field(default=None, pattern=PAN)
    tan: str | None = Field(default=None, pattern=TAN)
    gstin: str | None = Field(default=None, pattern=GSTIN)
    pf_registration_no: str | None = Field(default=None, max_length=32)
    esi_registration_no: str | None = Field(default=None, max_length=32)
    registered_address: str | None = Field(default=None, max_length=512)
    #: Professional Tax registration number per state code.
    pt_registrations: dict[str, str] | None = None
    #: The work week for this company's people with no shift of their own.
    default_shift_id: uuid.UUID | None = None
    #: How a month's salary is divided into days: calendar | fixed | working_days.
    salary_days_basis: SalaryDaysBasis | None = None
    #: The divisor for `fixed`, usually 26 or 30.
    salary_fixed_days: int | None = Field(default=None, ge=20, le=31)

    @field_validator("pan", "tan", "gstin", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> object:
        return _upper(value) or None

    @field_validator("pt_registrations")
    @classmethod
    def _pt_states(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return value
        return {str(_state(state)): number.strip() for state, number in value.items()}


class LegalEntityCreate(LegalEntityFields):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=32, pattern=CODE)

    @model_validator(mode="after")
    def _fixed_needs_days(self) -> LegalEntityCreate:
        if self.salary_days_basis == "fixed" and self.salary_fixed_days is None:
            raise ValueError("Say how many days a month is divided by, such as 26 or 30.")
        return self


class LegalEntityUpdate(LegalEntityFields):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    code: str | None = Field(default=None, min_length=1, max_length=32, pattern=CODE)
    is_active: bool | None = None


class LegalEntityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    pan: str | None
    tan: str | None
    gstin: str | None
    pf_registration_no: str | None
    esi_registration_no: str | None
    pt_registrations: dict
    registered_address: str | None
    is_active: bool
    default_shift_id: uuid.UUID | None = None
    salary_days_basis: str = "calendar"
    salary_fixed_days: int | None = None
    #: The company's logo (a path on the API), or None when it has none.
    logo_url: str | None = None
    #: People in this company who have not left. Set on the list only.
    headcount: int | None = None


# ── Locations ───────────────────────────────────────────────────────────────


class LocationFields(BaseModel):
    city: str | None = Field(default=None, max_length=128)
    pincode: str | None = Field(default=None, pattern=PINCODE)

    @field_validator("pincode", mode="before")
    @classmethod
    def _blank_pincode(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value


class LocationCreate(LocationFields):
    legal_entity_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=32, pattern=CODE)
    state_code: str
    timezone: str = "Asia/Kolkata"

    _check_state = field_validator("state_code", mode="before")(_state)
    _check_zone = field_validator("timezone")(_timezone)


class LocationUpdate(LocationFields):
    model_config = ConfigDict(extra="forbid")

    legal_entity_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    code: str | None = Field(default=None, min_length=1, max_length=32, pattern=CODE)
    state_code: str | None = None
    timezone: str | None = None
    is_active: bool | None = None

    _check_state = field_validator("state_code", mode="before")(_state)
    _check_zone = field_validator("timezone")(_timezone)


class LocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID
    name: str
    code: str
    city: str | None
    state_code: str
    country_code: str
    pincode: str | None
    timezone: str
    is_active: bool


# ── Departments ─────────────────────────────────────────────────────────────


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=32, pattern=CODE)
    parent_id: uuid.UUID | None = None
    head_employee_id: uuid.UUID | None = None


class DepartmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    code: str | None = Field(default=None, min_length=1, max_length=32, pattern=CODE)
    parent_id: uuid.UUID | None = None
    head_employee_id: uuid.UUID | None = None
    is_active: bool | None = None


class DepartmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    parent_id: uuid.UUID | None
    head_employee_id: uuid.UUID | None
    is_active: bool
