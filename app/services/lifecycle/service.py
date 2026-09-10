"""Raising, approving and effecting lifecycle events.

═══════════════════════════════════════════════════════════════════════════════
THE CASCADE IS ALL OR NOTHING
═══════════════════════════════════════════════════════════════════════════════

Effecting a promotion touches the employee record, the position assignment, the
salary series and the letter engine. A half-applied promotion — new grade, no
salary revision, no letter — is very hard to unpick, and worse, nobody knows it
happened. So `effect_event` runs the whole cascade inside a savepoint and rolls
the savepoint back on any failure, leaving the event marked FAILED with the
reason on it.

The savepoint matters rather than the outer transaction: a sweep effecting forty
events must not lose thirty-nine because the fortieth had a bad salary structure.
Each event succeeds or fails on its own, and the ones that failed are visible.

**A future-dated event does not apply early.** `due_events` selects on
`effective_from <= today` and `status == APPROVED`. There is no other path from
APPROVED to EFFECTED, so an approval in March for an April date changes nothing
in March — and lands on 1 April whether or not anybody remembers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus
from app.models.lifecycle import (
    LifecycleBatch,
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    ProbationPolicy,
    ProbationReminder,
)
from app.models.org import Location
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate
from app.services import audit
from app.services.lifecycle import events as event_engine
from app.services.lifecycle import probation as probation_engine
from app.services.payroll import service as payroll_service

__all__ = [
    "approve_event",
    "cancel_event",
    "due_events",
    "effect_due_events",
    "effect_event",
    "employee_history",
    "raise_bulk",
    "raise_event",
    "resolve_probation_drift",
    "send_probation_reminders",
]


# ═══════════════════════════════════════════════════════════════════════════
# Loading
# ═══════════════════════════════════════════════════════════════════════════


async def _event(session: AsyncSession, user: User, event_id: uuid.UUID) -> LifecycleEvent:
    row = await session.scalar(
        tenant_scoped(
            select(LifecycleEvent).where(LifecycleEvent.id == event_id),
            LifecycleEvent,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return row


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    row = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return row


async def _location_facts(
    session: AsyncSession, user: User, location_id: uuid.UUID | None
) -> event_engine.LocationFacts | None:
    if location_id is None:
        return None
    location = await session.scalar(
        tenant_scoped(select(Location).where(Location.id == location_id), Location, user)
    )
    if location is None:
        return None
    return event_engine.LocationFacts(
        location_id=str(location.id),
        name=location.name,
        state_code=location.state_code,
        timezone_name=location.timezone,
    )


async def _pending_approvals_held(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> int:
    """How many decisions the outgoing manager is holding for this person."""
    rows = await session.scalars(
        tenant_scoped(
            select(LeaveRequest).where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.status == LeaveRequestStatus.PENDING,
            ),
            LeaveRequest,
            user,
        )
    )
    return len(rows.all())


async def _facts(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    event_type: LifecycleEventType,
    effective_from: date,
    new_values: dict[str, Any],
    reason: str,
    new_ctc_annual: Decimal | None,
) -> event_engine.EventFacts:
    allowed = event_engine.CHANGED_FIELDS[event_engine.EventType(event_type.value)]
    previous = {
        key: _plain(getattr(employee, key, None)) for key in allowed
    }

    previous_location = await _location_facts(session, user, employee.location_id)
    new_location_id = new_values.get("location_id")
    new_location = (
        await _location_facts(session, user, _as_uuid(new_location_id))
        if new_location_id
        else None
    )

    return event_engine.EventFacts(
        event_type=event_engine.EventType(event_type.value),
        employee_id=str(employee.id),
        effective_from=effective_from,
        previous_values=previous,
        new_values={k: _plain(v) for k, v in new_values.items()},
        reason=reason,
        new_ctc_annual=new_ctc_annual,
        previous_location=previous_location,
        new_location=new_location,
        pending_approvals_held=(
            await _pending_approvals_held(session, user, employee.id)
            if "manager_id" in new_values
            else 0
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Raising and approving
# ═══════════════════════════════════════════════════════════════════════════


async def raise_event(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    event_type: LifecycleEventType,
    effective_from: date,
    new_values: dict[str, Any],
    reason: str,
    new_ctc_annual: Decimal | None = None,
    batch_id: uuid.UUID | None = None,
    today: date | None = None,
    request: Request | None = None,
) -> LifecycleEvent:
    """Draft an event, with its consequences worked out and recorded."""
    resolution_day = today or date.today()
    employee = await _employee(session, user, employee_id)

    facts = await _facts(
        session, user, employee,
        event_type=event_type, effective_from=effective_from,
        new_values=new_values, reason=reason, new_ctc_annual=new_ctc_annual,
    )
    try:
        event_engine.validate_event(facts, today=resolution_day)
    except event_engine.LifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    described = event_engine.describe(facts, today=resolution_day)

    row = LifecycleEvent(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        event_type=event_type,
        effective_from=effective_from,
        previous_values=facts.previous_values,
        new_values=facts.new_values,
        reason=reason,
        new_ctc_annual=new_ctc_annual,
        status=LifecycleStatus.PENDING_APPROVAL,
        # Snapshotted, so "did the approver know PT would change" is answerable
        # about the moment they signed rather than about what the code says now.
        consequences_shown=described["consequences"],
        raised_by_user_id=user.id,
        batch_id=batch_id,
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="lifecycle_event",
        entity_id=row.id,
        summary=(
            f"{event_type.value.replace('_', ' ').title()} raised for "
            f"{employee.employee_code}, effective {effective_from}"
            + (
                f" with {described['payroll_consequences']} payroll consequence(s)"
                if described["payroll_consequences"]
                else ""
            )
        ),
        changes={"new_values": facts.new_values, "reason": reason},
        request=request,
    )
    return row


async def preview_event(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    event_type: LifecycleEventType,
    effective_from: date,
    new_values: dict[str, Any],
    reason: str = "preview",
    new_ctc_annual: Decimal | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """What this event would do, before anybody commits to it.

    The approval screen. An approver signing a cross-state transfer without
    being told PT changes is approving something they did not read.
    """
    employee = await _employee(session, user, employee_id)
    facts = await _facts(
        session, user, employee,
        event_type=event_type, effective_from=effective_from,
        new_values=new_values, reason=reason, new_ctc_annual=new_ctc_annual,
    )
    return event_engine.describe(facts, today=today or date.today())


async def approve_event(
    session: AsyncSession,
    user: User,
    *,
    event_id: uuid.UUID,
    approve: bool = True,
    note: str = "",
    today: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Approve an event. Effecting it is a separate step, on its own date."""
    resolution_day = today or date.today()
    row = await _event(session, user, event_id)

    if row.status is not LifecycleStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This event is {row.status.value}, not awaiting approval.",
        )

    if not approve:
        row.status = LifecycleStatus.CANCELLED
        row.cancelled_reason = note or "Not approved."
        await session.flush()
        return {"event_id": str(row.id), "status": row.status.value}

    row.status = LifecycleStatus.APPROVED
    row.approved_by_user_id = user.id
    row.approved_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="lifecycle_event",
        entity_id=row.id,
        summary=(
            f"{row.event_type.value} approved for effect on {row.effective_from}"
        ),
        changes={"note": note, "consequences_shown": row.consequences_shown},
        request=request,
    )
    return {
        "event_id": str(row.id),
        "status": row.status.value,
        "effective_from": row.effective_from.isoformat(),
        "will_apply": (
            "immediately on the next sweep"
            if row.effective_from <= resolution_day
            else f"on {row.effective_from}"
        ),
    }


async def cancel_event(
    session: AsyncSession, user: User, *, event_id: uuid.UUID, reason: str
) -> dict[str, Any]:
    row = await _event(session, user, event_id)
    if row.status is LifecycleStatus.EFFECTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This event has already been effected. Cancelling it would not undo "
                "the change — raise a reversing event instead."
            ),
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A cancellation needs a reason.",
        )
    row.status = LifecycleStatus.CANCELLED
    row.cancelled_reason = reason
    await session.flush()
    return {"event_id": str(row.id), "status": row.status.value}


# ═══════════════════════════════════════════════════════════════════════════
# Effecting — the cascade
# ═══════════════════════════════════════════════════════════════════════════


async def due_events(
    session: AsyncSession, user: User, *, on: date | None = None
) -> list[LifecycleEvent]:
    """Approved events whose date has arrived.

    The only path from APPROVED to EFFECTED. An approval in March for an April
    date is not selected in March, which is what makes future dating work
    without anybody remembering.
    """
    today = on or date.today()
    rows = await session.scalars(
        tenant_scoped(
            select(LifecycleEvent)
            .where(
                LifecycleEvent.status == LifecycleStatus.APPROVED,
                LifecycleEvent.effective_from <= today,
            )
            .order_by(LifecycleEvent.effective_from),
            LifecycleEvent,
            user,
        )
    )
    return list(rows.all())


async def effect_event(
    session: AsyncSession,
    user: User,
    *,
    event_id: uuid.UUID,
    on: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Apply the whole cascade, or none of it.

    Wrapped in a savepoint rather than relying on the outer transaction: a sweep
    effecting forty events must not lose thirty-nine because the fortieth had a
    bad salary structure. Each event succeeds or fails on its own, and a failure
    leaves a row saying so — a half-applied promotion nobody knows about is much
    worse than a visible failure.
    """
    today = on or date.today()
    row = await _event(session, user, event_id)

    if row.status is not LifecycleStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This event is {row.status.value}. Only an approved event can be "
                "effected."
            ),
        )
    if row.effective_from > today:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This event takes effect on {row.effective_from}, which has not "
                "arrived. Effecting it early would change the record before the "
                "decision was due to apply."
            ),
        )

    row.attempt_count += 1
    applied: list[str] = []

    try:
        async with session.begin_nested():
            employee = await _employee(session, user, row.employee_id)

            # ── The employee record ─────────────────────────────────────────
            for key, value in (row.new_values or {}).items():
                setattr(employee, key, _coerce(key, value))
                applied.append(f"employee.{key}")

            if row.event_type is LifecycleEventType.CONFIRMATION:
                employee.status = EmploymentStatus.ACTIVE
                applied.append("employee.status")

            # ── Position assignment (slice 23) ──────────────────────────────
            if "manager_id" in (row.new_values or {}):
                moved = await _reassign_pending_approvals(
                    session, user, employee_id=employee.id,
                    new_manager_id=_as_uuid(row.new_values["manager_id"]),
                )
                if moved:
                    applied.append(f"reassigned {moved} pending approval(s)")

            # ── Salary revision (slice 01, arrears in slice 12) ─────────────
            if row.new_ctc_annual is not None:
                salary = await _revise_salary(
                    session, user, employee=employee,
                    ctc_annual=Decimal(str(row.new_ctc_annual)),
                    effective_from=row.effective_from,
                    reason=f"{row.event_type.value}: {row.reason}",
                    request=request,
                )
                row.salary_revision_id = salary.id
                applied.append("salary revision")

            row.status = LifecycleStatus.EFFECTED
            row.effected_at = datetime.now(UTC)
            row.failure_reason = None
            await session.flush()

    except Exception as exc:
        # The savepoint is rolled back, so nothing above landed. The event is
        # marked outside it so the failure itself survives.
        row.status = LifecycleStatus.FAILED
        row.failure_reason = f"{type(exc).__name__}: {exc}"[:2000]
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="lifecycle_event",
            entity_id=row.id,
            summary=(
                f"Effecting {row.event_type.value} failed and was rolled back "
                f"entirely: {exc}"
            ),
            request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This event could not be effected and nothing was applied: {exc}. "
                "The whole cascade was rolled back — a half-applied change is very "
                "hard to unpick."
            ),
        ) from exc

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="lifecycle_event",
        entity_id=row.id,
        summary=(
            f"{row.event_type.value} effected on {row.effective_from}: "
            f"{', '.join(applied)}"
        ),
        changes={"applied": applied, "previous_values": row.previous_values},
        request=request,
    )
    return {
        "event_id": str(row.id),
        "status": row.status.value,
        "effected_at": row.effected_at.isoformat(),
        "applied": applied,
    }


async def _revise_salary(
    session: AsyncSession,
    user: User,
    *,
    employee: Employee,
    ctc_annual: Decimal,
    effective_from: date,
    reason: str,
    request: Request | None,
):
    """Route the pay change through payroll's own assignment path.

    Not a copy of it. The offer/payslip identity argument from slice 19 applies
    here too: a promotion letter quoting one breakup and a payslip paying another
    is the same trust incident, arriving eighteen months later.
    """
    from app.models.payroll import EmployeeSalary

    current = await session.scalar(
        tenant_scoped(
            select(EmployeeSalary)
            .where(
                EmployeeSalary.employee_id == employee.id,
                EmployeeSalary.effective_to.is_(None),
            )
            .order_by(EmployeeSalary.effective_from.desc()),
            EmployeeSalary,
            user,
        )
    )
    if current is None:
        raise ValueError(
            "This employee has no current salary, so there is nothing to revise. "
            "Assign a salary structure first."
        )

    return await payroll_service.assign_salary(
        session,
        user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=current.structure_id,
            ctc_annual=ctc_annual,
            effective_from=effective_from,
            revision_reason=reason[:255],
        ),
        request=request,
        # The cascade owns the transaction. A commit here would close the
        # savepoint this is running inside, and the all-or-nothing guarantee
        # would quietly stop holding.
        commit=False,
    )


async def _reassign_pending_approvals(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID, new_manager_id
) -> int:
    """Move what the outgoing manager was holding.

    Left behind, those requests sit with somebody who no longer has any reason to
    look at them, and the employee waits for a decision nobody is going to make.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
                select(LeaveRequest).where(
                    LeaveRequest.employee_id == employee_id,
                    LeaveRequest.status == LeaveRequestStatus.PENDING,
                ),
                LeaveRequest,
                user,
            )
        )
    ).all()

    for request_row in rows:
        # New dicts, not mutated ones. A JSONB column assigned a list whose
        # dicts were edited in place compares equal to what SQLAlchemy already
        # has, so the change is never flagged dirty and never written — the
        # reassignment appears to work, reports a count, and silently does
        # nothing. Found by an integration test asserting on the stored value.
        chain = [
            (
                {
                    **step,
                    "approver_employee_id": (
                        str(new_manager_id) if new_manager_id else None
                    ),
                    "reassigned_reason": "The employee's manager changed.",
                }
                if isinstance(step, dict) and step.get("decision") in (None, "pending")
                else step
            )
            for step in (request_row.approval_chain or [])
        ]
        request_row.approval_chain = chain
    await session.flush()
    return len(rows)


async def effect_due_events(
    session: AsyncSession, user: User, *, on: date | None = None
) -> dict[str, Any]:
    """The scheduled sweep. Each event succeeds or fails on its own."""
    today = on or date.today()
    due = await due_events(session, user, on=today)

    effected: list[str] = []
    failed: list[dict[str, str]] = []

    for event in due:
        try:
            await effect_event(session, user, event_id=event.id, on=today)
            effected.append(str(event.id))
        except HTTPException as exc:
            failed.append({"event_id": str(event.id), "reason": str(exc.detail)})

    return {
        "as_of": today.isoformat(),
        "due": len(due),
        "effected": len(effected),
        "failed": failed,
        "note": (
            "Each event is applied in its own savepoint, so one failure does not "
            "affect the others — and a failed event is marked FAILED with its reason "
            "rather than silently skipped."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Bulk
# ═══════════════════════════════════════════════════════════════════════════


async def raise_bulk(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    event_type: LifecycleEventType,
    effective_from: date,
    reason: str,
    rows: list[dict[str, Any]],
    approve_individually: bool = True,
    today: date | None = None,
) -> dict[str, Any]:
    """One decision producing many events, with per-employee results.

    A four-hundred-person increment cycle where one employee fails validation
    must not fail the other three hundred and ninety-nine, and the one that
    failed has to be named — a count of successes leaves somebody reconciling
    two lists by hand.
    """
    batch = LifecycleBatch(
        tenant_id=user.tenant_id,
        name=name,
        event_type=event_type,
        effective_from=effective_from,
        reason=reason,
        approve_individually=approve_individually,
        raised_by_user_id=user.id,
    )
    session.add(batch)
    await session.flush()

    raised: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []

    for row in rows:
        employee_id = _as_uuid(row.get("employee_id"))
        try:
            async with session.begin_nested():
                event = await raise_event(
                    session,
                    user,
                    employee_id=employee_id,
                    event_type=event_type,
                    effective_from=effective_from,
                    new_values=row.get("new_values", {}),
                    reason=row.get("reason") or reason,
                    new_ctc_annual=(
                        Decimal(str(row["new_ctc_annual"]))
                        if row.get("new_ctc_annual") is not None
                        else None
                    ),
                    batch_id=batch.id,
                    today=today,
                )
            raised.append({"employee_id": str(employee_id), "event_id": str(event.id)})
        except HTTPException as exc:
            rejected.append({"employee_id": str(employee_id), "reason": str(exc.detail)})

    return {
        "batch_id": str(batch.id),
        "name": batch.name,
        "raised": raised,
        "rejected": rejected,
        "raised_count": len(raised),
        "rejected_count": len(rejected),
    }


# ═══════════════════════════════════════════════════════════════════════════
# History
# ═══════════════════════════════════════════════════════════════════════════


async def employee_history(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID
) -> dict[str, Any]:
    """Every event, effective date, approver and letter.

    What an audit or a dispute asks for, and simply the most useful screen in
    Core HR. Cancelled and failed events are included — "we tried and it did not
    go through" is part of the history, and a view that hid it would leave
    somebody looking at a gap.
    """
    employee = await _employee(session, user, employee_id)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(LifecycleEvent)
                .where(LifecycleEvent.employee_id == employee_id)
                .order_by(LifecycleEvent.effective_from.desc()),
                LifecycleEvent,
                user,
            )
        )
    ).all()

    return {
        "employee_id": str(employee.id),
        "employee_code": employee.employee_code,
        "events": [
            {
                "event_id": str(e.id),
                "type": e.event_type.value,
                "effective_from": e.effective_from.isoformat(),
                "status": e.status.value,
                "changes": [
                    {
                        "field": key,
                        "from": (e.previous_values or {}).get(key),
                        "to": value,
                    }
                    for key, value in sorted((e.new_values or {}).items())
                ],
                "salary_revision": (
                    str(e.new_ctc_annual) if e.new_ctc_annual is not None else None
                ),
                "reason": e.reason,
                "approved_by": str(e.approved_by_user_id) if e.approved_by_user_id else None,
                "approved_at": e.approved_at.isoformat() if e.approved_at else None,
                "effected_at": e.effected_at.isoformat() if e.effected_at else None,
                "letter_id": str(e.letter_id) if e.letter_id else None,
                "failure_reason": e.failure_reason,
                "batch_id": str(e.batch_id) if e.batch_id else None,
            }
            for e in rows
        ],
    }


async def state_on(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID, on: date
) -> dict[str, Any]:
    """The employee record as it stood on a given date.

    Reconstructed by walking effected events backwards from today. The current
    record plus the events is the source; there is no separate history table to
    fall out of step with it.
    """
    employee = await _employee(session, user, employee_id)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(LifecycleEvent)
                .where(
                    LifecycleEvent.employee_id == employee_id,
                    LifecycleEvent.status == LifecycleStatus.EFFECTED,
                    LifecycleEvent.effective_from > on,
                )
                .order_by(LifecycleEvent.effective_from.desc()),
                LifecycleEvent,
                user,
            )
        )
    ).all()

    tracked = {"grade", "designation", "department_id", "location_id", "manager_id",
               "employment_type", "status", "notice_period_days"}
    state = {key: _plain(getattr(employee, key, None)) for key in tracked}

    # Newest first: each event undone puts the record back one step.
    for event in rows:
        for key, value in (event.previous_values or {}).items():
            if key in tracked and key in (event.new_values or {}):
                state[key] = value

    return {
        "employee_id": str(employee.id),
        "as_of": on.isoformat(),
        "state": state,
        "events_undone": len(rows),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Probation
# ═══════════════════════════════════════════════════════════════════════════


async def _policy(session: AsyncSession, user: User) -> ProbationPolicy:
    policy = await session.scalar(
        tenant_scoped(select(ProbationPolicy), ProbationPolicy, user)
    )
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "This tenant has no probation policy configured. Auto-confirming on "
                "silence and requiring a decision lead to opposite outcomes for a real "
                "person, so there is no default — somebody has to choose."
            ),
        )
    return policy


async def _review_facts(
    session: AsyncSession, user: User
) -> list[probation_engine.ReviewFacts]:
    from app.models.cycle_config import ProbationReview

    reviews = (
        await session.scalars(
            tenant_scoped(select(ProbationReview), ProbationReview, user)
        )
    ).all()

    sent = (
        await session.scalars(
            tenant_scoped(select(ProbationReminder), ProbationReminder, user)
        )
    ).all()
    by_review: dict[uuid.UUID, set[probation_engine.ReminderStep]] = {}
    for reminder in sent:
        by_review.setdefault(reminder.review_id, set()).add(
            probation_engine.ReminderStep(reminder.step)
        )

    out: list[probation_engine.ReviewFacts] = []
    for review in reviews:
        employee = await session.get(Employee, review.employee_id)
        manager_user_id = None
        skip_user_id = None
        if employee is not None and employee.manager_id:
            manager = await session.get(Employee, employee.manager_id)
            manager_user = await session.scalar(
                select(User).where(User.employee_id == employee.manager_id)
            )
            manager_user_id = str(manager_user.id) if manager_user else None
            if manager is not None and manager.manager_id:
                skip_user = await session.scalar(
                    select(User).where(User.employee_id == manager.manager_id)
                )
                skip_user_id = str(skip_user.id) if skip_user else None

        out.append(
            probation_engine.ReviewFacts(
                review_id=str(review.id),
                employee_id=str(review.employee_id),
                employee_label=(
                    employee.display_name or employee.employee_code
                    if employee
                    else str(review.employee_id)
                ),
                probation_end_date=review.probation_end_date,
                review_due_date=review.review_due_date,
                outcome=review.outcome.value if review.outcome else None,
                manager_user_id=manager_user_id,
                skip_level_user_id=skip_user_id,
                reminders_sent=frozenset(by_review.get(review.id, set())),
                extends_review_id=(
                    str(review.extends_review_id) if review.extends_review_id else None
                ),
            )
        )
    return out


async def send_probation_reminders(
    session: AsyncSession, user: User, *, on: date | None = None
) -> dict[str, Any]:
    """The escalating reminder sweep.

    Unconfirmed employees drifting past probation with nobody noticing is the
    normal failure mode, and each escalation step is a wider circle of people who
    could act.
    """
    today = on or date.today()
    policy = await _policy(session, user)
    reviews = await _review_facts(session, user)
    reminders = probation_engine.due_reminders(reviews, on=today) if False else \
        probation_engine.due_reminders(reviews, today=today)

    from app.services.notifications import dispatch

    sent: list[dict[str, Any]] = []
    for reminder in reminders:
        recipient = reminder.recipient_user_id
        if reminder.step is probation_engine.ReminderStep.HR:
            recipient = (
                str(policy.hr_escalation_user_id)
                if policy.hr_escalation_user_id
                else None
            )

        session.add(
            ProbationReminder(
                tenant_id=user.tenant_id,
                review_id=uuid.UUID(reminder.review_id),
                step=reminder.step.value,
                recipient_user_id=_as_uuid(recipient),
                sent_at=datetime.now(UTC),
                days_until_due=reminder.days_until_due,
                message=reminder.message,
            )
        )
        if recipient:
            await dispatch.notify(
                session,
                tenant_id=user.tenant_id,
                event_key="probation.review_due",
                recipient_user_ids=[uuid.UUID(recipient)],
                payload={"message": reminder.message},
                commit=False,
            )
        sent.append(reminder.as_dict())

    await session.flush()
    return {"as_of": today.isoformat(), "sent": sent, "count": len(sent)}


async def resolve_probation_drift(
    session: AsyncSession, user: User, *, on: date | None = None
) -> dict[str, Any]:
    """What happens to reviews nobody decided, under this tenant's own policy."""
    today = on or date.today()
    policy = await _policy(session, user)
    reviews = await _review_facts(session, user)

    cases = probation_engine.resolve_silence(
        reviews,
        policy=probation_engine.SilencePolicy(policy.silence_policy.value),
        today=today,
        grace_days=policy.grace_days,
    )
    return {
        "as_of": today.isoformat(),
        "policy": policy.silence_policy.value,
        "grace_days": policy.grace_days,
        "cases": [case.as_dict() for case in cases],
        "count": len(cases),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _plain(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return value


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


#: Fields that are stored as ids or enums and arrive as strings.
_UUID_FIELDS = frozenset({"location_id", "department_id", "manager_id"})
_DATE_FIELDS = frozenset({"probation_end_date"})


def _coerce(key: str, value: Any) -> Any:
    """Turn a JSON value back into what the column expects."""
    if value is None:
        return None
    if key in _UUID_FIELDS:
        return _as_uuid(value)
    if key in _DATE_FIELDS:
        return date.fromisoformat(value) if isinstance(value, str) else value
    if key == "employment_type":
        from app.models.employee import EmploymentType

        return EmploymentType(value) if isinstance(value, str) else value
    if key == "status":
        return EmploymentStatus(value) if isinstance(value, str) else value
    return value
