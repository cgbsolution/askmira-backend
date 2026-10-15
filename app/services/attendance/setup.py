"""Shifts, rosters, assignments and geofenced work locations (slice 02 sections 1-2).

The configuration attendance is computed against.

Assignments are effective-dated. A new assignment closes the open one on the day
before it starts, so a historical day keeps evaluating against the shift that
was in force then. One that would start on or before a later assignment is
refused rather than overlapping it: two shifts on one day is a day nobody can
compute.

Editing a shift changes nothing already computed. The days move only when they
are recomputed, and a locked payroll period cannot be recomputed without an
audited unlock — which is the guard against a shift edit quietly rewriting a
month that has been paid.
"""

from __future__ import annotations

import uuid
from datetime import time, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import (
    Roster,
    RosterAssignment,
    RosterDay,
    Shift,
    ShiftAssignment,
    WorkLocation,
)
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.org import Location
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.attendance import (
    AssignmentRead,
    AttendanceSettings,
    AttendanceSettingsRead,
    RosterAssignmentCreate,
    RosterCreate,
    RosterUpdate,
    ShiftAssignmentCreate,
    ShiftCreate,
    ShiftUpdate,
    WorkLocationCreate,
    WorkLocationPin,
)
from app.services import audit
from app.services.attendance import qualifications

_SHIFT_FIELDS = (
    "name",
    "start_time",
    "end_time",
    "break_minutes",
    "grace_in_minutes",
    "grace_out_minutes",
    "half_day_threshold_minutes",
    "full_day_threshold_minutes",
    "is_night_shift",
    "weekly_off_days",
    "allowance_per_day",
    "allowance_name",
    "min_headcount",
    "location_id",
    "work_area",
)


async def _check_site(session: AsyncSession, user: User, location_id: uuid.UUID | None) -> None:
    if location_id is None:
        return
    from app.models.org import Location

    found = await session.scalar(
        tenant_scoped(select(Location.id).where(Location.id == location_id), Location, user)
    )
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That location is not one of this workspace's.",
        )


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _snapshot(shift: Shift) -> dict[str, Any]:
    out: dict[str, Any] = {"code": shift.code, "active": shift.active}
    for field in _SHIFT_FIELDS:
        value = getattr(shift, field)
        if isinstance(value, time):
            value = value.isoformat()
        elif isinstance(value, Decimal | uuid.UUID):
            value = str(value)
        out[field] = value
    out["required_course_ids"] = list(shift.required_course_ids or [])
    return out


# ── Shifts ──────────────────────────────────────────────────────────────────


async def list_shifts(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[Shift]:
    stmt = select(Shift).order_by(Shift.code)
    if not include_inactive:
        stmt = stmt.where(Shift.active.is_(True))
    return list((await session.scalars(tenant_scoped(stmt, Shift, user))).all())


async def _shift(session: AsyncSession, user: User, shift_id: uuid.UUID) -> Shift:
    row = await session.scalar(
        tenant_scoped(select(Shift).where(Shift.id == shift_id), Shift, user)
    )
    if row is None:
        raise _not_found("Shift")
    return row


async def create_shift(
    session: AsyncSession, user: User, data: ShiftCreate, request: Request | None = None
) -> Shift:
    code = data.code.upper()
    duplicate = await session.scalar(
        tenant_scoped(select(Shift.id).where(Shift.code == code), Shift, user)
    )
    if duplicate is not None:
        raise _conflict(f"A shift with code {code} already exists")
    await _check_site(session, user, data.location_id)
    data.work_area = (data.work_area or "").strip() or None

    # The first shift a workspace creates becomes its default, so people with
    # no assignment are judged against something from day one. HR can move the
    # default to another shift at any time.
    has_default = await session.scalar(
        tenant_scoped(select(Shift.id).where(Shift.is_default.is_(True)), Shift, user)
    )
    shift = Shift(
        tenant_id=user.tenant_id,
        code=code,
        active=True,
        is_default=has_default is None,
        **{field: getattr(data, field) for field in _SHIFT_FIELDS},
        required_course_ids=await qualifications.check_course_ids(
            session, user, data.required_course_ids
        ),
    )
    session.add(shift)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="shift",
        entity_id=shift.id,
        actor=user,
        changes=_snapshot(shift),
        summary=f"Created shift {code} ({data.start_time}-{data.end_time})",
        request=request,
    )
    await session.commit()
    await session.refresh(shift)
    return shift


async def update_shift(
    session: AsyncSession,
    user: User,
    shift_id: uuid.UUID,
    data: ShiftUpdate,
    request: Request | None = None,
) -> Shift:
    shift = await _shift(session, user, shift_id)
    before = _snapshot(shift)
    await _check_site(session, user, data.location_id)
    data.work_area = (data.work_area or "").strip() or None
    for field in _SHIFT_FIELDS:
        setattr(shift, field, getattr(data, field))
    shift.required_course_ids = await qualifications.check_course_ids(
        session, user, data.required_course_ids
    )
    shift.active = data.active
    if not shift.active and shift.is_default:
        raise _conflict(
            "This is the default shift. Make another shift the default before deactivating it."
        )
    after = _snapshot(shift)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="shift",
        entity_id=shift.id,
        actor=user,
        changes={"before": before, "after": after},
        summary=(
            f"Updated shift {shift.code}. Days already computed keep their values "
            "until recomputed; a locked period needs an audited unlock first."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(shift)
    return shift


async def set_default_shift(
    session: AsyncSession, user: User, shift_id: uuid.UUID, request: Request | None = None
) -> Shift:
    """Make one shift the workspace default; the previous default stops being it."""
    shift = await _shift(session, user, shift_id)
    if not shift.active:
        raise _conflict("An inactive shift cannot be the default.")
    previous = (
        await session.scalars(
            tenant_scoped(
                select(Shift).where(Shift.is_default.is_(True), Shift.id != shift.id),
                Shift,
                user,
            )
        )
    ).all()
    for row in previous:
        row.is_default = False
    await session.flush()
    shift.is_default = True
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="shift",
        entity_id=shift.id,
        actor=user,
        changes={
            "is_default": {"from": False, "to": True},
            "previous_default": [row.code for row in previous],
        },
        summary=(
            f"Made {shift.code} the default shift. It applies to anyone without a shift or "
            "roster assignment; days already computed keep their values until recomputed."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(shift)
    return shift


# ── Rosters ─────────────────────────────────────────────────────────────────


async def list_rosters(session: AsyncSession, user: User) -> list[Roster]:
    stmt = tenant_scoped(select(Roster).order_by(Roster.name), Roster, user)
    return list((await session.scalars(stmt)).all())


async def _roster(session: AsyncSession, user: User, roster_id: uuid.UUID) -> Roster:
    row = await session.scalar(
        tenant_scoped(select(Roster).where(Roster.id == roster_id), Roster, user)
    )
    if row is None:
        raise _not_found("Roster")
    return row


async def _check_roster_shifts(
    session: AsyncSession, user: User, data: RosterCreate | RosterUpdate
) -> None:
    wanted = {d.shift_id for d in data.days if d.shift_id is not None}
    if not wanted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A rotation with every day off has nobody working. "
                "Assign a shift to at least one day."
            ),
        )
    found = set(
        (
            await session.scalars(
                tenant_scoped(select(Shift.id).where(Shift.id.in_(wanted)), Shift, user)
            )
        ).all()
    )
    if wanted - found:
        raise _not_found("Shift")


async def create_roster(
    session: AsyncSession, user: User, data: RosterCreate, request: Request | None = None
) -> Roster:
    duplicate = await session.scalar(
        tenant_scoped(select(Roster.id).where(Roster.name == data.name), Roster, user)
    )
    if duplicate is not None:
        raise _conflict(f"A roster named {data.name} already exists")
    await _check_roster_shifts(session, user, data)

    roster = Roster(
        tenant_id=user.tenant_id,
        name=data.name,
        description=data.description,
        cycle_days=data.cycle_days,
        active=True,
    )
    session.add(roster)
    await session.flush()
    for day in data.days:
        session.add(
            RosterDay(
                tenant_id=user.tenant_id,
                roster_id=roster.id,
                day_index=day.day_index,
                shift_id=day.shift_id,
            )
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="roster",
        entity_id=roster.id,
        actor=user,
        changes={
            "cycle_days": data.cycle_days,
            "days": [
                {"day_index": d.day_index, "shift_id": str(d.shift_id) if d.shift_id else None}
                for d in data.days
            ],
        },
        summary=f"Created roster {data.name} ({data.cycle_days}-day rotation)",
        request=request,
    )
    await session.commit()
    await session.refresh(roster, attribute_names=["days"])
    return roster


async def update_roster(
    session: AsyncSession,
    user: User,
    roster_id: uuid.UUID,
    data: RosterUpdate,
    request: Request | None = None,
) -> Roster:
    roster = await _roster(session, user, roster_id)
    duplicate = await session.scalar(
        tenant_scoped(
            select(Roster.id).where(Roster.name == data.name, Roster.id != roster.id),
            Roster,
            user,
        )
    )
    if duplicate is not None:
        raise _conflict(f"A roster named {data.name} already exists")
    await _check_roster_shifts(session, user, data)

    before = {
        "name": roster.name,
        "cycle_days": roster.cycle_days,
        "active": roster.active,
        "days": [
            {"day_index": d.day_index, "shift_id": str(d.shift_id) if d.shift_id else None}
            for d in sorted(roster.days, key=lambda d: d.day_index)
        ],
    }

    # Old days go first and are flushed, so the unique (roster, day_index)
    # constraint never sees the old and new row for the same position together.
    for day in list(roster.days):
        await session.delete(day)
    await session.flush()

    roster.name = data.name
    roster.description = data.description
    roster.cycle_days = data.cycle_days
    roster.active = data.active
    for day in data.days:
        session.add(
            RosterDay(
                tenant_id=user.tenant_id,
                roster_id=roster.id,
                day_index=day.day_index,
                shift_id=day.shift_id,
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="roster",
        entity_id=roster.id,
        actor=user,
        changes={
            "before": before,
            "after": {
                "name": data.name,
                "cycle_days": data.cycle_days,
                "active": data.active,
                "days": [
                    {"day_index": d.day_index, "shift_id": str(d.shift_id) if d.shift_id else None}
                    for d in data.days
                ],
            },
        },
        summary=f"Updated roster {data.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(roster, attribute_names=["days"])
    return roster


# ── Assignments ─────────────────────────────────────────────────────────────


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    row = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == employee_id, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )
    if row is None:
        raise _not_found("Employee")
    return row


async def _close_open_assignments(
    session: AsyncSession,
    user: User,
    model: type[ShiftAssignment] | type[RosterAssignment],
    employee_id: uuid.UUID,
    effective_from,
) -> list[dict[str, str]]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(model).where(
                    model.employee_id == employee_id,
                    or_(model.effective_to.is_(None), model.effective_to >= effective_from),
                ),
                model,
                user,
            )
        )
    ).all()
    closed: list[dict[str, str]] = []
    for row in rows:
        if row.effective_from >= effective_from:
            raise _conflict(
                f"An assignment already starts on {row.effective_from}. End or replace "
                "that one first — two assignments on one day cannot be computed."
            )
        row.effective_to = effective_from - timedelta(days=1)
        closed.append({"id": str(row.id), "effective_to": row.effective_to.isoformat()})
    return closed


def _assignment_read(
    row: ShiftAssignment | RosterAssignment, employee: Employee, target_name: str
) -> AssignmentRead:
    is_shift = isinstance(row, ShiftAssignment)
    return AssignmentRead(
        id=row.id,
        kind="shift" if is_shift else "roster",
        employee_id=employee.id,
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        shift_id=row.shift_id if is_shift else None,
        roster_id=None if is_shift else row.roster_id,
        target_name=target_name,
        cycle_start_date=None if is_shift else row.cycle_start_date,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
    )


async def list_assignments(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID | None = None
) -> list[AssignmentRead]:
    shift_stmt = select(ShiftAssignment, Employee, Shift).join(
        Employee, Employee.id == ShiftAssignment.employee_id
    ).join(Shift, Shift.id == ShiftAssignment.shift_id)
    roster_stmt = select(RosterAssignment, Employee, Roster).join(
        Employee, Employee.id == RosterAssignment.employee_id
    ).join(Roster, Roster.id == RosterAssignment.roster_id)
    if employee_id is not None:
        shift_stmt = shift_stmt.where(ShiftAssignment.employee_id == employee_id)
        roster_stmt = roster_stmt.where(RosterAssignment.employee_id == employee_id)

    out: list[AssignmentRead] = []
    for row, employee, shift in (
        await session.execute(tenant_scoped(shift_stmt, ShiftAssignment, user))
    ).all():
        out.append(_assignment_read(row, employee, f"{shift.code} · {shift.name}"))
    for row, employee, roster in (
        await session.execute(tenant_scoped(roster_stmt, RosterAssignment, user))
    ).all():
        out.append(_assignment_read(row, employee, roster.name))

    out.sort(key=lambda a: (a.employee_code, -a.effective_from.toordinal(), a.kind))
    return out


async def assign_shift(
    session: AsyncSession,
    user: User,
    data: ShiftAssignmentCreate,
    request: Request | None = None,
) -> AssignmentRead:
    employee = await _employee(session, user, data.employee_id)
    shift = await _shift(session, user, data.shift_id)
    if not shift.active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Shift {shift.code} is inactive. Reactivate it before assigning it.",
        )
    await qualifications.require_qualified(
        session, user, employee.id, [shift], data.effective_from, who=employee.employee_code
    )
    closed = await _close_open_assignments(
        session, user, ShiftAssignment, employee.id, data.effective_from
    )
    row = ShiftAssignment(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        shift_id=shift.id,
        effective_from=data.effective_from,
        effective_to=data.effective_to,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="shift_assignment",
        entity_id=row.id,
        actor=user,
        changes={
            "employee_id": str(employee.id),
            "shift_id": str(shift.id),
            "effective_from": data.effective_from.isoformat(),
            "effective_to": data.effective_to.isoformat() if data.effective_to else None,
            "closed": closed,
        },
        summary=(
            f"Assigned {employee.employee_code} to shift {shift.code} "
            f"from {data.effective_from}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _assignment_read(row, employee, f"{shift.code} · {shift.name}")


async def assign_roster(
    session: AsyncSession,
    user: User,
    data: RosterAssignmentCreate,
    request: Request | None = None,
) -> AssignmentRead:
    employee = await _employee(session, user, data.employee_id)
    roster = await _roster(session, user, data.roster_id)
    if not roster.active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Roster {roster.name} is inactive. Reactivate it before assigning it.",
        )
    await qualifications.require_qualified(
        session,
        user,
        employee.id,
        [day.shift for day in roster.days if day.shift is not None],
        data.effective_from,
        who=employee.employee_code,
    )
    closed = await _close_open_assignments(
        session, user, RosterAssignment, employee.id, data.effective_from
    )
    row = RosterAssignment(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        roster_id=roster.id,
        cycle_start_date=data.cycle_start_date,
        effective_from=data.effective_from,
        effective_to=data.effective_to,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="roster_assignment",
        entity_id=row.id,
        actor=user,
        changes={
            "employee_id": str(employee.id),
            "roster_id": str(roster.id),
            "cycle_start_date": data.cycle_start_date.isoformat(),
            "effective_from": data.effective_from.isoformat(),
            "effective_to": data.effective_to.isoformat() if data.effective_to else None,
            "closed": closed,
        },
        summary=(
            f"Assigned {employee.employee_code} to roster {roster.name} "
            f"from {data.effective_from}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _assignment_read(row, employee, roster.name)


async def end_assignment(
    session: AsyncSession,
    user: User,
    *,
    kind: Literal["shift", "roster"],
    assignment_id: uuid.UUID,
    effective_to,
    request: Request | None = None,
) -> AssignmentRead:
    model = ShiftAssignment if kind == "shift" else RosterAssignment
    row = await session.scalar(
        tenant_scoped(select(model).where(model.id == assignment_id), model, user)
    )
    if row is None:
        raise _not_found("Assignment")
    if effective_to < row.effective_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"This assignment starts on {row.effective_from}; it cannot end before that.",
        )
    previous = row.effective_to
    row.effective_to = effective_to
    employee = await _employee(session, user, row.employee_id)
    if isinstance(row, ShiftAssignment):
        target = await _shift(session, user, row.shift_id)
        target_name = f"{target.code} · {target.name}"
    else:
        target_name = (await _roster(session, user, row.roster_id)).name

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type=f"{kind}_assignment",
        entity_id=row.id,
        actor=user,
        changes={
            "effective_to": {
                "before": previous.isoformat() if previous else None,
                "after": effective_to.isoformat(),
            }
        },
        summary=f"Ended {employee.employee_code}'s {kind} assignment on {effective_to}",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _assignment_read(row, employee, target_name)


# ── Work locations ──────────────────────────────────────────────────────────


async def list_work_locations(session: AsyncSession, user: User) -> list[WorkLocation]:
    stmt = tenant_scoped(select(WorkLocation).order_by(WorkLocation.name), WorkLocation, user)
    return list((await session.scalars(stmt)).all())


async def create_work_location(
    session: AsyncSession,
    user: User,
    data: WorkLocationCreate,
    request: Request | None = None,
) -> WorkLocation:
    if data.location_id is not None:
        exists = await session.scalar(
            tenant_scoped(
                select(Location.id).where(Location.id == data.location_id), Location, user
            )
        )
        if exists is None:
            raise _not_found("Location")

    row = WorkLocation(
        tenant_id=user.tenant_id,
        name=data.name,
        location_id=data.location_id,
        latitude=data.latitude,
        longitude=data.longitude,
        radius_metres=data.radius_metres,
        active=True,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="work_location",
        entity_id=row.id,
        actor=user,
        changes={"name": data.name, "radius_metres": data.radius_metres},
        summary=f"Added geofenced work location {data.name} ({data.radius_metres} m)",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


# ── Workspace attendance settings ───────────────────────────────────────────


async def attendance_settings(session: AsyncSession, user: User) -> AttendanceSettings:
    """The workspace's attendance settings; "optional" location when never set."""
    tenant = await session.get(Tenant, user.tenant_id)
    raw = ((tenant.settings or {}).get("attendance") or {}) if tenant else {}
    return AttendanceSettings.model_validate(raw)


async def attendance_settings_with_locations(
    session: AsyncSession, user: User
) -> AttendanceSettingsRead:
    """The settings, plus the active work locations the punch map draws."""
    settings = await attendance_settings(session, user)
    stmt = select(WorkLocation).where(WorkLocation.active.is_(True)).order_by(WorkLocation.name)
    locations = (await session.scalars(tenant_scoped(stmt, WorkLocation, user))).all()
    return AttendanceSettingsRead(
        **settings.model_dump(),
        work_locations=[
            WorkLocationPin(
                name=row.name,
                latitude=float(row.latitude),
                longitude=float(row.longitude),
                radius_metres=row.radius_metres,
            )
            for row in locations
        ],
    )


async def update_attendance_settings(
    session: AsyncSession,
    user: User,
    data: AttendanceSettings,
    request: Request | None = None,
) -> AttendanceSettings:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise _not_found("Workspace")
    before = await attendance_settings(session, user)
    settings = dict(tenant.settings or {})
    # Only what was sent: saving the location rule must not reset the photo rule.
    settings["attendance"] = {
        **(settings.get("attendance") or {}),
        **data.model_dump(exclude_unset=True),
    }
    # Reassigned, not mutated: JSONB changes are only seen on assignment.
    tenant.settings = settings
    after = AttendanceSettings.model_validate(settings["attendance"])
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="attendance_settings",
        entity_id=tenant.id,
        actor=user,
        changes={"before": before.model_dump(), "after": after.model_dump()},
        summary=(
            f"Punch location is now {after.location_mode}, punch photo {after.photo_mode}"
        ),
        request=request,
    )
    await session.commit()
    return after
