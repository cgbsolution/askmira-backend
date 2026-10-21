"""Courses — POSH, compliance, safety, skills — assignments, quizzes and certificates."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services import learning

router = APIRouter(prefix="/learning", tags=["learning"])

HR_ONLY = [Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))]


class AnswersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[int] = Field(default_factory=list, max_length=50)


@router.get("/courses", dependencies=HR_ONLY)
async def list_courses(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await learning.list_courses(session, user)


@router.post("/courses", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY)
async def create_course(
    payload: learning.CourseIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await learning.create_course(session, user, payload, request=request)


@router.post("/courses/{course_id}/assign", dependencies=HR_ONLY)
async def assign(
    course_id: uuid.UUID,
    payload: learning.AssignIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """To named people, or an audience; anyone already current is skipped."""
    return await learning.assign(session, user, course_id, payload, request=request)


@router.get("/courses/{course_id}/report", dependencies=HR_ONLY)
async def report(course_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await learning.course_report(session, user, course_id)


@router.get("/me")
async def my_courses(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Courses you have been asked to take, and the ones you have completed."""
    return await learning.my_assignments(session, user)


@router.get("/me/{assignment_id}")
async def open_course(
    assignment_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await learning.open_course(session, user, assignment_id)


@router.post("/me/{assignment_id}/complete")
async def complete(
    assignment_id: uuid.UUID,
    payload: AnswersIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Submit the quiz (or confirm a course with none). Scored here, never in the browser."""
    return await learning.complete(
        session, user, assignment_id, answers=payload.answers, request=request
    )


@router.get("/assignments/{assignment_id}/certificate", response_model=None)
async def certificate(assignment_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    data, name = await learning.certificate_pdf(session, user, assignment_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
