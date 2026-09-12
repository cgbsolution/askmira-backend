"""Hiring / ATS endpoints — the re-platformed AnytimeHire module (FRD 5.5).

Blocking gate (FS-HIR-02, M1): the Fit Score parity defect and the proctoring
badge miscalibration carried over from AnytimeHire must be fixed and
independently re-verified before this module appears in any customer-facing
demo. The regression test asserting that Fit Score varies with AI Round Score
belongs in `tests/` and is release-blocking.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, tenant_scoped
from app.models.hiring import Candidate, CandidateStage, Requisition, RequisitionStatus
from app.schemas.common import Page

router = APIRouter(prefix="/hiring", tags=["hiring"])


class RequisitionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    title: str
    department_id: uuid.UUID | None
    location_id: uuid.UUID | None
    hiring_manager_id: uuid.UUID | None
    openings: int
    status: RequisitionStatus
    target_close_date: date | None


class CandidateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requisition_id: uuid.UUID
    full_name: str
    email: str
    phone: str | None
    stage: CandidateStage
    source: str | None
    total_experience_years: float | None
    fit_score: float | None
    converted_employee_id: uuid.UUID | None


@router.get("/requisitions", response_model=Page[RequisitionRead])
async def list_requisitions(
    user: CurrentUser,
    session: DbSession,
    requisition_status: RequisitionStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[RequisitionRead]:
    stmt = tenant_scoped(select(Requisition), Requisition, user)
    if requisition_status:
        stmt = stmt.where(Requisition.status == requisition_status)

    rows = (
        await session.scalars(
            stmt.order_by(Requisition.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return Page[RequisitionRead](
        items=[RequisitionRead.model_validate(row) for row in rows],
        total=len(rows),
        page=page,
        page_size=page_size,
    )


@router.get("/candidates", response_model=Page[CandidateRead])
async def list_candidates(
    user: CurrentUser,
    session: DbSession,
    requisition_id: uuid.UUID | None = None,
    stage: CandidateStage | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[CandidateRead]:
    stmt = tenant_scoped(select(Candidate), Candidate, user)
    if not permissions.is_hr(user):
        # Candidates are people who have not joined, with a phone number and a
        # fit score. Outside HR only the requisition's hiring manager or
        # recruiter sees them.
        permissions.require_role(
            user,
            permissions.MANAGER_ROLES,
            detail=(
                "Candidates are visible to HR and to the hiring manager on "
                "the requisition."
            ),
        )
        me = permissions.own_employee_id(user)
        mine = tenant_scoped(
            select(Requisition.id).where(
                or_(Requisition.hiring_manager_id == me, Requisition.recruiter_id == me)
            ),
            Requisition,
            user,
        )
        stmt = stmt.where(Candidate.requisition_id.in_(mine))
    if requisition_id:
        stmt = stmt.where(Candidate.requisition_id == requisition_id)
    if stage:
        stmt = stmt.where(Candidate.stage == stage)

    rows = (
        await session.scalars(
            stmt.order_by(Candidate.fit_score.desc().nullslast(), Candidate.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return Page[CandidateRead](
        items=[CandidateRead.model_validate(row) for row in rows],
        total=len(rows),
        page=page,
        page_size=page_size,
    )


# TODO(V1 step 6): AI JD drafting (FS-HIR-01, 10s budget, versioned on publish),
# resume parsing, stage transitions, and candidate → employee conversion, which
# carries pre-hire signal into the post-hire record (FRD 7.2).
