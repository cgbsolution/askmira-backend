"""Retention and deletion endpoints (slice 26).

Two audiences:

- **`/retention/...`** — configuration and execution. HR and admin, and the
  destructive ones are admin only.
- **`/retention/trust-centre`** — readable by any authenticated user. An
  employee asking "what happens to my data when I leave" should not have to go
  through their HR team to find out.

Nothing here executes anything without going through the planner: a plan is
produced, read, approved if it is large, and only then acted on.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.retention import (
    LegalHold,
    RetentionActionKind,
    RetentionActionLog,
    RetentionRun,
)
from app.models.user import UserRole
from app.services.retention import anonymise as anonymise_engine
from app.services.retention import planner as planner_engine
from app.services.retention import service as retention_service
from app.services.retention.taxonomy import trust_centre_taxonomy

router = APIRouter(tags=["retention"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


# ── Contracts ───────────────────────────────────────────────────────────────


class PolicyIn(BaseModel):
    data_category_key: str = Field(min_length=1, max_length=64)
    retention_period_months: int = Field(ge=1)
    action: RetentionActionKind | None = None
    active: bool = True


class HoldIn(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
    #: Empty means all. A hold with every dimension empty freezes the whole
    #: tenant, which is a legitimate instruction and is treated as the broadest
    #: hold rather than as no hold.
    employee_ids: list[uuid.UUID] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    from_date: date | None = None
    to_date: date | None = None


class HoldReleaseIn(BaseModel):
    release_reason: str = Field(min_length=1, max_length=4000)


class CandidateIn(BaseModel):
    record_id: str
    table: str
    data_category_key: str
    subject_id: str
    record_created: date
    employment_ended: date | None = None
    last_accessed: date | None = None
    department: str | None = None


class PlanIn(BaseModel):
    candidates: list[CandidateIn] = Field(default_factory=list)
    today: date | None = None
    approval_threshold: int = Field(
        default=planner_engine.DEFAULT_APPROVAL_THRESHOLD, ge=1
    )
    #: Defaults to True. A dry run is the default posture here, not a debug
    #: flag — asking for a destructive run has to be a deliberate `false`.
    dry_run: bool = True


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class KAnonymityIn(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    k: int = Field(default=anonymise_engine.DEFAULT_K, ge=2)
    quasi_identifiers: list[str] = Field(default_factory=list)


# ── The taxonomy ────────────────────────────────────────────────────────────


@router.get("/retention/taxonomy")
async def taxonomy(user: CurrentUser) -> dict[str, Any]:
    """How the DPDP-versus-statute conflict is resolved, category by category.

    Not role-gated. This is the answer to "what happens to my data", and routing
    an employee's question about their own record through their HR team is a way
    of not answering it.
    """
    return trust_centre_taxonomy()


@router.get("/retention/trust-centre")
async def trust_centre(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """The taxonomy plus this tenant's own policies, holds and backup position."""
    return await retention_service.trust_centre(session, user)


# ── Policies ────────────────────────────────────────────────────────────────


@router.get("/retention/policies")
async def list_policies(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Configured policies, and the categories with none.

    The second list is the one worth reading: a category with no policy is
    retained forever, and a page showing only what is configured would present
    that as nothing to do.
    """
    return await retention_service.list_policies(session, user)


@router.put("/retention/policies")
async def set_policy(
    payload: PolicyIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Set one policy. Below the statutory floor is refused, with the statute named."""
    row = await retention_service.set_policy(
        session,
        user,
        data_category_key=payload.data_category_key,
        retention_period_months=payload.retention_period_months,
        action=payload.action,
        active=payload.active,
        request=request,
    )
    return {
        "id": str(row.id),
        "category": row.data_category_key,
        "retention_period_months": row.retention_period_months,
        "statutory_minimum_months": row.statutory_minimum_months,
        "action": row.action.value,
    }


@router.post("/retention/policies/seed", status_code=status.HTTP_201_CREATED)
async def seed_policies(
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Write the system defaults — the statutory floor exactly — for what is unset."""
    written = await retention_service.seed_default_policies(
        session, user, request=request
    )
    return {"created": len(written)}


# ── Legal holds ─────────────────────────────────────────────────────────────


@router.post("/retention/legal-holds", status_code=status.HTTP_201_CREATED)
async def place_hold(
    payload: HoldIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Freeze a scope of records against every retention policy."""
    row = await retention_service.place_hold(
        session,
        user,
        reason=payload.reason,
        employee_ids=payload.employee_ids,
        departments=payload.departments,
        from_date=payload.from_date,
        to_date=payload.to_date,
        request=request,
    )
    return {"id": str(row.id), "placed_at": row.placed_at.isoformat()}


@router.get("/retention/legal-holds")
async def list_holds(
    session: DbSession,
    user: CurrentUser,
    include_released: bool = False,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    stmt = tenant_scoped(select(LegalHold), LegalHold, user)
    if not include_released:
        stmt = stmt.where(LegalHold.released_at.is_(None))
    rows = (
        (await session.execute(stmt.order_by(LegalHold.placed_at.desc())))
        .scalars()
        .all()
    )
    return {
        "holds": [
            {
                "id": str(row.id),
                "reason": row.reason,
                "employee_count": len(row.employee_ids or []) or "all",
                "departments": row.departments,
                "from_date": row.from_date.isoformat() if row.from_date else None,
                "to_date": row.to_date.isoformat() if row.to_date else None,
                "placed_at": row.placed_at.isoformat(),
                "released_at": (
                    row.released_at.isoformat() if row.released_at else None
                ),
                "release_reason": row.release_reason,
                "is_active": row.is_active,
            }
            for row in rows
        ]
    }


@router.post("/retention/legal-holds/{hold_id}/release")
async def release_hold(
    hold_id: uuid.UUID,
    payload: HoldReleaseIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Release a hold. Records the release; never deletes the hold row."""
    row = await retention_service.release_hold(
        session,
        user,
        hold_id=hold_id,
        release_reason=payload.release_reason,
        request=request,
    )
    return {"id": str(row.id), "released_at": row.released_at.isoformat()}


# ── Planning and execution ──────────────────────────────────────────────────


@router.post("/retention/runs", status_code=status.HTTP_201_CREATED)
async def plan_run(
    payload: PlanIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Evaluate the policies and record what would happen. Changes no records."""
    row = await retention_service.plan_run(
        session,
        user,
        candidates=[
            planner_engine.CandidateRecord(
                record_id=c.record_id,
                table=c.table,
                data_category_key=c.data_category_key,
                subject_id=c.subject_id,
                record_created=c.record_created,
                employment_ended=c.employment_ended,
                last_accessed=c.last_accessed,
                department=c.department,
            )
            for c in payload.candidates
        ],
        today=payload.today,
        approval_threshold=payload.approval_threshold,
        is_dry_run=payload.dry_run,
        request=request,
    )
    return {
        "id": str(row.id),
        "status": row.status.value,
        "records_evaluated": row.records_evaluated,
        "records_to_act": row.records_to_act,
        "records_held": row.records_held,
        "records_blocked": row.records_blocked,
        "counts_by_action": row.counts_by_action,
        "plan": retention_service.public_snapshot(row.plan_snapshot),
    }


class SweepIn(BaseModel):
    today: date | None = None
    approval_threshold: int = Field(
        default=planner_engine.DEFAULT_APPROVAL_THRESHOLD, ge=1
    )


@router.post("/retention/sweep/preview", status_code=status.HTTP_201_CREATED)
async def preview_sweep(
    payload: SweepIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """A dry run over the records this workspace actually holds. Changes nothing.

    Candidates are gathered from every classified table with an active policy,
    so the preview is what tonight's sweep would do, not a hand-picked sample.
    """
    as_of = payload.today or date.today()
    candidates = await retention_service.gather_candidates(session, user, today=as_of)
    row = await retention_service.plan_run(
        session,
        user,
        candidates=candidates,
        today=as_of,
        approval_threshold=payload.approval_threshold,
        is_dry_run=True,
        request=request,
    )
    return {
        "id": str(row.id),
        "status": row.status.value,
        "candidates": len(candidates),
        "records_to_act": row.records_to_act,
        "records_held": row.records_held,
        "records_blocked": row.records_blocked,
        "counts_by_action": row.counts_by_action,
        "plan": retention_service.public_snapshot(row.plan_snapshot),
    }


@router.post("/retention/sweep/plan", status_code=status.HTTP_201_CREATED)
async def plan_sweep(
    payload: SweepIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Plan a real run over the workspace's records.

    Nothing is deleted yet: a run under the threshold is ready to execute, and a
    larger one waits for somebody else's approval.
    """
    as_of = payload.today or date.today()
    candidates = await retention_service.gather_candidates(session, user, today=as_of)
    row = await retention_service.plan_run(
        session,
        user,
        candidates=candidates,
        today=as_of,
        approval_threshold=payload.approval_threshold,
        is_dry_run=False,
        request=request,
    )
    return {
        "id": str(row.id),
        "status": row.status.value,
        "candidates": len(candidates),
        "records_to_act": row.records_to_act,
        "records_held": row.records_held,
        "counts_by_action": row.counts_by_action,
        "plan": retention_service.public_snapshot(row.plan_snapshot),
    }


@router.post("/retention/suppressions/apply")
async def apply_suppressions(
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Remove any erased record that has reappeared — after a backup restore."""
    return await retention_service.apply_suppressions(
        session, user, today=date.today(), request=request
    )


@router.get("/retention/runs")
async def list_runs(
    session: DbSession,
    user: CurrentUser,
    limit: int = 50,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    stmt = (
        tenant_scoped(select(RetentionRun), RetentionRun, user)
        .order_by(RetentionRun.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "runs": [
            {
                "id": str(row.id),
                "evaluated_on": row.evaluated_on.isoformat(),
                "status": row.status.value,
                "records_to_act": row.records_to_act,
                "records_held": row.records_held,
                "records_blocked": row.records_blocked,
                "counts_by_action": row.counts_by_action,
                "approved_by_user_id": (
                    str(row.approved_by_user_id)
                    if row.approved_by_user_id
                    else None
                ),
                "executed_at": (
                    row.executed_at.isoformat() if row.executed_at else None
                ),
            }
            for row in rows
        ]
    }


@router.post("/retention/runs/{run_id}/approve")
async def approve_run(
    run_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Authorise a run above the volume threshold."""
    row = await retention_service.approve_run(
        session, user, run_id=run_id, request=request
    )
    return {"id": str(row.id), "status": row.status.value}


@router.post("/retention/runs/{run_id}/reject")
async def reject_run(
    run_id: uuid.UUID,
    payload: RejectIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await retention_service.reject_run(
        session, user, run_id=run_id, reason=payload.reason, request=request
    )
    return {"id": str(row.id), "status": row.status.value}


@router.post("/retention/runs/{run_id}/execute")
async def execute_run(
    run_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Execute an approved run.

    Deletes or anonymises each planned record, re-checking legal holds first,
    and reports the real counts — `records_actually_removed` is what was removed.
    """
    return await retention_service.execute_plan(
        session, user, run_id=run_id, request=request
    )


@router.get("/retention/action-log")
async def action_log(
    session: DbSession,
    user: CurrentUser,
    limit: int = 100,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """What was executed, in what volume, by whose authority. Insert-only.

    Sample record ids are hashed. A log of what was deleted that contained
    readable identifiers of the deleted would be a copy of the thing deleted.
    """
    stmt = (
        tenant_scoped(select(RetentionActionLog), RetentionActionLog, user)
        .order_by(RetentionActionLog.executed_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "entries": [
            {
                "run_id": str(row.run_id),
                "category": row.data_category_key,
                "record_type": row.record_type,
                "action": row.action.value,
                "record_count": row.record_count,
                "executed_at": row.executed_at.isoformat(),
                "approved_by_user_id": (
                    str(row.approved_by_user_id)
                    if row.approved_by_user_id
                    else None
                ),
                "sample_record_hashes": row.sample_record_hashes,
            }
            for row in rows
        ]
    }


# ── Anonymisation and suppression ───────────────────────────────────────────


@router.post("/retention/k-anonymity")
async def check_k_anonymity(
    payload: KAnonymityIn,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Whether a set of anonymised records is actually anonymous.

    A record alone in its combination of quasi-identifiers identifies somebody
    as surely as their name did, and this is the check that says so.
    """
    report = anonymise_engine.check_k_anonymity(
        payload.records,
        k=payload.k,
        quasi_identifiers=tuple(payload.quasi_identifiers),
    )
    return report.as_dict()


@router.get("/retention/suppressions")
async def suppressions(
    session: DbSession,
    user: CurrentUser,
    today: date | None = None,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """The list a backup restore would have to apply, and the honest note on it."""
    return await retention_service.suppression_list(session, user, today=today)
