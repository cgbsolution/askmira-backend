"""A customer approves or rejects a consultant's week from its email link: no sign-in.

The token in the path is the credential: single use, unguessable, and good for
that one week only. GET only shows the hours (mail scanners open links); POST
decides. Nothing here ever carries a rate or an amount. See
app.services.consultants.timesheet_approval.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.core.deps import DbSession
from app.services.consultants import timesheet_approval

router = APIRouter(prefix="/public/timesheets", tags=["timesheets"])

_PRIVATE = {"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"}


class DecisionIn(BaseModel):
    approve: bool
    reason: str | None = Field(default=None, max_length=2000)


@router.get("/{token}")
async def describe(
    token: str, session: DbSession, request: Request, response: Response
) -> dict[str, Any]:
    """The week's hours and whether it is still waiting for the customer."""
    response.headers.update(_PRIVATE)
    return await timesheet_approval.describe(session, token, request)


@router.post("/{token}/decision")
async def decide(
    token: str, payload: DecisionIn, session: DbSession, request: Request, response: Response
) -> dict[str, Any]:
    """Approve the week, or reject it with a reason. Works once."""
    response.headers.update(_PRIVATE)
    return await timesheet_approval.decide(
        session,
        token,
        approve=payload.approve,
        reason=payload.reason,
        ip=request.client.host if request.client else None,
        request=request,
    )
