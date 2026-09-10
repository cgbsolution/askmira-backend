"""Distribution analysis, outlier detection, and enforcement.

═══════════════════════════════════════════════════════════════════════════════
THE SMALL-GROUP PROBLEM
═══════════════════════════════════════════════════════════════════════════════

A manager with three reports cannot hit a 10% target. Ten percent of three is
0.3 people. Whatever they do, they are "out of tolerance" — and the only way to
comply is to rate someone according to arithmetic rather than performance.

Enforcing a distribution on a group too small to express it is not strictness,
it is statistical illiteracy with a person on the receiving end. So groups below
`min_group_size` are excluded from enforcement and **told they were excluded and
why**, rather than silently passing or silently failing.

═══════════════════════════════════════════════════════════════════════════════
WHAT THIS MODULE DOES NOT DO
═══════════════════════════════════════════════════════════════════════════════

It does not take a view on whether forced distribution is good practice. Clients
decided that before they bought the software, they disagree strongly with each
other, and a product that editorialises in its error messages is a product that
argues with its users about their own HR policy. All three modes are supported
and none is described as preferable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "CalibrationError",
    "DistributionResult",
    "EnforcementMode",
    "ManagerProfile",
    "RatingCount",
    "Target",
    "analyse_distribution",
    "detect_outliers",
    "enforcement_blockers",
]

ZERO = Decimal(0)
HUNDRED = Decimal(100)


class CalibrationError(ValueError):
    """A distribution cannot be analysed from what was supplied."""


class EnforcementMode:
    NONE = "none"
    ADVISORY = "advisory"
    ENFORCED = "enforced"

    ALL = (NONE, ADVISORY, ENFORCED)


@dataclass(frozen=True, slots=True)
class Target:
    """The share of a group expected at one rating, and how far off is allowed."""

    rating: str
    target_percent: Decimal
    tolerance_percent: Decimal = ZERO

    @property
    def lower_bound(self) -> Decimal:
        return max(ZERO, self.target_percent - self.tolerance_percent)

    @property
    def upper_bound(self) -> Decimal:
        return min(HUNDRED, self.target_percent + self.tolerance_percent)


@dataclass(frozen=True, slots=True)
class RatingCount:
    rating: str
    count: int
    actual_percent: Decimal
    target_percent: Decimal | None
    lower_bound: Decimal | None
    upper_bound: Decimal | None
    is_within_tolerance: bool
    # Signed: negative means fewer than target, positive means more.
    gap_percent: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "rating": self.rating,
            "count": self.count,
            "actual_percent": str(self.actual_percent),
            "target_percent": str(self.target_percent) if self.target_percent is not None else None,
            "lower_bound": str(self.lower_bound) if self.lower_bound is not None else None,
            "upper_bound": str(self.upper_bound) if self.upper_bound is not None else None,
            "is_within_tolerance": self.is_within_tolerance,
            "gap_percent": str(self.gap_percent),
        }


@dataclass(frozen=True, slots=True)
class DistributionResult:
    scope: str
    scope_value: str
    population: int
    counts: list[RatingCount] = field(default_factory=list)
    # True when the group is too small for a distribution to mean anything.
    is_excluded_as_small: bool = False
    exclusion_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_within_tolerance(self) -> bool:
        """An excluded group is not "in tolerance" — it is not being measured.

        Reported as compliant so it does not block, but `is_excluded_as_small`
        is what a reader should look at.
        """
        if self.is_excluded_as_small:
            return True
        return all(c.is_within_tolerance for c in self.counts)

    @property
    def breaches(self) -> list[RatingCount]:
        if self.is_excluded_as_small:
            return []
        return [c for c in self.counts if not c.is_within_tolerance]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "scope_value": self.scope_value,
            "population": self.population,
            "is_excluded_as_small": self.is_excluded_as_small,
            "exclusion_reason": self.exclusion_reason,
            "is_within_tolerance": self.is_within_tolerance,
            "counts": [c.as_dict() for c in self.counts],
            "breaches": [c.rating for c in self.breaches],
            "notes": self.notes,
        }


def _percent(part: int, whole: int) -> Decimal:
    if whole == 0:
        return ZERO
    return (Decimal(part) / Decimal(whole) * HUNDRED).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def analyse_distribution(
    *,
    scope: str,
    scope_value: str,
    ratings: list[str],
    targets: list[Target],
    min_group_size: int = 1,
) -> DistributionResult:
    """Actual against target for one scope.

    Args:
        ratings: one entry per employee in scope, the rating they currently
            hold. Unrated employees are not passed in — they are a different
            problem, and counting them as a rating would distort the shape.
        min_group_size: below this the group is excluded from enforcement.
            A manager with three reports cannot hit a 10% target, and forcing
            it means rating someone by arithmetic.
    """
    if min_group_size < 1:
        raise CalibrationError("The minimum group size must be at least one.")

    population = len(ratings)
    by_target = {t.rating: t for t in targets}

    if population == 0:
        return DistributionResult(
            scope=scope,
            scope_value=scope_value,
            population=0,
            is_excluded_as_small=True,
            exclusion_reason="Nobody in this scope has a rating yet.",
        )

    observed: dict[str, int] = {}
    for rating in ratings:
        observed[rating] = observed.get(rating, 0) + 1

    # Every target rating appears even at zero, so a rating nobody was given is
    # visible as a gap rather than absent from the table.
    all_ratings = list(dict.fromkeys([*by_target, *observed]))

    excluded = population < min_group_size
    exclusion_reason = None
    notes: list[str] = []

    if excluded:
        smallest_meaningful = (
            min((t.target_percent for t in targets if t.target_percent > ZERO), default=ZERO)
        )
        one_person = _percent(1, population)
        exclusion_reason = (
            f"{population} employee(s) in scope, below the minimum of {min_group_size}. "
            f"One person here is {one_person}% of the group, so a target of "
            f"{smallest_meaningful}% cannot be expressed at all — hitting it would mean "
            "rating someone by arithmetic rather than performance. This scope is "
            "reported but not enforced."
        )

    counts: list[RatingCount] = []
    for rating in all_ratings:
        count = observed.get(rating, 0)
        actual = _percent(count, population)
        target = by_target.get(rating)

        if target is None:
            counts.append(
                RatingCount(
                    rating=rating, count=count, actual_percent=actual,
                    target_percent=None, lower_bound=None, upper_bound=None,
                    is_within_tolerance=True, gap_percent=ZERO,
                )
            )
            if count:
                notes.append(
                    f"'{rating}' has no distribution target, so its {count} employee(s) "
                    "are counted but not measured."
                )
            continue

        within = target.lower_bound <= actual <= target.upper_bound
        counts.append(
            RatingCount(
                rating=rating,
                count=count,
                actual_percent=actual,
                target_percent=target.target_percent,
                lower_bound=target.lower_bound,
                upper_bound=target.upper_bound,
                is_within_tolerance=excluded or within,
                gap_percent=actual - target.target_percent,
            )
        )

    return DistributionResult(
        scope=scope,
        scope_value=scope_value,
        population=population,
        counts=counts,
        is_excluded_as_small=excluded,
        exclusion_reason=exclusion_reason,
        notes=notes,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Enforcement
# ═══════════════════════════════════════════════════════════════════════════


def enforcement_blockers(
    *, mode: str, distributions: list[DistributionResult]
) -> list[dict[str, Any]]:
    """What stops the cycle closing, under this mode.

    Returns an empty list under NONE and ADVISORY — advisory means the gap is
    shown and closure proceeds, which is the whole distinction between the two.

    Under ENFORCED, each blocker names the scope and the exact overshoot, so a
    facilitator knows what to change rather than being told only that something
    is wrong.
    """
    if mode not in EnforcementMode.ALL:
        raise CalibrationError(
            f"'{mode}' is not an enforcement mode. Use one of: "
            f"{', '.join(EnforcementMode.ALL)}."
        )
    if mode in (EnforcementMode.NONE, EnforcementMode.ADVISORY):
        return []

    blockers: list[dict[str, Any]] = []
    for distribution in distributions:
        if distribution.is_excluded_as_small or distribution.is_within_tolerance:
            continue
        for breach in distribution.breaches:
            direction = "above" if breach.gap_percent > ZERO else "below"
            bound = breach.upper_bound if breach.gap_percent > ZERO else breach.lower_bound
            over_by = (
                breach.actual_percent - (bound or ZERO)
                if breach.gap_percent > ZERO
                else (bound or ZERO) - breach.actual_percent
            )
            blockers.append(
                {
                    "scope": distribution.scope,
                    "scope_value": distribution.scope_value,
                    "rating": breach.rating,
                    "population": distribution.population,
                    "count": breach.count,
                    "actual_percent": str(breach.actual_percent),
                    "allowed_range": f"{breach.lower_bound} to {breach.upper_bound}%",
                    "message": (
                        f"{distribution.scope} '{distribution.scope_value}' has "
                        f"{breach.count} of {distribution.population} at "
                        f"'{breach.rating}' ({breach.actual_percent}%), which is "
                        f"{over_by.quantize(Decimal('0.01'))} points {direction} the "
                        f"allowed {breach.lower_bound} to {breach.upper_bound}%."
                    ),
                }
            )
    return blockers


# ═══════════════════════════════════════════════════════════════════════════
# Outlier managers
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ManagerProfile:
    manager_id: str
    manager_name: str
    population: int
    mean_score: Decimal
    # Deviation from the peer mean, in points. Positive is lenient.
    deviation: Decimal
    classification: str  # lenient | severe | typical | not_assessed
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "manager_id": self.manager_id,
            "manager_name": self.manager_name,
            "population": self.population,
            "mean_score": str(self.mean_score),
            "deviation": str(self.deviation),
            "classification": self.classification,
            "reason": self.reason,
        }


def detect_outliers(
    *,
    managers: list[dict[str, Any]],
    threshold: Decimal = Decimal(10),
    min_group_size: int = 4,
) -> list[ManagerProfile]:
    """Managers whose ratings sit consistently away from their peers'.

    Each entry is `{"manager_id", "manager_name", "scores": [Decimal, ...]}`.

    This is a prompt for a conversation, not a verdict. A manager may be
    lenient, or may genuinely lead a strong team — the data cannot tell those
    apart, and a facilitator asking the question is the point. Managers below
    `min_group_size` are not assessed at all: a mean over two people says
    nothing about the person who set it.
    """
    if not managers:
        return []
    if threshold <= ZERO:
        raise CalibrationError("The outlier threshold must be greater than zero.")

    assessable = [
        m for m in managers if len(m.get("scores", [])) >= min_group_size
    ]

    profiles: list[ManagerProfile] = []

    if not assessable:
        for manager in managers:
            scores = manager.get("scores", [])
            profiles.append(
                ManagerProfile(
                    manager_id=str(manager["manager_id"]),
                    manager_name=str(manager.get("manager_name", "")),
                    population=len(scores),
                    mean_score=_mean(scores),
                    deviation=ZERO,
                    classification="not_assessed",
                    reason=(
                        f"{len(scores)} rated report(s), below the {min_group_size} "
                        "needed for a mean to say anything about the rater."
                    ),
                )
            )
        return profiles

    # The peer mean is over assessable managers only, so a two-report manager's
    # extreme average does not drag the baseline it is compared against.
    peer_mean = _mean([score for m in assessable for score in m["scores"]])

    for manager in managers:
        scores = manager.get("scores", [])
        mean = _mean(scores)

        if len(scores) < min_group_size:
            profiles.append(
                ManagerProfile(
                    manager_id=str(manager["manager_id"]),
                    manager_name=str(manager.get("manager_name", "")),
                    population=len(scores),
                    mean_score=mean,
                    deviation=ZERO,
                    classification="not_assessed",
                    reason=(
                        f"{len(scores)} rated report(s), below the {min_group_size} "
                        "needed for a mean to say anything about the rater."
                    ),
                )
            )
            continue

        deviation = (mean - peer_mean).quantize(Decimal("0.01"))
        if deviation >= threshold:
            classification = "lenient"
            reason = (
                f"Mean of {mean} against a peer mean of {peer_mean} — "
                f"{deviation} points higher. Worth asking about; it may be leniency or "
                "it may be a genuinely strong team, and the data cannot tell those "
                "apart."
            )
        elif deviation <= -threshold:
            classification = "severe"
            reason = (
                f"Mean of {mean} against a peer mean of {peer_mean} — "
                f"{abs(deviation)} points lower. Worth asking about; it may be severity "
                "or it may be a team that genuinely struggled."
            )
        else:
            classification = "typical"
            reason = f"Mean of {mean}, within {threshold} points of the peer mean."

        profiles.append(
            ManagerProfile(
                manager_id=str(manager["manager_id"]),
                manager_name=str(manager.get("manager_name", "")),
                population=len(scores),
                mean_score=mean,
                deviation=deviation,
                classification=classification,
                reason=reason,
            )
        )

    return profiles


def _mean(scores: list[Decimal]) -> Decimal:
    if not scores:
        return ZERO
    for score in scores:
        if not isinstance(score, Decimal):
            raise CalibrationError("Scores must be Decimals, not floats")
    return (sum(scores, ZERO) / Decimal(len(scores))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
