"""Attendance exports for HR and managers: punches with places, days, and totals.

Four files, for any day, week, month or range of dates up to 93 days:

- **Punch in and out** — one row per person per day: the first punch in and
  the last punch out, each with its time, the office it was at (or how far
  from it), its street address and a Google Maps link; then the day's status,
  shift and hours.
- **Every punch** — one row per punch, with the same place details, the
  source (app, web, device…) and the coordinates.
- **Day-wise attendance** — one row per person per day: status, shift, first
  in, last out, hours, late, early, overtime, payable fraction, corrections.
- **Summary** — one row per person for the whole period: present, half
  days, absent, leave, holidays, week offs, late, hours, payable days, LOP.

Who is in the file: HR, everyone (themselves included); a manager, their
reporting line and themselves. Narrowed by company, project, department and
location as on the screen, or to the people ticked there. People who had not
joined or had left before the period are left out.

Statuses come from the computed days (the nightly job computes every past
day). A day not computed yet — today, say — reads "In progress".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any, Literal

from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import (
    AttendanceDay,
    AttendancePunch,
    AttendanceStatus,
    PunchDirection,
    PunchSource,
    Shift,
)
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department, LegalEntity, Location
from app.models.timesheets import Project
from app.models.user import User
from app.services import audit, sheets
from app.services.attendance import addresses
from app.services.attendance import service as attendance_service
from app.services.attendance.review import OrgFilters, apply_org_filters, check_range

__all__ = ["KINDS", "AttendanceFile", "ExportKind", "export_attendance"]

ExportKind = Literal["in_out", "punches", "daily", "summary"]
FileType = Literal["xlsx", "csv"]

KINDS: dict[str, str] = {
    "in_out": "Punch in and out",
    "punches": "Every punch",
    "daily": "Day-wise attendance",
    "summary": "Summary",
}

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Address lookups one export may make, and how long it may wait for them.
#: Places looked up before are free; a first export of a new month can leave
#: some addresses blank, and the next export fills them.
_LOOKUPS = 1500
_LOOKUP_SECONDS = 25.0

_STATUS = {
    AttendanceStatus.PRESENT: "Present",
    AttendanceStatus.ABSENT: "Absent",
    AttendanceStatus.HALF_DAY: "Half day",
    AttendanceStatus.WEEK_OFF: "Week off",
    AttendanceStatus.HOLIDAY: "Holiday",
    AttendanceStatus.ON_LEAVE: "On leave",
    AttendanceStatus.LOP: "Absent (loss of pay)",
    AttendanceStatus.NEEDS_REGULARISATION: "Needs correction",
}
_SOURCE = {
    PunchSource.BIOMETRIC: "Biometric device",
    PunchSource.MOBILE_GEO: "Mobile app",
    PunchSource.WEB: "Web",
    PunchSource.MANUAL: "Entered by HR",
    PunchSource.IMPORT: "Imported",
    PunchSource.KIOSK: "Kiosk",
}
_WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_PERSON = [
    sheets.Column("Employee code", "text"),
    sheets.Column("Name", "text", 24),
    sheets.Column("Designation", "text", 22),
    sheets.Column("Company", "text", 22),
    sheets.Column("Project", "text", 18),
    sheets.Column("Department", "text", 16),
    sheets.Column("Location", "text", 16),
]


@dataclass(frozen=True)
class AttendanceFile:
    content: bytes
    filename: str
    media_type: str
    people: int
    rows: int


@dataclass
class _Context:
    today: date
    zones: dict[uuid.UUID, tzinfo]
    companies: dict[uuid.UUID, str]
    projects: dict[uuid.UUID, str]
    departments: dict[uuid.UUID, str]
    locations: dict[uuid.UUID, str]
    places: dict[addresses.Place, str | None]
    shifts: dict[uuid.UUID, str]


def _person(ctx: _Context, e: Employee) -> list[Any]:
    def name(table: dict[uuid.UUID, str], key: uuid.UUID | None) -> str | None:
        return table.get(key) if key else None

    return [
        e.employee_code,
        e.full_name,
        e.designation,
        name(ctx.companies, e.legal_entity_id),
        name(ctx.projects, e.project_id),
        name(ctx.departments, e.department_id),
        name(ctx.locations, e.location_id),
    ]


def _local(ctx: _Context, punch: AttendancePunch) -> datetime:
    return punch.punched_at.astimezone(ctx.zones.get(punch.employee_id, UTC))


def _place_text(punch: AttendancePunch) -> str | None:
    """Where, in words: "At Pune office", "2.3 km from Pune office", "Location shared"."""
    if punch.latitude is None:
        return None
    if not punch.location_label:
        return "Location shared"
    if not punch.outside_geofence:
        return f"At {punch.location_label}"
    beyond = punch.nearest_location_metres
    if beyond is None:
        return f"Away from {punch.location_label}"
    distance = f"{beyond / 1000:.1f} km" if beyond >= 1000 else f"{beyond} m"
    return f"{distance} from {punch.location_label}"


def _address(ctx: _Context, punch: AttendancePunch) -> str | None:
    place = addresses.place_of(punch.latitude, punch.longitude)
    return ctx.places.get(place) if place else None


def _map(punch: AttendancePunch) -> sheets.Link | None:
    if punch.latitude is None or punch.longitude is None:
        return None
    return sheets.Link(addresses.maps_link(punch.latitude, punch.longitude))


def _status(ctx: _Context, day: AttendanceDay | None, on: date) -> str:
    if day is not None:
        return _STATUS.get(day.status, day.status.value)
    return "In progress" if on >= ctx.today else "Not computed"


def _minutes(value: int | None) -> int | None:
    return value if value else None


# ── The four layouts ────────────────────────────────────────────────────────


def _in_out(
    ctx: _Context,
    people: list[Employee],
    dates: list[date],
    punches: dict[tuple[uuid.UUID, date], list[AttendancePunch]],
    days: dict[tuple[uuid.UUID, date], AttendanceDay],
) -> tuple[list[sheets.Column], list[list[Any]]]:
    columns = [
        *_PERSON,
        sheets.Column("Date", "date"),
        sheets.Column("Day", "text", 6),
        sheets.Column("Status", "text", 16),
        sheets.Column("Shift", "text", 18),
        sheets.Column("In time", "time", 9),
        sheets.Column("In place", "text", 24),
        sheets.Column("In address", "text", 48),
        sheets.Column("In map (Google Maps)", "link", 14),
        sheets.Column("Out time", "time", 9),
        sheets.Column("Out place", "text", 24),
        sheets.Column("Out address", "text", 48),
        sheets.Column("Out map (Google Maps)", "link", 14),
        sheets.Column("Worked (h:mm)", "minutes", 10),
        sheets.Column("Late by (h:mm)", "minutes", 10),
        sheets.Column("Left early by (h:mm)", "minutes", 10),
        sheets.Column("Punches", "number", 8),
    ]
    rows: list[list[Any]] = []
    for employee in people:
        for on in _tenure(employee, dates):
            own = punches.get((employee.id, on), [])
            first_in = next((p for p in own if p.direction is PunchDirection.IN), None)
            last_out = next((p for p in reversed(own) if p.direction is PunchDirection.OUT), None)
            day = days.get((employee.id, on))
            rows.append(
                [
                    *_person(ctx, employee),
                    on,
                    _WEEKDAY[on.weekday()],
                    _status(ctx, day, on),
                    ctx.shifts.get(day.shift_id) if day and day.shift_id else None,
                    _local(ctx, first_in).time().replace(second=0, microsecond=0)
                    if first_in
                    else None,
                    _place_text(first_in) if first_in else None,
                    _address(ctx, first_in) if first_in else None,
                    _map(first_in) if first_in else None,
                    _local(ctx, last_out).time().replace(second=0, microsecond=0)
                    if last_out
                    else None,
                    _place_text(last_out) if last_out else None,
                    _address(ctx, last_out) if last_out else None,
                    _map(last_out) if last_out else None,
                    _minutes(day.worked_minutes) if day else None,
                    _minutes(day.late_by_minutes) if day else None,
                    _minutes(day.early_by_minutes) if day else None,
                    len(own) or None,
                ]
            )
    return columns, rows


def _every_punch(
    ctx: _Context,
    people: list[Employee],
    punches: dict[tuple[uuid.UUID, date], list[AttendancePunch]],
) -> tuple[list[sheets.Column], list[list[Any]]]:
    columns = [
        *_PERSON,
        sheets.Column("Date", "date"),
        sheets.Column("Day", "text", 6),
        sheets.Column("Time", "time", 9),
        sheets.Column("In / Out", "text", 8),
        sheets.Column("Source", "text", 16),
        sheets.Column("Place", "text", 24),
        sheets.Column("Outside work locations", "text", 10),
        sheets.Column("Distance from office (m)", "number", 12),
        sheets.Column("Address", "text", 48),
        sheets.Column("Map (Google Maps)", "link", 14),
        sheets.Column("Latitude", "number", 12),
        sheets.Column("Longitude", "number", 12),
        sheets.Column("Photo taken", "text", 8),
    ]
    by_person: dict[uuid.UUID, list[AttendancePunch]] = {}
    for (employee_id, _), own in sorted(punches.items(), key=lambda item: item[0][1]):
        by_person.setdefault(employee_id, []).extend(own)
    rows: list[list[Any]] = []
    for employee in people:
        for punch in by_person.get(employee.id, []):
            local = _local(ctx, punch)
            located = punch.latitude is not None
            rows.append(
                [
                    *_person(ctx, employee),
                    local.date(),
                    _WEEKDAY[local.weekday()],
                    local.time().replace(second=0, microsecond=0),
                    "In" if punch.direction is PunchDirection.IN else "Out",
                    _SOURCE.get(punch.source, punch.source.value),
                    _place_text(punch),
                    ("Yes" if punch.outside_geofence else "No") if located else None,
                    punch.nearest_location_metres if punch.outside_geofence else None,
                    _address(ctx, punch),
                    _map(punch),
                    float(punch.latitude) if located else None,
                    float(punch.longitude) if punch.longitude is not None else None,
                    "Yes" if punch.photo_key else "No",
                ]
            )
    return columns, rows


def _daily(
    ctx: _Context,
    people: list[Employee],
    dates: list[date],
    punches: dict[tuple[uuid.UUID, date], list[AttendancePunch]],
    days: dict[tuple[uuid.UUID, date], AttendanceDay],
) -> tuple[list[sheets.Column], list[list[Any]]]:
    columns = [
        *_PERSON,
        sheets.Column("Date", "date"),
        sheets.Column("Day", "text", 6),
        sheets.Column("Status", "text", 16),
        sheets.Column("Shift", "text", 18),
        sheets.Column("First in", "time", 9),
        sheets.Column("Last out", "time", 9),
        sheets.Column("Worked (h:mm)", "minutes", 10),
        sheets.Column("Late by (h:mm)", "minutes", 10),
        sheets.Column("Left early by (h:mm)", "minutes", 10),
        sheets.Column("Overtime (h:mm)", "minutes", 10),
        sheets.Column("Payable day", "number", 9),
        sheets.Column("Corrected", "text", 9),
        sheets.Column("Note", "text", 40),
    ]
    rows: list[list[Any]] = []
    for employee in people:
        for on in _tenure(employee, dates):
            day = days.get((employee.id, on))
            own = punches.get((employee.id, on), [])
            first = (
                day.first_in
                if day and day.first_in
                else next((p.punched_at for p in own if p.direction is PunchDirection.IN), None)
            )
            last = (
                day.last_out
                if day and day.last_out
                else next(
                    (p.punched_at for p in reversed(own) if p.direction is PunchDirection.OUT),
                    None,
                )
            )
            zone = ctx.zones.get(employee.id, UTC)
            rows.append(
                [
                    *_person(ctx, employee),
                    on,
                    _WEEKDAY[on.weekday()],
                    _status(ctx, day, on),
                    ctx.shifts.get(day.shift_id) if day and day.shift_id else None,
                    first.astimezone(zone).time().replace(second=0, microsecond=0)
                    if first
                    else None,
                    last.astimezone(zone).time().replace(second=0, microsecond=0) if last else None,
                    _minutes(day.worked_minutes) if day else None,
                    _minutes(day.late_by_minutes) if day else None,
                    _minutes(day.early_by_minutes) if day else None,
                    _minutes(day.overtime_minutes) if day else None,
                    Decimal(str(day.payable_fraction)).normalize() if day else None,
                    ("Yes" if day.is_regularised else "No") if day else None,
                    day.computation_note if day else None,
                ]
            )
    return columns, rows


def _summary(
    ctx: _Context,
    people: list[Employee],
    dates: list[date],
    days: dict[tuple[uuid.UUID, date], AttendanceDay],
) -> tuple[list[sheets.Column], list[list[Any]]]:
    columns = [
        *_PERSON,
        sheets.Column("From", "date"),
        sheets.Column("To", "date"),
        sheets.Column("Days in period", "number", 9),
        sheets.Column("Present", "number", 9),
        sheets.Column("Half days", "number", 9),
        sheets.Column("Absent", "number", 9),
        sheets.Column("On leave", "number", 9),
        sheets.Column("Holidays", "number", 9),
        sheets.Column("Week offs", "number", 9),
        sheets.Column("Needs correction", "number", 11),
        sheets.Column("Not computed", "number", 10),
        sheets.Column("Late days", "number", 9),
        sheets.Column("Late (h:mm)", "minutes", 10),
        sheets.Column("Worked (h:mm)", "minutes", 10),
        sheets.Column("Overtime (h:mm)", "minutes", 10),
        sheets.Column("Payable days", "number", 10),
        sheets.Column("Loss-of-pay days", "number", 10),
    ]
    rows: list[list[Any]] = []
    for employee in people:
        span = _tenure(employee, dates)
        own = [days[(employee.id, on)] for on in span if (employee.id, on) in days]

        def count(*statuses: AttendanceStatus, own: list[AttendanceDay] = own) -> int:
            return sum(1 for d in own if d.status in statuses)

        payable = sum((Decimal(str(d.payable_fraction)) for d in own), start=Decimal(0))
        # Same rule as payroll's LOP feed: every unpaid fraction but a week off.
        lop = sum(
            (
                Decimal(1) - Decimal(str(d.payable_fraction))
                for d in own
                if d.status is not AttendanceStatus.WEEK_OFF
            ),
            start=Decimal(0),
        )
        rows.append(
            [
                *_person(ctx, employee),
                span[0] if span else None,
                span[-1] if span else None,
                len(span),
                count(AttendanceStatus.PRESENT),
                count(AttendanceStatus.HALF_DAY),
                count(AttendanceStatus.ABSENT, AttendanceStatus.LOP),
                count(AttendanceStatus.ON_LEAVE),
                count(AttendanceStatus.HOLIDAY),
                count(AttendanceStatus.WEEK_OFF),
                count(AttendanceStatus.NEEDS_REGULARISATION),
                len(span) - len(own),
                sum(1 for d in own if d.late_by_minutes > 0),
                sum(d.late_by_minutes for d in own),
                sum(d.worked_minutes for d in own),
                sum(d.overtime_minutes for d in own),
                payable.normalize(),
                lop.normalize(),
            ]
        )
    return columns, rows


def _tenure(employee: Employee, dates: list[date]) -> list[date]:
    """The dates of the period this person worked here: from joining, up to leaving."""
    joined = employee.date_of_joining
    left = employee.date_of_exit
    return [d for d in dates if (joined is None or d >= joined) and (left is None or d <= left)]


# ── Reading ─────────────────────────────────────────────────────────────────


async def _names(session: AsyncSession, user: User, model: Any) -> dict[uuid.UUID, str]:
    rows = await session.execute(tenant_scoped(select(model.id, model.name), model, user))
    return dict(rows.all())  # type: ignore[arg-type]


async def _people(
    session: AsyncSession,
    user: User,
    *,
    start: date,
    end: date,
    filters: OrgFilters,
    employee_ids: list[uuid.UUID] | None,
) -> list[Employee]:
    stmt = select(Employee).where(
        Employee.deleted_at.is_(None),
        Employee.status != EmploymentStatus.PRE_JOINING,
        or_(Employee.date_of_joining.is_(None), Employee.date_of_joining <= end),
        # Somebody who left before the period has nothing in it; somebody
        # marked as left with no leaving date cannot be placed, so is left out.
        or_(
            Employee.status != EmploymentStatus.EXITED,
            Employee.date_of_exit >= start,
        ),
    )
    stmt = apply_org_filters(stmt, filters)
    visible = await permissions.visible_employee_ids(session, user)
    if visible is not None:
        stmt = stmt.where(Employee.id.in_(visible or [uuid.UUID(int=0)]))
    if employee_ids is not None:
        stmt = stmt.where(Employee.id.in_(employee_ids or [uuid.UUID(int=0)]))
    stmt = stmt.order_by(Employee.first_name, Employee.last_name, Employee.employee_code)
    return list((await session.scalars(tenant_scoped(stmt, Employee, user))).all())


async def export_attendance(
    session: AsyncSession,
    user: User,
    *,
    start: date,
    end: date,
    kind: ExportKind,
    filters: OrgFilters,
    employee_ids: list[uuid.UUID] | None = None,
    file_type: FileType = "xlsx",
    request: Request | None = None,
) -> AttendanceFile:
    """Build one attendance file. HR sees everyone; a manager, their reporting line."""
    if not permissions.is_hr(user) and not user.has_reports:
        raise permissions.forbidden("Only HR and managers can export attendance.")
    check_range(start, end)
    dates = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    people = await _people(
        session, user, start=start, end=end, filters=filters, employee_ids=employee_ids
    )
    ids = [e.id for e in people]
    today = await attendance_service.local_today(session, user)
    zones = await attendance_service.employee_timezones(session, user, ids) if ids else {}

    punches: dict[tuple[uuid.UUID, date], list[AttendancePunch]] = {}
    if ids and kind != "summary":
        rows = (
            await session.scalars(
                tenant_scoped(
                    select(AttendancePunch)
                    .where(
                        AttendancePunch.employee_id.in_(ids),
                        AttendancePunch.punched_at
                        >= datetime.combine(start - timedelta(days=1), datetime.min.time(), UTC),
                        AttendancePunch.punched_at
                        < datetime.combine(end + timedelta(days=2), datetime.min.time(), UTC),
                    )
                    .order_by(AttendancePunch.punched_at),
                    AttendancePunch,
                    user,
                )
            )
        ).all()
        for punch in rows:
            on = punch.punched_at.astimezone(zones.get(punch.employee_id, UTC)).date()
            if start <= on <= end:
                punches.setdefault((punch.employee_id, on), []).append(punch)

    days: dict[tuple[uuid.UUID, date], AttendanceDay] = {}
    if ids and kind != "punches":
        for day in (
            await session.scalars(
                tenant_scoped(
                    select(AttendanceDay).where(
                        AttendanceDay.employee_id.in_(ids),
                        AttendanceDay.attendance_date >= start,
                        AttendanceDay.attendance_date <= end,
                    ),
                    AttendanceDay,
                    user,
                )
            )
        ).all():
            days[(day.employee_id, day.attendance_date)] = day

    places: dict[addresses.Place, str | None] = {}
    if kind in ("in_out", "punches") and punches:
        wanted = [
            (p.latitude, p.longitude)
            for ps in punches.values()
            for p in (
                ps
                if kind == "punches"
                else [
                    next((x for x in ps if x.direction is PunchDirection.IN), None),
                    next((x for x in reversed(ps) if x.direction is PunchDirection.OUT), None),
                ]
            )
            if p is not None and p.latitude is not None
        ]
        places = await addresses.addresses_for(
            session, user, wanted, budget_seconds=_LOOKUP_SECONDS, max_lookups=_LOOKUPS
        )

    ctx = _Context(
        today=today,
        zones=zones,
        companies=await _names(session, user, LegalEntity),
        projects=await _names(session, user, Project),
        departments=await _names(session, user, Department),
        locations=await _names(session, user, Location),
        places=places,
        shifts=await _names(session, user, Shift),
    )
    if kind == "in_out":
        columns, rows = _in_out(ctx, people, dates, punches, days)
    elif kind == "punches":
        columns, rows = _every_punch(ctx, people, punches)
    elif kind == "daily":
        columns, rows = _daily(ctx, people, dates, punches, days)
    else:
        columns, rows = _summary(ctx, people, dates, days)

    label = KINDS[kind]
    content = (
        sheets.xlsx_bytes(columns, rows, sheet_title=label, row_count=len(rows))
        if file_type == "xlsx"
        else sheets.csv_bytes(columns, rows)
    )
    span = start.isoformat() if start == end else f"{start.isoformat()}-to-{end.isoformat()}"
    slug = {"in_out": "punch-in-out", "punches": "punches", "daily": "daily", "summary": "summary"}
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.EXPORT,
        entity_type="attendance",
        summary=(
            f"Exported attendance ({label}, {start:%d %b %Y} to {end:%d %b %Y}, "
            f"{len(people)} people) as {'Excel' if file_type == 'xlsx' else 'CSV'}"
        ),
        changes={
            "kind": kind,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "people": len(people),
            "rows": len(rows),
            "format": file_type,
            "ticked_people": len(employee_ids) if employee_ids is not None else None,
            "addresses_included": kind in ("in_out", "punches"),
            **filters.audit(),
        },
        request=request,
    )
    return AttendanceFile(
        content=content,
        filename=f"attendance-{slug[kind]}-{span}.{file_type}",
        media_type=_XLSX if file_type == "xlsx" else "text/csv; charset=utf-8",
        people=len(people),
        rows=len(rows),
    )
