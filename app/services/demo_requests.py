"""Demo requests: received from the website, followed up by the platform team.

**Who can touch them.** Anyone may *send* one (the public endpoint, rate
limited and guarded by a honeypot). Only the platform's super admin may list
or update them. A demo request belongs to no workspace — the sender is not a
customer yet — so there is no tenant scope to apply; the super-admin check is
the whole of the access rule, and it is made here as well as on the route.

**Telling the team.** Every active super admin gets an in-app notification
and an email, queued in the same transaction as the request so a request is
never stored without the team hearing about it (and never announced without
being stored). Delivery itself is left to the delivery job, so the website's
visitor never waits on a mail relay.

**Audit.** A super admin's change of status or notes is audited in their own
workspace, in the same transaction as the change, like every other platform
action. The submission itself is not an action inside any workspace and has
no audit row; the request row *is* its record.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.audit import AuditAction
from app.models.demo_request import DemoRequest, DemoRequestStatus
from app.models.notifications import Channel, DigestMode
from app.models.user import User, UserRole
from app.schemas.demo_request import DemoRequestCreate, DemoRequestRead, DemoRequestUpdate
from app.services import audit
from app.services.auth import web_app_url
from app.services.notifications.dispatch import notify, register_event

__all__ = [
    "EVENT_KEY",
    "RETENTION_DAYS",
    "erase",
    "list_requests",
    "purge_stale",
    "submit",
    "update",
]

#: How long an enquiry is kept after the last activity on it, as the website's
#: privacy policy says. A lead nobody has touched in a year is not "being
#: discussed" any more (DPDP s.8(7)).
RETENTION_DAYS = 365

EVENT_KEY = "platform.demo_request.received"

register_event(
    EVENT_KEY,
    "New demo request: {company}",
    "{name} from {company} ({size} employees) asked for {topic}. Email: {email}. "
    "Interested in: {interests}. Follow up in the platform console: {link}",
    preferences={Channel.EMAIL: DigestMode.IMMEDIATE, Channel.IN_APP: DigestMode.IMMEDIATE},
)

_TOPIC_WORDS = {
    "demo": "a demo",
    "pricing": "pricing",
    "partnership": "a partnership",
    "other": "a conversation",
}


def _require_super(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN or getattr(user, "operator_grant_id", None):
        raise permissions.forbidden("Only the platform's super admin sees demo requests.")


async def _super_admins(session: AsyncSession) -> list[User]:
    """Every active platform super admin. A platform-wide read, by design: the
    team that follows up on demo requests is the platform's, not a workspace's."""
    rows = await session.scalars(
        select(User).where(User.role == UserRole.SUPER_ADMIN, User.is_active.is_(True))
    )
    return list(rows)


async def submit(session: AsyncSession, payload: DemoRequestCreate) -> DemoRequest:
    """Store a request from the website and queue word of it to the platform team."""
    row = DemoRequest(
        full_name=payload.full_name,
        work_email=str(payload.work_email).lower(),
        company=payload.company,
        phone=payload.phone,
        company_size=payload.company_size,
        role=payload.role,
        interests=list(payload.interests),
        message=payload.message,
        topic=payload.topic,
        source_path=payload.source_path,
        consented_at=datetime.now(UTC),
        status=DemoRequestStatus.NEW,
    )
    session.add(row)
    await session.flush()

    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for admin in await _super_admins(session):
        by_tenant[admin.tenant_id].append(admin.id)
    message = {
        "name": row.full_name,
        "company": row.company,
        "size": row.company_size,
        "topic": _TOPIC_WORDS.get(row.topic, "a conversation"),
        "email": row.work_email,
        "interests": ", ".join(row.interests) or "not said",
        "demo_request_id": str(row.id),
        "link": f"{web_app_url()}/platform/demo-requests",
    }
    for tenant_id, user_ids in by_tenant.items():
        await notify(
            session,
            tenant_id=tenant_id,
            event_key=EVENT_KEY,
            recipient_user_ids=user_ids,
            payload=message,
            commit=False,
            deliver=False,
        )
    await session.commit()
    await session.refresh(row)
    return row


def _read(row: DemoRequest, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    data = DemoRequestRead.model_validate(row)
    data.handled_by_name = names.get(row.handled_by_user_id) if row.handled_by_user_id else None
    return data.model_dump(mode="json")


async def _names(session: AsyncSession, rows: list[DemoRequest]) -> dict[uuid.UUID, str]:
    ids = {r.handled_by_user_id for r in rows if r.handled_by_user_id}
    if not ids:
        return {}
    users = await session.scalars(select(User).where(User.id.in_(ids)))
    return {u.id: u.full_name for u in users}


async def list_requests(
    session: AsyncSession, user: User, *, status_filter: DemoRequestStatus | None = None
) -> list[dict[str, Any]]:
    """All requests, newest first; optionally one status."""
    _require_super(user)
    query = select(DemoRequest).order_by(DemoRequest.created_at.desc())
    if status_filter is not None:
        query = query.where(DemoRequest.status == status_filter)
    rows = list(await session.scalars(query))
    names = await _names(session, rows)
    return [_read(r, names) for r in rows]


async def update(
    session: AsyncSession,
    user: User,
    request_id: uuid.UUID,
    payload: DemoRequestUpdate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Change a request's status or notes, audited with the change."""
    _require_super(user)
    row = await session.get(DemoRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demo request not found")

    changes: dict[str, Any] = {}
    if payload.status is not None and payload.status is not row.status:
        changes["status"] = {"from": row.status.value, "to": payload.status.value}
        row.status = payload.status
        row.handled_by_user_id = user.id
        row.handled_at = datetime.now(UTC)
    if "notes" in payload.model_fields_set:
        notes = (payload.notes or "").strip() or None
        if notes != row.notes:
            # The notes themselves stay out of the audit log; that they changed does not.
            changes["notes"] = "changed"
            row.notes = notes
    if not changes:
        names = await _names(session, [row])
        return _read(row, names)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="demo_request",
        entity_id=row.id,
        actor=user,
        actor_type="platform",
        changes=changes,
        summary=f"Demo request from {row.company} updated",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    names = await _names(session, [row])
    return _read(row, names)


async def erase(
    session: AsyncSession, user: User, request_id: uuid.UUID, *, request: Request | None = None
) -> None:
    """Delete one enquiry because its sender asked (DPDP s.12). Audited, not undoable."""
    _require_super(user)
    row = await session.get(DemoRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demo request not found")
    company = row.company
    await session.delete(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="demo_request",
        entity_id=request_id,
        actor=user,
        actor_type="platform",
        # The sender's name and email go with the row; the log keeps the company.
        summary=f"Website enquiry from {company} deleted at the sender's request",
        request=request,
    )
    await session.commit()


async def purge_stale(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete enquiries with no activity for `RETENTION_DAYS`. The platform job's work."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS)
    result = await session.execute(delete(DemoRequest).where(DemoRequest.updated_at < cutoff))
    return int(result.rowcount or 0)
