"""Overtime: from locked attendance to an approved, priced payslip line.

The flow, and who decides what:

1. **Sync.** Every attendance day with overtime minutes becomes a pending
   `OvertimeEntry`, counted by the policy for the employee's location: below the
   policy's minimum it is not counted, it is rounded down to the policy's block,
   and capped per day. Rest-day work counts only where the policy says it does.
   Sync never touches a decided entry; a pending one follows its attendance day
   if the day is recomputed.

   Where the policy sets a weekly threshold (the Factories Act's 48 hours), a
   finished week is assessed too: hours worked in it beyond the threshold, less
   what was already counted day by day, become one `weekly` entry dated the
   week's last day. A week is assessed only once it is over, so a day's entry a
   manager has already decided is never revisited.
2. **Decide.** The line manager or HR approves (fewer minutes if they choose,
   never more) or rejects with a reason. Nobody decides their own. The policy's
   quarterly cap is applied at approval: minutes past it are refused, and the
   note says so. Where the policy needs no approval, sync approves on its behalf.
3. **Price.** At approval, from the salary in force *on the day worked*: the
   ordinary hourly rate is the monthly amount of the policy's wage components
   over its days divisor and hours per day, and overtime is that times the
   multiplier. Every number is the policy's, none is the code's.
4. **Pay.** `payroll.inputs.OvertimeProvider` pays approved entries in the next
   run and marks them paid when that run is approved.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import AttendanceDay, AttendanceStatus
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.org import Location
from app.models.payroll_inputs import OvertimeEntry, OvertimePolicy, OvertimeStatus
from app.models.user import User
from app.schemas.payroll_inputs import (
    OvertimeEntryRead,
    OvertimePolicyCreate,
    OvertimePolicyRead,
    OvertimePolicyUpdate,
    OvertimeSyncResult,
)
from app.services import audit

__all__ = [
    "counted_minutes",
    "create_policy",
    "decide",
    "decide_many",
    "list_entries",
    "list_policies",
    "price",
    "sync",
    "update_policy",
]

_RATE = Decimal("0.0001")
_RUPEE = Decimal("0.01")
_REST_DAYS = (AttendanceStatus.WEEK_OFF, AttendanceStatus.HOLIDAY)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


# ── Pure rules ──────────────────────────────────────────────────────────────


def counted_minutes(policy: OvertimePolicy, recorded: int) -> int:
    """A day's overtime as the policy counts it: minimum, rounding, daily cap."""
    if recorded <= 0 or recorded < policy.minimum_minutes:
        return 0
    minutes = recorded
    if policy.rounding_minutes:
        minutes = (minutes // policy.rounding_minutes) * policy.rounding_minutes
    if policy.daily_cap_minutes is not None:
        minutes = min(minutes, policy.daily_cap_minutes)
    return max(0, minutes)


def price(
    policy: OvertimePolicy, monthly_wage: Decimal, minutes: int
) -> tuple[Decimal, Decimal, str]:
    """(hourly rate, amount, basis) for `minutes` of overtime."""
    divisor = Decimal(policy.days_divisor) * Decimal(str(policy.hours_per_day))
    hourly = (monthly_wage / divisor).quantize(_RATE, rounding=ROUND_HALF_UP)
    multiplier = Decimal(str(policy.rate_multiplier))
    amount = (hourly * multiplier * Decimal(minutes) / Decimal(60)).quantize(
        _RUPEE, rounding=ROUND_HALF_UP
    )
    codes = " + ".join(policy.wage_component_codes)
    basis = (
        f"{minutes} min at {multiplier}x the ordinary rate: {codes} of {monthly_wage} a month "
        f"÷ {policy.days_divisor} days ÷ {policy.hours_per_day} hours = {hourly} an hour."
    )
    return hourly, amount, basis


def weekly_excess(policy: OvertimePolicy, week: list[tuple[int, int, bool]]) -> int:
    """Minutes of a week past the policy's weekly threshold, not counted daily.

    `week` is (worked minutes, daily overtime minutes, is a rest day) for each
    day. A rest day's work counts toward the week only where rest-day work
    counts as overtime at all.
    """
    if not policy.weekly_threshold_minutes:
        return 0
    worked = counted = 0
    for worked_minutes, overtime_minutes, is_rest in week:
        if is_rest and not policy.count_rest_days:
            continue
        worked += max(0, worked_minutes)
        counted += counted_minutes(policy, overtime_minutes)
    excess = worked - policy.weekly_threshold_minutes - counted
    if excess <= 0:
        return 0
    if policy.rounding_minutes:
        excess = (excess // policy.rounding_minutes) * policy.rounding_minutes
    return max(0, excess)


def _is_week_end(policy: OvertimePolicy, day: date) -> bool:
    return day.weekday() == (policy.week_starts_on + 6) % 7


def _quarter_bounds(day: date) -> tuple[date, date]:
    first_month = ((day.month - 1) // 3) * 3 + 1
    start = date(day.year, first_month, 1)
    end = date(day.year + (first_month == 10), (first_month + 3 - 1) % 12 + 1, 1)
    return start, end


# ── Policies ────────────────────────────────────────────────────────────────


async def _location_names(session: AsyncSession, user: User, ids: set) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(Location.id, Location.name).where(Location.id.in_(wanted)), Location, user
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


def _policy_read(row: OvertimePolicy, locations: dict[uuid.UUID, str]) -> OvertimePolicyRead:
    return OvertimePolicyRead(
        id=row.id,
        name=row.name,
        location_id=row.location_id,
        location_name=locations.get(row.location_id) if row.location_id else None,
        rate_multiplier=Decimal(str(row.rate_multiplier)),
        wage_component_codes=list(row.wage_component_codes or []),
        days_divisor=row.days_divisor,
        hours_per_day=Decimal(str(row.hours_per_day)),
        minimum_minutes=row.minimum_minutes,
        rounding_minutes=row.rounding_minutes,
        daily_cap_minutes=row.daily_cap_minutes,
        quarterly_cap_minutes=row.quarterly_cap_minutes,
        count_rest_days=row.count_rest_days,
        requires_approval=row.requires_approval,
        weekly_threshold_minutes=row.weekly_threshold_minutes,
        week_starts_on=row.week_starts_on,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        is_active=row.is_active,
    )


async def list_policies(session: AsyncSession, user: User) -> list[OvertimePolicyRead]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(OvertimePolicy).order_by(
                    OvertimePolicy.location_id.is_(None).desc(),
                    OvertimePolicy.effective_from.desc(),
                ),
                OvertimePolicy,
                user,
            )
        )
    ).all()
    locations = await _location_names(session, user, {r.location_id for r in rows})
    return [_policy_read(r, locations) for r in rows]


async def create_policy(
    session: AsyncSession,
    user: User,
    payload: OvertimePolicyCreate,
    *,
    request: Request | None = None,
) -> OvertimePolicyRead:
    permissions.require_hr(user, detail="Only HR sets overtime policy.")
    if payload.location_id is not None:
        found = await session.scalar(
            tenant_scoped(
                select(Location.id).where(Location.id == payload.location_id), Location, user
            )
        )
        if found is None:
            raise _unprocessable("That location is not in this workspace.")
    codes = [c.strip().upper() for c in payload.wage_component_codes if c.strip()]
    if not codes:
        raise _unprocessable("Name the salary components that make up the ordinary rate.")

    # A new policy for a location closes the one it replaces the day before.
    previous = (
        await session.scalars(
            tenant_scoped(
                select(OvertimePolicy).where(
                    OvertimePolicy.is_active.is_(True),
                    OvertimePolicy.location_id.is_(None)
                    if payload.location_id is None
                    else OvertimePolicy.location_id == payload.location_id,
                    or_(
                        OvertimePolicy.effective_to.is_(None),
                        OvertimePolicy.effective_to >= payload.effective_from,
                    ),
                ),
                OvertimePolicy,
                user,
            )
        )
    ).all()
    for older in previous:
        if older.effective_from >= payload.effective_from:
            raise _unprocessable(
                f"'{older.name}' already starts on {older.effective_from}. A replacement "
                "policy must start after the one it replaces."
            )
        older.effective_to = date.fromordinal(payload.effective_from.toordinal() - 1)

    row = OvertimePolicy(
        tenant_id=user.tenant_id,
        **{**payload.model_dump(), "wage_component_codes": codes, "name": payload.name.strip()},
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="overtime_policy",
        entity_id=row.id,
        actor=user,
        changes={
            "multiplier": str(row.rate_multiplier),
            "components": codes,
            "days_divisor": row.days_divisor,
            "hours_per_day": str(row.hours_per_day),
            "quarterly_cap_minutes": row.quarterly_cap_minutes,
            "weekly_threshold_minutes": row.weekly_threshold_minutes,
            "effective_from": row.effective_from.isoformat(),
            "superseded": [str(o.id) for o in previous],
        },
        summary=f"Created overtime policy '{row.name}'",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _policy_read(row, await _location_names(session, user, {row.location_id}))


async def update_policy(
    session: AsyncSession,
    user: User,
    policy_id: uuid.UUID,
    payload: OvertimePolicyUpdate,
    *,
    request: Request | None = None,
) -> OvertimePolicyRead:
    permissions.require_hr(user, detail="Only HR sets overtime policy.")
    row = await session.scalar(
        tenant_scoped(
            select(OvertimePolicy).where(OvertimePolicy.id == policy_id), OvertimePolicy, user
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")
    changes = payload.model_dump(exclude_unset=True)
    if (
        "effective_to" in changes
        and changes["effective_to"] is not None
        and (changes["effective_to"] < row.effective_from)
    ):
        raise _unprocessable("A policy cannot end before it starts.")
    before = {key: str(getattr(row, key)) for key in changes}
    for key, value in changes.items():
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="overtime_policy",
        entity_id=row.id,
        actor=user,
        changes={"before": before, "after": {k: str(v) for k, v in changes.items()}},
        summary=f"Updated overtime policy '{row.name}'",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _policy_read(row, await _location_names(session, user, {row.location_id}))


async def _policies_in_force(
    session: AsyncSession, user: User, start: date, end: date
) -> list[OvertimePolicy]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(OvertimePolicy).where(
                        OvertimePolicy.is_active.is_(True),
                        OvertimePolicy.effective_from <= end,
                        or_(
                            OvertimePolicy.effective_to.is_(None),
                            OvertimePolicy.effective_to >= start,
                        ),
                    ),
                    OvertimePolicy,
                    user,
                )
            )
        ).all()
    )


def _policy_for(
    policies: list[OvertimePolicy], location_id: uuid.UUID | None, day: date
) -> OvertimePolicy | None:
    in_force = [
        p
        for p in policies
        if p.effective_from <= day and (p.effective_to is None or p.effective_to >= day)
    ]
    own = [p for p in in_force if location_id is not None and p.location_id == location_id]
    if own:
        return max(own, key=lambda p: p.effective_from)
    default = [p for p in in_force if p.location_id is None]
    return max(default, key=lambda p: p.effective_from) if default else None


# ── Sync from attendance ────────────────────────────────────────────────────


async def _monthly_wage(
    session: AsyncSession, user: User, employee_id: uuid.UUID, policy: OvertimePolicy, day: date
) -> Decimal:
    from app.services.payroll.service import active_salary_on

    salary = await active_salary_on(session, user, employee_id, day)
    if salary is None:
        raise _unprocessable(
            f"No salary was in force on {day:%d %b %Y}, so this overtime cannot be priced. "
            "Assign the salary first."
        )
    wanted = {c.upper() for c in policy.wage_component_codes}
    wage = sum(
        (
            Decimal(str(c.monthly_amount))
            for c in salary.components
            if c.component_code.upper() in wanted
        ),
        start=Decimal(0),
    )
    if wage <= 0:
        raise _unprocessable(
            f"The salary in force on {day:%d %b %Y} has none of the components "
            f"{', '.join(sorted(wanted))} that this overtime policy prices on."
        )
    return wage


async def _approve_entry(
    session: AsyncSession,
    user: User,
    entry: OvertimeEntry,
    policy: OvertimePolicy,
    minutes: int,
    note: str | None,
    *,
    decided_by: uuid.UUID | None,
) -> None:
    """Price and approve one entry, applying the quarterly cap."""
    notes = [note] if note else []
    if policy.quarterly_cap_minutes is not None:
        q_start, q_end = _quarter_bounds(entry.attendance_date)
        used = int(
            await session.scalar(
                tenant_scoped(
                    select(func.coalesce(func.sum(OvertimeEntry.approved_minutes), 0)).where(
                        OvertimeEntry.employee_id == entry.employee_id,
                        OvertimeEntry.status.in_([OvertimeStatus.APPROVED, OvertimeStatus.PAID]),
                        OvertimeEntry.attendance_date >= q_start,
                        OvertimeEntry.attendance_date < q_end,
                        OvertimeEntry.id != entry.id,
                    ),
                    OvertimeEntry,
                    user,
                )
            )
            or 0
        )
        room = max(0, policy.quarterly_cap_minutes - used)
        if room <= 0:
            raise _unprocessable(
                f"The quarter's overtime limit of {policy.quarterly_cap_minutes} minutes is "
                "already used. Approving more would exceed the statutory limit the policy holds."
            )
        if minutes > room:
            notes.append(
                f"Reduced from {minutes} to {room} min: the quarter's limit of "
                f"{policy.quarterly_cap_minutes} min."
            )
            minutes = room
    wage = await _monthly_wage(session, user, entry.employee_id, policy, entry.attendance_date)
    hourly, amount, basis = price(policy, wage, minutes)
    if entry.kind == "weekly":
        basis = (
            f"Week ending {entry.attendance_date:%d %b %Y}: hours past the weekly limit of "
            f"{(policy.weekly_threshold_minutes or 0) // 60} h not counted day by day. {basis}"
        )
    entry.approved_minutes = minutes
    entry.hourly_rate = hourly
    entry.rate_multiplier = Decimal(str(policy.rate_multiplier))
    entry.amount = amount
    entry.basis = basis
    entry.status = OvertimeStatus.APPROVED
    entry.decided_by_user_id = decided_by
    entry.decided_at = datetime.now(UTC)
    entry.decision_note = " ".join(notes) or None


async def sync(
    session: AsyncSession,
    user: User,
    *,
    start: date,
    end: date,
    request: Request | None = None,
) -> OvertimeSyncResult:
    """Bring overtime entries in line with attendance for a date range."""
    permissions.require_hr(user, detail="Only HR syncs overtime from attendance.")
    if end < start:
        raise _unprocessable("The range ends before it starts.")
    if (end - start).days > 92:
        raise _unprocessable("Sync at most a quarter at a time.")

    # A week ending in the range may begin up to six days before it.
    fetch_start = date.fromordinal(start.toordinal() - 6)
    policies = await _policies_in_force(session, user, fetch_start, end)
    fetched = (
        await session.execute(
            tenant_scoped(
                select(AttendanceDay, Employee.location_id, Employee.employee_code)
                .join(Employee, AttendanceDay.employee_id == Employee.id)
                .where(
                    AttendanceDay.attendance_date >= fetch_start,
                    AttendanceDay.attendance_date <= end,
                ),
                AttendanceDay,
                user,
            )
        )
    ).all()
    days = [row for row in fetched if row[0].attendance_date >= start]
    existing = {
        (e.employee_id, e.attendance_date, e.kind): e
        for e in (
            await session.scalars(
                tenant_scoped(
                    select(OvertimeEntry).where(
                        OvertimeEntry.attendance_date >= start,
                        OvertimeEntry.attendance_date <= end,
                    ),
                    OvertimeEntry,
                    user,
                )
            )
        ).all()
    }

    created = updated = removed = auto = 0
    without_policy: list[str] = []
    seen: set[tuple[uuid.UUID, date, str]] = set()
    for day, location_id, code in days:
        key = (day.employee_id, day.attendance_date, "daily")
        seen.add(key)
        entry = existing.get(key)
        recorded = int(day.overtime_minutes or 0)
        is_rest = day.status in _REST_DAYS
        policy = _policy_for(policies, location_id, day.attendance_date) if recorded else None
        if recorded and policy is None:
            without_policy.append(f"{code} on {day.attendance_date.isoformat()}")
        eligible = 0
        if policy is not None and (not is_rest or policy.count_rest_days):
            eligible = counted_minutes(policy, recorded)

        if entry is not None and entry.status is not OvertimeStatus.PENDING:
            continue  # decided: attendance moving later does not reopen it
        if eligible <= 0:
            if entry is not None:
                # No longer overtime after a recompute: the pending claim goes too.
                no_longer_due = entry
                await session.delete(no_longer_due)
                removed += 1
            continue
        if entry is None:
            entry = OvertimeEntry(
                tenant_id=user.tenant_id,
                employee_id=day.employee_id,
                attendance_date=day.attendance_date,
                policy_id=policy.id,  # type: ignore[union-attr]
                recorded_minutes=recorded,
                eligible_minutes=eligible,
                is_rest_day=is_rest,
                status=OvertimeStatus.PENDING,
            )
            session.add(entry)
            created += 1
        elif (entry.recorded_minutes, entry.eligible_minutes, entry.policy_id) != (
            recorded,
            eligible,
            policy.id,  # type: ignore[union-attr]
        ):
            entry.recorded_minutes = recorded
            entry.eligible_minutes = eligible
            entry.policy_id = policy.id  # type: ignore[union-attr]
            entry.is_rest_day = is_rest
            updated += 1
        if policy is not None and not policy.requires_approval:
            await session.flush()
            try:
                await _approve_entry(
                    session,
                    user,
                    entry,
                    policy,
                    eligible,
                    "Approved by policy: this overtime policy needs no approval.",
                    decided_by=None,
                )
                auto += 1
            except HTTPException:
                pass  # unpriceable or over the cap: left pending for a person to look at

    # ── Weekly: finished weeks past the policy's weekly threshold ───────────
    from app.services.attendance.service import local_today

    today = await local_today(session, user)
    by_employee: dict[uuid.UUID, dict[date, AttendanceDay]] = {}
    where: dict[uuid.UUID, uuid.UUID | None] = {}
    for day, location_id, _code in fetched:
        by_employee.setdefault(day.employee_id, {})[day.attendance_date] = day
        where[day.employee_id] = location_id
    last_assessable = min(end, date.fromordinal(today.toordinal() - 1))
    for employee_id, calendar_days in by_employee.items():
        for ordinal in range(start.toordinal(), last_assessable.toordinal() + 1):
            week_end = date.fromordinal(ordinal)
            policy = _policy_for(policies, where[employee_id], week_end)
            if policy is None or not policy.weekly_threshold_minutes:
                continue
            if not _is_week_end(policy, week_end):
                continue
            week = [
                (d.worked_minutes or 0, d.overtime_minutes or 0, d.status in _REST_DAYS)
                for offset in range(7)
                if (d := calendar_days.get(date.fromordinal(ordinal - offset))) is not None
            ]
            excess = weekly_excess(policy, week)
            key = (employee_id, week_end, "weekly")
            seen.add(key)
            entry = existing.get(key)
            if entry is not None and entry.status is not OvertimeStatus.PENDING:
                continue
            if excess <= 0:
                if entry is not None:
                    no_longer_due = entry
                    await session.delete(no_longer_due)
                    removed += 1
                continue
            if entry is None:
                entry = OvertimeEntry(
                    tenant_id=user.tenant_id,
                    employee_id=employee_id,
                    attendance_date=week_end,
                    kind="weekly",
                    policy_id=policy.id,
                    recorded_minutes=excess,
                    eligible_minutes=excess,
                    is_rest_day=False,
                    status=OvertimeStatus.PENDING,
                )
                session.add(entry)
                created += 1
            elif (entry.recorded_minutes, entry.policy_id) != (excess, policy.id):
                entry.recorded_minutes = excess
                entry.eligible_minutes = excess
                entry.policy_id = policy.id
                updated += 1
            if not policy.requires_approval:
                await session.flush()
                try:
                    await _approve_entry(
                        session,
                        user,
                        entry,
                        policy,
                        excess,
                        "Approved by policy: this overtime policy needs no approval.",
                        decided_by=None,
                    )
                    auto += 1
                except HTTPException:
                    pass

    # A pending daily entry whose attendance day no longer exists goes with it.
    for key, pending in existing.items():
        if key[2] != "daily":
            continue
        if key not in seen and pending.status is OvertimeStatus.PENDING:
            await session.delete(pending)
            removed += 1

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="overtime_entry",
        actor=user,
        changes={
            "start": start.isoformat(),
            "end": end.isoformat(),
            "created": created,
            "updated": updated,
            "removed": removed,
            "auto_approved": auto,
        },
        summary=f"Synced overtime from attendance for {start} to {end}",
        request=request,
    )
    await session.commit()
    return OvertimeSyncResult(
        created=created,
        updated=updated,
        removed=removed,
        auto_approved=auto,
        without_policy=without_policy[:200],
    )


# ── Reads and decisions ─────────────────────────────────────────────────────


async def _may_decide(session: AsyncSession, user: User, employee_id: uuid.UUID) -> bool:
    if user.employee_id is not None and user.employee_id == employee_id:
        return False
    if permissions.is_hr(user):
        return True
    return await permissions.manages(session, user, employee_id)


async def _reads(
    session: AsyncSession, user: User, rows: list[OvertimeEntry]
) -> list[OvertimeEntryRead]:
    if not rows:
        return []
    employees = {
        e.id: e
        for e in (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(Employee.id.in_({r.employee_id for r in rows})),
                    Employee,
                    user,
                )
            )
        ).all()
    }
    policies = {
        p.id: p.name
        for p in (
            await session.scalars(
                tenant_scoped(
                    select(OvertimePolicy).where(
                        OvertimePolicy.id.in_({r.policy_id for r in rows})
                    ),
                    OvertimePolicy,
                    user,
                )
            )
        ).all()
    }
    decider_ids = {r.decided_by_user_id for r in rows if r.decided_by_user_id}
    deciders = (
        {
            row[0]: row[1]
            for row in (
                await session.execute(
                    tenant_scoped(
                        select(User.id, User.full_name).where(User.id.in_(decider_ids)),
                        User,
                        user,
                    )
                )
            ).all()
        }
        if decider_ids
        else {}
    )
    line = None if permissions.is_hr(user) else await permissions.reporting_line(session, user)
    reads = []
    for row in rows:
        employee = employees.get(row.employee_id)
        may = (
            row.status is OvertimeStatus.PENDING
            and (user.employee_id is None or user.employee_id != row.employee_id)
            and (line is None or row.employee_id in line)
        )
        reads.append(
            OvertimeEntryRead(
                id=row.id,
                employee_id=row.employee_id,
                employee_code=employee.employee_code if employee else None,
                employee_name=employee.full_name if employee else None,
                attendance_date=row.attendance_date,
                kind=row.kind,
                policy_id=row.policy_id,
                policy_name=policies.get(row.policy_id),
                recorded_minutes=row.recorded_minutes,
                eligible_minutes=row.eligible_minutes,
                approved_minutes=row.approved_minutes,
                is_rest_day=row.is_rest_day,
                hourly_rate=Decimal(str(row.hourly_rate)) if row.hourly_rate is not None else None,
                rate_multiplier=(
                    Decimal(str(row.rate_multiplier)) if row.rate_multiplier is not None else None
                ),
                amount=Decimal(str(row.amount)) if row.amount is not None else None,
                basis=row.basis,
                status=row.status,
                decided_by_name=deciders.get(row.decided_by_user_id)
                if row.decided_by_user_id
                else (
                    "Policy"
                    if row.decided_at and row.status is not OvertimeStatus.PENDING
                    else None
                ),
                decided_at=row.decided_at,
                decision_note=row.decision_note,
                paid_in_run_id=row.paid_in_run_id,
                caller_may_decide=may,
            )
        )
    return reads


async def list_entries(
    session: AsyncSession,
    user: User,
    *,
    entry_status: OvertimeStatus | None = None,
    start: date | None = None,
    end: date | None = None,
    employee_id: uuid.UUID | None = None,
    mine: bool = False,
    limit: int = 500,
) -> list[OvertimeEntryRead]:
    """Entries the caller may see: their own, their reporting line's, or everyone's (HR)."""
    conditions = []
    if mine:
        conditions.append(OvertimeEntry.employee_id == permissions.own_employee_id(user))
    else:
        visible = await permissions.visible_employee_ids(session, user)
        if visible is not None:
            conditions.append(OvertimeEntry.employee_id.in_(visible or {uuid.uuid4()}))
    if employee_id is not None:
        conditions.append(OvertimeEntry.employee_id == employee_id)
    if entry_status is not None:
        conditions.append(OvertimeEntry.status == entry_status)
    if start is not None:
        conditions.append(OvertimeEntry.attendance_date >= start)
    if end is not None:
        conditions.append(OvertimeEntry.attendance_date <= end)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(OvertimeEntry)
                .where(and_(*conditions) if conditions else True)
                .order_by(OvertimeEntry.attendance_date.desc())
                .limit(limit),
                OvertimeEntry,
                user,
            )
        )
    ).all()
    return await _reads(session, user, list(rows))


async def _decide_one(
    session: AsyncSession,
    user: User,
    entry: OvertimeEntry,
    *,
    approve_it: bool,
    minutes: int | None,
    note: str | None,
) -> None:
    if not await _may_decide(session, user, entry.employee_id):
        raise permissions.forbidden(
            "Overtime is decided by the employee's line manager or HR, and never by the person "
            "who worked it."
        )
    if entry.status is not OvertimeStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This overtime is already {entry.status.value}.",
        )
    clean = (note or "").strip() or None
    if not approve_it:
        if clean is None:
            raise _unprocessable("Say why this overtime is rejected.")
        entry.status = OvertimeStatus.REJECTED
        entry.decided_by_user_id = user.id
        entry.decided_at = datetime.now(UTC)
        entry.decision_note = clean
        return
    chosen = minutes if minutes is not None else entry.eligible_minutes
    if chosen > entry.eligible_minutes:
        raise _unprocessable(
            f"At most {entry.eligible_minutes} minutes can be approved: that is what the "
            "attendance record and the policy count."
        )
    policy = await session.scalar(
        tenant_scoped(
            select(OvertimePolicy).where(OvertimePolicy.id == entry.policy_id),
            OvertimePolicy,
            user,
        )
    )
    if policy is None:
        raise _unprocessable("The overtime policy for this day no longer exists.")
    await _approve_entry(session, user, entry, policy, chosen, clean, decided_by=user.id)


async def decide(
    session: AsyncSession,
    user: User,
    entry_id: uuid.UUID,
    *,
    approve_it: bool,
    minutes: int | None = None,
    note: str | None = None,
    request: Request | None = None,
) -> OvertimeEntryRead:
    entry = await session.scalar(
        tenant_scoped(
            select(OvertimeEntry).where(OvertimeEntry.id == entry_id), OvertimeEntry, user
        )
    )
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Overtime not found")
    await _decide_one(session, user, entry, approve_it=approve_it, minutes=minutes, note=note)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve_it else AuditAction.REJECT,
        entity_type="overtime_entry",
        entity_id=entry.id,
        actor=user,
        changes={
            "status": entry.status.value,
            "minutes": entry.approved_minutes,
            "amount": str(entry.amount) if entry.amount is not None else None,
            "note": entry.decision_note,
        },
        summary=f"{'Approved' if approve_it else 'Rejected'} overtime of {entry.attendance_date}",
        request=request,
    )
    await session.commit()
    await session.refresh(entry)
    return (await _reads(session, user, [entry]))[0]


async def decide_many(
    session: AsyncSession,
    user: User,
    entry_ids: list[uuid.UUID],
    *,
    approve_it: bool,
    note: str | None = None,
    request: Request | None = None,
) -> list[OvertimeEntryRead]:
    """Decide several at once, all or nothing."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(OvertimeEntry)
                .where(OvertimeEntry.id.in_(entry_ids))
                .order_by(OvertimeEntry.attendance_date),
                OvertimeEntry,
                user,
            )
        )
    ).all()
    if len(rows) != len(set(entry_ids)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Overtime not found")
    try:
        for entry in rows:
            await _decide_one(session, user, entry, approve_it=approve_it, minutes=None, note=note)
            await session.flush()
    except HTTPException:
        await session.rollback()
        raise
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve_it else AuditAction.REJECT,
        entity_type="overtime_entry",
        actor=user,
        changes={"entries": len(rows), "note": (note or "").strip() or None},
        summary=f"{'Approved' if approve_it else 'Rejected'} {len(rows)} overtime entries",
        request=request,
    )
    await session.commit()
    for entry in rows:
        await session.refresh(entry)
    return await _reads(session, user, list(rows))


async def nightly_sync(session: AsyncSession, user: User, *, as_of: date) -> dict:
    """The scheduler's sync: the last seven days, so a late regularisation is caught."""
    start = date.fromordinal(as_of.toordinal() - 7)
    result = await sync(session, user, start=start, end=as_of)
    return result.model_dump()
