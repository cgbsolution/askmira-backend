"""Running a suite: results, cost, latency, and the diff against last time.

═══════════════════════════════════════════════════════════════════════════════
A PASS RATE IS NOT A RESULT
═══════════════════════════════════════════════════════════════════════════════

"14 of 15 passed" tells you nothing about whether the agent is getting better or
worse. The discrimination spread drifting from 34 points to 21 over four prompt
changes is a suite that passes every time and an agent quietly failing — and by
the time it crosses the threshold, the change that caused it is months back.

So every verdict carries its measurements, and `compare` diffs a run against the
previous one: newly failing, newly passing, and **numeric measurements that
moved**. The third is the one that catches a slow degradation.

═══════════════════════════════════════════════════════════════════════════════
COST AND LATENCY SIT IN THE SAME REPORT AS QUALITY
═══════════════════════════════════════════════════════════════════════════════

A prompt change that improves the judge score and triples the token count is not
obviously an improvement, and if the two numbers live on different dashboards
nobody makes that trade deliberately. Both are recorded per case and totalled
per run.

Pure. The agent is a callable the caller supplies; this module never constructs
one, never touches a session, and never calls a model.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.agenteval.assertions import (
    AssertionKind,
    MalformedAssertionError,
    Verdict,
    check,
)
from app.services.agenteval.suites import EvalCase, EvalSuite

__all__ = [
    "CaseResult",
    "RunDiff",
    "RunReport",
    "compare",
    "run_suite",
]

#: An agent is any callable from an input dict to an output. Deliberately not a
#: class or a protocol with a `.run()`: the harness has to be able to evaluate a
#: plain function, a model wrapper and a lambda in a test without adapters.
Agent = Callable[[dict[str, Any]], Any]

#: Optional per-call cost accounting, supplied by whatever made the call. The
#: runner does not estimate tokens — an estimate in a margin model is a number
#: somebody will treat as measured.
CostFn = Callable[[dict[str, Any], Any], tuple[int, float]]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case, its verdicts, and what it cost."""

    case_name: str
    passed: bool
    verdicts: tuple[Verdict, ...]
    outputs: tuple[Any, ...]
    latency_ms: float
    tokens: int
    cost_inr: float
    is_regression_case: bool
    source_incident: str | None
    tags: tuple[str, ...]
    #: Set where the case could not be evaluated — a malformed assertion, or an
    #: agent that raised. Distinct from a failure: this is a broken suite or a
    #: crashing agent, and lumping it in with real regressions hides both.
    error: str | None = None

    @property
    def failure_reason(self) -> str:
        if self.error:
            return self.error
        return "; ".join(v.reason for v in self.verdicts if not v.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case_name,
            "passed": self.passed,
            "error": self.error,
            "failure_reason": self.failure_reason,
            "is_regression_case": self.is_regression_case,
            "source_incident": self.source_incident,
            "tags": list(self.tags),
            "latency_ms": round(self.latency_ms, 2),
            "tokens": self.tokens,
            "cost_inr": round(self.cost_inr, 4),
            "verdicts": [v.as_dict() for v in self.verdicts],
        }


@dataclass(frozen=True, slots=True)
class RunReport:
    """One execution of one suite against one agent version."""

    agent_key: str
    agent_fingerprint: str
    suite_version: int
    results: tuple[CaseResult, ...]
    total_latency_ms: float
    total_tokens: int
    total_cost_inr: float
    triggered_by: str = "manual"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def errored(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if r.error)

    @property
    def failed_regressions(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if not r.passed and r.is_regression_case)

    def measurements(self) -> dict[str, float]:
        """Every numeric measurement in the run, keyed by case and metric.

        This is what `compare` diffs. Flattened rather than nested so a
        comparison is a dict lookup and a new metric appearing does not need a
        schema change.
        """
        out: dict[str, float] = {}
        for result in self.results:
            for verdict in result.verdicts:
                for key, value in verdict.measured.items():
                    if isinstance(value, bool) or not isinstance(value, int | float):
                        continue
                    out[f"{result.case_name}.{verdict.kind.value}.{key}"] = float(value)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_key": self.agent_key,
            "agent_fingerprint": self.agent_fingerprint,
            "suite_version": self.suite_version,
            "triggered_by": self.triggered_by,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "error_count": len(self.errored),
            "failed_regression_count": len(self.failed_regressions),
            "total_latency_ms": round(self.total_latency_ms, 2),
            "total_tokens": self.total_tokens,
            "total_cost_inr": round(self.total_cost_inr, 4),
            "metrics": self.measurements(),
            "results": [r.as_dict() for r in self.results],
        }


def _run_case(
    case: EvalCase,
    agent: Agent,
    *,
    cost_fn: CostFn | None,
    judge_scores: dict[str, float],
) -> CaseResult:
    outputs: list[Any] = []
    tokens = 0
    cost = 0.0

    started = time.perf_counter()
    try:
        for payload in case.inputs:
            output = agent(payload)
            outputs.append(output)
            if cost_fn is not None:
                case_tokens, case_cost = cost_fn(payload, output)
                tokens += case_tokens
                cost += case_cost
    except Exception as exc:
        # An agent that raises is an error, not a failure. Reporting it as a
        # failing case would put a crash in the same list as a regression, and
        # the two need different responses.
        elapsed = (time.perf_counter() - started) * 1000
        return CaseResult(
            case_name=case.name,
            passed=False,
            verdicts=(),
            outputs=tuple(outputs),
            latency_ms=elapsed,
            tokens=tokens,
            cost_inr=cost,
            is_regression_case=case.is_regression_case,
            source_incident=case.source_incident,
            tags=case.tags,
            error=f"the agent raised {type(exc).__name__}: {exc}",
        )
    elapsed = (time.perf_counter() - started) * 1000

    verdicts: list[Verdict] = []
    error: str | None = None
    for spec in case.assertions:
        effective = spec
        if spec.kind is AssertionKind.LLM_JUDGE and case.name in judge_scores:
            effective = type(spec)(
                kind=spec.kind,
                config={**spec.config, "judge_score": judge_scores[case.name]},
                rationale=spec.rationale,
            )
        try:
            verdicts.append(check(effective, outputs))
        except MalformedAssertionError as exc:
            error = f"{spec.kind.value} assertion could not be evaluated: {exc}"
            break

    passed = error is None and all(v.passed for v in verdicts)
    return CaseResult(
        case_name=case.name,
        passed=passed,
        verdicts=tuple(verdicts),
        outputs=tuple(outputs),
        latency_ms=elapsed,
        tokens=tokens,
        cost_inr=cost,
        is_regression_case=case.is_regression_case,
        source_incident=case.source_incident,
        tags=case.tags,
        error=error,
    )


def run_suite(
    suite: EvalSuite,
    agent: Agent,
    *,
    agent_fingerprint: str,
    cost_fn: CostFn | None = None,
    judge_scores: dict[str, float] | None = None,
    triggered_by: str = "manual",
    only_tags: Sequence[str] = (),
) -> RunReport:
    """Run every case in a suite against one agent.

    `only_tags` filters, and it deliberately **cannot filter out a regression
    case**. Tag filtering exists so somebody can iterate on the grounding cases
    without waiting for the rest; letting it skip a regression case would make
    "I ran the suite" mean something different depending on the flags, which is
    how a blocked merge becomes an unblocked one.
    """
    cases = suite.cases
    if only_tags:
        wanted = set(only_tags)
        cases = tuple(c for c in suite.cases if c.is_regression_case or (set(c.tags) & wanted))

    results = [
        _run_case(case, agent, cost_fn=cost_fn, judge_scores=judge_scores or {}) for case in cases
    ]

    return RunReport(
        agent_key=suite.agent_key,
        agent_fingerprint=agent_fingerprint,
        suite_version=suite.version,
        results=tuple(results),
        total_latency_ms=sum(r.latency_ms for r in results),
        total_tokens=sum(r.tokens for r in results),
        total_cost_inr=sum(r.cost_inr for r in results),
        triggered_by=triggered_by,
    )


@dataclass(frozen=True, slots=True)
class MetricMove:
    """One measurement that changed between runs."""

    key: str
    before: float
    after: float

    @property
    def delta(self) -> float:
        return self.after - self.before

    @property
    def relative(self) -> float:
        return self.delta / abs(self.before) if self.before else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.key,
            "before": round(self.before, 4),
            "after": round(self.after, 4),
            "delta": round(self.delta, 4),
            "relative": round(self.relative, 4),
        }


@dataclass(frozen=True, slots=True)
class RunDiff:
    """This run against the last one.

    `moved` is why this class exists. Newly-failing and newly-passing are
    obvious and every harness reports them; a discrimination spread sliding from
    34 to 21 while the assertion still passes is the one that gets shipped.
    """

    newly_failing: tuple[str, ...]
    newly_passing: tuple[str, ...]
    moved: tuple[MetricMove, ...]
    cost_delta_inr: float
    latency_delta_ms: float
    token_delta: int

    @property
    def is_clean(self) -> bool:
        return not self.newly_failing

    def as_dict(self) -> dict[str, Any]:
        return {
            "newly_failing": list(self.newly_failing),
            "newly_passing": list(self.newly_passing),
            "metrics_moved": [m.as_dict() for m in self.moved],
            "cost_delta_inr": round(self.cost_delta_inr, 4),
            "latency_delta_ms": round(self.latency_delta_ms, 2),
            "token_delta": self.token_delta,
        }


def compare(previous: RunReport, current: RunReport, *, move_threshold: float = 0.10) -> RunDiff:
    """Diff two runs, including the measurements that moved.

    `move_threshold` is relative, so a 10% move is 10% whether the metric is a
    spread out of 100 or a coefficient of variation. An absolute threshold would
    be noise on one and blind on the other.
    """
    before = {r.case_name: r.passed for r in previous.results}
    after = {r.case_name: r.passed for r in current.results}

    newly_failing = tuple(sorted(name for name, ok in after.items() if not ok and before.get(name)))
    newly_passing = tuple(
        sorted(name for name, ok in after.items() if ok and name in before and not before[name])
    )

    before_metrics = previous.measurements()
    after_metrics = current.measurements()
    moved = tuple(
        sorted(
            (
                MetricMove(key=key, before=before_metrics[key], after=value)
                for key, value in after_metrics.items()
                if key in before_metrics
                and abs(value - before_metrics[key]) > abs(before_metrics[key]) * move_threshold
            ),
            key=lambda m: -abs(m.relative),
        )
    )

    return RunDiff(
        newly_failing=newly_failing,
        newly_passing=newly_passing,
        moved=moved,
        cost_delta_inr=current.total_cost_inr - previous.total_cost_inr,
        latency_delta_ms=current.total_latency_ms - previous.total_latency_ms,
        token_delta=current.total_tokens - previous.total_tokens,
    )
