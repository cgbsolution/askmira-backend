"""An employee's own resignation: submit it, withdraw it, and have it accepted.

The exit it creates is the same `ExitRecord` HR opens, through the same
`open_exit` — the duplicate check, notice arithmetic, clearance checklist and
asset hook are not repeated here. What this module adds is the part that only
exists when the employee starts it:

- **It waits to be accepted.** A submitted resignation sits in `initiated`
  until the line manager or HR accepts it and fixes the last working day. Until
  then the employee may withdraw it themselves; after that, withdrawing is a
  conversation with HR, who cancel it through the normal path.
- **The proposed last day comes from the notice period.** Resignation date is
  the workspace's today (IST, not the server's UTC date), and the proposal is
  that plus the contractual notice. An earlier date may be asked for; the
  shortfall is computed and shown, not hidden.
- **Nobody accepts their own.** The acceptor is somebody in the leaver's
  reporting line, or HR — `permissions.require_manager_or_hr`, which refuses
  self for everyone, HR included.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import ExitRecord, ExitStatus, ExitType
from app.models.user import User
from app.services import audit
from app.services.exit_workflow import operations
from app.services.exit_workflow.service import (
    notice_shortfall,
    open_exit,
    status_for_shortfall,
)
from app.services.notifications.dispatch import notify, register_event

__all__ = ["accept", "inbox_items", "my_exit", "resign", "withdraw"]

EVENT_SUBMITTED = "exit.resignation.submitted"
EVENT_WITHDRAWN = "exit.resignation.withdrawn"
EVENT_ACCEPTED = "exit.resignation.accepted"

register_event(
    EVENT_SUBMITTED,
    "{employee_name} has resigned",
    "{employee_name} submitted their resignation on {resignation_date}, proposing "
    "{last_working_day} as their last working day. Reason: {reason}. It is waiting for "
    "the line manager or HR to accept it.",
)
register_event(
    EVENT_WITHDRAWN,
    "{employee_name} withdrew their resignation",
    "{employee_name} withdrew the resignation they submitted on {resignation_date}. "
    "Nothing further is needed.",
)
register_event(
    EVENT_ACCEPTED,
    "Your resignation has been accepted",
    "{accepted_by} accepted your resignation. Your last working day is {last_working_day}. {note}",
)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exit record not found")


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return employee


async def _record(session: AsyncSession, user: User, exit_id: uuid.UUID) -> ExitRecord:
    record = await session.scalar(
        tenant_scoped(select(ExitRecord).where(ExitRecord.id == exit_id), ExitRecord, user)
    )
    if record is None:
        raise _not_found()
    return record


async def _recipients(session: AsyncSession, user: User, employee: Employee) -> list[uuid.UUID]:
    """The leaver's line manager and HR — never the person acting."""
    manager_logins: list[uuid.UUID] = []
    if employee.manager_id is not None:
        manager_logins = list(
            (
                await session.scalars(
                    tenant_scoped(
                        select(User.id).where(
                            User.employee_id == employee.manager_id, User.is_active.is_(True)
                        ),
                        User,
                        user,
                    )
                )
            ).all()
        )
    hr_logins = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.role.in_(permissions.HR_ROLES), User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )
    seen: list[uuid.UUID] = []
    for login in [*manager_logins, *hr_logins]:
        if login != user.id and login not in seen:
            seen.append(login)
    return seen


def _day(on: date | None) -> str:
    return on.strftime("%d %b %Y") if on else "not set"


# ═══════════════════════════════════════════════════════════════════════════
# The employee
# ═══════════════════════════════════════════════════════════════════════════


async def resign(
    session: AsyncSession,
    user: User,
    *,
    reason: str,
    requested_last_working_day: date | None = None,
    comments: str | None = None,
    request: Request | None = None,
) -> ExitRecord:
    """Submit your own resignation. Does not commit."""
    from app.services.attendance.service import local_today

    employee = await _employee(session, user, permissions.own_employee_id(user))
    if employee.status is EmploymentStatus.EXITED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Your employment has already ended."
        )

    today = await local_today(session, user)
    notice_days = employee.notice_period_days or 0
    last_day = requested_last_working_day or today + timedelta(days=notice_days)
    if last_day < today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The last working day cannot be before today.",
        )

    # One exit per person per resignation date is a database rule. A
    # resignation withdrawn today cannot be resubmitted with today's date.
    same_day = await session.scalar(
        tenant_scoped(
            select(ExitRecord.id).where(
                ExitRecord.employee_id == employee.id,
                ExitRecord.resignation_date == today,
                ExitRecord.status == ExitStatus.CANCELLED,
            ),
            ExitRecord,
            user,
        )
    )
    if same_day is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "You withdrew a resignation dated today. A new one can be submitted "
                "tomorrow, or HR can record it for you today."
            ),
        )

    record = await open_exit(
        session,
        user,
        employee,
        exit_type=ExitType.RESIGNATION,
        resignation_date=today,
        requested_last_working_day=last_day,
        reason=reason.strip(),
        initiated_by_employee=True,
        employee_comments=(comments or "").strip() or None,
        request=request,
    )

    recipients = await _recipients(session, user, employee)
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_SUBMITTED,
            recipient_user_ids=recipients,
            payload={
                "employee_name": employee.full_name,
                "resignation_date": _day(today),
                "last_working_day": _day(last_day),
                "reason": record.reason or "",
            },
            commit=False,
        )
    return record


async def my_exit(session: AsyncSession, user: User) -> ExitRecord | None:
    """Your most recent exit record, withdrawn ones included, or None."""
    own = permissions.own_employee_id(user)
    return await session.scalar(
        tenant_scoped(
            select(ExitRecord)
            .where(ExitRecord.employee_id == own)
            .order_by(ExitRecord.created_at.desc())
            .limit(1),
            ExitRecord,
            user,
        )
    )


async def withdraw(
    session: AsyncSession,
    user: User,
    exit_id: uuid.UUID,
    *,
    reason: str,
    request: Request | None = None,
) -> ExitRecord:
    """Take back your own resignation while nobody has accepted it. Does not commit."""
    own = permissions.own_employee_id(user)
    record = await _record(session, user, exit_id)
    if record.employee_id != own:
        raise _not_found()
    if not record.initiated_by_employee:
        raise permissions.forbidden(
            "This exit was opened by HR or your manager, so it is withdrawn through them."
        )
    if record.status is ExitStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This resignation is already withdrawn."
        )
    if record.accepted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Your resignation has already been accepted. To withdraw it now, speak to "
                "your manager or HR, who can cancel it."
            ),
        )
    record = await operations.cancel_record(
        session,
        user,
        record,
        reason=reason,
        summary=f"Withdrew their resignation: {reason}",
        request=request,
    )
    record.withdrawn_at = datetime.now(UTC)
    await session.flush()

    employee = await _employee(session, user, own)
    recipients = await _recipients(session, user, employee)
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_WITHDRAWN,
            recipient_user_ids=recipients,
            payload={
                "employee_name": employee.full_name,
                "resignation_date": _day(record.resignation_date),
            },
            commit=False,
        )
    return record


# ═══════════════════════════════════════════════════════════════════════════
# The line manager or HR
# ═══════════════════════════════════════════════════════════════════════════


async def accept(
    session: AsyncSession,
    user: User,
    exit_id: uuid.UUID,
    *,
    approved_last_working_day: date | None = None,
    note: str | None = None,
    request: Request | None = None,
) -> ExitRecord:
    """Accept a submitted resignation and fix the last working day. Does not commit.

    The shortfall and the status are recomputed from the agreed date with the
    same rule an HR-opened exit uses.
    """
    record = await operations.visible_exit(session, user, exit_id)
    await permissions.require_manager_or_hr(
        session,
        user,
        record.employee_id,
        detail="Only the leaver's line manager or HR can accept a resignation.",
    )
    if record.status is ExitStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This resignation was withdrawn or cancelled.",
        )
    if record.accepted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This resignation is already accepted."
        )

    last_day = approved_last_working_day or record.requested_last_working_day
    if last_day is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Set the last working day to accept the resignation.",
        )
    if last_day < record.resignation_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The last working day cannot be before the resignation date.",
        )

    shortfall = notice_shortfall(record.notice_period_days, record.resignation_date, last_day)
    before = {
        "approved_last_working_day": str(record.approved_last_working_day),
        "status": record.status.value,
    }
    record.approved_last_working_day = last_day
    record.notice_shortfall_days = shortfall
    record.status = status_for_shortfall(shortfall)
    record.accepted_at = datetime.now(UTC)
    record.accepted_by_user_id = user.id
    await session.flush()

    employee = await _employee(session, user, record.employee_id)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="exit_record",
        entity_id=record.id,
        summary=(
            f"Accepted {employee.employee_code}'s resignation; last working day {last_day}"
            f"{f', {shortfall} day(s) of notice short' if shortfall else ''}"
        ),
        changes={
            "before": before,
            "approved_last_working_day": str(last_day),
            "notice_shortfall_days": shortfall,
            "status": record.status.value,
            "note": note,
        },
        request=request,
    )
    logins = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.employee_id == employee.id, User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )
    if logins:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_ACCEPTED,
            recipient_user_ids=logins,
            payload={
                "accepted_by": user.full_name,
                "last_working_day": _day(last_day),
                "note": (note or "").strip(),
            },
            commit=False,
        )
    return record


async def inbox_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Resignations waiting to be accepted, for the approvals inbox.

    HR sees every one; a manager, those from their reporting line; nobody their own.
    """
    stmt = tenant_scoped(
        select(ExitRecord, Employee)
        .join(Employee, Employee.id == ExitRecord.employee_id)
        .where(
            ExitRecord.initiated_by_employee.is_(True),
            ExitRecord.accepted_at.is_(None),
            ExitRecord.status != ExitStatus.CANCELLED,
        )
        .order_by(ExitRecord.created_at),
        ExitRecord,
        user,
    )
    if not permissions.is_hr(user):
        line = await permissions.reporting_line(session, user)
        if not line:
            return []
        stmt = stmt.where(ExitRecord.employee_id.in_(line))
    if user.employee_id is not None:
        stmt = stmt.where(ExitRecord.employee_id != user.employee_id)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": record.id,
            "title": "Resignation to accept",
            "subject_name": employee.full_name,
            "detail": f"Proposed last day {_day(record.requested_last_working_day)}",
            "submitted_at": record.created_at,
            "due_on": record.requested_last_working_day,
        }
        for record, employee in rows
    ]
