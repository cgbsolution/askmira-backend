"""The approvals inbox: everything waiting on the caller, across modules.

Read-only. Each item links to the page where it is decided, and that module's
own endpoint remains the authority on the decision.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentUser, DbSession
from app.schemas.approvals import ApprovalInboxRead
from app.services.approvals import service as approvals_service

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=ApprovalInboxRead)
async def list_approvals(user: CurrentUser, session: DbSession) -> ApprovalInboxRead:
    return await approvals_service.inbox(session, user)


@router.get("/count")
async def approvals_count(user: CurrentUser, session: DbSession) -> dict[str, int]:
    """Just the number, for a badge."""
    result = await approvals_service.inbox(session, user)
    return {"count": result.total}
