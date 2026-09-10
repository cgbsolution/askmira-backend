"""Attendance, shifts and rosters (slice 02).

This module has to produce one defensible number: payable days per employee per
period. Everything else here is in service of that, because it is what payroll
prorates against and what an employee will dispute if it is wrong.

Invariants:

1. `AttendancePunch` is insert-only. There is no update or delete path in the
   service layer and none should be added. A punch is an observation — it
   happened. Corrections are `AttendanceRegularisation` records, which leave
   both the original observation and the correction visible.
2. `AttendanceDay` is derived, never authoritative. It can be recomputed from
   punches, leave and the holiday calendar at any time, and recomputation is
   idempotent.
3. A punch outside a geofence is stored and flagged, never rejected. An
   employee who cannot punch stops trying, and then the data is gone.
4. Payroll requires an `AttendancePeriodLock`. Assuming zero LOP silently
   overpays, which is worse than blocking the run.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class PunchDirection(str, enum.Enum):
    IN = "in"
    OUT = "out"


class PunchSource(str, enum.Enum):
    BIOMETRIC = "biometric"
    MOBILE_GEO = "mobile_geo"
    WEB = "web"
    MANUAL = "manual"
    IMPORT = "import"


class AttendanceStatus(str, enum.Enum):
    PRESENT = "present"
    ABSENT = "absent"
    HALF_DAY = "half_day"
    WEEK_OFF = "week_off"
    HOLIDAY = "holiday"
    ON_LEAVE = "on_leave"
    # Absent *and* unpaid — this is the status payroll counts as loss of pay.
    LOP = "lop"
    # An unpaired punch, or a day a manager must look at. Never silently a
    # zero-hours absence.
    NEEDS_REGULARISATION = "needs_regularisation"


class RegularisationStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


# ── Shifts and rosters ──────────────────────────────────────────────────────


class Shift(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A working pattern.

    `is_night_shift` is not cosmetic: when a shift spans midnight, punches
    after midnight belong to the previous day's attendance record. That single
    rule is the most common source of attendance bugs in this category of
    product.
    """

    __tablename__ = "shifts"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_shifts_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    break_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Grace before "late" or "early leaving" is recorded at all.
    grace_in_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    grace_out_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Worked-minute thresholds. Below half-day is absent; at or above full-day
    # is present; between the two is a half day.
    half_day_threshold_minutes: Mapped[int] = mapped_column(Integer, default=240, nullable=False)
    full_day_threshold_minutes: Mapped[int] = mapped_column(Integer, default=480, nullable=False)

    is_night_shift: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ISO weekday numbers, Monday = 1 through Sunday = 7.
    weekly_off_days: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), default=list, nullable=False
    )

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<Shift {self.code} {self.start_time}-{self.end_time}>"


class ShiftAssignment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Which shift an employee works, effective-dated.

    Historical attendance always evaluates against the shift in force on that
    day, never the current one.
    """

    __tablename__ = "shift_assignments"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    shift_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("shifts.id", ondelete="RESTRICT"), nullable=False
    )

    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    shift: Mapped[Shift] = relationship(lazy="selectin")


class Roster(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A rotating shift pattern, for manufacturing and multi-location clients."""

    __tablename__ = "rosters"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_rosters_tenant_name"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Length of one rotation, e.g. 7 for a weekly pattern, 21 for three weeks.
    cycle_days: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    days: Mapped[list[RosterDay]] = relationship(
        back_populates="roster", cascade="all, delete-orphan", lazy="selectin"
    )


class RosterDay(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One day of a rotation. A null shift means a week off."""

    __tablename__ = "roster_days"
    __table_args__ = (
        UniqueConstraint("tenant_id", "roster_id", "day_index", name="uq_roster_day_index"),
    )

    roster_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("rosters.id", ondelete="CASCADE"), nullable=False
    )
    # Zero-based position in the cycle.
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("shifts.id", ondelete="RESTRICT"), nullable=True
    )

    roster: Mapped[Roster] = relationship(back_populates="days")
    shift: Mapped[Shift | None] = relationship(lazy="selectin")


class RosterAssignment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An employee on a rotation, anchored to the date the cycle starts."""

    __tablename__ = "roster_assignments"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    roster_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("rosters.id", ondelete="RESTRICT"), nullable=False
    )
    cycle_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    roster: Mapped[Roster] = relationship(lazy="selectin")


# ── Geofencing ──────────────────────────────────────────────────────────────


class WorkLocation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A geofenced place of work.

    A mobile punch outside every assigned location's radius is stored and
    flagged, never rejected — see the module docstring.
    """

    __tablename__ = "work_locations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )

    latitude: Mapped[float] = mapped_column(Numeric(10, 7), nullable=False)
    longitude: Mapped[float] = mapped_column(Numeric(10, 7), nullable=False)
    radius_metres: Mapped[int] = mapped_column(Integer, default=200, nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# ── Punches ─────────────────────────────────────────────────────────────────


class AttendancePunch(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A raw punch. Insert-only.

    There is deliberately no `updated_by`, no soft-delete flag and no service
    method that mutates one. A punch is an observation of something that
    happened; changing it would make the derived attendance unexplainable.
    """

    __tablename__ = "attendance_punches"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    punched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    direction: Mapped[PunchDirection] = mapped_column(
        Enum(PunchDirection, name="punch_direction"), nullable=False
    )
    source: Mapped[PunchSource] = mapped_column(
        Enum(PunchSource, name="punch_source"), nullable=False
    )

    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    latitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)

    # True when a mobile punch fell outside every assigned work location.
    # Stored, flagged, and left for HR to judge.
    outside_geofence: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    nearest_location_metres: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Whatever the device or client sent, kept verbatim for dispute resolution.
    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    def __repr__(self) -> str:
        return f"<AttendancePunch {self.direction.value} @ {self.punched_at}>"


# ── Derived daily result ────────────────────────────────────────────────────


class AttendanceDay(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The computed result for one employee on one day.

    Derived from punches, leave and the holiday calendar. Recomputable at any
    time, and recomputation is idempotent — running it twice produces identical
    values.
    """

    __tablename__ = "attendance_days"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_id", "attendance_date", name="uq_attendance_day_once"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    attendance_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("shifts.id", ondelete="SET NULL"), nullable=True
    )

    first_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worked_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[AttendanceStatus] = mapped_column(
        Enum(AttendanceStatus, name="attendance_status"), nullable=False, index=True
    )
    # How much of a payable day this is: 1, 0.5 or 0. Payroll sums these.
    payable_fraction: Mapped[float] = mapped_column(Numeric(3, 2), default=0, nullable=False)

    late_by_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    early_by_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    is_regularised: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Why the computation landed where it did — shown to the employee, so it
    # has to be readable rather than a status code.
    computation_note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<AttendanceDay {self.attendance_date} {self.status.value}>"


class AttendanceRegularisation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A correction request against a computed day.

    The original punches stay untouched; an approved regularisation overrides
    the derived status and both remain visible.
    """

    __tablename__ = "attendance_regularisations"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    attendance_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    requested_status: Mapped[AttendanceStatus] = mapped_column(
        Enum(AttendanceStatus, name="attendance_status"), nullable=False
    )
    # Optional corrected times, when the employee is fixing a missed punch
    # rather than the whole day's status.
    requested_first_in: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_last_out: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[RegularisationStatus] = mapped_column(
        Enum(RegularisationStatus, name="regularisation_status"),
        default=RegularisationStatus.PENDING,
        nullable=False,
        index=True,
    )

    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AttendancePeriodLock(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Attendance finalised for a period.

    Payroll requires this. Punches for a locked period are still accepted —
    people do forget — but they cannot change the computed days without an
    explicit unlock, which is audited.
    """

    __tablename__ = "attendance_period_locks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "period_start", "period_end", name="uq_attendance_lock_period"
        ),
    )

    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    locked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # An unlock does not delete the row — it records who reopened the period
    # and why, so the reopening is as visible as the lock.
    unlocked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unlock_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_active(self) -> bool:
        return self.unlocked_at is None
