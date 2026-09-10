"""Nomination-based awards: the window, the jury, and what a juror may read.

═══════════════════════════════════════════════════════════════════════════════
AN AWARD IS NOT A LOUD SHOUTOUT
═══════════════════════════════════════════════════════════════════════════════

A shoutout is instant, peer-to-peer and unadjudicated, and it is valuable
because it is cheap. An award is periodic, adjudicated and scarce, and it is
valuable because it is not. Running the second on the machinery of the first
produces a monthly popularity contest that the winners themselves stop
respecting by the third cycle.

So this module holds the three things that make it an award: a window that
closes, a jury whose scores are independent, and a result that goes on the
employee's record permanently rather than scrolling off a feed.

═══════════════════════════════════════════════════════════════════════════════
JURY ANCHORING IS PREVENTED SERVER-SIDE, THE SAME AS INTERVIEW FEEDBACK
═══════════════════════════════════════════════════════════════════════════════

A juror who reads "9/10, outstanding, obvious winner" before scoring writes a
different score, and five opinions that anchored on the first are one opinion
with four signatures. Hiding the panel in the UI is not the control; the control
is that the server does not send the other scores.

The rule is deliberately identical to `interviews.feedback.visible_feedback_for`
— one anchoring rule in the product, applied wherever people score people.

Pure. `today` and `now` are always supplied.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any

__all__ = [
    "AwardError",
    "JuryScore",
    "Nomination",
    "NominationWindow",
    "WindowState",
    "aggregate_scores",
    "ranked_shortlist",
    "visible_scores_for",
    "window_state",
]


class AwardError(ValueError):
    """An award operation this module refuses."""


class WindowState(str, enum.Enum):
    NOT_OPEN = "not_open"
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class NominationWindow:
    """When nominations may be made.

    Both ends are **inclusive**. A window advertised as closing on the 20th and
    actually closing at 00:00 on the 20th loses every nomination made on the day
    people were told they had, and the support ticket is indistinguishable from
    a bug.
    """

    opens_on: date
    closes_on: date

    def __post_init__(self) -> None:
        if self.closes_on < self.opens_on:
            raise AwardError(
                f"A nomination window cannot close ({self.closes_on}) before it "
                f"opens ({self.opens_on})."
            )


def window_state(window: NominationWindow, *, today: date) -> WindowState:
    if today < window.opens_on:
        return WindowState.NOT_OPEN
    if today > window.closes_on:
        return WindowState.CLOSED
    return WindowState.OPEN


def check_window_open(window: NominationWindow, *, today: date) -> None:
    """Raise unless a nomination may be made today, saying when it can.

    A refusal that does not give the date sends somebody to HR to ask, and HR
    accepts it by email, which is the window not existing.
    """
    state = window_state(window, today=today)
    if state is WindowState.NOT_OPEN:
        raise AwardError(
            f"Nominations for this programme open on "
            f"{window.opens_on.isoformat()}. Nothing is lost by waiting - the "
            "citation you write today is the citation you can submit then."
        )
    if state is WindowState.CLOSED:
        raise AwardError(
            f"Nominations closed on {window.closes_on.isoformat()}. Accepting a "
            "late one would mean the jury scores a different field from the one "
            "everybody else was told about."
        )


@dataclass(frozen=True, slots=True)
class Nomination:
    """One person put forward, with the case for them."""

    nomination_id: str
    nominee_id: str
    nominator_id: str
    category_key: str
    citation: str
    #: Ids of shoutouts, goals or project records that support the citation.
    evidence_refs: tuple[str, ...] = ()
    value_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.citation.strip():
            raise AwardError(
                "A nomination needs a citation. An award decided from a name "
                "with no case attached is a vote, and a vote is a popularity "
                "contest with a jury watching."
            )
        if self.nominee_id == self.nominator_id:
            raise AwardError(
                "Somebody cannot nominate themselves. A self-nomination is not "
                "wrong in principle, but it is a different instrument and "
                "mixing the two makes the shortlist unreadable."
            )


@dataclass(frozen=True, slots=True)
class JuryScore:
    """One juror's assessment of one nomination."""

    nomination_id: str
    juror_id: str
    #: Criterion key to score. The criteria come from the programme.
    scores: dict[str, int] = field(default_factory=dict)
    comments: str = ""
    is_submitted: bool = False

    @property
    def total(self) -> float:
        if not self.scores:
            return 0.0
        return float(sum(self.scores.values()))

    @property
    def mean(self) -> float:
        if not self.scores:
            return 0.0
        return round(statistics.fmean(self.scores.values()), 2)


def visible_scores_for(
    *,
    viewer_juror_id: str,
    all_scores: list[JuryScore],
    hide_until_submitted: bool = True,
) -> list[JuryScore]:
    """What one juror may read right now.

    A juror who has not submitted sees only their own. Reading another juror's
    9/10 and "obvious winner" before writing your own measurably changes what
    you write, and five opinions that anchored on the first are one opinion.

    The same rule as interview feedback, deliberately: one anchoring rule in the
    product, applied wherever people score people.
    """
    if not hide_until_submitted:
        return list(all_scores)

    own = next((s for s in all_scores if s.juror_id == viewer_juror_id), None)
    if own is not None and own.is_submitted:
        return [s for s in all_scores if s.is_submitted]

    return [s for s in all_scores if s.juror_id == viewer_juror_id]


def outstanding_jurors(
    *, jurors: list[str], scores: list[JuryScore]
) -> list[str]:
    """Who has not scored yet, named rather than counted.

    "3 outstanding" makes somebody open five records to find out who to chase.
    """
    submitted = {s.juror_id for s in scores if s.is_submitted}
    return [j for j in jurors if j not in submitted]


@dataclass(frozen=True, slots=True)
class NominationResult:
    """One nomination's standing after the jury has scored it."""

    nomination_id: str
    nominee_id: str
    category_key: str
    juror_count: int
    mean_score: float
    per_criterion: dict[str, float]
    spread: float
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "nomination_id": self.nomination_id,
            "nominee_id": self.nominee_id,
            "category_key": self.category_key,
            "juror_count": self.juror_count,
            "mean_score": self.mean_score,
            "per_criterion": self.per_criterion,
            "spread": self.spread,
            "note": self.note,
        }


def aggregate_scores(
    nomination: Nomination, scores: list[JuryScore]
) -> NominationResult:
    """Combine the jury's submitted scores for one nomination.

    Only submitted scores count. A draft is somebody's thinking, and averaging
    it in means the standings move as people type.

    `spread` is reported because a mean of 7 from scores of 7, 7, 7 and a mean
    of 7 from 2, 9, 10 are different facts about the same nomination, and only
    one of them is a decision the jury has actually reached.
    """
    submitted = [s for s in scores if s.is_submitted]
    if not submitted:
        return NominationResult(
            nomination_id=nomination.nomination_id,
            nominee_id=nomination.nominee_id,
            category_key=nomination.category_key,
            juror_count=0,
            mean_score=0.0,
            per_criterion={},
            spread=0.0,
            note="No juror has submitted a score yet.",
        )

    criteria: dict[str, list[int]] = {}
    for score in submitted:
        for key, value in score.scores.items():
            criteria.setdefault(key, []).append(value)

    per_criterion = {
        key: round(statistics.fmean(values), 2) for key, values in criteria.items()
    }
    means = [s.mean for s in submitted]
    spread = round(max(means) - min(means), 2) if len(means) > 1 else 0.0

    return NominationResult(
        nomination_id=nomination.nomination_id,
        nominee_id=nomination.nominee_id,
        category_key=nomination.category_key,
        juror_count=len(submitted),
        mean_score=round(statistics.fmean(means), 2),
        per_criterion=per_criterion,
        spread=spread,
        note=(
            f"{len(submitted)} juror(s). Scores range {spread} apart - worth a "
            "conversation before the announcement."
            if spread >= 2
            else f"{len(submitted)} juror(s), closely agreed."
        ),
    )


def ranked_shortlist(
    results: list[NominationResult],
    *,
    category_key: str,
    minimum_jurors: int = 2,
) -> dict[str, Any]:
    """The standings in one category, with ties left as ties.

    A tie is broken by a person, not by the id that happens to sort first. An
    award decided by alphabetical order is one the runner-up eventually hears
    about.
    """
    in_category = [
        r
        for r in results
        if r.category_key == category_key and r.juror_count >= minimum_jurors
    ]
    thin = [
        r
        for r in results
        if r.category_key == category_key and r.juror_count < minimum_jurors
    ]

    ordered = sorted(in_category, key=lambda r: r.mean_score, reverse=True)
    top = ordered[0].mean_score if ordered else None
    tied = [r for r in ordered if top is not None and r.mean_score == top]

    return {
        "category_key": category_key,
        "ranked": [r.as_dict() for r in ordered],
        "leading": [r.nomination_id for r in tied],
        "tied": len(tied) > 1,
        "awaiting_jury": [r.nomination_id for r in thin],
        "note": (
            f"{len(tied)} nominations are level at the top. The jury decides "
            "between them - a tie broken by sort order is one the runner-up "
            "hears about."
            if len(tied) > 1
            else "One clear leader on the submitted scores."
        ),
    }
