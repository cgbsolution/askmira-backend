"""Income tax declaration, proof and projection contracts (slice 07).

Money is `Decimal` and serialises as a string. A tax figure that passes through a
JSON number becomes a float on the other side, and a figure that drifts in the
browser is still a wrong figure.

Tax financial years are written "2026-27".
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.tax import (
    DeclarationStatus,
    DeclarationWindow,
    HouseRentDeclaration,
    PreviousEmployerIncome,
    ProofStatus,
    TaxDeclaration,
    TaxProof,
    TaxRegime,
    WindowType,
)

# ── Windows ─────────────────────────────────────────────────────────────────


class WindowUpsert(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    window_type: WindowType
    opens_at: date
    closes_at: date
    allow_late_submission: bool = False
    reminder_days_before: list[int] | None = Field(default=None, max_length=12)


class WindowRead(BaseModel):
    id: uuid.UUID
    financial_year: str
    window_type: WindowType
    opens_at: date
    closes_at: date
    allow_late_submission: bool
    is_open: bool
    reminder_days_before: list[int]

    @classmethod
    def from_model(cls, window: DeclarationWindow, today: date) -> WindowRead:
        return cls(
            id=window.id,
            financial_year=window.financial_year,
            window_type=window.window_type,
            opens_at=window.opens_at,
            closes_at=window.closes_at,
            allow_late_submission=window.allow_late_submission,
            is_open=window.is_open(today)
            or (window.allow_late_submission and today > window.closes_at),
            reminder_days_before=[int(day) for day in (window.reminder_days_before or [])],
        )


class ReminderRunRead(BaseModel):
    windows_checked: int
    notifications_raised: int
    events: list[str]


# ── Regime ──────────────────────────────────────────────────────────────────


class RegimeElectionRequest(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    regime: TaxRegime
    employee_id: uuid.UUID | None = None


class RegimeElectionResponse(BaseModel):
    financial_year: str
    regime: TaxRegime
    switch_count: int
    full_year_recomputed: bool
    months_recomputed: int = 0


class RegimeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    financial_year: str
    regime: TaxRegime
    elected_at: datetime
    locked_at: datetime | None
    switch_count: int
    previous_regime: TaxRegime | None


# ── Declarations and proofs ─────────────────────────────────────────────────


class DeclarationUpsert(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    section: str = Field(min_length=2, max_length=16, pattern=r"^[A-Za-z0-9]+$")
    sub_category: str = Field(default="", max_length=64)
    declared_amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    description: str | None = Field(default=None, max_length=255)
    employee_id: uuid.UUID | None = None


class ProofRead(BaseModel):
    id: uuid.UUID
    declaration_id: uuid.UUID
    proof_amount: Decimal
    accepted_amount: Decimal
    status: ProofStatus
    verifier_remarks: str | None
    verified_at: datetime | None
    document_name: str | None
    has_document: bool
    created_at: datetime

    @classmethod
    def from_model(cls, proof: TaxProof) -> ProofRead:
        return cls(
            id=proof.id,
            declaration_id=proof.declaration_id,
            proof_amount=Decimal(str(proof.proof_amount)),
            accepted_amount=Decimal(str(proof.accepted_amount or 0)),
            status=proof.status,
            verifier_remarks=proof.verifier_remarks,
            verified_at=proof.verified_at,
            document_name=proof.document_name,
            has_document=bool(proof.document_storage_key),
            created_at=proof.created_at,
        )


class DeclarationRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    financial_year: str
    section: str
    sub_category: str
    description: str | None
    declared_amount: Decimal
    accepted_amount: Decimal
    status: DeclarationStatus
    submitted_at: datetime | None
    locked_at: datetime | None
    proofs: list[ProofRead]

    @classmethod
    def from_model(cls, declaration: TaxDeclaration) -> DeclarationRead:
        return cls(
            id=declaration.id,
            employee_id=declaration.employee_id,
            financial_year=declaration.financial_year,
            section=declaration.section,
            sub_category=declaration.sub_category,
            description=declaration.description,
            declared_amount=Decimal(str(declaration.declared_amount)),
            accepted_amount=Decimal(str(declaration.accepted_amount)),
            status=declaration.status,
            submitted_at=declaration.submitted_at,
            locked_at=declaration.locked_at,
            proofs=[
                ProofRead.from_model(proof)
                for proof in sorted(declaration.proofs, key=lambda p: p.created_at)
            ],
        )


class ProofDecision(BaseModel):
    decision: ProofStatus
    accepted_amount: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    remarks: str = Field(default="", max_length=2000)


class ProofQueueItem(BaseModel):
    id: uuid.UUID
    declaration_id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    financial_year: str
    section: str
    sub_category: str
    declared_amount: Decimal
    proof_amount: Decimal
    accepted_amount: Decimal
    status: ProofStatus
    verifier_remarks: str | None
    verified_at: datetime | None
    document_name: str | None
    has_document: bool
    created_at: datetime


# ── House rent and previous employer ────────────────────────────────────────


class RentMonth(BaseModel):
    month: int = Field(ge=1, le=12)
    year: int = Field(ge=2000, le=2100)
    rent: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class HouseRentUpsert(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    monthly_rent: list[RentMonth] = Field(min_length=1, max_length=12)
    landlord_name: str | None = Field(default=None, max_length=255)
    landlord_pan: str | None = Field(default=None, pattern=r"^[A-Za-z]{5}[0-9]{4}[A-Za-z]$")
    property_address: str | None = Field(default=None, max_length=2000)
    is_metro: bool = False
    employee_id: uuid.UUID | None = None


class HouseRentRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    financial_year: str
    monthly_rent: list[dict[str, Any]]
    annual_rent: Decimal
    landlord_name: str | None
    landlord_pan: str | None
    property_address: str | None
    is_metro: bool
    status: ProofStatus

    @classmethod
    def from_model(cls, record: HouseRentDeclaration) -> HouseRentRead:
        return cls(
            id=record.id,
            employee_id=record.employee_id,
            financial_year=record.financial_year,
            monthly_rent=list(record.monthly_rent or []),
            annual_rent=Decimal(str(record.annual_rent)),
            landlord_name=record.landlord_name,
            landlord_pan=record.landlord_pan,
            property_address=record.property_address,
            is_metro=record.is_metro,
            status=record.status,
        )


class PreviousEmployerUpsert(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    employer_name: str | None = Field(default=None, max_length=255)
    gross_salary: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    tds_deducted: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    pf_deducted: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    professional_tax: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    exempt_allowances: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)


class PreviousEmployerRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    financial_year: str
    employer_name: str | None
    gross_salary: Decimal
    tds_deducted: Decimal
    pf_deducted: Decimal
    professional_tax: Decimal
    exempt_allowances: Decimal
    has_document: bool
    status: ProofStatus

    @classmethod
    def from_model(cls, record: PreviousEmployerIncome) -> PreviousEmployerRead:
        return cls(
            id=record.id,
            employee_id=record.employee_id,
            financial_year=record.financial_year,
            employer_name=record.employer_name,
            gross_salary=Decimal(str(record.gross_salary)),
            tds_deducted=Decimal(str(record.tds_deducted)),
            pf_deducted=Decimal(str(record.pf_deducted)),
            professional_tax=Decimal(str(record.professional_tax)),
            exempt_allowances=Decimal(str(record.exempt_allowances)),
            has_document=bool(record.document_storage_key),
            status=record.status,
        )


# ── Projection ──────────────────────────────────────────────────────────────


class SnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    financial_year: str
    regime: TaxRegime
    computed_for_month: int
    gross_annual_income: Decimal
    taxable_income: Decimal
    annual_tax_liability: Decimal
    monthly_tds: Decimal
    working: dict
    created_at: datetime


# ── Form 12BB ───────────────────────────────────────────────────────────────


class Form12BBIssue(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    parent_name: str = Field(min_length=1, max_length=255)
    place: str = Field(min_length=1, max_length=128)
    employee_id: uuid.UUID | None = None
