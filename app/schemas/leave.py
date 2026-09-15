"""Time-off request and response schemas.

Day quantities are `Decimal` and serialise as strings, the same way money does:
a half-day balance that drifts through a float in the browser is still wrong.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.leave import LeaveRequestStatus

AccrualFrequency = Literal["monthly", "quarterly", "annual", "on_joining"]


# ── Catalogue ───────────────────────────────────────────────────────────────


class LeaveTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9_]+$")
    description: str | None = Field(default=None, max_length=2000)
    is_paid: bool = True
    allows_half_day: bool = True
    requires_document: bool = False


class LeaveTypeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    description: str | None
    is_paid: bool
    allows_half_day: bool
    requires_document: bool
    is_statutory: bool


class LeavePolicyCreate(BaseModel):
    leave_type_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    location_id: uuid.UUID | None = None
    grade: str | None = Field(default=None, max_length=32)
    annual_quota_days: Decimal = Field(ge=0, le=366, decimal_places=2)
    accrual_frequency: AccrualFrequency = "monthly"
    max_carry_forward_days: Decimal = Field(default=Decimal(0), ge=0, le=366, decimal_places=2)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=366)
    min_notice_days: int = Field(default=0, ge=0, le=365)
    allows_negative_balance: bool = False
    effective_from: date


class LeavePolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    leave_type_id: uuid.UUID
    name: str
    location_id: uuid.UUID | None
    grade: str | None
    annual_quota_days: Decimal
    accrual_frequency: str
    max_carry_forward_days: Decimal
    max_consecutive_days: int | None
    min_notice_days: int
    allows_negative_balance: bool
    effective_from: date
    effective_to: date | None
    is_active: bool


class HolidayCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    holiday_date: date
    state_code: str | None = Field(default=None, min_length=2, max_length=4)
    location_id: uuid.UUID | None = None
    is_optional: bool = False


class HolidayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    holiday_date: date
    state_code: str | None
    location_id: uuid.UUID | None
    is_optional: bool


# ── Requests ────────────────────────────────────────────────────────────────


class LeaveRequestCreate(BaseModel):
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    is_half_day: bool = False
    reason: str | None = Field(default=None, max_length=2000)
    document_url: str | None = Field(default=None, max_length=1024)
    #: HR raising a request on somebody's behalf. Omitted means "for me".
    employee_id: uuid.UUID | None = None


class LeaveDecision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class ApprovalStepRead(BaseModel):
    step: int
    role: str
    approver_name: str
    status: str
    decided_by_name: str | None = None
    decided_at: datetime | None = None
    note: str | None = None


class LeaveRequestRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_photo_url: str | None
    leave_type_id: uuid.UUID
    leave_type_name: str
    start_date: date
    end_date: date
    total_days: Decimal
    is_half_day: bool
    reason: str | None
    document_url: str | None
    status: LeaveRequestStatus
    approval_chain: list[ApprovalStepRead]
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime
    #: True when the caller may approve or reject this request right now.
    can_decide: bool = False
    #: True when the caller may cancel it.
    can_cancel: bool = False


class SkippedHoliday(BaseModel):
    date: date
    name: str


class LeavePreviewRead(BaseModel):
    total_days: Decimal
    working_days: int
    weekly_offs: list[date]
    holidays: list[SkippedHoliday]
    available_before: Decimal | None
    pending_days: Decimal | None
    available_after: Decimal | None
    approvers: list[str]
    allowed: bool
    problems: list[str]
    warnings: list[str]


class LeaveBalanceRead(BaseModel):
    leave_type_id: uuid.UUID
    leave_type_name: str
    leave_type_code: str
    is_paid: bool
    #: Statutory entitlements (maternity, for example) that apply on an event
    #: rather than being drawn down through the year.
    is_statutory: bool
    year: int
    policy_name: str | None
    annual_quota_days: Decimal | None
    accrual_frequency: str | None
    opening: Decimal
    accrued: Decimal
    carried_forward: Decimal
    used: Decimal
    encashed: Decimal
    pending: Decimal
    available: Decimal
    available_after_pending: Decimal


class WhoIsOutRead(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    photo_url: str | None
    start_date: date
    end_date: date
    is_half_day: bool
    status: LeaveRequestStatus
    #: Only for yourself, your reporting line and HR. Everybody else learns
    #: that a colleague is out, not why.
    leave_type_name: str | None


class AccrualRunRead(BaseModel):
    year: int
    as_of: date
    balances_updated: int
