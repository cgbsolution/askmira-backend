"""What blocks a merge, what warns, and what is refused before it can run.

═══════════════════════════════════════════════════════════════════════════════
SEVERITY IS A PROPERTY OF THE ASSERTION KIND, NOT OF THE CASE
═══════════════════════════════════════════════════════════════════════════════

If a case can declare its own severity, the first person shipping under a
deadline downgrades the discrimination check on the agent that most needs it,
and there is a plausible sentence in the pull request explaining why. So the
mapping lives here, once, and applies to every suite.

- **A failed regression case blocks, always.** It is tagged to a defect that
  actually happened; a merge that reintroduces it is a merge that ships the
  incident again.
- **Discrimination, monotonicity, grounding and refusal block.** These are the
  classes where a failure means the output is wrong in a way the user cannot
  see — a score that does not discriminate, a citation that does not hold, a
  refusal that leaks.
- **Open-ended quality warns.** An LLM-judge score dropping below a rubric
  threshold is worth knowing and is not worth blocking a release on a second
  model's opinion.
- **An error blocks.** An agent that raised or a suite that is malformed is not
  a passing run, and treating "could not evaluate" as anything other than a
  block is how a broken suite becomes a green pipeline.

═══════════════════════════════════════════════════════════════════════════════
A SCORING AGENT WITHOUT A DISCRIMINATION ASSERTION IS REFUSED
═══════════════════════════════════════════════════════════════════════════════

Not warned about. `missing_discrimination` is checked before a run is accepted,
and an invariant runs it over every registered suite — because the failure this
whole slice exists to catch got through precisely by nobody writing the check.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from app.services.agenteval.assertions import AssertionKind
from app.services.agenteval.runner import RunDiff, RunReport
from app.services.agenteval.suites import SUITES, EvalSuite

__all__ = [
    "BLOCKING_KINDS",
    "GateDecision",
    "GateOutcome",
    "SuiteDefect",
    "evaluate_gate",
    "missing_discrimination",
    "suite_defects",
]


#: Assertion kinds whose failure blocks a merge. Everything not in here warns.
#:
#: Grounding and refusal are here because their failure modes are invisible to
#: the person reading the output: a citation that resolves to the wrong record
#: reads exactly like one that resolves to the right one.
BLOCKING_KINDS: frozenset[AssertionKind] = frozenset(
    {
        AssertionKind.DISCRIMINATION,
        AssertionKind.MONOTONICITY,
        AssertionKind.GROUNDING,
        AssertionKind.REFUSAL,
        AssertionKind.EXACT,
        AssertionKind.STRUCTURAL,
        AssertionKind.NUMERIC_RANGE,
        AssertionKind.NOT_CONTAINS,
        AssertionKind.CONSISTENCY,
    }
)

#: Kinds whose failure is reported and does not block.
WARNING_KINDS: frozenset[AssertionKind] = frozenset(
    {AssertionKind.LLM_JUDGE, AssertionKind.CONTAINS}
)


class GateOutcome(str, enum.Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"

    @property
    def allows_merge(self) -> bool:
        return self is not GateOutcome.BLOCK


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether this run lets a change through, and why not."""

    outcome: GateOutcome
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "allows_merge": self.outcome.allows_merge,
            "blocking_reasons": list(self.blocking_reasons),
            "warnings": list(self.warnings),
        }


def evaluate_gate(report: RunReport, *, diff: RunDiff | None = None) -> GateDecision:
    """Decide whether this run permits a merge.

    A diff is optional and adds warnings only — a metric that moved is worth
    saying out loud and is not on its own a reason to block, because the
    threshold that would make it one is the assertion, and the assertion still
    passed.
    """
    blocking: list[str] = []
    warnings: list[str] = []

    for result in report.results:
        if result.error:
            blocking.append(
                f"{result.case_name} could not be evaluated: {result.error}. A "
                "case that did not run is not a case that passed."
            )
            continue
        if result.passed:
            continue

        if result.is_regression_case:
            blocking.append(
                f"{result.case_name} is a regression case for "
                f"{result.source_incident} and failed: {result.failure_reason} "
                "Merging this reintroduces a defect that already reached "
                "production once."
            )
            continue

        blocked_here = [v for v in result.verdicts if not v.passed and v.kind in BLOCKING_KINDS]
        if blocked_here:
            blocking.append(
                f"{result.case_name} failed "
                f"{', '.join(v.kind.value for v in blocked_here)}: "
                f"{result.failure_reason}"
            )
        else:
            warnings.append(f"{result.case_name} failed: {result.failure_reason}")

    if diff is not None:
        for move in diff.moved:
            warnings.append(
                f"{move.key} moved from {move.before:.4g} to {move.after:.4g} "
                f"({move.relative:+.1%}). The assertion still passes; this is "
                "the shape of a slow degradation."
            )
        if diff.cost_delta_inr > 0:
            warnings.append(f"cost rose by ₹{diff.cost_delta_inr:.4f} across the suite.")

    if blocking:
        outcome = GateOutcome.BLOCK
    elif warnings:
        outcome = GateOutcome.WARN
    else:
        outcome = GateOutcome.PASS

    return GateDecision(
        outcome=outcome,
        blocking_reasons=tuple(blocking),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class SuiteDefect:
    """Something wrong with a suite itself, rather than with an agent."""

    agent_key: str
    problem: str

    def __str__(self) -> str:
        return f"{self.agent_key}: {self.problem}"


def missing_discrimination(suite: EvalSuite) -> SuiteDefect | None:
    """Refuse a scoring suite that has no discrimination assertion.

    The single most important check in this module. The Fit Score defect reached
    a benchmark because no test compared outputs across candidates, and a
    harness that permits a scoring suite without that assertion has reproduced
    the conditions exactly.
    """
    if not suite.is_scoring_agent:
        return None

    has_it = any(
        assertion.kind is AssertionKind.DISCRIMINATION
        for case in suite.cases
        for assertion in case.assertions
    )
    if has_it:
        return None

    return SuiteDefect(
        agent_key=suite.agent_key,
        problem=(
            "is a scoring agent with no discrimination assertion. A scorer that "
            "returns near-identical values for every input passes every other "
            "check in this harness — that is exactly how the Fit Score defect "
            "reached a benchmark."
        ),
    )


def _judge_only_on_consequential_output(suite: EvalSuite) -> list[SuiteDefect]:
    """A consequential output checked only by a second model is not checked."""
    out: list[SuiteDefect] = []
    for case in suite.cases:
        kinds = {a.kind for a in case.assertions}
        if kinds == {AssertionKind.LLM_JUDGE}:
            out.append(
                SuiteDefect(
                    agent_key=suite.agent_key,
                    problem=(
                        f"case {case.name!r} is checked by an LLM judge and "
                        "nothing else. A judge is a second model with the same "
                        "failure modes as the first; it is a supplement, never "
                        "the only assertion on a consequential output."
                    ),
                )
            )
    return out


def suite_defects(suite: EvalSuite) -> list[SuiteDefect]:
    """Everything wrong with one suite. Read by an invariant over every suite."""
    defects: list[SuiteDefect] = []

    missing = missing_discrimination(suite)
    if missing is not None:
        defects.append(missing)

    defects.extend(_judge_only_on_consequential_output(suite))

    if not suite.cases:
        defects.append(
            SuiteDefect(
                agent_key=suite.agent_key,
                problem=(
                    "has no cases. An empty suite passes every run and proves "
                    "nothing, which is worse than no suite because it looks "
                    "like coverage."
                ),
            )
        )

    for case in suite.cases:
        if not case.assertions:
            defects.append(
                SuiteDefect(
                    agent_key=suite.agent_key,
                    problem=f"case {case.name!r} has no assertions.",
                )
            )
        for assertion in case.assertions:
            if assertion.kind.compares_across_inputs and len(case.inputs) < 2:
                defects.append(
                    SuiteDefect(
                        agent_key=suite.agent_key,
                        problem=(
                            f"case {case.name!r} has a "
                            f"{assertion.kind.value} assertion and only "
                            f"{len(case.inputs)} input(s). It cannot compare "
                            "across inputs and would pass vacuously."
                        ),
                    )
                )
            if not assertion.rationale:
                defects.append(
                    SuiteDefect(
                        agent_key=suite.agent_key,
                        problem=(
                            f"case {case.name!r} has a "
                            f"{assertion.kind.value} assertion with no "
                            "rationale. Whoever sees this go red needs to know "
                            "whether it matters."
                        ),
                    )
                )

    return defects


def all_suite_defects() -> list[SuiteDefect]:
    """Every defect across every registered suite. The build gate."""
    return [d for suite in SUITES.values() for d in suite_defects(suite)]
