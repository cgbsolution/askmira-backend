"""The balance ledger: every movement that is not accrual or leave taken.

HR's opening balances, credits and debits; comp-off credits and their expiry;
the year-end carry-forward and lapse; encashment. Each is one insert-only
`LeaveBalanceAdjustment` row and, in the same transaction, the matching column
on the year's `LeaveBalance` row — so the balance the Time page shows, the one
encashment reads and the history an employee disputes are the same numbers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import (
    LeaveBalance,
    LeaveBalanceAdjustment,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.leave import (
    BalanceAdjustmentCreate,
    BalanceAdjustmentRead,
    BalanceHistoryEntry,
    BalanceHistoryRead,
    YearEndRead,
)
from app.services import audit
from app.services.leave import engine
from app.services.leave import service as core

#: Kinds that move a column other than `adjusted`.
_OPENING = "opening"
_CARRY_IN = "carry_forward_in"
_ENCASHMENT = "encashment"

#: The marker a closed year leaves in `tenant.settings`, so the daily job does
#: nothing on the 364 days a year it has nothing to do.
YEAR_END_KEY = "leave_years_closed"

LABELS = {
    "accrual": "Accrued",
    "taken": "Leave taken",
    "opening": "Opening balance",
    "credit": "Credited by HR",
    "debit": "Debited by HR",
    "comp_off": "Comp-off credit",
    "comp_off_expiry": "Comp-off lapsed",
    "encashment": "Encashed",
    "carry_forward_in": "Carried forward",
    "carry_forward_out": "Carried into next year",
    "lapse": "Lapsed at year end",
    "unitemised": "Brought in before itemised history",
}


def apply_to_row(row: LeaveBalance, kind: str, days: Decimal) -> None:
    """Move the balance row's column for one adjustment. `days` is signed."""
    if kind == _OPENING:
        row.opening_balance = engine.days(row.opening_balance) + days
    elif kind == _CARRY_IN:
        row.carried_forward = engine.days(row.carried_forward) + days
    elif kind == _ENCASHMENT:
        row.encashed = engine.days(row.encashed) - days
    else:
        row.adjusted = engine.days(row.adjusted) + days


async def post(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    year: int,
    kind: str,
    days: Decimal,
    reason: str,
    effective_on: date,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
    expires_on: date | None = None,
) -> LeaveBalanceAdjustment:
    """Write one ledger row and move the balance with it. Flushes, never commits."""
    row = await core.ensure_ledger_row(session, user, employee_id, leave_type_id, year)
    apply_to_row(row, kind, days)
    entry = LeaveBalanceAdjustment(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        year=year,
        kind=kind,
        days=days,
        reason=reason,
        effective_on=effective_on,
        created_by_user_id=user.id,
        source_type=source_type,
        source_id=source_id,
        expires_on=expires_on,
    )
    session.add(entry)
    await session.flush()
    return entry


def _read(entry: LeaveBalanceAdjustment) -> BalanceAdjustmentRead:
    return BalanceAdjustmentRead(
        id=entry.id,
        employee_id=entry.employee_id,
        leave_type_id=entry.leave_type_id,
        year=entry.year,
        kind=entry.kind,
        days=engine.days(entry.days),
        reason=entry.reason,
        effective_on=entry.effective_on,
        created_at=entry.created_at,
    )


# ── HR adjustments ──────────────────────────────────────────────────────────


async def adjust(
    session: AsyncSession,
    user: User,
    payload: BalanceAdjustmentCreate,
    *,
    request: Request | None = None,
) -> BalanceAdjustmentRead:
    """An opening balance, a credit or a debit, by HR, with a reason.

    Opening sets the figure (the ledger records the difference); credit and
    debit move it. Nobody adjusts their own balance, HR included.
    """
    permissions.require_hr(user, detail="Only HR can adjust leave balances.")
    if user.employee_id is not None and user.employee_id == payload.employee_id:
        raise permissions.forbidden("You cannot adjust your own leave balance.")
    employee = await core._employee(session, user, payload.employee_id)
    leave_type = await core._any_leave_type(session, user, payload.leave_type_id)
    today = await core.today_for(session, user, employee)

    if payload.kind == "opening":
        row = await core.ensure_ledger_row(session, user, employee.id, leave_type.id, payload.year)
        delta = payload.days - engine.days(row.opening_balance)
        if delta == engine.ZERO:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"The opening balance is already {payload.days}.",
            )
        reason = f"Opening balance set to {payload.days}. {payload.reason.strip()}"
    else:
        if payload.days <= engine.ZERO:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Credit or debit at least half a day.",
            )
        delta = payload.days if payload.kind == "credit" else -payload.days
        reason = payload.reason.strip()

    entry = await post(
        session,
        user,
        employee_id=employee.id,
        leave_type_id=leave_type.id,
        year=payload.year,
        kind=payload.kind,
        days=delta,
        reason=reason,
        effective_on=min(today, date(payload.year, 12, 31))
        if payload.year <= today.year
        else date(payload.year, 1, 1),
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_balance",
        entity_id=entry.id,
        actor=user,
        changes={
            "employee_id": str(employee.id),
            "leave_type": leave_type.code,
            "year": payload.year,
            "kind": payload.kind,
            "days": str(delta),
            "reason": payload.reason,
        },
        summary=(
            f"{LABELS[payload.kind]} {abs(delta)} day(s) of {leave_type.name} for "
            f"{core._name(employee)}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(entry)
    return _read(entry)


# ── History ─────────────────────────────────────────────────────────────────


async def history(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None,
    leave_type_id: uuid.UUID,
    year: int,
) -> BalanceHistoryRead:
    """Every credit and debit behind one balance, in date order, with a running total.

    Accruals are listed from the policy (they are computed, not stored);
    everything else is a ledger row or an approved request. The final running
    total is the balance card's `available`, and is returned so the screen can
    show the two agree.
    """
    target = employee_id if employee_id is not None else permissions.own_employee_id(user)
    visible = await permissions.visible_employee_ids(session, user)
    if visible is not None and target not in visible:
        raise permissions.forbidden(
            "You can only see balance history for yourself or for people who report to you."
        )
    employee = await core._employee(session, user, target)
    leave_type = await core._any_leave_type(session, user, leave_type_id)
    today = await core.today_for(session, user, employee)
    as_of = core._accrual_as_of(year, today)
    policy = await core._policy(session, user, employee, leave_type.id, as_of)

    entries: list[tuple[date, int, str, Decimal, str | None]] = []
    if policy is not None:
        for on, amount in engine.accrual_schedule(
            annual_quota=engine.days(policy.annual_quota_days),
            frequency=policy.accrual_frequency,
            year=year,
            as_of=as_of,
            joined_on=employee.date_of_joining,
        ):
            entries.append((on, 1, "accrual", amount, policy.name))

    adjustments = (
        await session.scalars(
            tenant_scoped(
                select(LeaveBalanceAdjustment)
                .where(
                    LeaveBalanceAdjustment.employee_id == employee.id,
                    LeaveBalanceAdjustment.leave_type_id == leave_type.id,
                    LeaveBalanceAdjustment.year == year,
                )
                .order_by(LeaveBalanceAdjustment.created_at),
                LeaveBalanceAdjustment,
                user,
            )
        )
    ).all()
    for entry in adjustments:
        order = 0 if entry.kind in (_OPENING, _CARRY_IN) else 2
        entries.append(
            (entry.effective_on, order, entry.kind, engine.days(entry.days), entry.reason)
        )

    taken = (
        await session.scalars(
            tenant_scoped(
                select(LeaveRequest)
                .where(
                    LeaveRequest.employee_id == employee.id,
                    LeaveRequest.leave_type_id == leave_type.id,
                    LeaveRequest.status == LeaveRequestStatus.APPROVED,
                    LeaveRequest.start_date >= date(year, 1, 1),
                    LeaveRequest.start_date <= date(year, 12, 31),
                )
                .order_by(LeaveRequest.start_date),
                LeaveRequest,
                user,
            )
        )
    ).all()
    for leave in taken:
        span = (
            leave.start_date.strftime("%d %b")
            if leave.start_date == leave.end_date
            else f"{leave.start_date:%d %b} to {leave.end_date:%d %b}"
        )
        entries.append((leave.start_date, 3, "taken", -engine.days(leave.total_days), span))

    # Balances loaded before the ledger existed (an import, a migration) have
    # figures on the row with no rows behind them. Show the difference as one
    # line rather than a history that does not add up to the card.
    row = await core._ledger_row(session, user, employee.id, leave_type.id, year)
    card = core._balance_from(
        row, core._accrual(policy, employee, year, as_of), engine.ZERO
    ).available
    unitemised = card - sum((item[3] for item in entries), engine.ZERO)
    if unitemised != engine.ZERO:
        entries.append((date(year, 1, 1), -1, "unitemised", unitemised, None))

    entries.sort(key=lambda item: (item[0], item[1]))
    running = engine.ZERO
    lines: list[BalanceHistoryEntry] = []
    for on, _, kind, amount, note in entries:
        running += amount
        lines.append(
            BalanceHistoryEntry(
                on=on,
                kind=kind,
                label=LABELS.get(kind, kind.replace("_", " ").capitalize()),
                days=amount,
                running_balance=running,
                note=note,
            )
        )
    return BalanceHistoryRead(
        employee_id=employee.id,
        employee_name=core._name(employee),
        leave_type_id=leave_type.id,
        leave_type_name=leave_type.name,
        year=year,
        entries=lines,
        available=running,
    )


# ── Encashment ──────────────────────────────────────────────────────────────


async def _type_by_code(session: AsyncSession, user: User, code: str) -> LeaveType:
    leave_type = await session.scalar(
        tenant_scoped(select(LeaveType).where(LeaveType.code == code), LeaveType, user)
    )
    if leave_type is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No leave type with code {code}"
        )
    return leave_type


def leave_year_today() -> int:
    """The leave year balances live in today (calendar years, India's today)."""
    return core.local_today().year


async def live_available(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    leave_type_code: str,
    year: int,
) -> Decimal:
    """The `available` figure the Time page shows for this person and type."""
    employee = await core._employee(session, user, employee_id)
    leave_type = await _type_by_code(session, user, leave_type_code)
    today = await core.today_for(session, user, employee)
    balance, _ = await core.live_balance(session, user, employee, leave_type, year, today)
    return balance.available


async def post_encashment(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    leave_type_code: str,
    year: int,
    days: Decimal,
    source_id: uuid.UUID,
) -> None:
    """Debit encashed days in the caller's transaction.

    Refuses when the live balance no longer covers them — a leave approved
    between the encashment request and its approval spends the same days.
    """
    if days <= engine.ZERO:
        return
    available = await live_available(
        session, user, employee_id=employee_id, leave_type_code=leave_type_code, year=year
    )
    if days > available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The {leave_type_code} balance is now {available} day(s), less than the "
                f"{days} to encash. Recompute the encashment."
            ),
        )
    leave_type = await _type_by_code(session, user, leave_type_code)
    await post(
        session,
        user,
        employee_id=employee_id,
        leave_type_id=leave_type.id,
        year=year,
        kind=_ENCASHMENT,
        days=-days,
        reason=f"Encashed {days} day(s)",
        effective_on=core.local_today(),
        source_type="encashment_request",
        source_id=source_id,
    )


# ── Year end ────────────────────────────────────────────────────────────────


async def _closed_years(session: AsyncSession, user: User) -> list[int]:
    tenant = await session.get(Tenant, user.tenant_id)
    value = (tenant.settings or {}).get(YEAR_END_KEY) if tenant else None
    return [int(v) for v in value] if isinstance(value, list) else []


async def close_year(
    session: AsyncSession, user: User, *, year: int, request: Request | None = None
) -> YearEndRead:
    """Close a leave year: carry up to the policy's limit into the next, lapse the rest.

    For every employee and leave type with a balance in `year`: what is left
    (accrued in full, since the year is over) moves into `year + 1` as
    `carried_forward`, up to `max_carry_forward_days`; the remainder lapses.
    Both are ledger rows on the old year, which leaves it at zero.

    Idempotent per balance: one that already has its carry-out row is skipped,
    so a rerun — or the daily job after a manual close — changes nothing.
    """
    today = core.local_today()
    if year >= today.year:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{year} has not ended yet. A year is closed from 1 January of the next.",
        )
    year_end = date(year, 12, 31)
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
    policies = await core._policies(session, user)
    rows = {
        (row.employee_id, row.leave_type_id): row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(LeaveBalance).where(LeaveBalance.year == year), LeaveBalance, user
                )
            )
        ).all()
    }
    done = {
        (employee_id, leave_type_id)
        for employee_id, leave_type_id in (
            await session.execute(
                tenant_scoped(
                    select(
                        LeaveBalanceAdjustment.employee_id, LeaveBalanceAdjustment.leave_type_id
                    ).where(
                        LeaveBalanceAdjustment.year == year,
                        LeaveBalanceAdjustment.kind.in_(("carry_forward_out", "lapse")),
                    ),
                    LeaveBalanceAdjustment,
                    user,
                )
            )
        ).all()
    }

    closed = already = 0
    carried_total = lapsed_total = engine.ZERO
    for employee in employees:
        for leave_type in leave_types:
            if not leave_type.is_paid:
                continue
            key = (employee.id, leave_type.id)
            if key in done:
                already += 1
                continue
            policy = engine.select_policy(
                [p for p in policies if p.leave_type_id == leave_type.id],
                location_id=employee.location_id,
                grade=employee.grade,
                gender=core.gender_of(employee),
                on=year_end,
            )
            row = rows.get(key)
            if row is None:
                # No balance was kept for this person in that year — a
                # workspace that went live after it, or somebody who joined
                # since. Closing a year nobody tracked would carry forward
                # leave nobody earned here.
                continue
            balance = core._balance_from(
                row, core._accrual(policy, employee, year, year_end), engine.ZERO
            )
            carried, lapsed = engine.year_end_split(
                balance.available, core.carry_cap(leave_type, policy)
            )
            if row is not None:
                row.accrued = balance.accrued
            if carried > engine.ZERO:
                await post(
                    session,
                    user,
                    employee_id=employee.id,
                    leave_type_id=leave_type.id,
                    year=year,
                    kind="carry_forward_out",
                    days=-carried,
                    reason=f"Carried into {year + 1}",
                    effective_on=year_end,
                )
                await post(
                    session,
                    user,
                    employee_id=employee.id,
                    leave_type_id=leave_type.id,
                    year=year + 1,
                    kind=_CARRY_IN,
                    days=carried,
                    reason=f"Carried forward from {year}",
                    effective_on=date(year + 1, 1, 1),
                )
            if lapsed > engine.ZERO:
                limit = core.carry_cap(leave_type, policy)
                await post(
                    session,
                    user,
                    employee_id=employee.id,
                    leave_type_id=leave_type.id,
                    year=year,
                    kind="lapse",
                    days=-lapsed,
                    reason=f"Beyond the {limit}-day carry-forward limit",
                    effective_on=year_end,
                )
            if carried or lapsed:
                closed += 1
                carried_total += carried
                lapsed_total += lapsed

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is not None:
        years = sorted({*await _closed_years(session, user), year})
        tenant.settings = {**(tenant.settings or {}), YEAR_END_KEY: years}
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="leave_balance",
        actor=user,
        changes={
            "year": year,
            "balances_closed": closed,
            "days_carried": str(carried_total),
            "days_lapsed": str(lapsed_total),
            "already_closed": already,
        },
        summary=f"Closed leave year {year}: carried {carried_total}, lapsed {lapsed_total}",
        request=request,
    )
    await session.commit()
    return YearEndRead(
        year=year,
        balances_closed=closed,
        days_carried=carried_total,
        days_lapsed=lapsed_total,
        already_closed=already,
    )


async def expire_comp_offs(session: AsyncSession, user: User, *, today: date) -> dict[str, int]:
    """Lapse comp-off credits past their expiry that were not used.

    Oldest credits are treated as used first. Runs after the year-end close in
    the same job, so a credit from December has already been carried into the
    year it is lapsed from.
    """
    due = (
        await session.scalars(
            tenant_scoped(
                select(LeaveBalanceAdjustment)
                .where(
                    LeaveBalanceAdjustment.kind == "comp_off",
                    LeaveBalanceAdjustment.expired_at.is_(None),
                    LeaveBalanceAdjustment.expires_on.is_not(None),
                    LeaveBalanceAdjustment.expires_on < today,
                )
                .order_by(LeaveBalanceAdjustment.expires_on),
                LeaveBalanceAdjustment,
                user,
            )
        )
    ).all()
    by_person: dict[tuple[uuid.UUID, uuid.UUID], list[LeaveBalanceAdjustment]] = {}
    for credit in due:
        by_person.setdefault((credit.employee_id, credit.leave_type_id), []).append(credit)

    lapsed_people = 0
    lapsed_days = engine.ZERO
    now = datetime.now(UTC)
    for (employee_id, leave_type_id), credits in by_person.items():
        employee = await session.get(Employee, employee_id)
        leave_type = await session.get(LeaveType, leave_type_id)
        if employee is None or leave_type is None:
            continue
        balance, _ = await core.live_balance(session, user, employee, leave_type, today.year, today)
        valid = (
            await session.scalars(
                tenant_scoped(
                    select(LeaveBalanceAdjustment.days).where(
                        LeaveBalanceAdjustment.employee_id == employee_id,
                        LeaveBalanceAdjustment.leave_type_id == leave_type_id,
                        LeaveBalanceAdjustment.kind == "comp_off",
                        LeaveBalanceAdjustment.expired_at.is_(None),
                        LeaveBalanceAdjustment.expires_on >= today,
                    ),
                    LeaveBalanceAdjustment,
                    user,
                )
            )
        ).all()
        lapse = engine.comp_off_lapse(
            available=balance.available,
            expiring=sum((engine.days(c.days) for c in credits), engine.ZERO),
            still_valid=sum((engine.days(d) for d in valid), engine.ZERO),
        )
        for credit in credits:
            credit.expired_at = now
        if lapse > engine.ZERO:
            await post(
                session,
                user,
                employee_id=employee_id,
                leave_type_id=leave_type_id,
                year=today.year,
                kind="comp_off_expiry",
                days=-lapse,
                reason=f"Comp-off unused by {max(c.expires_on for c in credits if c.expires_on)}",
                effective_on=today,
                source_type="comp_off_expiry",
            )
            lapsed_people += 1
            lapsed_days += lapse

    if due:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="leave_balance",
            actor=user,
            changes={"credits_expired": len(due), "days_lapsed": str(lapsed_days)},
            summary=f"Lapsed {lapsed_days} unused comp-off day(s)",
        )
    await session.commit()
    return {"credits_expired": len(due), "people": lapsed_people}


async def year_end_job(session: AsyncSession, user: User, *, today: date) -> dict[str, object]:
    """The scheduler's daily entry point: close last year once, then lapse comp-offs."""
    closed = None
    if today.year - 1 not in await _closed_years(session, user):
        closed = await close_year(session, user, year=today.year - 1)
    expired = await expire_comp_offs(session, user, today=today)
    return {
        "year_closed": today.year - 1 if closed else None,
        "balances_closed": closed.balances_closed if closed else 0,
        "comp_off_credits_expired": expired["credits_expired"],
    }
