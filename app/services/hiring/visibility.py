"""Who may see a requisition, a candidate or an interview — the one place it lives.

The rule, unchanged from the original candidate list and now applied everywhere:
candidates are people who have not joined, with a phone number and a salary
expectation. **HR sees them; outside HR, only the requisition's hiring manager or
recruiter does.** A panellist sees the interview they are on and nothing else.

Anything a caller may not see is a 404, not a 403. A 403 on somebody else's
candidate confirms the candidate exists.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.hiring import Candidate, Requisition, RequisitionStatus
from app.models.interview import Interview, InterviewPanelist
from app.models.user import User

__all__ = [
    "is_hiring_team",
    "load_candidate",
    "load_interview",
    "load_requisition",
    "not_found",
    "team_requisition_ids",
    "visible_requisitions",
]


def not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def is_hiring_team(user: User, requisition: Requisition) -> bool:
    """The hiring manager or recruiter on this requisition, or whoever raised it."""
    if requisition.raised_by_user_id is not None and requisition.raised_by_user_id == user.id:
        return True
    me = user.employee_id
    return me is not None and me in (requisition.hiring_manager_id, requisition.recruiter_id)


def team_requisition_ids(user: User) -> Select:
    """Requisitions this caller is on the hiring team of, as a sub-select."""
    conditions = [Requisition.raised_by_user_id == user.id]
    if user.employee_id is not None:
        conditions.append(Requisition.hiring_manager_id == user.employee_id)
        conditions.append(Requisition.recruiter_id == user.employee_id)
    return tenant_scoped(select(Requisition.id).where(or_(*conditions)), Requisition, user)


def visible_requisitions(user: User) -> Select:
    """Requisitions this caller may list.

    HR: all of them. Anybody else: open roles — every employee can refer into
    one — plus, for a manager, the requisitions they are on the hiring team of,
    whatever their status.
    """
    stmt = tenant_scoped(select(Requisition), Requisition, user)
    if permissions.is_hr(user):
        return stmt
    if user.has_reports:
        return stmt.where(
            or_(
                Requisition.status == RequisitionStatus.OPEN,
                Requisition.id.in_(team_requisition_ids(user)),
            )
        )
    return stmt.where(Requisition.status == RequisitionStatus.OPEN)


async def load_requisition(
    session: AsyncSession, user: User, requisition_id: uuid.UUID, *, team_only: bool = True
) -> Requisition:
    """A requisition this caller may work on: HR, or its hiring team."""
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None:
        raise not_found("Requisition")
    if team_only and not permissions.is_hr(user) and not is_hiring_team(user, requisition):
        raise not_found("Requisition")
    return requisition


async def load_candidate(
    session: AsyncSession, user: User, candidate_id: uuid.UUID
) -> tuple[Candidate, Requisition]:
    """A candidate this caller may see, with their requisition."""
    candidate = await session.scalar(
        tenant_scoped(select(Candidate).where(Candidate.id == candidate_id), Candidate, user)
    )
    if candidate is None:
        raise not_found("Candidate")
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == candidate.requisition_id),
            Requisition,
            user,
        )
    )
    if requisition is None:
        raise not_found("Candidate")
    if not permissions.is_hr(user) and not is_hiring_team(user, requisition):
        raise not_found("Candidate")
    return candidate, requisition


async def load_interview(
    session: AsyncSession, user: User, interview_id: uuid.UUID
) -> tuple[Interview, InterviewPanelist | None, Requisition | None, bool]:
    """An interview this caller may see.

    Returns the interview, the caller's own panel row (if any), the requisition,
    and whether the caller is HR or on the hiring team. Someone who is neither on
    the panel nor on the hiring team gets a 404.
    """
    interview = await session.scalar(
        tenant_scoped(select(Interview).where(Interview.id == interview_id), Interview, user)
    )
    if interview is None:
        raise not_found("Interview")
    mine = await session.scalar(
        tenant_scoped(
            select(InterviewPanelist).where(
                InterviewPanelist.interview_id == interview.id,
                InterviewPanelist.user_id == user.id,
            ),
            InterviewPanelist,
            user,
        )
    )
    candidate = await session.scalar(
        tenant_scoped(
            select(Candidate).where(Candidate.id == interview.candidate_id), Candidate, user
        )
    )
    requisition = None
    if candidate is not None:
        requisition = await session.scalar(
            tenant_scoped(
                select(Requisition).where(Requisition.id == candidate.requisition_id),
                Requisition,
                user,
            )
        )
    oversees = permissions.is_hr(user) or (
        requisition is not None and is_hiring_team(user, requisition)
    )
    if mine is None and not oversees:
        raise not_found("Interview")
    return interview, mine, requisition, oversees
