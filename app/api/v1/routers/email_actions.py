"""Approve or reject from an approval email: the page the email's buttons open.

No sign-in in front of these. The token in the path names one approver and
one request; GET only describes it (mail scanners open links), and POST
decides it as that approver. See app.services.approvals.email_actions.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.core.deps import DbSession
from app.services.approvals import email_actions

router = APIRouter(prefix="/public/email-actions", tags=["approvals"])


class EmailDecision(BaseModel):
    action: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=1000)


@router.get("/{token}")
async def describe(token: str, session: DbSession, request: Request) -> dict[str, Any]:
    """What the link is about, and whether it is still waiting on its approver."""
    return await email_actions.describe(session, token, request)


@router.post("/{token}")
async def decide(
    token: str, payload: EmailDecision, session: DbSession, request: Request
) -> dict[str, Any]:
    """Approve or reject, as the approver the link was sent to."""
    return await email_actions.decide(
        session, token, request, approve=payload.action == "approve", comment=payload.comment
    )
