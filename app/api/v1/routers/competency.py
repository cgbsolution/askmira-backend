"""Rating scales, competencies, templates and weighted scoring (slice 16)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.competency import CompetencyCategory
from app.schemas.performance import (
    AppraisalTemplateCreate,
    AppraisalTemplateRead,
    CompetencyCreate,
    CompetencyRead,
    CompetencyTemplateCreate,
    CompetencyTemplateRead,
    CompetencyUpdate,
    ScaleCreate,
    ScaleRead,
)
from app.services.appraisal import competency_service

router = APIRouter(tags=["competency"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]
RATERS = [Depends(require_roles(*permissions.HR_ROLES, line_managers=True))]


@router.post(
    "/competency/scales",
    response_model=ScaleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_scale(
    payload: ScaleCreate, user: CurrentUser, session: DbSession, request: Request
) -> ScaleRead:
    row = await competency_service.create_scale(session, user, payload, request=request)
    await session.commit()
    return ScaleRead.model_validate(row)


@router.get("/competency/scales", response_model=list[ScaleRead], dependencies=RATERS)
async def list_scales(user: CurrentUser, session: DbSession) -> list[ScaleRead]:
    return [
        ScaleRead.model_validate(row) for row in await competency_service.list_scales(session, user)
    ]


@router.post(
    "/competency/competencies",
    response_model=CompetencyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_competency(
    payload: CompetencyCreate, user: CurrentUser, session: DbSession, request: Request
) -> CompetencyRead:
    row = await competency_service.create_competency(session, user, payload, request=request)
    await session.commit()
    return CompetencyRead.model_validate(row)


@router.patch(
    "/competency/competencies/{competency_id}",
    response_model=CompetencyRead,
    dependencies=HR_ONLY,
)
async def update_competency(
    competency_id: uuid.UUID,
    payload: CompetencyUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> CompetencyRead:
    row = await competency_service.update_competency(
        session, user, competency_id, payload, request=request
    )
    await session.commit()
    return CompetencyRead.model_validate(row)


@router.get("/competency/competencies", response_model=list[CompetencyRead], dependencies=RATERS)
async def list_competencies(
    user: CurrentUser, session: DbSession, category: CompetencyCategory | None = None
) -> list[CompetencyRead]:
    rows = await competency_service.list_competencies(session, user, category=category)
    return [CompetencyRead.model_validate(row) for row in rows]


@router.post(
    "/competency/templates",
    response_model=CompetencyTemplateRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_competency_template(
    payload: CompetencyTemplateCreate, user: CurrentUser, session: DbSession, request: Request
) -> CompetencyTemplateRead:
    row = await competency_service.create_competency_template(
        session, user, payload, request=request
    )
    await session.commit()
    return CompetencyTemplateRead.model_validate(row)


@router.get(
    "/competency/templates", response_model=list[CompetencyTemplateRead], dependencies=RATERS
)
async def list_competency_templates(
    user: CurrentUser, session: DbSession
) -> list[CompetencyTemplateRead]:
    rows = await competency_service.list_competency_templates(session, user)
    return [CompetencyTemplateRead.model_validate(row) for row in rows]


@router.put(
    "/competency/appraisal-templates",
    response_model=AppraisalTemplateRead,
    dependencies=HR_ONLY,
)
async def set_appraisal_template(
    payload: AppraisalTemplateCreate, user: CurrentUser, session: DbSession, request: Request
) -> AppraisalTemplateRead:
    """The weighting and the score bands for a draft cycle, validated now."""
    row = await competency_service.create_appraisal_template(
        session, user, payload, request=request
    )
    await session.commit()
    return AppraisalTemplateRead.model_validate(row)


@router.get(
    "/competency/appraisal-templates",
    response_model=AppraisalTemplateRead | None,
    dependencies=HR_ONLY,
)
async def get_appraisal_template(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> AppraisalTemplateRead | None:
    row = await competency_service.get_appraisal_template(session, user, cycle_id)
    return AppraisalTemplateRead.model_validate(row) if row else None


@router.get("/competency/evidence", dependencies=RATERS)
async def recognition_evidence(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID,
    cycle_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Recognition against each competency's values. Evidence, not a score."""
    return await competency_service.employee_evidence(
        session, user, employee_id=employee_id, cycle_id=cycle_id
    )
