"""Attendance, shift, roster and work-location contracts (slice 02).

Money-adjacent figures (payable and LOP days) are `Decimal` and serialise as
strings, so the portal never sums halves in floating point.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.attendance import (
    AttendanceStatus,
    PunchDirection,
    PunchSource,
    RegularisationKind,
    RegularisationStatus,
)

# ── Punches ─────────────────────────────────────────────────────────────────


class PunchCreate(BaseModel):
    """A punch.

    Outside HR, `punched_at`, `source` and `device_id` are refused: a person
    recording their own attendance does so now, from the web or their phone.
    Accepting a back-dated "biometric" punch from an employee would make every
    punch in the table unfalsifiable in name only.
    """

    employee_id: uuid.UUID | None = Field(
        default=None,
        description="Omit to punch for yourself. Only HR may punch for someone else.",
    )
    punched_at: datetime | None = Field(
        default=None, description="HR only. Defaults to now."
    )
    direction: PunchDirection
    source: PunchSource | None = Field(
        default=None, description="Defaults to web, or mobile_geo when coordinates are sent."
    )
    device_id: str | None = Field(default=None, max_length=128)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _both_coordinates_or_neither(self) -> PunchCreate:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Send both latitude and longitude, or neither.")
        return self


class PunchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    punched_at: datetime
    direction: PunchDirection
    source: PunchSource
    device_id: str | None
    outside_geofence: bool
    nearest_location_metres: int | None


class PunchImportRow(BaseModel):
    """One normalised row from a device export."""

    employee_code: str = Field(min_length=1, max_length=32)
    punched_at: datetime
    direction: PunchDirection
    device_id: str | None = Field(default=None, max_length=128)


class PunchImportRequest(BaseModel):
    rows: list[PunchImportRow] = Field(min_length=1, max_length=5000)


class PunchImportRowError(BaseModel):
    row: int = Field(description="1-based data row number (the header is row 0).")
    employee_code: str | None
    error: str


class PunchImportResult(BaseModel):
    imported: int
    duplicates: int = 0
    skipped_unknown_employee: list[str]
    errors: list[PunchImportRowError] = Field(default_factory=list)


# ── Computed days ───────────────────────────────────────────────────────────


class AttendanceDayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    attendance_date: date
    shift_id: uuid.UUID | None = None
    status: AttendanceStatus
    payable_fraction: Decimal
    worked_minutes: int
    first_in: datetime | None
    last_out: datetime | None
    late_by_minutes: int
    early_by_minutes: int
    overtime_minutes: int = 0
    is_regularised: bool
    computation_note: str | None


class RecomputeRequest(BaseModel):
    employee_ids: list[uuid.UUID] | None = None
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> RecomputeRequest:
        if self.end < self.start:
            raise ValueError("End date cannot be before start date.")
        if (self.end - self.start).days > 92:
            raise ValueError("Recompute at most 93 days at a time.")
        return self


class RecomputeResult(BaseModel):
    days_computed: int


class EmployeeAttendanceSummary(BaseModel):
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    payable_days: Decimal
    lop_days: Decimal
    present_days: int
    half_days: int
    absent_days: int
    on_leave_days: int
    late_count: int
    needs_regularisation: int
    days_computed: int = 0


class AttendanceSummaryResponse(BaseModel):
    """`is_locked` is on the response because it is the gate payroll reads."""

    period_start: date
    period_end: date
    is_locked: bool
    employees: list[EmployeeAttendanceSummary]


class ShiftBrief(BaseModel):
    """A shift as the person working it needs to see it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    start_time: time
    end_time: time
    break_minutes: int
    grace_in_minutes: int
    is_night_shift: bool
    weekly_off_days: list[int]


class CalendarHoliday(BaseModel):
    holiday_date: date
    name: str


class CalendarLeave(BaseModel):
    leave_date: date
    leave_type: str
    is_half_day: bool
    #: approved | pending
    status: str


class MyAttendancePeriod(BaseModel):
    """One person's days over a period, with the totals computed server-side.

    Also carries what a calendar needs for days that have no computed row yet —
    the holidays that apply to this person, their approved and pending leave,
    and the shift they are on — so the portal never guesses any of it.
    """

    period_start: date
    period_end: date
    is_locked: bool
    summary: EmployeeAttendanceSummary
    days: list[AttendanceDayRead]
    #: The workspace's local date, so "today" and "future" match the server.
    today: date
    date_of_joining: date | None = None
    shift_today: ShiftBrief | None = None
    #: Today's shift is the workspace default rather than an assignment.
    shift_today_is_default: bool = False
    #: No shift, roster or default applies — days cannot be judged.
    has_no_shift: bool = False
    shifts: list[ShiftBrief] = Field(default_factory=list)
    holidays: list[CalendarHoliday] = Field(default_factory=list)
    leaves: list[CalendarLeave] = Field(default_factory=list)


class TeamMemberToday(BaseModel):
    """One person's day so far, for a manager or HR."""

    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    designation: str | None = None
    #: in | out | not_in | on_leave | holiday | week_off | on_duty | work_from_home
    state: str
    first_in: datetime | None = None
    last_punch_at: datetime | None = None
    last_direction: PunchDirection | None = None
    shift_name: str | None = None
    shift_start: time | None = None
    late_by_minutes: int = 0
    detail: str | None = None


class TeamToday(BaseModel):
    today: date
    members: list[TeamMemberToday]
    counts: dict[str, int]


# ── Regularisation ──────────────────────────────────────────────────────────

#: What a regularisation may ask for. Leave is requested as leave (it has a
#: balance); holiday and week-off come from the calendar and the shift.
REGULARISABLE_STATUSES = frozenset(
    {
        AttendanceStatus.PRESENT,
        AttendanceStatus.HALF_DAY,
        AttendanceStatus.ABSENT,
        AttendanceStatus.LOP,
    }
)


class RegularisationCreate(BaseModel):
    employee_id: uuid.UUID | None = None
    attendance_date: date
    kind: RegularisationKind = RegularisationKind.STATUS_CHANGE
    requested_status: AttendanceStatus = AttendanceStatus.PRESENT
    reason: str = Field(min_length=3, max_length=1000)
    requested_first_in: datetime | None = None
    requested_last_out: datetime | None = None

    @field_validator("requested_status")
    @classmethod
    def _regularisable(cls, value: AttendanceStatus) -> AttendanceStatus:
        if value not in REGULARISABLE_STATUSES:
            raise ValueError(
                "A regularisation can set present, half day, absent or loss of pay. "
                "Time off is requested as leave, which has a balance."
            )
        return value

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give a reason.")
        return value.strip()


class RegularisationRangeCreate(BaseModel):
    """On-duty or work-from-home over several days — one request per working day."""

    employee_id: uuid.UUID | None = None
    kind: RegularisationKind
    start_date: date
    end_date: date
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give a reason.")
        return value.strip()


class RegularisationDecision(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason_on_reject(self) -> RegularisationDecision:
        if not self.approve and not (self.note or "").strip():
            raise ValueError("Say why the regularisation is rejected.")
        return self


class RegularisationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    attendance_date: date
    kind: RegularisationKind = RegularisationKind.STATUS_CHANGE
    requested_status: AttendanceStatus
    requested_first_in: datetime | None = None
    requested_last_out: datetime | None = None
    reason: str
    status: RegularisationStatus
    decided_at: datetime | None
    decision_note: str | None = None
    created_at: datetime | None = None


class RegularisationQueueItem(RegularisationRead):
    """A pending request as the approver sees it."""

    employee_code: str
    employee_name: str
    current_status: AttendanceStatus | None
    current_note: str | None
    period_locked: bool


# ── Exceptions ──────────────────────────────────────────────────────────────


class AttendanceException(BaseModel):
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    attendance_date: date
    kind: str = Field(
        description="needs_regularisation | absent | late | early_out | outside_geofence"
    )
    status: AttendanceStatus | None
    late_by_minutes: int = 0
    early_by_minutes: int = 0
    detail: str
    has_pending_regularisation: bool = False


class AttendanceExceptionReport(BaseModel):
    period_start: date
    period_end: date
    employees_in_scope: int
    exceptions: list[AttendanceException]


# ── Locks ───────────────────────────────────────────────────────────────────


class LockRequest(BaseModel):
    period_start: date
    period_end: date

    @model_validator(mode="after")
    def _ordered(self) -> LockRequest:
        if self.period_end < self.period_start:
            raise ValueError("The period cannot end before it starts.")
        return self


class UnlockRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class LockRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period_start: date
    period_end: date
    locked_at: datetime
    locked_by_user_id: uuid.UUID | None = None
    unlocked_at: datetime | None
    unlocked_by_user_id: uuid.UUID | None = None
    unlock_reason: str | None


# ── Shifts ──────────────────────────────────────────────────────────────────


def _check_weekdays(days: list[int]) -> list[int]:
    if any(d < 1 or d > 7 for d in days):
        raise ValueError("Weekly off days are ISO weekdays, 1 (Monday) to 7 (Sunday).")
    return sorted(set(days))


class ShiftBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    start_time: time
    end_time: time
    break_minutes: int = Field(default=0, ge=0, le=600)
    grace_in_minutes: int = Field(default=0, ge=0, le=240)
    grace_out_minutes: int = Field(default=0, ge=0, le=240)
    half_day_threshold_minutes: int = Field(ge=0, le=1440)
    full_day_threshold_minutes: int = Field(ge=1, le=1440)
    is_night_shift: bool = False
    weekly_off_days: list[int] = Field(default_factory=list, max_length=7)

    @field_validator("weekly_off_days")
    @classmethod
    def _weekdays(cls, value: list[int]) -> list[int]:
        return _check_weekdays(value)

    @model_validator(mode="after")
    def _consistent(self) -> ShiftBase:
        if self.half_day_threshold_minutes >= self.full_day_threshold_minutes:
            raise ValueError("The half-day threshold must be below the full-day threshold.")
        if self.end_time <= self.start_time and not self.is_night_shift:
            raise ValueError(
                "This shift ends at or before it starts. Mark it as a night shift if it "
                "runs past midnight."
            )
        return self


class ShiftCreate(ShiftBase):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")


class ShiftUpdate(ShiftBase):
    active: bool = True


class ShiftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    start_time: time
    end_time: time
    break_minutes: int
    grace_in_minutes: int
    grace_out_minutes: int
    half_day_threshold_minutes: int
    full_day_threshold_minutes: int
    is_night_shift: bool
    weekly_off_days: list[int]
    active: bool
    is_default: bool = False


# ── Rosters ─────────────────────────────────────────────────────────────────


class RosterDayIn(BaseModel):
    day_index: int = Field(ge=0, le=365)
    #: Null means a week off on that day of the rotation.
    shift_id: uuid.UUID | None = None


class RosterBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    cycle_days: int = Field(ge=1, le=366)
    days: list[RosterDayIn] = Field(min_length=1, max_length=366)

    @model_validator(mode="after")
    def _covers_cycle(self) -> RosterBase:
        indexes = [d.day_index for d in self.days]
        if len(indexes) != len(set(indexes)):
            raise ValueError("Each day of the rotation can appear only once.")
        if sorted(indexes) != list(range(self.cycle_days)):
            raise ValueError(
                f"Give exactly one entry for each of the {self.cycle_days} days of the "
                f"rotation (day 0 to day {self.cycle_days - 1}); a null shift is a week off."
            )
        return self


class RosterCreate(RosterBase):
    pass


class RosterUpdate(RosterBase):
    active: bool = True


class RosterDayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day_index: int
    shift_id: uuid.UUID | None


class RosterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    cycle_days: int
    active: bool
    days: list[RosterDayRead]


# ── Assignments ─────────────────────────────────────────────────────────────


class ShiftAssignmentCreate(BaseModel):
    employee_id: uuid.UUID
    shift_id: uuid.UUID
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> ShiftAssignmentCreate:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("An assignment cannot end before it starts.")
        return self


class RosterAssignmentCreate(BaseModel):
    employee_id: uuid.UUID
    roster_id: uuid.UUID
    cycle_start_date: date
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> RosterAssignmentCreate:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("An assignment cannot end before it starts.")
        return self


class AssignmentEnd(BaseModel):
    effective_to: date


class AssignmentRead(BaseModel):
    id: uuid.UUID
    kind: str = Field(description="shift | roster")
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    shift_id: uuid.UUID | None = None
    roster_id: uuid.UUID | None = None
    target_name: str
    cycle_start_date: date | None = None
    effective_from: date
    effective_to: date | None


# ── Work locations ──────────────────────────────────────────────────────────


class WorkLocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    location_id: uuid.UUID | None = None
    latitude: Decimal = Field(ge=-90, le=90)
    longitude: Decimal = Field(ge=-180, le=180)
    radius_metres: int = Field(default=200, ge=10, le=50_000)


class WorkLocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    location_id: uuid.UUID | None
    latitude: Decimal
    longitude: Decimal
    radius_metres: int
    active: bool
