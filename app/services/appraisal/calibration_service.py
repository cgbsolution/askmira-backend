"""Calibration session persistence: the workspace, the lock, and the reopen.

The analysis lives in `calibration.py` and is exercised there. This module is
the database side, plus the three things that only make sense against real
records:

- **A change without a justification is refused**, at the service layer, so the
  API cannot bypass what the screen enforces.
- **Locking writes the agreed ratings back** to the appraisals, in one
  transaction with the change records.
- **Reopening a calibrated appraisal needs an HR override** and is audited,
  because the rating that is being undone was agreed by a group of people who
  are not in the room when it is undone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.appraisal import Appraisal, AppraisalCycle
from app.models.audit import AuditAction
from app.models.calibration import (
    CalibrationChange,
    CalibrationMode,
    CalibrationPolicy,
    CalibrationScope,
    CalibrationSession,
    DistributionTarget,
    SessionStatus,
)
from app.models.employee import Employee
from app.models.user import User
from app.services import audit
from app.services.appraisal import calibration

__all__ = [
    "apply_change",
    "cycle_distribution",
    "lock_session",
    "reopen_calibrated",
    "session_workspace",
]


async def _policy(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> CalibrationPolicy | None:
    return await session.scalar(
        tenant_scoped(
            select(CalibrationPolicy).where(CalibrationPolicy.cycle_id == cycle_id),
            CalibrationPolicy,
            user,
        )
    )


async def _targets(
    session: AsyncSession, user: User, policy_id: uuid.UUID
) -> list[calibration.Target]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(DistributionTarget).where(
                    DistributionTarget.policy_id == policy_id
                ),
                DistributionTarget,
                user,
            )
        )
    ).all()
    return [
        calibration.Target(
            rating=row.rating_value,
            target_percent=Decimal(str(row.target_percent)),
            tolerance_percent=Decimal(str(row.tolerance_percent)),
        )
        for row in rows
    ]


async def _calibration_session(
    session: AsyncSession, user: User, session_id: uuid.UUID
) -> CalibrationSession:
    row = await session.scalar(
        tenant_scoped(
            select(CalibrationSession).where(CalibrationSession.id == session_id),
            CalibrationSession,
            user,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Calibration session not found"
        )
    return row


def _scope_key(scope: CalibrationScope, employee: Employee) -> str:
    if scope is CalibrationScope.DEPARTMENT:
        return str(employee.department_id or "unassigned")
    if scope is CalibrationScope.MANAGER:
        return str(employee.manager_id or "unassigned")
    if scope is CalibrationScope.GRADE:
        return employee.grade or "ungraded"
    return "company"


# ═══════════════════════════════════════════════════════════════════════════
# Distribution across a cycle
# ═══════════════════════════════════════════════════════════════════════════


async def cycle_distribution(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> dict[str, Any]:
    """Actual against target for every scope in the cycle.

    Returns the blockers too, so a caller asking "can this cycle close" and a
    caller asking "what does the distribution look like" get the same answer
    from one query rather than two that can disagree.
    """
    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == cycle_id),
            AppraisalCycle,
            user,
        )
    )
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found")

    policy = await _policy(session, user, cycle_id)
    if policy is None or policy.mode is CalibrationMode.NONE:
        return {
            "mode": policy.mode.value if policy else "none",
            "distributions": [],
            "blockers": [],
            "note": (
                "No calibration policy applies to this cycle, so no distribution is "
                "computed."
            ),
        }

    targets = await _targets(session, user, policy.id)
    rows = (
        await session.execute(
            tenant_scoped(
                select(Appraisal, Employee)
                .join(Employee, Appraisal.employee_id == Employee.id)
                .where(
                    Appraisal.cycle_id == cycle_id,
                    Appraisal.final_rating.isnot(None),
                ),
                Appraisal,
                user,
            )
        )
    ).all()

    grouped: dict[str, list[str]] = {}
    for appraisal, employee in rows:
        key = _scope_key(policy.scope, employee)
        grouped.setdefault(key, []).append(appraisal.final_rating or "")

    distributions = [
        calibration.analyse_distribution(
            scope=policy.scope.value,
            scope_value=key,
            ratings=values,
            targets=targets,
            min_group_size=policy.min_group_size,
        )
        for key, values in sorted(grouped.items())
    ]
    blockers = calibration.enforcement_blockers(
        mode=policy.mode.value, distributions=distributions
    )

    return {
        "mode": policy.mode.value,
        "scope": policy.scope.value,
        "min_group_size": policy.min_group_size,
        "distributions": [d.as_dict() for d in distributions],
        "blockers": blockers,
        "can_close": not blockers,
        "excluded_scopes": [
            {"scope_value": d.scope_value, "reason": d.exclusion_reason}
            for d in distributions
            if d.is_excluded_as_small
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
# The session workspace
# ═══════════════════════════════════════════════════════════════════════════


async def session_workspace(
    session: AsyncSession, user: User, session_id: uuid.UUID
) -> dict[str, Any]:
    """Everything the facilitator's screen needs, in one call.

    The grid, the live distribution, and each employee's peers at the same
    grade. Assembled server-side because a screen that fetches these
    separately shows a distribution that disagrees with the rows above it for
    a few hundred milliseconds after every change.
    """
    calib = await _calibration_session(session, user, session_id)
    policy = await _policy(session, user, calib.cycle_id)
    targets = await _targets(session, user, policy.id) if policy else []

    rows = (
        await session.execute(
            tenant_scoped(
                select(Appraisal, Employee)
                .join(Employee, Appraisal.employee_id == Employee.id)
                .where(Appraisal.cycle_id == calib.cycle_id),
                Appraisal,
                user,
            )
        )
    ).all()

    in_scope = [
        (appraisal, employee)
        for appraisal, employee in rows
        if policy is None
        or _scope_key(policy.scope, employee) == calib.scope_value
    ]

    grid = [
        {
            "appraisal_id": str(appraisal.id),
            "employee_id": str(employee.id),
            "employee_code": employee.employee_code,
            "name": employee.full_name,
            "grade": employee.grade,
            "manager_id": str(employee.manager_id) if employee.manager_id else None,
            "date_of_joining": (
                employee.date_of_joining.isoformat() if employee.date_of_joining else None
            ),
            "current_rating": appraisal.final_rating,
            "current_score": (
                str(appraisal.final_score) if appraisal.final_score is not None else None
            ),
            "status": appraisal.status.value,
        }
        for appraisal, employee in in_scope
    ]

    rated = [r["current_rating"] for r in grid if r["current_rating"]]
    distribution = calibration.analyse_distribution(
        scope=calib.scope.value,
        scope_value=calib.scope_value,
        ratings=rated,
        targets=targets,
        min_group_size=policy.min_group_size if policy else 1,
    )

    # Peers at the same grade, so "is this rating right" has something to be
    # answered against.
    by_grade: dict[str, list[str]] = {}
    for row in grid:
        if row["current_score"] is not None:
            by_grade.setdefault(row["grade"] or "ungraded", []).append(row["current_score"])

    return {
        "session": {
            "id": str(calib.id),
            "name": calib.name,
            "scope": calib.scope.value,
            "scope_value": calib.scope_value,
            "status": calib.status.value,
            "locked_at": calib.locked_at.isoformat() if calib.locked_at else None,
        },
        "grid": grid,
        "distribution": distribution.as_dict(),
        "peer_scores_by_grade": by_grade,
        "mode": policy.mode.value if policy else "none",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Changing a rating
# ═══════════════════════════════════════════════════════════════════════════


async def apply_change(
    session: AsyncSession,
    user: User,
    *,
    session_id: uuid.UUID,
    appraisal_id: uuid.UUID,
    new_rating: str,
    new_score: Decimal | None = None,
    justification: str,
    request: Request | None = None,
) -> CalibrationChange:
    """Change one rating inside a session.

    The justification is mandatory here, in the service, not only in the
    screen. A rating changed in a room the employee was not in, by people who
    do not manage them, with no recorded reason, is indefensible the moment it
    is questioned.
    """
    if not justification.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A calibration change needs a justification. This rating was agreed in a "
                "room the employee was not in — if it is questioned later, the reason is "
                "what the organisation has to answer with."
            ),
        )

    calib = await _calibration_session(session, user, session_id)
    if calib.locked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This session is locked and its ratings have been written back. Reopen "
                "the appraisal with an HR override to change it."
            ),
        )
    if calib.status is SessionStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This session was cancelled.",
        )

    appraisal = await session.scalar(
        tenant_scoped(
            select(Appraisal).where(Appraisal.id == appraisal_id), Appraisal, user
        )
    )
    if appraisal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appraisal not found")
    if appraisal.cycle_id != calib.cycle_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That appraisal belongs to a different cycle from this session.",
        )

    change = CalibrationChange(
        tenant_id=user.tenant_id,
        session_id=calib.id,
        appraisal_id=appraisal.id,
        employee_id=appraisal.employee_id,
        previous_rating=appraisal.final_rating,
        new_rating=new_rating,
        previous_score=appraisal.final_score,
        new_score=new_score,
        changed_by_user_id=user.id,
        changed_at=datetime.now(UTC),
        justification=justification.strip(),
    )
    session.add(change)

    if calib.status is SessionStatus.SCHEDULED:
        calib.status = SessionStatus.IN_PROGRESS
        calib.started_at = datetime.now(UTC)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="calibration_change",
        entity_id=change.id,
        summary=(
            f"Calibrated a rating from '{change.previous_rating}' to '{new_rating}': "
            f"{change.justification}"
        ),
        changes={
            "appraisal_id": str(appraisal.id),
            "previous_rating": change.previous_rating,
            "new_rating": new_rating,
            "justification": change.justification,
        },
        request=request,
    )
    return change


# ═══════════════════════════════════════════════════════════════════════════
# Locking and reopening
# ═══════════════════════════════════════════════════════════════════════════


async def lock_session(
    session: AsyncSession,
    user: User,
    *,
    session_id: uuid.UUID,
    request: Request | None = None,
) -> dict[str, Any]:
    """Write the agreed ratings back to the appraisals.

    One transaction: the appraisal updates and the record of what changed them
    are durable together, or neither is. A rating written back without its
    justification surviving would be the worst of both.
    """
    calib = await _calibration_session(session, user, session_id)
    if calib.locked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This session is already locked."
        )

    changes = (
        await session.scalars(
            tenant_scoped(
                select(CalibrationChange)
                .where(CalibrationChange.session_id == calib.id)
                .order_by(CalibrationChange.changed_at),
                CalibrationChange,
                user,
            )
        )
    ).all()

    # The last change per appraisal wins — a rating moved twice in a session
    # ends where the room left it, and both moves stay in the history.
    latest: dict[uuid.UUID, CalibrationChange] = {}
    for change in changes:
        latest[change.appraisal_id] = change

    applied = 0
    for appraisal_id, change in latest.items():
        appraisal = await session.scalar(
            tenant_scoped(
                select(Appraisal).where(Appraisal.id == appraisal_id), Appraisal, user
            )
        )
        if appraisal is None:
            continue
        appraisal.final_rating = change.new_rating
        if change.new_score is not None:
            appraisal.final_score = change.new_score
        change.applied_on_lock = True
        applied += 1

    workspace = await session_workspace(session, user, session_id)
    calib.final_distribution = workspace["distribution"]
    calib.status = SessionStatus.COMPLETED
    calib.locked_at = datetime.now(UTC)
    calib.locked_by_user_id = user.id
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="calibration_session",
        entity_id=calib.id,
        summary=(
            f"Locked '{calib.name}': {applied} rating(s) written back from "
            f"{len(changes)} recorded change(s)"
        ),
        changes={"applied": applied, "total_changes": len(changes)},
        request=request,
    )
    return {
        "session_id": str(calib.id),
        "ratings_written": applied,
        "changes_recorded": len(changes),
        "final_distribution": calib.final_distribution,
    }


async def reopen_calibrated(
    session: AsyncSession,
    user: User,
    *,
    appraisal_id: uuid.UUID,
    reason: str,
    hr_override: bool = False,
    request: Request | None = None,
) -> Appraisal:
    """Reopen an appraisal whose rating was set in calibration.

    Requires an explicit HR override. The rating being undone was agreed by a
    group of people who are not in the room when it is undone, and every
    calibration change behind it stays in the record — reopening does not erase
    the session, it adds to the history.
    """
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Reopening a calibrated appraisal needs a reason.",
        )

    appraisal = await session.scalar(
        tenant_scoped(
            select(Appraisal).where(Appraisal.id == appraisal_id), Appraisal, user
        )
    )
    if appraisal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appraisal not found")

    history = (
        await session.scalars(
            tenant_scoped(
                select(CalibrationChange)
                .where(CalibrationChange.appraisal_id == appraisal_id)
                .order_by(CalibrationChange.changed_at),
                CalibrationChange,
                user,
            )
        )
    ).all()

    if history and not hr_override:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": (
                    "This rating was set in calibration and cannot be reopened without an "
                    "HR override. It was agreed by a group of people who are not in the "
                    "room now."
                ),
                "calibration_history": [
                    {
                        "from": c.previous_rating,
                        "to": c.new_rating,
                        "changed_at": c.changed_at.isoformat(),
                        "justification": c.justification,
                    }
                    for c in history
                ],
            },
        )

    appraisal.reopened_count += 1
    appraisal.reopen_reason = reason
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="appraisal",
        entity_id=appraisal.id,
        summary=(
            f"Reopened a calibrated appraisal under HR override: {reason} "
            f"({len(history)} calibration change(s) remain on the record)"
        ),
        changes={
            "reason": reason,
            "hr_override": hr_override,
            "calibration_changes_retained": len(history),
        },
        request=request,
    )
    return appraisal
