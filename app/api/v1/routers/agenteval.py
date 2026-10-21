"""Agent evaluation endpoints (slice 27).

The one that matters is `POST /agent-eval/{agent_key}/run`: it returns the gate
outcome, and a `block` is a block. Everything else here exists to make that call
meaningful — registering suites, reading the history, capturing the feedback
that becomes tomorrow's cases.

Admin-gated throughout. An eval run is a statement about whether a release may
ship, and the ability to trigger one against a chosen implementation is the
ability to produce a green run somebody might quote.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.agenteval import AgentFeedback, EvalTrigger
from app.models.user import UserRole
from app.services.agenteval import service as eval_service
from app.services.agenteval.drift import ScoreWindow, detect_drift
from app.services.agenteval.gating import all_suite_defects
from app.services.agenteval.suites import SUITES

router = APIRouter(tags=["agent-eval"])

ADMIN_ROLES = (UserRole.ADMIN,)
HR_ROLES = (*ADMIN_ROLES, UserRole.SUB_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class RunIn(BaseModel):
    triggered_by: EvalTrigger = EvalTrigger.MANUAL
    #: Filters cases by tag. Regression cases are always included regardless —
    #: letting a tag filter skip one would make "I ran the suite" mean something
    #: different depending on the flags.
    only_tags: list[str] = Field(default_factory=list)
    #: Run the suite against a different registered implementation. Used to
    #: demonstrate that a suite catches a known defect.
    agent_override: str | None = None


class FeedbackIn(BaseModel):
    agent_key: str = Field(min_length=1, max_length=64)
    agent_input: dict[str, Any] = Field(default_factory=dict)
    agent_output: dict[str, Any] = Field(default_factory=dict)
    was_useful: bool
    comment: str | None = Field(default=None, max_length=4000)


class PromoteIn(BaseModel):
    case_name: str = Field(min_length=1, max_length=160)
    assertions: list[dict[str, Any]] = Field(default_factory=list)
    source_incident_id: str | None = Field(default=None, max_length=64)


class DriftIn(BaseModel):
    baseline_scores: list[float] = Field(default_factory=list)
    current_scores: list[float] = Field(default_factory=list)
    baseline_refusal_rate: float | None = None
    current_refusal_rate: float | None = None
    baseline_grounding_failure_rate: float | None = None
    current_grounding_failure_rate: float | None = None
    sample_count: int = 0
    baseline_latency_ms: float | None = None
    current_latency_ms: float | None = None
    baseline_cost_inr: float | None = None
    current_cost_inr: float | None = None


# ── Suites ──────────────────────────────────────────────────────────────────


@router.get("/agent-eval/suites")
async def list_suites(user: CurrentUser) -> dict[str, Any]:
    """The canonical suites defined in code, and any defect in them.

    `defects` should always be empty — an invariant fails the build otherwise.
    It is exposed anyway, because a suite that cannot catch what it exists for
    is the failure this whole slice is about and it should be visible from
    outside the test suite too.
    """
    return {
        "suites": [s.as_dict() for s in SUITES.values()],
        "defects": [str(d) for d in all_suite_defects()],
    }


@router.get("/agent-eval/coverage")
async def coverage(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Which agents have suites registered, and which do not."""
    return await eval_service.coverage(session, user)


@router.post("/agent-eval/suites/seed", status_code=status.HTTP_201_CREATED)
async def seed_suites(
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Register the canonical suites for every agent named in the brief."""
    written = await eval_service.seed_canonical_suites(session, user, request=request)
    return {"registered": written}


# ── Running ─────────────────────────────────────────────────────────────────


@router.post("/agent-eval/{agent_key}/run", status_code=status.HTTP_201_CREATED)
async def run(
    agent_key: str,
    payload: RunIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Run a registered suite and return the gate outcome.

    `allows_merge` is false on a block, and a failed regression case always
    blocks — it is tagged to a defect that reached production once, and merging
    past it ships that defect again.
    """
    row = await eval_service.execute_suite(
        session,
        user,
        agent_key=agent_key,
        triggered_by=payload.triggered_by,
        only_tags=tuple(payload.only_tags),
        agent_override=payload.agent_override,
        request=request,
    )
    return {
        "id": str(row.id),
        "agent_key": row.agent_key,
        "agent_fingerprint": row.agent_fingerprint,
        "pass_count": row.pass_count,
        "fail_count": row.fail_count,
        "error_count": row.error_count,
        "gate_outcome": row.gate_outcome.value,
        "allows_merge": row.gate_outcome.value != "block",
        "blocking_reasons": row.blocking_reasons,
        "warnings": row.warnings,
        "diff_from_previous": row.diff_from_previous,
        "metrics": row.metrics,
        "total_tokens": row.total_tokens,
        "total_cost_inr": float(row.total_cost_inr),
        "total_latency_ms": float(row.total_latency_ms),
    }


@router.get("/agent-eval/{agent_key}/runs")
async def run_history(
    agent_key: str,
    session: DbSession,
    user: CurrentUser,
    limit: int = 20,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Run history with the diff against the previous run on each.

    The diff is the useful column: a slow degradation is a series of green runs
    whose measurements are sliding, and a pass rate does not show it.
    """
    return await eval_service.run_history(session, user, agent_key=agent_key, limit=limit)


# ── Feedback ────────────────────────────────────────────────────────────────


@router.post("/agent-eval/feedback", status_code=status.HTTP_201_CREATED)
async def capture_feedback(
    payload: FeedbackIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Record what somebody thought of a live agent output.

    Not role-gated. The person best placed to say an agent output was wrong is
    whoever was shown it, and requiring an admin to relay that is how feedback
    stops arriving.
    """
    row = await eval_service.capture_feedback(
        session,
        user,
        agent_key=payload.agent_key,
        agent_input=payload.agent_input,
        agent_output=payload.agent_output,
        was_useful=payload.was_useful,
        comment=payload.comment,
        request=request,
    )
    return {"id": str(row.id)}


@router.get("/agent-eval/feedback")
async def list_feedback(
    session: DbSession,
    user: CurrentUser,
    agent_key: str | None = None,
    unpromoted_only: bool = False,
    limit: int = 100,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Feedback, with the unpromoted ones separable.

    `unpromoted_only` is the working queue: feedback that has not become a case
    is feedback nobody acted on, and it stops being useful the moment nobody can
    tell which is which.
    """
    stmt = tenant_scoped(select(AgentFeedback), AgentFeedback, user)
    if agent_key:
        stmt = stmt.where(AgentFeedback.agent_key == agent_key)
    if unpromoted_only:
        stmt = stmt.where(AgentFeedback.promoted_to_case_id.is_(None))
    rows = (
        (await session.execute(stmt.order_by(AgentFeedback.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return {
        "feedback": [
            {
                "id": str(row.id),
                "agent_key": row.agent_key,
                "was_useful": row.was_useful,
                "comment": row.comment,
                "created_at": row.created_at.isoformat(),
                "promoted_to_case_id": (
                    str(row.promoted_to_case_id) if row.promoted_to_case_id else None
                ),
            }
            for row in rows
        ]
    }


@router.post(
    "/agent-eval/feedback/{feedback_id}/promote",
    status_code=status.HTTP_201_CREATED,
)
async def promote_feedback(
    feedback_id: uuid.UUID,
    payload: PromoteIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Turn feedback into a case. This is how the suite stays relevant."""
    row = await eval_service.promote_feedback_to_case(
        session,
        user,
        feedback_id=feedback_id,
        case_name=payload.case_name,
        assertions=payload.assertions,
        source_incident_id=payload.source_incident_id,
        request=request,
    )
    return {
        "id": str(row.id),
        "case_name": row.name,
        "is_regression_case": row.is_regression_case,
    }


# ── Drift ───────────────────────────────────────────────────────────────────


@router.post("/agent-eval/drift")
async def check_drift(
    payload: DriftIn,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Compare a live window against a baseline. Alerts, never gates.

    The Fit Score defect was visible in the live distribution long before
    anybody looked at it — four hundred candidates, every score between 71 and
    73. `distribution_collapse` is that chart, as a function.
    """
    alerts = detect_drift(
        baseline_scores=(
            ScoreWindow("baseline", tuple(payload.baseline_scores))
            if payload.baseline_scores
            else None
        ),
        current_scores=(
            ScoreWindow("current", tuple(payload.current_scores))
            if payload.current_scores
            else None
        ),
        baseline_refusal_rate=payload.baseline_refusal_rate,
        current_refusal_rate=payload.current_refusal_rate,
        baseline_grounding_failure_rate=payload.baseline_grounding_failure_rate,
        current_grounding_failure_rate=payload.current_grounding_failure_rate,
        sample_count=payload.sample_count or len(payload.current_scores),
        baseline_latency_ms=payload.baseline_latency_ms,
        current_latency_ms=payload.current_latency_ms,
        baseline_cost_inr=payload.baseline_cost_inr,
        current_cost_inr=payload.current_cost_inr,
    )
    return {
        "alerts": [a.as_dict() for a in alerts],
        "clean": not alerts,
    }
