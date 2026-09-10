"""Weighted score computation, and the band that turns a score into a rating.

═══════════════════════════════════════════════════════════════════════════════
THE TWO THINGS THAT QUIETLY PENALISE PEOPLE
═══════════════════════════════════════════════════════════════════════════════

**A non-applicable competency scored as zero.** "Manages a team" does not apply
to an individual contributor. Scoring it zero and dividing by the full weight
drags their score down for a thing they were never expected to do — and it is
invisible, because the competency is on the template so it looks deliberate.
Non-applicable items are removed from the denominator, and the remaining
weights are renormalised.

**A band boundary that belongs to neither side, or to both.** A score of
exactly 3.5 between a "3.0-3.5" band and a "3.5-4.0" band either falls through
to nothing or matches two. Bands here are half-open — inclusive lower bound,
exclusive upper — with the top band closed, so every score in range maps to
exactly one rating and the edges are testable.
═══════════════════════════════════════════════════════════════════════════════

Everything here is pure: scales, weights and bands come in as data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Any

__all__ = [
    "Band",
    "CompetencyRating",
    "ScoreBreakdown",
    "ScoringError",
    "WeightSplit",
    "compute_score",
    "map_to_band",
    "validate_weights",
]

ZERO = Decimal(0)
HUNDRED = Decimal(100)


class ScoringError(ValueError):
    """A score cannot be computed from the configuration supplied."""


def _check(name: str, value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise ScoringError(f"{name} must be a Decimal, not a float")
    if value < ZERO:
        raise ScoringError(f"{name} cannot be negative")
    return value


# ═══════════════════════════════════════════════════════════════════════════
# Weights
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class WeightSplit:
    """How much goals, competencies and values each contribute.

    Commonly 70/30 goals to competencies, with values added where the tenant
    uses them.
    """

    goal_weight: Decimal
    competency_weight: Decimal
    values_weight: Decimal = ZERO

    def as_dict(self) -> dict[str, str]:
        return {
            "goal_weight": str(self.goal_weight),
            "competency_weight": str(self.competency_weight),
            "values_weight": str(self.values_weight),
        }


def validate_weights(split: WeightSplit) -> None:
    """Weights must sum to 100, checked at save.

    At computation time it is too late: the cycle has launched, managers have
    started rating, and a template nobody can score is discovered by the person
    trying to submit rather than the person who configured it.
    """
    for name, value in (
        ("The goal weight", split.goal_weight),
        ("The competency weight", split.competency_weight),
        ("The values weight", split.values_weight),
    ):
        _check(name, value)

    total = split.goal_weight + split.competency_weight + split.values_weight
    if total != HUNDRED:
        raise ScoringError(
            f"The weights sum to {total}, not 100. A template that cannot be scored is "
            "discovered by the manager trying to submit rather than the person who "
            "configured it, so this is checked now."
        )


# ═══════════════════════════════════════════════════════════════════════════
# Competency ratings
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class CompetencyRating:
    """One competency, its weight on the template, and what it was rated.

    `is_applicable=False` removes it from the calculation entirely. `rating` of
    None with `is_applicable=True` is an unrated competency, which is a
    different problem and is refused.
    """

    code: str
    name: str
    weight: Decimal
    rating: Decimal | None = None
    is_applicable: bool = True
    not_applicable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """The final score, and enough of the working to defend it."""

    goal_score: Decimal
    competency_score: Decimal
    values_score: Decimal
    weighted_total: Decimal
    # The score before rounding, so the rounding is visible rather than assumed.
    unrounded_total: Decimal
    rounding: str
    excluded_competencies: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal_score": str(self.goal_score),
            "competency_score": str(self.competency_score),
            "values_score": str(self.values_score),
            "weighted_total": str(self.weighted_total),
            "unrounded_total": str(self.unrounded_total),
            "rounding": self.rounding,
            "excluded_competencies": self.excluded_competencies,
            "notes": self.notes,
        }


def _competency_score(
    ratings: list[CompetencyRating], scale_max: Decimal
) -> tuple[Decimal, list[dict[str, str]], list[str]]:
    """The weighted competency average, over applicable items only.

    Renormalising against the applicable weights is what stops "manages a team"
    dragging down an individual contributor's score for a thing they were never
    expected to do.
    """
    applicable = [r for r in ratings if r.is_applicable]
    excluded = [
        {
            "code": r.code,
            "name": r.name,
            "weight": str(r.weight),
            "reason": r.not_applicable_reason or "Marked not applicable.",
        }
        for r in ratings
        if not r.is_applicable
    ]
    notes: list[str] = []

    if not applicable:
        raise ScoringError(
            "Every competency on this template is marked not applicable, so there is "
            "nothing to score. Either the template is wrong for this role, or the "
            "competency weight should be zero."
        )

    unrated = [r.code for r in applicable if r.rating is None]
    if unrated:
        raise ScoringError(
            f"These competencies are applicable but unrated: {', '.join(unrated)}. An "
            "unrated competency is not the same as one that does not apply — scoring it "
            "as zero would penalise the employee for the rater's omission."
        )

    total_weight = sum((r.weight for r in applicable), ZERO)
    if total_weight <= ZERO:
        raise ScoringError(
            "The applicable competencies carry no weight between them, so the average "
            "is undefined."
        )

    if excluded:
        excluded_weight = sum((Decimal(e["weight"]) for e in excluded), ZERO)
        notes.append(
            f"{len(excluded)} competency(ies) carrying {excluded_weight} of weight were "
            "not applicable and are excluded from the calculation. The remaining weights "
            "are renormalised — scoring them zero would penalise the employee for a "
            "thing they were never expected to do."
        )

    weighted = sum(
        ((r.rating or ZERO) * r.weight for r in applicable), ZERO
    )
    # Expressed out of 100 so the three dimensions combine on one basis.
    average = weighted / total_weight
    return (average / scale_max * HUNDRED), excluded, notes


def compute_score(
    *,
    split: WeightSplit,
    goal_score: Decimal,
    competency_ratings: list[CompetencyRating],
    scale_max: Decimal,
    values_score: Decimal | None = None,
    precision: int = 2,
) -> ScoreBreakdown:
    """The final weighted score, out of 100.

    Args:
        goal_score: goal achievement, already expressed out of 100.
        scale_max: the top of the rating scale, so a 4-point and a 5-point
            scale produce comparable percentages.
        values_score: out of 100. Required when the split gives values any
            weight, refused when it does not — a values score on a template
            that does not use them is a configuration mistake, not something
            to silently drop.
    """
    validate_weights(split)
    _check("The goal score", goal_score)
    _check("The scale maximum", scale_max)
    if scale_max <= ZERO:
        raise ScoringError("The scale maximum must be greater than zero.")

    notes: list[str] = []
    excluded: list[dict[str, str]] = []

    competency_component = ZERO
    if split.competency_weight > ZERO:
        competency_component, excluded, competency_notes = _competency_score(
            competency_ratings, scale_max
        )
        notes.extend(competency_notes)
    elif competency_ratings:
        notes.append(
            "Competencies were rated but carry no weight on this template, so they do "
            "not affect the score."
        )

    resolved_values = ZERO
    if split.values_weight > ZERO:
        if values_score is None:
            raise ScoringError(
                f"This template gives values {split.values_weight} of the weight, but no "
                "values score was supplied."
            )
        resolved_values = _check("The values score", values_score)
    elif values_score is not None:
        raise ScoringError(
            "A values score was supplied but this template gives values no weight. "
            "Silently dropping it would hide a configuration mistake."
        )

    unrounded = (
        goal_score * split.goal_weight
        + competency_component * split.competency_weight
        + resolved_values * split.values_weight
    ) / HUNDRED

    quantum = Decimal(1).scaleb(-precision)
    rounded = unrounded.quantize(quantum, rounding=ROUND_HALF_UP)

    return ScoreBreakdown(
        goal_score=goal_score,
        competency_score=competency_component.quantize(quantum, rounding=ROUND_HALF_UP),
        values_score=resolved_values,
        weighted_total=rounded,
        unrounded_total=unrounded,
        rounding=f"half-up to {precision} decimal place(s)",
        excluded_competencies=excluded,
        notes=notes,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Score bands
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Band:
    """One score band.

    Half-open: `lower <= score < upper`, with the top band closed at its upper
    bound. A score of exactly 3.5 between a "3.0-3.5" and a "3.5-4.0" band
    would otherwise either fall through to nothing or match both.
    """

    label: str
    lower: Decimal
    upper: Decimal
    description: str | None = None


def map_to_band(score: Decimal, bands: list[Band]) -> Band:
    """Which rating a score earns.

    Bands are checked in order, lowest first, and the highest band includes its
    upper bound so a perfect score has somewhere to land.
    """
    if not bands:
        raise ScoringError(
            "No score bands are configured, so a score cannot be turned into a rating."
        )
    _check("The score", score)

    ordered = sorted(bands, key=lambda b: b.lower)
    for index, band in enumerate(ordered):
        if band.upper <= band.lower:
            raise ScoringError(
                f"Band '{band.label}' runs from {band.lower} to {band.upper}, which is "
                "not a range."
            )
        is_top = index == len(ordered) - 1
        if band.lower <= score < band.upper or (is_top and score == band.upper):
            return band

    lowest, highest = ordered[0], ordered[-1]
    raise ScoringError(
        f"A score of {score} falls outside every band ({lowest.lower} to "
        f"{highest.upper}). Either the bands do not cover the scale or the score is "
        "wrong; neither should be resolved by guessing a rating."
    )


def validate_bands(bands: list[Band]) -> None:
    """Bands must be contiguous and non-overlapping.

    A gap silently produces an unratable score; an overlap makes the rating
    depend on the order the bands happen to be stored in.
    """
    if not bands:
        raise ScoringError("At least one band is needed.")

    ordered = sorted(bands, key=lambda b: b.lower)
    for previous, current in pairwise(ordered):
        if current.lower < previous.upper:
            raise ScoringError(
                f"'{previous.label}' and '{current.label}' overlap between "
                f"{current.lower} and {previous.upper}. The rating would depend on "
                "which band happened to be checked first."
            )
        if current.lower > previous.upper:
            raise ScoringError(
                f"There is a gap between '{previous.label}' (ending {previous.upper}) "
                f"and '{current.label}' (starting {current.lower}). A score in the gap "
                "would have no rating."
            )
