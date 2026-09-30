"""HR helpdesk tickets: raising, the conversation, working the queue, the SLA.

═══════════════════════════════════════════════════════════════════════════════
WHO SEES A TICKET
═══════════════════════════════════════════════════════════════════════════════

- **The requester** — the employee the ticket is about — sees it with its public
  thread. Never an internal note, never a file attached to one.
- **Agents** work it: HR (tenant admin, HR admin, HR ops), the owner of its
  category, and whoever it is assigned to.
- **A confidential ticket** (a grievance) narrows the agents to the category
  owner, the assignee and tenant/HR admins. HR ops does not see it.

Anyone else gets a 404, not a 403, so a ticket's existence is not confirmed to a
colleague guessing ids. And nobody works their own ticket: an HR admin who
raises one is its requester like anybody else, and it stays out of their queue.

═══════════════════════════════════════════════════════════════════════════════
THE SLA CLOCK
═══════════════════════════════════════════════════════════════════════════════

`due_at` is the raise time plus the category's SLA in *calendar* hours (see
`rules.compute_due_at`). Reopening restarts it. It does not pause while the
ticket waits on the employee — the queue shows the deadline it computed rather
than a clock with exceptions nobody can check.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, status
from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.config import settings
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.helpdesk import (
    HelpdeskAttachment,
    HelpdeskCategory,
    HelpdeskComment,
    HelpdeskPriority,
    HelpdeskSequence,
    HelpdeskTicket,
    HelpdeskTicketStatus,
)
from app.models.notifications import Channel, DigestMode
from app.models.user import User, UserRole
from app.schemas.helpdesk import (
    AgentOption,
    AttachmentRead,
    CommentRead,
    MySummary,
    QueueSummary,
    TicketAgentUpdate,
    TicketDetail,
    TicketSummary,
)
from app.services import audit
from app.services.helpdesk import rules
from app.services.notifications.dispatch import notify, register_event
from app.services.storage import FileRejectedError, StorageError, get_storage, validate_upload

__all__ = [
    "add_comment",
    "assign_ticket",
    "auto_close_resolved",
    "cancel_ticket",
    "create_ticket",
    "download_attachment",
    "get_ticket",
    "is_agent",
    "list_agents",
    "list_my_tickets",
    "list_queue",
    "my_summary",
    "queue_summary",
    "rate_ticket",
    "reopen_ticket",
    "ticket_assignees",
    "update_ticket",
]

INDIA = ZoneInfo("Asia/Kolkata")

#: Who reads a confidential ticket besides its requester, owner and assignee.
ADMIN_ROLES: frozenset[UserRole] = frozenset({UserRole.TENANT_ADMIN, UserRole.HR_ADMIN})

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

STATUS_LABEL: dict[HelpdeskTicketStatus, str] = {
    HelpdeskTicketStatus.OPEN: "Open",
    HelpdeskTicketStatus.IN_PROGRESS: "In progress",
    HelpdeskTicketStatus.WAITING_ON_EMPLOYEE: "Waiting on employee",
    HelpdeskTicketStatus.RESOLVED: "Resolved",
    HelpdeskTicketStatus.CLOSED: "Closed",
    HelpdeskTicketStatus.CANCELLED: "Cancelled",
}
PRIORITY_LABEL: dict[HelpdeskPriority, str] = {
    HelpdeskPriority.LOW: "Low",
    HelpdeskPriority.MEDIUM: "Medium",
    HelpdeskPriority.HIGH: "High",
    HelpdeskPriority.URGENT: "Urgent",
}

# ── Notification events ─────────────────────────────────────────────────────
# The subject line of a confidential ticket is never put in a notification:
# an email subject is read on lock screens and in shared inboxes.

_PROMPT = {Channel.IN_APP: DigestMode.IMMEDIATE, Channel.EMAIL: DigestMode.IMMEDIATE}

register_event(
    "helpdesk.ticket.created",
    "New helpdesk ticket {ticket_number}",
    "{employee_name} raised {ticket_number} in {category}: {subject}",
    preferences=_PROMPT,
)
register_event(
    "helpdesk.ticket.assigned",
    "{ticket_number} is assigned to you",
    "{actor_name} assigned {ticket_number} ({category}) to you: {subject}",
    preferences=_PROMPT,
)
register_event(
    "helpdesk.ticket.replied",
    "New reply on {ticket_number}",
    "{author_name} replied on {ticket_number}: {subject}",
    preferences=_PROMPT,
)
register_event(
    "helpdesk.ticket.resolved",
    "{ticket_number} is resolved",
    "{actor_name} marked {ticket_number} ({subject}) resolved. If something is still "
    "wrong you can reopen it within 7 days; otherwise, rate how it was handled.",
    preferences=_PROMPT,
)


# ═══════════════════════════════════════════════════════════════════════════
# Who may do what
# ═══════════════════════════════════════════════════════════════════════════

Viewer = Literal["requester", "agent"]


def _is_admin(user: User) -> bool:
    return user.role in ADMIN_ROLES or user.role is UserRole.PLATFORM_ADMIN


def viewer_role(
    user: User, ticket: HelpdeskTicket, owner_user_id: uuid.UUID | None
) -> Viewer | None:
    """How the caller relates to a ticket, or None when they may not see it."""
    if user.employee_id is not None and ticket.employee_id == user.employee_id:
        return "requester"
    if owner_user_id is not None and owner_user_id == user.id:
        return "agent"
    if ticket.assignee_user_id is not None and ticket.assignee_user_id == user.id:
        return "agent"
    if ticket.is_confidential:
        return "agent" if _is_admin(user) else None
    return "agent" if permissions.is_hr(user) else None


def _eligible_assignee(
    candidate: User, ticket: HelpdeskTicket, owner_user_id: uuid.UUID | None
) -> bool:
    """Who may be handed a ticket: never its requester, and for a confidential
    one only the owner or a tenant/HR admin."""
    if not candidate.is_active:
        return False
    if candidate.employee_id is not None and candidate.employee_id == ticket.employee_id:
        return False
    if owner_user_id is not None and candidate.id == owner_user_id:
        return True
    roles = ADMIN_ROLES if ticket.is_confidential else permissions.HR_ROLES
    return candidate.role in roles


async def _load(
    session: AsyncSession, user: User, ticket_id: uuid.UUID, *, lock: bool = False
) -> tuple[HelpdeskTicket, HelpdeskCategory]:
    stmt = tenant_scoped(
        select(HelpdeskTicket, HelpdeskCategory)
        .join(HelpdeskCategory, HelpdeskCategory.id == HelpdeskTicket.category_id)
        .where(HelpdeskTicket.id == ticket_id),
        HelpdeskTicket,
        user,
    )
    if lock:
        stmt = stmt.with_for_update(of=HelpdeskTicket)
    row = (await session.execute(stmt)).first()
    if row is None:
        raise _NOT_FOUND
    return row[0], row[1]


async def _visible_ticket(
    session: AsyncSession, user: User, ticket_id: uuid.UUID, *, lock: bool = False
) -> tuple[HelpdeskTicket, HelpdeskCategory, Viewer]:
    """The ticket and how the caller relates to it. Not theirs to see: 404."""
    ticket, category = await _load(session, user, ticket_id, lock=lock)
    role = viewer_role(user, ticket, category.owner_user_id)
    if role is None:
        raise _NOT_FOUND
    return ticket, category, role


async def _requester_ticket(
    session: AsyncSession, user: User, ticket_id: uuid.UUID
) -> tuple[HelpdeskTicket, HelpdeskCategory]:
    ticket, category, role = await _visible_ticket(session, user, ticket_id, lock=True)
    if role != "requester":
        raise permissions.forbidden("Only the employee who raised this ticket can do that.")
    return ticket, category


async def _agent_ticket(
    session: AsyncSession, user: User, ticket_id: uuid.UUID, *, lock: bool = True
) -> tuple[HelpdeskTicket, HelpdeskCategory]:
    ticket, category, role = await _visible_ticket(session, user, ticket_id, lock=lock)
    if role != "agent":
        raise permissions.forbidden(
            "You raised this ticket, so somebody else works it. You can reply, cancel, "
            "reopen or rate it from My tickets."
        )
    return ticket, category


async def _owned_categories(session: AsyncSession, user: User) -> set[uuid.UUID]:
    rows = await session.scalars(
        tenant_scoped(
            select(HelpdeskCategory.id).where(HelpdeskCategory.owner_user_id == user.id),
            HelpdeskCategory,
            user,
        )
    )
    return set(rows.all())


async def _require_agent(session: AsyncSession, user: User) -> set[uuid.UUID]:
    """The categories the caller owns; refused unless they are HR or own one."""
    owned = await _owned_categories(session, user)
    if not permissions.is_hr(user) and not owned:
        raise permissions.forbidden("The helpdesk queue is for HR and category owners.")
    return owned


def _agent_scope(stmt: Select, user: User, owned: set[uuid.UUID]) -> Select:
    """Narrow a ticket query to what the caller may work, minus their own."""
    if not _is_admin(user):
        allowed: list[Any] = [HelpdeskTicket.assignee_user_id == user.id]
        if owned:
            allowed.append(HelpdeskTicket.category_id.in_(owned))
        if permissions.is_hr(user):
            allowed.append(HelpdeskTicket.is_confidential.is_(False))
        stmt = stmt.where(or_(*allowed))
    if user.employee_id is not None:
        stmt = stmt.where(HelpdeskTicket.employee_id != user.employee_id)
    return stmt


async def is_agent(session: AsyncSession, user: User) -> bool:
    return permissions.is_hr(user) or bool(await _owned_categories(session, user))


# ═══════════════════════════════════════════════════════════════════════════
# Serialising
# ═══════════════════════════════════════════════════════════════════════════


async def _users(session: AsyncSession, user: User, ids: set[Any]) -> dict[uuid.UUID, User]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    rows = await session.scalars(tenant_scoped(select(User).where(User.id.in_(ids)), User, user))
    return {row.id: row for row in rows.all()}


async def _employees(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, Employee]:
    if not ids:
        return {}
    rows = await session.scalars(
        tenant_scoped(select(Employee).where(Employee.id.in_(ids)), Employee, user)
    )
    return {row.id: row for row in rows.all()}


def _summary(
    ticket: HelpdeskTicket,
    category: HelpdeskCategory,
    employee: Employee | None,
    users: dict[uuid.UUID, User],
    now: datetime,
) -> TicketSummary:
    raiser = users.get(ticket.raised_by_user_id) if ticket.raised_by_user_id else None
    assignee = users.get(ticket.assignee_user_id) if ticket.assignee_user_id else None
    return TicketSummary(
        id=ticket.id,
        ticket_number=ticket.ticket_number,
        subject=ticket.subject,
        category_id=category.id,
        category_name=category.name,
        request_kind=category.request_kind,
        priority=ticket.priority,
        status=ticket.status,
        is_confidential=ticket.is_confidential,
        employee_id=ticket.employee_id,
        employee_name=employee.full_name if employee else "Former employee",
        employee_code=employee.employee_code if employee else "",
        raised_by_user_id=ticket.raised_by_user_id,
        raised_on_behalf=raiser is not None and raiser.employee_id != ticket.employee_id,
        assignee_user_id=ticket.assignee_user_id,
        assignee_name=assignee.full_name if assignee else None,
        sla_hours=ticket.sla_hours,
        due_at=ticket.due_at,
        sla_state=rules.sla_state(  # type: ignore[arg-type]
            status=ticket.status,
            due_at=ticket.due_at,
            sla_hours=ticket.sla_hours,
            resolved_at=ticket.resolved_at,
            now=now,
        ),
        first_response_at=ticket.first_response_at,
        resolved_at=ticket.resolved_at,
        closed_at=ticket.closed_at,
        satisfaction_rating=ticket.satisfaction_rating,
        created_at=ticket.created_at,
        last_activity_at=ticket.last_activity_at,
    )


async def _summaries(
    session: AsyncSession,
    user: User,
    rows: list[tuple[HelpdeskTicket, HelpdeskCategory]],
) -> list[TicketSummary]:
    employees = await _employees(session, user, {t.employee_id for t, _ in rows})
    users = await _users(
        session,
        user,
        {t.assignee_user_id for t, _ in rows} | {t.raised_by_user_id for t, _ in rows},
    )
    now = datetime.now(UTC)
    return [_summary(t, c, employees.get(t.employee_id), users, now) for t, c in rows]


def _attachment_read(row: HelpdeskAttachment) -> AttachmentRead:
    return AttachmentRead(
        id=row.id,
        file_name=row.file_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
    )


async def _detail(
    session: AsyncSession,
    user: User,
    ticket: HelpdeskTicket,
    category: HelpdeskCategory,
    role: Viewer,
) -> TicketDetail:
    comment_stmt = tenant_scoped(
        select(HelpdeskComment)
        .where(HelpdeskComment.ticket_id == ticket.id)
        .order_by(HelpdeskComment.created_at, HelpdeskComment.id),
        HelpdeskComment,
        user,
    )
    if role == "requester":
        comment_stmt = comment_stmt.where(HelpdeskComment.is_internal.is_(False))
    comments = list((await session.scalars(comment_stmt)).all())
    visible_comment_ids = {c.id for c in comments}

    attachments = (
        await session.scalars(
            tenant_scoped(
                select(HelpdeskAttachment)
                .where(HelpdeskAttachment.ticket_id == ticket.id)
                .order_by(HelpdeskAttachment.created_at),
                HelpdeskAttachment,
                user,
            )
        )
    ).all()
    on_ticket: list[AttachmentRead] = []
    by_comment: dict[uuid.UUID, list[AttachmentRead]] = {}
    for row in attachments:
        if row.comment_id is None:
            on_ticket.append(_attachment_read(row))
        elif row.comment_id in visible_comment_ids:
            # A file on an internal note is as internal as the note.
            by_comment.setdefault(row.comment_id, []).append(_attachment_read(row))

    users = await _users(
        session,
        user,
        {c.author_user_id for c in comments}
        | {ticket.assignee_user_id, ticket.raised_by_user_id},
    )
    employees = await _employees(session, user, {ticket.employee_id})
    now = datetime.now(UTC)
    summary = _summary(ticket, category, employees.get(ticket.employee_id), users, now)

    thread = []
    for comment in comments:
        author = users.get(comment.author_user_id) if comment.author_user_id else None
        thread.append(
            CommentRead(
                id=comment.id,
                author_user_id=comment.author_user_id,
                author_name=author.full_name if author else None,
                body=comment.body,
                is_internal=comment.is_internal,
                is_system=comment.is_system,
                is_from_requester=author is not None
                and author.employee_id is not None
                and author.employee_id == ticket.employee_id,
                created_at=comment.created_at,
                attachments=by_comment.get(comment.id, []),
            )
        )

    terminal = ticket.status in rules.TERMINAL_STATUSES
    requester = role == "requester"
    reopenable = requester and rules.can_reopen(
        status=ticket.status, resolved_at=ticket.resolved_at, rated_at=ticket.rated_at, now=now
    )
    return TicketDetail(
        **summary.model_dump(),
        description=ticket.description,
        cancelled_at=ticket.cancelled_at,
        reopened_count=ticket.reopened_count,
        satisfaction_comment=ticket.satisfaction_comment,
        rated_at=ticket.rated_at,
        attachments=on_ticket,
        comments=thread,
        viewer=role,
        can_reply=not terminal
        and not (requester and ticket.status is HelpdeskTicketStatus.RESOLVED),
        can_add_internal_note=not requester and not terminal,
        can_cancel=requester and ticket.status in rules.ACTIVE_STATUSES,
        can_reopen=reopenable,
        reopen_until=(ticket.resolved_at + rules.REOPEN_WINDOW) if reopenable else None,
        can_rate=requester
        and ticket.resolved_at is not None
        and ticket.rated_at is None
        and ticket.status
        in {HelpdeskTicketStatus.RESOLVED, HelpdeskTicketStatus.CLOSED},
        can_manage=not requester and not terminal,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Files
# ═══════════════════════════════════════════════════════════════════════════

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._\-()]+")


def safe_file_name(filename: str | None, extension: str) -> str:
    """A display name from the client's filename: no path, no odd characters,
    and the extension the bytes say rather than the one the name claims."""
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = _UNSAFE_NAME.sub("_", stem).strip(" ._") or "attachment"
    return f"{stem[:120]}{extension}"


@dataclass(slots=True)
class _Stored:
    key: str
    name: str
    content_type: str
    size: int


async def _store(user: User, data: bytes, filename: str | None) -> _Stored:
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"The file is larger than the {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                "limit."
            ),
        )
    try:
        kind = validate_upload(data, max_bytes=settings.MAX_UPLOAD_BYTES)
    except FileRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    storage = get_storage()
    key = storage.new_key(user.tenant_id, "helpdesk", kind.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return _Stored(key, safe_file_name(filename, kind.extension), kind.mime_type, len(data))


async def _discard(stored: _Stored | None) -> None:
    """The row did not land, so the file must not linger unreferenced."""
    if stored is not None:
        with contextlib.suppress(StorageError):
            await get_storage().delete(stored.key)


def _attach(
    user: User, ticket: HelpdeskTicket, stored: _Stored, comment_id: uuid.UUID | None
) -> HelpdeskAttachment:
    return HelpdeskAttachment(
        tenant_id=user.tenant_id,
        ticket_id=ticket.id,
        comment_id=comment_id,
        uploaded_by_user_id=user.id,
        storage_key=stored.key,
        file_name=stored.name,
        content_type=stored.content_type,
        size_bytes=stored.size,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Notifications
# ═══════════════════════════════════════════════════════════════════════════


def _payload(ticket: HelpdeskTicket, category: HelpdeskCategory, **extra: str) -> dict[str, str]:
    return {
        "ticket_number": ticket.ticket_number,
        "subject": "Confidential ticket" if ticket.is_confidential else ticket.subject,
        "category": category.name,
        **extra,
    }


async def _requester_user_ids(
    session: AsyncSession, user: User, ticket: HelpdeskTicket
) -> list[uuid.UUID]:
    rows = await session.scalars(
        tenant_scoped(
            select(User.id).where(
                User.employee_id == ticket.employee_id, User.is_active.is_(True)
            ),
            User,
            user,
        )
    )
    return list(rows.all())


async def _routing_user_ids(
    session: AsyncSession, user: User, ticket: HelpdeskTicket, category: HelpdeskCategory
) -> list[uuid.UUID]:
    """Who hears about a ticket nobody holds yet: its category owner, else HR —
    only tenant and HR admins for a confidential one."""
    if category.owner_user_id is not None:
        owner = await session.scalar(
            tenant_scoped(
                select(User).where(
                    User.id == category.owner_user_id, User.is_active.is_(True)
                ),
                User,
                user,
            )
        )
        if owner is not None and owner.employee_id != ticket.employee_id:
            return [owner.id]
    roles = ADMIN_ROLES if ticket.is_confidential else permissions.HR_ROLES
    stmt = tenant_scoped(
        select(User.id).where(User.role.in_(roles), User.is_active.is_(True)), User, user
    )
    stmt = stmt.where(or_(User.employee_id.is_(None), User.employee_id != ticket.employee_id))
    return list((await session.scalars(stmt)).all())


async def _send(
    session: AsyncSession,
    user: User,
    event_key: str,
    recipients: list[uuid.UUID],
    payload: dict[str, str],
) -> None:
    # Nobody is told about what they just did themselves.
    targets = [r for r in dict.fromkeys(recipients) if r != user.id]
    if targets:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=event_key,
            recipient_user_ids=targets,
            payload=payload,
            commit=False,
        )


def _comment(
    user: User,
    ticket: HelpdeskTicket,
    body: str,
    *,
    internal: bool = False,
    system: bool = False,
) -> HelpdeskComment:
    """A line in the thread, stamped now.

    Stamped from the clock rather than the database's `now()`, which is the
    transaction's start: a note and the status line after it, written in one
    transaction, would otherwise share a timestamp and sort at random.
    """
    return HelpdeskComment(
        tenant_id=user.tenant_id,
        ticket_id=ticket.id,
        author_user_id=user.id,
        body=body,
        is_internal=internal,
        is_system=system,
        created_at=datetime.now(UTC),
    )


def _system_line(user: User, ticket: HelpdeskTicket, body: str) -> HelpdeskComment:
    return _comment(user, ticket, body, system=True)


async def _audit(
    session: AsyncSession,
    user: User,
    ticket: HelpdeskTicket,
    action: AuditAction,
    summary: str,
    changes: dict[str, Any],
    request: Request | None,
    *,
    entity_type: str = "helpdesk_ticket",
    entity_id: uuid.UUID | None = None,
) -> None:
    # Ticket text is never copied into the audit trail: a grievance's words
    # belong on the ticket, readable by the people allowed to read it.
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id or ticket.id,
        actor=user,
        changes={"ticket_number": ticket.ticket_number, **changes},
        summary=summary,
        request=request,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Raising a ticket
# ═══════════════════════════════════════════════════════════════════════════


async def _next_number(session: AsyncSession, user: User) -> int:
    """The next ticket number for this tenant, under a row lock.

    Insert-if-missing first (the database settles the race when two first
    tickets arrive together), then lock and increment. Must run inside the
    caller's transaction; the caller commits.
    """
    await session.execute(
        pg_insert(HelpdeskSequence)
        .values(id=uuid.uuid4(), tenant_id=user.tenant_id, last_value=0)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    sequence = await session.scalar(
        tenant_scoped(select(HelpdeskSequence), HelpdeskSequence, user).with_for_update()
    )
    if sequence is None:  # pragma: no cover - the upsert above guarantees a row
        raise RuntimeError("Helpdesk sequence row vanished between insert and lock")
    sequence.last_value += 1
    return sequence.last_value


async def create_ticket(
    session: AsyncSession,
    user: User,
    *,
    category_id: uuid.UUID,
    subject: str,
    description: str,
    priority: HelpdeskPriority = HelpdeskPriority.MEDIUM,
    employee_id: uuid.UUID | None = None,
    file_data: bytes | None = None,
    file_name: str | None = None,
    request: Request | None = None,
) -> TicketSummary:
    """Raise a ticket — for yourself, or, for HR, on an employee's behalf.

    A category with an owner is handed straight to them; one without lands in
    HR's shared queue unassigned.
    """
    employee_id = permissions.resolve_employee(
        user, employee_id, detail="You can raise a helpdesk ticket only for yourself."
    )
    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == employee_id, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    category = await session.scalar(
        tenant_scoped(
            select(HelpdeskCategory).where(HelpdeskCategory.id == category_id),
            HelpdeskCategory,
            user,
        )
    )
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    if not category.active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{category.name}' is no longer taking new tickets. Pick another category.",
        )
    subject, description = subject.strip(), description.strip()
    if not subject or not description:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A ticket needs a subject and a description.",
        )

    stored = await _store(user, file_data, file_name) if file_data else None
    try:
        now = datetime.now(UTC)
        seq = await _next_number(session, user)

        owner = None
        if category.owner_user_id is not None:
            owner = await session.scalar(
                tenant_scoped(
                    select(User).where(User.id == category.owner_user_id), User, user
                )
            )
        ticket = HelpdeskTicket(
            tenant_id=user.tenant_id,
            ticket_seq=seq,
            ticket_number=rules.format_ticket_number(seq),
            employee_id=employee.id,
            raised_by_user_id=user.id,
            category_id=category.id,
            subject=subject,
            description=description,
            priority=priority,
            status=HelpdeskTicketStatus.OPEN,
            is_confidential=category.is_confidential,
            sla_hours=category.sla_hours,
            due_at=rules.compute_due_at(now, category.sla_hours),
            last_activity_at=now,
            created_at=now,
        )
        if owner is not None and _eligible_assignee(owner, ticket, owner.id):
            ticket.assignee_user_id = owner.id
        session.add(ticket)
        await session.flush()
        if stored is not None:
            session.add(_attach(user, ticket, stored, None))

        on_behalf = user.employee_id != employee.id
        await _audit(
            session,
            user,
            ticket,
            AuditAction.CREATE,
            f"Raised {ticket.ticket_number} in '{category.name}'"
            + (f" on behalf of {employee.employee_code}" if on_behalf else ""),
            {
                "category": category.name,
                "priority": priority.value,
                "employee_code": employee.employee_code,
                "on_behalf": on_behalf,
                "confidential": ticket.is_confidential,
                "has_attachment": stored is not None,
            },
            request,
        )
        recipients = (
            [ticket.assignee_user_id]
            if ticket.assignee_user_id
            else await _routing_user_ids(session, user, ticket, category)
        )
        requester_ids = set(await _requester_user_ids(session, user, ticket))
        await _send(
            session,
            user,
            "helpdesk.ticket.created",
            [r for r in recipients if r not in requester_ids],
            _payload(ticket, category, employee_name=employee.full_name),
        )
        await session.commit()
    except BaseException:
        await _discard(stored)
        raise

    await session.refresh(ticket)
    return (await _summaries(session, user, [(ticket, category)]))[0]


# ═══════════════════════════════════════════════════════════════════════════
# Reads
# ═══════════════════════════════════════════════════════════════════════════

_STATUS_GROUPS: dict[str, frozenset[HelpdeskTicketStatus]] = {
    "active": rules.ACTIVE_STATUSES,
    "done": rules.TERMINAL_STATUSES,
}


def _status_filter(stmt: Select, status_filter: str | None) -> Select:
    if not status_filter or status_filter == "any":
        return stmt
    if status_filter in _STATUS_GROUPS:
        return stmt.where(HelpdeskTicket.status.in_(_STATUS_GROUPS[status_filter]))
    try:
        wanted = HelpdeskTicketStatus(status_filter)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{status_filter}' is not a ticket status.",
        ) from exc
    return stmt.where(HelpdeskTicket.status == wanted)


def _with_category(stmt: Select) -> Select:
    return stmt.join(HelpdeskCategory, HelpdeskCategory.id == HelpdeskTicket.category_id)


async def list_my_tickets(
    session: AsyncSession, user: User, *, status_filter: str | None = None
) -> list[TicketSummary]:
    """The caller's own tickets, most recently active first."""
    employee_id = user.employee_id
    if employee_id is None:
        return []
    stmt = tenant_scoped(
        _with_category(select(HelpdeskTicket, HelpdeskCategory))
        .where(HelpdeskTicket.employee_id == employee_id)
        .order_by(HelpdeskTicket.last_activity_at.desc()),
        HelpdeskTicket,
        user,
    )
    stmt = _status_filter(stmt, status_filter)
    rows = [(t, c) for t, c in (await session.execute(stmt)).all()]
    return await _summaries(session, user, rows)


async def my_summary(session: AsyncSession, user: User) -> MySummary:
    """The caller's open tickets for the home card, and whether they work the queue."""
    agent = await is_agent(session, user)
    employee_id = user.employee_id
    if employee_id is None:
        return MySummary(open_count=0, waiting_on_you=0, latest=[], is_agent=agent)

    counts = (
        await session.execute(
            tenant_scoped(
                select(
                    func.count().filter(HelpdeskTicket.status.in_(rules.ACTIVE_STATUSES)),
                    func.count().filter(
                        HelpdeskTicket.status == HelpdeskTicketStatus.WAITING_ON_EMPLOYEE
                    ),
                ).where(HelpdeskTicket.employee_id == employee_id),
                HelpdeskTicket,
                user,
            )
        )
    ).one()
    latest_stmt = tenant_scoped(
        _with_category(select(HelpdeskTicket, HelpdeskCategory))
        .where(HelpdeskTicket.employee_id == employee_id)
        .order_by(
            case((HelpdeskTicket.status.in_(rules.ACTIVE_STATUSES), 0), else_=1),
            HelpdeskTicket.last_activity_at.desc(),
        )
        .limit(3),
        HelpdeskTicket,
        user,
    )
    rows = [(t, c) for t, c in (await session.execute(latest_stmt)).all()]
    return MySummary(
        open_count=counts[0] or 0,
        waiting_on_you=counts[1] or 0,
        latest=await _summaries(session, user, rows),
        is_agent=agent,
    )


async def get_ticket(session: AsyncSession, user: User, ticket_id: uuid.UUID) -> TicketDetail:
    ticket, category, role = await _visible_ticket(session, user, ticket_id)
    return await _detail(session, user, ticket, category, role)


async def download_attachment(
    session: AsyncSession,
    user: User,
    attachment_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> tuple[str, str, bytes]:
    """A file on a ticket the caller may see. One on an internal note is a 404
    to the requester. Every download is audited."""
    row = await session.scalar(
        tenant_scoped(
            select(HelpdeskAttachment).where(HelpdeskAttachment.id == attachment_id),
            HelpdeskAttachment,
            user,
        )
    )
    missing = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if row is None:
        raise missing
    try:
        ticket, _category, role = await _visible_ticket(session, user, row.ticket_id)
    except HTTPException:
        raise missing from None
    if role == "requester" and row.comment_id is not None:
        internal = await session.scalar(
            tenant_scoped(
                select(HelpdeskComment.is_internal).where(HelpdeskComment.id == row.comment_id),
                HelpdeskComment,
                user,
            )
        )
        if internal:
            raise missing

    try:
        data = await get_storage().get(row.storage_key)
    except StorageError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The stored file is missing. Ask for it to be attached again.",
        )
    await _audit(
        session,
        user,
        ticket,
        AuditAction.EXPORT,
        f"Downloaded an attachment on {ticket.ticket_number}",
        {"file_name": row.file_name},
        request,
        entity_type="helpdesk_attachment",
        entity_id=row.id,
    )
    await session.commit()
    return row.file_name, row.content_type, data


# ═══════════════════════════════════════════════════════════════════════════
# The conversation
# ═══════════════════════════════════════════════════════════════════════════


async def add_comment(
    session: AsyncSession,
    user: User,
    ticket_id: uuid.UUID,
    *,
    body: str,
    is_internal: bool = False,
    file_data: bytes | None = None,
    file_name: str | None = None,
    request: Request | None = None,
) -> TicketDetail:
    """A reply, or for an agent an internal note.

    An agent's first public reply is the ticket's first response and moves an
    open ticket to in progress. The employee answering a ticket that was
    waiting on them moves it back to in progress.
    """
    ticket, category, role = await _visible_ticket(session, user, ticket_id, lock=True)
    body = body.strip()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Write a message."
        )
    if ticket.status in rules.TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{ticket.ticket_number} is {STATUS_LABEL[ticket.status].lower()}. "
                "Raise a new ticket and mention this number."
            ),
        )
    if role == "requester":
        if is_internal:
            raise permissions.forbidden("Internal notes are for the people working the ticket.")
        if ticket.status is HelpdeskTicketStatus.RESOLVED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This ticket is resolved. Reopen it if something is still wrong.",
            )

    stored = await _store(user, file_data, file_name) if file_data else None
    try:
        now = datetime.now(UTC)
        comment = _comment(user, ticket, body, internal=is_internal)
        session.add(comment)
        await session.flush()
        if stored is not None:
            session.add(_attach(user, ticket, stored, comment.id))

        before = ticket.status
        if role == "agent" and not is_internal:
            if ticket.first_response_at is None:
                ticket.first_response_at = now
            if ticket.status is HelpdeskTicketStatus.OPEN:
                ticket.status = HelpdeskTicketStatus.IN_PROGRESS
        if role == "requester" and ticket.status is HelpdeskTicketStatus.WAITING_ON_EMPLOYEE:
            ticket.status = HelpdeskTicketStatus.IN_PROGRESS
        ticket.last_activity_at = now

        await _audit(
            session,
            user,
            ticket,
            AuditAction.CREATE,
            f"{'Internal note' if is_internal else 'Reply'} on {ticket.ticket_number}",
            {
                "internal": is_internal,
                "by": role,
                "has_attachment": stored is not None,
                **(
                    {"status": {"from": before.value, "to": ticket.status.value}}
                    if before is not ticket.status
                    else {}
                ),
            },
            request,
            entity_type="helpdesk_comment",
            entity_id=comment.id,
        )
        if not is_internal:
            payload = _payload(ticket, category, author_name=user.full_name)
            if role == "agent":
                recipients = await _requester_user_ids(session, user, ticket)
            elif ticket.assignee_user_id is not None:
                recipients = [ticket.assignee_user_id]
            else:
                recipients = await _routing_user_ids(session, user, ticket, category)
            await _send(session, user, "helpdesk.ticket.replied", recipients, payload)
        await session.commit()
    except BaseException:
        await _discard(stored)
        raise
    return await get_ticket(session, user, ticket_id)


# ═══════════════════════════════════════════════════════════════════════════
# The requester's actions
# ═══════════════════════════════════════════════════════════════════════════


async def cancel_ticket(
    session: AsyncSession,
    user: User,
    ticket_id: uuid.UUID,
    *,
    reason: str | None = None,
    request: Request | None = None,
) -> TicketDetail:
    """Withdraw a ticket that has not been resolved yet."""
    ticket, _category = await _requester_ticket(session, user, ticket_id)
    if ticket.status not in rules.ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {STATUS_LABEL[ticket.status].lower()} ticket cannot be cancelled.",
        )
    now = datetime.now(UTC)
    before = ticket.status
    ticket.status = HelpdeskTicketStatus.CANCELLED
    ticket.cancelled_at = now
    ticket.last_activity_at = now
    if reason:
        session.add(_comment(user, ticket, reason))
    session.add(_system_line(user, ticket, "Cancelled by the employee."))
    await _audit(
        session,
        user,
        ticket,
        AuditAction.UPDATE,
        f"Cancelled {ticket.ticket_number}",
        {"status": {"from": before.value, "to": ticket.status.value}},
        request,
    )
    await session.commit()
    return await get_ticket(session, user, ticket_id)


async def reopen_ticket(
    session: AsyncSession,
    user: User,
    ticket_id: uuid.UUID,
    *,
    reason: str,
    request: Request | None = None,
) -> TicketDetail:
    """Reopen within seven days of resolution, saying what is still wrong. The
    SLA clock starts again from now."""
    ticket, category = await _requester_ticket(session, user, ticket_id)
    now = datetime.now(UTC)
    if not rules.can_reopen(
        status=ticket.status, resolved_at=ticket.resolved_at, rated_at=ticket.rated_at, now=now
    ):
        if ticket.rated_at is not None:
            detail = (
                "You rated this ticket, which closed it. Raise a new ticket and mention "
                f"{ticket.ticket_number}."
            )
        elif ticket.resolved_at is not None and ticket.status in {
            HelpdeskTicketStatus.RESOLVED,
            HelpdeskTicketStatus.CLOSED,
        }:
            detail = (
                "A ticket can be reopened for 7 days after it is resolved. Raise a new "
                f"ticket and mention {ticket.ticket_number}."
            )
        else:
            detail = f"A {STATUS_LABEL[ticket.status].lower()} ticket cannot be reopened."
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    before = ticket.status
    ticket.status = HelpdeskTicketStatus.OPEN
    ticket.resolved_at = None
    ticket.closed_at = None
    ticket.reopened_count += 1
    ticket.due_at = rules.compute_due_at(now, ticket.sla_hours)
    ticket.last_activity_at = now
    session.add(_comment(user, ticket, reason))
    session.add(_system_line(user, ticket, "Reopened by the employee."))
    await _audit(
        session,
        user,
        ticket,
        AuditAction.UPDATE,
        f"Reopened {ticket.ticket_number}",
        {"status": {"from": before.value, "to": ticket.status.value}},
        request,
    )
    recipients = (
        [ticket.assignee_user_id]
        if ticket.assignee_user_id
        else await _routing_user_ids(session, user, ticket, category)
    )
    await _send(
        session,
        user,
        "helpdesk.ticket.replied",
        recipients,
        _payload(ticket, category, author_name=user.full_name),
    )
    await session.commit()
    return await get_ticket(session, user, ticket_id)


async def rate_ticket(
    session: AsyncSession,
    user: User,
    ticket_id: uuid.UUID,
    *,
    rating: int,
    comment: str | None = None,
    request: Request | None = None,
) -> TicketDetail:
    """Rate a resolved ticket, once. Rating accepts the resolution and closes it."""
    ticket, _category = await _requester_ticket(session, user, ticket_id)
    if ticket.rated_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="You have already rated this ticket."
        )
    if ticket.resolved_at is None or ticket.status not in {
        HelpdeskTicketStatus.RESOLVED,
        HelpdeskTicketStatus.CLOSED,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A ticket can be rated once it is resolved.",
        )
    now = datetime.now(UTC)
    before = ticket.status
    ticket.satisfaction_rating = rating
    ticket.satisfaction_comment = comment
    ticket.rated_at = now
    if ticket.status is HelpdeskTicketStatus.RESOLVED:
        ticket.status = HelpdeskTicketStatus.CLOSED
        ticket.closed_at = now
    ticket.last_activity_at = now
    session.add(_system_line(user, ticket, f"Rated {rating} out of 5 by the employee."))
    await _audit(
        session,
        user,
        ticket,
        AuditAction.UPDATE,
        f"Rated {ticket.ticket_number} {rating}/5",
        {
            "rating": rating,
            **(
                {"status": {"from": before.value, "to": ticket.status.value}}
                if before is not ticket.status
                else {}
            ),
        },
        request,
    )
    await session.commit()
    return await get_ticket(session, user, ticket_id)


# ═══════════════════════════════════════════════════════════════════════════
# The agent's actions
# ═══════════════════════════════════════════════════════════════════════════


async def ticket_assignees(
    session: AsyncSession, user: User, ticket_id: uuid.UUID
) -> list[AgentOption]:
    """Who this ticket may be handed to."""
    ticket, category = await _agent_ticket(session, user, ticket_id, lock=False)
    roles = ADMIN_ROLES if ticket.is_confidential else permissions.HR_ROLES
    conditions: list[Any] = [User.role.in_(roles)]
    if category.owner_user_id is not None:
        conditions.append(User.id == category.owner_user_id)
    candidates = (
        await session.scalars(
            tenant_scoped(
                select(User)
                .where(User.is_active.is_(True), or_(*conditions))
                .order_by(User.full_name),
                User,
                user,
            )
        )
    ).all()
    return [
        AgentOption(
            user_id=c.id,
            full_name=c.full_name,
            role=c.role,
            is_category_owner=c.id == category.owner_user_id,
        )
        for c in candidates
        if _eligible_assignee(c, ticket, category.owner_user_id)
    ]


async def assign_ticket(
    session: AsyncSession,
    user: User,
    ticket_id: uuid.UUID,
    *,
    assignee_user_id: uuid.UUID | None,
    request: Request | None = None,
) -> TicketDetail:
    ticket, category = await _agent_ticket(session, user, ticket_id)
    if ticket.status in rules.TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {STATUS_LABEL[ticket.status].lower()} ticket cannot be reassigned.",
        )
    if assignee_user_id == ticket.assignee_user_id:
        return await get_ticket(session, user, ticket_id)

    assignee: User | None = None
    if assignee_user_id is not None:
        assignee = await session.scalar(
            tenant_scoped(select(User).where(User.id == assignee_user_id), User, user)
        )
        if assignee is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="That person was not found."
            )
        if not _eligible_assignee(assignee, ticket, category.owner_user_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{assignee.full_name} cannot work this ticket. "
                    + (
                        "A confidential ticket goes only to its category owner or a "
                        "tenant or HR admin."
                        if ticket.is_confidential
                        else "Pick someone in HR or the category owner."
                    )
                ),
            )

    previous = ticket.assignee_user_id
    now = datetime.now(UTC)
    ticket.assignee_user_id = assignee.id if assignee else None
    ticket.last_activity_at = now
    session.add(
        _system_line(
            user,
            ticket,
            f"Assigned to {assignee.full_name}." if assignee else "Unassigned.",
        )
    )
    await _audit(
        session,
        user,
        ticket,
        AuditAction.UPDATE,
        f"{'Assigned' if assignee else 'Unassigned'} {ticket.ticket_number}",
        {
            "assignee_user_id": {
                "from": str(previous) if previous else None,
                "to": str(assignee.id) if assignee else None,
            }
        },
        request,
    )
    if assignee is not None:
        await _send(
            session,
            user,
            "helpdesk.ticket.assigned",
            [assignee.id],
            _payload(ticket, category, actor_name=user.full_name),
        )
    await session.commit()
    return await get_ticket(session, user, ticket_id)


async def update_ticket(
    session: AsyncSession,
    user: User,
    ticket_id: uuid.UUID,
    payload: TicketAgentUpdate,
    *,
    request: Request | None = None,
) -> TicketDetail:
    """Change status or priority, optionally with a public note — the
    resolution, or why a ticket is being closed without one."""
    ticket, category = await _agent_ticket(session, user, ticket_id)
    if ticket.status in rules.TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{ticket.ticket_number} is {STATUS_LABEL[ticket.status].lower()}.",
        )
    new_status = payload.status if payload.status is not ticket.status else None
    new_priority = payload.priority if payload.priority is not ticket.priority else None
    if new_status is None and new_priority is None and not payload.note:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Nothing to change."
        )
    if new_status is not None:
        if not rules.agent_may_move(ticket.status, new_status):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"A ticket that is {STATUS_LABEL[ticket.status].lower()} cannot be moved "
                    f"to {STATUS_LABEL[new_status].lower()}."
                ),
            )
        if (
            new_status is HelpdeskTicketStatus.CLOSED
            and ticket.status is not HelpdeskTicketStatus.RESOLVED
            and not payload.note
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Say why you are closing a ticket that was not resolved.",
            )

    now = datetime.now(UTC)
    changes: dict[str, Any] = {}
    if payload.note:
        session.add(_comment(user, ticket, payload.note))
        if ticket.first_response_at is None:
            ticket.first_response_at = now
        changes["note"] = True
    if new_priority is not None:
        changes["priority"] = {"from": ticket.priority.value, "to": new_priority.value}
        session.add(
            _system_line(
                user,
                ticket,
                f"Priority changed from {PRIORITY_LABEL[ticket.priority]} to "
                f"{PRIORITY_LABEL[new_priority]}.",
            )
        )
        ticket.priority = new_priority
    if new_status is not None:
        changes["status"] = {"from": ticket.status.value, "to": new_status.value}
        session.add(
            _system_line(
                user,
                ticket,
                f"Status changed from {STATUS_LABEL[ticket.status]} to "
                f"{STATUS_LABEL[new_status]}.",
            )
        )
        if new_status is HelpdeskTicketStatus.RESOLVED:
            ticket.resolved_at = now
        elif new_status is HelpdeskTicketStatus.CLOSED:
            ticket.closed_at = now
        elif ticket.status is HelpdeskTicketStatus.RESOLVED:
            # Taken back from resolved: it is being worked again.
            ticket.resolved_at = None
        ticket.status = new_status
    ticket.last_activity_at = now

    await _audit(
        session,
        user,
        ticket,
        AuditAction.UPDATE,
        f"Updated {ticket.ticket_number}",
        changes,
        request,
    )
    requesters = await _requester_user_ids(session, user, ticket)
    if new_status is HelpdeskTicketStatus.RESOLVED:
        await _send(
            session,
            user,
            "helpdesk.ticket.resolved",
            requesters,
            _payload(ticket, category, actor_name=user.full_name),
        )
    elif payload.note:
        await _send(
            session,
            user,
            "helpdesk.ticket.replied",
            requesters,
            _payload(ticket, category, author_name=user.full_name),
        )
    await session.commit()
    return await get_ticket(session, user, ticket_id)


# ═══════════════════════════════════════════════════════════════════════════
# The queue
# ═══════════════════════════════════════════════════════════════════════════


async def list_queue(
    session: AsyncSession,
    user: User,
    *,
    status_filter: str | None = None,
    category_id: uuid.UUID | None = None,
    assignee: str | None = None,
    priority: HelpdeskPriority | None = None,
    overdue: bool = False,
    search: str | None = None,
    limit: int = 200,
) -> list[TicketSummary]:
    """Tickets the caller may work, most urgent first: active before done,
    earliest deadline first, then most recent activity.

    `assignee` is `me`, `unassigned` or a user id.
    """
    owned = await _require_agent(session, user)
    stmt = tenant_scoped(
        _with_category(select(HelpdeskTicket, HelpdeskCategory)).join(
            Employee, Employee.id == HelpdeskTicket.employee_id
        ),
        HelpdeskTicket,
        user,
    )
    stmt = _agent_scope(stmt, user, owned)
    stmt = _status_filter(stmt, status_filter)
    if category_id is not None:
        stmt = stmt.where(HelpdeskTicket.category_id == category_id)
    if priority is not None:
        stmt = stmt.where(HelpdeskTicket.priority == priority)
    if assignee:
        if assignee == "me":
            stmt = stmt.where(HelpdeskTicket.assignee_user_id == user.id)
        elif assignee == "unassigned":
            stmt = stmt.where(HelpdeskTicket.assignee_user_id.is_(None))
        else:
            try:
                stmt = stmt.where(HelpdeskTicket.assignee_user_id == uuid.UUID(assignee))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="assignee must be 'me', 'unassigned' or a user id.",
                ) from exc
    if overdue:
        stmt = stmt.where(
            HelpdeskTicket.status.in_(rules.ACTIVE_STATUSES),
            HelpdeskTicket.due_at < datetime.now(UTC),
        )
    term = (search or "").strip()
    if term:
        escaped = term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        like = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                HelpdeskTicket.subject.ilike(like),
                HelpdeskTicket.ticket_number.ilike(like),
                Employee.first_name.ilike(like),
                Employee.last_name.ilike(like),
                Employee.display_name.ilike(like),
                Employee.employee_code.ilike(like),
            )
        )
    stmt = stmt.order_by(
        case((HelpdeskTicket.status.in_(rules.ACTIVE_STATUSES), 0), else_=1),
        HelpdeskTicket.due_at,
        HelpdeskTicket.last_activity_at.desc(),
    ).limit(limit)
    rows = [(t, c) for t, c in (await session.execute(stmt)).all()]
    return await _summaries(session, user, rows)


def _month_start(now: datetime) -> datetime:
    local = now.astimezone(INDIA)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _hours(seconds: Any) -> float | None:
    return None if seconds is None else round(float(seconds) / 3600, 1)


async def queue_summary(session: AsyncSession, user: User) -> QueueSummary:
    """The queue's figures over what the caller may work.

    Open, overdue, due soon and unassigned are as of now. The rest are this
    calendar month in India time: tickets resolved, the average hours to a first
    response for tickets first answered this month, the average hours to
    resolution for tickets resolved this month, and the average rating given
    this month — each with the count it is an average of.
    """
    owned = await _require_agent(session, user)
    now = datetime.now(UTC)
    start = _month_start(now)

    def scoped(stmt: Select) -> Select:
        return _agent_scope(tenant_scoped(stmt, HelpdeskTicket, user), user, owned)

    active = (
        await session.execute(
            scoped(
                select(
                    HelpdeskTicket.due_at,
                    HelpdeskTicket.sla_hours,
                    HelpdeskTicket.assignee_user_id,
                ).where(HelpdeskTicket.status.in_(rules.ACTIVE_STATUSES))
            )
        )
    ).all()
    overdue = sum(1 for row in active if now > row.due_at)
    due_soon = sum(
        1
        for row in active
        if now <= row.due_at and row.due_at - now <= rules.due_soon_threshold(row.sla_hours)
    )
    unassigned = sum(1 for row in active if row.assignee_user_id is None)

    resolved = (
        await session.execute(
            scoped(
                select(
                    func.count(),
                    func.avg(
                        func.extract(
                            "epoch", HelpdeskTicket.resolved_at - HelpdeskTicket.created_at
                        )
                    ),
                ).where(HelpdeskTicket.resolved_at >= start)
            )
        )
    ).one()
    first = (
        await session.execute(
            scoped(
                select(
                    func.count(),
                    func.avg(
                        func.extract(
                            "epoch", HelpdeskTicket.first_response_at - HelpdeskTicket.created_at
                        )
                    ),
                ).where(HelpdeskTicket.first_response_at >= start)
            )
        )
    ).one()
    rated = (
        await session.execute(
            scoped(
                select(func.count(), func.avg(HelpdeskTicket.satisfaction_rating)).where(
                    HelpdeskTicket.rated_at >= start
                )
            )
        )
    ).one()

    return QueueSummary(
        open=len(active),
        overdue=overdue,
        due_soon=due_soon,
        unassigned=unassigned,
        period_start=start.astimezone(INDIA).date(),
        resolved_this_month=resolved[0] or 0,
        avg_first_response_hours=_hours(first[1]),
        first_response_sample=first[0] or 0,
        avg_resolution_hours=_hours(resolved[1]),
        resolution_sample=resolved[0] or 0,
        avg_satisfaction=None if rated[1] is None else round(float(rated[1]), 1),
        satisfaction_sample=rated[0] or 0,
    )


async def list_agents(session: AsyncSession, user: User) -> list[AgentOption]:
    """People who work the helpdesk: HR, and anyone who owns a category."""
    await _require_agent(session, user)
    owners = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(HelpdeskCategory.owner_user_id).where(
                        HelpdeskCategory.owner_user_id.is_not(None)
                    ),
                    HelpdeskCategory,
                    user,
                )
            )
        ).all()
    )
    conditions: list[Any] = [User.role.in_(permissions.HR_ROLES)]
    if owners:
        conditions.append(User.id.in_(owners))
    rows = (
        await session.scalars(
            tenant_scoped(
                select(User)
                .where(User.is_active.is_(True), or_(*conditions))
                .order_by(User.full_name),
                User,
                user,
            )
        )
    ).all()
    return [
        AgentOption(
            user_id=row.id,
            full_name=row.full_name,
            role=row.role,
            is_category_owner=row.id in owners,
        )
        for row in rows
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled
# ═══════════════════════════════════════════════════════════════════════════


async def auto_close_resolved(
    session: AsyncSession, actor: User, *, now: datetime | None = None
) -> dict[str, Any]:
    """Close tickets resolved more than seven days ago that nobody reopened or
    rated. Runs as the tenant's system actor, so the audit rows name no person."""
    now = now or datetime.now(UTC)
    cutoff = now - rules.AUTO_CLOSE_AFTER
    tickets = (
        await session.scalars(
            tenant_scoped(
                select(HelpdeskTicket)
                .where(
                    HelpdeskTicket.status == HelpdeskTicketStatus.RESOLVED,
                    HelpdeskTicket.resolved_at <= cutoff,
                )
                .with_for_update(skip_locked=True),
                HelpdeskTicket,
                actor,
            )
        )
    ).all()
    for ticket in tickets:
        ticket.status = HelpdeskTicketStatus.CLOSED
        ticket.closed_at = now
        ticket.last_activity_at = now
        session.add(
            HelpdeskComment(
                tenant_id=actor.tenant_id,
                ticket_id=ticket.id,
                author_user_id=None,
                body="Closed automatically 7 days after it was resolved.",
                is_system=True,
                created_at=now,
            )
        )
        await audit.record(
            session,
            tenant_id=actor.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="helpdesk_ticket",
            entity_id=ticket.id,
            actor_user_id=None,
            actor_type="system",
            changes={
                "ticket_number": ticket.ticket_number,
                "status": {"from": "resolved", "to": "closed"},
            },
            summary=f"Closed {ticket.ticket_number} automatically, 7 days after resolution",
        )
    await session.commit()
    return {"closed": len(tickets), "as_of": now.astimezone(INDIA).date().isoformat()}

