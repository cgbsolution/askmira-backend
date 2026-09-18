"""Time off: apply, approve, cancel, balances and the team calendar.

The arithmetic lives in `engine`; this module loads what the arithmetic needs,
decides who is allowed to do what, and writes the result with its audit entry
in the same transaction.

The ledger (`LeaveBalance`) is the record the encashment and exit flows read.
`used` moves only here — on approval and on cancelling approved leave — and the
row's `accrued` is refreshed whenever the row is touched, or for everybody at
once by the accrual run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import ShiftAssignment
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import (
    Holiday,
    LeaveBalance,
    LeavePolicy,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from app.models.org import Location
from app.models.user import User
from app.schemas.leave import (
    AccrualRunRead,
    HolidayCreate,
    LeaveBalanceRead,
    LeavePolicyCreate,
    LeavePreviewRead,
    LeaveRequestCreate,
    LeaveRequestRead,
    LeaveTypeCreate,
    SkippedHoliday,
    WhoIsOutRead,
)
from app.services import audit
from app.services.leave import engine
from app.services.notifications import dispatch

#: The team calendar is a month view; a wider range is a report, not a calendar.
MAX_CALENDAR_DAYS = 62

OPEN_STATUSES = (LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _name(employee: Employee) -> str:
    return employee.display_name or employee.full_name


# ── Loading ─────────────────────────────────────────────────────────────────


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == employee_id, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )
    if employee is None:
        raise _not_found("Employee not found")
    return employee


async def _leave_type(session: AsyncSession, user: User, leave_type_id: uuid.UUID) -> LeaveType:
    leave_type = await session.scalar(
        tenant_scoped(
            select(LeaveType).where(LeaveType.id == leave_type_id, LeaveType.is_active.is_(True)),
            LeaveType,
            user,
        )
    )
    if leave_type is None:
        raise _not_found("Leave type not found")
    return leave_type


async def _weekly_off_days(
    session: AsyncSession, user: User, employee_id: uuid.UUID, on: date
) -> tuple[int, ...]:
    """The employee's shift weekly offs, or Saturday and Sunday without a shift."""
    assignment = await session.scalar(
        tenant_scoped(
            select(ShiftAssignment)
            .where(
                ShiftAssignment.employee_id == employee_id,
                ShiftAssignment.effective_from <= on,
                or_(ShiftAssignment.effective_to.is_(None), ShiftAssignment.effective_to >= on),
            )
            .order_by(ShiftAssignment.effective_from.desc()),
            ShiftAssignment,
            user,
        )
    )
    if assignment is not None and assignment.shift.weekly_off_days:
        return tuple(assignment.shift.weekly_off_days)
    return engine.DEFAULT_WEEKLY_OFF_DAYS


async def _holidays(
    session: AsyncSession, user: User, employee: Employee, start: date, end: date
) -> dict[date, str]:
    """Mandatory holidays for the employee's location: national, their state's, their office's.

    Optional (restricted) holidays are chosen by the employee, so they are not
    skipped automatically.
    """
    state_code = None
    if employee.location_id is not None:
        state_code = await session.scalar(
            tenant_scoped(
                select(Location.state_code).where(Location.id == employee.location_id),
                Location,
                user,
            )
        )
    stmt = tenant_scoped(
        select(Holiday).where(
            Holiday.holiday_date >= start,
            Holiday.holiday_date <= end,
            Holiday.is_optional.is_(False),
            or_(Holiday.state_code.is_(None), Holiday.state_code == state_code),
            or_(Holiday.location_id.is_(None), Holiday.location_id == employee.location_id),
        ),
        Holiday,
        user,
    )
    return {h.holiday_date: h.name for h in (await session.scalars(stmt)).all()}


async def _policy(
    session: AsyncSession, user: User, employee: Employee, leave_type_id: uuid.UUID, on: date
) -> LeavePolicy | None:
    policies = (
        await session.scalars(
            tenant_scoped(
                select(LeavePolicy).where(LeavePolicy.leave_type_id == leave_type_id),
                LeavePolicy,
                user,
            )
        )
    ).all()
    return engine.select_policy(
        policies, location_id=employee.location_id, grade=employee.grade, on=on
    )


def _accrual(policy: LeavePolicy | None, employee: Employee, year: int, today: date) -> Decimal:
    if policy is None:
        return engine.ZERO
    return engine.accrued_to_date(
        annual_quota=engine.days(policy.annual_quota_days),
        frequency=policy.accrual_frequency,
        year=year,
        as_of=min(today, date(year, 12, 31)),
        joined_on=employee.date_of_joining,
    )


async def _ledger_row(
    session: AsyncSession, user: User, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year: int
) -> LeaveBalance | None:
    return await session.scalar(
        tenant_scoped(
            select(LeaveBalance).where(
                LeaveBalance.employee_id == employee_id,
                LeaveBalance.leave_type_id == leave_type_id,
                LeaveBalance.year == year,
            ),
            LeaveBalance,
            user,
        )
    )


async def _pending_days(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    year: int,
    *,
    exclude: uuid.UUID | None = None,
) -> Decimal:
    stmt = tenant_scoped(
        select(func.coalesce(func.sum(LeaveRequest.total_days), 0)).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.leave_type_id == leave_type_id,
            LeaveRequest.status == LeaveRequestStatus.PENDING,
            func.extract("year", LeaveRequest.start_date) == year,
        ),
        LeaveRequest,
        user,
    )
    if exclude is not None:
        stmt = stmt.where(LeaveRequest.id != exclude)
    return engine.days(await session.scalar(stmt))


async def _balance(
    session: AsyncSession,
    user: User,
    employee: Employee,
    leave_type_id: uuid.UUID,
    year: int,
    policy: LeavePolicy | None,
    today: date,
) -> engine.Balance:
    row = await _ledger_row(session, user, employee.id, leave_type_id, year)
    return engine.Balance(
        opening=engine.days(row.opening_balance) if row else engine.ZERO,
        # Accrual is a pure function of the policy and the calendar, so the
        # live figure is shown; the row catches up the next time it is written.
        accrued=_accrual(policy, employee, year, today),
        carried_forward=engine.days(row.carried_forward) if row else engine.ZERO,
        used=engine.days(row.used) if row else engine.ZERO,
        encashed=engine.days(row.encashed) if row else engine.ZERO,
        pending=await _pending_days(session, user, employee.id, leave_type_id, year),
    )


async def _post_to_ledger(
    session: AsyncSession,
    user: User,
    employee: Employee,
    leave_type_id: uuid.UUID,
    year: int,
    *,
    used_delta: Decimal,
    today: date,
) -> LeaveBalance:
    """Move `used` on the ledger row, creating it and refreshing accrual as it goes."""
    policy = await _policy(session, user, employee, leave_type_id, date(year, 12, 31))
    row = await _ledger_row(session, user, employee.id, leave_type_id, year)
    if row is None:
        row = LeaveBalance(
            tenant_id=user.tenant_id,
            employee_id=employee.id,
            leave_type_id=leave_type_id,
            year=year,
            opening_balance=engine.ZERO,
            accrued=engine.ZERO,
            used=engine.ZERO,
            encashed=engine.ZERO,
            carried_forward=engine.ZERO,
        )
        session.add(row)
    row.accrued = _accrual(policy, employee, year, today)
    row.used = engine.days(row.used) + used_delta
    await session.flush()
    return row


# ── Approval chain ──────────────────────────────────────────────────────────


async def _approvers(
    session: AsyncSession, user: User, employee: Employee
) -> list[engine.Approver]:
    """The line manager when they can sign in; HR otherwise.

    A manager with no login cannot act, and a request routed to somebody who
    cannot act is a request that waits forever.
    """
    if employee.manager_id is not None:
        manager = await session.scalar(
            tenant_scoped(
                select(Employee).where(
                    Employee.id == employee.manager_id, Employee.deleted_at.is_(None)
                ),
                Employee,
                user,
            )
        )
        if manager is not None:
            manager_user = await session.scalar(
                tenant_scoped(
                    select(User).where(User.employee_id == manager.id, User.is_active.is_(True)),
                    User,
                    user,
                )
            )
            if manager_user is not None:
                return [engine.Approver("manager", manager_user.id, manager.id, _name(manager))]
    return [engine.Approver("hr", None, None, "HR team")]


def _may_decide(user: User, leave: LeaveRequest) -> bool:
    """The named approver of the current step, or HR, and never the person on leave."""
    if user.employee_id is not None and user.employee_id == leave.employee_id:
        return False
    if leave.status is not LeaveRequestStatus.PENDING:
        return False
    index = engine.current_step(leave.approval_chain)
    if index is None:
        return False
    if permissions.is_hr(user):
        return True
    return leave.approval_chain[index].get("approver_user_id") == str(user.id)


def _may_cancel(user: User, leave: LeaveRequest, today: date) -> bool:
    if leave.status not in OPEN_STATUSES:
        return False
    if permissions.is_hr(user):
        return True
    if user.employee_id != leave.employee_id:
        return False
    return leave.status is LeaveRequestStatus.PENDING or leave.start_date > today


async def _recipients(
    session: AsyncSession, user: User, chain: list[dict[str, Any]]
) -> list[uuid.UUID]:
    """Who to tell that a step is waiting: the named approver, or everybody in HR."""
    index = engine.current_step(chain)
    if index is None:
        return []
    named = chain[index].get("approver_user_id")
    if named:
        return [uuid.UUID(named)]
    return list(
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


# ── Serialising ─────────────────────────────────────────────────────────────


def _read(
    leave: LeaveRequest, employee: Employee, leave_type: LeaveType, user: User, today: date
) -> LeaveRequestRead:
    return LeaveRequestRead(
        id=leave.id,
        employee_id=leave.employee_id,
        employee_name=_name(employee),
        employee_photo_url=employee.photo_url,
        leave_type_id=leave.leave_type_id,
        leave_type_name=leave_type.name,
        start_date=leave.start_date,
        end_date=leave.end_date,
        total_days=engine.days(leave.total_days),
        is_half_day=leave.is_half_day,
        reason=leave.reason,
        document_url=leave.document_url,
        status=leave.status,
        approval_chain=leave.approval_chain,
        decided_at=leave.decided_at,
        decision_note=leave.decision_note,
        created_at=leave.created_at,
        can_decide=_may_decide(user, leave),
        can_cancel=_may_cancel(user, leave, today),
    )


async def _read_one(session: AsyncSession, user: User, leave: LeaveRequest) -> LeaveRequestRead:
    employee = await _employee(session, user, leave.employee_id)
    leave_type = await session.get(LeaveType, leave.leave_type_id)
    assert leave_type is not None  # RESTRICT foreign key
    return _read(leave, employee, leave_type, user, date.today())


# ── Requests ────────────────────────────────────────────────────────────────


async def _assess(
    session: AsyncSession,
    user: User,
    employee: Employee,
    payload: LeaveRequestCreate,
    today: date,
) -> tuple[engine.DayCount, engine.Assessment, engine.Balance | None, LeaveType]:
    leave_type = await _leave_type(session, user, payload.leave_type_id)
    weekly_offs = await _weekly_off_days(session, user, employee.id, payload.start_date)
    holidays = await _holidays(session, user, employee, payload.start_date, payload.end_date)
    try:
        count = engine.count_leave_days(
            payload.start_date,
            payload.end_date,
            half_day=payload.is_half_day,
            weekly_off_days=weekly_offs,
            holidays=holidays,
        )
    except engine.LeaveRuleError as exc:
        raise _unprocessable(str(exc)) from exc

    policy = await _policy(session, user, employee, leave_type.id, payload.start_date)
    year = payload.start_date.year
    balance = (
        await _balance(session, user, employee, leave_type.id, year, policy, today)
        if policy is not None
        else None
    )
    overlapping = await session.scalar(
        tenant_scoped(
            select(func.count(LeaveRequest.id)).where(
                LeaveRequest.employee_id == employee.id,
                LeaveRequest.status.in_(OPEN_STATUSES),
                LeaveRequest.start_date <= payload.end_date,
                LeaveRequest.end_date >= payload.start_date,
            ),
            LeaveRequest,
            user,
        )
    )
    rules = (
        engine.PolicyRules(
            min_notice_days=policy.min_notice_days,
            max_consecutive_days=policy.max_consecutive_days,
            allows_negative_balance=policy.allows_negative_balance,
        )
        if policy is not None
        else None
    )
    assessment = engine.assess_request(
        count=count,
        start=payload.start_date,
        today=today,
        half_day=payload.is_half_day,
        allows_half_day=leave_type.allows_half_day,
        requires_document=leave_type.requires_document,
        has_document=bool(payload.document_url),
        rules=rules,
        balance=balance,
        overlapping=overlapping or 0,
    )
    return count, assessment, balance, leave_type


async def preview(
    session: AsyncSession, user: User, payload: LeaveRequestCreate
) -> LeavePreviewRead:
    """What a request would cost and whether it would be accepted, without saving it."""
    employee_id = permissions.resolve_employee(
        user, payload.employee_id, detail="Only HR can request leave on somebody's behalf."
    )
    employee = await _employee(session, user, employee_id)
    today = date.today()
    count, assessment, balance, _ = await _assess(session, user, employee, payload, today)
    approvers = await _approvers(session, user, employee)
    return LeavePreviewRead(
        total_days=count.total,
        working_days=len(count.working_dates),
        weekly_offs=list(count.weekly_offs),
        holidays=[SkippedHoliday(date=d, name=n) for d, n in count.holidays],
        available_before=balance.available if balance else None,
        pending_days=balance.pending if balance else None,
        available_after=(balance.available_after_pending - count.total) if balance else None,
        approvers=[a.name for a in approvers],
        allowed=assessment.allowed,
        problems=assessment.problems,
        warnings=assessment.warnings,
    )


async def apply(
    session: AsyncSession,
    user: User,
    payload: LeaveRequestCreate,
    *,
    request: Request | None = None,
) -> LeaveRequestRead:
    employee_id = permissions.resolve_employee(
        user, payload.employee_id, detail="Only HR can request leave on somebody's behalf."
    )
    employee = await _employee(session, user, employee_id)
    if employee.status is EmploymentStatus.EXITED:
        raise _unprocessable("Leave cannot be requested for somebody who has left.")
    today = date.today()
    count, assessment, _, leave_type = await _assess(session, user, employee, payload, today)
    if not assessment.allowed:
        raise _unprocessable(" ".join(assessment.problems))

    chain = engine.build_chain(await _approvers(session, user, employee))
    leave = LeaveRequest(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        leave_type_id=leave_type.id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        total_days=count.total,
        is_half_day=payload.is_half_day,
        reason=payload.reason,
        document_url=payload.document_url,
        status=LeaveRequestStatus.PENDING,
        approval_chain=chain,
    )
    session.add(leave)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="leave_request",
        entity_id=leave.id,
        actor=user,
        changes={
            "employee_id": str(employee.id),
            "leave_type": leave_type.code,
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
            "total_days": str(count.total),
            "on_behalf": employee.id != user.employee_id,
        },
        summary=f"Requested {count.total} day(s) of {leave_type.name} for {_name(employee)}",
        request=request,
    )
    await dispatch.notify(
        session,
        tenant_id=user.tenant_id,
        event_key="leave.approval.requested",
        recipient_user_ids=await _recipients(session, user, chain),
        payload={
            "employee_name": _name(employee),
            "leave_type": leave_type.name,
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
        },
        commit=False,
    )
    await session.commit()
    await session.refresh(leave)
    return _read(leave, employee, leave_type, user, today)


async def _locked_request(session: AsyncSession, user: User, request_id: uuid.UUID) -> LeaveRequest:
    leave = await session.scalar(
        tenant_scoped(
            select(LeaveRequest).where(LeaveRequest.id == request_id).with_for_update(),
            LeaveRequest,
            user,
        )
    )
    if leave is None:
        raise _not_found("Leave request not found")
    return leave


async def decide(
    session: AsyncSession,
    user: User,
    request_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
) -> LeaveRequestRead:
    leave = await _locked_request(session, user, request_id)
    if leave.status is not LeaveRequestStatus.PENDING:
        raise _conflict(f"This request is already {leave.status.value}.")
    if user.employee_id is not None and user.employee_id == leave.employee_id:
        raise permissions.forbidden("You cannot decide your own leave request.")
    if not _may_decide(user, leave):
        raise permissions.forbidden(
            "This request is waiting on somebody else. Only its approver or HR can decide it."
        )
    if not approve and not (note and note.strip()):
        raise _unprocessable("Say why when declining, so the person knows what to change.")

    employee = await _employee(session, user, leave.employee_id)
    leave_type = await session.get(LeaveType, leave.leave_type_id)
    assert leave_type is not None
    now = datetime.now(UTC)
    today = now.date()

    chain, outcome = engine.apply_decision(
        leave.approval_chain,
        approve=approve,
        actor_user_id=user.id,
        actor_name=user.full_name,
        note=note,
        at=now,
    )
    leave.approval_chain = chain

    if outcome in ("approved", "rejected"):
        leave.status = (
            LeaveRequestStatus.APPROVED if outcome == "approved" else LeaveRequestStatus.REJECTED
        )
        leave.decided_by_user_id = user.id
        leave.decided_at = now
        leave.decision_note = note
    if outcome == "approved":
        await _post_to_ledger(
            session,
            user,
            employee,
            leave.leave_type_id,
            leave.start_date.year,
            used_delta=engine.days(leave.total_days),
            today=today,
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="leave_request",
        entity_id=leave.id,
        actor=user,
        changes={"outcome": outcome, "note": note},
        summary=(
            f"{'Approved' if approve else 'Declined'} {leave_type.name} for {_name(employee)}"
            + (" (step approved, next approver asked)" if outcome == "advanced" else "")
        ),
        request=request,
    )

    template_payload = {
        "employee_name": _name(employee),
        "leave_type": leave_type.name,
        "start_date": leave.start_date.isoformat(),
        "end_date": leave.end_date.isoformat(),
    }
    if outcome == "advanced":
        await dispatch.notify(
            session,
            tenant_id=user.tenant_id,
            event_key="leave.approval.requested",
            recipient_user_ids=await _recipients(session, user, chain),
            payload=template_payload,
            commit=False,
        )
    else:
        applicant = await session.scalar(
            tenant_scoped(select(User.id).where(User.employee_id == employee.id), User, user)
        )
        if applicant is not None:
            await dispatch.notify(
                session,
                tenant_id=user.tenant_id,
                event_key="leave.approval.decided",
                recipient_user_ids=[applicant],
                payload={**template_payload, "decision": outcome},
                commit=False,
            )

    await session.commit()
    await session.refresh(leave)
    return _read(leave, employee, leave_type, user, today)


async def cancel(
    session: AsyncSession, user: User, request_id: uuid.UUID, *, request: Request | None = None
) -> LeaveRequestRead:
    """Withdraw a request.

    Pending leave can always be withdrawn by its owner. Approved leave can be
    cancelled by its owner until it starts; after that only HR can, because
    leave that has been taken has already fed attendance and possibly payroll.
    """
    leave = await _locked_request(session, user, request_id)
    permissions.require_self_or_hr(
        user, leave.employee_id, detail="You can only cancel your own leave requests."
    )
    today = date.today()
    if leave.status not in OPEN_STATUSES:
        raise _conflict(f"This request is already {leave.status.value}.")
    if not _may_cancel(user, leave, today):
        raise permissions.forbidden(
            "This leave has already started. Ask HR to cancel it, since attendance may "
            "already reflect it."
        )

    employee = await _employee(session, user, leave.employee_id)
    leave_type = await session.get(LeaveType, leave.leave_type_id)
    assert leave_type is not None
    was_approved = leave.status is LeaveRequestStatus.APPROVED
    if was_approved:
        await _post_to_ledger(
            session,
            user,
            employee,
            leave.leave_type_id,
            leave.start_date.year,
            used_delta=-engine.days(leave.total_days),
            today=today,
        )
    leave.status = LeaveRequestStatus.CANCELLED

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_request",
        entity_id=leave.id,
        actor=user,
        changes={"status": "cancelled", "was_approved": was_approved},
        summary=f"Cancelled {leave_type.name} for {_name(employee)}",
        request=request,
    )
    await session.commit()
    await session.refresh(leave)
    return _read(leave, employee, leave_type, user, today)


async def list_requests(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None,
    request_status: LeaveRequestStatus | None,
    page: int,
    page_size: int,
) -> tuple[list[LeaveRequestRead], int]:
    """Your own requests, a manager's reporting line, or everybody's for HR.

    Reasons are routinely medical, so the visibility rule is the same one the
    employee profile uses.
    """
    stmt = tenant_scoped(
        select(LeaveRequest, Employee, LeaveType)
        .join(Employee, Employee.id == LeaveRequest.employee_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id),
        LeaveRequest,
        user,
    )
    visible = await permissions.visible_employee_ids(session, user)
    if visible is not None:
        if employee_id is not None and employee_id not in visible:
            raise permissions.forbidden(
                "You can only see leave requests for yourself or for people who report to you."
            )
        stmt = stmt.where(LeaveRequest.employee_id.in_(visible))
    if employee_id is not None:
        stmt = stmt.where(LeaveRequest.employee_id == employee_id)
    if request_status is not None:
        stmt = stmt.where(LeaveRequest.status == request_status)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await session.execute(
            stmt.order_by(LeaveRequest.start_date.desc(), LeaveRequest.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    today = date.today()
    return [_read(leave, emp, lt, user, today) for leave, emp, lt in rows], total or 0


async def approvals_inbox(session: AsyncSession, user: User) -> list[LeaveRequestRead]:
    """Pending requests the caller can decide right now, oldest first.

    For a manager, the steps that name them. For HR, every pending request in
    the company except their own — HR is the fallback approver and the
    escalation path when a manager is away.
    """
    stmt = tenant_scoped(
        select(LeaveRequest, Employee, LeaveType)
        .join(Employee, Employee.id == LeaveRequest.employee_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .where(LeaveRequest.status == LeaveRequestStatus.PENDING),
        LeaveRequest,
        user,
    )
    if not permissions.is_hr(user):
        # Narrow in SQL before the per-step check: a manager only ever
        # approves people in their own reporting line.
        line = await permissions.reporting_line(session, user)
        if not line:
            return []
        stmt = stmt.where(LeaveRequest.employee_id.in_(line))
    rows = (await session.execute(stmt.order_by(LeaveRequest.created_at))).all()
    today = date.today()
    return [
        _read(leave, emp, lt, user, today)
        for leave, emp, lt in rows
        if _may_decide(user, leave)
    ]


async def pending_for(session: AsyncSession, user: User) -> int:
    return len(await approvals_inbox(session, user))


async def who_is_out(
    session: AsyncSession, user: User, *, start: date, end: date
) -> list[WhoIsOutRead]:
    """The team calendar.

    Everybody sees who is out on approved leave — that is what lets a team
    plan. The leave type, and pending requests, are shown only for yourself,
    your reporting line and HR.
    """
    if end < start:
        raise _unprocessable("The end of the range cannot be before its start.")
    if (end - start).days > MAX_CALENDAR_DAYS:
        raise _unprocessable(f"Pick a range of at most {MAX_CALENDAR_DAYS} days.")

    visible = await permissions.visible_employee_ids(session, user)
    stmt = tenant_scoped(
        select(LeaveRequest, Employee, LeaveType)
        .join(Employee, Employee.id == LeaveRequest.employee_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .where(
            LeaveRequest.status.in_(OPEN_STATUSES),
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
            Employee.deleted_at.is_(None),
        ),
        LeaveRequest,
        user,
    )
    result: list[WhoIsOutRead] = []
    for leave, emp, lt in (await session.execute(stmt.order_by(LeaveRequest.start_date))).all():
        detailed = visible is None or leave.employee_id in visible
        if leave.status is LeaveRequestStatus.PENDING and not detailed:
            continue
        result.append(
            WhoIsOutRead(
                employee_id=emp.id,
                employee_name=_name(emp),
                photo_url=emp.photo_url,
                start_date=leave.start_date,
                end_date=leave.end_date,
                is_half_day=leave.is_half_day,
                status=leave.status,
                leave_type_name=lt.name if detailed else None,
            )
        )
    return result


async def balances(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID | None, year: int
) -> list[LeaveBalanceRead]:
    """Balances for every leave type the person has a policy or a ledger row for."""
    target = employee_id if employee_id is not None else permissions.own_employee_id(user)
    visible = await permissions.visible_employee_ids(session, user)
    if visible is not None and target not in visible:
        raise permissions.forbidden(
            "You can only see balances for yourself or for people who report to you."
        )
    employee = await _employee(session, user, target)
    today = date.today()
    on = min(max(today, date(year, 1, 1)), date(year, 12, 31))

    # Four queries for every leave type at once, not four per type: this is
    # the first thing on the Time screen and the database is a network hop away.
    leave_types = (
        await session.scalars(
            tenant_scoped(
                select(LeaveType).where(LeaveType.is_active.is_(True)).order_by(LeaveType.name),
                LeaveType,
                user,
            )
        )
    ).all()
    policies = (await session.scalars(tenant_scoped(select(LeavePolicy), LeavePolicy, user))).all()
    ledger = {
        row.leave_type_id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(LeaveBalance).where(
                        LeaveBalance.employee_id == employee.id, LeaveBalance.year == year
                    ),
                    LeaveBalance,
                    user,
                )
            )
        ).all()
    }
    pending_by_type = {
        leave_type_id: engine.days(total)
        for leave_type_id, total in (
            await session.execute(
                tenant_scoped(
                    select(LeaveRequest.leave_type_id, func.sum(LeaveRequest.total_days))
                    .where(
                        LeaveRequest.employee_id == employee.id,
                        LeaveRequest.status == LeaveRequestStatus.PENDING,
                        func.extract("year", LeaveRequest.start_date) == year,
                    )
                    .group_by(LeaveRequest.leave_type_id),
                    LeaveRequest,
                    user,
                )
            )
        ).all()
    }

    result: list[LeaveBalanceRead] = []
    for leave_type in leave_types:
        policy = engine.select_policy(
            [p for p in policies if p.leave_type_id == leave_type.id],
            location_id=employee.location_id,
            grade=employee.grade,
            on=on,
        )
        row = ledger.get(leave_type.id)
        if policy is None and row is None:
            continue
        balance = engine.Balance(
            opening=engine.days(row.opening_balance) if row else engine.ZERO,
            accrued=_accrual(policy, employee, year, today),
            carried_forward=engine.days(row.carried_forward) if row else engine.ZERO,
            used=engine.days(row.used) if row else engine.ZERO,
            encashed=engine.days(row.encashed) if row else engine.ZERO,
            pending=pending_by_type.get(leave_type.id, engine.ZERO),
        )
        result.append(
            LeaveBalanceRead(
                leave_type_id=leave_type.id,
                leave_type_name=leave_type.name,
                leave_type_code=leave_type.code,
                is_paid=leave_type.is_paid,
                is_statutory=leave_type.is_statutory,
                year=year,
                policy_name=policy.name if policy else None,
                annual_quota_days=engine.days(policy.annual_quota_days) if policy else None,
                accrual_frequency=policy.accrual_frequency if policy else None,
                opening=balance.opening,
                accrued=balance.accrued,
                carried_forward=balance.carried_forward,
                used=balance.used,
                encashed=balance.encashed,
                pending=balance.pending,
                available=balance.available,
                available_after_pending=balance.available_after_pending,
            )
        )
    return result


async def run_accruals(
    session: AsyncSession, user: User, *, year: int, request: Request | None = None
) -> AccrualRunRead:
    """Bring every active employee's ledger `accrued` up to date for a year.

    Idempotent: accrual is a function of the policy and the calendar, so a
    second run on the same day writes the same values.
    """
    today = date.today()
    as_of = min(today, date(year, 12, 31))
    employees = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.deleted_at.is_(None), Employee.status != EmploymentStatus.EXITED
                ),
                Employee,
                user,
            )
        )
    ).all()
    leave_types = (
        await session.scalars(
            tenant_scoped(select(LeaveType).where(LeaveType.is_active.is_(True)), LeaveType, user)
        )
    ).all()

    updated = 0
    for employee in employees:
        for leave_type in leave_types:
            policy = await _policy(session, user, employee, leave_type.id, as_of)
            if policy is None:
                continue
            await _post_to_ledger(
                session,
                user,
                employee,
                leave_type.id,
                year,
                used_delta=engine.ZERO,
                today=today,
            )
            updated += 1

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_balance",
        actor=user,
        changes={"year": year, "as_of": as_of.isoformat(), "balances_updated": updated},
        summary=f"Posted leave accruals for {year} as of {as_of}",
        request=request,
    )
    await session.commit()
    return AccrualRunRead(year=year, as_of=as_of, balances_updated=updated)


# ── Catalogue administration ────────────────────────────────────────────────


async def list_types(session: AsyncSession, user: User) -> list[LeaveType]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(LeaveType)
                    .where(LeaveType.is_active.is_(True))
                    .order_by(LeaveType.name),
                    LeaveType,
                    user,
                )
            )
        ).all()
    )


async def create_type(
    session: AsyncSession, user: User, payload: LeaveTypeCreate, *, request: Request | None = None
) -> LeaveType:
    duplicate = await session.scalar(
        tenant_scoped(select(LeaveType.id).where(LeaveType.code == payload.code), LeaveType, user)
    )
    if duplicate is not None:
        raise _conflict(f"A leave type with code {payload.code} already exists.")
    leave_type = LeaveType(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(leave_type)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="leave_type",
        entity_id=leave_type.id,
        actor=user,
        changes=payload.model_dump(),
        summary=f"Created leave type {payload.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(leave_type)
    return leave_type


async def list_policies(session: AsyncSession, user: User) -> list[LeavePolicy]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(LeavePolicy)
                    .where(LeavePolicy.is_active.is_(True))
                    .order_by(LeavePolicy.effective_from.desc()),
                    LeavePolicy,
                    user,
                )
            )
        ).all()
    )


async def create_policy(
    session: AsyncSession,
    user: User,
    payload: LeavePolicyCreate,
    *,
    request: Request | None = None,
) -> LeavePolicy:
    """Add a policy version.

    An existing policy for the same population is closed the day before the new
    one takes effect rather than edited, so a balance computed for last year
    still reads last year's quota.
    """
    await _leave_type(session, user, payload.leave_type_id)
    if payload.location_id is not None:
        location = await session.scalar(
            tenant_scoped(
                select(Location.id).where(Location.id == payload.location_id), Location, user
            )
        )
        if location is None:
            raise _not_found("Location not found")

    superseded = (
        await session.scalars(
            tenant_scoped(
                select(LeavePolicy).where(
                    LeavePolicy.leave_type_id == payload.leave_type_id,
                    LeavePolicy.is_active.is_(True),
                    LeavePolicy.effective_to.is_(None),
                    LeavePolicy.effective_from < payload.effective_from,
                    LeavePolicy.location_id.is_(None)
                    if payload.location_id is None
                    else LeavePolicy.location_id == payload.location_id,
                    LeavePolicy.grade.is_(None)
                    if payload.grade is None
                    else LeavePolicy.grade == payload.grade,
                ),
                LeavePolicy,
                user,
            )
        )
    ).all()
    for previous in superseded:
        previous.effective_to = payload.effective_from - timedelta(days=1)

    policy = LeavePolicy(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(policy)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="leave_policy",
        entity_id=policy.id,
        actor=user,
        changes={
            **payload.model_dump(mode="json"),
            "superseded": [str(p.id) for p in superseded],
        },
        summary=f"Created leave policy {payload.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(policy)
    return policy


async def list_holidays(
    session: AsyncSession, user: User, *, year: int, state_code: str | None
) -> list[Holiday]:
    stmt = tenant_scoped(
        select(Holiday).where(Holiday.year == year).order_by(Holiday.holiday_date),
        Holiday,
        user,
    )
    if state_code:
        stmt = stmt.where(or_(Holiday.state_code == state_code, Holiday.state_code.is_(None)))
    return list((await session.scalars(stmt)).all())


async def create_holiday(
    session: AsyncSession, user: User, payload: HolidayCreate, *, request: Request | None = None
) -> Holiday:
    duplicate = await session.scalar(
        tenant_scoped(
            select(Holiday.id).where(
                Holiday.holiday_date == payload.holiday_date,
                Holiday.state_code.is_(None)
                if payload.state_code is None
                else Holiday.state_code == payload.state_code,
            ),
            Holiday,
            user,
        )
    )
    if duplicate is not None:
        raise _conflict("That date already has a holiday for the same scope.")
    holiday = Holiday(
        tenant_id=user.tenant_id, year=payload.holiday_date.year, **payload.model_dump()
    )
    session.add(holiday)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="holiday",
        entity_id=holiday.id,
        actor=user,
        changes=payload.model_dump(mode="json"),
        summary=f"Added holiday {payload.name} on {payload.holiday_date}",
        request=request,
    )
    await session.commit()
    await session.refresh(holiday)
    return holiday
