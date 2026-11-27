"""Field visits: visit in and visit out, for anyone who goes out to see clients.

A sales or marketing person presses Visit in when they reach a client and
Visit out when they leave. Each visit keeps who or where it was, and the time
and location at both ends, so HR and managers can see how many visits were
made and where.

Rules:

- A location is always required, whatever the workspace's punch rule, and it
  must be as accurate as `location_accuracy_metres` when an admin sets one
  (off by default: any location). The screens keep the buttons off until such
  a fix arrives; the server refuses anything worse.
- One open visit per person (a partial unique index backs this up against a
  double tap).
- The server stamps both times. A visit does not touch attendance: it never
  changes a day's status or hours, and nobody has to punch in first.
- Visit out may be pressed only by the person on the visit.

Who sees what: the person their own visits; a manager their reporting line;
HR everyone. A visit-in photo, where an older app build sent one, the same.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, tzinfo

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import FieldVisit
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.user import User
from app.schemas.attendance import FieldVisitList, FieldVisitRead, VisitEnd, VisitStart
from app.services import audit
from app.services.attendance import addresses, punch_photos
from app.services.attendance import service as attendance_service
from app.services.attendance.review import OrgFilters, apply_org_filters, check_range
from app.services.attendance.setup import attendance_settings
from app.services.storage import get_storage

__all__ = [
    "end_visit",
    "list_visits",
    "my_visits",
    "open_visit",
    "start_visit",
    "visit_photo",
    "visits_between",
]


def _not_linked() -> HTTPException:
    return permissions.forbidden(
        "Your login is not linked to an employee record, so it cannot record visits."
    )


def _own_employee(user: User) -> uuid.UUID:
    if user.employee_id is None:
        raise _not_linked()
    return user.employee_id


async def _check_place(session: AsyncSession, user: User, accuracy_metres: float) -> int:
    """Refuse a location less accurate than the workspace asks; the accuracy as stored."""
    settings = await attendance_settings(session, user)
    attendance_service.check_accuracy(accuracy_metres, settings.location_accuracy_metres)
    return round(accuracy_metres)


async def open_visit(session: AsyncSession, user: User) -> FieldVisit | None:
    """The caller's visit that has a visit in and no visit out yet."""
    employee_id = _own_employee(user)
    return await session.scalar(
        tenant_scoped(
            select(FieldVisit).where(
                FieldVisit.employee_id == employee_id, FieldVisit.ended_at.is_(None)
            ),
            FieldVisit,
            user,
        )
    )


def _already_open(visit: FieldVisit) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"You are already on a visit to {visit.client_name}. Press Visit out there "
            "before starting the next one."
        ),
    )


async def start_visit(
    session: AsyncSession, user: User, data: VisitStart, request: Request | None = None
) -> FieldVisitRead:
    employee_id = user.employee_id
    if employee_id is None:
        raise _not_linked()
    attendance_service.refuse_fake_location(data.is_mocked)
    accuracy = await _check_place(session, user, data.accuracy_metres)
    current = await open_visit(session, user)
    if current is not None:
        raise _already_open(current)
    photo_key = (
        await punch_photos.store_photo(user, data.photo, folder="visit-photos")
        if data.photo
        else None
    )

    visit = FieldVisit(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        client_name=data.client_name,
        started_at=datetime.now(UTC),
        start_latitude=data.latitude,
        start_longitude=data.longitude,
        start_accuracy_metres=accuracy,
        start_source=data.source,
        start_photo_key=photo_key,
    )
    session.add(visit)
    try:
        await session.flush()
    except IntegrityError as exc:
        # A second tap that raced the first: the partial unique index caught it.
        await session.rollback()
        if photo_key:
            await get_storage().delete(photo_key)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a visit open. Press Visit out before starting another.",
        ) from exc
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="field_visit",
        entity_id=visit.id,
        actor=user,
        changes={
            "client_name": visit.client_name,
            "source": data.source,
            "accuracy_metres": accuracy,
        },
        summary=f"Visit in at {visit.client_name}",
        request=request,
    )
    await session.commit()
    await session.refresh(visit)
    return (await _read(session, user, [visit]))[0]


async def end_visit(
    session: AsyncSession,
    user: User,
    visit_id: uuid.UUID,
    data: VisitEnd,
    request: Request | None = None,
) -> FieldVisitRead:
    employee_id = _own_employee(user)
    visit = await session.scalar(
        tenant_scoped(
            select(FieldVisit).where(FieldVisit.id == visit_id).with_for_update(),
            FieldVisit,
            user,
        )
    )
    if visit is None or visit.employee_id != employee_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Visit not found.")
    if visit.ended_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You already pressed Visit out at {visit.client_name}.",
        )
    attendance_service.refuse_fake_location(data.is_mocked)
    accuracy = await _check_place(session, user, data.accuracy_metres)
    visit.ended_at = datetime.now(UTC)
    visit.end_latitude = data.latitude  # type: ignore[assignment]
    visit.end_longitude = data.longitude  # type: ignore[assignment]
    visit.end_accuracy_metres = accuracy
    visit.end_source = data.source
    minutes = int((visit.ended_at - visit.started_at).total_seconds() // 60)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="field_visit",
        entity_id=visit.id,
        actor=user,
        changes={
            "client_name": visit.client_name,
            "source": data.source,
            "accuracy_metres": accuracy,
            "duration_minutes": minutes,
        },
        summary=f"Visit out at {visit.client_name} after {minutes} min",
        request=request,
    )
    await session.commit()
    await session.refresh(visit)
    return (await _read(session, user, [visit]))[0]


# ── Reading ─────────────────────────────────────────────────────────────────


def _utc_bounds(start: date, end: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """From the start of `start` to the end of `end`, in the workspace's time."""
    low = datetime.combine(start, datetime.min.time(), zone).astimezone(UTC)
    high = datetime.combine(end + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
    return low, high


async def visits_between(
    session: AsyncSession,
    user: User,
    *,
    start: date,
    end: date,
    employee_ids: list[uuid.UUID] | None,
) -> list[FieldVisit]:
    """Visits that began between the two dates (workspace time), oldest first.

    `employee_ids` None means everybody this caller may see; the caller has
    already narrowed it to them.
    """
    zone = await attendance_service.tenant_timezone(session, user)
    low, high = _utc_bounds(start, end, zone)
    stmt = select(FieldVisit).where(FieldVisit.started_at >= low, FieldVisit.started_at < high)
    if employee_ids is not None:
        stmt = stmt.where(FieldVisit.employee_id.in_(employee_ids or [uuid.UUID(int=0)]))
    stmt = stmt.order_by(FieldVisit.started_at)
    return list((await session.scalars(tenant_scoped(stmt, FieldVisit, user))).all())


async def _read(
    session: AsyncSession, user: User, visits: list[FieldVisit], *, lookups: int = 60
) -> list[FieldVisitRead]:
    ids = {v.employee_id for v in visits}
    people: dict[uuid.UUID, Employee] = {}
    if ids:
        rows = await session.scalars(
            tenant_scoped(select(Employee).where(Employee.id.in_(ids)), Employee, user)
        )
        people = {e.id: e for e in rows.all()}
    places = await addresses.addresses_for(
        session,
        user,
        [(v.start_latitude, v.start_longitude) for v in visits]
        + [(v.end_latitude, v.end_longitude) for v in visits if v.end_latitude is not None],
        budget_seconds=4.0,
        max_lookups=lookups,
    )
    # Addresses found now are kept for next time.
    await session.commit()

    def address(lat: object, lng: object) -> str | None:
        place = addresses.place_of(lat, lng)
        return places.get(place) if place else None

    out: list[FieldVisitRead] = []
    for v in visits:
        person = people.get(v.employee_id)
        out.append(
            FieldVisitRead(
                id=v.id,
                employee_id=v.employee_id,
                employee_code=person.employee_code if person else None,
                employee_name=person.full_name if person else None,
                client_name=v.client_name,
                started_at=v.started_at,
                start_latitude=float(v.start_latitude),
                start_longitude=float(v.start_longitude),
                start_accuracy_metres=v.start_accuracy_metres,
                start_source=v.start_source,
                start_address=address(v.start_latitude, v.start_longitude),
                has_photo=v.has_photo,
                ended_at=v.ended_at,
                end_latitude=float(v.end_latitude) if v.end_latitude is not None else None,
                end_longitude=float(v.end_longitude) if v.end_longitude is not None else None,
                end_accuracy_metres=v.end_accuracy_metres,
                end_source=v.end_source,
                end_address=address(v.end_latitude, v.end_longitude),
                duration_minutes=(
                    int((v.ended_at - v.started_at).total_seconds() // 60)
                    if v.ended_at is not None
                    else None
                ),
            )
        )
    return out


def _list(rows: list[FieldVisitRead]) -> FieldVisitList:
    return FieldVisitList(visits=rows, total=len(rows), people=len({r.employee_id for r in rows}))


async def my_visits(session: AsyncSession, user: User, *, start: date, end: date) -> FieldVisitList:
    employee_id = user.employee_id
    if employee_id is None:
        raise _not_linked()
    check_range(start, end)
    visits = await visits_between(session, user, start=start, end=end, employee_ids=[employee_id])
    return _list(await _read(session, user, visits))


async def list_visits(
    session: AsyncSession,
    user: User,
    *,
    start: date,
    end: date,
    filters: OrgFilters,
    employee_id: uuid.UUID | None = None,
) -> FieldVisitList:
    """Visits for HR (everyone) or a manager (their reporting line and themselves)."""
    if not permissions.is_hr(user) and not user.has_reports:
        raise permissions.forbidden("Only HR and managers see other people's visits.")
    check_range(start, end)
    stmt = select(Employee.id).where(Employee.deleted_at.is_(None))
    stmt = apply_org_filters(stmt, filters)
    visible = await permissions.visible_employee_ids(session, user)
    if visible is not None:
        stmt = stmt.where(Employee.id.in_(visible or [uuid.UUID(int=0)]))
    if employee_id is not None:
        stmt = stmt.where(Employee.id == employee_id)
    ids = list((await session.scalars(tenant_scoped(stmt, Employee, user))).all())
    visits = await visits_between(session, user, start=start, end=end, employee_ids=ids)
    return _list(await _read(session, user, visits, lookups=200))


async def visit_photo(session: AsyncSession, user: User, visit_id: uuid.UUID) -> tuple[bytes, str]:
    """The visit-in selfie, for the person, their reporting line or HR."""
    visit = await session.scalar(
        tenant_scoped(select(FieldVisit).where(FieldVisit.id == visit_id), FieldVisit, user)
    )
    if visit is None or not visit.start_photo_key:
        raise HTTPException(status_code=404, detail="No photo with this visit.")
    if user.employee_id != visit.employee_id:
        await permissions.require_manager_or_hr(
            session,
            user,
            visit.employee_id,
            detail="A visit photo is for the person, their manager and HR.",
        )
    data = await get_storage().get(visit.start_photo_key)
    if data is None:
        raise HTTPException(status_code=404, detail="The photo is missing from storage.")
    media = "image/png" if visit.start_photo_key.endswith(".png") else "image/jpeg"
    return data, media
