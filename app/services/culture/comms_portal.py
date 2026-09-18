"""Announcements and celebration privacy as people use them (slice 32).

An announcement reaches a person if it has been published, has not expired, and
its audience includes them - decided here, on the server, before anything is
returned. Acknowledging one you are not in the audience for is a 404, the same
as one that does not exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.culture_depth import (
    Announcement,
    AnnouncementAcknowledgement,
    EventPrivacyPreference,
)
from app.models.employee import Employee
from app.models.user import User
from app.schemas.culture import (
    AcknowledgeCreate,
    AnnouncementCreate,
    AnnouncementManageRead,
    AnnouncementRead,
    EventPrivacyRead,
    EventPrivacyUpdate,
)
from app.services.culture import comms as comms_engine
from app.services.culture import service as culture
from app.services.culture.feed import person_refs

__all__ = [
    "acknowledge",
    "acknowledgement_register",
    "create_announcement",
    "get_my_event_privacy",
    "list_managed",
    "list_my_announcements",
    "set_my_event_privacy",
]

#: How far back "announcements for me" reaches before the audience filter.
_RECENT = 200


def _audience(row: Announcement) -> comms_engine.Audience:
    spec = row.audience or {}
    return comms_engine.Audience(
        department_ids=tuple(spec.get("department_ids", [])),
        location_ids=tuple(spec.get("location_ids", [])),
        grades=tuple(spec.get("grades", [])),
    )


def _includes(row: Announcement, employee: Employee | None) -> bool:
    audience = _audience(row)
    if audience.is_everyone:
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


def _read(row: Announcement, acks: list[AnnouncementAcknowledgement]) -> AnnouncementRead:
    current = comms_engine.body_hash(row.title, row.body)
    ack = next((a for a in acks if a.content_hash == current), None)
    return AnnouncementRead(
        id=row.id,
        title=row.title,
        body=row.body,
        publish_at=row.publish_at,
        expires_at=row.expires_at,
        requires_acknowledgement=row.requires_acknowledgement,
        acknowledged=ack is not None,
        acknowledged_at=ack.acknowledged_at if ack else None,
    )


async def _me(session: AsyncSession, user: User) -> Employee | None:
    if user.employee_id is None:
        return None
    return await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == user.employee_id), Employee, user)
    )


def _live(now: datetime):
    return (
        Announcement.publish_at <= now,
        or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
    )


async def _acks_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[AnnouncementAcknowledgement]]:
    if not ids:
        return {}
    out: dict[uuid.UUID, list[AnnouncementAcknowledgement]] = {}
    for ack in (
        await session.scalars(
            tenant_scoped(
                select(AnnouncementAcknowledgement).where(
                    AnnouncementAcknowledgement.employee_id == employee_id,
                    AnnouncementAcknowledgement.announcement_id.in_(ids),
                ),
                AnnouncementAcknowledgement,
                user,
            )
        )
    ).all():
        out.setdefault(ack.announcement_id, []).append(ack)
    return out


async def list_my_announcements(
    session: AsyncSession, user: User, *, limit: int = 50
) -> list[AnnouncementRead]:
    """Live announcements whose audience includes the caller, newest first."""
    now = datetime.now(UTC)
    me = await _me(session, user)
    rows = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(Announcement)
                    .where(*_live(now))
                    .order_by(Announcement.publish_at.desc())
                    .limit(_RECENT),
                    Announcement,
                    user,
                )
            )
        ).all()
    )
    visible = [row for row in rows if _includes(row, me)][:limit]
    acks = await _acks_for(session, user, me.id, [r.id for r in visible]) if me else {}
    return [_read(row, acks.get(row.id, [])) for row in visible]


async def acknowledge(
    session: AsyncSession,
    user: User,
    announcement_id: uuid.UUID,
    payload: AcknowledgeCreate,
) -> AnnouncementRead:
    """Acknowledge the current text of an announcement sent to you.

    Only the recipient acknowledges - HR included - because an acknowledgement
    recorded by somebody else is not one.
    """
    me_id = permissions.own_employee_id(user)
    me = await _me(session, user)
    now = datetime.now(UTC)
    row = await session.scalar(
        tenant_scoped(
            select(Announcement).where(Announcement.id == announcement_id, *_live(now)),
            Announcement,
            user,
        )
    )
    if row is None or not _includes(row, me):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such announcement.")
    await culture.acknowledge(
        session, user, announcement_id=row.id, employee_id=me_id, channel=payload.channel
    )
    acks = await _acks_for(session, user, me_id, [row.id])
    return _read(row, acks.get(row.id, []))


def _manage_read(row: Announcement, now: datetime) -> AnnouncementManageRead:
    return AnnouncementManageRead(
        id=row.id,
        title=row.title,
        body=row.body,
        publish_at=row.publish_at,
        expires_at=row.expires_at,
        requires_acknowledgement=row.requires_acknowledgement,
        channels=list(row.channels or []),
        audience=dict(row.audience or {}),
        audience_description=_audience(row).describe(),
        delivered_at=row.delivered_at,
        delivered_count=row.delivered_count or 0,
        is_live=row.publish_at <= now and (row.expires_at is None or row.expires_at > now),
        created_at=row.created_at,
    )


async def list_managed(session: AsyncSession, user: User) -> list[AnnouncementManageRead]:
    permissions.require_hr(user, detail="Announcements are managed by HR.")
    now = datetime.now(UTC)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(Announcement).order_by(Announcement.publish_at.desc()).limit(_RECENT),
                Announcement,
                user,
            )
        )
    ).all()
    return [_manage_read(row, now) for row in rows]


async def create_announcement(
    session: AsyncSession,
    user: User,
    payload: AnnouncementCreate,
    *,
    request: Request | None = None,
) -> AnnouncementManageRead:
    permissions.require_hr(user, detail="Announcements are published by HR.")
    row = await culture.announce(
        session,
        user,
        title=payload.title.strip(),
        body=payload.body,
        publish_at=payload.publish_at or datetime.now(UTC),
        audience=payload.audience.model_dump(),
        expires_at=payload.expires_at,
        requires_acknowledgement=payload.requires_acknowledgement,
        channels=list(dict.fromkeys(payload.channels)),
        request=request,
    )
    return _manage_read(row, datetime.now(UTC))


async def acknowledgement_register(
    session: AsyncSession, user: User, announcement_id: uuid.UUID
) -> dict[str, Any]:
    """The register, with names - what a POSH or IT-policy audit asks for."""
    permissions.require_hr(user, detail="The acknowledgement register is for HR.")
    report = await culture.acknowledgement_register(
        session, user, announcement_id=announcement_id
    )
    acks = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(AnnouncementAcknowledgement)
                    .where(
                        AnnouncementAcknowledgement.announcement_id == announcement_id,
                        AnnouncementAcknowledgement.content_hash == report["content_hash"],
                    )
                    .order_by(AnnouncementAcknowledgement.acknowledged_at),
                    AnnouncementAcknowledgement,
                    user,
                )
            )
        ).all()
    )
    ids = {uuid.UUID(i) for i in [*report["outstanding"], *report["stale"]]}
    ids |= {a.employee_id for a in acks}
    people = await person_refs(session, user, ids)
    report["people"] = {str(k): v.name for k, v in people.items()}
    report["acknowledged_people"] = [
        {
            "employee_id": str(a.employee_id),
            "name": people[a.employee_id].name,
            "acknowledged_at": a.acknowledged_at.isoformat(),
            "channel": a.channel,
        }
        for a in acks
    ]
    return report


# ── Celebration privacy ─────────────────────────────────────────────────────


async def get_my_event_privacy(session: AsyncSession, user: User) -> EventPrivacyRead:
    me = permissions.own_employee_id(user)
    row = await session.scalar(
        tenant_scoped(
            select(EventPrivacyPreference).where(EventPrivacyPreference.employee_id == me),
            EventPrivacyPreference,
            user,
        )
    )
    if row is None:
        return EventPrivacyRead(
            share_birthday=False,
            share_work_anniversary=False,
            birthday_scope="team",
            configured=False,
        )
    return EventPrivacyRead(
        share_birthday=row.share_birthday,
        share_work_anniversary=row.share_work_anniversary,
        birthday_scope="company" if row.birthday_scope == "company" else "team",
        configured=True,
    )


async def set_my_event_privacy(
    session: AsyncSession,
    user: User,
    payload: EventPrivacyUpdate,
    *,
    request: Request | None = None,
) -> EventPrivacyRead:
    """Your own choice, only. Nobody consents on anybody's behalf."""
    me = permissions.own_employee_id(user)
    row = await culture.set_event_privacy(
        session,
        user,
        employee_id=me,
        share_birthday=payload.share_birthday,
        share_work_anniversary=payload.share_work_anniversary,
        birthday_scope=payload.birthday_scope,
        request=request,
    )
    return EventPrivacyRead(
        share_birthday=row.share_birthday,
        share_work_anniversary=row.share_work_anniversary,
        birthday_scope="company" if row.birthday_scope == "company" else "team",
        configured=True,
    )
