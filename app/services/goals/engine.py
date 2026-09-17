"""Goal progress and the goal score, as pure arithmetic.

═══════════════════════════════════════════════════════════════════════════════
THE GOAL SCORE IS MEASURED, AND SAYS WHAT IT WAS MEASURED FROM
═══════════════════════════════════════════════════════════════════════════════

The appraisal template gives goals a share of the final score. That share has
to come from somewhere a person can check, so it is computed here from key
results — each measured from a start value towards a target — and every figure
travels with the goals that produced it.

Three rules carry the weight:

1. **A goal with no key results has no measured progress.** It is excluded from
   the score and named as excluded, never counted as zero. Zero would penalise
   somebody for how a goal was written down rather than for what they did.
2. **A cancelled goal is excluded**, for the same reason: the business withdrew
   it, the person did not fail it.
3. **Weights that do not sum to 100 are renormalised and the note says so.**
   Refusing would stall a review over a bookkeeping slip; silently rescaling
   would hide it.

Everything is Decimal. Nothing here touches a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "GoalError",
    "GoalFacts",
    "GoalProgress",
    "GoalScore",
    "KeyResultFacts",
    "check_alignment",
    "goal_progress",
    "goal_score",
    "key_result_progress",
    "validate_key_result",
    "validate_weight_budget",
]

ZERO = Decimal(0)
HUNDRED = Decimal(100)
CENT = Decimal("0.01")

#: A parent chain deeper than this is a loop or a data error, not a strategy.
MAX_ALIGNMENT_DEPTH = 10

MEASURED_KINDS = ("number", "percent", "currency")
MILESTONE = "milestone"
SCORED_STATUSES = ("active", "completed")


class GoalError(ValueError):
    """A goal or key result this module refuses."""


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(name: str, value: Any) -> Decimal:
    if isinstance(value, float):
        raise GoalError(f"{name} must be a Decimal, not a float.")
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ═══════════════════════════════════════════════════════════════════════════
# Key results
# ═══════════════════════════════════════════════════════════════════════════


def validate_key_result(*, kind: str, start: Decimal, target: Decimal) -> None:
    """Refuse a key result that cannot show progress.

    A measured result whose target equals its start has no distance to travel,
    so every value is either 0% or 100% — a milestone pretending to be a
    number. Say it is a milestone instead.
    """
    if kind == MILESTONE:
        return
    if kind not in MEASURED_KINDS:
        raise GoalError(f"'{kind}' is not a key result kind.")
    if _decimal("The start value", start) == _decimal("The target", target):
        raise GoalError(
            "The target is the same as the starting value, so there is no progress "
            "to measure. Use a milestone for something that is simply done or not."
        )


def key_result_progress(*, kind: str, start: Decimal, target: Decimal, current: Decimal) -> Decimal:
    """Progress from start to target, out of 100, clamped to 0..100.

    Works for a falling target too ("cut churn from 8% to 5%"): progress is the
    share of the distance travelled in the right direction.
    """
    current = _decimal("The current value", current)
    if kind == MILESTONE:
        return HUNDRED if current >= Decimal(1) else ZERO
    start = _decimal("The start value", start)
    target = _decimal("The target", target)
    span = target - start
    if span == ZERO:
        return HUNDRED if current == target else ZERO
    raw = (current - start) / span * HUNDRED
    return _q(min(HUNDRED, max(ZERO, raw)))


@dataclass(frozen=True, slots=True)
class KeyResultFacts:
    key_result_id: str
    title: str
    kind: str
    start: Decimal
    target: Decimal
    current: Decimal
    weight: Decimal = Decimal(1)


@dataclass(frozen=True, slots=True)
class GoalProgress:
    progress: Decimal | None
    contributions: list[dict[str, str]] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "progress": str(self.progress) if self.progress is not None else None,
            "contributions": self.contributions,
            "note": self.note,
        }


def goal_progress(key_results: list[KeyResultFacts]) -> GoalProgress:
    """A goal's progress: the weighted progress of its key results."""
    if not key_results:
        return GoalProgress(
            progress=None,
            note=(
                "No key results, so there is no measured progress. Add one to make this goal count."
            ),
        )
    total_weight = sum((_decimal("A key result weight", kr.weight) for kr in key_results), ZERO)
    if total_weight <= ZERO:
        raise GoalError("The key results on this goal carry no weight between them.")

    contributions: list[dict[str, str]] = []
    weighted = ZERO
    for kr in key_results:
        progress = key_result_progress(
            kind=kr.kind, start=kr.start, target=kr.target, current=kr.current
        )
        weighted += progress * kr.weight
        contributions.append(
            {
                "key_result_id": kr.key_result_id,
                "title": kr.title,
                "progress": str(progress),
                "weight": str(kr.weight),
            }
        )
    return GoalProgress(
        progress=_q(weighted / total_weight),
        contributions=contributions,
        note=f"Weighted across {len(key_results)} key result(s).",
    )


# ═══════════════════════════════════════════════════════════════════════════
# The goal score
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class GoalFacts:
    goal_id: str
    title: str
    status: str
    weight: Decimal
    key_results: list[KeyResultFacts] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GoalScore:
    """The goal component of a review, with everything it was computed from."""

    score: Decimal | None
    #: Share of the declared goal weight that had measured progress behind it.
    #: 100 means every weighted goal was measured; 40 means the score rests on
    #: less than half of what was agreed, which a reviewer should know.
    measured_weight_percent: Decimal
    contributing: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": str(self.score) if self.score is not None else None,
            "measured_weight_percent": str(self.measured_weight_percent),
            "contributing": self.contributing,
            "excluded": self.excluded,
            "notes": self.notes,
        }


def goal_score(goals: list[GoalFacts]) -> GoalScore:
    """The weighted goal score, out of 100, over measurable goals only."""
    contributing: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    notes: list[str] = []

    measured: list[tuple[GoalFacts, Decimal]] = []
    declared_weight = ZERO
    for goal in goals:
        weight = _decimal("A goal weight", goal.weight)
        if goal.status not in SCORED_STATUSES:
            excluded.append(
                {
                    "goal_id": goal.goal_id,
                    "title": goal.title,
                    "reason": f"The goal is {goal.status}, so it does not count.",
                }
            )
            continue
        declared_weight += weight
        progress = goal_progress(goal.key_results)
        if progress.progress is None:
            excluded.append({"goal_id": goal.goal_id, "title": goal.title, "reason": progress.note})
            continue
        measured.append((goal, progress.progress))
        contributing.append(
            {
                "goal_id": goal.goal_id,
                "title": goal.title,
                "status": goal.status,
                "weight": str(weight),
                "progress": str(progress.progress),
                "key_results": progress.contributions,
            }
        )

    if not measured:
        notes.append(
            "No goal in this review has measured progress, so there is no goal score. "
            "It is left empty rather than scored zero."
        )
        return GoalScore(
            score=None,
            measured_weight_percent=ZERO,
            contributing=contributing,
            excluded=excluded,
            notes=notes,
        )

    total = sum((_decimal("A goal weight", g.weight) for g, _ in measured), ZERO)
    if total <= ZERO:
        # Every measured goal carries zero weight: treat them equally and say so.
        notes.append("None of the measured goals carries a weight, so they are counted equally.")
        score = _q(sum((p for _, p in measured), ZERO) / Decimal(len(measured)))
        coverage = HUNDRED
    else:
        if total != HUNDRED:
            notes.append(
                f"The measured goals carry {_q(total)} of weight rather than 100, so "
                "their weights are scaled to 100."
            )
        score = _q(sum((p * g.weight for g, p in measured), ZERO) / total)
        coverage = _q(total / declared_weight * HUNDRED) if declared_weight > ZERO else HUNDRED

    for goal in contributing:
        goal["share_of_score"] = (
            str(_q(Decimal(goal["weight"]) / total * HUNDRED)) if total > ZERO else None
        )
    if excluded:
        notes.append(f"{len(excluded)} goal(s) are excluded from the score; each says why.")
    return GoalScore(
        score=score,
        measured_weight_percent=min(HUNDRED, coverage),
        contributing=contributing,
        excluded=excluded,
        notes=notes,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Weight budget and alignment
# ═══════════════════════════════════════════════════════════════════════════


def validate_weight_budget(*, existing: Decimal, proposed: Decimal) -> None:
    """One person's goal weights in a cycle may not add up to more than 100."""
    existing = _decimal("The existing weight", existing)
    proposed = _decimal("The proposed weight", proposed)
    if proposed < ZERO or proposed > HUNDRED:
        raise GoalError("A goal weight is between 0 and 100.")
    if existing + proposed > HUNDRED:
        raise GoalError(
            f"This person's goals already carry {_q(existing)} of weight in this cycle; "
            f"adding {_q(proposed)} would take them past 100. Reduce this goal's weight "
            "or another goal's first."
        )


def check_alignment(*, goal_id: str | None, parent_id: str, parents: dict[str, str | None]) -> None:
    """Refuse an alignment that loops back on itself.

    `parents` maps goal id to its parent id for the goals reachable from
    `parent_id`. A goal aligned to its own descendant makes "which company goal
    does this serve" unanswerable.
    """
    if goal_id is not None and parent_id == goal_id:
        raise GoalError("A goal cannot be aligned to itself.")
    seen: set[str] = set()
    current: str | None = parent_id
    for _ in range(MAX_ALIGNMENT_DEPTH + 1):
        if current is None:
            return
        if goal_id is not None and current == goal_id:
            raise GoalError(
                "That alignment would loop: the chosen parent already sits under this goal."
            )
        if current in seen:
            raise GoalError("The goals above this one already form a loop.")
        seen.add(current)
        current = parents.get(current)
    raise GoalError(
        f"The alignment chain is deeper than {MAX_ALIGNMENT_DEPTH} levels, which is a "
        "data problem rather than a strategy."
    )
