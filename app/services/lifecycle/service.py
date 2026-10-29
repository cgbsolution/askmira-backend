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

**The letter is part of the cascade.** Where the tenant has an active template
for the event (see `_letter_template_for`), the letter is issued inside the same
savepoint, so a promotion whose letter cannot be generated is not effected
either. Where there is no template, the event is effected and the result says
plainly that no letter was issued.

**Nobody moves their own record.** An HR admin cannot raise, decide, effect or
cancel an event about themselves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.documents import LetterCategory, LetterTemplate
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus
from app.models.lifecycle import (
    LifecycleBatch,
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    ProbationPolicy,
    ProbationReminder,
    SilencePolicy,
)
from app.models.org import Department, LegalEntity, Location
from app.models.timesheets import Project
from app.models.user import User
from app.schemas.lifecycle import EventRead, ProbationPolicyIn, ProbationPolicyRead
from app.schemas.payroll import EmployeeSalaryCreate
from app.services import audit
from app.services.documents import service as letter_service
from app.services.lifecycle import events as event_engine
from app.services.lifecycle import probation as probation_engine
from app.services.notifications.dispatch import notify, register_event
from app.services.payroll import companies
from app.services.payroll import service as payroll_service

#: A lifecycle event waiting on one approver in its chain.
EVENT_STEP_WAITING = "lifecycle.approval.waiting"
register_event(
    EVENT_STEP_WAITING,
    "A {event} for {employee_name} is waiting on you",
    "A {event} for {employee_name}, effective {effective_from}, needs your approval. "
    "Open Approvals to decide it.",
)

__all__ = [
    "approve_event",
    "cancel_event",
    "due_events",
    "effect_due_events",
    "effect_event",
    "employee_history",
    "get_probation_policy",
    "list_batches",
    "list_events",
    "raise_bulk",
    "raise_event",
    "resolve_probation_drift",
    "send_probation_reminders",
    "set_probation_policy",
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


def _refuse_self(user: User, employee_id: uuid.UUID, *, doing: str) -> None:
    if user.employee_id is not None and employee_id == user.employee_id:
        raise permissions.forbidden(
            f"You cannot {doing} a lifecycle event about yourself. Somebody else in HR has to."
        )


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


async def _pending_approvals_held(session: AsyncSession, user: User, employee_id: uuid.UUID) -> int:
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


async def _check_new_references(
    session: AsyncSession, user: User, employee_id: uuid.UUID, new_values: dict[str, Any]
) -> None:
    """An event may only point the record at things in this workspace."""
    for key, model, label in (
        ("legal_entity_id", LegalEntity, "company"),
        ("project_id", Project, "project"),
        ("location_id", Location, "location"),
        ("department_id", Department, "department"),
        ("manager_id", Employee, "manager"),
    ):
        value = new_values.get(key)
        if value in (None, ""):
            continue
        try:
            target = _as_uuid(value)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{value}' is not a valid {label} id.",
            ) from exc
        if key == "manager_id" and target == employee_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="An employee cannot report to themselves.",
            )
        found = await session.scalar(
            tenant_scoped(select(model.id).where(model.id == target), model, user)
        )
        if found is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"That {label} is not part of this workspace.",
            )


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
    previous = {key: _plain(getattr(employee, key, None)) for key in allowed}

    previous_location = await _location_facts(session, user, employee.location_id)
    new_location_id = new_values.get("location_id")
    new_location = (
        await _location_facts(session, user, _as_uuid(new_location_id)) if new_location_id else None
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
    status_on_raise: LifecycleStatus = LifecycleStatus.PENDING_APPROVAL,
) -> LifecycleEvent:
    """Draft an event, with its consequences worked out and recorded.

    `status_on_raise` is DRAFT only for an agent: its event waits, outside every
    approver's queue, until a person approves the agent's proposal to submit it.
    """
    _refuse_self(user, employee_id, doing="raise")
    resolution_day = today or date.today()
    employee = await _employee(session, user, employee_id)
    await _check_new_references(session, user, employee.id, new_values)
    if new_values.get("legal_entity_id"):
        await companies.check_no_later_change(session, user, employee.id, effective_from)

    facts = await _facts(
        session,
        user,
        employee,
        event_type=event_type,
        effective_from=effective_from,
        new_values=new_values,
        reason=reason,
        new_ctc_annual=new_ctc_annual,
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
        status=status_on_raise,
        # Snapshotted, so "did the approver know PT would change" is answerable
        # about the moment they signed rather than about what the code says now.
        consequences_shown=described["consequences"],
        raised_by_user_id=user.id,
        batch_id=batch_id,
    )
    session.add(row)
    await session.flush()
    if status_on_raise is LifecycleStatus.PENDING_APPROVAL:
        await attach_chain(session, user, row, employee)

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


async def attach_chain(
    session: AsyncSession, user: User, row: LifecycleEvent, employee: Employee | None = None
) -> None:
    """Snapshot who approves this event, from the tenant's rules (HR by default),
    and tell the first approver."""
    from app.models.approval_rules import ApprovalProcess
    from app.services.approvals import rules

    subject = employee or await _employee(session, user, row.employee_id)
    row.approval_chain = await rules.attach(
        session,
        user,
        subject,
        rules.Facts(
            process=ApprovalProcess.LIFECYCLE,
            department_id=subject.department_id,
            location_id=subject.location_id,
            category=row.event_type.value,
        ),
    )
    first = rules.chain_first_named(row.approval_chain)
    if first is not None and first != user.id:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_STEP_WAITING,
            recipient_user_ids=[first],
            payload={
                "event": row.event_type.value.replace("_", " "),
                "employee_name": subject.full_name,
                "effective_from": f"{row.effective_from:%d %b %Y}",
            },
            commit=False,
        )


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
    await _check_new_references(session, user, employee.id, new_values)
    if new_values.get("legal_entity_id"):
        await companies.check_no_later_change(session, user, employee.id, effective_from)
    facts = await _facts(
        session,
        user,
        employee,
        event_type=event_type,
        effective_from=effective_from,
        new_values=new_values,
        reason=reason,
        new_ctc_annual=new_ctc_annual,
    )
    described = event_engine.describe(facts, today=today or date.today())
    try:
        event_engine.validate_event(facts, today=today or date.today())
        described["problems"] = []
    except event_engine.LifecycleError as exc:
        described["problems"] = [str(exc)]
    template = (
        await _letter_template_for(session, user, event_type)
        if described["needs_a_letter"]
        else None
    )
    described["letter_template"] = (
        {"id": str(template.id), "code": template.code, "name": template.name} if template else None
    )
    return described


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
    from app.services.approvals import rules

    resolution_day = today or date.today()
    row = await _event(session, user, event_id)
    _refuse_self(user, row.employee_id, doing="decide")

    if row.status is not LifecycleStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This event is {row.status.value}, not awaiting approval.",
        )

    chain = list(row.approval_chain or [])
    if not chain:
        # Raised before approval chains: an admin's decision, as it always was.
        if not permissions.is_admin(user):
            raise permissions.forbidden("Only an admin approves this lifecycle event.")
    else:
        chain, outcome = await rules.decide(
            session,
            user,
            chain,
            approve=approve,
            note=note or None,
            hr_may=permissions.is_admin(user),
        )
        row.approval_chain = chain
        if outcome == "advanced":
            await session.flush()
            await audit.record(
                session,
                tenant_id=user.tenant_id,
                actor=user,
                action=AuditAction.APPROVE,
                entity_type="lifecycle_event",
                entity_id=row.id,
                summary=f"{row.event_type.value}: a step approved, passed to the next approver",
                changes={"note": note},
                request=request,
            )
            following = rules.chain_first_named(chain)
            employee = await _employee(session, user, row.employee_id)
            recipients = (
                [following]
                if following is not None
                else await rules.next_recipients(session, user, chain)
            )
            recipients = [uid for uid in recipients if uid != user.id]
            if recipients:
                await notify(
                    session,
                    tenant_id=user.tenant_id,
                    event_key=EVENT_STEP_WAITING,
                    recipient_user_ids=recipients,
                    payload={
                        "event": row.event_type.value.replace("_", " "),
                        "employee_name": employee.full_name,
                        "effective_from": f"{row.effective_from:%d %b %Y}",
                    },
                    commit=False,
                )
            index = next(
                (i for i, step in enumerate(chain) if step.get("status") == "pending"), None
            )
            return {
                "event_id": str(row.id),
                "status": row.status.value,
                "waiting_on": chain[index].get("approver_name") if index is not None else None,
            }

    if not approve:
        row.status = LifecycleStatus.CANCELLED
        row.cancelled_reason = note or "Not approved."
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.REJECT,
            entity_type="lifecycle_event",
            entity_id=row.id,
            summary=f"{row.event_type.value} not approved",
            changes={"note": row.cancelled_reason},
            request=request,
        )
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
        summary=(f"{row.event_type.value} approved for effect on {row.effective_from}"),
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
    session: AsyncSession,
    user: User,
    *,
    event_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> dict[str, Any]:
    row = await _event(session, user, event_id)
    _refuse_self(user, row.employee_id, doing="cancel")
    if row.status is LifecycleStatus.EFFECTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This event has already been effected. Cancelling it would not undo "
                "the change — raise a reversing event instead."
            ),
        )
    if row.status is LifecycleStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This event is already cancelled."
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A cancellation needs a reason.",
        )
    previous = row.status.value
    row.status = LifecycleStatus.CANCELLED
    row.cancelled_reason = reason
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="lifecycle_event",
        entity_id=row.id,
        summary=f"{row.event_type.value} cancelled (was {previous})",
        changes={"reason": reason},
        request=request,
    )
    return {"event_id": str(row.id), "status": row.status.value}


# ═══════════════════════════════════════════════════════════════════════════
# Effecting — the cascade
# ═══════════════════════════════════════════════════════════════════════════


async def _close_open_probation_review(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> bool:
    """A confirmation that took effect decides the review it answers.

    Otherwise the review stays undecided and the reminder sweep keeps chasing a
    manager about somebody who is already confirmed.
    """
    from app.models.cycle_config import ProbationOutcome, ProbationReview

    review = await session.scalar(
        tenant_scoped(
            select(ProbationReview)
            .where(
                ProbationReview.employee_id == employee_id,
                ProbationReview.outcome.is_(None),
            )
            .order_by(ProbationReview.probation_end_date.desc())
            .limit(1),
            ProbationReview,
            user,
        )
    )
    if review is None:
        return False
    review.outcome = ProbationOutcome.CONFIRM
    review.outcome_recorded_at = datetime.now(UTC)
    review.outcome_recorded_by_user_id = user.id
    return True


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


#: Which letter category an event's letter belongs to, where one is standard.
_LETTER_CATEGORY: dict[LifecycleEventType, LetterCategory] = {
    LifecycleEventType.CONFIRMATION: LetterCategory.CONFIRMATION,
    LifecycleEventType.PROMOTION: LetterCategory.PROMOTION,
    LifecycleEventType.TRANSFER: LetterCategory.TRANSFER,
    LifecycleEventType.DEPUTATION: LetterCategory.TRANSFER,
    LifecycleEventType.LOCATION_CHANGE: LetterCategory.TRANSFER,
    LifecycleEventType.DEPARTMENT_CHANGE: LetterCategory.TRANSFER,
    # Deployed to a client's project: the same letter as a transfer, where there is one.
    LifecycleEventType.PROJECT_CHANGE: LetterCategory.TRANSFER,
}


async def _letter_template_for(
    session: AsyncSession, user: User, event_type: LifecycleEventType
) -> LetterTemplate | None:
    """The template an event's letter is issued from.

    An active template coded `LIFECYCLE_<EVENT_TYPE>` (e.g. `LIFECYCLE_PROBATION_EXTENSION`)
    wins, so a tenant can give any event its own letter. Otherwise the first
    active template, by code, in the event's standard category. None means the
    tenant has no letter for this event.
    """
    specific = await session.scalar(
        tenant_scoped(
            select(LetterTemplate).where(
                LetterTemplate.code == f"LIFECYCLE_{event_type.value.upper()}",
                LetterTemplate.active.is_(True),
            ),
            LetterTemplate,
            user,
        )
    )
    if specific is not None:
        return specific
    category = _LETTER_CATEGORY.get(event_type)
    if category is None:
        return None
    return await session.scalar(
        tenant_scoped(
            select(LetterTemplate)
            .where(LetterTemplate.category == category, LetterTemplate.active.is_(True))
            .order_by(LetterTemplate.code)
            .limit(1),
            LetterTemplate,
            user,
        )
    )


async def _letter_manual_values(
    session: AsyncSession,
    user: User,
    row: LifecycleEvent,
    *,
    previous_ctc: Decimal | None = None,
) -> dict[str, Any]:
    """What an event's letter can say beyond the employee record.

    Offered as `manual.*` tokens: `effective_date`, `reason`, `new_ctc_annual`,
    and for a pay change `previous_ctc` and `increase_percentage` (the standard
    increment letter requires both), every changed field by name, and the
    readable `new_department_name`, `new_location_name` and `new_manager_name`.
    """
    values: dict[str, Any] = {
        key: value for key, value in (row.new_values or {}).items() if value is not None
    }
    values["effective_date"] = row.effective_from
    values["reason"] = row.reason
    if row.new_ctc_annual is not None:
        new_ctc = Decimal(str(row.new_ctc_annual))
        values["new_ctc_annual"] = new_ctc
        if previous_ctc is not None:
            values["previous_ctc"] = (
                f"{previous_ctc.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
            )
            values["increase_percentage"] = str(
                ((new_ctc - previous_ctc) / previous_ctc * Decimal(100)).quantize(Decimal("0.01"))
                if previous_ctc
                else Decimal(0)
            )
    for key, model, label in (
        ("legal_entity_id", LegalEntity, "new_company_name"),
        ("project_id", Project, "new_project_name"),
        ("department_id", Department, "new_department_name"),
        ("location_id", Location, "new_location_name"),
    ):
        if (row.new_values or {}).get(key):
            target = await session.scalar(
                tenant_scoped(
                    select(model).where(model.id == _as_uuid(row.new_values[key])), model, user
                )
            )
            if target is not None:
                values[label] = target.name
    if (row.new_values or {}).get("manager_id"):
        manager = await session.scalar(
            tenant_scoped(
                select(Employee).where(Employee.id == _as_uuid(row.new_values["manager_id"])),
                Employee,
                user,
            )
        )
        if manager is not None:
            values["new_manager_name"] = manager.full_name
    return values


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
    _refuse_self(user, row.employee_id, doing="effect")

    if row.status is not LifecycleStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"This event is {row.status.value}. Only an approved event can be effected."),
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
    letter_result: dict[str, Any] = {"letter_id": None, "reference_number": None, "note": None}

    try:
        async with session.begin_nested():
            employee = await _employee(session, user, row.employee_id)

            # A company change approved for a past date cannot land after a
            # later company change has already taken effect: the history
            # would contradict itself about the months between.
            if (row.new_values or {}).get("legal_entity_id"):
                await companies.check_no_later_change(
                    session, user, row.employee_id, row.effective_from
                )

            # What this replaces is what the record holds now. A change made
            # after the event was raised (another transfer, a move from the
            # directory) may have moved it on from the snapshot taken then,
            # and the history — which company paid which month — reads these.
            row.previous_values = {
                **(row.previous_values or {}),
                **{
                    key: _plain(getattr(employee, key, None))
                    for key in (row.new_values or {})
                    if hasattr(employee, key)
                },
            }

            # ── The employee record ─────────────────────────────────────────
            for key, value in (row.new_values or {}).items():
                setattr(employee, key, _coerce(key, value))
                applied.append(f"employee.{key}")

            if row.event_type is LifecycleEventType.CONFIRMATION:
                employee.status = EmploymentStatus.ACTIVE
                applied.append("employee.status")
                closed = await _close_open_probation_review(session, user, employee.id)
                if closed:
                    applied.append("probation_review.outcome")

            # ── Position assignment (slice 23) ──────────────────────────────
            if "manager_id" in (row.new_values or {}):
                moved = await _reassign_pending_approvals(
                    session,
                    user,
                    employee_id=employee.id,
                    new_manager_id=_as_uuid(row.new_values["manager_id"]),
                )
                if moved:
                    applied.append(f"reassigned {moved} pending approval(s)")

            # ── Salary revision (slice 01, arrears in slice 12) ─────────────
            previous_ctc: Decimal | None = None
            if row.new_ctc_annual is not None:
                salary, previous_ctc = await _revise_salary(
                    session,
                    user,
                    employee=employee,
                    ctc_annual=Decimal(str(row.new_ctc_annual)),
                    effective_from=row.effective_from,
                    reason=f"{row.event_type.value}: {row.reason}",
                    request=request,
                )
                row.salary_revision_id = salary.id
                applied.append("salary revision")

            # ── Letter (slice 03) ───────────────────────────────────────────
            # Inside the savepoint: a promotion whose letter cannot be merged
            # (a required value missing, no salary for a CTC table) is not
            # effected either, and the failure names the letter's problem.
            if event_engine.EventType(row.event_type.value).needs_a_letter:
                template = await _letter_template_for(session, user, row.event_type)
                if template is None:
                    letter_result["note"] = (
                        f"No letter was issued: there is no active "
                        f"{_LETTER_CATEGORY.get(row.event_type, LetterCategory.CUSTOM).value} "
                        f"template, nor one coded LIFECYCLE_{row.event_type.value.upper()}."
                    )
                else:
                    await session.flush()
                    letter = await letter_service.issue_letter(
                        session,
                        user,
                        employee_id=employee.id,
                        template_id=template.id,
                        manual_values=await _letter_manual_values(
                            session, user, row, previous_ctc=previous_ctc
                        ),
                        as_of=row.effective_from,
                        request=request,
                        commit=False,
                    )
                    row.letter_id = letter.id
                    letter_result["letter_id"] = str(letter.id)
                    letter_result["reference_number"] = letter.reference_number
                    applied.append(f"letter {letter.reference_number}")

            row.status = LifecycleStatus.EFFECTED
            row.effected_at = datetime.now(UTC)
            row.failure_reason = None
            await session.flush()

    except Exception as exc:
        # The savepoint is rolled back, so nothing above landed. The event is
        # marked outside it so the failure itself survives.
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        row.status = LifecycleStatus.FAILED
        row.failure_reason = f"{type(exc).__name__}: {detail}"[:2000]
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="lifecycle_event",
            entity_id=row.id,
            summary=(
                f"Effecting {row.event_type.value} failed and was rolled back entirely: {detail}"
            ),
            request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This event could not be effected and nothing was applied: {detail}. "
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
            f"{', '.join(applied) or 'no field changes'}"
            + (f". {letter_result['note']}" if letter_result["note"] else "")
        ),
        changes={"applied": applied, "previous_values": row.previous_values},
        request=request,
    )
    return {
        "event_id": str(row.id),
        "status": row.status.value,
        "effected_at": row.effected_at.isoformat(),
        "applied": applied,
        "letter": letter_result,
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

    Returns the new salary and the annual CTC it replaces, which the letter quotes.
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

    previous_ctc = Decimal(str(current.ctc_annual))
    salary = await payroll_service.assign_salary(
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
    return salary, previous_ctc


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
                    "approver_employee_id": (str(new_manager_id) if new_manager_id else None),
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
    request: Request | None = None,
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
                    request=request,
                )
            raised.append({"employee_id": str(employee_id), "event_id": str(event.id)})
        except HTTPException as exc:
            rejected.append({"employee_id": str(employee_id), "reason": str(exc.detail)})

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="lifecycle_batch",
        entity_id=batch.id,
        changes={"raised": len(raised), "rejected": len(rejected)},
        summary=f"Bulk {event_type.value} '{name}': {len(raised)} raised, {len(rejected)} rejected",
        request=request,
    )
    return {
        "batch_id": str(batch.id),
        "name": batch.name,
        "raised": raised,
        "rejected": rejected,
        "raised_count": len(raised),
        "rejected_count": len(rejected),
    }


async def list_batches(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = await session.scalars(
        tenant_scoped(
            select(LifecycleBatch).order_by(LifecycleBatch.created_at.desc()),
            LifecycleBatch,
            user,
        )
    )
    return [
        {
            "batch_id": str(b.id),
            "name": b.name,
            "event_type": b.event_type.value,
            "effective_from": b.effective_from.isoformat(),
            "reason": b.reason,
            "events": len(b.events),
            "approve_individually": b.approve_individually,
            "created_at": b.created_at.isoformat(),
        }
        for b in rows.all()
    ]


# ═══════════════════════════════════════════════════════════════════════════
# History
# ═══════════════════════════════════════════════════════════════════════════


async def _require_visible(session: AsyncSession, user: User, employee_id: uuid.UUID) -> None:
    """HR sees anybody's history; a manager only their own reporting line."""
    await permissions.require_manager_or_hr(
        session,
        user,
        employee_id,
        detail="You can only see lifecycle history for your own reporting line.",
        allow_self=True,
    )


async def list_events(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None = None,
    event_status: LifecycleStatus | None = None,
    event_type: LifecycleEventType | None = None,
    limit: int = 500,
) -> list[EventRead]:
    stmt = select(LifecycleEvent).order_by(
        LifecycleEvent.effective_from.desc(), LifecycleEvent.created_at.desc()
    )
    visible = await permissions.visible_employee_ids(session, user)
    if visible is not None:
        stmt = stmt.where(LifecycleEvent.employee_id.in_(visible))
    if employee_id is not None:
        stmt = stmt.where(LifecycleEvent.employee_id == employee_id)
    if event_status is not None:
        stmt = stmt.where(LifecycleEvent.status == event_status)
    if event_type is not None:
        stmt = stmt.where(LifecycleEvent.event_type == event_type)
    rows = list(
        (await session.scalars(tenant_scoped(stmt.limit(limit), LifecycleEvent, user))).all()
    )

    people: dict[uuid.UUID, Employee] = {}
    ids = {row.employee_id for row in rows}
    if ids:
        people = {
            e.id: e
            for e in (
                await session.scalars(
                    tenant_scoped(select(Employee).where(Employee.id.in_(ids)), Employee, user)
                )
            ).all()
        }
    return [
        EventRead(
            id=row.id,
            employee_id=row.employee_id,
            employee_name=people[row.employee_id].full_name if row.employee_id in people else "",
            employee_code=(
                people[row.employee_id].employee_code if row.employee_id in people else ""
            ),
            event_type=row.event_type,
            effective_from=row.effective_from,
            status=row.status,
            previous_values=row.previous_values or {},
            new_values=row.new_values or {},
            reason=row.reason,
            new_ctc_annual=_dec(row.new_ctc_annual),
            consequences_shown=row.consequences_shown or [],
            approved_at=row.approved_at,
            effected_at=row.effected_at,
            cancelled_reason=row.cancelled_reason,
            failure_reason=row.failure_reason,
            letter_id=row.letter_id,
            batch_id=row.batch_id,
            created_at=row.created_at,
            approval_chain=list(row.approval_chain or []),
        )
        for row in rows
    ]


async def event_for_decision(session: AsyncSession, user: User, event_id: uuid.UUID) -> EventRead:
    """One event, for an admin or for whoever its approval chain is waiting on.

    A rule may name somebody outside HR — a regional head, a finance controller —
    who has no lifecycle screen of their own; this is what they decide from.
    """
    from app.services.approvals import rules

    row = await _event(session, user, event_id)
    chain = list(row.approval_chain or [])
    may = bool(chain) and await rules.may_decide(
        session, user, chain, hr_may=permissions.is_admin(user)
    )
    if user.employee_id is not None and user.employee_id == row.employee_id:
        raise permissions.forbidden("This event is about you; somebody else decides it.")
    if not (permissions.is_admin(user) or may):
        raise permissions.forbidden("This event is not waiting on you.")
    employee = await _employee(session, user, row.employee_id)
    return EventRead(
        id=row.id,
        employee_id=row.employee_id,
        employee_name=employee.full_name,
        employee_code=employee.employee_code,
        event_type=row.event_type,
        effective_from=row.effective_from,
        status=row.status,
        previous_values=row.previous_values or {},
        new_values=row.new_values or {},
        reason=row.reason,
        new_ctc_annual=_dec(row.new_ctc_annual),
        consequences_shown=row.consequences_shown or [],
        approved_at=row.approved_at,
        effected_at=row.effected_at,
        cancelled_reason=row.cancelled_reason,
        failure_reason=row.failure_reason,
        letter_id=row.letter_id,
        batch_id=row.batch_id,
        created_at=row.created_at,
        approval_chain=chain,
        caller_may_decide=(
            row.status is LifecycleStatus.PENDING_APPROVAL
            and (may or (not chain and permissions.is_admin(user)))
        ),
    )


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
    await _require_visible(session, user, employee.id)
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

    approver_ids = {e.approved_by_user_id for e in rows if e.approved_by_user_id}
    approvers: dict[uuid.UUID, str] = {}
    if approver_ids:
        approvers = {
            u.id: u.full_name
            for u in (
                await session.scalars(
                    tenant_scoped(select(User).where(User.id.in_(approver_ids)), User, user)
                )
            ).all()
        }

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
                "approved_by_name": approvers.get(e.approved_by_user_id),
                "approved_at": e.approved_at.isoformat() if e.approved_at else None,
                "effected_at": e.effected_at.isoformat() if e.effected_at else None,
                "letter_id": str(e.letter_id) if e.letter_id else None,
                "failure_reason": e.failure_reason,
                "cancelled_reason": e.cancelled_reason,
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
    await _require_visible(session, user, employee.id)
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

    tracked = {
        "grade",
        "designation",
        "legal_entity_id",
        "project_id",
        "department_id",
        "location_id",
        "manager_id",
        "employment_type",
        "status",
        "notice_period_days",
    }
    state = {key: _plain(getattr(employee, key, None)) for key in tracked}

    # Newest first: each event undone puts the record back one step.
    for event in rows:
        for key, value in (event.previous_values or {}).items():
            if key in tracked and key in (event.new_values or {}):
                state[key] = value

    manager_name = None
    if state.get("manager_id"):
        manager = await session.scalar(
            tenant_scoped(
                select(Employee).where(Employee.id == _as_uuid(state["manager_id"])),
                Employee,
                user,
            )
        )
        manager_name = manager.full_name if manager else None

    return {
        "employee_id": str(employee.id),
        "as_of": on.isoformat(),
        "state": state,
        "manager_name": manager_name,
        "events_undone": len(rows),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Probation
# ═══════════════════════════════════════════════════════════════════════════


async def _policy(session: AsyncSession, user: User) -> ProbationPolicy:
    policy = await session.scalar(tenant_scoped(select(ProbationPolicy), ProbationPolicy, user))
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


_SILENCE_NOTE = {
    SilencePolicy.AUTO_CONFIRM: "the employee is confirmed.",
    SilencePolicy.REQUIRE_DECISION: "nothing happens until somebody decides.",
    SilencePolicy.AUTO_EXTEND_ONCE: "probation extends once, then escalates.",
}


async def get_probation_policy(session: AsyncSession, user: User) -> ProbationPolicyRead:
    """The configured policy, or a plain statement that there is none — never a default."""
    policy = await session.scalar(tenant_scoped(select(ProbationPolicy), ProbationPolicy, user))
    if policy is None:
        return ProbationPolicyRead(
            configured=False,
            silence_policy=None,
            grace_days=None,
            confirmation_raises_salary=None,
            hr_escalation_user_id=None,
            note=(
                "No probation policy is configured. Reminders and drift resolution are "
                "refused until somebody chooses what silence means."
            ),
        )
    return ProbationPolicyRead(
        configured=True,
        silence_policy=policy.silence_policy,
        grace_days=policy.grace_days,
        confirmation_raises_salary=policy.confirmation_raises_salary,
        hr_escalation_user_id=policy.hr_escalation_user_id,
        note="Silence now means " + _SILENCE_NOTE[policy.silence_policy],
    )


async def set_probation_policy(
    session: AsyncSession,
    user: User,
    payload: ProbationPolicyIn,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Choose what silence means. There is no default and there will not be one."""
    if payload.hr_escalation_user_id is not None:
        escalation = await session.scalar(
            tenant_scoped(
                select(User).where(
                    User.id == payload.hr_escalation_user_id, User.is_active.is_(True)
                ),
                User,
                user,
            )
        )
        if escalation is None or escalation.role not in permissions.HR_ROLES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="The escalation contact must be an active HR user in this workspace.",
            )

    existing = await session.scalar(tenant_scoped(select(ProbationPolicy), ProbationPolicy, user))
    before = (
        {"silence_policy": existing.silence_policy.value, "grace_days": existing.grace_days}
        if existing
        else None
    )
    policy = existing or ProbationPolicy(
        tenant_id=user.tenant_id, silence_policy=payload.silence_policy
    )
    policy.silence_policy = payload.silence_policy
    policy.grace_days = payload.grace_days
    policy.confirmation_raises_salary = payload.confirmation_raises_salary
    policy.hr_escalation_user_id = payload.hr_escalation_user_id
    policy.configured_by_user_id = user.id
    session.add(policy)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE if existing else AuditAction.CREATE,
        entity_type="probation_policy",
        entity_id=policy.id,
        changes={"before": before, "after": payload.model_dump(mode="json")},
        summary=f"Probation silence policy set to {payload.silence_policy.value}",
        request=request,
    )
    return {
        "silence_policy": policy.silence_policy.value,
        "grace_days": policy.grace_days,
        "note": "Silence now means " + _SILENCE_NOTE[policy.silence_policy],
    }


async def _review_facts(session: AsyncSession, user: User) -> list[probation_engine.ReviewFacts]:
    from app.models.cycle_config import ProbationReview

    reviews = (
        await session.scalars(tenant_scoped(select(ProbationReview), ProbationReview, user))
    ).all()

    sent = (
        await session.scalars(tenant_scoped(select(ProbationReminder), ProbationReminder, user))
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
                tenant_scoped(
                    select(User).where(User.employee_id == employee.manager_id), User, user
                )
            )
            manager_user_id = str(manager_user.id) if manager_user else None
            if manager is not None and manager.manager_id:
                skip_user = await session.scalar(
                    tenant_scoped(
                        select(User).where(User.employee_id == manager.manager_id), User, user
                    )
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
    reminders = probation_engine.due_reminders(reviews, today=today)

    from app.services.notifications import dispatch

    sent: list[dict[str, Any]] = []
    for reminder in reminders:
        recipient = reminder.recipient_user_id
        if reminder.step is probation_engine.ReminderStep.HR:
            recipient = str(policy.hr_escalation_user_id) if policy.hr_escalation_user_id else None

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
# Direct edits
# ═══════════════════════════════════════════════════════════════════════════

#: How a direct edit of the record is filed as events: the field that decides
#: the type, and what else that type may carry. Read in order; a field already
#: filed is not filed again, so a department and manager change together is
#: one department change, as it would be if raised as an event.
_DIRECT_EDIT_EVENTS: tuple[tuple[LifecycleEventType, frozenset[str]], ...] = (
    (LifecycleEventType.TRANSFER, frozenset({"legal_entity_id"})),
    (LifecycleEventType.PROJECT_CHANGE, frozenset({"project_id"})),
    (LifecycleEventType.DEPARTMENT_CHANGE, frozenset({"department_id"})),
    (LifecycleEventType.LOCATION_CHANGE, frozenset({"location_id"})),
    (LifecycleEventType.MANAGER_CHANGE, frozenset({"manager_id"})),
    (LifecycleEventType.REDESIGNATION, frozenset({"designation"})),
    (LifecycleEventType.GRADE_CHANGE, frozenset({"grade"})),
    (
        LifecycleEventType.EMPLOYMENT_TYPE_CHANGE,
        frozenset({"employment_type", "notice_period_days"}),
    ),
    (LifecycleEventType.PROBATION_EXTENSION, frozenset({"probation_end_date"})),
)

#: Every field a direct edit files as an event.
DIRECT_EDIT_FIELDS = frozenset().union(
    *(
        event_engine.CHANGED_FIELDS[event_engine.EventType(kind.value)]
        for kind, _ in _DIRECT_EDIT_EVENTS
    )
)


def record_direct_edit(
    session: AsyncSession,
    user: User,
    employee: Employee,
    changes: dict[str, Any],
    *,
    on: date | None = None,
) -> list[LifecycleEvent]:
    """File HR's direct edit of job fields as effected lifecycle events.

    The edit has already been decided by somebody allowed to make it, so the
    events are born effected, today, with that person as approver. What this
    keeps is the history: without it a department changed on the record exists
    only in the audit log, and the as-of view (`state_on`) would report the new
    department for dates before the change. Call it before the new values are
    written to the record; the caller commits, with its audit row.
    """
    moved = {
        key: value
        for key, value in changes.items()
        if key in DIRECT_EDIT_FIELDS and _plain(getattr(employee, key)) != _plain(value)
    }
    if not moved:
        return []
    now = datetime.now(UTC)
    effective = on or date.today()
    created: list[LifecycleEvent] = []
    for kind, triggers in _DIRECT_EDIT_EVENTS:
        if not triggers & moved.keys():
            continue
        allowed = event_engine.CHANGED_FIELDS[event_engine.EventType(kind.value)]
        fields = allowed & moved.keys()
        event = LifecycleEvent(
            tenant_id=user.tenant_id,
            employee_id=employee.id,
            event_type=kind,
            effective_from=effective,
            previous_values={key: _plain(getattr(employee, key)) for key in sorted(fields)},
            new_values={key: _plain(moved[key]) for key in sorted(fields)},
            reason="Changed directly on the employee record.",
            status=LifecycleStatus.EFFECTED,
            raised_by_user_id=user.id,
            approved_by_user_id=user.id,
            approved_at=now,
            effected_at=now,
        )
        session.add(event)
        created.append(event)
        for key in fields:
            moved.pop(key)
    return created


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


def _dec(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


#: Fields that are stored as ids or enums and arrive as strings.
_UUID_FIELDS = frozenset(
    {"legal_entity_id", "project_id", "location_id", "department_id", "manager_id"}
)
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
