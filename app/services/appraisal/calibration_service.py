"""Calibration session persistence: policies, the workspace, the lock, the reopen.

The analysis lives in `calibration.py` and is exercised there. This module is
the database side, plus the things that only make sense against real records:

- **A change without a justification is refused**, at the service layer, so the
  API cannot bypass what the screen enforces.
- **Nobody calibrates their own rating**, facilitator and HR included.
- **Who is in the room decides who sees the room.** A session's workspace is
  visible to its facilitator, its participants and HR — not to every manager in
  the company. Only the facilitator or HR changes a rating.
- **Locking writes the agreed ratings back** to the appraisals, in one
  transaction with the change records.
- **Reopening needs an HR override** and is audited, because the rating being
  undone was agreed by a group of people who are not in the room when it is.
- **Evidence travels into the room.** Each person's goal score, recognition
  and review entries are available inside the workspace, at the moment a rating
  is argued over.
"""

from __future__ import annotations

import statistics
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.appraisal import (
    Appraisal,
    AppraisalCycle,
    AppraisalStageEntry,
)
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
from app.models.competency import AppraisalTemplate
from app.models.culture import Shoutout, ShoutoutVisibility
from app.models.employee import Employee
from app.models.org import Department
from app.models.user import User
from app.schemas.performance import PolicyCreate, SessionCreate
from app.services import audit
from app.services.appraisal import calibration, review
from app.services.goals import service as goals_service

__all__ = [
    "apply_change",
    "clone_policy",
    "create_policy",
    "create_session",
    "cycle_distribution",
    "get_policy",
    "list_changes",
    "list_sessions",
    "lock_session",
    "person_evidence",
    "reopen_calibrated",
    "reopen_session",
    "session_workspace",
    "set_session_status",
]


def _conflict(detail: Any) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


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


async def _target_rows(
    session: AsyncSession, user: User, policy_id: uuid.UUID
) -> list[DistributionTarget]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(DistributionTarget).where(DistributionTarget.policy_id == policy_id),
                    DistributionTarget,
                    user,
                )
            )
        ).all()
    )


async def _targets(
    session: AsyncSession, user: User, policy_id: uuid.UUID
) -> list[calibration.Target]:
    return [
        calibration.Target(
            rating=row.rating_value,
            target_percent=Decimal(str(row.target_percent)),
            tolerance_percent=Decimal(str(row.tolerance_percent)),
        )
        for row in await _target_rows(session, user, policy_id)
    ]


async def _cycle(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> AppraisalCycle:
    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == cycle_id), AppraisalCycle, user
        )
    )
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found")
    return cycle


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


def _in_room(user: User, calib: CalibrationSession) -> bool:
    return calib.facilitator_user_id == user.id or str(user.id) in {
        str(pid) for pid in (calib.participant_user_ids or [])
    }


def _require_room(user: User, calib: CalibrationSession) -> None:
    if permissions.is_hr(user) or _in_room(user, calib):
        return
    raise permissions.forbidden("You are not a facilitator or participant of this session.")


def _require_facilitator(user: User, calib: CalibrationSession) -> None:
    if permissions.is_hr(user) or calib.facilitator_user_id == user.id:
        return
    raise permissions.forbidden("Only the session's facilitator or HR can change ratings.")


def _scope_key(scope: CalibrationScope, employee: Employee) -> str:
    if scope is CalibrationScope.DEPARTMENT:
        return str(employee.department_id or "unassigned")
    if scope is CalibrationScope.MANAGER:
        return str(employee.manager_id or "unassigned")
    if scope is CalibrationScope.GRADE:
        return employee.grade or "ungraded"
    return "company"


# ═══════════════════════════════════════════════════════════════════════════
# Policies
# ═══════════════════════════════════════════════════════════════════════════


async def create_policy(
    session: AsyncSession, user: User, payload: PolicyCreate, *, request: Request | None = None
) -> CalibrationPolicy:
    """How calibration runs for one cycle. All three modes are first class."""
    permissions.require_hr(user, detail="Calibration policies are set by HR.")
    await _cycle(session, user, payload.cycle_id)
    if payload.mode is not CalibrationMode.NONE and not payload.targets:
        raise _unprocessable(
            f"A '{payload.mode.value}' policy needs distribution targets — there is "
            "nothing to measure against without them."
        )
    total = sum((t.target_percent for t in payload.targets), Decimal(0))
    if payload.targets and total != Decimal(100):
        raise _unprocessable(
            f"The distribution targets sum to {total}%, not 100%. A target set that "
            "does not cover the population cannot be satisfied by any distribution."
        )
    labels = [t.rating_value for t in payload.targets]
    if len(set(labels)) != len(labels):
        raise _unprocessable("Two targets name the same rating.")
    if await _policy(session, user, payload.cycle_id) is not None:
        raise _conflict("This cycle already has a calibration policy.")

    policy = CalibrationPolicy(
        tenant_id=user.tenant_id,
        cycle_id=payload.cycle_id,
        mode=payload.mode,
        scope=payload.scope,
        min_group_size=payload.min_group_size,
        disclose_calibration_to_employee=payload.disclose_calibration_to_employee,
    )
    session.add(policy)
    await session.flush()
    for target in payload.targets:
        session.add(
            DistributionTarget(tenant_id=user.tenant_id, policy_id=policy.id, **target.model_dump())
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="calibration_policy",
        entity_id=policy.id,
        summary=f"Set a {payload.mode.value} calibration policy by {payload.scope.value}",
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    return policy


async def get_policy(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> dict[str, Any] | None:
    await _cycle(session, user, cycle_id)
    policy = await _policy(session, user, cycle_id)
    if policy is None:
        return None
    return {
        "id": policy.id,
        "cycle_id": policy.cycle_id,
        "mode": policy.mode,
        "scope": policy.scope,
        "min_group_size": policy.min_group_size,
        "disclose_calibration_to_employee": policy.disclose_calibration_to_employee,
        "targets": [
            {
                "rating_value": t.rating_value,
                "target_percent": t.target_percent,
                "tolerance_percent": t.tolerance_percent,
            }
            for t in await _target_rows(session, user, policy.id)
        ],
    }


async def clone_policy(
    session: AsyncSession,
    user: User,
    *,
    source_cycle_id: uuid.UUID,
    target_cycle_id: uuid.UUID,
) -> bool:
    source = await _policy(session, user, source_cycle_id)
    if source is None:
        return False
    copy = CalibrationPolicy(
        tenant_id=user.tenant_id,
        cycle_id=target_cycle_id,
        mode=source.mode,
        scope=source.scope,
        min_group_size=source.min_group_size,
        disclose_calibration_to_employee=source.disclose_calibration_to_employee,
    )
    session.add(copy)
    await session.flush()
    for target in await _target_rows(session, user, source.id):
        session.add(
            DistributionTarget(
                tenant_id=user.tenant_id,
                policy_id=copy.id,
                rating_value=target.rating_value,
                target_percent=target.target_percent,
                tolerance_percent=target.tolerance_percent,
            )
        )
    return True


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
    await _cycle(session, user, cycle_id)
    policy = await _policy(session, user, cycle_id)
    if policy is None or policy.mode is CalibrationMode.NONE:
        return {
            "mode": policy.mode.value if policy else "none",
            "distributions": [],
            "blockers": [],
            "can_close": True,
            "excluded_scopes": [],
            "note": (
                "No calibration policy applies to this cycle, so no distribution is computed."
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
    blockers = calibration.enforcement_blockers(mode=policy.mode.value, distributions=distributions)

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
# Sessions
# ═══════════════════════════════════════════════════════════════════════════


async def create_session(
    session: AsyncSession, user: User, payload: SessionCreate, *, request: Request | None = None
) -> CalibrationSession:
    permissions.require_hr(user, detail="Calibration sessions are set up by HR.")
    await _cycle(session, user, payload.cycle_id)
    wanted = set(payload.participant_user_ids)
    if payload.facilitator_user_id is not None:
        wanted.add(payload.facilitator_user_id)
    if wanted:
        found = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(User.id).where(User.id.in_(wanted), User.is_active.is_(True)),
                        User,
                        user,
                    )
                )
            ).all()
        )
        if found != wanted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="A facilitator or participant was not found in this workspace.",
            )
    row = CalibrationSession(
        tenant_id=user.tenant_id,
        cycle_id=payload.cycle_id,
        name=payload.name,
        scope=payload.scope,
        scope_value=payload.scope_value,
        facilitator_user_id=payload.facilitator_user_id,
        participant_user_ids=[str(u) for u in payload.participant_user_ids],
        scheduled_at=payload.scheduled_at,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="calibration_session",
        entity_id=row.id,
        summary=f"Set up calibration session '{payload.name}'",
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    return row


def _session_payload(
    calib: CalibrationSession, cycle_names: dict[uuid.UUID, str], names: dict[uuid.UUID, str]
) -> dict[str, Any]:
    return {
        "id": calib.id,
        "cycle_id": calib.cycle_id,
        "cycle_name": cycle_names.get(calib.cycle_id),
        "name": calib.name,
        "scope": calib.scope,
        "scope_value": calib.scope_value,
        "status": calib.status,
        "facilitator_user_id": calib.facilitator_user_id,
        "facilitator_name": names.get(calib.facilitator_user_id)
        if calib.facilitator_user_id
        else None,
        "participant_user_ids": [uuid.UUID(str(p)) for p in (calib.participant_user_ids or [])],
        "scheduled_at": calib.scheduled_at,
        "locked_at": calib.locked_at,
    }


async def list_sessions(
    session: AsyncSession, user: User, *, cycle_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """HR sees every session; anybody else sees the sessions they are in."""
    stmt = tenant_scoped(select(CalibrationSession), CalibrationSession, user).order_by(
        CalibrationSession.created_at.desc()
    )
    if cycle_id is not None:
        stmt = stmt.where(CalibrationSession.cycle_id == cycle_id)
    rows = [
        r
        for r in (await session.scalars(stmt)).all()
        if permissions.is_hr(user) or _in_room(user, r)
    ]
    cycle_names: dict[uuid.UUID, str] = {}
    names: dict[uuid.UUID, str] = {}
    if rows:
        cycle_names = {
            c.id: c.name
            for c in (
                await session.scalars(
                    tenant_scoped(
                        select(AppraisalCycle).where(
                            AppraisalCycle.id.in_({r.cycle_id for r in rows})
                        ),
                        AppraisalCycle,
                        user,
                    )
                )
            ).all()
        }
        facilitators = {r.facilitator_user_id for r in rows if r.facilitator_user_id}
        if facilitators:
            names = {
                u.id: u.full_name
                for u in (
                    await session.scalars(
                        tenant_scoped(select(User).where(User.id.in_(facilitators)), User, user)
                    )
                ).all()
            }
    return [_session_payload(r, cycle_names, names) for r in rows]


async def set_session_status(
    session: AsyncSession,
    user: User,
    *,
    session_id: uuid.UUID,
    action: str,
    request: Request | None = None,
) -> CalibrationSession:
    """Start (facilitator or HR) or cancel (HR) a session."""
    calib = await _calibration_session(session, user, session_id)
    if calib.locked_at is not None:
        raise _conflict("This session is locked.")
    if action == "start":
        _require_facilitator(user, calib)
        if calib.status is not SessionStatus.SCHEDULED:
            raise _conflict(f"This session is already {calib.status.value}.")
        calib.status = SessionStatus.IN_PROGRESS
        calib.started_at = datetime.now(UTC)
    elif action == "cancel":
        permissions.require_hr(user, detail="Only HR can cancel a calibration session.")
        if calib.status is SessionStatus.CANCELLED:
            raise _conflict("This session is already cancelled.")
        calib.status = SessionStatus.CANCELLED
    else:
        raise _unprocessable(f"'{action}' is not a session action.")
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="calibration_session",
        entity_id=calib.id,
        summary=f"Calibration session '{calib.name}': {action}",
        changes={"status": calib.status.value},
        request=request,
    )
    return calib


# ═══════════════════════════════════════════════════════════════════════════
# The session workspace
# ═══════════════════════════════════════════════════════════════════════════


async def _rating_options(
    session: AsyncSession, user: User, cycle_id: uuid.UUID, policy: CalibrationPolicy | None
) -> list[str]:
    template = await session.scalar(
        tenant_scoped(
            select(AppraisalTemplate).where(AppraisalTemplate.cycle_id == cycle_id),
            AppraisalTemplate,
            user,
        )
    )
    labels = [b["label"] for b in (template.score_bands or [])] if template else []
    if not labels and policy is not None:
        labels = [t.rating_value for t in await _target_rows(session, user, policy.id)]
    return labels


async def session_workspace(
    session: AsyncSession, user: User, session_id: uuid.UUID
) -> dict[str, Any]:
    """Everything the facilitator's screen needs, in one call.

    The grid (rating, score, goal score, tenure, recognition), the live
    distribution against target, outlier managers, and each person's peers at
    the same grade. Assembled server-side because a screen that fetches these
    separately shows a distribution that disagrees with the rows above it for a
    few hundred milliseconds after every change.
    """
    calib = await _calibration_session(session, user, session_id)
    _require_room(user, calib)
    cycle = await _cycle(session, user, calib.cycle_id)
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
        if calib.scope is CalibrationScope.COMPANY
        or _scope_key(calib.scope, employee) == calib.scope_value
    ]
    employee_ids = [e.id for _, e in in_scope]

    goal_scores = await goals_service.goal_scores_for_many(
        session, user, employee_ids=employee_ids, cycle=cycle
    )
    recognition_counts: dict[uuid.UUID, int] = {}
    if employee_ids:
        recognition_counts = {
            to_id: count
            for to_id, count in (
                await session.execute(
                    tenant_scoped(
                        select(Shoutout.to_employee_id, func.count(Shoutout.id))
                        .where(
                            Shoutout.to_employee_id.in_(employee_ids),
                            Shoutout.visibility != ShoutoutVisibility.PRIVATE,
                            func.date(Shoutout.created_at) >= cycle.period_start,
                            func.date(Shoutout.created_at) <= cycle.period_end,
                        )
                        .group_by(Shoutout.to_employee_id),
                        Shoutout,
                        user,
                    )
                )
            ).all()
        }
    manager_ids = {e.manager_id for _, e in in_scope if e.manager_id}
    department_ids = {e.department_id for _, e in in_scope if e.department_id}
    managers: dict[uuid.UUID, str] = {}
    departments: dict[uuid.UUID, str] = {}
    if manager_ids:
        managers = {
            m.id: m.full_name
            for m in (
                await session.scalars(
                    tenant_scoped(
                        select(Employee).where(Employee.id.in_(manager_ids)), Employee, user
                    )
                )
            ).all()
        }
    if department_ids:
        departments = {
            d.id: d.name
            for d in (
                await session.scalars(
                    tenant_scoped(
                        select(Department).where(Department.id.in_(department_ids)),
                        Department,
                        user,
                    )
                )
            ).all()
        }

    today = date.today()
    grid = []
    for appraisal, employee in in_scope:
        goal = goal_scores.get(employee.id)
        tenure = (
            max(0, (today - employee.date_of_joining).days // 30)
            if employee.date_of_joining
            else None
        )
        grid.append(
            {
                "appraisal_id": str(appraisal.id),
                "employee_id": str(employee.id),
                "employee_code": employee.employee_code,
                "name": employee.full_name,
                "grade": employee.grade,
                "department": departments.get(employee.department_id),
                "manager_id": str(employee.manager_id) if employee.manager_id else None,
                "manager_name": managers.get(employee.manager_id),
                "date_of_joining": (
                    employee.date_of_joining.isoformat() if employee.date_of_joining else None
                ),
                "tenure_months": tenure,
                "current_rating": appraisal.final_rating,
                "current_score": (
                    str(appraisal.final_score) if appraisal.final_score is not None else None
                ),
                "goal_score": str(goal.score) if goal and goal.score is not None else None,
                "goal_measured_weight_percent": (
                    str(goal.measured_weight_percent) if goal else None
                ),
                "recognition_count": recognition_counts.get(employee.id, 0),
                "status": appraisal.status.value,
                "is_self": employee.id == user.employee_id,
            }
        )

    rated = [r["current_rating"] for r in grid if r["current_rating"]]
    distribution = calibration.analyse_distribution(
        scope=calib.scope.value,
        scope_value=calib.scope_value,
        ratings=rated,
        targets=targets,
        min_group_size=policy.min_group_size if policy else 1,
    )

    # Peers at the same grade, so "is this rating right" has something to be
    # answered against. The person's own score is left out of their comparison.
    by_grade: dict[str, list[str]] = {}
    for row in grid:
        if row["current_score"] is not None:
            by_grade.setdefault(row["grade"] or "ungraded", []).append(row["current_score"])
    for row in grid:
        peers = [
            Decimal(r["current_score"])
            for r in grid
            if r["current_score"] is not None
            and (r["grade"] or "ungraded") == (row["grade"] or "ungraded")
            and r["appraisal_id"] != row["appraisal_id"]
        ]
        row["peer_count"] = len(peers)
        row["peer_median_score"] = (
            str(Decimal(str(statistics.median(peers))).quantize(Decimal("0.01"))) if peers else None
        )

    by_manager: dict[str, dict[str, Any]] = {}
    for row in grid:
        if row["manager_id"] and row["current_score"] is not None:
            entry = by_manager.setdefault(
                row["manager_id"],
                {
                    "manager_id": row["manager_id"],
                    "manager_name": row["manager_name"] or "",
                    "scores": [],
                },
            )
            entry["scores"].append(Decimal(row["current_score"]))
    outliers = [
        profile.as_dict()
        for profile in calibration.detect_outliers(managers=list(by_manager.values()))
    ]

    is_hr = permissions.is_hr(user)
    return {
        "session": {
            "id": str(calib.id),
            "cycle_id": str(calib.cycle_id),
            "cycle_name": cycle.name,
            "name": calib.name,
            "scope": calib.scope.value,
            "scope_value": calib.scope_value,
            "status": calib.status.value,
            "locked_at": calib.locked_at.isoformat() if calib.locked_at else None,
        },
        "viewer": {
            "is_hr": is_hr,
            "is_facilitator": calib.facilitator_user_id == user.id,
            "can_change": (is_hr or calib.facilitator_user_id == user.id)
            and calib.locked_at is None
            and calib.status is not SessionStatus.CANCELLED,
            "can_lock": is_hr
            and calib.locked_at is None
            and calib.status is not SessionStatus.CANCELLED,
            "can_reopen": is_hr and calib.locked_at is not None,
        },
        "rating_options": await _rating_options(session, user, calib.cycle_id, policy),
        "grid": grid,
        "distribution": distribution.as_dict(),
        "peer_scores_by_grade": by_grade,
        "outliers": outliers,
        "mode": policy.mode.value if policy else "none",
        "min_group_size": policy.min_group_size if policy else None,
    }


async def person_evidence(
    session: AsyncSession, user: User, *, session_id: uuid.UUID, appraisal_id: uuid.UUID
) -> dict[str, Any]:
    """One person's evidence inside the room: goals, recognition, review entries."""
    calib = await _calibration_session(session, user, session_id)
    _require_room(user, calib)
    appraisal, cycle, stages, employee = await review.load_appraisal(session, user, appraisal_id)
    if appraisal.cycle_id != calib.cycle_id:
        raise _conflict("That appraisal belongs to a different cycle from this session.")
    if (
        calib.scope is not CalibrationScope.COMPANY
        and _scope_key(calib.scope, employee) != calib.scope_value
    ):
        raise permissions.forbidden("That person is outside this session's scope.")

    bundle = await review.load_bundle(session, user, appraisal, cycle)
    competencies = [c for _, c in bundle.items]
    evidence = await review.recognition_evidence(
        session,
        user,
        appraisal=appraisal,
        cycle=cycle,
        competencies=competencies,
        viewer_is_subject=False,
    )
    goal = await goals_service.goal_score_for(session, user, employee_id=employee.id, cycle=cycle)
    stage_by_id = {s.id: s for s in stages}
    entries = (
        await session.scalars(
            tenant_scoped(
                select(AppraisalStageEntry)
                .where(
                    AppraisalStageEntry.appraisal_id == appraisal.id,
                    AppraisalStageEntry.submitted_at.isnot(None),
                )
                .order_by(AppraisalStageEntry.submitted_at),
                AppraisalStageEntry,
                user,
            )
        )
    ).all()
    history = (
        await session.scalars(
            tenant_scoped(
                select(CalibrationChange)
                .where(CalibrationChange.appraisal_id == appraisal.id)
                .order_by(CalibrationChange.changed_at),
                CalibrationChange,
                user,
            )
        )
    ).all()
    return {
        "appraisal_id": str(appraisal.id),
        "employee_name": employee.full_name,
        "goals": goal.as_dict(),
        "recognition": {
            "values": evidence["values"],
            "by_competency": [
                {**(evidence["by_competency"].get(str(c.id)) or {}), "competency_id": str(c.id)}
                for c in competencies
            ],
        },
        "entries": [
            {
                "stage_label": stage_by_id[e.stage_id].label if e.stage_id in stage_by_id else None,
                "stage_role": (
                    stage_by_id[e.stage_id].role.value if e.stage_id in stage_by_id else None
                ),
                "score": str(e.score) if e.score is not None else None,
                "comments": e.comments,
                "returned_reason": e.returned_reason,
            }
            for e in entries
            if e.stage_id in stage_by_id
        ],
        "calibration_history": [
            {
                "from": c.previous_rating,
                "to": c.new_rating,
                "changed_at": c.changed_at.isoformat(),
                "justification": c.justification,
            }
            for c in history
        ],
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
        raise _unprocessable(
            "A calibration change needs a justification. This rating was agreed in a "
            "room the employee was not in — if it is questioned later, the reason is "
            "what the organisation has to answer with."
        )

    calib = await _calibration_session(session, user, session_id)
    _require_facilitator(user, calib)
    if calib.locked_at is not None:
        raise _conflict(
            "This session is locked and its ratings have been written back. Reopen "
            "the appraisal with an HR override to change it."
        )
    if calib.status is SessionStatus.CANCELLED:
        raise _conflict("This session was cancelled.")

    appraisal = await session.scalar(
        tenant_scoped(select(Appraisal).where(Appraisal.id == appraisal_id), Appraisal, user)
    )
    if appraisal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appraisal not found")
    if appraisal.cycle_id != calib.cycle_id:
        raise _conflict("That appraisal belongs to a different cycle from this session.")
    if appraisal.employee_id == user.employee_id:
        raise permissions.forbidden(
            "You cannot calibrate your own rating. Somebody else in the room has to."
        )

    options = await _rating_options(
        session, user, calib.cycle_id, await _policy(session, user, calib.cycle_id)
    )
    if options and new_rating not in options:
        raise _unprocessable(
            f"'{new_rating}' is not a rating in this cycle. Choose one of: {', '.join(options)}."
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


async def list_changes(
    session: AsyncSession, user: User, session_id: uuid.UUID
) -> list[CalibrationChange]:
    calib = await _calibration_session(session, user, session_id)
    _require_room(user, calib)
    return list(
        (
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
    )


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
    are durable together, or neither is.
    """
    permissions.require_hr(user, detail="Only HR can lock a calibration session.")
    calib = await _calibration_session(session, user, session_id)
    if calib.locked_at is not None:
        raise _conflict("This session is already locked.")
    if calib.status is SessionStatus.CANCELLED:
        raise _conflict("This session was cancelled.")

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
            tenant_scoped(select(Appraisal).where(Appraisal.id == appraisal_id), Appraisal, user)
        )
        if appraisal is None:
            continue
        appraisal.final_rating = change.new_rating
        if change.new_score is not None:
            appraisal.final_score = change.new_score
        change.applied_on_lock = True
        applied += 1

    await session.flush()
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


async def reopen_session(
    session: AsyncSession,
    user: User,
    *,
    session_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> CalibrationSession:
    """Unlock a session so the room can reconvene. The earlier lock stays in the audit."""
    permissions.require_hr(user, detail="Only HR can reopen a locked calibration session.")
    if not reason.strip():
        raise _unprocessable("Reopening a locked session needs a reason.")
    calib = await _calibration_session(session, user, session_id)
    if calib.locked_at is None:
        raise _conflict("This session is not locked.")
    previous = {
        "locked_at": calib.locked_at.isoformat(),
        "locked_by_user_id": str(calib.locked_by_user_id) if calib.locked_by_user_id else None,
        "final_distribution": calib.final_distribution,
    }
    calib.locked_at = None
    calib.locked_by_user_id = None
    calib.status = SessionStatus.IN_PROGRESS
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="calibration_session",
        entity_id=calib.id,
        summary=f"Reopened locked session '{calib.name}': {reason}",
        changes={"reason": reason, "previous_lock": previous},
        request=request,
    )
    return calib


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
        raise _unprocessable("Reopening a calibrated appraisal needs a reason.")

    appraisal = await session.scalar(
        tenant_scoped(select(Appraisal).where(Appraisal.id == appraisal_id), Appraisal, user)
    )
    if appraisal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appraisal not found")
    if appraisal.employee_id == user.employee_id:
        raise permissions.forbidden("You cannot reopen your own calibrated rating.")

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
