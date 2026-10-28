"""The holiday calendar: who sees which holidays, HR's edits, and optional holidays.

Which holiday applies to whom is decided once, in `holidays.holiday_applies`;
this module only lists, edits and records opt-ins. A viewer's default list is
their own: national holidays, their state's and their office's — a Chennai
employee has no use for Karnataka Rajyotsava. HR can widen it to a state or to
everything.

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
from app.models.user import User
from app.schemas.leave import (
    HolidayCreate,
    HolidayRead,
    HolidayUpdate,
    OptionalHolidaysRead,
)
from app.services import audit
from app.services.leave import service as core
from app.services.leave.holidays import holiday_applies


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


Place = tuple[uuid.UUID | None, str | None, uuid.UUID | None]


async def _place(session: AsyncSession, user: User) -> Place | None:
    """The caller's work location, its state and their legal entity, or None
    without an employee record."""
    if user.employee_id is None:
        return None
    row = (
        await session.execute(
            tenant_scoped(
                select(Employee.location_id, Location.state_code, Employee.legal_entity_id)
                .outerjoin(Location, Location.id == Employee.location_id)
                .where(Employee.id == user.employee_id),
                Employee,
                user,
            )
        )
    ).first()
    return (row[0], row[1], row[2]) if row else (None, None, None)


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
    place: Place | None,
    selected: set[uuid.UUID],
) -> HolidayRead:
    applies = place is not None and holiday_applies(
        holiday,
        state_code=place[1],
        location_id=place[0],
        legal_entity_id=place[2],
        include_optional=True,
    )
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


async def list_holidays(
    session: AsyncSession,
    user: User,
    *,
    year: int,
    scope: str = "mine",
    state_code: str | None = None,
) -> list[HolidayRead]:
    """A year's holidays.

    `mine` (the default) is the caller's own calendar; `all` is every holiday in
    the workspace; a `state_code` is that state's list with the national ones
    and those of its offices. Reference material, but it is still nobody's
    business to be shown another state's list by default.
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
    place = await _place(session, user)
    selected = await _selected_ids(session, user, year)
    rows = [
        _read(h, locations=locations, entities=entities, place=place, selected=selected)
        for h in holidays
    ]

    if state_code:
        code = state_code.upper()
        return [
            row for row in rows if row.scope == "national" or (row.state_code or "").upper() == code
        ]
    if scope == "all" or place is None:
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


async def _refuse_duplicate(
    session: AsyncSession,
    user: User,
    *,
    on: date,
    state_code: str | None,
    location_id: uuid.UUID | None,
    legal_entity_id: uuid.UUID | None = None,
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
    )
    if exclude is not None:
        stmt = stmt.where(Holiday.id != exclude)
    if await session.scalar(tenant_scoped(stmt, Holiday, user)) is not None:
        raise _conflict("That date already has a holiday for the same scope.")


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
        place=await _place(session, user),
        selected=await _selected_ids(session, user, holiday.year),
    )


async def create_holiday(
    session: AsyncSession, user: User, payload: HolidayCreate, *, request: Request | None = None
) -> HolidayRead:
    state_code = payload.state_code.upper() if payload.state_code else None
    if payload.location_id is not None:
        await _location_state(session, user, payload.location_id)
    await _check_entity(session, user, payload.legal_entity_id, payload.location_id)
    await _refuse_duplicate(
        session,
        user,
        on=payload.holiday_date,
        state_code=state_code,
        location_id=payload.location_id,
        legal_entity_id=payload.legal_entity_id,
    )
    holiday = Holiday(
        tenant_id=user.tenant_id,
        name=payload.name.strip(),
        holiday_date=payload.holiday_date,
        year=payload.holiday_date.year,
        state_code=state_code,
        location_id=payload.location_id,
        legal_entity_id=payload.legal_entity_id,
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
        summary=f"Added holiday {payload.name} on {payload.holiday_date}",
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
    await _refuse_duplicate(
        session,
        user,
        on=new_date,
        state_code=state_code,
        location_id=location_id,
        legal_entity_id=legal_entity_id,
        exclude=holiday.id,
    )

    if payload.name is not None:
        holiday.name = payload.name.strip()
    holiday.holiday_date = new_date
    holiday.year = new_date.year
    holiday.state_code = state_code
    holiday.location_id = location_id
    holiday.legal_entity_id = legal_entity_id
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
        summary=f"Edited holiday {holiday.name} on {holiday.holiday_date}",
        request=request,
    )
    await session.commit()
    await session.refresh(holiday)
    return await _view(session, user, holiday)


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
    if place is None or not holiday_applies(
        holiday,
        state_code=place[1],
        location_id=place[0],
        legal_entity_id=place[2],
        include_optional=True,
    ):
        raise _conflict(f"{holiday.name} is not offered at your work location.")
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
    "create_holiday",
    "delete_holiday",
    "list_holidays",
    "optional_holidays",
    "select_optional",
    "update_holiday",
    "withdraw_optional",
]
