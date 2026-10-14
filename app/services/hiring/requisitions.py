"""Requisitions: raising, editing, approval, budget checks and vacancy accounting.

Four decisions worth stating.

**The budget breach is a headline, not a field.** Approvers approve — that is
what the button says and what the day is full of. A requisition that exceeds
approved headcount or its CTC band puts a written flag at the top of the
approval payload and records, on the approver's own row, that it was shown to
them. Salary creep happens one individually defensible approval at a time.

**The chain comes from the decision.** A junior backfill and a senior new
position do not need the same signatures. Resolved from grade and CTC band, and
snapshotted onto the requisition so a later change to the rules cannot rewrite
who was asked.

**Nobody signs their own.** The person who raised a requisition never approves
it, and a step is signed only by somebody holding the role it names — see
`approvers.can_decide_step`. Before this the route let any manager decide any
step, which made the chain a list of names rather than a list of signatures.

**Vacancies are accounted, not inferred.** `filled_count` moves when an offer
takes a seat and moves back when one is revoked, declined, lapsed or dropped.
Counting joined employees instead would show a requisition as open for the
three months between a release and a joining, and as filled forever after a
revocation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.hiring import Candidate, Requisition, RequisitionStatus
from app.models.offer import (
    ApprovalChainRule,
    ApprovalDecision,
    ApprovalScope,
    Offer,
    OfferApproval,
    OfferStatus,
    RequisitionApproval,
)
from app.models.org import Department, Location
from app.models.user import User
from app.schemas.hiring import RequisitionCreate, RequisitionUpdate
from app.services import audit
from app.services.hiring import approval_chain, approvers, visibility
from app.services.positions import service as position_service

__all__ = [
    "approve_requisition",
    "budget_flags",
    "change_status",
    "create_requisition",
    "list_requisitions",
    "load_rules",
    "pending_decisions",
    "release_vacancy",
    "requisition_detail",
    "reserve_vacancy",
    "submit_requisition",
    "update_requisition",
]


def _money(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def load_rules(
    session: AsyncSession, user: User, scope: ApprovalScope
) -> list[approval_chain.RuleSpec]:
    """The active chain rules for one scope, as engine specs."""
    rows = await session.scalars(
        tenant_scoped(
            select(ApprovalChainRule).where(
                ApprovalChainRule.scope == scope,
                ApprovalChainRule.is_active.is_(True),
            ),
            ApprovalChainRule,
            user,
        )
    )
    return [
        approval_chain.RuleSpec(
            rule_id=str(row.id),
            sequence=row.sequence,
            approver_role=row.approver_role,
            ctc_min=Decimal(str(row.ctc_min)),
            ctc_max=None if row.ctc_max is None else Decimal(str(row.ctc_max)),
            grades=tuple(row.grades or ()),
            only_when_over_band=row.only_when_over_band,
            label=row.label or "",
        )
        for row in rows
    ]


def budget_flags(requisition: Requisition, *, live_headcount: int | None = None) -> list[dict]:
    """What the approver has to be told, in words, before they sign.

    Returned as a list rather than a boolean because an approver needs to know
    *which* budget this breaks. Empty means nothing to flag; a caller cannot
    mistake that for a falsy zero.
    """
    flags: list[dict] = []

    approved = requisition.approved_headcount
    if approved is not None:
        # Position management (slice 23) is configured. `live_headcount` is what
        # is already on the books for this position; the requisition adds to it.
        existing = live_headcount or 0
        requested = existing + requisition.openings
        if requested > approved:
            flags.append(
                {
                    "kind": "headcount",
                    "severity": "blocking_attention",
                    "message": (
                        f"This requisition takes the position to {requested} people "
                        f"against an approved headcount of {approved}. Approving it "
                        f"adds {requested - approved} unbudgeted "
                        f"{'seat' if requested - approved == 1 else 'seats'}."
                    ),
                }
            )
    else:
        flags.append(
            {
                "kind": "headcount_unknown",
                "severity": "informational",
                "message": (
                    "No approved headcount is configured for this position, so this "
                    "requisition has not been checked against one. It is being "
                    "approved on the CTC band alone."
                ),
            }
        )

    band_min = _money(requisition.budget_ctc_min)
    band_max = _money(requisition.budget_ctc_max)
    if band_max is None:
        flags.append(
            {
                "kind": "no_ctc_band",
                "severity": "informational",
                "message": (
                    "This requisition has no budgeted CTC ceiling, so no offer against "
                    "it can ever be flagged as over band."
                ),
            }
        )
    elif band_min is not None and band_min > band_max:
        flags.append(
            {
                "kind": "inverted_band",
                "severity": "blocking_attention",
                "message": (
                    f"The budgeted band runs from {band_min} down to {band_max}. No "
                    "offer can sit inside it."
                ),
            }
        )

    return flags


# ═══════════════════════════════════════════════════════════════════════════
# Raising and editing
# ═══════════════════════════════════════════════════════════════════════════


async def _check_references(
    session: AsyncSession,
    user: User,
    *,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    employee_ids: tuple[uuid.UUID | None, ...] = (),
) -> None:
    """Every id in the body must belong to this tenant.

    A foreign key to another company's department is accepted by the database
    and quietly links two tenants' records.
    """
    if department_id is not None and await session.scalar(
        tenant_scoped(select(Department.id).where(Department.id == department_id), Department, user)
    ) is None:
        raise _unprocessable("That department is not in this workspace.")
    if location_id is not None and await session.scalar(
        tenant_scoped(select(Location.id).where(Location.id == location_id), Location, user)
    ) is None:
        raise _unprocessable("That location is not in this workspace.")
    for employee_id in employee_ids:
        if employee_id is not None and await session.scalar(
            tenant_scoped(select(Employee.id).where(Employee.id == employee_id), Employee, user)
        ) is None:
            raise _unprocessable("A person named on this requisition is not in this workspace.")


async def _next_code(session: AsyncSession, user: User) -> str:
    year = datetime.now(UTC).year
    count = await session.scalar(
        tenant_scoped(select(func.count()).select_from(Requisition), Requisition, user)
    )
    number = int(count or 0) + 1
    while True:
        code = f"REQ-{year}-{number:04d}"
        taken = await session.scalar(
            tenant_scoped(select(Requisition.id).where(Requisition.code == code), Requisition, user)
        )
        if taken is None:
            return code
        number += 1


async def create_requisition(
    session: AsyncSession,
    user: User,
    payload: RequisitionCreate,
    *,
    request: Request | None = None,
) -> Requisition:
    """Raise a draft requisition.

    A manager raises for their own team: the hiring manager is them, and naming
    somebody else is refused rather than silently replaced. HR may raise for
    anyone.
    """
    hiring_manager_id = payload.hiring_manager_id
    if not permissions.is_hr(user):
        me = permissions.own_employee_id(user)
        if hiring_manager_id is not None and hiring_manager_id != me:
            raise permissions.forbidden(
                "A manager raises requisitions for their own team. Ask HR to raise one "
                "with somebody else as the hiring manager."
            )
        hiring_manager_id = me

    await _check_references(
        session,
        user,
        department_id=payload.department_id,
        location_id=payload.location_id,
        employee_ids=(
            hiring_manager_id,
            payload.recruiter_id,
            payload.replacement_for_employee_id,
        ),
    )

    if payload.code:
        taken = await session.scalar(
            tenant_scoped(
                select(Requisition.id).where(Requisition.code == payload.code),
                Requisition,
                user,
            )
        )
        if taken is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"The code {payload.code} is already used by another requisition.",
            )
    code = payload.code or await _next_code(session, user)

    requisition = Requisition(
        tenant_id=user.tenant_id,
        code=code,
        title=payload.title.strip(),
        department_id=payload.department_id,
        location_id=payload.location_id,
        hiring_manager_id=hiring_manager_id,
        recruiter_id=payload.recruiter_id,
        replacement_for_employee_id=payload.replacement_for_employee_id,
        openings=payload.openings,
        employment_type=payload.employment_type,
        grade=payload.grade,
        experience_min_years=payload.experience_min_years,
        experience_max_years=payload.experience_max_years,
        budget_ctc_min=payload.budget_ctc_min,
        budget_ctc_max=payload.budget_ctc_max,
        justification=payload.justification,
        description=payload.description,
        skills=list(payload.skills),
        jd_versions=[],
        target_joining_date=payload.target_joining_date,
        target_close_date=payload.target_close_date,
        status=RequisitionStatus.DRAFT,
        raised_by_user_id=user.id,
    )
    session.add(requisition)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=f"Requisition {requisition.code} raised as a draft: {requisition.title}",
        changes={"openings": requisition.openings, "grade": requisition.grade},
        request=request,
    )
    return requisition


_EDITABLE = (RequisitionStatus.DRAFT, RequisitionStatus.REJECTED)


async def update_requisition(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID,
    payload: RequisitionUpdate,
    request: Request | None = None,
) -> Requisition:
    """Edit a draft or a rejected requisition.

    Once it is with approvers the terms are what they are deciding on; editing
    under them would reuse their attention on numbers they never saw.
    """
    requisition = await visibility.load_requisition(session, user, requisition_id)
    if requisition.status not in _EDITABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This requisition is {requisition.status.value.replace('_', ' ')}. Only a "
                "draft or a rejected requisition can be edited."
            ),
        )

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise _unprocessable("Nothing to change.")
    if "title" in changes and changes["title"] is None:
        raise _unprocessable("A requisition needs a title.")
    if "openings" in changes and changes["openings"] is None:
        raise _unprocessable("A requisition needs at least one opening.")
    if "employment_type" in changes and changes["employment_type"] is None:
        changes.pop("employment_type")
    if "description_ai_assisted" in changes and changes["description_ai_assisted"] is None:
        changes.pop("description_ai_assisted")
    if "description" in changes and "description_ai_assisted" not in changes:
        # Edited by hand without saying it came from a draft: a person's words.
        changes["description_ai_assisted"] = False
    if "skills" in changes:
        changes["skills"] = list(
            dict.fromkeys(s.strip()[:64] for s in (changes["skills"] or []) if s and s.strip())
        )

    if (
        "hiring_manager_id" in changes
        and not permissions.is_hr(user)
        and changes["hiring_manager_id"] != permissions.own_employee_id(user)
    ):
        raise permissions.forbidden(
            "Only HR can make somebody else the hiring manager on a requisition."
        )

    await _check_references(
        session,
        user,
        department_id=changes.get("department_id"),
        location_id=changes.get("location_id"),
        employee_ids=(
            changes.get("hiring_manager_id"),
            changes.get("recruiter_id"),
            changes.get("replacement_for_employee_id"),
        ),
    )

    exp_min = changes.get("experience_min_years", requisition.experience_min_years)
    exp_max = changes.get("experience_max_years", requisition.experience_max_years)
    if exp_min is not None and exp_max is not None and exp_min > exp_max:
        raise _unprocessable("Minimum experience is above the maximum.")
    band_min = _money(changes.get("budget_ctc_min", requisition.budget_ctc_min))
    band_max = _money(changes.get("budget_ctc_max", requisition.budget_ctc_max))
    if band_min is not None and band_max is not None and band_min > band_max:
        raise _unprocessable("The budget floor is above the ceiling; no offer could fit it.")

    for field, value in changes.items():
        setattr(requisition, field, value)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=f"Requisition {requisition.code} edited: {', '.join(sorted(changes))}",
        changes={"fields": sorted(changes)},
        request=request,
    )
    return requisition


def publish_jd(requisition: Requisition, *, now: datetime) -> bool:
    """Record the description candidates will apply against, as a new version.

    FS-HIR-01: a published JD is versioned, because a candidate applied against
    a specific wording and that wording has to stay retrievable. Called when a
    requisition opens or reopens; a description unchanged since the last
    version adds nothing.
    """
    body = (requisition.description or "").strip()
    versions = list(requisition.jd_versions or [])
    if not body or (versions and versions[-1].get("body") == body):
        return False
    versions.append(
        {
            "version": len(versions) + 1,
            "body": body,
            "published_at": now.isoformat(),
            "ai_generated": bool(requisition.description_ai_assisted),
        }
    )
    # Reassigned rather than appended in place, so the JSONB change is seen.
    requisition.jd_versions = versions
    return True


_STATUS_MOVES: dict[RequisitionStatus, frozenset[RequisitionStatus]] = {
    RequisitionStatus.OPEN: frozenset({RequisitionStatus.ON_HOLD, RequisitionStatus.CLOSED}),
    RequisitionStatus.ON_HOLD: frozenset({RequisitionStatus.OPEN, RequisitionStatus.CLOSED}),
    RequisitionStatus.DRAFT: frozenset({RequisitionStatus.CANCELLED}),
    RequisitionStatus.REJECTED: frozenset({RequisitionStatus.CANCELLED}),
    RequisitionStatus.PENDING_APPROVAL: frozenset({RequisitionStatus.CANCELLED}),
    RequisitionStatus.FULFILLED: frozenset({RequisitionStatus.CLOSED}),
}


async def change_status(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID,
    to_status: RequisitionStatus,
    reason: str,
    request: Request | None = None,
) -> Requisition:
    """Hold, reopen, close or cancel, with a reason.

    Opening is not here: a requisition opens by being approved. Reopening one
    that was put on hold is.
    """
    requisition = await visibility.load_requisition(session, user, requisition_id)
    allowed = _STATUS_MOVES.get(requisition.status, frozenset())
    if to_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A {requisition.status.value.replace('_', ' ')} requisition cannot be "
                f"moved to {to_status.value.replace('_', ' ')}."
            ),
        )
    if not reason.strip():
        raise _unprocessable("A status change needs a reason.")

    previous = requisition.status
    requisition.status = to_status
    if to_status is RequisitionStatus.CLOSED:
        requisition.closed_at = datetime.now(UTC)
    if to_status is RequisitionStatus.OPEN:
        publish_jd(requisition, now=datetime.now(UTC))
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=(
            f"Requisition {requisition.code} moved from {previous.value} to "
            f"{to_status.value}: {reason.strip()}"
        ),
        request=request,
    )
    return requisition


# ═══════════════════════════════════════════════════════════════════════════
# Reading
# ═══════════════════════════════════════════════════════════════════════════


async def list_requisitions(
    session: AsyncSession,
    user: User,
    *,
    requisition_status: RequisitionStatus | None = None,
    mine: bool = False,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[Requisition], int]:
    """The requisitions this caller may list, newest first, with the real total."""
    stmt = visibility.visible_requisitions(user)
    if mine:
        stmt = stmt.where(Requisition.id.in_(visibility.team_requisition_ids(user)))
    if requisition_status is not None:
        stmt = stmt.where(Requisition.status == requisition_status)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await session.scalars(
            stmt.order_by(Requisition.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return list(rows), int(total or 0)


def _person_name(employee: Employee | None) -> str | None:
    if employee is None:
        return None
    return employee.display_name or " ".join(
        part for part in (employee.first_name, employee.last_name) if part
    )


async def _employee(
    session: AsyncSession, user: User, employee_id: uuid.UUID | None
) -> Employee | None:
    if employee_id is None:
        return None
    return await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )


async def _user_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids)), User, user)
    )
    return {row.id: row.full_name for row in rows}


async def _steps(
    session: AsyncSession, user: User, requisition_id: uuid.UUID
) -> list[RequisitionApproval]:
    rows = await session.scalars(
        tenant_scoped(
            select(RequisitionApproval)
            .where(RequisitionApproval.requisition_id == requisition_id)
            .order_by(RequisitionApproval.sequence),
            RequisitionApproval,
            user,
        )
    )
    return list(rows.all())


def _authority_for(
    user: User, requisition: Requisition, step: RequisitionApproval
) -> approvers.StepAuthority:
    return approvers.can_decide_step(
        approver_role=step.approver_role,
        user_role=user.role.value,
        has_reports=user.has_reports,
        is_hiring_manager=(
            user.employee_id is not None and user.employee_id == requisition.hiring_manager_id
        ),
        is_originator=(
            requisition.raised_by_user_id is not None
            and requisition.raised_by_user_id == user.id
        ),
    )


async def requisition_detail(
    session: AsyncSession, user: User, *, requisition_id: uuid.UUID
) -> dict[str, Any]:
    """Everything the requisition screen needs, including what this caller may do.

    Visible to HR and the hiring team. A manager can also open a requisition
    whose current approval step they are eligible to sign — they cannot decide
    something they are not allowed to read.
    """
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None:
        raise visibility.not_found("Requisition")

    steps = await _steps(session, user, requisition.id)
    pending = next((s for s in steps if s.decision is ApprovalDecision.PENDING), None)
    authority = (
        _authority_for(user, requisition, pending)
        if pending is not None and requisition.status is RequisitionStatus.PENDING_APPROVAL
        else None
    )
    on_team = permissions.is_hr(user) or visibility.is_hiring_team(user, requisition)
    if not on_team and not (authority is not None and authority.allowed):
        raise visibility.not_found("Requisition")

    names = await _user_names(
        session, user, {s.decided_by_user_id for s in steps if s.decided_by_user_id}
    )
    hiring_manager = await _employee(session, user, requisition.hiring_manager_id)
    recruiter = await _employee(session, user, requisition.recruiter_id)
    department = (
        await session.scalar(
            tenant_scoped(
                select(Department).where(Department.id == requisition.department_id),
                Department,
                user,
            )
        )
        if requisition.department_id
        else None
    )
    location = (
        await session.scalar(
            tenant_scoped(
                select(Location).where(Location.id == requisition.location_id), Location, user
            )
        )
        if requisition.location_id
        else None
    )
    candidate_count = await session.scalar(
        tenant_scoped(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.requisition_id == requisition.id),
            Candidate,
            user,
        )
    )

    editable = requisition.status in _EDITABLE and on_team
    return {
        "id": requisition.id,
        "code": requisition.code,
        "title": requisition.title,
        "department_id": requisition.department_id,
        "location_id": requisition.location_id,
        "hiring_manager_id": requisition.hiring_manager_id,
        "recruiter_id": requisition.recruiter_id,
        "openings": requisition.openings,
        "open_vacancies": requisition.open_vacancies,
        "employment_type": requisition.employment_type,
        "status": requisition.status,
        "target_close_date": requisition.target_close_date,
        "created_at": requisition.created_at,
        "grade": requisition.grade,
        "experience_min_years": requisition.experience_min_years,
        "experience_max_years": requisition.experience_max_years,
        "budget_ctc_min": _money(requisition.budget_ctc_min),
        "budget_ctc_max": _money(requisition.budget_ctc_max),
        "justification": requisition.justification,
        "description": requisition.description,
        "description_ai_assisted": requisition.description_ai_assisted,
        "jd_versions": [
            {
                "version": v.get("version"),
                "published_at": v.get("published_at"),
                "ai_generated": bool(v.get("ai_generated")),
            }
            for v in (requisition.jd_versions or [])
        ],
        "skills": list(requisition.skills or []),
        "replacement_for_employee_id": requisition.replacement_for_employee_id,
        "target_joining_date": requisition.target_joining_date,
        "raised_by_user_id": requisition.raised_by_user_id,
        "approved_at": requisition.approved_at,
        "filled_count": requisition.filled_count,
        "approved_headcount": requisition.approved_headcount,
        "hiring_manager_name": _person_name(hiring_manager),
        "recruiter_name": _person_name(recruiter),
        "department_name": department.name if department else None,
        "location_name": location.name if location else None,
        "approvals": [
            {
                "sequence": s.sequence,
                "role": s.approver_role,
                "label": s.label,
                "reason": s.reason,
                "decision": s.decision.value,
                "decided_by": names.get(s.decided_by_user_id) if s.decided_by_user_id else None,
                "decided_at": s.decided_at,
                "note": s.note,
                "flags_shown": list(s.flags_shown or []),
            }
            for s in steps
        ],
        # What the approvers are shown: the snapshot taken at submission, or
        # the live check for a draft that has not gone yet.
        "budget_flags": list(steps[0].flags_shown or []) if steps else budget_flags(requisition),
        "can_edit": editable,
        "can_submit": editable,
        "can_decide": bool(authority and authority.allowed),
        "decide_blocked_reason": (
            authority.message if authority is not None and not authority.allowed else None
        ),
        "candidate_count": int(candidate_count or 0),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Approval
# ═══════════════════════════════════════════════════════════════════════════


async def submit_requisition(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID,
    live_headcount: int | None = None,
    request: Request | None = None,
) -> dict:
    """Send a requisition for approval, resolving and recording its chain."""
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")
    if not permissions.is_hr(user) and not visibility.is_hiring_team(user, requisition):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")

    if requisition.status not in (RequisitionStatus.DRAFT, RequisitionStatus.REJECTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This requisition is {requisition.status.value}, not a draft. Only a "
                "draft or a rejected requisition can be sent for approval."
            ),
        )
    if not requisition.justification:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A requisition needs a justification. An approver deciding on a "
                "headcount request with no stated reason is signing a number."
            ),
        )

    rules = await load_rules(session, user, ApprovalScope.REQUISITION)
    try:
        approval_chain.validate_rules(rules)
        # A requisition is approved on its ceiling, since that is the most it
        # can cost. Approving on the floor would route the expensive half of
        # every band to whoever signs the cheap end.
        ceiling = _money(requisition.budget_ctc_max) or Decimal(0)
        chain = approval_chain.resolve_chain(
            rules, ctc_annual=ceiling, grade=requisition.grade, over_band=False
        )
    except approval_chain.ApprovalChainError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    flags = budget_flags(requisition, live_headcount=live_headcount)
    # Position management (slice 23) has its own view of whether this seat is
    # approved, frozen, already filled, or budgeted below what is being asked
    # for. Asked here rather than duplicated, so the two modules cannot disagree.
    flags.extend(
        await position_service.off_budget_flags(
            session,
            user,
            position_id=requisition.position_id,
            department_id=requisition.department_id,
            requested_ctc_max=_money(requisition.budget_ctc_max),
        )
    )

    existing = await session.scalars(
        tenant_scoped(
            select(RequisitionApproval).where(
                RequisitionApproval.requisition_id == requisition.id
            ),
            RequisitionApproval,
            user,
        )
    )
    for row in existing.all():
        await session.delete(row)
    await session.flush()

    for step in chain:
        session.add(
            RequisitionApproval(
                tenant_id=user.tenant_id,
                requisition_id=requisition.id,
                sequence=step.sequence,
                approver_role=step.approver_role,
                label=step.label,
                reason=step.reason,
                # Snapshotted on every step, so "did they see the headcount
                # breach" is answerable about each approver individually.
                flags_shown=flags,
            )
        )

    requisition.status = RequisitionStatus.PENDING_APPROVAL
    # Whoever raised it stays the originator. A requisition drafted by a manager
    # and sent on by HR was still raised by the manager, who still cannot sign it.
    requisition.raised_by_user_id = requisition.raised_by_user_id or user.id
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=(
            f"Requisition {requisition.code} sent for approval to "
            f"{len(chain)} approver(s)"
            + (f" with {len(flags)} budget flag(s)" if flags else "")
        ),
        changes={"chain": [s.approver_role for s in chain], "flags": flags},
        request=request,
    )
    return {
        "requisition_id": str(requisition.id),
        "status": requisition.status.value,
        "chain": [
            {"sequence": s.sequence, "role": s.approver_role, "reason": s.reason}
            for s in chain
        ],
        "flags": flags,
    }


async def approve_requisition(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> dict:
    """Record one approver's decision.

    The first rejection ends it. Collecting the rest of the signatures on a
    requisition somebody has already refused wastes everyone's time and makes
    the refusal look negotiable.
    """
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")
    if requisition.status is not RequisitionStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This requisition is {requisition.status.value}, not awaiting approval.",
        )

    steps = await _steps(session, user, requisition.id)
    pending = next((s for s in steps if s.decision is ApprovalDecision.PENDING), None)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Every approval step on this requisition has already been decided.",
        )

    authority = _authority_for(user, requisition, pending)
    if not authority.allowed:
        raise permissions.forbidden(authority.message)
    if not approve and not (note or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A rejection needs a note. The person who raised it has to know what "
                "would change the answer."
            ),
        )

    pending.decision = (
        ApprovalDecision.APPROVED if approve else ApprovalDecision.REJECTED
    )
    pending.decided_by_user_id = user.id
    pending.decided_at = datetime.now(UTC)
    pending.note = (
        f"{note or ''} [signed on behalf of {authority.on_behalf_of}]".strip()
        if authority.on_behalf_of
        else note
    )

    if not approve:
        requisition.status = RequisitionStatus.REJECTED
        remaining = 0
    else:
        remaining = sum(
            1 for s in steps if s.decision is ApprovalDecision.PENDING
        )
        if remaining == 0:
            requisition.status = RequisitionStatus.OPEN
            requisition.approved_by_user_id = user.id
            requisition.approved_at = datetime.now(UTC)
            publish_jd(requisition, now=requisition.approved_at)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=(
            f"Requisition {requisition.code} "
            f"{'approved' if approve else 'rejected'} at step {pending.sequence} "
            f"({pending.approver_role})"
            + (f", signed on behalf of {authority.on_behalf_of}" if authority.on_behalf_of else "")
        ),
        changes={"note": note, "flags_shown": pending.flags_shown},
        request=request,
    )
    return {
        "requisition_id": str(requisition.id),
        "status": requisition.status.value,
        "step": pending.sequence,
        "remaining_steps": remaining,
    }


async def pending_decisions(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Requisitions and offers waiting on a step this caller may sign.

    Excludes anything they raised or drafted — it would only ever come back as a
    refusal.
    """
    items: list[dict[str, Any]] = []

    requisitions = (
        await session.scalars(
            tenant_scoped(
                select(Requisition)
                .where(Requisition.status == RequisitionStatus.PENDING_APPROVAL)
                .order_by(Requisition.updated_at),
                Requisition,
                user,
            )
        )
    ).all()
    for requisition in requisitions:
        steps = await _steps(session, user, requisition.id)
        pending = next((s for s in steps if s.decision is ApprovalDecision.PENDING), None)
        if pending is None or not _authority_for(user, requisition, pending).allowed:
            continue
        items.append(
            {
                "kind": "requisition",
                "id": requisition.id,
                "code": requisition.code,
                "title": requisition.title,
                "step": pending.sequence,
                "step_role": pending.approver_role,
                "step_reason": pending.reason,
                "flags": list(pending.flags_shown or []),
                "submitted_at": pending.created_at,
            }
        )

    offers = (
        await session.scalars(
            tenant_scoped(
                select(Offer)
                .where(Offer.status == OfferStatus.PENDING_APPROVAL)
                .order_by(Offer.updated_at),
                Offer,
                user,
            )
        )
    ).all()
    for offer in offers:
        requisition = await session.scalar(
            tenant_scoped(
                select(Requisition).where(Requisition.id == offer.requisition_id),
                Requisition,
                user,
            )
        )
        if requisition is None:
            continue
        offer_steps = (
            await session.scalars(
                tenant_scoped(
                    select(OfferApproval)
                    .where(OfferApproval.offer_id == offer.id)
                    .order_by(OfferApproval.sequence),
                    OfferApproval,
                    user,
                )
            )
        ).all()
        pending_offer = next(
            (
                s
                for s in offer_steps
                if s.decision in (ApprovalDecision.PENDING, ApprovalDecision.VOIDED)
            ),
            None,
        )
        if pending_offer is None:
            continue
        authority = approvers.can_decide_step(
            approver_role=pending_offer.approver_role,
            user_role=user.role.value,
            has_reports=user.has_reports,
            is_hiring_manager=(
                user.employee_id is not None
                and user.employee_id == requisition.hiring_manager_id
            ),
            is_originator=offer.created_by_user_id == user.id,
        )
        if not authority.allowed:
            continue
        deviation = (offer.approval_context or {}).get("budget_deviation") or {}
        items.append(
            {
                "kind": "offer",
                "id": offer.id,
                "code": offer.code,
                "title": offer.designation,
                "step": pending_offer.sequence,
                "step_role": pending_offer.approver_role,
                "step_reason": pending_offer.reason,
                "flags": (
                    [{"kind": "over_band", "severity": "blocking_attention",
                      "message": deviation.get("headline", "")}]
                    if deviation.get("is_over_band")
                    else []
                ),
                "submitted_at": pending_offer.created_at,
            }
        )
    return items


# ═══════════════════════════════════════════════════════════════════════════
# Vacancies
# ═══════════════════════════════════════════════════════════════════════════


async def reserve_vacancy(
    session: AsyncSession, user: User, *, requisition: Requisition
) -> None:
    """Take a seat for an offer being released.

    Refuses when the requisition is full. Two offers released against one
    opening is a real thing that happens under pressure, and it is much cheaper
    to catch here than after both are accepted.
    """
    if requisition.status not in (RequisitionStatus.OPEN, RequisitionStatus.FULFILLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Requisition {requisition.code} is {requisition.status.value}. An offer "
                "cannot be released against a requisition that is not open."
            ),
        )
    if requisition.filled_count >= requisition.openings:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Requisition {requisition.code} has {requisition.openings} opening(s), "
                f"all of them already held by a live offer or a joined employee. Raise "
                "the opening count or wait for one to be released."
            ),
        )
    requisition.filled_count += 1
    if requisition.filled_count >= requisition.openings:
        requisition.status = RequisitionStatus.FULFILLED
    await session.flush()


async def release_vacancy(
    session: AsyncSession, user: User, *, requisition: Requisition, reason: str
) -> None:
    """Put a seat back after a revocation, decline, lapse or drop.

    The requisition reopens. Leaving it FULFILLED would show the seat as filled
    while it is empty, which is how a team waits a quarter for a role nobody
    knows is vacant.
    """
    if requisition.filled_count > 0:
        requisition.filled_count -= 1
    if (
        requisition.filled_count < requisition.openings
        and requisition.status is RequisitionStatus.FULFILLED
    ):
        requisition.status = RequisitionStatus.OPEN
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=(
            f"Vacancy reopened on requisition {requisition.code}: {reason}. "
            f"{requisition.open_vacancies} of {requisition.openings} now open."
        ),
    )
