"""Structured feedback, anchoring protection, and the gate on advancing.

═══════════════════════════════════════════════════════════════════════════════
THREE RULES, EACH FOR A DIFFERENT REASON
═══════════════════════════════════════════════════════════════════════════════

**Feedback is mandatory before a candidate advances.** Not encouraged, enforced.
Unrecorded verbal feedback is how a hiring decision becomes undefendable six
months later when somebody asks why one of two similar candidates was rejected —
and it is how bias survives, because nothing written down can be examined.

**Panelists cannot see each other's feedback before submitting their own.** The
anchoring effect is large and well documented: a junior interviewer who reads
"STRONG_YES, brilliant" writes a better review than they would have. Hiding peer
feedback until submission costs nothing and is the difference between four
opinions and one opinion repeated four times. Configurable per tenant because
some clients run deliberately collaborative panels — but the default is hidden,
because the default is what most tenants will run.

**A recommendation is never averaged into a number.** STRONG_NO and STRONG_YES
do not cancel out to "neutral"; they mean the panel disagrees, which is the most
informative thing that could have happened and the thing an average destroys.
The consolidation reports the spread and names the dissent.

Pure. No database.
"""

from __future__ import annotations

import enum
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "Consolidation",
    "FeedbackError",
    "PanelFeedback",
    "Recommendation",
    "consolidate",
    "missing_feedback",
    "visible_feedback_for",
]


#: Every competency is rated on one scale, so two panelists' numbers mean the
#: same thing. The web and mobile scorecards ask for it; this is where it holds.
RATING_FLOOR = 1
RATING_CEILING = 10


class FeedbackError(ValueError):
    """Feedback that cannot be recorded as given."""


class Recommendation(str, enum.Enum):
    """Deliberately four values with no middle.

    A five-point scale with a neutral centre collects neutrals. An interviewer
    who genuinely cannot decide should say so in `concerns`, where somebody will
    read it, rather than in a score that averages away.
    """

    STRONG_YES = "strong_yes"
    YES = "yes"
    NO = "no"
    STRONG_NO = "strong_no"

    @property
    def is_positive(self) -> bool:
        return self in (Recommendation.STRONG_YES, Recommendation.YES)

    @property
    def is_strong(self) -> bool:
        return self in (Recommendation.STRONG_YES, Recommendation.STRONG_NO)


@dataclass(frozen=True, slots=True)
class PanelFeedback:
    """One panelist's view of one interview."""

    panelist_id: str
    interview_id: str
    recommendation: Recommendation
    #: {competency_code: score}. Structured against the slice 16 framework so
    #: two interviewers' views are comparable rather than two essays.
    ratings: dict[str, Decimal] = field(default_factory=dict)
    strengths: str = ""
    concerns: str = ""
    submitted_at: datetime | None = None
    panelist_label: str = ""
    is_chair: bool = False

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None


def visible_feedback_for(
    *,
    viewer_panelist_id: str,
    all_feedback: list[PanelFeedback],
    hide_until_submitted: bool = True,
    viewer_is_hiring_manager: bool = False,
) -> list[PanelFeedback]:
    """What one person is allowed to read right now.

    A panelist who has not submitted sees only their own. Reading "STRONG_YES,
    brilliant" before writing your own review measurably changes what you write,
    and four opinions that anchored on the first are one opinion.

    The hiring manager is not exempt while they are also on the panel: their
    review anchors just as easily, and it anchors everyone else's too.
    """
    if not hide_until_submitted:
        return list(all_feedback)

    own = next((f for f in all_feedback if f.panelist_id == viewer_panelist_id), None)
    viewer_has_submitted = own is not None and own.is_submitted

    if viewer_has_submitted:
        return [f for f in all_feedback if f.is_submitted]

    if viewer_is_hiring_manager and own is None:
        # A hiring manager who is not on the panel has nothing to anchor. They
        # still only see what has actually been submitted.
        return [f for f in all_feedback if f.is_submitted]

    return [f for f in all_feedback if f.panelist_id == viewer_panelist_id]


def missing_feedback(
    *,
    panelists: list[tuple[str, str, bool]],
    feedback: list[PanelFeedback],
) -> list[str]:
    """Who still owes feedback. Empty means the stage can close.

    `panelists` is `(panelist_id, label, is_required)`. Observers are not
    required — an observer who had to file a review would either stop observing
    or start filing empty ones.

    Named rather than counted: "2 outstanding" makes a recruiter open four
    records to find out who to chase.
    """
    submitted = {f.panelist_id for f in feedback if f.is_submitted}
    return [
        label or panelist_id
        for panelist_id, label, is_required in panelists
        if is_required and panelist_id not in submitted
    ]


@dataclass(frozen=True, slots=True)
class Consolidation:
    """What the panel actually said, for the hiring manager.

    No overall score, deliberately. See the module docstring.
    """

    total_required: int
    submitted: int
    counts: dict[str, int]
    is_unanimous: bool
    has_strong_dissent: bool
    headline: str
    per_competency: dict[str, dict[str, Any]]
    outstanding: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.outstanding

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_required": self.total_required,
            "submitted": self.submitted,
            "counts": self.counts,
            "unanimous": self.is_unanimous,
            "strong_dissent": self.has_strong_dissent,
            "headline": self.headline,
            "per_competency": self.per_competency,
            "outstanding": list(self.outstanding),
            "complete": self.is_complete,
        }


def consolidate(
    *,
    feedback: list[PanelFeedback],
    panelists: list[tuple[str, str, bool]],
) -> Consolidation:
    """The panel's view, with the disagreement kept rather than averaged out.

    STRONG_NO and STRONG_YES in the same panel is the most informative thing
    that can happen in a hiring process. An average turns it into "neutral",
    which is the one reading that is definitely wrong.
    """
    submitted = [f for f in feedback if f.is_submitted]
    outstanding = missing_feedback(panelists=panelists, feedback=feedback)
    required = sum(1 for _, _, is_required in panelists if is_required)

    counts = Counter(f.recommendation.value for f in submitted)
    positives = sum(1 for f in submitted if f.recommendation.is_positive)
    negatives = len(submitted) - positives
    unanimous = bool(submitted) and (positives == 0 or negatives == 0)

    strong_dissent = any(f.recommendation is Recommendation.STRONG_YES for f in submitted) and any(
        f.recommendation is Recommendation.STRONG_NO for f in submitted
    )

    if not submitted:
        headline = "No feedback submitted yet. Nothing here says anything about this candidate."
    elif strong_dissent:
        yes_names = [
            f.panelist_label or f.panelist_id
            for f in submitted
            if f.recommendation is Recommendation.STRONG_YES
        ]
        no_names = [
            f.panelist_label or f.panelist_id
            for f in submitted
            if f.recommendation is Recommendation.STRONG_NO
        ]
        headline = (
            f"The panel disagrees strongly: {', '.join(yes_names)} said strong yes and "
            f"{', '.join(no_names)} said strong no. That is worth a conversation before "
            "a decision, not an average."
        )
    elif unanimous and positives:
        headline = f"All {len(submitted)} panelists recommend proceeding."
    elif unanimous:
        headline = f"All {len(submitted)} panelists recommend not proceeding."
    else:
        headline = (
            f"Split panel: {positives} for, {negatives} against. The concerns below are "
            "where the disagreement is."
        )

    if outstanding:
        headline += f" Still waiting on {len(outstanding)}: {', '.join(outstanding)}."

    # Per competency, the spread rather than the mean — two interviewers scoring
    # 2 and 8 is not a 5, it is a disagreement about the same person.
    per_competency: dict[str, dict[str, Any]] = {}
    codes = {code for f in submitted for code in f.ratings}
    for code in sorted(codes):
        scores = [
            (f.panelist_label or f.panelist_id, Decimal(str(f.ratings[code])))
            for f in submitted
            if code in f.ratings
        ]
        values = [score for _, score in scores]
        per_competency[code] = {
            "scores": {name: str(value) for name, value in scores},
            "lowest": str(min(values)),
            "highest": str(max(values)),
            "spread": str(max(values) - min(values)),
            "raters": len(values),
        }

    return Consolidation(
        total_required=required,
        submitted=len(submitted),
        counts=dict(counts),
        is_unanimous=unanimous,
        has_strong_dissent=strong_dissent,
        headline=headline,
        per_competency=per_competency,
        outstanding=tuple(outstanding),
    )


def validate_feedback(
    *,
    recommendation: Recommendation,
    ratings: dict[str, Decimal],
    required_competencies: list[str],
    concerns: str,
) -> None:
    """Refuse feedback that cannot be acted on.

    A negative recommendation with no stated concern is the specific thing that
    makes a rejection undefendable, so it is the one case where prose is
    required rather than encouraged.
    """
    off_scale = sorted(
        code
        for code, value in ratings.items()
        if value != value.to_integral_value() or not RATING_FLOOR <= value <= RATING_CEILING
    )
    if off_scale:
        raise FeedbackError(
            f"These ratings are off the scale: {off_scale}. Each competency is rated "
            f"with a whole number from {RATING_FLOOR} to {RATING_CEILING}, the same scale "
            "every other panelist used."
        )

    missing = [code for code in required_competencies if code not in ratings]
    if missing:
        raise FeedbackError(
            f"These competencies were not rated: {sorted(missing)}. Feedback structured "
            "against the framework is what makes two interviewers comparable; a partial "
            "form is two essays again."
        )

    if not recommendation.is_positive and not concerns.strip():
        raise FeedbackError(
            "A recommendation against needs a stated concern. 'No' with nothing behind "
            "it cannot be explained to the candidate, cannot be examined for bias, and "
            "cannot be defended if the decision is ever questioned."
        )
