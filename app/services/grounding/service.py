"""The DB-facing half of grounding: the one retrieval path, and the log.

`retrieve` is the only place an agent gets grounding content. An invariant
checks the forbidden sources are refused here rather than filtered by a caller,
and that the scope is applied to the query rather than to the results.

`ground_and_verify` is the pipeline an agent calls — retrieve, build the
prompt, verify the response, log. It is split into `prepare` and `conclude` so
an agent whose model call is async (the employee assistant) runs the same
steps in the same order, with its own call in the middle.
"""

from __future__ import annotations

import hashlib
import inspect
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_filter, tenant_scoped
from app.models.employee import Employee
from app.models.grounding import (
    AgentInvocation,
    InvocationOutcome,
    PromptInjectionAttempt,
)
from app.models.user import User
from app.services.grounding import citations as citation_engine
from app.services.grounding import confidence as confidence_engine
from app.services.grounding import injection as injection_engine
from app.services.grounding import refusals as refusal_engine
from app.services.grounding import sensitive as sensitive_engine
from app.services.grounding.retrieval import (
    FORBIDDEN_SOURCES,
    RetrievalError,
    RetrievalResult,
    RetrievalScope,
    RetrievedRecord,
    ScopeKind,
    assert_retrievable,
    enforce_scope,
    scope_for,
)

__all__ = [
    "Grounding",
    "conclude",
    "ground_and_verify",
    "log_injection_attempts",
    "log_local_answer",
    "log_refusal",
    "prepare",
    "resolve_scope",
    "retrieve",
]

#: The tables an agent may read from, and the column that says whose record it
#: is. A source not in here cannot be retrieved at all — an allowlist rather
#: than a blocklist, so a table added by a future slice is unreachable until
#: somebody decides it should be readable.
RETRIEVABLE_SOURCES: dict[str, str] = {
    "employees": "id",
    "employee_salaries": "employee_id",
    "payslips": "employee_id",
    "leave_requests": "employee_id",
    "leave_balances": "employee_id",
    "attendance_days": "employee_id",
    "appraisals": "employee_id",
    "lifecycle_events": "employee_id",
    "employee_documents": "employee_id",
    "holidays": "",
    "leave_types": "",
    "leave_policies": "",
    "payroll_rule_sets": "",
    "assistant_articles": "",
}


def _narrowing(table: Any, today: date) -> list[Any]:
    """Conditions a source carries beyond tenant and scope.

    A draft article is not policy, a retired leave type answers nothing, and
    last year's holidays crowd out this year's within the per-source limit.
    """
    if table.name == "assistant_articles":
        return [table.c.is_published.is_(True)]
    if table.name in ("leave_types", "leave_policies"):
        return [table.c.is_active.is_(True)]
    if table.name == "holidays":
        return [table.c.holiday_date >= today - timedelta(days=31)]
    return []


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


async def resolve_scope(session: AsyncSession, user: User) -> RetrievalScope:
    """The requester's grounding reach, resolved against the org chart.

    A manager's team is expanded here, once, into a concrete set of employee
    ids. Leaving the expansion to each query would let two call sites disagree
    about what "their team" means — and one of them would be wider.
    """
    reachable: set[str] = set()
    if user.employee_id:
        # Direct and indirect reports, walked breadth-first. Depth-capped: a
        # cycle in the reporting line would otherwise loop forever, and a cycle
        # is a real state in a system where two people can be set as each
        # other's manager during a reorg.
        frontier = {user.employee_id}
        for _ in range(10):
            if not frontier:
                break
            stmt = tenant_scoped(
                select(Employee.id).where(Employee.manager_id.in_(frontier)),
                Employee,
                user,
            )
            rows = set((await session.execute(stmt)).scalars().all())
            new = {str(r) for r in rows} - reachable
            if not new:
                break
            reachable |= new
            frontier = {uuid.UUID(r) for r in new}

    return scope_for(
        role=permissions.scope_role(user),
        tenant_id=str(user.tenant_id),
        requester_employee_id=str(user.employee_id) if user.employee_id else None,
        reachable_employee_ids=frozenset(reachable),
    )


async def retrieve(
    session: AsyncSession,
    user: User,
    *,
    sources: tuple[str, ...],
    employee_ids: tuple[str, ...] = (),
    limit_per_source: int = 25,
) -> RetrievalResult:
    """The single retrieval path. Scoped at the query, not at the output.

    Forbidden sources raise. Sources outside the allowlist are refused and
    reported rather than silently dropped, so an agent that could not ground an
    answer can say which source it could not reach — "I do not have access to
    that" is a better answer than a confident one built on what was left.
    """
    scope = await resolve_scope(session, user)

    for source in sources:
        # Raises. There is no role that unlocks a forbidden source and no
        # parameter that overrides it.
        assert_retrievable(source)

    wanted = list(employee_ids)
    if wanted and scope.kind is not ScopeKind.TENANT:
        out_of_reach = [e for e in wanted if not scope.covers(e)]
        if out_of_reach:
            raise _forbidden(
                "That question needs records for somebody outside your access. "
                "The retrieval is refused rather than filtered afterwards: an "
                "answer built on data you may not see is a disclosure even when "
                "the answer does not name it."
            )

    records: list[RetrievedRecord] = []
    refused: list[str] = []
    notes: list[str] = []

    for source in sources:
        subject_column = RETRIEVABLE_SOURCES.get(source)
        if subject_column is None:
            refused.append(source)
            notes.append(
                f"{source!r} is not on the agent-retrievable allowlist. Adding "
                "a table to the schema does not make it readable by an agent; "
                "somebody has to decide it should be."
            )
            continue

        rows = await _fetch(
            session,
            user,
            source=source,
            subject_column=subject_column,
            scope=scope,
            employee_ids=wanted,
            limit=limit_per_source,
        )
        records.extend(rows)

    kept, dropped = enforce_scope(records, scope)
    if dropped:
        notes.append(
            f"{len(dropped)} record(s) came back outside the requester's scope "
            "and were dropped. This should never happen — it means a query was "
            "wider than the scope applied to it."
        )

    return RetrievalResult(
        records=tuple(kept),
        scope=scope,
        refused_sources=tuple(refused),
        dropped_out_of_scope=tuple(dropped),
        notes=tuple(notes),
    )


async def _fetch(
    session: AsyncSession,
    user: User,
    *,
    source: str,
    subject_column: str,
    scope: RetrievalScope,
    employee_ids: list[str],
    limit: int,
) -> list[RetrievedRecord]:
    """Read one source, with the scope in the WHERE clause.

    Uses the mapped table rather than raw SQL so `tenant_scoped` applies and the
    column names are checked against the model. The scope filter is added to the
    statement, never applied to the result — see the module docstring for why
    those are not equivalent.
    """
    from app.models.base import Base

    table = Base.metadata.tables.get(source)
    if table is None:
        return []

    stmt = select(table).where(tenant_filter(table, user.tenant_id))
    for condition in _narrowing(table, date.today()):
        stmt = stmt.where(condition)
    if source == "holidays":
        stmt = stmt.order_by(table.c.holiday_date)

    if subject_column and subject_column in table.c:
        column = table.c[subject_column]
        if scope.kind is not ScopeKind.TENANT:
            reachable = scope.employee_ids
            if not reachable:
                return []
            stmt = stmt.where(column.in_([uuid.UUID(e) for e in reachable]))
        if employee_ids:
            stmt = stmt.where(column.in_([uuid.UUID(e) for e in employee_ids]))

    rows = (await session.execute(stmt.limit(limit))).mappings().all()

    out: list[RetrievedRecord] = []
    for row in rows:
        subject = row.get(subject_column) if subject_column else None
        out.append(
            RetrievedRecord(
                source=source,
                record_id=str(row["id"]),
                content={
                    key: str(value)
                    for key, value in row.items()
                    if key not in ("tenant_id", "created_at", "updated_at")
                    and value is not None
                },
                subject_employee_id=str(subject) if subject else None,
            )
        )
    return out


def _prompt_fingerprint(system_rules: str) -> str:
    return hashlib.sha256(system_rules.encode("utf-8")).hexdigest()[:16]


async def log_injection_attempts(
    session: AsyncSession,
    user: User,
    *,
    attempts: list[injection_engine.InjectionAttempt],
    source_table: str | None = None,
    source_record_id: uuid.UUID | None = None,
    invocation_id: uuid.UUID | None = None,
    commit: bool = True,
) -> int:
    """Record flagged attempts. Never blocks the work they were found in."""
    for attempt in attempts:
        session.add(
            PromptInjectionAttempt(
                tenant_id=user.tenant_id,
                source_field=attempt.field or "unknown",
                source_table=source_table,
                source_record_id=source_record_id,
                severity=attempt.severity.value,
                matched_text=attempt.matched[:400],
                intent=attempt.intent,
                invocation_id=invocation_id,
            )
        )
    await session.flush()
    if commit:
        await session.commit()
    return len(attempts)


@dataclass(slots=True)
class Grounding:
    """Everything `prepare` established, for `conclude` to finish."""

    retrieved: RetrievalResult
    refusal: refusal_engine.RefusalOutcome
    attempts: list[injection_engine.InjectionAttempt]
    prompt: str
    started: datetime


async def prepare(
    session: AsyncSession,
    user: User,
    *,
    agent_key: str,
    question: str,
    sources: tuple[str, ...],
    system_rules: str,
    model: str = "unwired",
    untrusted_sections: tuple[tuple[str, str], ...] = (),
    employee_ids: tuple[str, ...] = (),
    requested_employee_id: str | None = None,
    now: datetime | None = None,
) -> Grounding | dict[str, Any]:
    """The steps before the model: retrieve, refuse what should be refused, fence.

    A dict back means the pipeline has already answered — with a refusal — and
    logged it; the caller returns it as is and calls no model.
    """
    started = now or datetime.now(UTC)

    try:
        retrieved = await retrieve(
            session, user, sources=sources, employee_ids=employee_ids
        )
    except RetrievalError as exc:
        return await _log_and_return(
            session,
            user,
            agent_key=agent_key,
            question=question,
            system_rules=system_rules,
            model=model,
            scope=await resolve_scope(session, user),
            retrieved=None,
            outcome=InvocationOutcome.REFUSED,
            raw_output=None,
            rendered=refusal_engine.RefusalReason.FORBIDDEN_TOPIC.message,
            refusal_reasons=[refusal_engine.RefusalReason.FORBIDDEN_TOPIC.value],
            started=started,
            detail=str(exc),
        )

    refusal = refusal_engine.classify(
        question,
        requested_employee_id=requested_employee_id,
        scope_covers=(
            requested_employee_id is None
            or retrieved.scope.covers(requested_employee_id)
        ),
        data_available=bool(retrieved.records) or not sources,
        touches_forbidden_topic=False,
    )

    if refusal.is_total:
        return await _log_and_return(
            session,
            user,
            agent_key=agent_key,
            question=question,
            system_rules=system_rules,
            model=model,
            scope=retrieved.scope,
            retrieved=retrieved,
            outcome=InvocationOutcome.REFUSED,
            raw_output=None,
            rendered=refusal.message(),
            refusal_reasons=[r.value for r in refusal.reasons],
            started=started,
        )

    attempts: list[injection_engine.InjectionAttempt] = []
    for label, content in untrusted_sections:
        attempts.extend(injection_engine.detect(content, field=label))

    prompt = injection_engine.build_prompt(
        system_rules=system_rules,
        untrusted_sections=list(untrusted_sections),
        grounding_blocks=[r.as_prompt_block() for r in retrieved.records],
    )
    return Grounding(
        retrieved=retrieved,
        refusal=refusal,
        attempts=attempts,
        prompt=prompt,
        started=started,
    )


async def conclude(
    session: AsyncSession,
    user: User,
    grounding: Grounding,
    *,
    agent_key: str,
    question: str,
    system_rules: str,
    model: str,
    raw_text: str,
    confidence: float | None = None,
    tokens: int = 0,
    sub_processor_key: str | None = None,
) -> dict[str, Any]:
    """The steps after the model: verify the citations, check inference, log."""
    retrieved = grounding.retrieved
    refusal = grounding.refusal

    existing_ids = retrieved.citation_ids
    verified = citation_engine.verify(
        raw_text,
        retrieved_ids=retrieved.citation_ids,
        existing_ids=existing_ids,
        forbidden_sources=FORBIDDEN_SOURCES,
    )

    rendered = verified.text
    outcome = (
        InvocationOutcome.WITHHELD
        if not verified.may_render
        else (
            InvocationOutcome.PARTIALLY_ANSWERED
            if refusal.reasons
            else InvocationOutcome.ANSWERED
        )
    )

    if verified.may_render:
        try:
            sensitive_engine.assert_no_cross_topic_inference(rendered)
        except ValueError as exc:
            outcome = InvocationOutcome.WITHHELD
            rendered = (
                f"{sensitive_engine.safe_summary_note()}\n\n"
                f"(Withheld: {exc})"
            )
        else:
            rendered = refusal_engine.split_response(
                factual_answer=rendered, outcome=refusal
            )

    band = None
    if confidence is not None:
        band = confidence_engine.band_for(confidence).value

    return await _log_and_return(
        session,
        user,
        agent_key=agent_key,
        question=question,
        system_rules=system_rules,
        model=model,
        scope=retrieved.scope,
        retrieved=retrieved,
        outcome=outcome,
        raw_output=raw_text,
        rendered=rendered,
        refusal_reasons=[r.value for r in refusal.reasons],
        started=grounding.started,
        verified=verified,
        confidence=confidence,
        confidence_band=band,
        injection_attempts=grounding.attempts,
        tokens=tokens,
        sub_processor_key=sub_processor_key,
    )


async def ground_and_verify(
    session: AsyncSession,
    user: User,
    *,
    agent_key: str,
    question: str,
    sources: tuple[str, ...],
    answer_fn: Any,
    system_rules: str,
    model: str = "unwired",
    untrusted_sections: tuple[tuple[str, str], ...] = (),
    employee_ids: tuple[str, ...] = (),
    requested_employee_id: str | None = None,
    confidence: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The whole pipeline, in the order it has to happen.

    Retrieve, refuse what should be refused, fence the untrusted text, call the
    agent, verify the citations, check for a cross-topic inference, and log all
    of it. `answer_fn` is a callable the caller supplies — sync or async — and
    this module never constructs an agent, which is what lets the pipeline be
    tested end to end against a deterministic function.
    """
    grounding = await prepare(
        session,
        user,
        agent_key=agent_key,
        question=question,
        sources=sources,
        system_rules=system_rules,
        model=model,
        untrusted_sections=untrusted_sections,
        employee_ids=employee_ids,
        requested_employee_id=requested_employee_id,
        now=now,
    )
    if isinstance(grounding, dict):
        return grounding

    raw = answer_fn(grounding.prompt)
    if inspect.isawaitable(raw):
        raw = await raw
    raw_text = raw if isinstance(raw, str) else str(raw)

    return await conclude(
        session,
        user,
        grounding,
        agent_key=agent_key,
        question=question,
        system_rules=system_rules,
        model=model,
        raw_text=raw_text,
        confidence=confidence,
    )


async def log_refusal(
    session: AsyncSession,
    user: User,
    grounding: Grounding,
    *,
    agent_key: str,
    question: str,
    system_rules: str,
    model: str,
    rendered: str,
    raw_output: str | None,
    reason: refusal_engine.RefusalReason,
    tokens: int = 0,
    sub_processor_key: str | None = None,
) -> dict[str, Any]:
    """Log a question the agent said its records could not answer."""
    return await _log_and_return(
        session,
        user,
        agent_key=agent_key,
        question=question,
        system_rules=system_rules,
        model=model,
        scope=grounding.retrieved.scope,
        retrieved=grounding.retrieved,
        outcome=InvocationOutcome.REFUSED,
        raw_output=raw_output,
        rendered=rendered,
        refusal_reasons=[reason.value],
        started=grounding.started,
        injection_attempts=grounding.attempts,
        tokens=tokens,
        sub_processor_key=sub_processor_key,
    )


async def log_local_answer(
    session: AsyncSession,
    user: User,
    grounding: Grounding,
    *,
    agent_key: str,
    question: str,
    system_rules: str,
    model: str,
    rendered: str,
    citations: list[str],
    tokens: int = 0,
    sub_processor_key: str | None = None,
) -> dict[str, Any]:
    """Log an answer AskMira composed itself from its own records.

    The model decided *what* was being asked; the facts in the answer were read
    by code and never sent to the model. Its citations are the records it read,
    verified by construction.
    """
    return await _log_and_return(
        session,
        user,
        agent_key=agent_key,
        question=question,
        system_rules=system_rules,
        model=model,
        scope=grounding.retrieved.scope,
        retrieved=grounding.retrieved,
        outcome=InvocationOutcome.ANSWERED,
        raw_output=None,
        rendered=rendered,
        refusal_reasons=[],
        started=grounding.started,
        injection_attempts=grounding.attempts,
        tokens=tokens,
        sub_processor_key=sub_processor_key,
        local_citations=citations,
    )


async def _log_and_return(
    session: AsyncSession,
    user: User,
    *,
    agent_key: str,
    question: str,
    system_rules: str,
    model: str,
    scope: RetrievalScope,
    retrieved: RetrievalResult | None,
    outcome: InvocationOutcome,
    raw_output: str | None,
    rendered: str,
    refusal_reasons: list[str],
    started: datetime,
    verified: citation_engine.VerifiedResponse | None = None,
    confidence: float | None = None,
    confidence_band: str | None = None,
    injection_attempts: list[injection_engine.InjectionAttempt] | None = None,
    detail: str | None = None,
    tokens: int = 0,
    sub_processor_key: str | None = None,
    local_citations: list[str] | None = None,
) -> dict[str, Any]:
    completed = datetime.now(UTC)
    citations = (
        list(local_citations)
        if local_citations is not None
        else (list(verified.citations) if verified else [])
    )
    citations_verified = local_citations is not None or bool(
        verified and verified.may_render
    )
    row = AgentInvocation(
        tenant_id=user.tenant_id,
        agent_key=agent_key,
        prompt_fingerprint=_prompt_fingerprint(system_rules),
        model=model,
        requested_by_user_id=user.id,
        scope_kind=scope.kind.value,
        scope_employee_count=len(scope.employee_ids),
        question=question,
        retrieved_record_ids=(
            sorted(retrieved.citation_ids) if retrieved else []
        ),
        refused_sources=list(retrieved.refused_sources) if retrieved else [],
        outcome=outcome,
        raw_output=raw_output,
        rendered_output=rendered,
        citations=citations,
        citation_failures=(
            [f.as_dict() for f in verified.failures] if verified else []
        ),
        citations_verified=citations_verified,
        refusal_reasons=refusal_reasons,
        confidence=confidence,
        confidence_band=confidence_band,
        latency_ms=(completed - started).total_seconds() * 1000,
        tokens=tokens,
        sub_processor_key=sub_processor_key,
        completed_at=completed,
    )
    session.add(row)
    await session.flush()

    if injection_attempts:
        await log_injection_attempts(
            session,
            user,
            attempts=injection_attempts,
            invocation_id=row.id,
            commit=False,
        )

    await session.commit()
    await session.refresh(row)

    return {
        "invocation_id": str(row.id),
        "outcome": outcome.value,
        # The only text a caller should render. `raw_output` is in the log for
        # debugging and is deliberately not returned — a response shape that
        # offered both would eventually have a call site pick the wrong one.
        "answer": rendered,
        "citations": citations,
        "citation_failures": (
            [f.as_dict() for f in verified.failures] if verified else []
        ),
        "refusal_reasons": refusal_reasons,
        "confidence": confidence,
        "confidence_band": confidence_band,
        "retrieved_record_count": len(retrieved.records) if retrieved else 0,
        "refused_sources": list(retrieved.refused_sources) if retrieved else [],
        "injection_attempts": (
            [a.as_dict() for a in injection_attempts] if injection_attempts else []
        ),
        "scope": scope.as_dict(),
        "detail": detail,
    }


async def invocation_log(
    session: AsyncSession,
    user: User,
    *,
    agent_key: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """What the AI Trust Center publishes from."""
    stmt = tenant_scoped(select(AgentInvocation), AgentInvocation, user)
    if agent_key:
        stmt = stmt.where(AgentInvocation.agent_key == agent_key)
    rows = (
        (
            await session.execute(
                stmt.order_by(AgentInvocation.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return {
        "invocations": [
            {
                "id": str(row.id),
                "agent_key": row.agent_key,
                "prompt_fingerprint": row.prompt_fingerprint,
                "model": row.model,
                "scope_kind": row.scope_kind,
                "outcome": row.outcome.value,
                "citations": row.citations,
                "citation_failures": row.citation_failures,
                "citations_verified": row.citations_verified,
                "refusal_reasons": row.refusal_reasons,
                "confidence": float(row.confidence) if row.confidence else None,
                "confidence_band": row.confidence_band,
                "retrieved_record_count": len(row.retrieved_record_ids),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "not_built": (
            "The assistant answers in AskMira only. Slack, Teams and WhatsApp "
            "channels are not built, so no question asked there appears here."
        ),
    }
