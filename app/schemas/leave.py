"""Time-off request and response schemas.

Day quantities are `Decimal` and serialise as strings, the same way money does:
a half-day balance that drifts through a float in the browser is still wrong.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.leave import CompOffStatus, LeaveRequestStatus
from app.schemas.common import Page

AccrualFrequency = Literal["monthly", "quarterly", "annual", "on_joining"]
HalfDaySession = Literal["first_half", "second_half"]
PolicyGender = Literal["female", "male"]
HolidayScope = Literal["national", "state", "location"]
#: What HR can post by hand. Everything else in the ledger is posted by a flow.
ManualAdjustmentKind = Literal["opening", "credit", "debit"]


# ── Catalogue ───────────────────────────────────────────────────────────────


class LeaveTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9_]+$")
    description: str | None = Field(default=None, max_length=2000)
    is_paid: bool = True
    allows_half_day: bool = True
    requires_document: bool = False
    is_statutory: bool = False
    is_comp_off: bool = False


class LeaveTypeUpdate(BaseModel):
    """Everything but the code, which balances and encashment policies key on."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    is_paid: bool | None = None
    allows_half_day: bool | None = None
    requires_document: bool | None = None
    is_comp_off: bool | None = None


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
    is_comp_off: bool
    is_active: bool


class LeavePolicyCreate(BaseModel):
    leave_type_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    location_id: uuid.UUID | None = None
    grade: str | None = Field(default=None, max_length=32)
    #: Limit to women or men (maternity, paternity). Null is everyone. Only a
    #: recorded opposite gender excludes; undisclosed never does.
    applies_to_gender: PolicyGender | None = None
    annual_quota_days: Decimal = Field(ge=0, le=366, decimal_places=2)
    accrual_frequency: AccrualFrequency = "monthly"
    max_carry_forward_days: Decimal = Field(default=Decimal(0), ge=0, le=366, decimal_places=2)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=366)
    min_notice_days: int = Field(default=0, ge=0, le=365)
    allows_negative_balance: bool = False
    sandwich_weekends_holidays: bool = False
    effective_from: date


class LeavePolicyUpdate(BaseModel):
    """A correction to one policy version, in place.

    For a change that should apply only from a date, add a new version instead
    (`LeavePolicyCreate` with the same population): the old one closes the day
    before and last year's balances keep reading last year's rules.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    #: Sent as null to open the policy to everyone; left out to keep it. A
    #: maternity policy set up for "everyone" is corrected here.
    applies_to_gender: PolicyGender | None = None
    annual_quota_days: Decimal | None = Field(default=None, ge=0, le=366, decimal_places=2)
    accrual_frequency: AccrualFrequency | None = None
    max_carry_forward_days: Decimal | None = Field(default=None, ge=0, le=366, decimal_places=2)
    #: Sent as null to remove the limit; left out to keep it.
    max_consecutive_days: int | None = Field(default=None, ge=1, le=366)
    min_notice_days: int | None = Field(default=None, ge=0, le=365)
    allows_negative_balance: bool | None = None
    sandwich_weekends_holidays: bool | None = None


class LeavePolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    leave_type_id: uuid.UUID
    name: str
    location_id: uuid.UUID | None
    grade: str | None
    applies_to_gender: str | None
    annual_quota_days: Decimal
    accrual_frequency: str
    max_carry_forward_days: Decimal
    max_consecutive_days: int | None
    min_notice_days: int
    allows_negative_balance: bool
    sandwich_weekends_holidays: bool
    effective_from: date
    effective_to: date | None
    is_active: bool


class HolidayCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    holiday_date: date
    state_code: str | None = Field(default=None, min_length=2, max_length=4)
    location_id: uuid.UUID | None = None
    #: One company of the group only; null for every legal entity.
    legal_entity_id: uuid.UUID | None = None
    is_optional: bool = False
    #: Unpaid: a day off that is loss of pay unless worked.
    is_paid: bool = True

    @model_validator(mode="after")
    def _one_scope(self) -> HolidayCreate:
        if self.state_code is not None and self.location_id is not None:
            raise ValueError(
                "Choose a state or a location, not both. A location already implies its state."
            )
        return self


class HolidayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    holiday_date: date | None = None
    scope: HolidayScope | None = None
    state_code: str | None = Field(default=None, min_length=2, max_length=4)
    location_id: uuid.UUID | None = None
    #: Send null to make it apply to every legal entity again.
    legal_entity_id: uuid.UUID | None = None
    is_optional: bool | None = None
    is_paid: bool | None = None


class HolidayRead(BaseModel):
    id: uuid.UUID
    name: str
    holiday_date: date
    state_code: str | None
    location_id: uuid.UUID | None
    location_name: str | None = None
    scope: HolidayScope
    legal_entity_id: uuid.UUID | None = None
    legal_entity_name: str | None = None
    is_optional: bool
    is_paid: bool = True
    #: Whether this holiday is in scope for the caller's own work location.
    applies_to_me: bool = False
    #: An optional holiday the caller has opted in to.
    selected: bool = False


class OptionalHolidaysRead(BaseModel):
    year: int
    quota: int
    chosen: int
    holidays: list[HolidayRead]


class LeaveSettingsRead(BaseModel):
    optional_holiday_quota: int
    comp_off_expiry_days: int
    comp_off_leave_type_id: uuid.UUID | None
    comp_off_leave_type_name: str | None


class LeaveSettingsUpdate(BaseModel):
    optional_holiday_quota: int | None = Field(default=None, ge=0, le=30)
    comp_off_expiry_days: int | None = Field(default=None, ge=1, le=365)


# ── Requests ────────────────────────────────────────────────────────────────


class LeaveRequestCreate(BaseModel):
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    is_half_day: bool = False
    #: Which half, for a half day. Optional for older clients; a half day
    #: without one cannot be paired with the other half of the same date.
    half_day_session: HalfDaySession | None = None
    reason: str | None = Field(default=None, max_length=2000)
    #: A key returned by `POST /leave/attachments`, never a URL typed by hand.
    document_url: str | None = Field(default=None, max_length=1024)
    #: HR raising a request on somebody's behalf. Omitted means "for me".
    employee_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _session_only_for_half_days(self) -> LeaveRequestCreate:
        if not self.is_half_day:
            self.half_day_session = None
        return self


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


class RequestBalanceRead(BaseModel):
    """The requester's balance of the type asked for, as whoever decides sees it.

    The same figures the request check weighed it against when it was raised,
    read again now: something may have been credited or taken since.
    """

    year: int
    #: What the ledger says today, before this request.
    available: Decimal
    #: Their other undecided requests of this type in the same year.
    other_pending: Decimal
    #: `available` less this request: what approving it leaves.
    after_approval: Decimal
    #: Days expected to carry in from the year before, for a request in next year.
    projected_carry_forward: Decimal | None = None


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
    half_day_session: HalfDaySession | None = None
    reason: str | None
    document_url: str | None
    #: True when a supporting document is attached and can be downloaded.
    has_document: bool = False
    status: LeaveRequestStatus
    approval_chain: list[ApprovalStepRead]
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime
    #: True when the caller may approve or reject this request right now.
    can_decide: bool = False
    #: True when the caller may cancel it.
    can_cancel: bool = False
    #: In the approvals inbox only, for a paid type with a balance to draw on.
    balance: RequestBalanceRead | None = None


class SkippedHoliday(BaseModel):
    date: date
    name: str


class LeavePreviewRead(BaseModel):
    total_days: Decimal
    working_days: int
    weekly_offs: list[date]
    holidays: list[SkippedHoliday]
    #: Offs and holidays charged under the policy's sandwich rule.
    sandwiched: list[date] = Field(default_factory=list)
    available_before: Decimal | None
    pending_days: Decimal | None
    available_after: Decimal | None
    #: Carry-forward into a year that has not closed yet, included above.
    projected_carry_forward: Decimal | None = None
    approvers: list[str]
    allowed: bool
    problems: list[str]
    warnings: list[str]


class LeaveAttachmentRead(BaseModel):
    document_url: str
    mime_type: str
    size_bytes: int


class LeaveBalanceRead(BaseModel):
    leave_type_id: uuid.UUID
    leave_type_name: str
    leave_type_code: str
    is_paid: bool
    #: Statutory entitlements (maternity, for example) that apply on an event
    #: rather than being drawn down through the year.
    is_statutory: bool
    is_comp_off: bool = False
    year: int
    policy_name: str | None
    annual_quota_days: Decimal | None
    accrual_frequency: str | None
    opening: Decimal
    accrued: Decimal
    carried_forward: Decimal
    adjusted: Decimal = Decimal(0)
    used: Decimal
    encashed: Decimal
    pending: Decimal
    available: Decimal
    available_after_pending: Decimal


class BalanceAdjustmentCreate(BaseModel):
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    year: int = Field(ge=2000, le=2100)
    kind: ManualAdjustmentKind
    #: For "opening", the opening balance to set. For "credit" and "debit",
    #: how many days to add or take away (always positive).
    days: Decimal = Field(ge=0, le=366, decimal_places=2)
    reason: str = Field(min_length=3, max_length=1000)


class BalanceAdjustmentRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    year: int
    kind: str
    days: Decimal
    reason: str
    effective_on: date
    created_at: datetime


class BalanceHistoryEntry(BaseModel):
    on: date
    #: accrual | taken | opening | credit | debit | comp_off | comp_off_expiry |
    #: encashment | carry_forward_in | carry_forward_out | lapse
    kind: str
    label: str
    #: Signed effect on the balance.
    days: Decimal
    running_balance: Decimal
    note: str | None = None


class BalanceHistoryRead(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    leave_type_id: uuid.UUID
    leave_type_name: str
    year: int
    entries: list[BalanceHistoryEntry]
    available: Decimal


class TeamBalanceColumn(BaseModel):
    """A leave type, as a column of the team table."""

    leave_type_id: uuid.UUID
    name: str
    code: str
    is_paid: bool
    is_statutory: bool
    is_comp_off: bool


class TeamBalanceCell(BaseModel):
    leave_type_id: uuid.UUID
    #: Opening + accrued + carried forward + adjustments: everything credited.
    entitled: Decimal
    used: Decimal
    encashed: Decimal
    pending: Decimal
    available: Decimal


class TeamBalanceRow(BaseModel):
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    photo_url: str | None
    designation: str | None
    department_name: str | None
    #: Only the types a policy or a ledger row gives this person.
    balances: list[TeamBalanceCell]


class TeamBalancesRead(Page[TeamBalanceRow]):
    year: int
    #: Every active leave type, in name order, whether or not this page uses it,
    #: so the columns do not move between pages.
    leave_types: list[TeamBalanceColumn]


class WhoIsOutRead(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    photo_url: str | None
    start_date: date
    end_date: date
    is_half_day: bool
    half_day_session: HalfDaySession | None = None
    status: LeaveRequestStatus
    #: Only for yourself, your reporting line and HR. Everybody else learns
    #: that a colleague is out, not why.
    leave_type_name: str | None


class AccrualRunRead(BaseModel):
    year: int
    as_of: date
    balances_updated: int


class YearEndRead(BaseModel):
    year: int
    balances_closed: int
    days_carried: Decimal
    days_lapsed: Decimal
    already_closed: int


# ── Comp-off ────────────────────────────────────────────────────────────────


class CompOffCreate(BaseModel):
    worked_date: date
    days: Literal["0.5", "1"] = "1"
    reason: str = Field(min_length=3, max_length=2000)
    #: HR raising a claim on somebody's behalf. Omitted means "for me".
    employee_id: uuid.UUID | None = None


class CompOffDecision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class CompOffRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_photo_url: str | None
    worked_date: date
    #: Why the date qualifies: "Weekly off" or the holiday's name.
    worked_day_label: str | None = None
    days: Decimal
    reason: str
    status: CompOffStatus
    approver_name: str
    decided_by_name: str | None
    decided_at: datetime | None
    decision_note: str | None
    expires_on: date | None
    created_at: datetime
    can_decide: bool = False
    can_cancel: bool = False
