"""Grounding endpoints (slice 28).

`POST /grounding/retrieve` is the one that matters: it is the only path to
grounding content, and it refuses rather than filters. Everything else reads
what happened.

The invocation log is what the AI Trust Center publishes from, so it is readable
by any authenticated user for their own calls and by HR for the tenant's — an
employee asking "what did the assistant see about me" is asking a question they
are entitled to an answer to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.grounding import PromptInjectionAttempt
from app.models.user import UserRole
from app.services.grounding import citations as citation_engine
from app.services.grounding import confidence as confidence_engine
from app.services.grounding import injection as injection_engine
from app.services.grounding import refusals as refusal_engine
from app.services.grounding import sensitive as sensitive_engine
from app.services.grounding import service as grounding
from app.services.grounding.retrieval import FORBIDDEN_SOURCES

router = APIRouter(tags=["grounding"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


# ── Contracts ───────────────────────────────────────────────────────────────


class RetrieveIn(BaseModel):
    sources: list[str] = Field(default_factory=list)
    employee_ids: list[str] = Field(default_factory=list)
    limit_per_source: int = Field(default=25, ge=1, le=200)


class VerifyIn(BaseModel):
    text: str = Field(min_length=1)
    retrieved_ids: list[str] = Field(default_factory=list)
    existing_ids: list[str] = Field(default_factory=list)
    require_citations: bool = True


class ClassifyIn(BaseModel):
    question: str = Field(min_length=1)
    requested_employee_id: str | None = None
    scope_covers: bool = True
    data_available: bool = True
    touches_forbidden_topic: bool = False


class ScanIn(BaseModel):
    text: str = Field(min_length=1)
    field: str = Field(default="", max_length=64)
    source_table: str | None = None
    source_record_id: uuid.UUID | None = None
    #: Whether to write the flagged attempts to the log. Off by default so a
    #: preview from a UI does not fill the queue.
    record: bool = False


class PresentIn(BaseModel):
    score: float
    confidence: float = Field(ge=0, le=1)
    signals: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    threshold: float = Field(default=confidence_engine.DEFAULT_THRESHOLD, ge=0, le=1)


# ── Retrieval ───────────────────────────────────────────────────────────────


@router.get("/grounding/sources")
async def sources(user: CurrentUser) -> dict[str, Any]:
    """What an agent may read, what it may never read, and why.

    Readable by anybody. An employee asking whether an agent can see their
    confidential report is asking about a promise the product made them, and
    routing that through HR is a way of not answering it.
    """
    return {
        "retrievable": sorted(grounding.RETRIEVABLE_SOURCES),
        "never_retrievable": sorted(FORBIDDEN_SOURCES),
        "statement": (
            "Confidential reports, disciplinary matters and credentials are "
            "outside every agent's reach. There is no role that unlocks them "
            "and no setting that changes it — an agent asked about one of these "
            "declines, and a person handles it."
        ),
    }


@router.post("/grounding/retrieve")
async def retrieve(
    payload: RetrieveIn,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """The single retrieval path, scoped to the caller.

    Not role-gated: the scope *is* the gate, and it is applied to the query.
    An employee gets their own records, a manager gets their team's, HR gets the
    tenant's — and a request for somebody outside that is refused rather than
    quietly returning less.
    """
    result = await grounding.retrieve(
        session,
        user,
        sources=tuple(payload.sources),
        employee_ids=tuple(payload.employee_ids),
        limit_per_source=payload.limit_per_source,
    )
    return {
        **result.as_dict(),
        "records": [r.as_dict() for r in result.records],
        "prompt_blocks": [r.as_prompt_block() for r in result.records],
    }


@router.get("/grounding/scope")
async def scope(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """What the caller can ground an answer in, resolved."""
    resolved = await grounding.resolve_scope(session, user)
    return resolved.as_dict()


# ── Verification ────────────────────────────────────────────────────────────


@router.post("/grounding/verify")
async def verify(payload: VerifyIn, user: CurrentUser) -> dict[str, Any]:
    """Check a response's citations. Returns what may be rendered, never both.

    `text` in the response is the withheld message when verification fails. A
    shape that returned the original alongside would eventually have a call site
    render the wrong one.
    """
    result = citation_engine.verify(
        payload.text,
        retrieved_ids=frozenset(payload.retrieved_ids),
        existing_ids=frozenset(payload.existing_ids or payload.retrieved_ids),
        forbidden_sources=FORBIDDEN_SOURCES,
        require_citations=payload.require_citations,
    )
    return result.as_dict()


@router.post("/grounding/classify-refusal")
async def classify_refusal(
    payload: ClassifyIn, user: CurrentUser
) -> dict[str, Any]:
    """Which refusals apply to a question, and what is still answerable.

    The advisory case is the interesting one: a tax question gets its factual
    half answered and its advisory half declined, in the same reply, with a
    human named.
    """
    outcome = refusal_engine.classify(
        payload.question,
        requested_employee_id=payload.requested_employee_id,
        scope_covers=payload.scope_covers,
        data_available=payload.data_available,
        touches_forbidden_topic=payload.touches_forbidden_topic,
    )
    return outcome.as_dict()


@router.post("/grounding/present-score")
async def present_score(payload: PresentIn, user: CurrentUser) -> dict[str, Any]:
    """Turn a raw score into what a screen should show.

    Below the threshold the response carries `score: null` — not a greyed-out
    number, not a number with a warning. A field holding a value the UI is asked
    not to show is a value some UI will show.
    """
    from fastapi import HTTPException

    try:
        result = confidence_engine.present(
            score=payload.score,
            confidence=payload.confidence,
            signals=tuple(payload.signals),
            missing=tuple(payload.missing),
            threshold=payload.threshold,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.as_dict()


# ── Injection ───────────────────────────────────────────────────────────────


@router.post("/grounding/scan-untrusted")
async def scan_untrusted(
    payload: ScanIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Flag injection attempts in employee- or candidate-supplied text.

    Reports; never blocks. A resume containing "ignore previous instructions" is
    not a reason to reject an application — the candidate may not have written
    it, and refusing outright would let anybody get a rival rejected by editing a
    shared document. The text is neutralised by the prompt fencing regardless.
    """
    attempts = injection_engine.detect(payload.text, field=payload.field)
    recorded = 0
    if payload.record and attempts:
        recorded = await grounding.log_injection_attempts(
            session,
            user,
            attempts=attempts,
            source_table=payload.source_table,
            source_record_id=payload.source_record_id,
        )
    return {
        "attempts": [a.as_dict() for a in attempts],
        "recorded": recorded,
        "neutralised_prompt_block": injection_engine.wrap(
            payload.text, label=payload.field or "untrusted"
        ),
        "note": (
            "Detection is the weakest of the three defences and is not the "
            "safety argument — the fencing and the instruction hierarchy are. "
            "This exists so an attempt is visible to a person."
        ),
    }


@router.get("/grounding/injection-attempts")
async def injection_attempts(
    session: DbSession,
    user: CurrentUser,
    unreviewed_only: bool = False,
    limit: int = 100,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    stmt = tenant_scoped(
        select(PromptInjectionAttempt), PromptInjectionAttempt, user
    )
    if unreviewed_only:
        stmt = stmt.where(PromptInjectionAttempt.reviewed_at.is_(None))
    rows = (
        (
            await session.execute(
                stmt.order_by(PromptInjectionAttempt.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "attempts": [
            {
                "id": str(row.id),
                "source_field": row.source_field,
                "source_table": row.source_table,
                "source_record_id": (
                    str(row.source_record_id) if row.source_record_id else None
                ),
                "severity": row.severity,
                "matched_text": row.matched_text,
                "intent": row.intent,
                "reviewed_at": (
                    row.reviewed_at.isoformat() if row.reviewed_at else None
                ),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.post("/grounding/injection-attempts/{attempt_id}/review")
async def review_attempt(
    attempt_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Mark an attempt as looked at.

    An unreviewed queue nobody can separate from a reviewed one stops being
    read, which is the same as not having one.
    """
    from fastapi import HTTPException

    stmt = tenant_scoped(
        select(PromptInjectionAttempt).where(
            PromptInjectionAttempt.id == attempt_id
        ),
        PromptInjectionAttempt,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such attempt.")
    row.reviewed_at = datetime.now(UTC)
    row.reviewed_by_user_id = user.id
    await session.commit()
    return {"id": str(row.id), "reviewed_at": row.reviewed_at.isoformat()}


# ── Sensitive topics ────────────────────────────────────────────────────────


@router.post("/grounding/check-inference", status_code=status.HTTP_200_OK)
async def check_inference(
    payload: VerifyIn, user: CurrentUser
) -> dict[str, Any]:
    """Whether a piece of text draws a conclusion across sensitive topics.

    Reading is not the same as inferring. An HR admin may read a sick-leave
    record and a rating; an agent joining them into "may not be a fit" is a
    conclusion nobody asked for, about a real person, delivered with the
    authority of a system.
    """
    findings = sensitive_engine.topics_in(payload.text)
    try:
        sensitive_engine.assert_no_cross_topic_inference(payload.text)
    except ValueError as exc:
        return {
            "permitted": False,
            "reason": str(exc),
            "topics": [f.as_dict() for f in findings],
            "safe_alternative": sensitive_engine.safe_summary_note(),
        }
    return {
        "permitted": True,
        "topics": [f.as_dict() for f in findings],
    }


# ── The log ─────────────────────────────────────────────────────────────────


@router.get("/grounding/invocations")
async def invocations(
    session: DbSession,
    user: CurrentUser,
    agent_key: str | None = None,
    limit: int = 100,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Every agent call, with enough detail to reconstruct the answer.

    What the AI Trust Center publishes from. Includes the citation failures, so
    "the model fabricated a record and the system caught it" is visible rather
    than being an absence of a log line.
    """
    return await grounding.invocation_log(
        session, user, agent_key=agent_key, limit=limit
    )
