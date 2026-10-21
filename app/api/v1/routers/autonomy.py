"""Human-in-the-loop endpoints (slice 29).

`GET /autonomy/transparency` is the one that sells the agent story: what each
agent is permitted to do, what it has done, what is waiting, and what was
reverted. It is the concrete answer to "what is your AI actually doing in my HR
system", and it is readable by any authenticated user — an employee asking what
an agent may do to their record is asking a question they are entitled to an
answer to, and routing it through HR is a way of not answering it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services.autonomy import service as autonomy
from app.services.autonomy.catalogue import (
    ACTIONS,
    AutonomyLevel,
    protected_actions,
)

router = APIRouter(tags=["autonomy"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


# ── Contracts ───────────────────────────────────────────────────────────────


class ConfigureIn(BaseModel):
    action_key: str = Field(min_length=1, max_length=96)
    autonomy: AutonomyLevel
    threshold_value: Decimal | None = None
    approver_roles: list[str] = Field(default_factory=list)


class ProposeIn(BaseModel):
    action_key: str = Field(min_length=1, max_length=96)
    subject_type: str = Field(min_length=1, max_length=64)
    subject_id: uuid.UUID
    proposed_change: dict[str, Any] = Field(default_factory=dict)
    #: Required for a reversible action. Captured now, because by the time
    #: somebody wants to revert the record has moved on.
    previous_state: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    magnitude: Decimal | None = None


class DecideIn(BaseModel):
    approve: bool
    #: Required on a rejection. The rejection rate is a product signal, and
    #: without the reasons nobody can tell which part of the agent is wrong.
    note: str = Field(default="", max_length=4000)


class RevertIn(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


# ── Catalogue and configuration ─────────────────────────────────────────────


@router.get("/autonomy/actions")
async def list_actions(user: CurrentUser) -> dict[str, Any]:
    """Every action an agent can take, its ceiling, and why the ceiling is there.

    Not role-gated. An employee asking whether an agent can change their
    appraisal rating is asking about a promise the product makes them.
    """
    return {
        "actions": [a.as_dict() for a in ACTIONS.values()],
        "protected": [a.as_dict() for a in protected_actions()],
        "statement": (
            "Four actions can never be applied automatically, whatever a client "
            "configures: applying a payroll rule change, releasing a "
            "disbursement, changing an appraisal rating, and any action on a "
            "confidential reporting case. The ceiling is enforced when a "
            "setting is written and again when it is read."
        ),
    }


@router.get("/autonomy/configuration")
async def get_configuration(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Every action with this tenant's setting, including the unconfigured ones.

    Showing only overrides would make an unconfigured action look like an absent
    one, and the default is the setting somebody needs to see.
    """
    return await autonomy.list_configuration(session, user)


@router.put("/autonomy/configuration")
async def configure(
    payload: ConfigureIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Set one action's autonomy. Above the ceiling is refused, with the reason.

    A client asking for auto-apply on payroll rule changes is told no and told
    why — "invalid configuration" means they ask again next quarter.
    """
    row = await autonomy.configure(
        session,
        user,
        action_key=payload.action_key,
        autonomy=payload.autonomy,
        threshold_value=payload.threshold_value,
        approver_roles=payload.approver_roles,
        request=request,
    )
    return {
        "id": str(row.id),
        "action_key": row.action_key,
        "autonomy": row.autonomy.value,
        "max_autonomy_at_time": row.max_autonomy_at_time.value,
        "threshold_value": (
            float(row.threshold_value) if row.threshold_value is not None else None
        ),
        "threshold_unit": row.threshold_unit,
    }


# ── The queue ───────────────────────────────────────────────────────────────


@router.post("/autonomy/proposals", status_code=status.HTTP_201_CREATED)
async def propose(
    payload: ProposeIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Create a proposal, routed by this tenant's configuration.

    Refused if an approver could not evaluate it: no reasoning, no citations, or
    a reversible action with no captured previous state.
    """
    row = await autonomy.propose(
        session,
        user,
        action_key=payload.action_key,
        subject_type=payload.subject_type,
        subject_id=payload.subject_id,
        proposed_change=payload.proposed_change,
        previous_state=payload.previous_state,
        reasoning=payload.reasoning,
        citations=tuple(payload.citations),
        confidence=payload.confidence,
        magnitude=payload.magnitude,
        request=request,
    )
    return {
        "id": str(row.id),
        "status": row.status.value,
        "routing_reason": row.routing_reason,
        "expires_at": row.expires_at.isoformat(),
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
    }


@router.get("/autonomy/proposals")
async def list_proposals(
    session: DbSession,
    user: CurrentUser,
    agent_key: str | None = None,
    impact: str | None = None,
    min_age_days: int | None = None,
    include_decided: bool = False,
    limit: int = 100,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """The one consolidated queue, filterable by agent, impact and age.

    Every proposal carries its reasoning and its citations, so an approver can
    disagree rather than only click.
    """
    return await autonomy.queue(
        session,
        user,
        agent_key=agent_key,
        impact=impact,
        min_age_days=min_age_days,
        include_decided=include_decided,
        limit=limit,
    )


@router.post("/autonomy/proposals/{proposal_id}/decide")
async def decide(
    proposal_id: uuid.UUID,
    payload: DecideIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Approve or reject. An expired proposal cannot be approved."""
    row = await autonomy.decide_proposal(
        session,
        user,
        proposal_id=proposal_id,
        approve=payload.approve,
        note=payload.note,
        request=request,
    )
    return {
        "id": str(row.id),
        "status": row.status.value,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


@router.post("/autonomy/proposals/expire")
async def expire_stale(
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Mark lapsed proposals expired. Never applies one late."""
    return await autonomy.expire_stale(session, user, request=request)


@router.post("/autonomy/proposals/{proposal_id}/revert")
async def revert(
    proposal_id: uuid.UUID,
    payload: RevertIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Undo an applied action.

    Records the reversal and, for a registered subject type (a regularisation, an
    expense claim, a candidate, a lifecycle event), writes the captured values
    back to the record in the same transaction.
    """
    outcome: dict[str, Any] = {}
    row = await autonomy.revert(
        session,
        user,
        proposal_id=proposal_id,
        reason=payload.reason,
        request=request,
        outcome=outcome,
    )
    restored = bool(outcome.get("restored_to_record"))
    return {
        "id": str(row.id),
        "proposal_id": str(row.proposal_id),
        "restored_state": row.restored_state,
        "reverted_at": row.reverted_at.isoformat(),
        "restored_to_record": restored,
        "not_built": (
            None
            if restored
            else (
                "The reversal is recorded and the subject record was not touched: "
                "this subject type has no registered undo, or the change was never "
                "applied to a record."
            )
        ),
    }


# ── Signal and transparency ─────────────────────────────────────────────────


@router.get("/autonomy/decision-stats")
async def decision_stats(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Approval and rejection rates per action, and what looks defective.

    A proposal type rejected ninety per cent of the time is a defective agent.
    The client will not file a bug — they will turn it off and mention it at
    renewal — so the metric has to surface to us as well as to them.
    """
    return await autonomy.decision_stats(session, user)


@router.get("/autonomy/transparency")
async def transparency(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """What each agent may do, has done, is waiting on, and reverted.

    Not role-gated. This is what an enterprise security review asks for, and it
    is also what an employee is owed about their own records.
    """
    return await autonomy.transparency_surface(session, user)
