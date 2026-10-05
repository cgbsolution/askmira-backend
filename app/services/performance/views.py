"""What the PIP and increment screens read, and the smaller PIP writes.

Split from `service.py` so that module stays the slice 35 decision path its
invariants describe. Everything here decides self / reporting line / HR through
`app.core.permissions` before it returns anything about a person.

Pay is the most sensitive thing on these screens, so two restraints are
deliberate:

- a manager sees current CTC only for people in their own reporting line, never
  their own row as something they can act on;
- the equity comparison a manager is shown carries no individual salary — the
  peer minimum and maximum are removed, because in a group of five the minimum
  is one named person's pay.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.appraisal import Appraisal, AppraisalCycle
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department
from app.models.payroll import EmployeeSalary
from app.models.performance_flow import (
    IncrementCycle,
    IncrementMatrixRow,
    IncrementRecommendation,
    MilestoneStatusEnum,
    Pip,
    PipCheckIn,
    PipEmployeeStatement,
    PipMilestone,
    PipStatusEnum,
    PromotionRecord,
    RecommendationStatusEnum,
)
from app.models.user import User
from app.services import audit
from app.services.performance import pip as pip_engine
from app.services.performance import service as performance

__all__ = [
    "add_employee_statement",
    "assess_milestone",
    "current_ctc",
    "increment_candidates",
    "increment_cycle_detail",
    "list_increment_cycles",
    "list_pips",
    "people",
    "pip_detail",
    "redact_equity",
]

OPEN_PIP = (PipStatusEnum.DRAFT, PipStatusEnum.ACTIVE, PipStatusEnum.EXTENDED)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _d(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def redact_equity(snapshot: dict[str, Any], *, is_hr: bool) -> dict[str, Any]:
    """Remove figures that are one identifiable person's pay, for non-HR viewers."""
    if is_hr or not snapshot:
        return snapshot
    redacted = dict(snapshot)
    parity = redacted.get("parity")
    if isinstance(parity, dict):
        redacted["parity"] = {k: v for k, v in parity.items() if k not in ("peer_min", "peer_max")}
    return redacted


# ═══════════════════════════════════════════════════════════════════════════
# People a caller may act for
# ═══════════════════════════════════════════════════════════════════════════


async def people(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The caller's reporting line, or everybody for HR — for pickers on these screens."""
    visible = await permissions.visible_employee_ids(session, user)
    stmt = tenant_scoped(
        select(Employee, User.id, User.role)
        .outerjoin(User, (User.employee_id == Employee.id) & (User.is_active.is_(True)))
        .where(Employee.deleted_at.is_(None), Employee.status != EmploymentStatus.EXITED),
        Employee,
        user,
    ).order_by(Employee.first_name, Employee.last_name)
    if visible is not None:
        stmt = stmt.where(Employee.id.in_(visible))
    rows = (await session.execute(stmt)).all()
    department_ids = {e.department_id for e, _, _ in rows if e.department_id}
    departments: dict[uuid.UUID, str] = {}
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
    return [
        {
            "employee_id": str(e.id),
            "name": e.full_name,
            "employee_code": e.employee_code,
            "designation": e.designation,
            "grade": e.grade,
            "department_id": str(e.department_id) if e.department_id else None,
            "department": departments.get(e.department_id),
            "manager_id": str(e.manager_id) if e.manager_id else None,
            "user_id": str(user_id) if user_id else None,
            "role": role.value if role else None,
            "is_self": e.id == user.employee_id,
        }
        for e, user_id, role in rows
    ]


# ═══════════════════════════════════════════════════════════════════════════
# PIPs
# ═══════════════════════════════════════════════════════════════════════════


async def _pip(session: AsyncSession, user: User, pip_id: uuid.UUID) -> Pip:
    row = await session.scalar(tenant_scoped(select(Pip).where(Pip.id == pip_id), Pip, user))
    if row is None:
        raise _not_found("No such PIP in this tenant.")
    return row


async def _may_see_pip(session: AsyncSession, user: User, row: Pip) -> bool:
    if permissions.is_hr(user) or row.employee_id == user.employee_id:
        return True
    if row.initiated_by_user_id == user.id:
        return True
    return await permissions.is_line_manager_of(session, user, row.employee_id)


async def list_pips(
    session: AsyncSession,
    user: User,
    *,
    pip_status: PipStatusEnum | None = None,
) -> list[dict[str, Any]]:
    """Plans the caller may see: their line's, ones they opened, their own, or all for HR."""
    stmt = tenant_scoped(
        select(Pip, Employee).join(Employee, Employee.id == Pip.employee_id), Pip, user
    ).order_by(Pip.start_date.desc())
    if pip_status is not None:
        stmt = stmt.where(Pip.status == pip_status)
    rows = (await session.execute(stmt)).all()
    if not permissions.is_hr(user):
        line = await permissions.reporting_line(session, user)
        rows = [
            (p, e)
            for p, e in rows
            if p.employee_id in line
            or p.initiated_by_user_id == user.id
            or p.employee_id == user.employee_id
        ]
    ids = [p.id for p, _ in rows]
    check_ins: dict[uuid.UUID, int] = {}
    milestones: dict[uuid.UUID, tuple[int, int]] = {}
    if ids:
        check_ins = dict(
            (
                await session.execute(
                    tenant_scoped(
                        select(PipCheckIn.pip_id, func.count(PipCheckIn.id))
                        .where(PipCheckIn.pip_id.in_(ids))
                        .group_by(PipCheckIn.pip_id),
                        PipCheckIn,
                        user,
                    )
                )
            ).all()
        )
        for pip_id, milestone_status, count in (
            await session.execute(
                tenant_scoped(
                    select(PipMilestone.pip_id, PipMilestone.status, func.count(PipMilestone.id))
                    .where(PipMilestone.pip_id.in_(ids))
                    .group_by(PipMilestone.pip_id, PipMilestone.status),
                    PipMilestone,
                    user,
                )
            )
        ).all():
            total, assessed = milestones.get(pip_id, (0, 0))
            milestones[pip_id] = (
                total + count,
                assessed + (count if milestone_status is not MilestoneStatusEnum.PENDING else 0),
            )
    return [
        {
            "pip_id": str(p.id),
            "employee_id": str(e.id),
            "employee_name": e.full_name,
            "employee_code": e.employee_code,
            "status": p.status.value,
            "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat(),
            "reason": p.reason,
            "check_ins": check_ins.get(p.id, 0),
            "milestones": milestones.get(p.id, (0, 0))[0],
            "milestones_assessed": milestones.get(p.id, (0, 0))[1],
            "is_mine": p.employee_id == user.employee_id,
        }
        for p, e in rows
    ]


async def pip_detail(session: AsyncSession, user: User, pip_id: uuid.UUID) -> dict[str, Any]:
    row = await _pip(session, user, pip_id)
    if not await _may_see_pip(session, user, row):
        raise permissions.forbidden("This improvement plan is not visible to you.")
    is_subject = row.employee_id == user.employee_id
    is_hr = permissions.is_hr(user)
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == row.employee_id), Employee, user)
    )
    facts = await performance._engine_pip(session, user, row)
    trail = pip_engine.trail_quality(facts)

    milestones = (
        await session.scalars(
            tenant_scoped(
                select(PipMilestone)
                .where(PipMilestone.pip_id == row.id)
                .order_by(PipMilestone.target_date),
                PipMilestone,
                user,
            )
        )
    ).all()
    check_ins = (
        await session.scalars(
            tenant_scoped(
                select(PipCheckIn)
                .where(PipCheckIn.pip_id == row.id)
                .order_by(PipCheckIn.scheduled_for),
                PipCheckIn,
                user,
            )
        )
    ).all()
    statements = (
        await session.scalars(
            tenant_scoped(
                select(PipEmployeeStatement)
                .where(PipEmployeeStatement.pip_id == row.id)
                .order_by(PipEmployeeStatement.recorded_at),
                PipEmployeeStatement,
                user,
            )
        )
    ).all()
    user_ids = {c.recorded_by_user_id for c in check_ins if c.recorded_by_user_id}
    if row.initiated_by_user_id:
        user_ids.add(row.initiated_by_user_id)
    names: dict[uuid.UUID, str] = {}
    if user_ids:
        names = {
            u.id: u.full_name
            for u in (
                await session.scalars(
                    tenant_scoped(select(User).where(User.id.in_(user_ids)), User, user)
                )
            ).all()
        }
    open_plan = row.status in OPEN_PIP
    can_record = (
        not is_subject
        and open_plan
        and (
            is_hr
            or row.initiated_by_user_id == user.id
            or await permissions.is_line_manager_of(session, user, row.employee_id)
        )
    )
    return {
        "pip": {
            "id": str(row.id),
            "employee_id": str(row.employee_id),
            "employee_name": employee.full_name if employee else None,
            "initiated_by_name": names.get(row.initiated_by_user_id)
            if row.initiated_by_user_id
            else None,
            "appraisal_id": str(row.appraisal_id) if row.appraisal_id else None,
            "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat(),
            "reason": row.reason,
            "expectations": row.expectations,
            "support_provided": row.support_provided,
            "status": row.status.value,
            "outcome_reason": row.outcome_reason,
            "outcome_recorded_on": (
                row.outcome_recorded_on.isoformat() if row.outcome_recorded_on else None
            ),
            "extensions": row.extensions or [],
            "weak_trail_acknowledged": row.weak_trail_acknowledged,
        },
        "milestones": [
            {
                "id": str(m.id),
                "description": m.description,
                "target_date": m.target_date.isoformat(),
                "measurable_criteria": m.measurable_criteria,
                "status": m.status.value,
                "assessed_on": m.assessed_on.isoformat() if m.assessed_on else None,
                "assessment_note": m.assessment_note,
            }
            for m in milestones
        ],
        "check_ins": [
            {
                "id": str(c.id),
                "milestone_id": str(c.milestone_id) if c.milestone_id else None,
                "scheduled_for": c.scheduled_for.isoformat(),
                "held_on": c.held_on.isoformat() if c.held_on else None,
                "manager_comments": c.manager_comments,
                "employee_comments": c.employee_comments,
                "employee_input_state": c.employee_input_state.value,
                "decline_reason": c.decline_reason,
                "recorded_by_name": names.get(c.recorded_by_user_id)
                if c.recorded_by_user_id
                else None,
            }
            for c in check_ins
        ],
        "statements": [
            {
                "id": str(s.id),
                "check_in_id": str(s.check_in_id) if s.check_in_id else None,
                "statement": s.statement,
                "recorded_at": s.recorded_at.isoformat(),
            }
            for s in statements
        ],
        "trail": trail.as_dict(),
        "viewer": {
            "is_subject": is_subject,
            "can_record_check_in": can_record,
            "can_assess_milestones": can_record,
            "can_add_statement": is_subject and open_plan,
            "can_close": is_hr and not is_subject and open_plan,
            "can_see_evidence_pack": is_hr and not is_subject,
        },
    }


async def assess_milestone(
    session: AsyncSession,
    user: User,
    *,
    pip_id: uuid.UUID,
    milestone_id: uuid.UUID,
    milestone_status: MilestoneStatusEnum,
    note: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record whether one milestone was met. Never by the person on the plan."""
    row = await _pip(session, user, pip_id)
    if row.initiated_by_user_id != user.id or row.employee_id == user.employee_id:
        await permissions.require_manager_or_hr(
            session,
            user,
            row.employee_id,
            detail="Milestones are assessed by the employee's manager or HR.",
        )
    if row.status not in OPEN_PIP:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This plan is {row.status.value}; its milestones are no longer assessed.",
        )
    if milestone_status is MilestoneStatusEnum.PENDING:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An assessment records met, not met or waived.",
        )
    if not note.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An assessment needs a note saying what it was based on.",
        )
    milestone = await session.scalar(
        tenant_scoped(
            select(PipMilestone).where(
                PipMilestone.id == milestone_id, PipMilestone.pip_id == row.id
            ),
            PipMilestone,
            user,
        )
    )
    if milestone is None:
        raise _not_found("That milestone is not part of this plan.")
    previous = milestone.status.value
    milestone.status = milestone_status
    milestone.assessed_on = date.today()
    milestone.assessment_note = note.strip()
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="pip_milestone",
        entity_id=milestone.id,
        actor=user,
        changes={"from": previous, "to": milestone_status.value, "note": note.strip()},
        summary=f"Milestone assessed as {milestone_status.value}: {milestone.description}",
        request=request,
    )
    await session.commit()
    return {"milestone_id": str(milestone.id), "status": milestone.status.value}


async def add_employee_statement(
    session: AsyncSession,
    user: User,
    *,
    pip_id: uuid.UUID,
    statement: str,
    check_in_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """The employee's own account, typed by them. Nobody writes it on their behalf."""
    row = await _pip(session, user, pip_id)
    permissions.require_self(
        user,
        row.employee_id,
        detail="Only the person on the plan can add their own statement to it.",
    )
    if row.status not in OPEN_PIP:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This plan is {row.status.value}. Ask HR to attach a later statement.",
        )
    if not statement.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The statement is empty."
        )
    if check_in_id is not None:
        found = await session.scalar(
            tenant_scoped(
                select(PipCheckIn.id).where(
                    PipCheckIn.id == check_in_id, PipCheckIn.pip_id == row.id
                ),
                PipCheckIn,
                user,
            )
        )
        if found is None:
            raise _not_found("That check-in is not part of this plan.")
    entry = PipEmployeeStatement(
        tenant_id=user.tenant_id,
        pip_id=row.id,
        check_in_id=check_in_id,
        statement=statement.strip(),
        author_user_id=user.id,
        recorded_at=datetime.now(UTC),
    )
    session.add(entry)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="pip_employee_statement",
        entity_id=entry.id,
        actor=user,
        changes={"pip_id": str(row.id), "check_in_id": str(check_in_id) if check_in_id else None},
        summary="The employee added their own statement to their improvement plan.",
        request=request,
    )
    await session.commit()
    return {"statement_id": str(entry.id), "recorded_at": entry.recorded_at.isoformat()}


# ═══════════════════════════════════════════════════════════════════════════
# Increments
# ═══════════════════════════════════════════════════════════════════════════


async def current_ctc(
    session: AsyncSession, user: User, employee_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Each employee's current annual CTC from their open salary assignment."""
    if not employee_ids:
        return {}
    rows = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeSalary)
                .where(
                    EmployeeSalary.employee_id.in_(employee_ids),
                    EmployeeSalary.effective_to.is_(None),
                )
                .order_by(EmployeeSalary.effective_from),
                EmployeeSalary,
                user,
            )
        )
    ).all()
    return {row.employee_id: _d(row.ctc_annual) for row in rows}


async def _increment_cycle(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> IncrementCycle:
    cycle = await session.scalar(
        tenant_scoped(
            select(IncrementCycle).where(IncrementCycle.id == cycle_id), IncrementCycle, user
        )
    )
    if cycle is None:
        raise _not_found("No such increment cycle in this tenant.")
    return cycle


async def list_increment_cycles(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    permissions.require_role(
        user,
        permissions.HR_ROLES,
        line_managers=True,
        detail="Increment cycles are for managers and HR.",
    )
    cycles = (
        await session.scalars(
            tenant_scoped(select(IncrementCycle), IncrementCycle, user).order_by(
                IncrementCycle.effective_from.desc()
            )
        )
    ).all()
    return [
        {
            "id": str(c.id),
            "name": c.name,
            "effective_from": c.effective_from.isoformat(),
            "status": c.status.value,
            "overage_mode": c.overage_mode.value,
            "letter_release_on": c.letter_release_on.isoformat() if c.letter_release_on else None,
            "letters_released_at": c.letters_released_at.isoformat()
            if c.letters_released_at
            else None,
            "linked_appraisal_cycle_id": (
                str(c.linked_appraisal_cycle_id) if c.linked_appraisal_cycle_id else None
            ),
        }
        for c in cycles
    ]


async def increment_cycle_detail(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> dict[str, Any]:
    permissions.require_role(
        user,
        permissions.HR_ROLES,
        line_managers=True,
        detail="Increment cycles are for managers and HR.",
    )
    cycle = await _increment_cycle(session, user, cycle_id)
    bands = (
        await session.scalars(
            tenant_scoped(
                select(IncrementMatrixRow)
                .where(IncrementMatrixRow.cycle_id == cycle.id)
                .order_by(IncrementMatrixRow.min_percentage.desc()),
                IncrementMatrixRow,
                user,
            )
        )
    ).all()
    counts = dict(
        (
            await session.execute(
                tenant_scoped(
                    select(IncrementRecommendation.status, func.count(IncrementRecommendation.id))
                    .where(IncrementRecommendation.cycle_id == cycle.id)
                    .group_by(IncrementRecommendation.status),
                    IncrementRecommendation,
                    user,
                )
            )
        ).all()
    )
    linked_name = None
    if cycle.linked_appraisal_cycle_id:
        linked_name = await session.scalar(
            tenant_scoped(
                select(AppraisalCycle.name).where(
                    AppraisalCycle.id == cycle.linked_appraisal_cycle_id
                ),
                AppraisalCycle,
                user,
            )
        )
    budget = await performance.budget_view(session, user, cycle_id=cycle.id, department_id=None)
    is_hr = permissions.is_hr(user)
    return {
        "cycle": {
            "id": str(cycle.id),
            "name": cycle.name,
            "effective_from": cycle.effective_from.isoformat(),
            "status": cycle.status.value,
            "overage_mode": cycle.overage_mode.value,
            "letter_release_on": cycle.letter_release_on.isoformat()
            if cycle.letter_release_on
            else None,
            "letters_released_at": (
                cycle.letters_released_at.isoformat() if cycle.letters_released_at else None
            ),
            "linked_appraisal_cycle_id": (
                str(cycle.linked_appraisal_cycle_id) if cycle.linked_appraisal_cycle_id else None
            ),
            "linked_appraisal_cycle_name": linked_name,
            "budget_by_department": cycle.budget_by_department if is_hr else None,
        },
        "bands": [
            {
                "rating_label": b.rating_label,
                "min_percentage": str(b.min_percentage),
                "max_percentage": str(b.max_percentage),
                "recommended_percentage": str(b.recommended_percentage),
            }
            for b in bands
        ],
        "company_budget": budget["company"],
        "recommendations_by_status": {k.value: v for k, v in counts.items()},
        "viewer": {"is_hr": is_hr, "can_release": is_hr},
    }


async def increment_candidates(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> dict[str, Any]:
    """The people the caller may recommend for, with rating, band, pay and status.

    A manager sees their own reporting line; HR sees everybody. The department
    headroom is returned for each department that appears, so the manager sees
    what is left while they allocate.
    """
    permissions.require_role(
        user,
        permissions.HR_ROLES,
        line_managers=True,
        detail="Increment recommendations are for managers and HR.",
    )
    cycle = await _increment_cycle(session, user, cycle_id)
    is_hr = permissions.is_hr(user)
    stmt = tenant_scoped(
        select(Employee).where(
            Employee.deleted_at.is_(None), Employee.status != EmploymentStatus.EXITED
        ),
        Employee,
        user,
    ).order_by(Employee.first_name, Employee.last_name)
    if not is_hr:
        line = await permissions.reporting_line(session, user)
        if not line:
            return {"rows": [], "departments": []}
        stmt = stmt.where(Employee.id.in_(line))
    employees = list((await session.scalars(stmt)).all())
    ids = [e.id for e in employees]

    ctcs = await current_ctc(session, user, ids)
    ratings: dict[uuid.UUID, str | None] = {}
    if cycle.linked_appraisal_cycle_id and ids:
        ratings = dict(
            (
                await session.execute(
                    tenant_scoped(
                        select(Appraisal.employee_id, Appraisal.final_rating).where(
                            Appraisal.cycle_id == cycle.linked_appraisal_cycle_id,
                            Appraisal.employee_id.in_(ids),
                        ),
                        Appraisal,
                        user,
                    )
                )
            ).all()
        )
    recommendations: dict[uuid.UUID, IncrementRecommendation] = {}
    promoted: set[uuid.UUID] = set()
    if ids:
        recommendations = {
            r.employee_id: r
            for r in (
                await session.scalars(
                    tenant_scoped(
                        select(IncrementRecommendation).where(
                            IncrementRecommendation.cycle_id == cycle.id,
                            IncrementRecommendation.employee_id.in_(ids),
                        ),
                        IncrementRecommendation,
                        user,
                    )
                )
            ).all()
        }
        rec_ids = [r.id for r in recommendations.values()]
        if rec_ids:
            promoted = set(
                (
                    await session.scalars(
                        tenant_scoped(
                            select(PromotionRecord.recommendation_id).where(
                                PromotionRecord.recommendation_id.in_(rec_ids)
                            ),
                            PromotionRecord,
                            user,
                        )
                    )
                ).all()
            )
    bands = {
        b.rating_label: b
        for b in (
            await session.scalars(
                tenant_scoped(
                    select(IncrementMatrixRow).where(IncrementMatrixRow.cycle_id == cycle.id),
                    IncrementMatrixRow,
                    user,
                )
            )
        ).all()
    }
    department_ids = {e.department_id for e in employees if e.department_id}
    departments: dict[uuid.UUID, str] = {}
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

    rows = []
    for e in employees:
        rating = ratings.get(e.id)
        band = bands.get(rating) if rating else None
        rec = recommendations.get(e.id)
        rows.append(
            {
                "employee_id": str(e.id),
                "name": e.full_name,
                "employee_code": e.employee_code,
                "grade": e.grade,
                "designation": e.designation,
                "department_id": str(e.department_id) if e.department_id else None,
                "department": departments.get(e.department_id),
                "current_ctc": str(ctcs[e.id]) if e.id in ctcs else None,
                "rating_label": rating,
                "band": (
                    {
                        "min": str(band.min_percentage),
                        "max": str(band.max_percentage),
                        "recommended": str(band.recommended_percentage),
                    }
                    if band
                    else None
                ),
                "recommendation": (
                    {
                        "id": str(rec.id),
                        "status": rec.status.value,
                        "rating_label": rec.rating_label,
                        "percentage": str(rec.recommended_percentage),
                        "recommended_ctc": str(rec.recommended_ctc),
                        "increase_amount": str(rec.increase_amount),
                        "within_band": rec.within_band,
                        "justification": rec.justification,
                        "requires_escalation": rec.requires_escalation,
                        "overage_amount": str(rec.overage_amount),
                        "promotion_to_grade": rec.promotion_to_grade,
                        "promotion_applied": rec.id in promoted,
                        "equity": redact_equity(rec.equity_snapshot or {}, is_hr=is_hr),
                    }
                    if rec
                    else None
                ),
                "is_self": e.id == user.employee_id,
                "can_recommend": e.id != user.employee_id
                and (
                    rec is None
                    or rec.status
                    in (
                        RecommendationStatusEnum.SUBMITTED,
                        RecommendationStatusEnum.ESCALATED,
                        RecommendationStatusEnum.DRAFT,
                    )
                ),
                "can_decide": is_hr
                and e.id != user.employee_id
                and rec is not None
                and rec.status
                in (RecommendationStatusEnum.SUBMITTED, RecommendationStatusEnum.ESCALATED),
                "can_promote": is_hr
                and e.id != user.employee_id
                and rec is not None
                and rec.status is RecommendationStatusEnum.APPROVED
                and bool(rec.promotion_to_grade)
                and rec.id not in promoted,
            }
        )

    department_budgets = []
    for department_id in sorted({e.department_id for e in employees}, key=str):
        view = await performance.budget_view(
            session, user, cycle_id=cycle.id, department_id=department_id
        )
        department_budgets.append(
            {
                "department_id": str(department_id) if department_id else None,
                "department": departments.get(department_id) if department_id else "No department",
                **view,
            }
        )
    return {"rows": rows, "departments": department_budgets}
