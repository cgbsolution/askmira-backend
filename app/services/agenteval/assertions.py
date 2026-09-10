"""The assertion types. Different agents fail differently.

═══════════════════════════════════════════════════════════════════════════════
DISCRIMINATION IS THE ONE THAT WOULD HAVE CAUGHT IT
═══════════════════════════════════════════════════════════════════════════════

The Fit Score defect was not "the score was wrong". Every individual score
looked plausible. The defect was that a candidate with a 19-point spread in
their round scores came out within a rounding error of one who did not — the
function returned something in the right range for every input and told you
nothing about any of them.

No exact-match test catches that. No contains test catches it. A human reviewing
five outputs individually does not catch it, because each one is defensible. It
is only visible when you compare outputs across inputs that should differ, which
is a shape of assertion most eval harnesses do not have.

`DISCRIMINATION` takes several cases that ought to produce different outputs and
fails if the spread between them is below a margin. It is mandatory on every
scoring agent — `gating.py` blocks a merge without one, rather than trusting
whoever writes the suite to remember.

═══════════════════════════════════════════════════════════════════════════════
WHY LLM-AS-JUDGE IS NEVER THE ONLY ASSERTION ON A CONSEQUENTIAL OUTPUT
═══════════════════════════════════════════════════════════════════════════════

A judge is a second model with the same failure modes as the first, including
the one above: asked to rate five summaries it will happily return 4, 4, 4, 4, 4.
Using it as the sole check on something that changes somebody's pay or their job
means the safety argument rests on a component nobody evaluated. It is a
supplement, and `gating.py` enforces that rather than recommending it.

Pure. Nothing here calls a model, a database or the network — an assertion is a
function from (expectation, actual outputs) to a verdict, which is what makes
the harness itself testable.
"""

from __future__ import annotations

import enum
import itertools
import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

__all__ = [
    "AssertionKind",
    "AssertionSpec",
    "MalformedAssertionError",
    "Verdict",
    "check",
]


class MalformedAssertionError(ValueError):
    """An assertion that cannot be evaluated as written.

    Distinct from an assertion that fails: a malformed expectation is a bug in
    the suite, and reporting it as a failing case would put it in the same list
    as a real regression.
    """


class Severity(str, enum.Enum):
    """What a failure does to the pipeline.

    Set per assertion kind in `gating.py`, not per case — otherwise the first
    person under deadline pressure downgrades the discrimination check on the
    agent that most needs it.
    """

    BLOCK = "block"
    WARN = "warn"


class AssertionKind(str, enum.Enum):
    """Every way a case can be checked."""

    #: Deterministic output, compared whole.
    EXACT = "exact"
    #: Deterministic shape: named keys present with the right types, extra keys
    #: allowed. For an agent whose contract is a structure rather than a value.
    STRUCTURAL = "structural"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    #: A numeric output inside a range. The weakest useful numeric check: it
    #: passes for an agent that returns the midpoint for everything, which is
    #: exactly why DISCRIMINATION exists.
    NUMERIC_RANGE = "numeric_range"
    #: Outputs across several inputs must differ by at least a margin.
    DISCRIMINATION = "discrimination"
    #: A better input produces a not-worse score.
    MONOTONICITY = "monotonicity"
    #: The output cites a record that exists and supports the claim.
    GROUNDING = "grounding"
    #: An out-of-scope or unauthorised request is declined.
    REFUSAL = "refusal"
    #: The same input across N runs stays inside a variance bound.
    CONSISTENCY = "consistency"
    #: A rubric-scored judgement of open-ended text. Supplement only.
    LLM_JUDGE = "llm_judge"

    @property
    def compares_across_inputs(self) -> bool:
        """Whether this kind needs several outputs rather than one.

        The three that do are the three that catch the failures a per-output
        check cannot see, which is not a coincidence.
        """
        return self in (
            AssertionKind.DISCRIMINATION,
            AssertionKind.MONOTONICITY,
            AssertionKind.CONSISTENCY,
        )


@dataclass(frozen=True, slots=True)
class AssertionSpec:
    """One expectation attached to a case."""

    kind: AssertionKind
    #: Kind-specific configuration. Validated by `check` rather than by a schema
    #: so the error names the missing key and the assertion that wanted it.
    config: dict[str, Any] = field(default_factory=dict)
    #: What this is protecting against, in a sentence. Shown in the failure, so
    #: whoever sees red at 6pm knows whether it matters.
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "config": self.config,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    """The result of one assertion."""

    kind: AssertionKind
    passed: bool
    #: Why it failed, in words somebody can act on. Empty on a pass.
    reason: str = ""
    #: The numbers behind the verdict — the observed spread, the variance, the
    #: margin. A pass/fail with no measurement cannot show a slow degradation.
    measured: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "passed": self.passed,
            "reason": self.reason,
            "measured": self.measured,
        }


def _require(config: dict[str, Any], key: str, kind: AssertionKind) -> Any:
    if key not in config:
        raise MalformedAssertionError(
            f"A {kind.value} assertion needs {key!r} in its config. Without it "
            "the assertion cannot be evaluated, and a case that cannot be "
            "evaluated must not be reported as passing."
        )
    return config[key]


def _as_float(value: Any, *, where: str) -> float:
    if isinstance(value, bool):
        raise MalformedAssertionError(f"{where} is a boolean, not a number.")
    if isinstance(value, int | float | Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise MalformedAssertionError(f"{where} is not a number: {value!r}") from exc
    raise MalformedAssertionError(f"{where} is not a number: {value!r}")


def _extract(output: Any, path: str | None) -> Any:
    """Pull a value out of an output by dotted path.

    `None` means the output itself. A missing key raises rather than returning
    `None`: an assertion silently comparing `None` to `None` passes, and that is
    a green test for an agent that stopped returning the field.
    """
    if path is None:
        return output
    current = output
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise MalformedAssertionError(
                    f"The output has no {path!r} (stopped at {part!r}). An "
                    "assertion cannot compare a field that is not there — this "
                    "is a failing agent or a stale suite, not a pass."
                )
            current = current[part]
        else:
            raise MalformedAssertionError(
                f"Cannot read {path!r} from a {type(current).__name__}."
            )
    return current


# ══════════════════════════════════════════════════════════════════════════════
# Single-output assertions
# ══════════════════════════════════════════════════════════════════════════════


def _exact(spec: AssertionSpec, output: Any) -> Verdict:
    expected = _require(spec.config, "expected", spec.kind)
    actual = _extract(output, spec.config.get("path"))
    passed = actual == expected
    return Verdict(
        kind=spec.kind,
        passed=passed,
        reason="" if passed else f"expected {expected!r}, got {actual!r}",
        measured={"expected": expected, "actual": actual},
    )


_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float, Decimal),
    "boolean": bool,
    "list": list,
    "object": dict,
}


def _structural(spec: AssertionSpec, output: Any) -> Verdict:
    """Named keys present with the right types. Extra keys allowed.

    Extra keys allowed on purpose: an agent adding a field is not a regression,
    and a strict check turns every enrichment into a suite edit, which is how a
    suite becomes something people disable.
    """
    shape = _require(spec.config, "shape", spec.kind)
    actual = _extract(output, spec.config.get("path"))
    if not isinstance(actual, dict):
        return Verdict(
            kind=spec.kind,
            passed=False,
            reason=f"expected an object, got {type(actual).__name__}",
        )

    problems: list[str] = []
    for key, type_name in shape.items():
        if key not in actual:
            problems.append(f"missing {key!r}")
            continue
        expected_type = _TYPES.get(type_name)
        if expected_type is None:
            raise MalformedAssertionError(
                f"{type_name!r} is not a known type. Use one of "
                f"{', '.join(sorted(_TYPES))}."
            )
        value = actual[key]
        if type_name == "number" and isinstance(value, bool):
            problems.append(f"{key!r} is a boolean, not a number")
        elif not isinstance(value, expected_type):
            problems.append(
                f"{key!r} is {type(value).__name__}, expected {type_name}"
            )

    return Verdict(
        kind=spec.kind,
        passed=not problems,
        reason="; ".join(problems),
        measured={"checked_keys": sorted(shape)},
    )


def _contains(spec: AssertionSpec, output: Any, *, negate: bool) -> Verdict:
    needles = _require(spec.config, "values", spec.kind)
    actual = _extract(output, spec.config.get("path"))
    haystack = str(actual) if not isinstance(actual, str) else actual
    if spec.config.get("case_insensitive", True):
        haystack = haystack.lower()
        needles = [str(n).lower() for n in needles]

    found = [n for n in needles if str(n) in haystack]
    missing = [n for n in needles if str(n) not in haystack]

    if negate:
        passed = not found
        reason = "" if passed else f"forbidden content present: {found}"
    else:
        passed = not missing
        reason = "" if passed else f"missing required content: {missing}"

    return Verdict(
        kind=spec.kind,
        passed=passed,
        reason=reason,
        measured={"found": found, "missing": missing},
    )


def _numeric_range(spec: AssertionSpec, output: Any) -> Verdict:
    actual = _as_float(
        _extract(output, spec.config.get("path")), where="the output"
    )
    low = spec.config.get("min")
    high = spec.config.get("max")
    if low is None and high is None:
        raise MalformedAssertionError(
            "A numeric_range assertion needs a 'min', a 'max' or both. With "
            "neither it passes for every number, which is not a check."
        )

    problems = []
    if low is not None and actual < float(low):
        problems.append(f"{actual} is below the minimum {low}")
    if high is not None and actual > float(high):
        problems.append(f"{actual} is above the maximum {high}")

    return Verdict(
        kind=spec.kind,
        passed=not problems,
        reason="; ".join(problems),
        measured={"actual": actual, "min": low, "max": high},
    )


#: Phrases that read as a decline. Deliberately broad: the cost of missing one
#: is an assertion that reports a genuine refusal as a failure to refuse, which
#: sends somebody to fix an agent that was already right. "could not" was added
#: after exactly that — the expense agent declines with "the total could not be
#: read" and the first version of this list scored it as an answer.
_REFUSAL_MARKERS = (
    "cannot",
    "can't",
    "could not",
    "couldn't",
    "not able to",
    "unable to",
    "not permitted",
    "not authorised",
    "not authorized",
    "do not have access",
    "don't have access",
    "outside",
    "decline",
    "won't",
    "will not",
)


def _refusal(spec: AssertionSpec, output: Any) -> Verdict:
    """An out-of-scope request is declined.

    Two halves, and the second is the one that matters. A refusal that still
    leaks the answer — "I can't tell you salaries, but hers is around 40 lakh" —
    is not a refusal, so `must_not_contain` is checked as well and defaults to
    the values the case was probing for.
    """
    actual = _extract(output, spec.config.get("path"))
    text = str(actual).lower()

    refused = spec.config.get("refused_field")
    if refused is not None:
        # A structured refusal: the agent returns a flag rather than prose.
        declined = bool(_extract(output, refused))
    else:
        declined = any(marker in text for marker in _REFUSAL_MARKERS)

    leaked = [
        str(v)
        for v in spec.config.get("must_not_contain", [])
        if str(v).lower() in text
    ]

    problems = []
    if not declined:
        problems.append("the request was not declined")
    if leaked:
        problems.append(
            f"the refusal still disclosed {leaked} — a refusal that leaks the "
            "answer is not a refusal"
        )

    return Verdict(
        kind=spec.kind,
        passed=not problems,
        reason="; ".join(problems),
        measured={"declined": declined, "leaked": leaked},
    )


_CITATION = re.compile(r"\[\[([a-zA-Z_]+):([A-Za-z0-9\-_]+)\]\]")


def _grounding(spec: AssertionSpec, output: Any) -> Verdict:
    """The output cites a record that exists and supports the claim.

    Three failures, and the third is the one people forget:

    1. No citation at all for a claim that needs one.
    2. A citation to a record that does not exist — the classic hallucination.
    3. A citation to a record that exists and **does not support the claim**.
       This is the dangerous one, because it survives every check that only
       verifies the id resolves. So `supporting_facts` maps a record id to what
       it actually says, and the claim has to be consistent with it.
    """
    actual = _extract(output, spec.config.get("path"))
    text = str(actual)

    known: dict[str, Any] = _require(spec.config, "known_records", spec.kind)
    supporting: dict[str, Any] = spec.config.get("supporting_facts", {})
    require_citation = spec.config.get("require_citation", True)

    cited = [f"{kind}:{ident}" for kind, ident in _CITATION.findall(text)]

    problems: list[str] = []
    if require_citation and not cited:
        problems.append(
            "no citation, for a claim that needs one. An unsourced assertion "
            "about somebody's record is the thing this agent must not produce."
        )

    unknown = [ref for ref in cited if ref not in known]
    if unknown:
        problems.append(f"cited records that do not exist: {unknown}")

    unsupported: list[str] = []
    for ref in cited:
        if ref in unknown:
            continue
        fact = supporting.get(ref)
        if fact is None:
            continue
        if str(fact).lower() not in text.lower():
            unsupported.append(ref)
    if unsupported:
        problems.append(
            f"cited {unsupported}, which exist but do not support the claim "
            "made. A citation that resolves is not a citation that holds."
        )

    return Verdict(
        kind=spec.kind,
        passed=not problems,
        reason="; ".join(problems),
        measured={
            "cited": cited,
            "unknown": unknown,
            "unsupported": unsupported,
        },
    )


def _llm_judge(spec: AssertionSpec, output: Any) -> Verdict:
    """A rubric score somebody else produced, compared against a threshold.

    The judgement itself is not made here — this module calls no model. The
    runner supplies `judge_score` from whatever produced it, and this asserts
    against the rubric's threshold. Keeping the call outside means the harness
    stays deterministic and testable, and it makes visible that the judge is an
    input to the eval rather than part of it.
    """
    threshold = _as_float(
        _require(spec.config, "min_score", spec.kind), where="min_score"
    )
    rubric = spec.config.get("rubric", "")
    if not rubric:
        raise MalformedAssertionError(
            "An llm_judge assertion needs a fixed rubric. A judge with no "
            "written rubric scores differently every time the prompt is "
            "regenerated, which makes the assertion unfalsifiable."
        )

    if "judge_score" not in spec.config:
        return Verdict(
            kind=spec.kind,
            passed=False,
            reason=(
                "no judge score was supplied for this case, so the assertion "
                "did not run. It is reported as a failure rather than skipped: "
                "a judge that silently did not run is a green test that "
                "checked nothing."
            ),
        )

    score = _as_float(spec.config["judge_score"], where="judge_score")
    passed = score >= threshold
    return Verdict(
        kind=spec.kind,
        passed=passed,
        reason="" if passed else f"judge scored {score}, below {threshold}",
        measured={"score": score, "threshold": threshold, "rubric": rubric},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Cross-output assertions — the ones that catch the Fit Score class of defect
# ══════════════════════════════════════════════════════════════════════════════


def _discrimination(spec: AssertionSpec, outputs: Sequence[Any]) -> Verdict:
    """Outputs that should differ actually do, by at least a margin.

    The assertion the Fit Score defect needed. Given the scores for several
    candidates who are not alike, the spread between the highest and the lowest
    has to be at least `min_spread`.

    `min_pairwise_gap` is the stricter, optional half: it also requires no two
    outputs to be closer together than a gap. A set of five scores where four
    are identical and one is an outlier passes a spread check and is still an
    agent that cannot tell four candidates apart.
    """
    min_spread = _as_float(
        _require(spec.config, "min_spread", spec.kind), where="min_spread"
    )
    path = spec.config.get("path")

    values = [
        _as_float(_extract(o, path), where=f"output {i}")
        for i, o in enumerate(outputs)
    ]
    if len(values) < 2:
        raise MalformedAssertionError(
            "A discrimination assertion needs at least two outputs. With one "
            "there is nothing to compare, and the assertion would pass "
            "vacuously — which is precisely how the original defect got "
            "through."
        )

    spread = max(values) - min(values)
    problems: list[str] = []
    if spread < min_spread:
        problems.append(
            f"the outputs span only {spread:.4g}, below the required "
            f"{min_spread:.4g}. These inputs should produce different results "
            "and this agent is returning near-identical values for all of them "
            "— the Fit Score failure, exactly."
        )

    min_gap = spec.config.get("min_pairwise_gap")
    closest: float | None = None
    if min_gap is not None:
        gap_floor = _as_float(min_gap, where="min_pairwise_gap")
        ordered = sorted(values)
        gaps = [b - a for a, b in itertools.pairwise(ordered)]
        closest = min(gaps) if gaps else 0.0
        if closest < gap_floor:
            problems.append(
                f"the two closest outputs are {closest:.4g} apart, below the "
                f"required {gap_floor:.4g}. A wide overall spread can hide a "
                "cluster the agent cannot tell apart."
            )

    return Verdict(
        kind=spec.kind,
        passed=not problems,
        reason=" ".join(problems),
        measured={
            "values": values,
            "spread": spread,
            "min_spread": min_spread,
            "closest_pair_gap": closest,
            "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        },
    )


def _monotonicity(spec: AssertionSpec, outputs: Sequence[Any]) -> Verdict:
    """A better input produces a not-worse score.

    The outputs arrive in the order the cases declared, which is the order the
    inputs improve in. Non-decreasing, not strictly increasing: two genuinely
    equivalent inputs scoring the same is correct, and demanding a strict
    increase would fail an agent for being right.
    """
    path = spec.config.get("path")
    tolerance = _as_float(spec.config.get("tolerance", 0), where="tolerance")

    values = [
        _as_float(_extract(o, path), where=f"output {i}")
        for i, o in enumerate(outputs)
    ]
    if len(values) < 2:
        raise MalformedAssertionError(
            "A monotonicity assertion needs at least two outputs, ordered from "
            "worst input to best."
        )

    violations = [
        f"position {i} scored {a:.4g} and position {i + 1} scored {b:.4g}"
        for i, (a, b) in enumerate(itertools.pairwise(values))
        if b < a - tolerance
    ]

    return Verdict(
        kind=spec.kind,
        passed=not violations,
        reason=(
            ""
            if not violations
            else (
                "a better input scored worse: "
                + "; ".join(violations)
                + ". The ordering of the cases is the ordering of the inputs, "
                "so this is the agent preferring the weaker one."
            )
        ),
        measured={"values": values, "tolerance": tolerance},
    )


def _consistency(spec: AssertionSpec, outputs: Sequence[Any]) -> Verdict:
    """The same input across N runs stays inside a variance bound.

    Measured as the coefficient of variation where the mean is non-zero, so the
    bound means the same thing for a score out of 100 and a score out of 5. A
    fixed absolute bound would be tight on one scale and meaningless on the
    other.
    """
    max_cv = _as_float(
        _require(spec.config, "max_coefficient_of_variation", spec.kind),
        where="max_coefficient_of_variation",
    )
    path = spec.config.get("path")

    values = [
        _as_float(_extract(o, path), where=f"run {i}")
        for i, o in enumerate(outputs)
    ]
    if len(values) < 3:
        raise MalformedAssertionError(
            "A consistency assertion needs at least three runs. Two samples "
            "produce a variance figure that is mostly noise."
        )

    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    if math.isclose(mean, 0.0, abs_tol=1e-9):
        # A mean of zero makes the coefficient undefined; fall back to the raw
        # spread rather than dividing by something near zero and reporting an
        # enormous number that means nothing.
        cv = stdev
        measure = "stdev (mean is zero)"
    else:
        cv = stdev / abs(mean)
        measure = "coefficient of variation"

    passed = cv <= max_cv
    return Verdict(
        kind=spec.kind,
        passed=passed,
        reason=(
            ""
            if passed
            else (
                f"the same input produced a {measure} of {cv:.4g} across "
                f"{len(values)} runs, above the bound of {max_cv:.4g}. Values: "
                f"{[round(v, 4) for v in values]}."
            )
        ),
        measured={
            "values": values,
            "mean": mean,
            "stdev": stdev,
            "coefficient_of_variation": cv,
            "bound": max_cv,
        },
    )


_SINGLE: dict[AssertionKind, Any] = {
    AssertionKind.EXACT: _exact,
    AssertionKind.STRUCTURAL: _structural,
    AssertionKind.NUMERIC_RANGE: _numeric_range,
    AssertionKind.REFUSAL: _refusal,
    AssertionKind.GROUNDING: _grounding,
    AssertionKind.LLM_JUDGE: _llm_judge,
}

_MULTI: dict[AssertionKind, Any] = {
    AssertionKind.DISCRIMINATION: _discrimination,
    AssertionKind.MONOTONICITY: _monotonicity,
    AssertionKind.CONSISTENCY: _consistency,
}


def check(spec: AssertionSpec, outputs: Sequence[Any]) -> Verdict:
    """Evaluate one assertion against one or more outputs.

    Always takes a sequence, even for a single-output kind, so the runner does
    not need two code paths and a cross-input assertion cannot be handed one
    output by accident — it raises instead, which is what `_discrimination`
    does when given a single value.
    """
    if not outputs:
        raise MalformedAssertionError(
            f"A {spec.kind.value} assertion was given no outputs. An assertion "
            "with nothing to check must not be reported as passing."
        )

    if spec.kind in _MULTI:
        return _MULTI[spec.kind](spec, outputs)
    if spec.kind in (AssertionKind.CONTAINS, AssertionKind.NOT_CONTAINS):
        return _contains(
            spec, outputs[0], negate=spec.kind is AssertionKind.NOT_CONTAINS
        )
    handler = _SINGLE.get(spec.kind)
    if handler is None:
        raise MalformedAssertionError(
            f"No handler for assertion kind {spec.kind.value!r}. Every kind in "
            "the enum needs one, or a case using it would silently do nothing."
        )
    return handler(spec, outputs[0])


def kinds_without_handlers() -> set[AssertionKind]:
    """Assertion kinds the dispatcher cannot evaluate. Read by an invariant.

    A kind added to the enum with no handler would raise at run time on
    whichever suite first used it, which is a bad moment to find out.
    """
    handled = (
        set(_SINGLE)
        | set(_MULTI)
        | {AssertionKind.CONTAINS, AssertionKind.NOT_CONTAINS}
    )
    return set(AssertionKind) - handled
