"""AI features: the assistant, drafting help, survey themes, analytics questions.

Every route here is thin: parse, gate, delegate. The governance — the tenant's
switch, the person's consent, the transfer record and the invocation log — is
in `app/services/ai/`, where no route can skip it.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_module, require_roles
from app.models.user import UserRole
from app.services.ai import analytics, assistant, governance, hiring, recognition, survey_topics

router = APIRouter(tags=["ai"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
HR_ONLY = [Depends(require_roles(*HR_ROLES))]
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]
HIRING_TEAM = [
    Depends(require_module("hiring")),
    Depends(require_roles(*HR_ROLES, line_managers=True)),
]


class QuestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=1000)


class ShoutoutDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_employee_id: uuid.UUID
    notes: str = Field(min_length=3, max_length=2000)
    value_code: str | None = Field(default=None, max_length=64)


class JdDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str = Field(default="", max_length=4000)


# ── Status and settings ─────────────────────────────────────────────────────


@router.get("/ai/status")
async def ai_status(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Whether AI is available here, which features are on, and the caller's consent."""
    return await governance.status_for(session, user)


@router.get("/admin/ai-settings", dependencies=ADMIN_ONLY)
async def get_ai_settings(user: CurrentUser, session: DbSession) -> governance.AiSettings:
    return await governance.get_settings(session, user)


@router.put("/admin/ai-settings", dependencies=ADMIN_ONLY)
async def set_ai_settings(
    payload: governance.AiSettings, user: CurrentUser, session: DbSession, request: Request
) -> governance.AiSettings:
    return await governance.set_settings(session, user, payload, request=request)


# ── The assistant ───────────────────────────────────────────────────────────


@router.post("/assistant/ask")
async def ask_assistant(
    payload: QuestionIn, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await assistant.ask(session, user, payload.question)


@router.get("/assistant/articles")
async def list_articles(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await assistant.list_articles(session, user)


@router.post("/assistant/articles", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY)
async def create_article(
    payload: assistant.ArticleIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await assistant.create_article(session, user, payload, request=request)


@router.patch("/assistant/articles/{article_id}", dependencies=HR_ONLY)
async def update_article(
    article_id: uuid.UUID,
    payload: assistant.ArticleUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await assistant.update_article(session, user, article_id, payload, request=request)


@router.delete(
    "/assistant/articles/{article_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=HR_ONLY,
)
async def delete_article(
    article_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await assistant.delete_article(session, user, article_id, request=request)


@router.get("/assistant/report", dependencies=HR_ONLY)
async def assistant_report(user: CurrentUser, session: DbSession, days: int = 30) -> dict[str, Any]:
    return await assistant.report(session, user, days=max(1, min(days, 365)))


# ── Recognition ─────────────────────────────────────────────────────────────


@router.post("/culture/shoutouts/draft", dependencies=[Depends(require_module("culture"))])
async def draft_shoutout(
    payload: ShoutoutDraftIn, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await recognition.draft(
        session,
        user,
        to_employee_id=payload.to_employee_id,
        notes=payload.notes,
        value_code=payload.value_code,
    )


# ── Survey themes ───────────────────────────────────────────────────────────


@router.get(
    "/culture/surveys/{survey_id}/topics",
    dependencies=[Depends(require_module("culture")), *HR_ONLY],
)
async def survey_topics_latest(
    survey_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any] | None:
    return await survey_topics.latest(session, user, survey_id)


@router.post(
    "/culture/surveys/{survey_id}/topics",
    dependencies=[Depends(require_module("culture")), *HR_ONLY],
)
async def survey_topics_generate(
    survey_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await survey_topics.generate(session, user, survey_id)


# ── Analytics ───────────────────────────────────────────────────────────────


@router.post("/analytics/ask")
async def ask_analytics(
    payload: QuestionIn, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await analytics.ask(session, user, payload.question)


# ── Hiring ──────────────────────────────────────────────────────────────────


@router.post("/hiring/requisitions/{requisition_id}/jd-draft", dependencies=HIRING_TEAM)
async def draft_jd(
    requisition_id: uuid.UUID, payload: JdDraftIn, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await hiring.draft_jd(session, user, requisition_id=requisition_id, notes=payload.notes)


@router.get("/hiring/candidates/{candidate_id}/screenings", dependencies=HIRING_TEAM)
async def list_screenings(
    candidate_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict[str, Any]]:
    return await hiring.screenings_for(session, user, candidate_id)


@router.post(
    "/hiring/candidates/{candidate_id}/screenings",
    status_code=status.HTTP_201_CREATED,
    dependencies=HIRING_TEAM,
)
async def screen_candidate(
    candidate_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await hiring.screen_candidate(session, user, candidate_id)
