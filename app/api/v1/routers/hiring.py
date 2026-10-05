"""Hiring / ATS endpoints: requisitions, candidates and the pipeline (FRD 5.5).

Thin: parse, authorise, delegate, serialise. Who may see what lives in
`app.services.hiring.visibility`; the approval rules in
`app.services.hiring.approvers`.

Blocking gate (FS-HIR-02, M1): AskMira does not compute a Fit Score. A stored
one is only ever returned beside the round scores and signals that back it, and
proctoring notes are reported as observations — see
`app.services.hiring.signals` and `tests/hiring/test_scoring_gate.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.hiring import CandidateStage, RequisitionStatus
from app.models.user import UserRole
from app.schemas.common import Page
from app.schemas.hiring import (
    CandidateCreate,
    CandidateSummary,
    PendingDecision,
    RequisitionCreate,
    RequisitionDetail,
    RequisitionStatusChange,
    RequisitionSummary,
    RequisitionUpdate,
    StageMove,
    StageMoveResult,
)
from app.services.hiring import candidates as candidate_service
from app.services.hiring import requisitions as requisition_service

router = APIRouter(prefix="/hiring", tags=["hiring"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)


def _attachment(data: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


# ── Requisitions ────────────────────────────────────────────────────────────


@router.get("/requisitions", response_model=Page[RequisitionSummary])
async def list_requisitions(
    user: CurrentUser,
    session: DbSession,
    requisition_status: RequisitionStatus | None = Query(default=None, alias="status"),
    mine: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[RequisitionSummary]:
    rows, total = await requisition_service.list_requisitions(
        session,
        user,
        requisition_status=requisition_status,
        mine=mine,
        page=page,
        page_size=page_size,
    )
    return Page[RequisitionSummary](
        items=[RequisitionSummary.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/requisitions",
    response_model=RequisitionDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def create_requisition(
    payload: RequisitionCreate, user: CurrentUser, session: DbSession, request: Request
) -> RequisitionDetail:
    requisition = await requisition_service.create_requisition(
        session, user, payload, request=request
    )
    await session.commit()
    return RequisitionDetail.model_validate(
        await requisition_service.requisition_detail(
            session, user, requisition_id=requisition.id
        )
    )


@router.get(
    "/requisitions/{requisition_id}",
    response_model=RequisitionDetail,
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def get_requisition(
    requisition_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> RequisitionDetail:
    return RequisitionDetail.model_validate(
        await requisition_service.requisition_detail(
            session, user, requisition_id=requisition_id
        )
    )


@router.patch(
    "/requisitions/{requisition_id}",
    response_model=RequisitionDetail,
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def update_requisition(
    requisition_id: uuid.UUID,
    payload: RequisitionUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> RequisitionDetail:
    await requisition_service.update_requisition(
        session, user, requisition_id=requisition_id, payload=payload, request=request
    )
    await session.commit()
    return RequisitionDetail.model_validate(
        await requisition_service.requisition_detail(
            session, user, requisition_id=requisition_id
        )
    )


@router.post(
    "/requisitions/{requisition_id}/status",
    response_model=RequisitionDetail,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def change_requisition_status(
    requisition_id: uuid.UUID,
    payload: RequisitionStatusChange,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> RequisitionDetail:
    await requisition_service.change_status(
        session,
        user,
        requisition_id=requisition_id,
        to_status=RequisitionStatus(payload.status),
        reason=payload.reason,
        request=request,
    )
    await session.commit()
    return RequisitionDetail.model_validate(
        await requisition_service.requisition_detail(
            session, user, requisition_id=requisition_id
        )
    )


@router.get(
    "/approvals",
    response_model=list[PendingDecision],
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def pending_decisions(user: CurrentUser, session: DbSession) -> list[PendingDecision]:
    """Requisitions and offers waiting on a step this caller may sign."""
    rows = await requisition_service.pending_decisions(session, user)
    return [PendingDecision.model_validate(row) for row in rows]


# ── Candidates ──────────────────────────────────────────────────────────────


@router.get(
    "/candidates",
    response_model=Page[CandidateSummary],
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def list_candidates(
    user: CurrentUser,
    session: DbSession,
    requisition_id: uuid.UUID | None = None,
    stage: CandidateStage | None = None,
    search: str | None = Query(default=None, max_length=80),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[CandidateSummary]:
    """Candidates visible to HR, or to the requisition's hiring manager or recruiter."""
    items, total = await candidate_service.list_candidates(
        session,
        user,
        requisition_id=requisition_id,
        stage=stage,
        search=search,
        page=page,
        page_size=page_size,
    )
    return Page[CandidateSummary](
        items=[CandidateSummary.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/candidates",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_candidate(
    payload: CandidateCreate, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    result = await candidate_service.create_candidate(session, user, payload, request=request)
    await session.commit()
    return result


@router.get(
    "/candidates/{candidate_id}",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def get_candidate(
    candidate_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await candidate_service.candidate_profile(session, user, candidate_id=candidate_id)


@router.post(
    "/candidates/{candidate_id}/stage",
    response_model=StageMoveResult,
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def move_candidate_stage(
    candidate_id: uuid.UUID,
    payload: StageMove,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> StageMoveResult:
    result = await candidate_service.move_stage(
        session,
        user,
        candidate_id=candidate_id,
        to_stage=payload.to_stage,
        reason=payload.reason,
        request=request,
    )
    await session.commit()
    return StageMoveResult.model_validate(result)


@router.get(
    "/candidates/{candidate_id}/resume",
    response_model=None,
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def download_resume(
    candidate_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> Response:
    data, filename = await candidate_service.resume_file(session, user, candidate_id=candidate_id)
    return _attachment(data, filename, "application/pdf")


@router.get(
    "/candidates/{candidate_id}/documents/{document_id}",
    response_model=None,
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def download_candidate_document(
    candidate_id: uuid.UUID, document_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> Response:
    data, filename, media_type = await candidate_service.document_file(
        session, user, candidate_id=candidate_id, document_id=document_id
    )
    return _attachment(data, filename, media_type)
