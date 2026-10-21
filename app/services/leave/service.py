"""Time off: apply, approve, cancel, balances and the team calendar.

The arithmetic lives in `engine`; this module loads what the arithmetic needs,
decides who is allowed to do what, and writes the result with its audit entry
in the same transaction.

The ledger (`LeaveBalance`) is the record the encashment and exit flows read.
`used` moves only here — on approval and on cancelling approved leave — and the
row's `accrued` is refreshed whenever the row is touched, or for everybody at
once by the accrual run. Every other movement (HR adjustments, comp-off, the
year-end carry-forward, encashment) goes through `app.services.leave.ledger`.

"Today" is India's today, not the server's: at 02:00 IST a UTC server is still
on yesterday, and a leave that "has not started yet" by UTC has started here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import ShiftAssignment
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import (
    LeaveBalance,
    LeavePolicy,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from app.models.org import Location
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.leave import (
    AccrualRunRead,
    LeaveBalanceRead,
    LeavePolicyCreate,
    LeavePolicyUpdate,
    LeavePreviewRead,
    LeaveRequestCreate,
    LeaveRequestRead,
    LeaveSettingsRead,
    LeaveSettingsUpdate,
    LeaveTypeCreate,
    LeaveTypeUpdate,
    RequestBalanceRead,
    SkippedHoliday,
    WhoIsOutRead,
)
from app.services import audit
from app.services.approvals.email_actions import ApprovalRef
from app.services.leave import attachments, engine
from app.services.leave.holidays import holidays_for_employees
from app.services.notifications import dispatch

#: The team calendar is a month view; a wider range is a report, not a calendar.
MAX_CALENDAR_DAYS = 62

OPEN_STATUSES = (LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED)

#: Where the product runs. A location's own timezone wins when it has one.
LOCAL_ZONE = ZoneInfo("Asia/Kolkata")

#: Tenant settings (`tenant.settings`) this module reads, with their defaults.
OPTIONAL_HOLIDAY_QUOTA_KEY = "optional_holiday_quota"
DEFAULT_OPTIONAL_HOLIDAY_QUOTA = 2
COMP_OFF_EXPIRY_KEY = "comp_off_expiry_days"
DEFAULT_COMP_OFF_EXPIRY_DAYS = 60

dispatch.register_event(
    "leave.request.cancelled",
    "Leave cancelled: {employee_name}",
    "{employee_name}'s {leave_type} for {start_date} to {end_date} was cancelled by "
    "{cancelled_by}.",
)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _name(employee: Employee) -> str:
    return employee.display_name or employee.full_name


def gender_of(employee: Employee) -> str | None:
    """The employee's gender as a policy compares it, or None when not recorded."""
    return employee.gender.value if employee.gender is not None else None


def local_today() -> date:
    """Today in India."""
    return datetime.now(LOCAL_ZONE).date()


def _today_in(zone_name: str | None) -> date:
    try:
        zone = ZoneInfo(zone_name) if zone_name else LOCAL_ZONE
    except (ZoneInfoNotFoundError, ValueError):
        zone = LOCAL_ZONE
    return datetime.now(zone).date()


async def today_for(session: AsyncSession, user: User, employee: Employee) -> date:
    """Today where this person works."""
    zone_name = None
    if employee.location_id is not None:
        zone_name = await session.scalar(
            tenant_scoped(
                select(Location.timezone).where(Location.id == employee.location_id),
                Location,
                user,
            )
        )
    return _today_in(zone_name)


async def todays_for(
    session: AsyncSession, user: User, employees: Sequence[Employee]
) -> dict[uuid.UUID, date]:
    """`today_for` for many people, in one query."""
    location_ids = {e.location_id for e in employees if e.location_id is not None}
    zones: dict[uuid.UUID, str | None] = {}
    if location_ids:
        zones = {
            location_id: zone
            for location_id, zone in (
                await session.execute(
                    tenant_scoped(
                        select(Location.id, Location.timezone).where(Location.id.in_(location_ids)),
                        Location,
                        user,
                    )
                )
            ).all()
        }
    return {
        e.id: _today_in(zones.get(e.location_id) if e.location_id is not None else None)
        for e in employees
    }


# ── Tenant settings ─────────────────────────────────────────────────────────


async def _tenant(session: AsyncSession, user: User) -> Tenant:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise _not_found("Workspace not found")
    return tenant


def _setting(tenant: Tenant, key: str, default: int) -> int:
    value = (tenant.settings or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


async def optional_holiday_quota(session: AsyncSession, user: User) -> int:
    return _setting(
        await _tenant(session, user), OPTIONAL_HOLIDAY_QUOTA_KEY, DEFAULT_OPTIONAL_HOLIDAY_QUOTA
    )


async def comp_off_expiry_days(session: AsyncSession, user: User) -> int:
    return _setting(await _tenant(session, user), COMP_OFF_EXPIRY_KEY, DEFAULT_COMP_OFF_EXPIRY_DAYS)


async def comp_off_type(session: AsyncSession, user: User) -> LeaveType | None:
    return await session.scalar(
        tenant_scoped(
            select(LeaveType)
            .where(LeaveType.is_comp_off.is_(True), LeaveType.is_active.is_(True))
            .order_by(LeaveType.created_at),
            LeaveType,
            user,
        )
    )


async def get_settings(session: AsyncSession, user: User) -> LeaveSettingsRead:
    tenant = await _tenant(session, user)
    comp_off = await comp_off_type(session, user)
    return LeaveSettingsRead(
        optional_holiday_quota=_setting(
            tenant, OPTIONAL_HOLIDAY_QUOTA_KEY, DEFAULT_OPTIONAL_HOLIDAY_QUOTA
        ),
        comp_off_expiry_days=_setting(tenant, COMP_OFF_EXPIRY_KEY, DEFAULT_COMP_OFF_EXPIRY_DAYS),
        comp_off_leave_type_id=comp_off.id if comp_off else None,
        comp_off_leave_type_name=comp_off.name if comp_off else None,
    )


async def update_settings(
    session: AsyncSession,
    user: User,
    payload: LeaveSettingsUpdate,
    *,
    request: Request | None = None,
) -> LeaveSettingsRead:
    permissions.require_hr(user, detail="Only HR can change leave settings.")
    tenant = await _tenant(session, user)
    before = {
        OPTIONAL_HOLIDAY_QUOTA_KEY: _setting(
            tenant, OPTIONAL_HOLIDAY_QUOTA_KEY, DEFAULT_OPTIONAL_HOLIDAY_QUOTA
        ),
        COMP_OFF_EXPIRY_KEY: _setting(tenant, COMP_OFF_EXPIRY_KEY, DEFAULT_COMP_OFF_EXPIRY_DAYS),
    }
    changes: dict[str, Any] = {}
    if payload.optional_holiday_quota is not None:
        changes[OPTIONAL_HOLIDAY_QUOTA_KEY] = payload.optional_holiday_quota
    if payload.comp_off_expiry_days is not None:
        changes[COMP_OFF_EXPIRY_KEY] = payload.comp_off_expiry_days
    if changes:
        # A new dict, not an in-place edit: JSONB change tracking sees assignment only.
        tenant.settings = {**(tenant.settings or {}), **changes}
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="leave_settings",
            entity_id=tenant.id,
            actor=user,
            changes={"before": before, "after": changes},
            summary="Changed leave settings",
            request=request,
        )
        await session.commit()
    return await get_settings(session, user)


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


async def _any_leave_type(session: AsyncSession, user: User, leave_type_id: uuid.UUID) -> LeaveType:
    """A leave type whether or not it is still offered."""
    leave_type = await session.scalar(
        tenant_scoped(select(LeaveType).where(LeaveType.id == leave_type_id), LeaveType, user)
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
    """This person's holidays: national, their state's, their office's, and the
    optional ones they opted in to. The same resolver attendance uses."""
    resolved = await holidays_for_employees(session, user, [employee.id], start, end)
    return resolved.get(employee.id, {})


async def _policies(
    session: AsyncSession, user: User, leave_type_id: uuid.UUID | None = None
) -> list[LeavePolicy]:
    """In creation order, so the engine's tie-break picks the latest correction."""
    stmt = select(LeavePolicy).order_by(LeavePolicy.created_at)
    if leave_type_id is not None:
        stmt = stmt.where(LeavePolicy.leave_type_id == leave_type_id)
    return list((await session.scalars(tenant_scoped(stmt, LeavePolicy, user))).all())


async def _policy(
    session: AsyncSession, user: User, employee: Employee, leave_type_id: uuid.UUID, on: date
) -> LeavePolicy | None:
    return engine.select_policy(
        await _policies(session, user, leave_type_id),
        location_id=employee.location_id,
        grade=employee.grade,
        gender=gender_of(employee),
        on=on,
    )


def _accrual_as_of(year: int, today: date, until: date | None = None) -> date:
    """The date a year's accrual is read at.

    Today for this year; the year end for a past one. For a year that has not
    started, the day the leave begins — so a December request for January
    leave is weighed against what January will have credited, not against
    nothing.
    """
    if year > today.year:
        return until if until is not None and until.year == year else date(year, 1, 1)
    return min(today, date(year, 12, 31))


def _accrual(policy: LeavePolicy | None, employee: Employee, year: int, as_of: date) -> Decimal:
    if policy is None:
        return engine.ZERO
    # Nothing is earned after the last day of employment. Without this a
    # leaver's balance, and the encashment paid from it, keeps growing.
    if employee.date_of_exit is not None and employee.date_of_exit < as_of:
        as_of = employee.date_of_exit
    return engine.accrued_to_date(
        annual_quota=engine.days(policy.annual_quota_days),
        frequency=policy.accrual_frequency,
        year=year,
        as_of=as_of,
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


async def ensure_ledger_row(
    session: AsyncSession, user: User, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year: int
) -> LeaveBalance:
    row = await _ledger_row(session, user, employee_id, leave_type_id, year)
    if row is None:
        row = LeaveBalance(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            year=year,
            opening_balance=engine.ZERO,
            accrued=engine.ZERO,
            used=engine.ZERO,
            encashed=engine.ZERO,
            carried_forward=engine.ZERO,
            adjusted=engine.ZERO,
        )
        session.add(row)
        await session.flush()
    return row


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


def _balance_from(
    row: LeaveBalance | None, accrued: Decimal, pending: Decimal, carry_in: Decimal = engine.ZERO
) -> engine.Balance:
    return engine.Balance(
        opening=engine.days(row.opening_balance) if row else engine.ZERO,
        # Accrual is a pure function of the policy and the calendar, so the
        # live figure is shown; the row catches up the next time it is written.
        accrued=accrued,
        carried_forward=(engine.days(row.carried_forward) if row else engine.ZERO) + carry_in,
        adjusted=engine.days(row.adjusted) if row else engine.ZERO,
        used=engine.days(row.used) if row else engine.ZERO,
        encashed=engine.days(row.encashed) if row else engine.ZERO,
        pending=pending,
    )


async def live_balance(
    session: AsyncSession,
    user: User,
    employee: Employee,
    leave_type: LeaveType,
    year: int,
    today: date,
    *,
    until: date | None = None,
) -> tuple[engine.Balance, Decimal]:
    """The balance the Time page shows, and the projected carry-in inside it.

    The one computation every reader uses — the balance cards, the request
    check, the encashment at exit — so no two screens can disagree.
    """
    policy = await _policy(
        session, user, employee, leave_type.id, _accrual_as_of(year, today, until)
    )
    row = await _ledger_row(session, user, employee.id, leave_type.id, year)
    projected = await _projected_carry_in(session, user, employee, leave_type, year, today)
    balance = _balance_from(
        row,
        _accrual(policy, employee, year, _accrual_as_of(year, today, until)),
        await _pending_days(session, user, employee.id, leave_type.id, year),
        projected,
    )
    return balance, projected


async def _projected_carry_in(
    session: AsyncSession,
    user: User,
    employee: Employee,
    leave_type: LeaveType,
    year: int,
    today: date,
) -> Decimal:
    """What this year will carry into next year, for a request in next year.

    Only one year ahead, and only until the year closes: after the year-end
    run the carry is on the new year's row and the old year's balance is zero,
    so this returns nothing and nothing is counted twice.
    """
    previous = year - 1
    if previous != today.year:
        return engine.ZERO
    year_end = date(previous, 12, 31)
    policy = await _policy(session, user, employee, leave_type.id, year_end)
    row = await _ledger_row(session, user, employee.id, leave_type.id, previous)
    if row is None:
        # Nothing tracked this year (see `ledger.close_year`): nothing will carry.
        return engine.ZERO
    balance = _balance_from(
        row,
        _accrual(policy, employee, previous, year_end),
        await _pending_days(session, user, employee.id, leave_type.id, previous),
    )
    cap = carry_cap(leave_type, policy)
    carried, _ = engine.year_end_split(balance.available_after_pending, cap)
    return carried


def carry_cap(leave_type: LeaveType, policy: LeavePolicy | None) -> Decimal | None:
    """How much may carry into a new year. Uncapped for comp-off, whose credits
    lapse on their own expiry date instead."""
    if leave_type.is_comp_off:
        return None
    return engine.days(policy.max_carry_forward_days) if policy else engine.ZERO


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
    row = await ensure_ledger_row(session, user, employee.id, leave_type_id, year)
    row.accrued = _accrual(policy, employee, year, _accrual_as_of(year, today))
    row.used = engine.days(row.used) + used_delta
    await session.flush()
    return row


# ── Approval chain ──────────────────────────────────────────────────────────


async def approvers_for(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    days: Decimal | None = None,
    leave_type_id: uuid.UUID | None = None,
) -> list[engine.Approver]:
    """The chain from the tenant's approval rules, else the line manager; HR otherwise.

    A manager with no login cannot act, and a request routed to somebody who
    cannot act is a request that waits forever — so a step nobody can take is
    skipped, and a chain left empty goes to HR.
    """
    from app.models.approval_rules import ApprovalProcess
    from app.services.approvals import rules

    steps, _rule = await rules.resolve_steps(
        session,
        user,
        employee,
        rules.Facts(
            process=ApprovalProcess.LEAVE,
            department_id=employee.department_id,
            location_id=employee.location_id,
            days=days,
            leave_type_id=leave_type_id,
        ),
    )
    if steps:
        return [
            engine.Approver(
                s.role if s.role != "user" else "approver",
                s.user_id,
                s.employee_id,
                s.name,
                escalate_after_hours=s.escalate_after_hours,
            )
            for s in steps
        ]
    return [engine.Approver("hr", None, None, "HR team")]


def _may_decide(user: User, leave: LeaveRequest) -> bool:
    """The approver of the current step — or whoever they delegated to — or HR.

    Never the person on leave. `acting_for_ids` is loaded by the callers that
    decide or list (see `_load_acting`); without it, only the caller themselves.
    """
    if user.employee_id is not None and user.employee_id == leave.employee_id:
        return False
    if leave.status is not LeaveRequestStatus.PENDING:
        return False
    index = engine.current_step(leave.approval_chain)
    if index is None:
        return False
    if permissions.is_hr(user):
        return True
    acting = getattr(user, "acting_for_ids", None) or {user.id}
    approver = leave.approval_chain[index].get("approver_user_id")
    return approver is not None and approver in {str(a) for a in acting}


async def _load_acting(session: AsyncSession, user: User) -> None:
    """Whose approvals this person may take today: theirs, and any delegated to them."""
    from app.services.approvals import rules

    user.acting_for_ids = await rules.acting_for(session, user)


def _may_cancel(user: User, leave: LeaveRequest, today: date) -> bool:
    if leave.status not in OPEN_STATUSES:
        return False
    if permissions.is_hr(user):
        return True
    if user.employee_id != leave.employee_id:
        return False
    return leave.status is LeaveRequestStatus.PENDING or leave.start_date > today


async def hr_user_ids(session: AsyncSession, user: User) -> list[uuid.UUID]:
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
    return await hr_user_ids(session, user)


# ── Serialising ─────────────────────────────────────────────────────────────


def _read(
    leave: LeaveRequest,
    employee: Employee,
    leave_type: LeaveType,
    user: User,
    today: date,
    *,
    balance: RequestBalanceRead | None = None,
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
        half_day_session=leave.half_day_session,  # type: ignore[arg-type]
        reason=leave.reason,
        document_url=leave.document_url,
        has_document=bool(
            leave.document_url and attachments.is_leave_document_key(user, leave.document_url)
        ),
        status=leave.status,
        approval_chain=leave.approval_chain,
        decided_at=leave.decided_at,
        decision_note=leave.decision_note,
        created_at=leave.created_at,
        can_decide=_may_decide(user, leave),
        can_cancel=_may_cancel(user, leave, today),
        balance=balance,
    )


# ── Requests ────────────────────────────────────────────────────────────────


async def _assess(
    session: AsyncSession,
    user: User,
    employee: Employee,
    payload: LeaveRequestCreate,
    today: date,
) -> tuple[engine.DayCount, engine.Assessment, engine.Balance | None, LeaveType, Decimal]:
    leave_type = await _leave_type(session, user, payload.leave_type_id)
    attachments.require_leave_document_key(user, payload.document_url)
    weekly_offs = await _weekly_off_days(session, user, employee.id, payload.start_date)
    holidays = await _holidays(session, user, employee, payload.start_date, payload.end_date)
    policy = await _policy(session, user, employee, leave_type.id, payload.start_date)
    try:
        count = engine.count_leave_days(
            payload.start_date,
            payload.end_date,
            half_day=payload.is_half_day,
            weekly_off_days=weekly_offs,
            holidays=holidays,
            sandwich=bool(policy and policy.sandwich_weekends_holidays),
        )
    except engine.LeaveRuleError as exc:
        raise _unprocessable(str(exc)) from exc

    if policy is not None:
        rules: engine.PolicyRules | None = engine.PolicyRules(
            min_notice_days=policy.min_notice_days,
            max_consecutive_days=policy.max_consecutive_days,
            allows_negative_balance=policy.allows_negative_balance,
        )
    elif leave_type.is_comp_off:
        # Comp-off has no quota to assign: the balance is whatever approved
        # claims credited, so the ledger is the policy.
        rules = engine.PolicyRules()
    else:
        rules = None

    year = payload.start_date.year
    balance: engine.Balance | None = None
    projected = engine.ZERO
    if leave_type.is_paid and rules is not None:
        balance, projected = await live_balance(
            session, user, employee, leave_type, year, today, until=payload.start_date
        )

    existing = (
        await session.execute(
            tenant_scoped(
                select(
                    LeaveRequest.start_date,
                    LeaveRequest.end_date,
                    LeaveRequest.is_half_day,
                    LeaveRequest.half_day_session,
                ).where(
                    LeaveRequest.employee_id == employee.id,
                    LeaveRequest.status.in_(OPEN_STATUSES),
                    LeaveRequest.start_date <= payload.end_date,
                    LeaveRequest.end_date >= payload.start_date,
                ),
                LeaveRequest,
                user,
            )
        )
    ).all()
    overlapping = engine.overlapping_requests(
        start=payload.start_date,
        end=payload.end_date,
        half_day=payload.is_half_day,
        session=payload.half_day_session,
        existing=[tuple(row) for row in existing],  # type: ignore[misc]
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
        overlapping=overlapping,
        is_paid=leave_type.is_paid,
    )
    if not leave_type.is_paid:
        assessment.warnings.append(
            "This leave is unpaid: the days count as loss of pay in payroll."
        )
    if projected > engine.ZERO:
        assessment.warnings.append(
            f"Includes {projected} day(s) expected to carry forward from {year - 1}."
        )
    return count, assessment, balance, leave_type, projected


async def preview(
    session: AsyncSession, user: User, payload: LeaveRequestCreate
) -> LeavePreviewRead:
    """What a request would cost and whether it would be accepted, without saving it."""
    employee_id = permissions.resolve_employee(
        user, payload.employee_id, detail="Only HR can request leave on somebody's behalf."
    )
    employee = await _employee(session, user, employee_id)
    today = await today_for(session, user, employee)
    count, assessment, balance, _, projected = await _assess(
        session, user, employee, payload, today
    )
    approvers = await approvers_for(
        session,
        user,
        employee,
        days=count.total,
        leave_type_id=payload.leave_type_id,
    )
    return LeavePreviewRead(
        total_days=count.total,
        working_days=len(count.working_dates),
        weekly_offs=list(count.weekly_offs),
        holidays=[SkippedHoliday(date=d, name=n) for d, n in count.holidays],
        sandwiched=list(count.sandwiched),
        available_before=balance.available if balance else None,
        pending_days=balance.pending if balance else None,
        available_after=(balance.available_after_pending - count.total) if balance else None,
        projected_carry_forward=projected if projected > engine.ZERO else None,
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
    today = await today_for(session, user, employee)
    count, assessment, _, leave_type, _ = await _assess(session, user, employee, payload, today)
    if not assessment.allowed:
        raise _unprocessable(" ".join(assessment.problems))
    if payload.document_url:
        await attachments.require_stored(payload.document_url)

    chain = engine.build_chain(
        await approvers_for(session, user, employee, days=count.total, leave_type_id=leave_type.id)
    )
    leave = LeaveRequest(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        leave_type_id=leave_type.id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        total_days=count.total,
        is_half_day=payload.is_half_day,
        half_day_session=payload.half_day_session,
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
            "half_day_session": payload.half_day_session,
            "sandwiched": [d.isoformat() for d in count.sandwiched],
            "has_document": bool(payload.document_url),
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
        approval=ApprovalRef("leave", leave.id),
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


async def _sync_attendance(
    session: AsyncSession, user: User, leave: LeaveRequest, today: date
) -> None:
    """Recompute the days of this leave that have already happened.

    Inside the caller's transaction, so the approval (or cancellation) and the
    attendance it changes commit together. Future days have no attendance yet;
    the nightly run computes them when they arrive.
    """
    from app.services.attendance import service as attendance

    end = min(leave.end_date, today)
    if leave.start_date > end:
        return
    await session.flush()
    try:
        await attendance.recompute_range(
            session,
            user,
            employee_ids=[leave.employee_id],
            start=leave.start_date,
            end=end,
            commit=False,
        )
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise
        # The period is locked for payroll. The leave decision stands; the
        # locked days change only through an audited unlock and recompute, so
        # payroll never runs on attendance that moved under it.


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
    await _load_acting(session, user)
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
    today = await today_for(session, user, employee)

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
    if outcome == "approved":
        await _sync_attendance(session, user, leave, today)

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
            approval=ApprovalRef("leave", leave.id),
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
    Whoever was asked to approve it is told.
    """
    leave = await _locked_request(session, user, request_id)
    permissions.require_self_or_hr(
        user, leave.employee_id, detail="You can only cancel your own leave requests."
    )
    employee = await _employee(session, user, leave.employee_id)
    today = await today_for(session, user, employee)
    if leave.status not in OPEN_STATUSES:
        raise _conflict(f"This request is already {leave.status.value}.")
    if not _may_cancel(user, leave, today):
        raise permissions.forbidden(
            "This leave has already started. Ask HR to cancel it, since attendance may "
            "already reflect it."
        )

    leave_type = await session.get(LeaveType, leave.leave_type_id)
    assert leave_type is not None
    was_approved = leave.status is LeaveRequestStatus.APPROVED
    # Who to tell, worked out before the chain stops naming a current step.
    if was_approved:
        told = {
            uuid.UUID(step["approver_user_id"])
            for step in leave.approval_chain
            if step.get("approver_user_id")
        }
        if leave.decided_by_user_id is not None:
            told.add(leave.decided_by_user_id)
    else:
        told = set(await _recipients(session, user, leave.approval_chain))
    if user.employee_id != leave.employee_id:
        applicant = await session.scalar(
            tenant_scoped(select(User.id).where(User.employee_id == employee.id), User, user)
        )
        if applicant is not None:
            told.add(applicant)
    told.discard(user.id)

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
    if was_approved:
        await _sync_attendance(session, user, leave, today)
    if told:
        await dispatch.notify(
            session,
            tenant_id=user.tenant_id,
            event_key="leave.request.cancelled",
            recipient_user_ids=sorted(told, key=str),
            payload={
                "employee_name": _name(employee),
                "leave_type": leave_type.name,
                "start_date": leave.start_date.isoformat(),
                "end_date": leave.end_date.isoformat(),
                "cancelled_by": user.full_name,
            },
            commit=False,
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
    await _load_acting(session, user)
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
    today = local_today()
    return [_read(leave, emp, lt, user, today) for leave, emp, lt in rows], total or 0


async def document(
    session: AsyncSession, user: User, request_id: uuid.UUID, *, request: Request | None = None
) -> tuple[str, str, bytes]:
    """A request's supporting document: `(filename, mime, bytes)`.

    For the person, whoever can see their requests (reporting line, HR), and
    anyone the approval chain names. Every download is audited — a medical
    certificate is exactly the thing somebody later asks "who looked at this".
    """
    leave = await session.scalar(
        tenant_scoped(select(LeaveRequest).where(LeaveRequest.id == request_id), LeaveRequest, user)
    )
    if leave is None:
        raise _not_found("Leave request not found")
    visible = await permissions.visible_employee_ids(session, user)
    named = {step.get("approver_user_id") for step in leave.approval_chain}
    if visible is not None and leave.employee_id not in visible and str(user.id) not in named:
        raise _not_found("Leave request not found")
    if not leave.document_url or not attachments.is_leave_document_key(user, leave.document_url):
        raise _not_found("This request has no document attached.")

    mime, data = await attachments.read(leave.document_url)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="leave_request",
        entity_id=leave.id,
        actor=user,
        summary="Downloaded the supporting document of a leave request",
        request=request,
    )
    await session.commit()
    return attachments.download_name(leave.id, mime), mime, data


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
    await _load_acting(session, user)
    if not permissions.is_hr(user):
        # Narrow in SQL before the per-step check: the caller's reporting line,
        # plus any request whose chain names them or somebody they stand in for
        # (a skip-level step, a named approver, a delegation).
        line = await permissions.reporting_line(session, user)
        named = [
            LeaveRequest.approval_chain.contains([{"approver_user_id": str(uid)}])
            for uid in user.acting_for_ids
        ]
        clauses = [*named]
        if line:
            clauses.append(LeaveRequest.employee_id.in_(line))
        stmt = stmt.where(or_(*clauses))
    rows = [
        (leave, emp, lt)
        for leave, emp, lt in (await session.execute(stmt.order_by(LeaveRequest.created_at))).all()
        if _may_decide(user, leave)
    ]
    today = local_today()
    todays = await todays_for(session, user, [emp for _, emp, _ in rows])
    # Somebody deciding a request wants to know whether the person has the
    # days, without leaving the inbox to look it up.
    return [
        _read(
            leave,
            emp,
            lt,
            user,
            today,
            balance=await _request_balance(session, user, leave, emp, lt, todays[emp.id]),
        )
        for leave, emp, lt in rows
    ]


async def _request_balance(
    session: AsyncSession,
    user: User,
    leave: LeaveRequest,
    employee: Employee,
    leave_type: LeaveType,
    today: date,
) -> RequestBalanceRead | None:
    """The requester's balance of the type asked for, read the way `_assess`
    read it when the request was raised. None where `_assess` checked no
    balance: unpaid leave, or a type no policy covers."""
    if not leave_type.is_paid:
        return None
    if not leave_type.is_comp_off and (
        await _policy(session, user, employee, leave_type.id, leave.start_date) is None
    ):
        return None
    balance, projected = await live_balance(
        session, user, employee, leave_type, leave.start_date.year, today, until=leave.start_date
    )
    requested = engine.days(leave.total_days)
    # `pending` includes this request, which is still pending until decided.
    return RequestBalanceRead(
        year=leave.start_date.year,
        available=balance.available,
        other_pending=max(engine.ZERO, balance.pending - requested),
        after_approval=balance.available - requested,
        projected_carry_forward=projected if projected > engine.ZERO else None,
    )


async def pending_for(session: AsyncSession, user: User) -> int:
    return len(await approvals_inbox(session, user))


async def _team_ids(session: AsyncSession, user: User) -> set[uuid.UUID]:
    """The caller, their manager, the people who share that manager, and
    everybody who reports to the caller."""
    if user.employee_id is None:
        return set()
    me = await _employee(session, user, user.employee_id)
    ids = {me.id} | await permissions.reporting_line(session, user)
    if me.manager_id is not None:
        ids.add(me.manager_id)
        ids |= set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(Employee.id).where(Employee.manager_id == me.manager_id),
                        Employee,
                        user,
                    )
                )
            ).all()
        )
    return ids


async def who_is_out(
    session: AsyncSession,
    user: User,
    *,
    start: date,
    end: date,
    department_id: uuid.UUID | None = None,
    team_only: bool = False,
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
    if department_id is not None:
        stmt = stmt.where(Employee.department_id == department_id)
    if team_only:
        team = await _team_ids(session, user)
        if not team:
            return []
        stmt = stmt.where(LeaveRequest.employee_id.in_(team))
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
                half_day_session=leave.half_day_session,  # type: ignore[arg-type]
                status=leave.status,
                leave_type_name=lt.name if detailed else None,
            )
        )
    return result


@dataclass(frozen=True)
class BalanceLine:
    """One person's balance of one leave type, and the policy behind it."""

    leave_type: LeaveType
    policy: LeavePolicy | None
    balance: engine.Balance


async def balance_lines(
    session: AsyncSession, user: User, employees: Sequence[Employee], year: int
) -> dict[uuid.UUID, list[BalanceLine]]:
    """Every listed person's balances for a year, in a fixed handful of queries.

    A leave type appears for somebody when a policy covers them or a ledger
    row exists. The Time page cards, the team table and its CSV all read this,
    so a person's own card and their row in HR's report cannot disagree.
    Callers check who may see whom; this only computes.
    """
    if not employees:
        return {}
    ids = [e.id for e in employees]
    leave_types = (
        await session.scalars(
            tenant_scoped(
                select(LeaveType).where(LeaveType.is_active.is_(True)).order_by(LeaveType.name),
                LeaveType,
                user,
            )
        )
    ).all()
    # Creation order is kept within each type, so the engine's tie-break still
    # picks the latest correction.
    policies_by_type: dict[uuid.UUID, list[LeavePolicy]] = {}
    for policy in await _policies(session, user):
        policies_by_type.setdefault(policy.leave_type_id, []).append(policy)
    ledger = {
        (row.employee_id, row.leave_type_id): row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(LeaveBalance).where(
                        LeaveBalance.employee_id.in_(ids), LeaveBalance.year == year
                    ),
                    LeaveBalance,
                    user,
                )
            )
        ).all()
    }
    pending = {
        (employee_id, leave_type_id): engine.days(total)
        for employee_id, leave_type_id, total in (
            await session.execute(
                tenant_scoped(
                    select(
                        LeaveRequest.employee_id,
                        LeaveRequest.leave_type_id,
                        func.sum(LeaveRequest.total_days),
                    )
                    .where(
                        LeaveRequest.employee_id.in_(ids),
                        LeaveRequest.status == LeaveRequestStatus.PENDING,
                        func.extract("year", LeaveRequest.start_date) == year,
                    )
                    .group_by(LeaveRequest.employee_id, LeaveRequest.leave_type_id),
                    LeaveRequest,
                    user,
                )
            )
        ).all()
    }
    todays = await todays_for(session, user, employees)

    result: dict[uuid.UUID, list[BalanceLine]] = {}
    for employee in employees:
        as_of = _accrual_as_of(year, todays[employee.id])
        lines: list[BalanceLine] = []
        for leave_type in leave_types:
            policy = engine.select_policy(
                policies_by_type.get(leave_type.id, []),
                location_id=employee.location_id,
                grade=employee.grade,
                gender=gender_of(employee),
                on=as_of,
            )
            row = ledger.get((employee.id, leave_type.id))
            if policy is None and row is None:
                continue
            lines.append(
                BalanceLine(
                    leave_type=leave_type,
                    policy=policy,
                    balance=_balance_from(
                        row,
                        _accrual(policy, employee, year, as_of),
                        pending.get((employee.id, leave_type.id), engine.ZERO),
                    ),
                )
            )
        result[employee.id] = lines
    return result


def _balance_read(line: BalanceLine, year: int) -> LeaveBalanceRead:
    leave_type, policy, balance = line.leave_type, line.policy, line.balance
    return LeaveBalanceRead(
        leave_type_id=leave_type.id,
        leave_type_name=leave_type.name,
        leave_type_code=leave_type.code,
        is_paid=leave_type.is_paid,
        is_statutory=leave_type.is_statutory,
        is_comp_off=leave_type.is_comp_off,
        year=year,
        policy_name=policy.name if policy else None,
        annual_quota_days=engine.days(policy.annual_quota_days) if policy else None,
        accrual_frequency=policy.accrual_frequency if policy else None,
        opening=balance.opening,
        accrued=balance.accrued,
        carried_forward=balance.carried_forward,
        adjusted=balance.adjusted,
        used=balance.used,
        encashed=balance.encashed,
        pending=balance.pending,
        available=balance.available,
        available_after_pending=balance.available_after_pending,
    )


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
    lines = await balance_lines(session, user, [employee], year)
    return [_balance_read(line, year) for line in lines[employee.id]]


async def run_accruals(
    session: AsyncSession, user: User, *, year: int, request: Request | None = None
) -> AccrualRunRead:
    """Bring every active employee's ledger `accrued` up to date for a year.

    Idempotent: accrual is a function of the policy and the calendar, so a
    second run on the same day writes the same values.
    """
    today = local_today()
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
    policies = await _policies(session, user)

    updated = 0
    for employee in employees:
        for leave_type in leave_types:
            policy = engine.select_policy(
                [p for p in policies if p.leave_type_id == leave_type.id],
                location_id=employee.location_id,
                grade=employee.grade,
                gender=gender_of(employee),
                on=as_of,
            )
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


async def list_types(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[LeaveType]:
    stmt = select(LeaveType).order_by(LeaveType.name)
    if not include_inactive:
        stmt = stmt.where(LeaveType.is_active.is_(True))
    return list((await session.scalars(tenant_scoped(stmt, LeaveType, user))).all())


async def _single_comp_off(session: AsyncSession, user: User, keep: LeaveType) -> list[str]:
    """Clear the comp-off flag from every other type. Returns their codes."""
    others = (
        await session.scalars(
            tenant_scoped(
                select(LeaveType).where(LeaveType.is_comp_off.is_(True), LeaveType.id != keep.id),
                LeaveType,
                user,
            )
        )
    ).all()
    for other in others:
        other.is_comp_off = False
    return [other.code for other in others]


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
    cleared = await _single_comp_off(session, user, leave_type) if payload.is_comp_off else []
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="leave_type",
        entity_id=leave_type.id,
        actor=user,
        changes={**payload.model_dump(), "comp_off_moved_from": cleared},
        summary=f"Created leave type {payload.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(leave_type)
    return leave_type


async def update_type(
    session: AsyncSession,
    user: User,
    leave_type_id: uuid.UUID,
    payload: LeaveTypeUpdate,
    *,
    request: Request | None = None,
) -> LeaveType:
    leave_type = await _any_leave_type(session, user, leave_type_id)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    before = {key: getattr(leave_type, key) for key in changes}
    for key, value in changes.items():
        setattr(leave_type, key, value)
    cleared = await _single_comp_off(session, user, leave_type) if payload.is_comp_off else []
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_type",
        entity_id=leave_type.id,
        actor=user,
        changes={"before": before, "after": changes, "comp_off_moved_from": cleared},
        summary=f"Edited leave type {leave_type.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(leave_type)
    return leave_type


async def set_type_active(
    session: AsyncSession,
    user: User,
    leave_type_id: uuid.UUID,
    *,
    active: bool,
    request: Request | None = None,
) -> LeaveType:
    """Stop (or resume) offering a leave type. History and balances stay."""
    leave_type = await _any_leave_type(session, user, leave_type_id)
    if not active:
        if leave_type.is_statutory:
            raise _conflict(
                f"{leave_type.name} is a statutory entitlement and cannot be withdrawn."
            )
        pending = await session.scalar(
            tenant_scoped(
                select(func.count(LeaveRequest.id)).where(
                    LeaveRequest.leave_type_id == leave_type.id,
                    LeaveRequest.status == LeaveRequestStatus.PENDING,
                ),
                LeaveRequest,
                user,
            )
        )
        if pending:
            raise _conflict(
                f"{pending} pending request(s) use {leave_type.name}. Decide them first, so "
                "nobody is left waiting on a type that no longer exists."
            )
    leave_type.is_active = active
    if not active:
        leave_type.is_comp_off = False
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_type",
        entity_id=leave_type.id,
        actor=user,
        changes={"is_active": active},
        summary=f"{'Reactivated' if active else 'Deactivated'} leave type {leave_type.name}",
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
                    .order_by(LeavePolicy.effective_from.desc(), LeavePolicy.created_at.desc()),
                    LeavePolicy,
                    user,
                )
            )
        ).all()
    )


def _same_population(
    stmt: Select[tuple[LeavePolicy]],
    location_id: uuid.UUID | None,
    grade: str | None,
    gender: str | None,
) -> Select[tuple[LeavePolicy]]:
    return stmt.where(
        LeavePolicy.location_id.is_(None)
        if location_id is None
        else LeavePolicy.location_id == location_id,
        LeavePolicy.grade.is_(None) if grade is None else LeavePolicy.grade == grade,
        LeavePolicy.applies_to_gender.is_(None)
        if gender is None
        else LeavePolicy.applies_to_gender == gender,
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
    still reads last year's quota. One starting the *same* day is replaced
    outright — that is a correction, and leaving it open meant the old rules
    silently kept winning.
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

    population = _same_population(
        select(LeavePolicy).where(
            LeavePolicy.leave_type_id == payload.leave_type_id,
            LeavePolicy.is_active.is_(True),
        ),
        payload.location_id,
        payload.grade,
        payload.applies_to_gender,
    )
    superseded = (
        await session.scalars(
            tenant_scoped(
                population.where(
                    or_(
                        LeavePolicy.effective_to.is_(None),
                        LeavePolicy.effective_to >= payload.effective_from,
                    ),
                    LeavePolicy.effective_from <= payload.effective_from,
                ),
                LeavePolicy,
                user,
            )
        )
    ).all()
    replaced: list[str] = []
    closed: list[str] = []
    for previous in superseded:
        if previous.effective_from == payload.effective_from:
            previous.is_active = False
            replaced.append(str(previous.id))
        else:
            previous.effective_to = payload.effective_from - timedelta(days=1)
            closed.append(str(previous.id))

    # A version slotted in before a later one ends where that one begins.
    following = await session.scalar(
        tenant_scoped(
            population.where(LeavePolicy.effective_from > payload.effective_from)
            .order_by(LeavePolicy.effective_from)
            .limit(1),
            LeavePolicy,
            user,
        )
    )

    policy = LeavePolicy(
        tenant_id=user.tenant_id,
        effective_to=following.effective_from - timedelta(days=1) if following else None,
        **payload.model_dump(),
    )
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
            "superseded": closed,
            "replaced": replaced,
        },
        summary=f"Created leave policy {payload.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(policy)
    return policy


async def _policy_or_404(session: AsyncSession, user: User, policy_id: uuid.UUID) -> LeavePolicy:
    policy = await session.scalar(
        tenant_scoped(select(LeavePolicy).where(LeavePolicy.id == policy_id), LeavePolicy, user)
    )
    if policy is None:
        raise _not_found("Leave policy not found")
    return policy


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


async def update_policy(
    session: AsyncSession,
    user: User,
    policy_id: uuid.UUID,
    payload: LeavePolicyUpdate,
    *,
    request: Request | None = None,
) -> LeavePolicy:
    """Correct a policy version in place, for the whole period it covers.

    Accrual is computed from the policy, so this changes every balance the
    version governs — which is what a correction means. A change that should
    start on a date is a new version instead.
    """
    policy = await _policy_or_404(session, user, policy_id)
    # Left-out fields keep their value. Null means "no change" everywhere but
    # the consecutive-days limit (null removes it) and the gender (null opens
    # the policy to everyone).
    nullable = {"max_consecutive_days", "applies_to_gender"}
    changes = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None or key in nullable
    }
    before = {key: _jsonable(getattr(policy, key)) for key in changes}
    for key, value in changes.items():
        setattr(policy, key, value)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_policy",
        entity_id=policy.id,
        actor=user,
        changes={"before": before, "after": {k: _jsonable(v) for k, v in changes.items()}},
        summary=f"Corrected leave policy {policy.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(policy)
    return policy


async def deactivate_policy(
    session: AsyncSession, user: User, policy_id: uuid.UUID, *, request: Request | None = None
) -> LeavePolicy:
    """Withdraw a policy version. Balances it governed stop accruing under it."""
    policy = await _policy_or_404(session, user, policy_id)
    policy.is_active = False
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_policy",
        entity_id=policy.id,
        actor=user,
        changes={"is_active": False},
        summary=f"Withdrew leave policy {policy.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(policy)
    return policy
