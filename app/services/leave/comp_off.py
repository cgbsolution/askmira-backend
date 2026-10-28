"""Compensatory off: a day off earned by working a holiday or a weekly off.

An employee claims the date they worked; the line manager (or HR, when there is
no manager who can sign in) approves it; approval credits the tenant's comp-off
leave type through the balance ledger, where it lapses after the tenant's
expiry window if it is not taken. Nobody approves their own claim.

A date qualifies only if it was a holiday or a weekly off *for that person* —
their state's holidays, their office's, the optional ones they chose, their
shift's weekly offs. Working a Karnataka holiday in Maharashtra is a working day.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import CompOffRequest, CompOffStatus
from app.models.user import User
from app.schemas.leave import CompOffCreate, CompOffRead
from app.services import audit
from app.services.approvals.email_actions import ApprovalRef
from app.services.leave import ledger
from app.services.leave import service as core
from app.services.notifications import dispatch

dispatch.register_event(
    "leave.compoff.requested",
    "Comp-off claim from {employee_name}",
    "{employee_name} has claimed {days} day(s) of comp-off for working on {worked_date} "
    "({worked_day}).",
)
dispatch.register_event(
    "leave.compoff.decided",
    "Your comp-off claim was {decision}",
    "Your comp-off claim for {worked_date} ({days} day(s)) was {decision}.",
)

WEEKLY_OFF = "Weekly off"


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comp-off claim not found")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _may_decide(user: User, claim: CompOffRequest) -> bool:
    if claim.status is not CompOffStatus.PENDING:
        return False
    if user.employee_id is not None and user.employee_id == claim.employee_id:
        return False
    if permissions.is_hr(user):
        return True
    return claim.approver_user_id is not None and claim.approver_user_id == user.id


def _may_cancel(user: User, claim: CompOffRequest) -> bool:
    if claim.status is not CompOffStatus.PENDING:
        return False
    return permissions.is_hr(user) or user.employee_id == claim.employee_id


def _read(
    claim: CompOffRequest, employee: Employee, user: User, label: str | None = None
) -> CompOffRead:
    return CompOffRead(
        id=claim.id,
        employee_id=claim.employee_id,
        employee_name=core._name(employee),
        employee_photo_url=employee.photo_url,
        worked_date=claim.worked_date,
        worked_day_label=label,
        days=Decimal(str(claim.days)),
        reason=claim.reason,
        status=claim.status,
        approver_name=claim.approver_name,
        decided_by_name=claim.decided_by_name,
        decided_at=claim.decided_at,
        decision_note=claim.decision_note,
        expires_on=claim.expires_on,
        created_at=claim.created_at,
        can_decide=_may_decide(user, claim),
        can_cancel=_may_cancel(user, claim),
    )


async def qualifying_label(
    session: AsyncSession, user: User, employee: Employee, worked: date
) -> str | None:
    """Why the date was a day off for this person, or None if it was a working day."""
    holidays = await core._holidays(session, user, employee, worked, worked)
    if worked in holidays:
        return holidays[worked]
    if (await core._weekly_off_check(session, user, employee.id, worked, worked))(worked):
        return WEEKLY_OFF
    return None


async def create(
    session: AsyncSession, user: User, payload: CompOffCreate, *, request: Request | None = None
) -> CompOffRead:
    employee_id = permissions.resolve_employee(
        user, payload.employee_id, detail="Only HR can claim comp-off on somebody's behalf."
    )
    employee = await core._employee(session, user, employee_id)
    if employee.status is EmploymentStatus.EXITED:
        raise _unprocessable("Comp-off cannot be claimed for somebody who has left.")
    if await core.comp_off_type(session, user) is None:
        raise _conflict(
            "Comp-off is not set up yet: HR marks one leave type as comp-off in Time off → Setup."
        )
    today = await core.today_for(session, user, employee)
    window = await core.comp_off_expiry_days(session, user)
    if payload.worked_date > today:
        raise _unprocessable("Claim comp-off after the day is worked, not before.")
    if payload.worked_date < today - timedelta(days=window):
        raise _unprocessable(f"Comp-off must be claimed within {window} days of the day worked.")
    label = await qualifying_label(session, user, employee, payload.worked_date)
    if label is None:
        raise _unprocessable(
            f"{payload.worked_date:%d %b %Y} was a working day for you, not a holiday or a "
            "weekly off, so it does not earn comp-off."
        )
    duplicate = await session.scalar(
        tenant_scoped(
            select(CompOffRequest.id).where(
                CompOffRequest.employee_id == employee.id,
                CompOffRequest.worked_date == payload.worked_date,
                CompOffRequest.status.in_((CompOffStatus.PENDING, CompOffStatus.APPROVED)),
            ),
            CompOffRequest,
            user,
        )
    )
    if duplicate is not None:
        raise _conflict("That day has already been claimed.")

    approver = (await core.approvers_for(session, user, employee))[0]
    claim = CompOffRequest(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        worked_date=payload.worked_date,
        days=Decimal(payload.days),
        reason=payload.reason.strip(),
        status=CompOffStatus.PENDING,
        approver_user_id=approver.user_id,
        approver_name=approver.name,
    )
    session.add(claim)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="comp_off_request",
        entity_id=claim.id,
        actor=user,
        changes={
            "employee_id": str(employee.id),
            "worked_date": payload.worked_date.isoformat(),
            "days": payload.days,
            "worked_day": label,
            "on_behalf": employee.id != user.employee_id,
        },
        summary=f"Claimed {payload.days} day(s) of comp-off for {core._name(employee)}",
        request=request,
    )
    recipients = [approver.user_id] if approver.user_id else await core.hr_user_ids(session, user)
    await dispatch.notify(
        session,
        tenant_id=user.tenant_id,
        event_key="leave.compoff.requested",
        recipient_user_ids=[r for r in recipients if r != user.id],
        payload={
            "employee_name": core._name(employee),
            "days": payload.days,
            "worked_date": payload.worked_date.isoformat(),
            "worked_day": label,
        },
        commit=False,
        approval=ApprovalRef("leave_comp_off", claim.id),
    )
    await session.commit()
    await session.refresh(claim)
    return _read(claim, employee, user, label)


async def list_claims(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID | None, mine_only: bool
) -> list[CompOffRead]:
    """Your own claims, your reporting line's, or everybody's for HR. Newest first."""
    stmt = tenant_scoped(
        select(CompOffRequest, Employee).join(Employee, Employee.id == CompOffRequest.employee_id),
        CompOffRequest,
        user,
    )
    visible = await permissions.visible_employee_ids(session, user)
    if mine_only:
        stmt = stmt.where(CompOffRequest.employee_id == permissions.own_employee_id(user))
    elif visible is not None:
        stmt = stmt.where(CompOffRequest.employee_id.in_(visible))
    if employee_id is not None:
        if visible is not None and employee_id not in visible:
            raise permissions.forbidden(
                "You can only see comp-off claims for yourself or for people who report to you."
            )
        stmt = stmt.where(CompOffRequest.employee_id == employee_id)
    rows = (
        await session.execute(
            stmt.order_by(
                CompOffRequest.worked_date.desc(), CompOffRequest.created_at.desc()
            ).limit(200)
        )
    ).all()
    return [_read(claim, emp, user) for claim, emp in rows]


async def approvals(session: AsyncSession, user: User) -> list[CompOffRead]:
    """Pending claims the caller can decide now, oldest first. Never their own."""
    stmt = tenant_scoped(
        select(CompOffRequest, Employee)
        .join(Employee, Employee.id == CompOffRequest.employee_id)
        .where(CompOffRequest.status == CompOffStatus.PENDING),
        CompOffRequest,
        user,
    )
    if not permissions.is_hr(user):
        stmt = stmt.where(CompOffRequest.approver_user_id == user.id)
    if user.employee_id is not None:
        stmt = stmt.where(CompOffRequest.employee_id != user.employee_id)
    rows = (await session.execute(stmt.order_by(CompOffRequest.created_at))).all()
    result = []
    for claim, emp in rows:
        if _may_decide(user, claim):
            label = await qualifying_label(session, user, emp, claim.worked_date)
            result.append(_read(claim, emp, user, label))
    return result


async def approval_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The unified approvals inbox's view of comp-off claims."""
    return [
        {
            "id": claim.id,
            "title": f"Comp-off for {claim.worked_date:%d %b %Y}"
            + (f" ({claim.worked_day_label})" if claim.worked_day_label else ""),
            "subject_name": claim.employee_name,
            "detail": f"{format(claim.days.normalize(), 'f')} day(s) · {claim.reason[:100]}",
            "submitted_at": claim.created_at,
        }
        for claim in await approvals(session, user)
    ]


async def _locked(session: AsyncSession, user: User, claim_id: uuid.UUID) -> CompOffRequest:
    claim = await session.scalar(
        tenant_scoped(
            select(CompOffRequest).where(CompOffRequest.id == claim_id).with_for_update(),
            CompOffRequest,
            user,
        )
    )
    if claim is None:
        raise _not_found()
    return claim


async def decide(
    session: AsyncSession,
    user: User,
    claim_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
) -> CompOffRead:
    claim = await _locked(session, user, claim_id)
    if claim.status is not CompOffStatus.PENDING:
        raise _conflict(f"This claim is already {claim.status.value}.")
    if user.employee_id is not None and user.employee_id == claim.employee_id:
        raise permissions.forbidden("You cannot decide your own comp-off claim.")
    if not _may_decide(user, claim):
        raise permissions.forbidden(
            "This claim is waiting on somebody else. Only its approver or HR can decide it."
        )
    if not approve and not (note and note.strip()):
        raise _unprocessable("Say why when declining, so the person knows what to change.")

    employee = await core._employee(session, user, claim.employee_id)
    today = await core.today_for(session, user, employee)
    now = datetime.now(UTC)
    days = Decimal(str(claim.days))

    if approve:
        leave_type = await core.comp_off_type(session, user)
        if leave_type is None:
            raise _conflict(
                "No leave type is marked as comp-off, so there is nowhere to credit this. "
                "HR sets one in Time off → Setup."
            )
        expiry = await core.comp_off_expiry_days(session, user)
        claim.leave_type_id = leave_type.id
        claim.expires_on = today + timedelta(days=expiry)
        await ledger.post(
            session,
            user,
            employee_id=employee.id,
            leave_type_id=leave_type.id,
            year=today.year,
            kind="comp_off",
            days=days,
            reason=f"Worked {claim.worked_date:%d %b %Y}",
            effective_on=today,
            source_type="comp_off_request",
            source_id=claim.id,
            expires_on=claim.expires_on,
        )
    claim.status = CompOffStatus.APPROVED if approve else CompOffStatus.REJECTED
    claim.decided_by_user_id = user.id
    claim.decided_by_name = user.full_name
    claim.decided_at = now
    claim.decision_note = note

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="comp_off_request",
        entity_id=claim.id,
        actor=user,
        changes={
            "days": str(days),
            "note": note,
            "expires_on": claim.expires_on.isoformat() if claim.expires_on else None,
        },
        summary=(
            f"{'Approved' if approve else 'Declined'} comp-off for {core._name(employee)} "
            f"({claim.worked_date})"
        ),
        request=request,
    )
    applicant = await session.scalar(
        tenant_scoped(select(User.id).where(User.employee_id == employee.id), User, user)
    )
    if applicant is not None:
        await dispatch.notify(
            session,
            tenant_id=user.tenant_id,
            event_key="leave.compoff.decided",
            recipient_user_ids=[applicant],
            payload={
                "worked_date": claim.worked_date.isoformat(),
                "days": str(days),
                "decision": "approved" if approve else "declined",
            },
            commit=False,
        )
    await session.commit()
    await session.refresh(claim)
    return _read(claim, employee, user)


async def cancel(
    session: AsyncSession, user: User, claim_id: uuid.UUID, *, request: Request | None = None
) -> CompOffRead:
    """Withdraw a pending claim. An approved one is already a balance; HR debits it."""
    claim = await _locked(session, user, claim_id)
    permissions.require_self_or_hr(
        user, claim.employee_id, detail="You can only withdraw your own comp-off claims."
    )
    if not _may_cancel(user, claim):
        raise _conflict(f"This claim is already {claim.status.value}.")
    employee = await core._employee(session, user, claim.employee_id)
    claim.status = CompOffStatus.CANCELLED
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="comp_off_request",
        entity_id=claim.id,
        actor=user,
        changes={"status": "cancelled"},
        summary=f"Withdrew comp-off claim for {claim.worked_date}",
        request=request,
    )
    await session.commit()
    await session.refresh(claim)
    return _read(claim, employee, user)
