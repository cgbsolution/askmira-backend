"""Onboarding: checklists, new joiners' plans, and the tasks on them.

Thin: parse, gate, delegate to `app.services.onboarding.service`, serialise.
Checklists and the new-joiner list are HR's. Everybody signed in reaches their
own onboarding (`/onboarding/me`) and the tasks assigned to them
(`/onboarding/my-tasks`) — a pre-joiner with a login included, which is why no
route here depends on employment status. Who may complete, skip or reassign a
particular task is decided per task in the service.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from app.core import permissions
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.onboarding import OnboardingPlanStatus
from app.schemas.onboarding import (
    MyTaskRead,
    OnboardingMe,
    OnboardingSummary,
    OwnerCandidate,
    PlanCancel,
    PlanCreate,
    PlanDetail,
    PlanRead,
    TaskComplete,
    TaskRead,
    TaskSkip,
    TaskUpdate,
    TemplateCreate,
    TemplateRead,
    TemplateUpdate,
)
from app.services.onboarding import service as onboarding

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]


async def _task_read(session, user, task_id: uuid.UUID) -> TaskRead:
    return TaskRead(**await onboarding.task_read(session, user, task_id))


# ── The caller's own ────────────────────────────────────────────────────────


@router.get("/me", response_model=OnboardingMe)
async def my_onboarding(user: CurrentUser, session: DbSession) -> OnboardingMe:
    """My onboarding plan and every task on it. `plan` is null when I have none."""
    plan = await onboarding.my_onboarding(session, user)
    return OnboardingMe(plan=PlanDetail(**plan) if plan else None)


@router.get("/me/summary", response_model=OnboardingSummary)
async def my_summary(user: CurrentUser, session: DbSession) -> OnboardingSummary:
    return OnboardingSummary(**await onboarding.my_summary(session, user))


@router.get("/my-tasks", response_model=list[MyTaskRead])
async def my_tasks(
    user: CurrentUser,
    session: DbSession,
    include_done: bool = Query(default=False),
) -> list[MyTaskRead]:
    """Tasks on other people's onboarding assigned to me: manager, buddy, IT, HR work."""
    rows = await onboarding.my_tasks(session, user, include_done=include_done)
    return [MyTaskRead(**row) for row in rows]


# ── Checklists (HR) ─────────────────────────────────────────────────────────


@router.get("/templates", response_model=list[TemplateRead], dependencies=HR_ONLY)
async def list_templates(user: CurrentUser, session: DbSession) -> list[TemplateRead]:
    return [TemplateRead(**row) for row in await onboarding.list_templates(session, user)]


@router.post(
    "/templates",
    response_model=TemplateRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_template(
    payload: TemplateCreate, user: CurrentUser, session: DbSession, request: Request
) -> TemplateRead:
    template = await onboarding.create_template(session, user, payload, request=request)
    await session.commit()
    return TemplateRead(**await onboarding.get_template(session, user, template.id))


@router.post(
    "/templates/standard",
    response_model=TemplateRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_standard_template(
    user: CurrentUser, session: DbSession, request: Request
) -> TemplateRead:
    """Build the standard Indian checklist, which HR then edits to fit."""
    template = await onboarding.create_standard_template(session, user, request=request)
    await session.commit()
    return TemplateRead(**await onboarding.get_template(session, user, template.id))


@router.get("/templates/{template_id}", response_model=TemplateRead, dependencies=HR_ONLY)
async def get_template(
    template_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> TemplateRead:
    return TemplateRead(**await onboarding.get_template(session, user, template_id))


@router.put("/templates/{template_id}", response_model=TemplateRead, dependencies=HR_ONLY)
async def update_template(
    template_id: uuid.UUID,
    payload: TemplateUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TemplateRead:
    await onboarding.update_template(session, user, template_id, payload, request=request)
    await session.commit()
    return TemplateRead(**await onboarding.get_template(session, user, template_id))


@router.delete(
    "/templates/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=HR_ONLY,
)
async def delete_template(
    template_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await onboarding.delete_template(session, user, template_id, request=request)
    await session.commit()


@router.get("/owner-candidates", response_model=list[OwnerCandidate], dependencies=HR_ONLY)
async def owner_candidates(user: CurrentUser, session: DbSession) -> list[OwnerCandidate]:
    return [OwnerCandidate(**row) for row in await onboarding.owner_candidates(session, user)]


# ── Plans ───────────────────────────────────────────────────────────────────


@router.get("/plans", response_model=list[PlanRead], dependencies=HR_ONLY)
async def list_plans(
    user: CurrentUser,
    session: DbSession,
    plan_status: OnboardingPlanStatus | None = Query(default=None, alias="status"),
    department_id: uuid.UUID | None = None,
) -> list[PlanRead]:
    rows = await onboarding.list_plans(
        session, user, plan_status=plan_status, department_id=department_id
    )
    return [PlanRead(**row) for row in rows]


@router.post(
    "/plans",
    response_model=PlanDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def start_plan(
    payload: PlanCreate, user: CurrentUser, session: DbSession, request: Request
) -> PlanDetail:
    """Start onboarding: tasks dated from the joining date, assignees resolved and told."""
    plan = await onboarding.start_plan(session, user, payload, request=request)
    await session.commit()
    return PlanDetail(**await onboarding.plan_detail(session, user, plan.id))


@router.get("/plans/{plan_id}", response_model=PlanDetail)
async def get_plan(plan_id: uuid.UUID, user: CurrentUser, session: DbSession) -> PlanDetail:
    """HR, the joiner, their buddy, their reporting line, or anyone holding a task on it."""
    return PlanDetail(**await onboarding.plan_detail(session, user, plan_id))


@router.post("/plans/{plan_id}/cancel", response_model=PlanDetail, dependencies=HR_ONLY)
async def cancel_plan(
    plan_id: uuid.UUID,
    payload: PlanCancel,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> PlanDetail:
    await onboarding.cancel_plan(session, user, plan_id, reason=payload.reason, request=request)
    await session.commit()
    return PlanDetail(**await onboarding.plan_detail(session, user, plan_id))


# ── Tasks ───────────────────────────────────────────────────────────────────


@router.post("/tasks/{task_id}/complete", response_model=TaskRead)
async def complete_task(
    task_id: uuid.UUID,
    payload: TaskComplete,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TaskRead:
    await onboarding.complete_task(session, user, task_id, note=payload.note, request=request)
    await session.commit()
    return await _task_read(session, user, task_id)


@router.post("/tasks/{task_id}/document", response_model=TaskRead)
async def upload_document(
    task_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: Annotated[UploadFile, File()],
) -> TaskRead:
    """Upload a PDF, PNG or JPEG for a document task; it lands in the joiner's vault."""
    # One byte past the limit is enough to know it is over the limit.
    data = await file.read(settings.MAX_UPLOAD_BYTES + 1)
    await onboarding.upload_for_task(session, user, task_id, data=data, request=request)
    return await _task_read(session, user, task_id)


@router.get("/tasks/{task_id}/document")
async def download_document(
    task_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    filename, mime, data = await onboarding.task_document(session, user, task_id, request=request)
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/tasks/{task_id}/skip", response_model=TaskRead, dependencies=HR_ONLY)
async def skip_task(
    task_id: uuid.UUID,
    payload: TaskSkip,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TaskRead:
    await onboarding.skip_task(session, user, task_id, reason=payload.reason, request=request)
    await session.commit()
    return await _task_read(session, user, task_id)


@router.patch("/tasks/{task_id}", response_model=TaskRead, dependencies=HR_ONLY)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TaskRead:
    """Reassign a task, move its due date, or both."""
    await onboarding.update_task(
        session,
        user,
        task_id,
        assignee_user_id=payload.assignee_user_id,
        due_date=payload.due_date,
        request=request,
    )
    await session.commit()
    return await _task_read(session, user, task_id)
