"""A customer approves or rejects a consultant's week from a link in an email.

When a consultant on the customer route submits a week, the customer contact
gets an email with the hours and two buttons, Approve and Reject. The contact
has no login, so the link is the credential: a random token that is shown once
(in the email), stored only as a SHA-256, valid for 14 days and good for one
decision.

The page the link opens never shows a rate or an amount, only hours. GET only
shows (mail scanners open links); the decision is a POST.

A link stops applying the moment the week it was sent for changes: edited,
sent again, decided by someone at the company, or reopened. Its token then
reads as "changed" (or "decided") and cannot decide anything.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_filter
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.timesheets import (
    Project,
    ProjectTask,
    Timesheet,
    TimesheetApprovalLink,
    TimesheetEntry,
    TimesheetStatus,
)
from app.models.user import User
from app.services import audit
from app.services.auth import web_app_url
from app.services.hiring.public_guard import Limit, throttle
from app.services.notifications import email_brand
from app.services.notifications.dispatch import notify
from app.services.notifications.email_layout import EmailAction

__all__ = [
    "LINK_LIFETIME",
    "contents_hash",
    "decide",
    "deliver",
    "describe",
    "issue_link",
    "revoke_links",
    "send_reminders",
    "send_to_customer",
]

logger = logging.getLogger("askmira.timesheets")

#: How long a customer has to decide.
LINK_LIFETIME = timedelta(days=14)

#: How long after an email goes out before the customer is nudged, and how many nudges.
REMIND_AFTER = timedelta(days=3)
MAX_REMINDERS = 2

READ_LIMIT = Limit("timesheet_link_read", 240, 600)
DECIDE_LIMIT = Limit("timesheet_link_decide", 20, 600)

EVENT_KEY = "timesheets.customer_request"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def contents_hash(entries: list[TimesheetEntry]) -> str:
    """A fingerprint of the hours a customer was shown, whatever order they are kept in."""
    rows = sorted(
        f"{e.work_date.isoformat()}|{e.project_id or ''}|{e.task_id or ''}|"
        f"{Decimal(str(e.hours)):.2f}"
        for e in entries
    )
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _error(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


async def _entries(session: AsyncSession, sheet: Timesheet) -> list[TimesheetEntry]:
    return list(
        (
            await session.scalars(
                select(TimesheetEntry)
                .where(
                    tenant_filter(TimesheetEntry, sheet.tenant_id),
                    TimesheetEntry.timesheet_id == sheet.id,
                )
                .order_by(TimesheetEntry.work_date, TimesheetEntry.created_at)
            )
        ).all()
    )


# ── Sending ────────────────────────────────────────────────────────────────


async def revoke_links(
    session: AsyncSession, tenant_id: uuid.UUID, timesheet_id: uuid.UUID
) -> None:
    """Close every link of this week that is still open. Safe to call twice.

    Callers hold the sheet's lock already (sheet first, then links).
    """
    now = datetime.now(UTC)
    open_links = (
        await session.scalars(
            select(TimesheetApprovalLink).where(
                tenant_filter(TimesheetApprovalLink, tenant_id),
                TimesheetApprovalLink.timesheet_id == timesheet_id,
                TimesheetApprovalLink.revoked_at.is_(None),
            )
        )
    ).all()
    for link in open_links:
        link.revoked_at = now


async def issue_link(
    session: AsyncSession, sheet: Timesheet, employee: Employee
) -> tuple[TimesheetApprovalLink, str]:
    """Close the week's earlier links and mint a new one; flushed, not committed.

    Returns the row and the token. The token exists only here: only its hash is kept.
    """
    email = (employee.customer_contact_email or "").strip()
    if not email:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Add the customer contact's email to this consultant before sending the week "
            "for approval.",
        )
    await revoke_links(session, sheet.tenant_id, sheet.id)
    token = secrets.token_urlsafe(32)
    link = TimesheetApprovalLink(
        tenant_id=sheet.tenant_id,
        timesheet_id=sheet.id,
        token_hash=_token_hash(token),
        contents_hash=contents_hash(await _entries(session, sheet)),
        sent_to_name=(employee.customer_contact_name or "").strip() or None,
        sent_to_email=email,
        expires_at=datetime.now(UTC) + LINK_LIFETIME,
    )
    session.add(link)
    await session.flush()
    return link, token


async def _reply_to(session: AsyncSession, employee: Employee) -> str | None:
    """Who the customer's reply should reach: the internal approver, else HR."""
    from app.models.user import UserRole

    scope = SimpleNamespace(tenant_id=employee.tenant_id)
    if employee.internal_approver_user_id:
        approver = await session.scalar(
            select(User.email).where(
                tenant_filter(User, scope), User.id == employee.internal_approver_user_id
            )
        )
        if approver:
            return approver
    return await session.scalar(
        select(User.email)
        .where(
            tenant_filter(User, scope),
            User.role.in_([UserRole.ADMIN, UserRole.SUB_ADMIN]),
            User.is_active.is_(True),
        )
        .order_by(User.created_at)
        .limit(1)
    )


async def deliver(
    session: AsyncSession, link: TimesheetApprovalLink, token: str, employee: Employee
) -> bool:
    """Email the customer their link. Never raises: a week stands without the email.

    Hours only, never a rate or an amount. Call after the transaction that made
    the link has committed. Commits the `sent_at` stamp when it goes.
    """
    from app.services.notifications.adapters import OutboundMessage
    from app.services.notifications.dispatch import email_adapter_for, email_wording, render

    try:
        # A savepoint: if a lookup fails, only this work is undone and the
        # caller's objects stay loaded.
        async with session.begin_nested():
            sheet = await session.scalar(
                select(Timesheet).where(
                    tenant_filter(Timesheet, link.tenant_id), Timesheet.id == link.timesheet_id
                )
            )
            tenant = await session.scalar(select(Tenant).where(Tenant.id == link.tenant_id))
            if sheet is None or tenant is None:
                return False
            entries = await _entries(session, sheet)
            hours = sum((Decimal(str(e.hours)) for e in entries), Decimal(0))
            base = web_app_url()
            if not base:
                logger.warning(
                    "No web address is set; the customer link for %s was not sent", sheet.id
                )
                return False
            page = f"{base}/timesheet/{token}"
            payload = {
                "company": tenant.name,
                "employee_name": employee.full_name,
                "contact_name": link.sent_to_name or "there",
                "hours": f"{hours.normalize():f}",
                "week": f"{sheet.week_start:%d %b %Y}",
            }
            wording = await email_wording(session, link.tenant_id, EVENT_KEY)
            if wording is None:
                return False
            subject, body = render(wording[0], payload), render(wording[1], payload)
            rendered = await email_brand.compose(
                session,
                link.tenant_id,
                subject=subject,
                body=body,
                audience="letter",
                actions=(
                    EmailAction(label="Approve", url=f"{page}?answer=approve"),
                    EmailAction(label="Reject", url=f"{page}?answer=reject"),
                ),
            )
            adapter, _relay = await email_adapter_for(session, link.tenant_id)
            reply_to = await _reply_to(session, employee)
            await adapter.send(
                OutboundMessage(
                    recipient_name=link.sent_to_name or link.sent_to_email,
                    recipient_email=link.sent_to_email,
                    recipient_phone=None,
                    subject=subject,
                    event_key=EVENT_KEY,
                    reply_to=reply_to,
                    **email_brand.as_message_fields(rendered),
                )
            )
            link.sent_at = datetime.now(UTC)
        await session.commit()
        return True
    except Exception:
        logger.warning(
            "Customer timesheet email for %s did not go", link.timesheet_id, exc_info=True
        )
        with contextlib.suppress(Exception):
            await session.rollback()
        return False


async def send_to_customer(
    session: AsyncSession, user: User, sheet: Timesheet, employee: Employee
) -> TimesheetApprovalLink:
    """Email the customer a fresh link for a week waiting on them; the earlier link closes.

    Only HR or the consultant's internal approver may, and only for a submitted
    customer-route week. Audited.
    """
    from app.services.consultants import can_manage_consultant

    if not await can_manage_consultant(session, user, employee):
        raise permissions.forbidden("Only HR or their approver can send this week again.")
    sheet_id = sheet.id
    locked = await session.scalar(
        select(Timesheet)
        .where(tenant_filter(Timesheet, user), Timesheet.id == sheet_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise _error(status.HTTP_404_NOT_FOUND, "Timesheet not found")
    if locked.status is not TimesheetStatus.SUBMITTED or locked.approval_route != "customer":
        raise _error(
            status.HTTP_409_CONFLICT,
            "Only a submitted week on the customer route can be sent to the customer.",
        )
    link, token = await issue_link(session, locked, employee)
    await audit.record(
        session,
        tenant_id=locked.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="timesheet",
        entity_id=locked.id,
        actor=user,
        changes={"sent_to": link.sent_to_email},
        summary=f"Sent the week of {locked.week_start} to {link.sent_to_email} again",
    )
    await session.commit()
    await deliver(session, link, token, employee)
    return link


async def send_reminders(session: AsyncSession, actor: User, *, now: datetime) -> dict[str, int]:
    """Nudge customers who have not answered: after 3 days, at most twice.

    Each nudge is a fresh email with a new link (the old one closes) that carries
    the count forward. A week whose email never went out is left alone.
    """
    # Plain values: each week commits, which expires the actor object.
    scope = SimpleNamespace(tenant_id=actor.tenant_id)
    actor_id = actor.id
    candidates = (
        await session.execute(
            select(Timesheet.id, Timesheet.employee_id).where(
                tenant_filter(Timesheet, scope),
                Timesheet.status == TimesheetStatus.SUBMITTED,
                Timesheet.approval_route == "customer",
            )
        )
    ).all()
    reminded = 0
    for sheet_id, employee_id in candidates:
        try:
            sent = await _remind_one(session, scope, actor_id, sheet_id, employee_id, now)
        except Exception:
            # One week failing (a removed contact email, a database error) must not
            # stop the rest: leave it for tomorrow and carry on.
            logger.warning("Reminder for timesheet %s did not go", sheet_id, exc_info=True)
            with contextlib.suppress(Exception):
                await session.rollback()
            continue
        reminded += 1 if sent else 0
    return {"reminded": reminded}


async def _remind_one(
    session: AsyncSession,
    scope: Any,
    actor_id: uuid.UUID | None,
    sheet_id: uuid.UUID,
    employee_id: uuid.UUID,
    now: datetime,
) -> bool:
    from app.services.consultants import is_consultant

    # Sheet first, then its links, as everywhere else.
    sheet = await session.scalar(
        select(Timesheet)
        .where(tenant_filter(Timesheet, scope), Timesheet.id == sheet_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        sheet is None
        or sheet.status is not TimesheetStatus.SUBMITTED
        or sheet.approval_route != "customer"
    ):
        await session.rollback()
        return False
    employee = await session.scalar(
        select(Employee).where(tenant_filter(Employee, scope), Employee.id == employee_id)
    )
    newest = await session.scalar(
        select(TimesheetApprovalLink)
        .where(
            tenant_filter(TimesheetApprovalLink, scope),
            TimesheetApprovalLink.timesheet_id == sheet_id,
        )
        .order_by(TimesheetApprovalLink.created_at.desc())
        .limit(1)
    )
    if (
        employee is None
        or not is_consultant(employee)
        or newest is None
        or newest.sent_at is None
        or newest.used_at is not None
        or newest.revoked_at is not None
        or now - newest.sent_at < REMIND_AFTER
        or newest.reminders_sent >= MAX_REMINDERS
    ):
        await session.rollback()
        return False
    carried = newest.reminders_sent + 1
    link, token = await issue_link(session, sheet, employee)
    link.reminders_sent = carried
    await audit.record(
        session,
        tenant_id=sheet.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="timesheet",
        entity_id=sheet.id,
        actor_user_id=actor_id,
        changes={"sent_to": link.sent_to_email, "reminder": carried},
        summary=f"Reminded {link.sent_to_email} about the week of {sheet.week_start}",
    )
    await session.commit()
    await deliver(session, link, token, employee)
    return True


# ── The customer's page ────────────────────────────────────────────────────


async def _resolve(
    session: AsyncSession, token: str, *, lock: bool = False
) -> tuple[TimesheetApprovalLink, Timesheet, Employee, list[TimesheetEntry]]:
    stmt = select(TimesheetApprovalLink).where(
        TimesheetApprovalLink.token_hash == _token_hash(token)
    )
    link = await session.scalar(stmt)
    if link is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "This link is not valid. Check you opened the whole link from the email.",
        )
    scope = SimpleNamespace(tenant_id=link.tenant_id)
    sheet_stmt = select(Timesheet).where(
        tenant_filter(Timesheet, scope), Timesheet.id == link.timesheet_id
    )
    if lock:
        # Sheet first, then the link, as every other writer does; re-read both fresh.
        sheet_stmt = sheet_stmt.with_for_update().execution_options(populate_existing=True)
    sheet = await session.scalar(sheet_stmt)
    if lock and sheet is not None:
        link = await session.scalar(
            stmt.with_for_update().execution_options(populate_existing=True)
        )
        if link is None:
            raise _error(status.HTTP_404_NOT_FOUND, "This link is not valid.")
    employee = (
        await session.scalar(
            select(Employee).where(tenant_filter(Employee, scope), Employee.id == sheet.employee_id)
        )
        if sheet is not None
        else None
    )
    if sheet is None or employee is None:
        raise _error(status.HTTP_404_NOT_FOUND, "This timesheet is no longer available.")
    return link, sheet, employee, await _entries(session, sheet)


def _state(link: TimesheetApprovalLink, sheet: Timesheet, entries: list[TimesheetEntry]) -> str:
    """waiting, decided, changed or expired. Expired only for a link nobody has used or closed."""
    if contents_hash(entries) != link.contents_hash:
        return "changed"
    if sheet.status in (TimesheetStatus.APPROVED, TimesheetStatus.REJECTED):
        return "decided"
    if link.used_at is not None or link.revoked_at is not None:
        return "changed"
    if link.expires_at <= datetime.now(UTC):
        return "expired"
    return "waiting"


async def describe(
    session: AsyncSession, token: str, request: Request | None = None
) -> dict[str, Any]:
    """What the link is about: hours only. Changes nothing."""
    if request is not None:
        throttle(request, READ_LIMIT)
    link, sheet, employee, entries = await _resolve(session, token)
    scope = SimpleNamespace(tenant_id=link.tenant_id)
    tenant = await session.scalar(select(Tenant).where(Tenant.id == link.tenant_id))
    project_ids = {e.project_id for e in entries if e.project_id}
    task_ids = {e.task_id for e in entries if e.task_id}
    projects = {
        p.id: p.name
        for p in (
            await session.scalars(
                select(Project).where(tenant_filter(Project, scope), Project.id.in_(project_ids))
            )
        ).all()
    }
    tasks = {
        t.id: t.name
        for t in (
            await session.scalars(
                select(ProjectTask).where(
                    tenant_filter(ProjectTask, scope), ProjectTask.id.in_(task_ids)
                )
            )
        ).all()
    }
    total = sum((Decimal(str(e.hours)) for e in entries), Decimal(0))
    week_start: date = sheet.week_start
    return {
        "state": _state(link, sheet, entries),
        "company": tenant.name if tenant else "",
        "consultant_name": employee.full_name,
        "week_start": week_start.isoformat(),
        "days": [
            {
                "date": e.work_date.isoformat(),
                "project": projects.get(e.project_id) if e.project_id else None,
                "task": tasks.get(e.task_id) if e.task_id else None,
                "hours": f"{Decimal(str(e.hours)):.2f}",
            }
            for e in entries
        ],
        "total_hours": f"{total:.2f}",
    }


async def decide(
    session: AsyncSession,
    token: str,
    *,
    approve: bool,
    reason: str | None,
    ip: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record the customer's decision. The link is spent whatever the answer."""
    from app.services.timesheets import _hr_user_ids

    if request is not None:
        throttle(request, DECIDE_LIMIT)
    link, sheet, employee, entries = await _resolve(session, token, lock=True)
    state = _state(link, sheet, entries)
    if state == "decided":
        raise _error(status.HTTP_409_CONFLICT, "This week has already been decided.")
    if state == "changed":
        raise _error(
            status.HTTP_409_CONFLICT,
            "This week was changed after the email was sent. Ask for a new email.",
        )
    if state == "expired":
        raise _error(status.HTTP_410_GONE, "This link has expired. Ask for a new email.")
    if sheet.status is not TimesheetStatus.SUBMITTED:
        raise _error(status.HTTP_409_CONFLICT, "This week is not waiting for a decision.")
    note = (reason or "").strip() or None
    if not approve and note is None:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Say what needs changing.")

    now = datetime.now(UTC)
    link.used_at = now
    link.decided_ip = (ip or "")[:64] or None
    sheet.status = TimesheetStatus.APPROVED if approve else TimesheetStatus.REJECTED
    sheet.decided_by_user_id = None
    sheet.decided_at = now
    sheet.decision_note = note
    sheet.approval_proof = {
        "source": "customer_link",
        "name": link.sent_to_name,
        "email": link.sent_to_email,
        "decided_at": now.isoformat(),
        "ip": link.decided_ip,
    }

    scope = SimpleNamespace(tenant_id=sheet.tenant_id)
    recipients = list(
        (
            await session.scalars(
                select(User.id).where(
                    tenant_filter(User, scope),
                    User.employee_id == sheet.employee_id,
                    User.is_active.is_(True),
                )
            )
        ).all()
    )
    approvers = (
        [employee.internal_approver_user_id]
        if employee.internal_approver_user_id
        else await _hr_user_ids(session, scope, not_employee_id=employee.id)
    )
    recipients += [uid for uid in approvers if uid not in recipients]
    if recipients:
        await notify(
            session,
            tenant_id=sheet.tenant_id,
            event_key="timesheets.decided",
            recipient_user_ids=recipients,
            payload={
                "week": f"{sheet.week_start:%d %b %Y}",
                "outcome": "approved by the customer" if approve else "sent back by the customer",
                "note_line": f" Reason: {note}" if note else "",
                "timesheet_id": str(sheet.id),
            },
            commit=False,
        )
    verb = "Approved" if approve else "Rejected"
    await audit.record(
        session,
        tenant_id=sheet.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="timesheet",
        entity_id=sheet.id,
        actor=None,
        actor_type="system",
        changes={"status": sheet.status.value, "note": note, "via": "customer_link"},
        summary=(
            f"{verb} by the customer contact {link.sent_to_email} from the emailed link, for "
            f"the week of {sheet.week_start}"
        ),
        request=request,
    )
    await session.commit()
    return {"state": "decided", "approved": approve}
