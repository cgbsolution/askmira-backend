"""The DB-facing half of agent evaluation.

Sessions, models and transactions. The assertions, the gating rules, the drift
maths and the golden suites live in the pure modules beside this — an invariant
checks none of them imports SQLAlchemy, because the assertion library has to be
runnable in CI against a branch with no database.

**A refused suite is refused at write time.** `register_suite` calls
`gating.suite_defects` and raises rather than storing a scoring suite with no
discrimination assertion. Storing it and reporting the defect later would give
somebody a green run against a suite that cannot catch the one failure it exists
for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.agenteval import (
    AgentFeedback,
    EvalCaseRow,
    EvalGateOutcome,
    EvalResultRow,
    EvalRunRow,
    EvalSuiteRow,
    EvalTrigger,
)
from app.models.audit import AuditAction
from app.models.user import User
from app.services import audit
from app.services.agenteval import fixtures
from app.services.agenteval.assertions import AssertionKind, AssertionSpec
from app.services.agenteval.gating import evaluate_gate, suite_defects
from app.services.agenteval.runner import RunReport, compare, run_suite
from app.services.agenteval.suites import SUITES, EvalCase, EvalSuite

__all__ = [
    "capture_feedback",
    "execute_suite",
    "latest_run",
    "promote_feedback_to_case",
    "register_suite",
    "seed_canonical_suites",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _to_engine_suite(
    row: EvalSuiteRow, case_rows: list[EvalCaseRow]
) -> EvalSuite:
    """Rebuild the pure suite from its stored rows.

    Takes the case rows explicitly rather than reading `row.cases`. A
    relationship loaded earlier in the same session does not see a case added
    since, so a suite could run without a case that had just been promoted into
    it — the run would be green and would have skipped the check somebody added
    precisely because it mattered.

    The engine never sees a model. That is what lets `run_suite` be tested
    against a hand-written suite and run in CI with no database at all.
    """
    cases: list[EvalCase] = []
    for case_row in case_rows:
        specs: list[AssertionSpec] = []
        for raw in case_row.assertions or []:
            try:
                kind = AssertionKind(raw["kind"])
            except (KeyError, ValueError) as exc:
                raise _bad_request(
                    f"Case {case_row.name!r} has an assertion with an unknown "
                    f"kind {raw.get('kind')!r}. A stored case that cannot be "
                    "parsed must not be skipped — it would be a silent gap in "
                    "the suite."
                ) from exc
            specs.append(
                AssertionSpec(
                    kind=kind,
                    config=raw.get("config", {}),
                    rationale=raw.get("rationale", ""),
                )
            )
        cases.append(
            EvalCase(
                name=case_row.name,
                inputs=tuple(case_row.inputs or []),
                assertions=tuple(specs),
                tags=tuple(case_row.tags or []),
                source_incident=case_row.source_incident_id,
                description=case_row.description,
            )
        )

    return EvalSuite(
        agent_key=row.agent_key,
        name=row.name,
        description=row.description,
        version=row.version,
        cases=tuple(cases),
        is_scoring_agent=row.is_scoring_agent,
    )


async def register_suite(
    session: AsyncSession,
    user: User,
    *,
    suite: EvalSuite,
    request: Request | None = None,
) -> EvalSuiteRow:
    """Store a suite, refusing one that cannot catch what it exists to catch.

    The refusal is the point. A scoring agent's suite with no discrimination
    assertion passes every run and would have passed the Fit Score defect;
    accepting it and flagging the problem on a dashboard reproduces the original
    conditions with a warning nobody read.
    """
    defects = suite_defects(suite)
    if defects:
        raise _bad_request(
            "This suite is refused:\n  - "
            + "\n  - ".join(d.problem for d in defects)
        )

    stmt = tenant_scoped(
        select(EvalSuiteRow).where(EvalSuiteRow.agent_key == suite.agent_key),
        EvalSuiteRow,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = EvalSuiteRow(tenant_id=user.tenant_id, agent_key=suite.agent_key)
        session.add(row)

    # Every column set before the flush. The first version added the row and
    # flushed immediately to get its id for the cases, which sent an INSERT with
    # a null `name` — the not-null constraint caught it, but only in an
    # integration test, because nothing in the unit suite writes a row.
    row.name = suite.name
    row.description = suite.description
    row.version = suite.version
    row.is_scoring_agent = suite.is_scoring_agent
    await session.flush()

    # Queried, not read off `row.cases`. A relationship on a row that was just
    # flushed is unloaded, and touching it emits a synchronous SELECT — which is
    # a MissingGreenlet on an async session, and only ever surfaces against a
    # real database.
    existing_rows = (
        (
            await session.execute(
                select(EvalCaseRow).where(EvalCaseRow.suite_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    existing = {case.name: case for case in existing_rows}
    for case in suite.cases:
        case_row = existing.get(case.name)
        if case_row is None:
            case_row = EvalCaseRow(
                tenant_id=user.tenant_id, suite_id=row.id, name=case.name
            )
            session.add(case_row)
        case_row.description = case.description
        case_row.inputs = list(case.inputs)
        case_row.assertions = [a.as_dict() for a in case.assertions]
        case_row.tags = list(case.tags)
        case_row.is_regression_case = case.is_regression_case
        case_row.source_incident_id = case.source_incident

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="eval_suite",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Eval suite for {suite.agent_key} registered at v{suite.version} "
            f"with {len(suite.cases)} case(s)."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def seed_canonical_suites(
    session: AsyncSession, user: User, *, request: Request | None = None
) -> list[str]:
    """Write the suites defined in code for every agent named in the brief.

    Backfill before new work: the brief's point is that suites for the agents
    that already exist are worth more than suites for the ones nobody has built.
    """
    written: list[str] = []
    for suite in SUITES.values():
        await register_suite(session, user, suite=suite, request=request)
        written.append(suite.agent_key)
    return written


async def _suite_row(
    session: AsyncSession, user: User, agent_key: str
) -> EvalSuiteRow:
    stmt = tenant_scoped(
        select(EvalSuiteRow).where(EvalSuiteRow.agent_key == agent_key),
        EvalSuiteRow,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found(
            f"No eval suite registered for {agent_key!r}. Seed the canonical "
            "suites or register one before running."
        )
    return row


async def latest_run(
    session: AsyncSession, user: User, *, agent_key: str
) -> EvalRunRow | None:
    stmt = (
        tenant_scoped(
            select(EvalRunRow).where(EvalRunRow.agent_key == agent_key),
            EvalRunRow,
            user,
        )
        .order_by(EvalRunRow.started_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _report_from_row(row: EvalRunRow) -> RunReport:
    """Rebuild enough of a report from a stored run to diff against it.

    Only the fields `compare` reads. A full reconstruction would need the
    verdict objects, and the stored measurements are what the diff is actually
    computed from.
    """
    from app.services.agenteval.runner import CaseResult

    results = tuple(
        CaseResult(
            case_name=r.case_name,
            passed=r.passed,
            verdicts=(),
            outputs=(),
            latency_ms=float(r.latency_ms),
            tokens=r.tokens,
            cost_inr=float(r.cost_inr),
            is_regression_case=r.is_regression_case,
            source_incident=r.source_incident_id,
            tags=(),
        )
        for r in row.results
    )
    report = RunReport(
        agent_key=row.agent_key,
        agent_fingerprint=row.agent_fingerprint,
        suite_version=row.suite_version,
        results=results,
        total_latency_ms=float(row.total_latency_ms),
        total_tokens=row.total_tokens,
        total_cost_inr=float(row.total_cost_inr),
    )
    # The stored metrics are authoritative — the rebuilt results carry no
    # verdicts, so `measurements()` would return an empty dict.
    report.metadata["stored_metrics"] = row.metrics or {}
    return report


async def execute_suite(
    session: AsyncSession,
    user: User,
    *,
    agent_key: str,
    triggered_by: EvalTrigger = EvalTrigger.MANUAL,
    only_tags: tuple[str, ...] = (),
    agent_override: str | None = None,
    request: Request | None = None,
    now: datetime | None = None,
) -> EvalRunRow:
    """Run a registered suite against the agent, record everything, gate.

    `agent_override` names a different registered implementation to run against
    — used to prove the harness catches a defect by running the current suite
    against `fit_score_defective`, which is the only honest way to demonstrate
    that with no production agent to break.
    """
    started = now or datetime.now(UTC)
    suite_row = await _suite_row(session, user, agent_key)
    case_rows = (
        (
            await session.execute(
                select(EvalCaseRow)
                .where(EvalCaseRow.suite_id == suite_row.id)
                .order_by(EvalCaseRow.created_at)
            )
        )
        .scalars()
        .all()
    )
    suite = _to_engine_suite(suite_row, list(case_rows))

    implementation_key = agent_override or agent_key
    agent = fixtures.AGENTS.get(implementation_key)
    if agent is None:
        raise _bad_request(
            f"No implementation registered for {implementation_key!r}. "
            f"Registered: {', '.join(sorted(fixtures.AGENTS))}."
        )

    report = run_suite(
        suite,
        agent,
        agent_fingerprint=fixtures.agent_fingerprint(implementation_key),
        triggered_by=triggered_by.value,
        only_tags=only_tags,
    )

    previous_row = await latest_run(session, user, agent_key=agent_key)
    diff = None
    if previous_row is not None:
        previous = _report_from_row(previous_row)
        # Diff the pass/fail transitions from the rebuilt results, and the
        # metric moves from the stored metrics, which is where they live.
        diff = compare(previous, report)
        stored = previous_row.metrics or {}
        if stored:
            from app.services.agenteval.runner import MetricMove, RunDiff

            current_metrics = report.measurements()
            moved = tuple(
                MetricMove(key=key, before=float(stored[key]), after=value)
                for key, value in current_metrics.items()
                if key in stored
                and abs(value - float(stored[key]))
                > abs(float(stored[key])) * 0.10
            )
            diff = RunDiff(
                newly_failing=diff.newly_failing,
                newly_passing=diff.newly_passing,
                moved=moved,
                cost_delta_inr=diff.cost_delta_inr,
                latency_delta_ms=diff.latency_delta_ms,
                token_delta=diff.token_delta,
            )

    decision = evaluate_gate(report, diff=diff)

    run_row = EvalRunRow(
        tenant_id=user.tenant_id,
        suite_id=suite_row.id,
        agent_key=agent_key,
        agent_fingerprint=report.agent_fingerprint,
        suite_version=report.suite_version,
        started_at=started,
        completed_at=datetime.now(UTC),
        triggered_by=triggered_by,
        pass_count=report.pass_count,
        fail_count=report.fail_count,
        error_count=len(report.errored),
        gate_outcome=EvalGateOutcome(decision.outcome.value),
        blocking_reasons=list(decision.blocking_reasons),
        warnings=list(decision.warnings),
        metrics=report.measurements(),
        diff_from_previous=diff.as_dict() if diff else {},
        total_latency_ms=report.total_latency_ms,
        total_tokens=report.total_tokens,
        total_cost_inr=report.total_cost_inr,
    )
    session.add(run_row)
    await session.flush()

    for result in report.results:
        session.add(
            EvalResultRow(
                tenant_id=user.tenant_id,
                run_id=run_row.id,
                case_name=result.case_name,
                passed=result.passed,
                is_regression_case=result.is_regression_case,
                source_incident_id=result.source_incident,
                actual_output=list(result.outputs),
                failure_reason=result.failure_reason or None,
                verdicts=[v.as_dict() for v in result.verdicts],
                measurements={
                    f"{v.kind.value}.{k}": val
                    for v in result.verdicts
                    for k, val in v.measured.items()
                    if isinstance(val, int | float) and not isinstance(val, bool)
                },
                latency_ms=result.latency_ms,
                tokens=result.tokens,
                cost_inr=result.cost_inr,
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.AI_ACTION,
        entity_type="eval_run",
        entity_id=run_row.id,
        actor=user,
        actor_type="system",
        summary=(
            f"Eval run for {agent_key} against {implementation_key}: "
            f"{report.pass_count} passed, {report.fail_count} failed, gate "
            f"{decision.outcome.value}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(run_row)
    return run_row


async def run_history(
    session: AsyncSession, user: User, *, agent_key: str, limit: int = 20
) -> dict[str, Any]:
    stmt = (
        tenant_scoped(
            select(EvalRunRow).where(EvalRunRow.agent_key == agent_key),
            EvalRunRow,
            user,
        )
        .order_by(EvalRunRow.started_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "agent_key": agent_key,
        "runs": [
            {
                "id": str(row.id),
                "started_at": row.started_at.isoformat(),
                "agent_fingerprint": row.agent_fingerprint,
                "suite_version": row.suite_version,
                "triggered_by": row.triggered_by.value,
                "pass_count": row.pass_count,
                "fail_count": row.fail_count,
                "error_count": row.error_count,
                "gate_outcome": row.gate_outcome.value,
                "allows_merge": row.gate_outcome is not EvalGateOutcome.BLOCK,
                "blocking_reasons": row.blocking_reasons,
                "warnings": row.warnings,
                "diff_from_previous": row.diff_from_previous,
                "total_tokens": row.total_tokens,
                "total_cost_inr": float(row.total_cost_inr),
                "total_latency_ms": float(row.total_latency_ms),
            }
            for row in rows
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Human feedback
# ══════════════════════════════════════════════════════════════════════════════


async def capture_feedback(
    session: AsyncSession,
    user: User,
    *,
    agent_key: str,
    agent_input: dict[str, Any],
    agent_output: dict[str, Any],
    was_useful: bool,
    comment: str | None = None,
    request: Request | None = None,
) -> AgentFeedback:
    """Record what a person thought of one live output.

    The input and the output are stored together, so turning this into a case
    later does not require anybody to reconstruct what the agent was given from
    memory — which is the step at which feedback normally stops becoming cases.
    """
    row = AgentFeedback(
        tenant_id=user.tenant_id,
        agent_key=agent_key,
        agent_input=agent_input,
        agent_output=agent_output,
        was_useful=was_useful,
        comment=comment,
        submitted_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await session.commit()
    await session.refresh(row)
    return row


async def promote_feedback_to_case(
    session: AsyncSession,
    user: User,
    *,
    feedback_id: uuid.UUID,
    case_name: str,
    assertions: list[dict[str, Any]],
    source_incident_id: str | None = None,
    request: Request | None = None,
) -> EvalCaseRow:
    """Turn a piece of feedback into a case in the suite.

    The route by which the suite stops being what somebody imagined could go
    wrong. `promoted_to_case_id` is written back onto the feedback, so "did we
    act on this" is answerable rather than a matter of faith.
    """
    stmt = tenant_scoped(
        select(AgentFeedback).where(AgentFeedback.id == feedback_id),
        AgentFeedback,
        user,
    )
    feedback = (await session.execute(stmt)).scalar_one_or_none()
    if feedback is None:
        raise _not_found("No such feedback.")
    if not assertions:
        raise _bad_request(
            "A case needs at least one assertion. A case with none passes every "
            "run and looks like coverage."
        )

    suite_row = await _suite_row(session, user, feedback.agent_key)
    case_row = EvalCaseRow(
        tenant_id=user.tenant_id,
        suite_id=suite_row.id,
        name=case_name,
        description=(feedback.comment or "Promoted from user feedback."),
        inputs=[feedback.agent_input],
        assertions=assertions,
        tags=["from_feedback"],
        is_regression_case=source_incident_id is not None,
        source_incident_id=source_incident_id,
    )
    session.add(case_row)
    await session.flush()

    feedback.promoted_to_case_id = case_row.id
    suite_row.version += 1

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="eval_case",
        entity_id=case_row.id,
        actor=user,
        summary=(
            f"Feedback promoted to eval case {case_name!r} on "
            f"{feedback.agent_key}; suite now v{suite_row.version}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(case_row)
    return case_row


async def coverage(session: AsyncSession, user: User) -> dict[str, Any]:
    """Which agents have suites, and what is missing.

    `agents_without_suites` is the list that matters. The brief's instruction
    was to backfill before building anything new, and a page listing only what
    is covered presents an uncovered agent as nothing to do.
    """
    stmt = tenant_scoped(select(EvalSuiteRow), EvalSuiteRow, user)
    rows = (await session.execute(stmt)).scalars().all()
    registered = {row.agent_key for row in rows}

    known_agents = {
        key for key in fixtures.AGENTS if not key.endswith("_defective")
    }

    return {
        "suites": [
            {
                "agent_key": row.agent_key,
                "name": row.name,
                "version": row.version,
                "is_scoring_agent": row.is_scoring_agent,
                "case_count": len(row.cases),
                "regression_case_count": sum(
                    1 for c in row.cases if c.is_regression_case
                ),
            }
            for row in rows
        ],
        "agents_without_suites": sorted(known_agents - registered),
        "not_built": (
            "No production AI agent exists in this codebase yet. The "
            "implementations these suites run against are reference "
            "implementations in services/agenteval/fixtures.py, including a "
            "reconstruction of the Fit Score defect. The suites are real; the "
            "agents they will guard are slices 28 and 29."
        ),
    }
