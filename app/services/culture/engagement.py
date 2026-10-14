"""Polls and company events: asked by HR, answered by the people in the audience.

Polls: one vote each, changeable while the poll is open. Results are counts;
an anonymous poll never returns who voted. Results are shown to a voter after
they vote, and to everyone once the poll closes. With fewer than
`MIN_RESPONSES` answers the per-option split is withheld from everyone but
HR on a named poll — with three answers, a split can name a voter.

Events: going / maybe / not going, changeable until answers close. A full
event refuses another "going" rather than over-booking; the list of who is
coming is for HR and the person who set the event up.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.engagement import EventRsvp, Poll, PollVote, RsvpResponse, TeamEvent
from app.models.user import User
from app.schemas.engagement import AudienceIn, EventCreate, PollCreate
from app.services import audit
from app.services.culture import comms as comms_engine

__all__ = [
    "MIN_RESPONSES",
    "cancel_event",
    "close_poll",
    "create_event",
    "create_poll",
    "list_events",
    "list_polls",
    "rsvp",
    "vote",
]

MIN_RESPONSES = 3


def _audience_json(audience: AudienceIn) -> dict[str, Any]:
    return {
        "department_ids": [str(v) for v in audience.department_ids],
        "location_ids": [str(v) for v in audience.location_ids],
        "grades": list(audience.grades),
    }


def _in_audience(spec: dict | None, employee: Employee | None, *, hr: bool) -> bool:
    audience = comms_engine.Audience(
        department_ids=tuple((spec or {}).get("department_ids", [])),
        location_ids=tuple((spec or {}).get("location_ids", [])),
        grades=tuple((spec or {}).get("grades", [])),
    )
    if hr or audience.is_everyone:
        return True
    if employee is None:
        return False
    return comms_engine.is_visible_to(
        audience,
        comms_engine.Recipient(
            employee_id=str(employee.id),
            department_id=str(employee.department_id) if employee.department_id else None,
            location_id=str(employee.location_id) if employee.location_id else None,
            grade=employee.grade,
        ),
    )


async def _me(session: AsyncSession, user: User) -> Employee | None:
    if user.employee_id is None:
        return None
    return await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == user.employee_id), Employee, user)
    )


def _require_hr(user: User) -> None:
    if not permissions.is_hr(user):
        raise permissions.forbidden("Polls and events are set up by HR.")


# ── Polls ───────────────────────────────────────────────────────────────────


def _is_open(poll: Poll, now: datetime) -> bool:
    return poll.closed_at is None and (poll.closes_at is None or poll.closes_at > now)


async def create_poll(
    session: AsyncSession, user: User, payload: PollCreate, *, request: Request | None
) -> dict[str, Any]:
    _require_hr(user)
    poll = Poll(
        tenant_id=user.tenant_id,
        question=payload.question.strip(),
        options=payload.options,
        audience=_audience_json(payload.audience),
        is_anonymous=payload.is_anonymous,
        closes_at=payload.closes_at,
        created_by_user_id=user.id,
    )
    session.add(poll)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="poll",
        entity_id=poll.id,
        actor=user,
        changes={
            "question": poll.question,
            "options": poll.options,
            "anonymous": poll.is_anonymous,
        },
        summary=f"Opened a poll: {poll.question}",
        request=request,
    )
    await session.commit()
    return (await list_polls(session, user, poll_id=poll.id))[0]


async def list_polls(
    session: AsyncSession, user: User, *, poll_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    hr = permissions.is_hr(user)
    me = await _me(session, user)
    now = datetime.now(UTC)
    stmt = select(Poll).order_by(Poll.created_at.desc()).limit(100)
    if poll_id is not None:
        stmt = stmt.where(Poll.id == poll_id)
    polls = [
        p
        for p in (await session.scalars(tenant_scoped(stmt, Poll, user))).all()
        if _in_audience(p.audience, me, hr=hr)
    ]
    if not polls:
        return []
    ids = [p.id for p in polls]
    counts: dict[uuid.UUID, dict[int, int]] = {}
    for poll_ref, option, count in (
        await session.execute(
            tenant_scoped(
                select(PollVote.poll_id, PollVote.option_index, func.count())
                .where(PollVote.poll_id.in_(ids))
                .group_by(PollVote.poll_id, PollVote.option_index),
                PollVote,
                user,
            )
        )
    ).all():
        counts.setdefault(poll_ref, {})[option] = int(count)
    mine: dict[uuid.UUID, int] = {}
    if me is not None:
        mine = dict(
            (
                await session.execute(
                    tenant_scoped(
                        select(PollVote.poll_id, PollVote.option_index).where(
                            PollVote.poll_id.in_(ids), PollVote.employee_id == me.id
                        ),
                        PollVote,
                        user,
                    )
                )
            ).all()
        )
    out = []
    for poll in polls:
        tally = counts.get(poll.id, {})
        total = sum(tally.values())
        is_open = _is_open(poll, now)
        voted = poll.id in mine
        may_see = hr or voted or not is_open
        enough = total >= MIN_RESPONSES or (hr and not poll.is_anonymous)
        results = (
            [{"option": label, "votes": tally.get(i, 0)} for i, label in enumerate(poll.options)]
            if may_see and enough
            else None
        )
        out.append(
            {
                "id": str(poll.id),
                "question": poll.question,
                "options": poll.options,
                "is_anonymous": poll.is_anonymous,
                "closes_at": poll.closes_at.isoformat() if poll.closes_at else None,
                "is_open": is_open,
                "responses": total,
                "your_vote": mine.get(poll.id),
                "results": results,
                "results_note": (
                    None
                    if results is not None
                    else "Vote to see the results."
                    if not voted and is_open
                    else f"Results show once {MIN_RESPONSES} people have answered."
                ),
            }
        )
    return out


async def vote(
    session: AsyncSession, user: User, poll_id: uuid.UUID, option_index: int
) -> dict[str, Any]:
    permissions.own_employee_id(user)
    me = await _me(session, user)
    poll = await session.scalar(tenant_scoped(select(Poll).where(Poll.id == poll_id), Poll, user))
    if poll is None or not _in_audience(poll.audience, me, hr=False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poll not found")
    if not _is_open(poll, datetime.now(UTC)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This poll has closed.")
    if option_index >= len(poll.options):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="That option does not exist."
        )
    existing = await session.scalar(
        tenant_scoped(
            select(PollVote).where(
                PollVote.poll_id == poll.id, PollVote.employee_id == user.employee_id
            ),
            PollVote,
            user,
        )
    )
    if existing is None:
        session.add(
            PollVote(
                tenant_id=user.tenant_id,
                poll_id=poll.id,
                employee_id=user.employee_id,
                option_index=option_index,
            )
        )
    else:
        existing.option_index = option_index
    # A vote is not audited per person: on an anonymous poll the audit trail
    # would be the very list of who voted what.
    await session.commit()
    return (await list_polls(session, user, poll_id=poll.id))[0]


async def close_poll(
    session: AsyncSession, user: User, poll_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    _require_hr(user)
    poll = await session.scalar(tenant_scoped(select(Poll).where(Poll.id == poll_id), Poll, user))
    if poll is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poll not found")
    if poll.closed_at is None:
        poll.closed_at = datetime.now(UTC)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="poll",
            entity_id=poll.id,
            actor=user,
            changes={"closed": True},
            summary=f"Closed the poll: {poll.question}",
            request=request,
        )
        await session.commit()
    return (await list_polls(session, user, poll_id=poll.id))[0]


# ── Events ──────────────────────────────────────────────────────────────────


async def create_event(
    session: AsyncSession, user: User, payload: EventCreate, *, request: Request | None
) -> dict[str, Any]:
    _require_hr(user)
    event = TeamEvent(
        tenant_id=user.tenant_id,
        title=payload.title.strip(),
        description=(payload.description or "").strip() or None,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        venue=(payload.venue or "").strip() or None,
        capacity=payload.capacity,
        rsvp_by=payload.rsvp_by,
        audience=_audience_json(payload.audience),
        created_by_user_id=user.id,
    )
    session.add(event)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="team_event",
        entity_id=event.id,
        actor=user,
        changes={"title": event.title, "starts_at": event.starts_at.isoformat()},
        summary=f"Created the event {event.title}",
        request=request,
    )
    await session.commit()
    return (await list_events(session, user, event_id=event.id))[0]


async def list_events(
    session: AsyncSession,
    user: User,
    *,
    event_id: uuid.UUID | None = None,
    include_past: bool = False,
) -> list[dict[str, Any]]:
    hr = permissions.is_hr(user)
    me = await _me(session, user)
    now = datetime.now(UTC)
    stmt = select(TeamEvent).order_by(TeamEvent.starts_at)
    if event_id is not None:
        stmt = stmt.where(TeamEvent.id == event_id)
    elif not include_past:
        stmt = stmt.where(TeamEvent.starts_at >= now)
    events = [
        e
        for e in (await session.scalars(tenant_scoped(stmt, TeamEvent, user))).all()
        if _in_audience(e.audience, me, hr=hr)
    ]
    if not events:
        return []
    ids = [e.id for e in events]
    tallies: dict[uuid.UUID, dict[str, int]] = {}
    for ref, response, count in (
        await session.execute(
            tenant_scoped(
                select(EventRsvp.event_id, EventRsvp.response, func.count())
                .where(EventRsvp.event_id.in_(ids))
                .group_by(EventRsvp.event_id, EventRsvp.response),
                EventRsvp,
                user,
            )
        )
    ).all():
        tallies.setdefault(ref, {})[response.value] = int(count)
    mine: dict[uuid.UUID, RsvpResponse] = {}
    if me is not None:
        mine = dict(
            (
                await session.execute(
                    tenant_scoped(
                        select(EventRsvp.event_id, EventRsvp.response).where(
                            EventRsvp.event_id.in_(ids), EventRsvp.employee_id == me.id
                        ),
                        EventRsvp,
                        user,
                    )
                )
            ).all()
        )
    attendees: dict[uuid.UUID, list[str]] = {}
    if hr:
        for ref, name_first, name_last, code in (
            await session.execute(
                tenant_scoped(
                    select(
                        EventRsvp.event_id,
                        Employee.first_name,
                        Employee.last_name,
                        Employee.employee_code,
                    )
                    .join(Employee, Employee.id == EventRsvp.employee_id)
                    .where(
                        EventRsvp.event_id.in_(ids),
                        EventRsvp.response == RsvpResponse.GOING,
                    ),
                    EventRsvp,
                    user,
                )
            )
        ).all():
            attendees.setdefault(ref, []).append(
                f"{' '.join(p for p in (name_first, name_last) if p)} ({code})"
            )
    out = []
    for event in events:
        tally = tallies.get(event.id, {})
        going = tally.get("going", 0)
        out.append(
            {
                "id": str(event.id),
                "title": event.title,
                "description": event.description,
                "starts_at": event.starts_at.isoformat(),
                "ends_at": event.ends_at.isoformat() if event.ends_at else None,
                "venue": event.venue,
                "capacity": event.capacity,
                "rsvp_by": event.rsvp_by.isoformat() if event.rsvp_by else None,
                "cancelled": event.cancelled_at is not None,
                "going": going,
                "maybe": tally.get("maybe", 0),
                "not_going": tally.get("not_going", 0),
                "is_full": event.capacity is not None and going >= event.capacity,
                "your_response": mine[event.id].value if event.id in mine else None,
                "attendees": attendees.get(event.id) if hr else None,
            }
        )
    return out


async def rsvp(
    session: AsyncSession, user: User, event_id: uuid.UUID, response: RsvpResponse
) -> dict[str, Any]:
    permissions.own_employee_id(user)
    me = await _me(session, user)
    event = await session.scalar(
        tenant_scoped(
            select(TeamEvent).where(TeamEvent.id == event_id).with_for_update(),
            TeamEvent,
            user,
        )
    )
    if event is None or not _in_audience(event.audience, me, hr=False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    now = datetime.now(UTC)
    if event.cancelled_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This event is cancelled.")
    closes = event.rsvp_by or event.starts_at
    if closes <= now:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Answers have closed.")
    existing = await session.scalar(
        tenant_scoped(
            select(EventRsvp).where(
                EventRsvp.event_id == event.id, EventRsvp.employee_id == user.employee_id
            ),
            EventRsvp,
            user,
        )
    )
    if (
        response is RsvpResponse.GOING
        and event.capacity is not None
        and (existing is None or existing.response is not RsvpResponse.GOING)
    ):
        going = await session.scalar(
            tenant_scoped(
                select(func.count())
                .select_from(EventRsvp)
                .where(EventRsvp.event_id == event.id, EventRsvp.response == RsvpResponse.GOING),
                EventRsvp,
                user,
            )
        )
        if (going or 0) >= event.capacity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The event is full. Answer maybe to be told if a place opens.",
            )
    if existing is None:
        session.add(
            EventRsvp(
                tenant_id=user.tenant_id,
                event_id=event.id,
                employee_id=user.employee_id,
                response=response,
            )
        )
    else:
        existing.response = response
    await session.commit()
    return (await list_events(session, user, event_id=event.id))[0]


async def cancel_event(
    session: AsyncSession, user: User, event_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    _require_hr(user)
    event = await session.scalar(
        tenant_scoped(select(TeamEvent).where(TeamEvent.id == event_id), TeamEvent, user)
    )
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    if event.cancelled_at is None:
        event.cancelled_at = datetime.now(UTC)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="team_event",
            entity_id=event.id,
            actor=user,
            changes={"cancelled": True},
            summary=f"Cancelled the event {event.title}",
            request=request,
        )
        await session.commit()
    return (await list_events(session, user, event_id=event.id))[0]
