"""1:1 meetings: scheduling, the shared agenda, action items and private notes.

Who is in a 1:1 is decided once, when it is scheduled: the caller and either
somebody in their reporting line (the caller is the manager) or their own
manager (the caller is the report). After that every read and write goes
through `_participant_meeting`, which answers "not found" to anybody who is not
one of the two — HR included. A 1:1 is the one conversation at work that only
works if it stays between the people in it.

Private notes are stored per author and only ever returned to their author.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.notifications import Channel, DigestMode
from app.models.one_on_one import (
    OneOnOne,
    OneOnOneItem,
    OneOnOneItemKind,
    OneOnOneNote,
    OneOnOneStatus,
)
from app.models.user import User
from app.schemas.one_on_one import (
    OneOnOneCancel,
    OneOnOneComplete,
    OneOnOneCreate,
    OneOnOneDetail,
    OneOnOneItemCreate,
    OneOnOneItemRead,
    OneOnOneItemUpdate,
    OneOnOneNoteWrite,
    OneOnOnePartner,
    OneOnOneRead,
    OneOnOneUpdate,
    ReportOneOnOneSummary,
)
from app.services import audit
from app.services.notifications.dispatch import notify, register_event
from app.services.performance import notices

__all__ = [
    "add_item",
    "cancel",
    "complete",
    "delete_item",
    "get_detail",
    "list_meetings",
    "partners",
    "reports_summary",
    "reschedule",
    "schedule",
    "update_item",
    "write_note",
]

_PROMPT = {Channel.IN_APP: DigestMode.IMMEDIATE, Channel.EMAIL: DigestMode.IMMEDIATE}

register_event(
    "performance.one_on_one.scheduled",
    "1:1 with {actor_name} on {when}",
    "{actor_name} scheduled a 1:1 with you on {when}. Add your talking points before it.",
    preferences=_PROMPT,
)
register_event(
    "performance.one_on_one.rescheduled",
    "Your 1:1 with {actor_name} moved to {when}",
    "{actor_name} moved your 1:1 to {when}.",
    preferences=_PROMPT,
)
register_event(
    "performance.one_on_one.cancelled",
    "Your 1:1 with {actor_name} on {when} is cancelled",
    "{actor_name} cancelled the 1:1 on {when}. {reason}",
    preferences=_PROMPT,
)

_NOT_FOUND = "1:1 not found"


def _not_found(detail: str = _NOT_FOUND) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _when(day: date, at: time | None) -> str:
    text = f"{day:%a %d %b %Y}"
    return f"{text}, {at:%H:%M}" if at is not None else text


# ── Loading ─────────────────────────────────────────────────────────────────


async def _participant_meeting(
    session: AsyncSession, user: User, meeting_id: uuid.UUID
) -> tuple[OneOnOne, str]:
    """The meeting and the caller's side of it — or 404 for anybody else.

    Not 403: a 1:1 somebody is not in is not theirs to know exists.
    """
    meeting = await session.scalar(
        tenant_scoped(select(OneOnOne).where(OneOnOne.id == meeting_id), OneOnOne, user)
    )
    if meeting is None or user.employee_id is None:
        raise _not_found()
    if meeting.manager_employee_id == user.employee_id:
        return meeting, "manager"
    if meeting.report_employee_id == user.employee_id:
        return meeting, "report"
    raise _not_found()


def _other(meeting: OneOnOne, side: str) -> uuid.UUID:
    return meeting.report_employee_id if side == "manager" else meeting.manager_employee_id


async def _names(
    session: AsyncSession, user: User, employee_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not employee_ids:
        return {}
    rows = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.id.in_(employee_ids)), Employee, user)
        )
    ).all()
    return {row.id: row.full_name for row in rows}


async def _user_names(
    session: AsyncSession, user: User, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(select(User.id, User.full_name).where(User.id.in_(user_ids)), User, user)
        )
    ).all()
    return {uid: name for uid, name in rows}


async def _item_counts(
    session: AsyncSession, user: User, meeting_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """(talking points, open action items) per meeting."""
    if not meeting_ids:
        return {}
    counts: dict[uuid.UUID, list[int]] = {mid: [0, 0] for mid in meeting_ids}
    rows = (
        await session.execute(
            tenant_scoped(
                select(OneOnOneItem.one_on_one_id, OneOnOneItem.kind, OneOnOneItem.done_at).where(
                    OneOnOneItem.one_on_one_id.in_(meeting_ids)
                ),
                OneOnOneItem,
                user,
            )
        )
    ).all()
    for meeting_id, kind, done_at in rows:
        if kind is OneOnOneItemKind.TALKING_POINT:
            counts[meeting_id][0] += 1
        elif done_at is None:
            counts[meeting_id][1] += 1
    return {mid: (c[0], c[1]) for mid, c in counts.items()}


def _read(
    meeting: OneOnOne,
    user: User,
    names: dict[uuid.UUID, str],
    counts: dict[uuid.UUID, tuple[int, int]],
) -> dict[str, Any]:
    side = "manager" if meeting.manager_employee_id == user.employee_id else "report"
    other = _other(meeting, side)
    talking, open_actions = counts.get(meeting.id, (0, 0))
    return {
        "id": meeting.id,
        "manager_employee_id": meeting.manager_employee_id,
        "manager_name": names.get(meeting.manager_employee_id, "Unknown"),
        "report_employee_id": meeting.report_employee_id,
        "report_name": names.get(meeting.report_employee_id, "Unknown"),
        "with_employee_id": other,
        "with_name": names.get(other, "Unknown"),
        "my_side": side,
        "scheduled_on": meeting.scheduled_on,
        "start_time": meeting.start_time,
        "duration_minutes": meeting.duration_minutes,
        "status": meeting.status,
        "summary": meeting.summary,
        "completed_at": meeting.completed_at,
        "cancelled_at": meeting.cancelled_at,
        "cancel_reason": meeting.cancel_reason,
        "talking_points": talking,
        "open_action_items": open_actions,
        "created_at": meeting.created_at,
    }


def _item_read(
    item: OneOnOneItem,
    user: User,
    names: dict[uuid.UUID, str],
    user_names: dict[uuid.UUID, str],
    *,
    from_meeting_on: date | None = None,
) -> OneOnOneItemRead:
    return OneOnOneItemRead(
        id=item.id,
        one_on_one_id=item.one_on_one_id,
        kind=item.kind,
        body=item.body,
        added_by_name=user_names.get(item.added_by_user_id) if item.added_by_user_id else None,
        is_mine=item.added_by_user_id == user.id,
        owner_employee_id=item.owner_employee_id,
        owner_name=names.get(item.owner_employee_id) if item.owner_employee_id else None,
        due_on=item.due_on,
        done_at=item.done_at,
        from_meeting_on=from_meeting_on,
    )


async def _other_user_ids(
    session: AsyncSession, user: User, meeting: OneOnOne, side: str
) -> list[uuid.UUID]:
    found = await notices.user_ids_for_employees(session, user, [_other(meeting, side)])
    return list(found.values())


# ── Scheduling ──────────────────────────────────────────────────────────────


async def schedule(
    session: AsyncSession, user: User, payload: OneOnOneCreate, *, request: Request | None = None
) -> OneOnOneDetail:
    """Schedule a 1:1 with somebody in your line, or with your own manager."""
    me = permissions.own_employee_id(user)
    if payload.with_employee_id == me:
        raise _unprocessable("A 1:1 needs somebody else in it.")
    other = await session.scalar(
        tenant_scoped(
            select(Employee).where(
                Employee.id == payload.with_employee_id,
                Employee.deleted_at.is_(None),
                Employee.status != EmploymentStatus.EXITED,
            ),
            Employee,
            user,
        )
    )
    if other is None:
        raise _not_found("Employee not found")
    mine = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == me), Employee, user)
    )
    if other.id in await permissions.reporting_line(session, user):
        manager_id, report_id = me, other.id
    elif mine is not None and mine.manager_id == other.id:
        manager_id, report_id = other.id, me
    else:
        raise permissions.forbidden(
            "A 1:1 is between you and somebody in your reporting line, or your own manager."
        )

    meeting = OneOnOne(
        tenant_id=user.tenant_id,
        manager_employee_id=manager_id,
        report_employee_id=report_id,
        scheduled_on=payload.scheduled_on,
        start_time=payload.start_time,
        duration_minutes=payload.duration_minutes,
        status=OneOnOneStatus.SCHEDULED,
        created_by_user_id=user.id,
    )
    session.add(meeting)
    await session.flush()
    agenda = [line.strip() for line in payload.agenda if line.strip()]
    for index, line in enumerate(agenda):
        session.add(
            OneOnOneItem(
                tenant_id=user.tenant_id,
                one_on_one_id=meeting.id,
                kind=OneOnOneItemKind.TALKING_POINT,
                body=line[:2000],
                added_by_user_id=user.id,
                display_order=index,
            )
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="one_on_one",
        entity_id=meeting.id,
        actor=user,
        changes={
            "manager_employee_id": str(manager_id),
            "report_employee_id": str(report_id),
            "scheduled_on": meeting.scheduled_on.isoformat(),
            "agenda_items": len(agenda),
        },
        summary=f"Scheduled a 1:1 with {other.full_name} on {meeting.scheduled_on:%d %b %Y}",
        request=request,
    )
    side = "manager" if manager_id == me else "report"
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key="performance.one_on_one.scheduled",
        recipient_user_ids=await _other_user_ids(session, user, meeting, side),
        payload={
            "actor_name": user.full_name,
            "when": _when(meeting.scheduled_on, meeting.start_time),
        },
        commit=False,
    )
    await session.commit()
    return await get_detail(session, user, meeting.id)


async def reschedule(
    session: AsyncSession,
    user: User,
    meeting_id: uuid.UUID,
    payload: OneOnOneUpdate,
    *,
    request: Request | None = None,
) -> OneOnOneDetail:
    meeting, side = await _participant_meeting(session, user, meeting_id)
    if meeting.status is not OneOnOneStatus.SCHEDULED:
        raise _conflict(f"This 1:1 is {meeting.status.value}; only a scheduled one can move.")
    changes: dict[str, Any] = {}
    if payload.scheduled_on is not None and payload.scheduled_on != meeting.scheduled_on:
        changes["scheduled_on"] = {
            "from": meeting.scheduled_on.isoformat(),
            "to": payload.scheduled_on.isoformat(),
        }
        meeting.scheduled_on = payload.scheduled_on
    if payload.clear_start_time and meeting.start_time is not None:
        changes["start_time"] = {"from": meeting.start_time.isoformat(), "to": None}
        meeting.start_time = None
    elif payload.start_time is not None and payload.start_time != meeting.start_time:
        changes["start_time"] = {
            "from": meeting.start_time.isoformat() if meeting.start_time else None,
            "to": payload.start_time.isoformat(),
        }
        meeting.start_time = payload.start_time
    duration = payload.duration_minutes
    if duration is not None and duration != meeting.duration_minutes:
        changes["duration_minutes"] = {
            "from": meeting.duration_minutes,
            "to": payload.duration_minutes,
        }
        meeting.duration_minutes = payload.duration_minutes
    if not changes:
        return await get_detail(session, user, meeting.id)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="one_on_one",
        entity_id=meeting.id,
        actor=user,
        changes=changes,
        summary=f"Rescheduled a 1:1 to {meeting.scheduled_on:%d %b %Y}",
        request=request,
    )
    if "scheduled_on" in changes or "start_time" in changes:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.one_on_one.rescheduled",
            recipient_user_ids=await _other_user_ids(session, user, meeting, side),
            payload={
                "actor_name": user.full_name,
                "when": _when(meeting.scheduled_on, meeting.start_time),
            },
            commit=False,
        )
    await session.commit()
    return await get_detail(session, user, meeting.id)


# ── Reading ─────────────────────────────────────────────────────────────────


async def list_meetings(
    session: AsyncSession,
    user: User,
    *,
    meeting_status: OneOnOneStatus | None = None,
    with_employee_id: uuid.UUID | None = None,
) -> list[OneOnOneRead]:
    """The caller's own 1:1s, from either side. Newest first."""
    if user.employee_id is None:
        return []
    me = user.employee_id
    stmt = tenant_scoped(
        select(OneOnOne).where(
            or_(OneOnOne.manager_employee_id == me, OneOnOne.report_employee_id == me)
        ),
        OneOnOne,
        user,
    ).order_by(OneOnOne.scheduled_on.desc(), OneOnOne.created_at.desc())
    if meeting_status is not None:
        stmt = stmt.where(OneOnOne.status == meeting_status)
    if with_employee_id is not None:
        stmt = stmt.where(
            or_(
                OneOnOne.manager_employee_id == with_employee_id,
                OneOnOne.report_employee_id == with_employee_id,
            )
        )
    meetings = list((await session.scalars(stmt)).all())
    names = await _names(
        session,
        user,
        {m.manager_employee_id for m in meetings} | {m.report_employee_id for m in meetings},
    )
    counts = await _item_counts(session, user, [m.id for m in meetings])
    return [OneOnOneRead(**_read(m, user, names, counts)) for m in meetings]


async def get_detail(session: AsyncSession, user: User, meeting_id: uuid.UUID) -> OneOnOneDetail:
    meeting, _side = await _participant_meeting(session, user, meeting_id)
    items = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(OneOnOneItem)
                    .where(OneOnOneItem.one_on_one_id == meeting.id)
                    .order_by(OneOnOneItem.display_order, OneOnOneItem.created_at),
                    OneOnOneItem,
                    user,
                )
            )
        ).all()
    )
    # Open action items from earlier meetings between the same two people.
    carried = (
        await session.execute(
            tenant_scoped(
                select(OneOnOneItem, OneOnOne.scheduled_on)
                .join(OneOnOne, OneOnOne.id == OneOnOneItem.one_on_one_id)
                .where(
                    OneOnOne.manager_employee_id == meeting.manager_employee_id,
                    OneOnOne.report_employee_id == meeting.report_employee_id,
                    OneOnOne.id != meeting.id,
                    OneOnOne.status != OneOnOneStatus.CANCELLED,
                    OneOnOne.scheduled_on <= meeting.scheduled_on,
                    OneOnOneItem.kind == OneOnOneItemKind.ACTION_ITEM,
                    OneOnOneItem.done_at.is_(None),
                )
                .order_by(OneOnOne.scheduled_on, OneOnOneItem.created_at),
                OneOnOneItem,
                user,
            )
        )
    ).all()
    note = await session.scalar(
        tenant_scoped(
            select(OneOnOneNote).where(
                OneOnOneNote.one_on_one_id == meeting.id,
                OneOnOneNote.author_user_id == user.id,
            ),
            OneOnOneNote,
            user,
        )
    )
    all_items = [*items, *(row for row, _ in carried)]
    names = await _names(
        session,
        user,
        {meeting.manager_employee_id, meeting.report_employee_id}
        | {i.owner_employee_id for i in all_items if i.owner_employee_id},
    )
    user_names = await _user_names(
        session, user, {i.added_by_user_id for i in all_items if i.added_by_user_id}
    )
    counts = await _item_counts(session, user, [meeting.id])
    return OneOnOneDetail(
        **_read(meeting, user, names, counts),
        items=[_item_read(i, user, names, user_names) for i in items],
        carried_over=[
            _item_read(i, user, names, user_names, from_meeting_on=held_on)
            for i, held_on in carried
        ],
        my_note=note.body if note else None,
        my_note_updated_at=note.updated_at if note else None,
    )


async def partners(session: AsyncSession, user: User) -> list[OneOnOnePartner]:
    """Who the caller can schedule a 1:1 with: their own manager and direct reports."""
    if user.employee_id is None:
        return []
    me = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == user.employee_id), Employee, user)
    )
    if me is None:
        return []
    conditions = [Employee.manager_id == me.id]
    if me.manager_id is not None:
        conditions.append(Employee.id == me.manager_id)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    or_(*conditions),
                    Employee.id != me.id,
                    Employee.deleted_at.is_(None),
                    Employee.status != EmploymentStatus.EXITED,
                ),
                Employee,
                user,
            ).order_by(Employee.first_name, Employee.last_name)
        )
    ).all()
    out = [
        OneOnOnePartner(
            employee_id=row.id,
            name=row.full_name,
            employee_code=row.employee_code,
            designation=row.designation,
            relation="manager" if row.id == me.manager_id else "report",
        )
        for row in rows
    ]
    return sorted(out, key=lambda p: p.relation != "manager")


async def reports_summary(session: AsyncSession, user: User) -> list[ReportOneOnOneSummary]:
    """Each of the caller's direct reports, with where their 1:1s stand."""
    if user.employee_id is None:
        return []
    me = user.employee_id
    reports = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(
                        Employee.manager_id == me,
                        Employee.id != me,
                        Employee.deleted_at.is_(None),
                        Employee.status != EmploymentStatus.EXITED,
                    ),
                    Employee,
                    user,
                ).order_by(Employee.first_name, Employee.last_name)
            )
        ).all()
    )
    if not reports:
        return []
    report_ids = [r.id for r in reports]
    meetings = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(OneOnOne).where(
                        OneOnOne.manager_employee_id == me,
                        OneOnOne.report_employee_id.in_(report_ids),
                    ),
                    OneOnOne,
                    user,
                )
            )
        ).all()
    )
    open_items = dict(
        (
            await session.execute(
                tenant_scoped(
                    select(OneOnOne.report_employee_id, func.count(OneOnOneItem.id))
                    .join(OneOnOne, OneOnOne.id == OneOnOneItem.one_on_one_id)
                    .where(
                        OneOnOne.manager_employee_id == me,
                        OneOnOne.report_employee_id.in_(report_ids),
                        OneOnOne.status != OneOnOneStatus.CANCELLED,
                        OneOnOneItem.kind == OneOnOneItemKind.ACTION_ITEM,
                        OneOnOneItem.done_at.is_(None),
                    )
                    .group_by(OneOnOne.report_employee_id),
                    OneOnOneItem,
                    user,
                )
            )
        ).all()
    )
    out: list[ReportOneOnOneSummary] = []
    for report in reports:
        theirs = [m for m in meetings if m.report_employee_id == report.id]
        held = [m for m in theirs if m.status is OneOnOneStatus.COMPLETED]
        upcoming = sorted(
            (m for m in theirs if m.status is OneOnOneStatus.SCHEDULED),
            key=lambda m: m.scheduled_on,
        )
        out.append(
            ReportOneOnOneSummary(
                employee_id=report.id,
                name=report.full_name,
                employee_code=report.employee_code,
                designation=report.designation,
                last_held_on=max((m.scheduled_on for m in held), default=None),
                next_scheduled_on=upcoming[0].scheduled_on if upcoming else None,
                next_one_on_one_id=upcoming[0].id if upcoming else None,
                held_count=len(held),
                open_action_items=int(open_items.get(report.id, 0)),
            )
        )
    return out


# ── The agenda and action items ─────────────────────────────────────────────


async def add_item(
    session: AsyncSession,
    user: User,
    meeting_id: uuid.UUID,
    payload: OneOnOneItemCreate,
    *,
    request: Request | None = None,
) -> OneOnOneDetail:
    """Either participant adds a talking point or an action item."""
    meeting, _side = await _participant_meeting(session, user, meeting_id)
    if meeting.status is not OneOnOneStatus.SCHEDULED:
        raise _conflict(f"This 1:1 is {meeting.status.value}; its agenda is closed.")
    owner: uuid.UUID | None = None
    if payload.kind is OneOnOneItemKind.ACTION_ITEM:
        owner = payload.owner_employee_id or user.employee_id
        if owner not in (meeting.manager_employee_id, meeting.report_employee_id):
            raise _unprocessable("An action item belongs to one of the two people in the 1:1.")
    elif payload.owner_employee_id is not None or payload.due_on is not None:
        raise _unprocessable("Only an action item has an owner and a due date.")
    position = await session.scalar(
        tenant_scoped(
            select(func.count(OneOnOneItem.id)).where(OneOnOneItem.one_on_one_id == meeting.id),
            OneOnOneItem,
            user,
        )
    )
    item = OneOnOneItem(
        tenant_id=user.tenant_id,
        one_on_one_id=meeting.id,
        kind=payload.kind,
        body=payload.body.strip(),
        added_by_user_id=user.id,
        owner_employee_id=owner,
        due_on=payload.due_on if payload.kind is OneOnOneItemKind.ACTION_ITEM else None,
        display_order=int(position or 0),
    )
    session.add(item)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="one_on_one_item",
        entity_id=item.id,
        actor=user,
        changes={"one_on_one_id": str(meeting.id), "kind": payload.kind.value},
        summary=f"Added a {payload.kind.value.replace('_', ' ')} to a 1:1",
        request=request,
    )
    await session.commit()
    return await get_detail(session, user, meeting.id)


async def _participant_item(
    session: AsyncSession, user: User, item_id: uuid.UUID
) -> tuple[OneOnOneItem, OneOnOne]:
    item = await session.scalar(
        tenant_scoped(select(OneOnOneItem).where(OneOnOneItem.id == item_id), OneOnOneItem, user)
    )
    if item is None:
        raise _not_found("Item not found")
    meeting, _side = await _participant_meeting(session, user, item.one_on_one_id)
    return item, meeting


async def update_item(
    session: AsyncSession,
    user: User,
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: OneOnOneItemUpdate,
    *,
    request: Request | None = None,
) -> OneOnOneDetail:
    """Tick an action item done (either participant), or edit your own item.

    An action item can be ticked from a later meeting — that is what carrying
    it over is for — so `meeting_id` is where the caller is looking, and the
    item may belong to an earlier meeting between the same two people.
    """
    viewing, _side = await _participant_meeting(session, user, meeting_id)
    item, meeting = await _participant_item(session, user, item_id)
    if (meeting.manager_employee_id, meeting.report_employee_id) != (
        viewing.manager_employee_id,
        viewing.report_employee_id,
    ):
        raise _not_found("Item not found")
    changes: dict[str, Any] = {}
    if payload.done is not None:
        if item.kind is not OneOnOneItemKind.ACTION_ITEM:
            raise _unprocessable("Only an action item is ticked off.")
        if payload.done and item.done_at is None:
            item.done_at = datetime.now(UTC)
            item.done_by_user_id = user.id
            changes["done"] = True
        elif not payload.done and item.done_at is not None:
            item.done_at = None
            item.done_by_user_id = None
            changes["done"] = False
    edits = {
        "body": payload.body.strip() if payload.body is not None else None,
        "owner_employee_id": payload.owner_employee_id,
        "due_on": payload.due_on,
    }
    if any(value is not None for value in edits.values()):
        if item.added_by_user_id != user.id:
            raise permissions.forbidden("Only whoever added an item can change its wording.")
        if meeting.status is OneOnOneStatus.CANCELLED:
            raise _conflict("This 1:1 was cancelled.")
        if edits["body"] is not None and edits["body"] != item.body:
            changes["body"] = True
            item.body = edits["body"]
        if item.kind is OneOnOneItemKind.ACTION_ITEM:
            if payload.owner_employee_id is not None:
                if payload.owner_employee_id not in (
                    meeting.manager_employee_id,
                    meeting.report_employee_id,
                ):
                    raise _unprocessable(
                        "An action item belongs to one of the two people in the 1:1."
                    )
                changes["owner_employee_id"] = str(payload.owner_employee_id)
                item.owner_employee_id = payload.owner_employee_id
            if payload.due_on is not None:
                changes["due_on"] = payload.due_on.isoformat()
                item.due_on = payload.due_on
        elif payload.owner_employee_id is not None or payload.due_on is not None:
            raise _unprocessable("Only an action item has an owner and a due date.")
    if changes:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="one_on_one_item",
            entity_id=item.id,
            actor=user,
            changes=changes,
            summary="Updated a 1:1 item",
            request=request,
        )
        await session.commit()
    return await get_detail(session, user, viewing.id)


async def delete_item(
    session: AsyncSession,
    user: User,
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> None:
    """Withdraw your own item from a meeting that has not happened yet."""
    item, meeting = await _participant_item(session, user, item_id)
    if meeting.id != meeting_id:
        raise _not_found("Item not found")
    if item.added_by_user_id != user.id:
        raise permissions.forbidden("Only whoever added an item can remove it.")
    if meeting.status is not OneOnOneStatus.SCHEDULED:
        raise _conflict("Items stay on a 1:1 once it has been held or cancelled.")
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="one_on_one_item",
        entity_id=item.id,
        actor=user,
        changes={"one_on_one_id": str(meeting.id), "kind": item.kind.value},
        summary="Removed an item from a 1:1",
        request=request,
    )
    await session.delete(item)
    await session.commit()


# ── Notes and outcome ───────────────────────────────────────────────────────


async def write_note(
    session: AsyncSession,
    user: User,
    meeting_id: uuid.UUID,
    payload: OneOnOneNoteWrite,
    *,
    request: Request | None = None,
) -> OneOnOneDetail:
    """Save the caller's private note. Keyed by `user.id`; nobody else reads it."""
    meeting, _side = await _participant_meeting(session, user, meeting_id)
    note = await session.scalar(
        tenant_scoped(
            select(OneOnOneNote).where(
                OneOnOneNote.one_on_one_id == meeting.id,
                OneOnOneNote.author_user_id == user.id,
            ),
            OneOnOneNote,
            user,
        )
    )
    body = payload.body.strip()
    if note is None:
        if not body:
            return await get_detail(session, user, meeting.id)
        note = OneOnOneNote(
            tenant_id=user.tenant_id,
            one_on_one_id=meeting.id,
            author_user_id=user.id,
            body=body,
        )
        session.add(note)
    else:
        note.body = body
    await session.flush()
    # The note's text is private, so the audit row records that it changed and
    # nothing of what it says.
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="one_on_one_private_note",
        entity_id=note.id,
        actor=user,
        changes={"one_on_one_id": str(meeting.id), "length": len(body)},
        summary="Saved private notes on a 1:1",
        request=request,
    )
    await session.commit()
    return await get_detail(session, user, meeting.id)


async def complete(
    session: AsyncSession,
    user: User,
    meeting_id: uuid.UUID,
    payload: OneOnOneComplete,
    *,
    request: Request | None = None,
) -> OneOnOneDetail:
    meeting, _side = await _participant_meeting(session, user, meeting_id)
    if meeting.status is not OneOnOneStatus.SCHEDULED:
        raise _conflict(f"This 1:1 is already {meeting.status.value}.")
    meeting.status = OneOnOneStatus.COMPLETED
    meeting.completed_at = datetime.now(UTC)
    meeting.completed_by_user_id = user.id
    meeting.summary = (payload.summary or "").strip() or None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="one_on_one",
        entity_id=meeting.id,
        actor=user,
        changes={"status": {"from": "scheduled", "to": "completed"}},
        summary=f"Marked the 1:1 on {meeting.scheduled_on:%d %b %Y} as held",
        request=request,
    )
    await session.commit()
    return await get_detail(session, user, meeting.id)


async def cancel(
    session: AsyncSession,
    user: User,
    meeting_id: uuid.UUID,
    payload: OneOnOneCancel,
    *,
    request: Request | None = None,
) -> OneOnOneDetail:
    meeting, side = await _participant_meeting(session, user, meeting_id)
    if meeting.status is not OneOnOneStatus.SCHEDULED:
        raise _conflict(f"This 1:1 is already {meeting.status.value}.")
    meeting.status = OneOnOneStatus.CANCELLED
    meeting.cancelled_at = datetime.now(UTC)
    meeting.cancel_reason = (payload.reason or "").strip() or None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="one_on_one",
        entity_id=meeting.id,
        actor=user,
        changes={"status": {"from": "scheduled", "to": "cancelled"}},
        summary=f"Cancelled the 1:1 on {meeting.scheduled_on:%d %b %Y}",
        request=request,
    )
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key="performance.one_on_one.cancelled",
        recipient_user_ids=await _other_user_ids(session, user, meeting, side),
        payload={
            "actor_name": user.full_name,
            "when": _when(meeting.scheduled_on, meeting.start_time),
            "reason": meeting.cancel_reason or "",
        },
        commit=False,
    )
    await session.commit()
    return await get_detail(session, user, meeting.id)
