"""Running a cycle: reminders, overdue escalation, and the scheduled sweep.

There is no scheduler in the product yet (see `docs/PROMPT_STATUS.md`,
cross-cutting gap 4). So every job here is an ordinary service function that
takes `(session, user)`: HR runs it from the cycle screen today, and a scheduler
calls `run_performance_sweep` with a service account tomorrow. Nothing about
the job changes when that happens.

Recipients are resolved from the org chart at the moment the job runs, not
stored at launch: a manager who changed in October gets November's reminder.
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
from app.models.appraisal import (
    Appraisal,
    AppraisalCycle,
    AppraisalStage,
    AppraisalStatus,
    CycleStatus,
    StageDelegation,
    StageRole,
)
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.user import User
from app.services import audit
from app.services.performance import notices

__all__ = [
    "REMINDER_DAYS_BEFORE_DUE",
    "escalate_appraisal",
    "escalate_overdue",
    "run_performance_sweep",
    "send_stage_reminders",
    "stage_owner_user_ids",
]

#: The sweep reminds owners this many days before a step is due, and on the day.
REMINDER_DAYS_BEFORE_DUE = (3, 1, 0)
DONE_STATUSES = (AppraisalStatus.COMPLETED, AppraisalStatus.RELEASED, AppraisalStatus.ACKNOWLEDGED)


async def _manager_of(
    session: AsyncSession, user: User, employee_id: uuid.UUID | None
) -> Employee | None:
    if employee_id is None:
        return None
    manager_id = await session.scalar(
        tenant_scoped(select(Employee.manager_id).where(Employee.id == employee_id), Employee, user)
    )
    if manager_id is None or manager_id == employee_id:
        return None
    return await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == manager_id), Employee, user)
    )


async def _delegates_of(
    session: AsyncSession, user: User, cycle_id: uuid.UUID, from_user_id: uuid.UUID
) -> list[uuid.UUID]:
    today = date.today()
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(StageDelegation.to_user_id).where(
                        StageDelegation.cycle_id == cycle_id,
                        StageDelegation.from_user_id == from_user_id,
                        StageDelegation.active.is_(True),
                        StageDelegation.effective_from <= today,
                        StageDelegation.effective_to >= today,
                    ),
                    StageDelegation,
                    user,
                )
            )
        ).all()
    )


async def stage_owner_user_ids(
    session: AsyncSession,
    user: User,
    *,
    appraisal: Appraisal,
    stage: AppraisalStage,
    employee: Employee,
) -> list[uuid.UUID]:
    """Who should hear that this step is waiting."""
    recipients: list[uuid.UUID] = []
    if stage.role is StageRole.SELF:
        found = await notices.user_ids_for_employees(session, user, [employee.id])
        recipients.extend(found.values())
    elif stage.role in (StageRole.MANAGER, StageRole.REVIEWER):
        owner = await _manager_of(session, user, employee.id)
        if stage.role is StageRole.REVIEWER and owner is not None:
            owner = await _manager_of(session, user, owner.id)
        owner_user = None
        if owner is not None:
            owner_user = (await notices.user_ids_for_employees(session, user, [owner.id])).get(
                owner.id
            )
        if owner_user is not None:
            recipients.append(owner_user)
            recipients.extend(await _delegates_of(session, user, appraisal.cycle_id, owner_user))
        else:
            recipients.extend(await notices.hr_user_ids(session, user))
    else:
        recipients.extend(await notices.hr_user_ids(session, user))
    if appraisal.escalated_to_user_id is not None:
        recipients.append(appraisal.escalated_to_user_id)
    return list(dict.fromkeys(recipients))


async def _cycle(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> AppraisalCycle:
    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == cycle_id), AppraisalCycle, user
        )
    )
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Appraisal cycle not found"
        )
    return cycle


async def _pending(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> tuple[list[AppraisalStage], list[tuple[Appraisal, Employee]]]:
    stages = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(AppraisalStage)
                    .where(AppraisalStage.cycle_id == cycle_id)
                    .order_by(AppraisalStage.sequence),
                    AppraisalStage,
                    user,
                )
            )
        ).all()
    )
    rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(Appraisal, Employee)
                    .join(Employee, Employee.id == Appraisal.employee_id)
                    .where(
                        Appraisal.cycle_id == cycle_id,
                        Appraisal.current_stage_id.isnot(None),
                        Appraisal.status.notin_(DONE_STATUSES),
                    ),
                    Appraisal,
                    user,
                )
            )
        ).all()
    )
    return stages, [(a, e) for a, e in rows]


async def send_stage_reminders(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    stage_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Remind everybody holding a pending step — one message per person per step."""
    permissions.require_hr(user, detail="Reminders are sent by HR.")
    cycle = await _cycle(session, user, cycle_id)
    if cycle.status not in (CycleStatus.ACTIVE, CycleStatus.IN_CALIBRATION):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This cycle is '{cycle.status.value}'; there is nothing to remind about.",
        )
    stages, rows = await _pending(session, user, cycle_id)
    stage_by_id = {s.id: s for s in stages}

    counts: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    appraisals = 0
    for appraisal, employee in rows:
        stage = stage_by_id.get(appraisal.current_stage_id)
        if stage is None or (stage_id is not None and stage.id != stage_id):
            continue
        appraisals += 1
        for recipient in await stage_owner_user_ids(
            session, user, appraisal=appraisal, stage=stage, employee=employee
        ):
            counts[(recipient, stage.id)] = counts.get((recipient, stage.id), 0) + 1

    for (recipient, sid), count in counts.items():
        stage = stage_by_id[sid]
        await notices.send(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.review.reminder",
            recipient_user_ids=[recipient],
            payload={
                "count": count,
                "stage_label": stage.label,
                "cycle_name": cycle.name,
                "due_date": stage.due_date.isoformat() if stage.due_date else "not set",
            },
        )

    by_stage: dict[str, int] = {}
    for (_, sid), count in counts.items():
        by_stage[stage_by_id[sid].label] = by_stage.get(stage_by_id[sid].label, 0) + count
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="appraisal_cycle",
        entity_id=cycle.id,
        summary=(
            f"Sent reminders on '{cycle.name}' to {len({r for r, _ in counts})} person(s) "
            f"covering {appraisals} pending review(s)"
        ),
        changes={"stage_id": str(stage_id) if stage_id else None, "by_stage": by_stage},
        request=request,
    )
    return {
        "cycle_id": str(cycle.id),
        "recipients": len({r for r, _ in counts}),
        "appraisals": appraisals,
        "by_stage": by_stage,
    }


async def escalate_appraisal(
    session: AsyncSession,
    user: User,
    *,
    cycle: AppraisalCycle,
    appraisal: Appraisal,
    stage: AppraisalStage,
    employee: Employee,
    days_overdue: int,
) -> dict[str, Any]:
    """Escalate one appraisal's current step one level above its owner.

    Self review: the employee's manager (to chase — they cannot write it).
    Manager step: the manager's manager. Reviewer step: the level above the
    reviewer. HR steps, or nobody above: HR.
    """
    levels = {StageRole.SELF: 1, StageRole.MANAGER: 2, StageRole.REVIEWER: 3}.get(stage.role, 0)
    level: Employee | None = None
    current_id: uuid.UUID | None = employee.id
    for _ in range(levels):
        level = await _manager_of(session, user, current_id)
        if level is None:
            break
        current_id = level.id
    target_user: uuid.UUID | None = None
    if level is not None:
        target_user = (await notices.user_ids_for_employees(session, user, [level.id])).get(
            level.id
        )
    if target_user is not None and target_user == (
        await notices.user_ids_for_employees(session, user, [employee.id])
    ).get(employee.id):
        target_user = None
    recipients = [target_user] if target_user else await notices.hr_user_ids(session, user)

    appraisal.escalated_at = datetime.now(UTC)
    appraisal.escalated_to_user_id = target_user
    await notices.send(
        session,
        tenant_id=user.tenant_id,
        event_key="performance.review.escalated",
        recipient_user_ids=recipients,
        payload={
            "employee_name": employee.full_name,
            "stage_label": stage.label,
            "cycle_name": cycle.name,
            "days": days_overdue,
        },
    )
    return {
        "appraisal_id": str(appraisal.id),
        "employee_code": employee.employee_code,
        "stage": stage.label,
        "days_overdue": days_overdue,
        "escalated_to_user_id": str(target_user) if target_user else None,
        "routed_to": "next level up" if target_user else "HR",
    }


async def escalate_overdue(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    as_of: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Raise every step overdue past the cycle's window to the next level up.

    The appraisal records who it went to, and that person may then act on a
    manager or reviewer step (their entry is marked escalated). A self-review
    cannot be written by anybody else, so an overdue one is escalated to the
    line manager to chase, not to complete. Each appraisal escalates once.
    """
    permissions.require_hr(user, detail="Escalations are run by HR.")
    cycle = await _cycle(session, user, cycle_id)
    today = as_of or date.today()
    stages, rows = await _pending(session, user, cycle_id)
    stage_by_id = {s.id: s for s in stages}

    items: list[dict[str, Any]] = []
    for appraisal, employee in rows:
        stage = stage_by_id.get(appraisal.current_stage_id)
        if stage is None or stage.due_date is None or appraisal.escalated_at is not None:
            continue
        days = (today - stage.due_date).days
        if days <= cycle.escalation_after_days:
            continue
        items.append(
            await escalate_appraisal(
                session,
                user,
                cycle=cycle,
                appraisal=appraisal,
                stage=stage,
                employee=employee,
                days_overdue=days,
            )
        )

    await session.flush()
    if items:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="appraisal_cycle",
            entity_id=cycle.id,
            summary=(
                f"Escalated {len(items)} overdue review step(s) on '{cycle.name}' past the "
                f"{cycle.escalation_after_days}-day window"
            ),
            changes={"escalated": items},
            request=request,
        )
    return {"cycle_id": str(cycle.id), "escalated": len(items), "items": items}


async def run_performance_sweep(
    session: AsyncSession, user: User, *, as_of: date | None = None
) -> dict[str, Any]:
    """The scheduled job, callable today by HR and tomorrow by a scheduler.

    For every running cycle: apply each closed stage window's configured
    auto-advance, escalate overdue steps, and remind owners of steps due in
    three days, one day, or today. Then schedule probation reviews for
    probations ending in the next 30 days. Commits once, at the end.
    """
    from app.services.appraisal import (
        cycle_config,  # local: cycle_config imports this module's users
    )

    permissions.require_hr(user, detail="The performance sweep runs as HR.")
    today = as_of or date.today()
    cycles = (
        await session.scalars(
            tenant_scoped(
                select(AppraisalCycle).where(
                    AppraisalCycle.status.in_([CycleStatus.ACTIVE, CycleStatus.IN_CALIBRATION])
                ),
                AppraisalCycle,
                user,
            )
        )
    ).all()

    report: list[dict[str, Any]] = []
    for cycle in cycles:
        advance = await cycle_config.run_auto_advance(session, user, cycle_id=cycle.id, as_of=today)
        escalation = await escalate_overdue(session, user, cycle_id=cycle.id, as_of=today)
        stages = (
            await session.scalars(
                tenant_scoped(
                    select(AppraisalStage).where(AppraisalStage.cycle_id == cycle.id),
                    AppraisalStage,
                    user,
                )
            )
        ).all()
        reminded = 0
        for stage in stages:
            if stage.due_date is None:
                continue
            if any(stage.due_date == today + timedelta(days=d) for d in REMINDER_DAYS_BEFORE_DUE):
                result = await send_stage_reminders(
                    session, user, cycle_id=cycle.id, stage_id=stage.id
                )
                reminded += result["recipients"]
        report.append(
            {
                "cycle_id": str(cycle.id),
                "cycle": cycle.name,
                "advanced": advance.get("advanced", 0),
                "auto_escalated": advance.get("escalated", 0),
                "held": advance.get("held", 0),
                "escalated": escalation["escalated"],
                "reminded": reminded,
            }
        )

    probation = await cycle_config.schedule_due_probation_reviews(
        session, user, as_of=today, horizon_days=30, lead_days=7
    )
    await session.commit()
    return {"as_of": today.isoformat(), "cycles": report, "probation": probation}
