"""A person's private calendar feed: holidays and approved leave, as an .ics address.

Any calendar app subscribes to it by address — Google Calendar's "From URL",
Outlook's "Subscribe from web", Apple Calendar — with no app registration and no
sign-in, so it works for every workspace on day one. It carries:

- the person's company holidays: the ones that apply to their location, state
  and company, and the optional ones they opted in to;
- their own approved leave, with its type;
- optionally, their direct reports' approved leave, as "<name> — on leave" with
  no type or reason (a manager already sees who is away; why is not theirs).

**The address is the credential.** Calendar apps cannot sign in, so the link
carries a long random token; only its SHA-256 is kept, it is shown once, making
a new link revokes the old, and a deactivated person's link stops answering.
Calendar apps re-read it every few hours; nothing is pushed.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.integrations import CalendarFeedToken
from app.models.leave import Holiday, LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.leave.holidays import holiday_applies, holiday_records_for_employees

__all__ = ["create_feed", "feed_status", "render_feed", "revoke_feed"]

#: How far back and ahead the feed reaches.
PAST = timedelta(days=90)
AHEAD = timedelta(days=400)
#: Don't write `last_used_at` on every poll.
TOUCH_EVERY = timedelta(hours=1)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def feed_path(token: str) -> str:
    return f"/api/v1/public/calendar/{token}.ics"


async def _active(
    session: AsyncSession, user: User, user_id: uuid.UUID
) -> CalendarFeedToken | None:
    """The live link of `user_id` — always the caller's own id, from the token."""
    return await session.scalar(
        tenant_scoped(
            select(CalendarFeedToken).where(
                CalendarFeedToken.user_id == user_id, CalendarFeedToken.revoked_at.is_(None)
            ),
            CalendarFeedToken,
            user,
        )
    )


async def _has_reports(session: AsyncSession, user: User) -> bool:
    if user.employee_id is None:
        return False
    found = await session.scalar(
        tenant_scoped(
            select(Employee.id).where(Employee.manager_id == user.employee_id).limit(1),
            Employee,
            user,
        )
    )
    return found is not None


async def feed_status(session: AsyncSession, user: User) -> dict[str, Any]:
    row = await _active(session, user, user.id)
    return {
        "active": row is not None,
        "include_team": bool(row and row.include_team),
        "created_at": row.created_at.isoformat() if row else None,
        "last_used_at": row.last_used_at.isoformat() if row and row.last_used_at else None,
        "has_team": await _has_reports(session, user),
    }


async def create_feed(
    session: AsyncSession, user: User, *, include_team: bool, request: Request | None = None
) -> dict[str, Any]:
    """A new link, shown once. Any earlier link stops working."""
    now = datetime.now(UTC)
    previous = await _active(session, user, user.id)
    if previous is not None:
        previous.revoked_at = now
    team = include_team and await _has_reports(session, user)
    token = secrets.token_urlsafe(32)
    row = CalendarFeedToken(
        tenant_id=user.tenant_id, user_id=user.id, token_hash=_hash(token), include_team=team
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="calendar_feed",
        entity_id=row.id,
        changes={"include_team": team, "replaced": previous is not None},
        summary="Calendar feed link created" + (" (replacing the earlier one)" if previous else ""),
        request=request,
    )
    await session.commit()
    return {"path": feed_path(token), **(await feed_status(session, user))}


async def revoke_feed(session: AsyncSession, user: User, *, request: Request | None = None) -> None:
    row = await _active(session, user, user.id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No calendar link is on.")
    row.revoked_at = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="calendar_feed",
        entity_id=row.id,
        summary="Calendar feed link turned off",
        request=request,
    )
    await session.commit()


# ── The .ics itself ─────────────────────────────────────────────────────────


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 line folding: at most 75 octets a line, continuations start with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: list[str] = []
    current = b""
    for char in line:
        encoded = char.encode("utf-8")
        limit = 75 if not parts else 74
        if len(current) + len(encoded) > limit:
            parts.append(current.decode("utf-8"))
            current = b""
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _event(uid: str, start: date, end_inclusive: date, summary: str, *, busy: bool) -> list[str]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}@askmira",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
        f"DTEND;VALUE=DATE:{end_inclusive + timedelta(days=1):%Y%m%d}",
        f"SUMMARY:{_escape(summary)}",
        f"TRANSP:{'OPAQUE' if busy else 'TRANSPARENT'}",
        "END:VEVENT",
    ]


def _leave_label(request: LeaveRequest, type_name: str | None) -> str:
    label = f"On leave — {type_name}" if type_name else "On leave"
    if request.is_half_day:
        half = (request.half_day_session or "").replace("_", " ").strip()
        label += f" (half day{', ' + half if half else ''})"
    return label


async def render_feed(session: AsyncSession, token: str) -> str:
    row = await session.scalar(
        select(CalendarFeedToken).where(
            CalendarFeedToken.token_hash == _hash(token), CalendarFeedToken.revoked_at.is_(None)
        )
    )
    owner = await session.get(User, row.user_id) if row is not None else None
    if row is None or owner is None or not owner.is_active or owner.tenant_id != row.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No calendar here.")

    now = datetime.now(UTC)
    today = now.date()
    start, end = today - PAST, today + AHEAD
    tenant = await session.get(Tenant, owner.tenant_id)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AskMira//Holidays and leave//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(f'AskMira — {tenant.name}' if tenant else 'AskMira')}",
        "X-WR-TIMEZONE:Asia/Kolkata",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    # Holidays that apply to this person; without an employee record, the
    # holidays that apply to everyone.
    if owner.employee_id is not None:
        records = await holiday_records_for_employees(
            session, owner, [owner.employee_id], start, end
        )
        holidays = sorted(records.get(owner.employee_id, {}).items())
        for day, holiday in holidays:
            lines += _event(
                f"holiday-{holiday.id}-{day:%Y%m%d}",
                day,
                day,
                f"{holiday.name} (holiday)",
                busy=False,
            )
    else:
        everyone = (
            await session.scalars(
                tenant_scoped(
                    select(Holiday).where(
                        Holiday.holiday_date >= start, Holiday.holiday_date <= end
                    ),
                    Holiday,
                    owner,
                )
            )
        ).all()
        for holiday in sorted(everyone, key=lambda h: h.holiday_date):
            if holiday_applies(holiday, state_code=None, location_id=None, legal_entity_id=None):
                lines += _event(
                    f"holiday-{holiday.id}",
                    holiday.holiday_date,
                    holiday.holiday_date,
                    f"{holiday.name} (holiday)",
                    busy=False,
                )

    # Approved leave: one's own with its type; the team's as "on leave" only.
    people: list[uuid.UUID] = [owner.employee_id] if owner.employee_id else []
    team: dict[uuid.UUID, str] = {}
    if row.include_team and owner.employee_id is not None:
        reports = (
            await session.execute(
                tenant_scoped(
                    select(Employee).where(Employee.manager_id == owner.employee_id),
                    Employee,
                    owner,
                )
            )
        ).scalars()
        team = {e.id: e.full_name for e in reports}
        people += list(team)
    if people:
        leaves = (
            await session.execute(
                tenant_scoped(
                    select(LeaveRequest, LeaveType.name)
                    .outerjoin(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                    .where(
                        LeaveRequest.employee_id.in_(people),
                        LeaveRequest.status == LeaveRequestStatus.APPROVED,
                        LeaveRequest.end_date >= start,
                        LeaveRequest.start_date <= end,
                    )
                    .order_by(LeaveRequest.start_date),
                    LeaveRequest,
                    owner,
                )
            )
        ).all()
        for leave, type_name in leaves:
            if leave.employee_id == owner.employee_id:
                summary, busy = _leave_label(leave, type_name), True
            else:
                summary, busy = f"{team.get(leave.employee_id, 'Team member')} — on leave", False
            lines += _event(
                f"leave-{leave.id}", leave.start_date, leave.end_date, summary, busy=busy
            )

    lines.append("END:VCALENDAR")
    body = "\r\n".join(_fold(line) for line in lines) + "\r\n"
    if row.last_used_at is None or now - row.last_used_at > TOUCH_EVERY:
        row.last_used_at = now
        await session.commit()
    return body
