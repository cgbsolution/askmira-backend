"""The holiday calendar: who sees which holidays, HR's edits, and optional holidays.

Which holiday applies to whom is decided once, in `holidays.holiday_applies`;
this module only lists, edits and records opt-ins. A viewer's default list is
their own: national holidays, their state's and their office's — a Chennai
employee has no use for Karnataka Rajyotsava — and their project's. HR can
widen it to a state, a project's calendar, or everything.

Each project has a calendar: the company's holidays plus any added for the
project, or — once it keeps its own (`Project.own_holidays_from`) — the
project's holidays only. HR starts an own calendar with a copy of the
company's, and copies one project's calendar to another, so a client's list is
set up once and changed only where the client differs.

Past holidays are not edited or deleted here. Attendance and payroll may
already have paid them; changing one is a correction to closed months, which
is not a calendar edit.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.leave import Holiday, LeaveRequest, OptionalHolidaySelection
from app.models.org import LegalEntity, Location
from app.models.timesheets import Project
from app.models.user import User
from app.schemas.leave import (
    HolidayCalendarRead,
    HolidayCalendarUpdate,
    HolidayCopyRequest,
    HolidayCopyResult,
    HolidayCreate,
    HolidayRead,
    HolidayUpdate,
    OptionalHolidaysRead,
)
from app.services import audit
from app.services.leave import service as core
from app.services.leave.holidays import holiday_applies, in_project_calendar


def _not_found(detail: str = "Holiday not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _scope(holiday: Holiday) -> str:
    if holiday.location_id is not None:
        return "location"
    if holiday.state_code is not None:
        return "state"
    return "national"


#: (location, its state, legal entity, project, the project's own-calendar date).
Place = tuple[uuid.UUID | None, str | None, uuid.UUID | None, uuid.UUID | None, date | None]


async def _place(session: AsyncSession, user: User) -> Place | None:
    """The caller's work location, its state, their legal entity and project,
    or None without an employee record."""
    if user.employee_id is None:
        return None
    row = (
        await session.execute(
            tenant_scoped(
                select(
                    Employee.location_id,
                    Location.state_code,
                    Employee.legal_entity_id,
                    Employee.project_id,
                    Project.own_holidays_from,
                )
                .outerjoin(Location, Location.id == Employee.location_id)
                .outerjoin(Project, Project.id == Employee.project_id)
                .where(Employee.id == user.employee_id),
                Employee,
                user,
            )
        )
    ).first()
    return (row[0], row[1], row[2], row[3], row[4]) if row else (None, None, None, None, None)


def _applies(holiday: Holiday, place: Place) -> bool:
    """In scope for someone at this place (optional ones included: to offer them)."""
    return holiday_applies(
        holiday,
        state_code=place[1],
        location_id=place[0],
        legal_entity_id=place[2],
        project_id=place[3],
        own_holidays_from=place[4],
        include_optional=True,
    )


async def _project_names(session: AsyncSession, user: User) -> dict[uuid.UUID, str]:
    rows = (
        await session.execute(tenant_scoped(select(Project.id, Project.name), Project, user))
    ).all()
    return {project_id: name for project_id, name in rows}


async def _entity_names(session: AsyncSession, user: User) -> dict[uuid.UUID, str]:
    rows = (
        await session.execute(
            tenant_scoped(select(LegalEntity.id, LegalEntity.name), LegalEntity, user)
        )
    ).all()
    return {entity_id: name for entity_id, name in rows}


async def _location_names(session: AsyncSession, user: User) -> dict[uuid.UUID, tuple[str, str]]:
    rows = (
        await session.execute(
            tenant_scoped(select(Location.id, Location.name, Location.state_code), Location, user)
        )
    ).all()
    return {location_id: (name, state) for location_id, name, state in rows}


def _read(
    holiday: Holiday,
    *,
    locations: dict[uuid.UUID, tuple[str, str]],
    entities: dict[uuid.UUID, str],
    projects: dict[uuid.UUID, str],
    place: Place | None,
    selected: set[uuid.UUID],
) -> HolidayRead:
    applies = place is not None and _applies(holiday, place)
    location = locations.get(holiday.location_id) if holiday.location_id else None
    return HolidayRead(
        id=holiday.id,
        name=holiday.name,
        holiday_date=holiday.holiday_date,
        state_code=holiday.state_code or (location[1] if location else None),
        location_id=holiday.location_id,
        location_name=location[0] if location else None,
        scope=_scope(holiday),  # type: ignore[arg-type]
        legal_entity_id=holiday.legal_entity_id,
        legal_entity_name=(
            entities.get(holiday.legal_entity_id) if holiday.legal_entity_id else None
        ),
        project_id=holiday.project_id,
        project_name=projects.get(holiday.project_id) if holiday.project_id else None,
        is_optional=holiday.is_optional,
        is_paid=holiday.is_paid,
        applies_to_me=applies,
        selected=holiday.id in selected,
    )


async def _selected_ids(session: AsyncSession, user: User, year: int) -> set[uuid.UUID]:
    if user.employee_id is None:
        return set()
    return set(
        (
            await session.scalars(
                tenant_scoped(
                    select(OptionalHolidaySelection.holiday_id).where(
                        OptionalHolidaySelection.employee_id == user.employee_id,
                        OptionalHolidaySelection.year == year,
                    ),
                    OptionalHolidaySelection,
                    user,
                )
            )
        ).all()
    )


async def _project_row(session: AsyncSession, user: User, project_id: uuid.UUID) -> Project:
    project = await session.scalar(
        tenant_scoped(select(Project).where(Project.id == project_id), Project, user)
    )
    if project is None:
        raise _not_found("Project not found")
    return project


async def list_holidays(
    session: AsyncSession,
    user: User,
    *,
    year: int,
    scope: str = "mine",
    state_code: str | None = None,
    project_id: uuid.UUID | None = None,
) -> list[HolidayRead]:
    """A year's holidays.

    `mine` (the default) is the caller's own calendar; `all` is every holiday in
    the workspace; `company` the company calendar alone, without any project's;
    a `state_code` is that state's list with the national ones and those of its
    offices; a `project_id` is that project's calendar — what its people get.
    Reference material, but it is still nobody's business to be shown another
    state's list by default, and another project's calendar is HR's only.
    """
    holidays = (
        await session.scalars(
            tenant_scoped(
                select(Holiday).where(Holiday.year == year).order_by(Holiday.holiday_date),
                Holiday,
                user,
            )
        )
    ).all()
    locations = await _location_names(session, user)
    entities = await _entity_names(session, user)
    projects = await _project_names(session, user)
    place = await _place(session, user)
    selected = await _selected_ids(session, user, year)
    is_hr = permissions.is_hr(user)
    own_project = place[3] if place else None

    if project_id is not None:
        if not is_hr and project_id != own_project:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only HR can see another project's holiday calendar.",
            )
        project = await _project_row(session, user, project_id)
        holidays = [
            h
            for h in holidays
            if in_project_calendar(
                h, project_id=project.id, own_holidays_from=project.own_holidays_from
            )
        ]
    elif not is_hr:
        # The company's holidays and your own project's; other projects' are
        # their people's business.
        holidays = [h for h in holidays if h.project_id is None or h.project_id == own_project]
    if scope == "company" and project_id is None:
        holidays = [h for h in holidays if h.project_id is None]

    rows = [
        _read(
            h,
            locations=locations,
            entities=entities,
            projects=projects,
            place=place,
            selected=selected,
        )
        for h in holidays
    ]

    if state_code:
        code = state_code.upper()
        return [
            row for row in rows if row.scope == "national" or (row.state_code or "").upper() == code
        ]
    if project_id is not None or scope in ("all", "company") or place is None:
        return rows
    return [row for row in rows if row.applies_to_me]


async def _location_state(session: AsyncSession, user: User, location_id: uuid.UUID) -> str:
    state = await session.scalar(
        tenant_scoped(select(Location.state_code).where(Location.id == location_id), Location, user)
    )
    if state is None:
        raise _not_found("Location not found")
    return state


async def _check_entity(
    session: AsyncSession,
    user: User,
    legal_entity_id: uuid.UUID | None,
    location_id: uuid.UUID | None,
) -> None:
    """The legal entity exists here, and an office holiday's office belongs to it."""
    if legal_entity_id is None:
        return
    found = await session.scalar(
        tenant_scoped(
            select(LegalEntity.id).where(LegalEntity.id == legal_entity_id), LegalEntity, user
        )
    )
    if found is None:
        raise _not_found("Legal entity not found")
    if location_id is not None:
        owner = await session.scalar(
            tenant_scoped(
                select(Location.legal_entity_id).where(Location.id == location_id), Location, user
            )
        )
        if owner != legal_entity_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That office belongs to another legal entity.",
            )


async def _check_project(
    session: AsyncSession,
    user: User,
    project_id: uuid.UUID | None,
    legal_entity_id: uuid.UUID | None,
) -> Project | None:
    """The project exists here, and a one-company holiday is for its company."""
    if project_id is None:
        return None
    project = await _project_row(session, user, project_id)
    if (
        legal_entity_id is not None
        and project.legal_entity_id is not None
        and project.legal_entity_id != legal_entity_id
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{project.name} belongs to another company of the group.",
        )
    return project


async def _refuse_duplicate(
    session: AsyncSession,
    user: User,
    *,
    on: date,
    state_code: str | None,
    location_id: uuid.UUID | None,
    legal_entity_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    exclude: uuid.UUID | None = None,
) -> None:
    stmt = select(Holiday.id).where(
        Holiday.holiday_date == on,
        Holiday.state_code.is_(None) if state_code is None else Holiday.state_code == state_code,
        Holiday.location_id.is_(None)
        if location_id is None
        else Holiday.location_id == location_id,
        Holiday.legal_entity_id.is_(None)
        if legal_entity_id is None
        else Holiday.legal_entity_id == legal_entity_id,
        Holiday.project_id.is_(None) if project_id is None else Holiday.project_id == project_id,
    )
    if exclude is not None:
        stmt = stmt.where(Holiday.id != exclude)
    if await session.scalar(tenant_scoped(stmt, Holiday, user)) is not None:
        raise _conflict(
            "That date already has a holiday for the same scope"
            + (" on this project's calendar." if project_id else " on the company calendar.")
        )


async def _one(session: AsyncSession, user: User, holiday_id: uuid.UUID) -> Holiday:
    holiday = await session.scalar(
        tenant_scoped(select(Holiday).where(Holiday.id == holiday_id), Holiday, user)
    )
    if holiday is None:
        raise _not_found()
    return holiday


async def _view(session: AsyncSession, user: User, holiday: Holiday) -> HolidayRead:
    return _read(
        holiday,
        locations=await _location_names(session, user),
        entities=await _entity_names(session, user),
        projects=await _project_names(session, user),
        place=await _place(session, user),
        selected=await _selected_ids(session, user, holiday.year),
    )


def _on_calendar(project: Project | None) -> str:
    return f" for {project.name}" if project is not None else ""


async def create_holiday(
    session: AsyncSession, user: User, payload: HolidayCreate, *, request: Request | None = None
) -> HolidayRead:
    state_code = payload.state_code.upper() if payload.state_code else None
    if payload.location_id is not None:
        await _location_state(session, user, payload.location_id)
    await _check_entity(session, user, payload.legal_entity_id, payload.location_id)
    project = await _check_project(session, user, payload.project_id, payload.legal_entity_id)
    await _refuse_duplicate(
        session,
        user,
        on=payload.holiday_date,
        state_code=state_code,
        location_id=payload.location_id,
        legal_entity_id=payload.legal_entity_id,
        project_id=payload.project_id,
    )
    holiday = Holiday(
        tenant_id=user.tenant_id,
        name=payload.name.strip(),
        holiday_date=payload.holiday_date,
        year=payload.holiday_date.year,
        state_code=state_code,
        location_id=payload.location_id,
        legal_entity_id=payload.legal_entity_id,
        project_id=payload.project_id,
        is_optional=payload.is_optional,
        is_paid=payload.is_paid,
    )
    session.add(holiday)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="holiday",
        entity_id=holiday.id,
        actor=user,
        changes=payload.model_dump(mode="json"),
        summary=f"Added holiday {payload.name} on {payload.holiday_date}{_on_calendar(project)}",
        request=request,
    )
    await session.commit()
    await session.refresh(holiday)
    return await _view(session, user, holiday)


def _refuse_past(holiday_date: date, today: date) -> None:
    if holiday_date < today:
        raise _conflict(
            "This holiday has already passed, and attendance and payroll may already "
            "reflect it. Past holidays are not edited here."
        )


async def update_holiday(
    session: AsyncSession,
    user: User,
    holiday_id: uuid.UUID,
    payload: HolidayUpdate,
    *,
    request: Request | None = None,
) -> HolidayRead:
    holiday = await _one(session, user, holiday_id)
    today = core.local_today()
    _refuse_past(holiday.holiday_date, today)
    before = {
        "name": holiday.name,
        "holiday_date": holiday.holiday_date.isoformat(),
        "state_code": holiday.state_code,
        "location_id": str(holiday.location_id) if holiday.location_id else None,
        "legal_entity_id": str(holiday.legal_entity_id) if holiday.legal_entity_id else None,
        "project_id": str(holiday.project_id) if holiday.project_id else None,
        "is_optional": holiday.is_optional,
        "is_paid": holiday.is_paid,
    }

    new_date = payload.holiday_date or holiday.holiday_date
    _refuse_past(new_date, today)
    state_code, location_id = holiday.state_code, holiday.location_id
    if payload.scope == "national":
        state_code, location_id = None, None
    elif payload.scope == "state":
        if not payload.state_code:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Name the state for a state holiday.",
            )
        state_code, location_id = payload.state_code.upper(), None
    elif payload.scope == "location":
        if payload.location_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Choose the location for a location holiday.",
            )
        await _location_state(session, user, payload.location_id)
        state_code, location_id = None, payload.location_id
    legal_entity_id = (
        payload.legal_entity_id
        if "legal_entity_id" in payload.model_fields_set
        else holiday.legal_entity_id
    )
    await _check_entity(session, user, legal_entity_id, location_id)
    project_id = (
        payload.project_id if "project_id" in payload.model_fields_set else holiday.project_id
    )
    project = await _check_project(session, user, project_id, legal_entity_id)
    await _refuse_duplicate(
        session,
        user,
        on=new_date,
        state_code=state_code,
        location_id=location_id,
        legal_entity_id=legal_entity_id,
        project_id=project_id,
        exclude=holiday.id,
    )

    if payload.name is not None:
        holiday.name = payload.name.strip()
    holiday.holiday_date = new_date
    holiday.year = new_date.year
    holiday.state_code = state_code
    holiday.location_id = location_id
    holiday.legal_entity_id = legal_entity_id
    holiday.project_id = project_id
    if payload.is_paid is not None:
        holiday.is_paid = payload.is_paid
    dropped = 0
    if payload.is_optional is not None:
        if holiday.is_optional and not payload.is_optional:
            # Mandatory now: the opt-ins no longer mean anything, and they
            # must not keep counting against anybody's quota.
            opt_ins = (
                await session.scalars(
                    tenant_scoped(
                        select(OptionalHolidaySelection).where(
                            OptionalHolidaySelection.holiday_id == holiday.id
                        ),
                        OptionalHolidaySelection,
                        user,
                    )
                )
            ).all()
            for opt_in in opt_ins:
                await session.delete(opt_in)
            dropped = len(opt_ins)
        holiday.is_optional = payload.is_optional
    if holiday.is_optional:
        dropped += await _drop_opt_ins_out_of_scope(session, user, holiday)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="holiday",
        entity_id=holiday.id,
        actor=user,
        changes={
            "before": before,
            "after": payload.model_dump(mode="json", exclude_unset=True),
            "opt_ins_dropped": dropped,
        },
        summary=f"Edited holiday {holiday.name} on {holiday.holiday_date}{_on_calendar(project)}",
        request=request,
    )
    await session.commit()
    await session.refresh(holiday)
    return await _view(session, user, holiday)


async def _drop_opt_ins_out_of_scope(session: AsyncSession, user: User, holiday: Holiday) -> int:
    """An optional holiday moved to another place or project: the opt-ins of
    people it no longer covers go, so they stop counting against their quota."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    OptionalHolidaySelection,
                    Employee.location_id,
                    Location.state_code,
                    Employee.legal_entity_id,
                    Employee.project_id,
                    Project.own_holidays_from,
                )
                .join(Employee, Employee.id == OptionalHolidaySelection.employee_id)
                .outerjoin(Location, Location.id == Employee.location_id)
                .outerjoin(Project, Project.id == Employee.project_id)
                .where(OptionalHolidaySelection.holiday_id == holiday.id),
                OptionalHolidaySelection,
                user,
            )
        )
    ).all()
    dropped = 0
    for selection, location_id, state, entity_id, project_id, own_from in rows:
        if not _applies(holiday, (location_id, state, entity_id, project_id, own_from)):
            await session.delete(selection)
            dropped += 1
    return dropped


async def delete_holiday(
    session: AsyncSession, user: User, holiday_id: uuid.UUID, *, request: Request | None = None
) -> None:
    holiday = await _one(session, user, holiday_id)
    _refuse_past(holiday.holiday_date, core.local_today())
    opt_ins = await session.scalar(
        tenant_scoped(
            select(func.count(OptionalHolidaySelection.id)).where(
                OptionalHolidaySelection.holiday_id == holiday.id
            ),
            OptionalHolidaySelection,
            user,
        )
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="holiday",
        entity_id=holiday.id,
        actor=user,
        changes={
            "name": holiday.name,
            "holiday_date": holiday.holiday_date.isoformat(),
            "state_code": holiday.state_code,
            "location_id": str(holiday.location_id) if holiday.location_id else None,
            "legal_entity_id": str(holiday.legal_entity_id) if holiday.legal_entity_id else None,
            "project_id": str(holiday.project_id) if holiday.project_id else None,
            "is_optional": holiday.is_optional,
            "is_paid": holiday.is_paid,
            "opt_ins_dropped": opt_ins or 0,
        },
        summary=f"Removed holiday {holiday.name} on {holiday.holiday_date}",
        request=request,
    )
    # The opt-ins go with it (ON DELETE CASCADE).
    await session.delete(holiday)
    await session.commit()


# ── Project calendars ───────────────────────────────────────────────────────


async def _year_holidays(session: AsyncSession, user: User, years: set[int]) -> list[Holiday]:
    if not years:
        return []
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(Holiday)
                    .where(Holiday.year.in_(sorted(years)))
                    .order_by(Holiday.holiday_date),
                    Holiday,
                    user,
                )
            )
        ).all()
    )


def _calendar_read(
    project: Project, holidays: list[Holiday], headcount: int
) -> HolidayCalendarRead:
    return HolidayCalendarRead(
        project_id=project.id,
        code=project.code,
        name=project.name,
        client=project.client,
        active=project.active,
        legal_entity_id=project.legal_entity_id,
        headcount=headcount,
        own_holidays_from=project.own_holidays_from,
        holiday_count=sum(
            1
            for h in holidays
            if in_project_calendar(
                h, project_id=project.id, own_holidays_from=project.own_holidays_from
            )
        ),
        own_count=sum(1 for h in holidays if h.project_id == project.id),
    )


async def list_calendars(
    session: AsyncSession, user: User, *, year: int
) -> list[HolidayCalendarRead]:
    """The company calendar and each project's, with what each holds in the year.

    A closed project is listed only while somebody is still on it or it still
    has a calendar of its own.
    """
    from app.services.timesheets import project_headcounts

    holidays = await _year_holidays(session, user, {year})
    projects = (
        await session.scalars(tenant_scoped(select(Project).order_by(Project.code), Project, user))
    ).all()
    counts = await project_headcounts(session, user)
    calendars = [
        HolidayCalendarRead(
            name="Company calendar",
            holiday_count=sum(1 for h in holidays if h.project_id is None),
        )
    ]
    for project in projects:
        read = _calendar_read(project, holidays, counts.get(project.id, 0))
        if project.active or read.headcount or read.own_count or project.own_holidays_from:
            calendars.append(read)
    return calendars


async def _copy_into(
    session: AsyncSession,
    user: User,
    project: Project,
    sources: list[Holiday],
    *,
    today: date,
    request: Request | None,
) -> HolidayCopyResult:
    """Copy holidays onto a project's calendar, skipping what it already has.

    "Already has" is what the project's people already get on that date for
    that place — its own holidays and, unless it keeps its own calendar, the
    company's. Past dates are skipped: attendance and payroll may already
    reflect them. One audit row per holiday added, in the caller's transaction.
    """
    existing = await _year_holidays(session, user, {h.year for h in sources})
    taken = {
        (h.holiday_date, h.state_code, h.location_id, h.legal_entity_id)
        for h in existing
        if in_project_calendar(
            h, project_id=project.id, own_holidays_from=project.own_holidays_from
        )
    }
    copied = already = past = other = 0
    for source in sorted(sources, key=lambda h: h.holiday_date):
        if source.holiday_date < today:
            past += 1
            continue
        if (
            source.legal_entity_id is not None
            and project.legal_entity_id is not None
            and source.legal_entity_id != project.legal_entity_id
        ):
            other += 1
            continue
        key = (source.holiday_date, source.state_code, source.location_id, source.legal_entity_id)
        if key in taken:
            already += 1
            continue
        holiday = Holiday(
            tenant_id=user.tenant_id,
            name=source.name,
            holiday_date=source.holiday_date,
            year=source.holiday_date.year,
            state_code=source.state_code,
            location_id=source.location_id,
            legal_entity_id=source.legal_entity_id,
            project_id=project.id,
            is_optional=source.is_optional,
            is_paid=source.is_paid,
        )
        session.add(holiday)
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="holiday",
            entity_id=holiday.id,
            actor=user,
            changes={
                "name": holiday.name,
                "holiday_date": holiday.holiday_date.isoformat(),
                "project_id": str(project.id),
                "copied_from": str(source.id),
            },
            summary=f"Copied holiday {holiday.name} on {holiday.holiday_date} to {project.name}",
            request=request,
        )
        taken.add(key)
        copied += 1
    return HolidayCopyResult(copied=copied, already_there=already, past=past, other_company=other)


async def set_calendar(
    session: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    payload: HolidayCalendarUpdate,
    *,
    year: int,
    request: Request | None = None,
) -> HolidayCalendarRead:
    """Let a project keep its own holiday calendar from a date, or follow the
    company's again.

    From the date on, the company's holidays stop applying to the project's
    people; before it they still do. The date is today or later — a past day
    has been worked out, and perhaps paid, with the company's holidays.
    Turning it on can start the calendar with the company's holidays from that
    date, so HR then changes only what the client does differently.
    """
    from app.services.timesheets import project_headcounts

    project = await _project_row(session, user, project_id)
    today = core.local_today()
    before = project.own_holidays_from
    after = payload.own_holidays_from
    if after is not None and after != before and after < today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Choose today or a later date. Days already passed were worked out with the "
                "company's holidays."
            ),
        )
    if after is not None and before is not None and after != before and before < today:
        raise _conflict(
            f"{project.name} has kept its own calendar since {before:%d %b %Y}, and the days "
            "since were worked out with it. Keep the date, or switch back to the company "
            "calendar."
        )
    project.own_holidays_from = after
    result: HolidayCopyResult | None = None
    if after is not None and payload.copy_company_holidays:
        company = (
            await session.scalars(
                tenant_scoped(
                    select(Holiday).where(
                        Holiday.project_id.is_(None), Holiday.holiday_date >= after
                    ),
                    Holiday,
                    user,
                )
            )
        ).all()
        result = await _copy_into(
            session, user, project, list(company), today=today, request=request
        )

    if after != before or (result is not None and result.copied):
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="project",
            entity_id=project.id,
            actor=user,
            changes={
                "own_holidays_from": [
                    before.isoformat() if before else None,
                    after.isoformat() if after else None,
                ],
                "company_holidays_copied": result.copied if result else 0,
            },
            summary=(
                f"{project.name} keeps its own holiday calendar from {after}"
                if after is not None
                else f"{project.name} follows the company holiday calendar again"
            ),
            request=request,
        )
        await session.commit()
        await session.refresh(project)
    counts = await project_headcounts(session, user)
    read = _calendar_read(
        project, await _year_holidays(session, user, {year}), counts.get(project.id, 0)
    )
    read.copied = result
    return read


async def copy_holidays(
    session: AsyncSession,
    user: User,
    payload: HolidayCopyRequest,
    *,
    request: Request | None = None,
) -> HolidayCopyResult:
    """Copy a year's holidays from the company calendar or another project's
    onto a project's calendar — what the source's people get, as listed."""
    target = await _project_row(session, user, payload.to_project_id)
    if payload.from_project_id == target.id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose another calendar to copy from.",
        )
    holidays = await _year_holidays(session, user, {payload.year})
    if payload.from_project_id is None:
        sources = [h for h in holidays if h.project_id is None]
    else:
        source = await _project_row(session, user, payload.from_project_id)
        sources = [
            h
            for h in holidays
            if in_project_calendar(
                h, project_id=source.id, own_holidays_from=source.own_holidays_from
            )
        ]
    result = await _copy_into(
        session, user, target, sources, today=core.local_today(), request=request
    )
    if result.copied:
        await session.commit()
    return result


# ── Optional holidays ───────────────────────────────────────────────────────


async def optional_holidays(
    session: AsyncSession, user: User, *, year: int
) -> OptionalHolidaysRead:
    """The optional holidays open to the caller this year, and how many are chosen."""
    permissions.own_employee_id(user)
    rows = [
        row
        for row in await list_holidays(session, user, year=year, scope="mine")
        if row.is_optional
    ]
    return OptionalHolidaysRead(
        year=year,
        quota=await core.optional_holiday_quota(session, user),
        chosen=sum(1 for row in rows if row.selected),
        holidays=rows,
    )


async def select_optional(
    session: AsyncSession, user: User, holiday_id: uuid.UUID, *, request: Request | None = None
) -> OptionalHolidaysRead:
    """Opt in to an optional holiday, within the yearly quota.

    Only for yourself — a floating holiday is the employee's choice. The day
    then counts as a holiday for them everywhere: attendance, leave counting,
    the calendar.
    """
    employee_id = permissions.own_employee_id(user)
    employee = await core._employee(session, user, employee_id)
    holiday = await _one(session, user, holiday_id)
    if not holiday.is_optional:
        raise _conflict(f"{holiday.name} is not an optional holiday; it applies automatically.")
    place = await _place(session, user)
    if place is None or not _applies(holiday, place):
        raise _conflict(f"{holiday.name} is not offered at your work location or project.")
    today = await core.today_for(session, user, employee)
    if holiday.holiday_date < today:
        raise _conflict("This holiday has already passed.")

    chosen = await _selected_ids(session, user, holiday.year)
    if holiday.id in chosen:
        return await optional_holidays(session, user, year=holiday.year)
    quota = await core.optional_holiday_quota(session, user)
    if len(chosen) >= quota:
        raise _conflict(
            f"You have already chosen {len(chosen)} of your {quota} optional holidays for "
            f"{holiday.year}. Withdraw one to choose another."
        )
    leave_that_day = await session.scalar(
        tenant_scoped(
            select(func.count(LeaveRequest.id)).where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.status.in_(core.OPEN_STATUSES),
                LeaveRequest.start_date <= holiday.holiday_date,
                LeaveRequest.end_date >= holiday.holiday_date,
            ),
            LeaveRequest,
            user,
        )
    )
    if leave_that_day:
        raise _conflict(
            "You have a leave request covering that day. Cancel it first, so the day is not "
            "charged twice."
        )

    selection = OptionalHolidaySelection(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        holiday_id=holiday.id,
        year=holiday.year,
        selected_by_user_id=user.id,
    )
    session.add(selection)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="optional_holiday_selection",
        entity_id=selection.id,
        actor=user,
        changes={"holiday": holiday.name, "date": holiday.holiday_date.isoformat()},
        summary=f"Opted in to {holiday.name} on {holiday.holiday_date}",
        request=request,
    )
    await _recompute_day(session, user, employee_id, holiday.holiday_date, today)
    await session.commit()
    return await optional_holidays(session, user, year=holiday.year)


async def withdraw_optional(
    session: AsyncSession, user: User, holiday_id: uuid.UUID, *, request: Request | None = None
) -> OptionalHolidaysRead:
    employee_id = permissions.own_employee_id(user)
    employee = await core._employee(session, user, employee_id)
    holiday = await _one(session, user, holiday_id)
    selection = await session.scalar(
        tenant_scoped(
            select(OptionalHolidaySelection).where(
                OptionalHolidaySelection.employee_id == employee_id,
                OptionalHolidaySelection.holiday_id == holiday.id,
            ),
            OptionalHolidaySelection,
            user,
        )
    )
    if selection is None:
        return await optional_holidays(session, user, year=holiday.year)
    today = await core.today_for(session, user, employee)
    if holiday.holiday_date < today:
        raise _conflict("This holiday has already passed; the choice stands.")
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="optional_holiday_selection",
        entity_id=selection.id,
        actor=user,
        changes={"holiday": holiday.name, "date": holiday.holiday_date.isoformat()},
        summary=f"Withdrew from {holiday.name} on {holiday.holiday_date}",
        request=request,
    )
    await session.delete(selection)
    await _recompute_day(session, user, employee_id, holiday.holiday_date, today)
    await session.commit()
    return await optional_holidays(session, user, year=holiday.year)


async def _recompute_day(
    session: AsyncSession, user: User, employee_id: uuid.UUID, on: date, today: date
) -> None:
    """Attendance for today, when the choice is about today. Future days are
    computed when they arrive."""
    from app.services.attendance import service as attendance

    if on > today:
        return
    await session.flush()
    try:
        await attendance.recompute_range(
            session, user, employee_ids=[employee_id], start=on, end=on, commit=False
        )
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise
        # Locked for payroll: the choice stands, the day changes only through
        # an audited unlock.


__all__ = [
    "copy_holidays",
    "create_holiday",
    "delete_holiday",
    "list_calendars",
    "list_holidays",
    "optional_holidays",
    "select_optional",
    "set_calendar",
    "update_holiday",
    "withdraw_optional",
]
