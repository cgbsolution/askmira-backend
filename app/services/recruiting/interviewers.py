"""Interviewer signals, framed as coaching rather than as a scorecard.

═══════════════════════════════════════════════════════════════════════════════
THIS IS THE MOST DANGEROUS ANALYTICS IN THE PRODUCT
═══════════════════════════════════════════════════════════════════════════════

An interviewer who learns that their pass rate is measured will raise it. That
is not cynicism, it is what measurement does, and the resulting hires are worse
while every number on the dashboard improves.

So everything here is shaped to be usable as coaching and awkward to use as a
scorecard:

- **No ranking.** No "top interviewer", no ordered list, no percentile. A league
  table is the artefact people optimise against, and once it exists somebody
  will screenshot it.
- **No verdict.** Severity is reported as a deviation with its sample size and
  an explicit statement that being strict is not being wrong. A demanding
  interviewer on a hard role should be strict.
- **A floor on the sample.** Below it, nothing is reported at all. Nobody's
  interviewing is characterised from four conversations.
- **`framing`** travels in the payload so the UI cannot present it as a score
  without deliberately dropping the sentence that says it is not one.

═══════════════════════════════════════════════════════════════════════════════
THE FEEDBACK QUALITY PROXY IS A PROXY, AND SAYS SO
═══════════════════════════════════════════════════════════════════════════════

Length, competency coverage and whether the recommendation matches the ratings.
None of those is quality; all of them correlate with it. A four-word review that
says "no - cannot write SQL, tested twice" is better than four paragraphs of
impressions, and the metric would score it lower. So it is named a proxy
everywhere it appears.

The one signal that is genuinely diagnostic is **incoherence**: a recommendation
of "strong hire" attached to ratings averaging 2 out of 5 means the form was
filled in afterwards, and that is worth a conversation regardless of length.

Pure.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

__all__ = [
    "COACHING_FRAMING",
    "MIN_INTERVIEWS_FOR_SIGNAL",
    "FeedbackFacts",
    "InterviewerError",
    "feedback_quality",
    "severity",
    "turnaround",
]


class InterviewerError(ValueError):
    """An interviewer figure this module refuses to produce."""


#: Below this many interviews, nothing is reported. Nobody's interviewing is
#: characterised from four conversations, and a number attached to a name is
#: repeated long after the caveat is forgotten.
MIN_INTERVIEWS_FOR_SIGNAL = 10

#: Travels with every payload. A UI that wants to present this as a score has to
#: deliberately drop the sentence saying it is not one.
COACHING_FRAMING = (
    "These are coaching signals, not a scorecard. An interviewer who learns "
    "their pass rate is measured will raise it, and the hires get worse while "
    "the dashboard improves. Being stricter than peers is not being wrong - a "
    "demanding interviewer on a hard role should be strict. What is worth a "
    "conversation is a large deviation the interviewer cannot explain, and "
    "feedback whose recommendation does not match its own ratings."
)


@dataclass(frozen=True, slots=True)
class FeedbackFacts:
    """One piece of interview feedback."""

    feedback_id: str
    interviewer_id: str
    candidate_id: str
    #: 1-5 per competency.
    competency_ratings: dict[str, int]
    #: The overall call, normalised to 1-5 the same way.
    recommendation_score: int
    comment_length: int
    #: Days between the interview and the feedback being submitted.
    turnaround_days: float
    advanced: bool

    @property
    def mean_rating(self) -> float:
        if not self.competency_ratings:
            return 0.0
        return statistics.fmean(self.competency_ratings.values())

    @property
    def is_incoherent(self) -> bool:
        """The recommendation and the ratings disagree by two points or more.

        This is the one genuinely diagnostic signal here: "strong hire" attached
        to ratings averaging 2 means the form was filled in afterwards, and that
        is worth a conversation whatever the word count.
        """
        if not self.competency_ratings:
            return False
        return abs(self.recommendation_score - self.mean_rating) >= 2.0


def severity(
    feedback: list[FeedbackFacts],
    *,
    interviewer_id: str,
    minimum: int = MIN_INTERVIEWS_FOR_SIGNAL,
) -> dict[str, Any]:
    """How this interviewer's ratings sit against everybody else's.

    The same technique as manager leniency in calibration, and the same
    restraint: a deviation is reported with its sample size and no verdict
    attached. There is deliberately no rank and no percentile - a league table
    is the artefact people optimise against.
    """
    mine = [f for f in feedback if f.interviewer_id == interviewer_id]
    others = [f for f in feedback if f.interviewer_id != interviewer_id]

    if len(mine) < minimum:
        return {
            "interviewer_id": interviewer_id,
            "interviews": len(mine),
            "mean_rating": None,
            "peer_mean_rating": None,
            "deviation": None,
            "framing": COACHING_FRAMING,
            "note": (
                f"{len(mine)} interview(s) - below the {minimum} needed. Nobody's "
                "interviewing is characterised from a handful of conversations, "
                "and a number attached to a name is repeated long after the "
                "caveat is forgotten."
            ),
        }
    if len(others) < minimum:
        return {
            "interviewer_id": interviewer_id,
            "interviews": len(mine),
            "mean_rating": round(statistics.fmean([f.mean_rating for f in mine]), 2),
            "peer_mean_rating": None,
            "deviation": None,
            "framing": COACHING_FRAMING,
            "note": (
                "Not enough interviews by other people to compare against. A "
                "deviation from a peer group of three is a deviation from three "
                "individuals."
            ),
        }

    mine_mean = statistics.fmean([f.mean_rating for f in mine])
    peer_mean = statistics.fmean([f.mean_rating for f in others])
    deviation = round(mine_mean - peer_mean, 2)
    advanced_rate = round(sum(1 for f in mine if f.advanced) / len(mine), 3)
    peer_advanced = round(sum(1 for f in others if f.advanced) / len(others), 3)

    if abs(deviation) < 0.3:
        reading = (
            "In line with peers. Nothing to discuss - which is itself worth "
            "knowing, because most people assume they are harsher than average."
        )
    elif deviation < 0:
        reading = (
            f"Rates {abs(deviation)} points below peers across {len(mine)} "
            "interviews. That may be the roles they see, the stage they "
            "interview at, or a standard they hold and can articulate. It is a "
            "question, not a finding."
        )
    else:
        reading = (
            f"Rates {deviation} points above peers across {len(mine)} "
            "interviews. Worth asking what they are seeing that others are "
            "not - the answer is sometimes that they are right."
        )

    return {
        "interviewer_id": interviewer_id,
        "interviews": len(mine),
        "mean_rating": round(mine_mean, 2),
        "peer_mean_rating": round(peer_mean, 2),
        "deviation": deviation,
        "advance_rate": advanced_rate,
        "peer_advance_rate": peer_advanced,
        "reading": reading,
        "framing": COACHING_FRAMING,
    }


def turnaround(
    feedback: list[FeedbackFacts],
    *,
    interviewer_id: str,
    minimum: int = MIN_INTERVIEWS_FOR_SIGNAL,
) -> dict[str, Any]:
    """How long feedback takes to arrive.

    The one metric here that is fair to hold somebody to, because it is entirely
    within their control and a candidate waiting eleven days for a decision is a
    candidate who accepts a different offer. Reported with the median, and the
    worst case beside it - a median of two days hides the one that took three
    weeks, and that is the one that lost a hire.
    """
    mine = [f for f in feedback if f.interviewer_id == interviewer_id]
    if len(mine) < minimum:
        return {
            "interviewer_id": interviewer_id,
            "interviews": len(mine),
            "median_days": None,
            "framing": COACHING_FRAMING,
            "note": f"{len(mine)} interview(s) - below the {minimum} needed.",
        }

    days = [f.turnaround_days for f in mine]
    return {
        "interviewer_id": interviewer_id,
        "interviews": len(mine),
        "median_days": round(statistics.median(days), 1),
        "slowest_days": round(max(days), 1),
        "over_two_days": sum(1 for d in days if d > 2),
        "reading": (
            f"Median {round(statistics.median(days), 1)} days, slowest "
            f"{round(max(days), 1)}. The slowest one matters: a median of two "
            "days hides the three-week wait that lost a candidate to another "
            "offer."
        ),
        "framing": COACHING_FRAMING,
    }


def feedback_quality(
    feedback: list[FeedbackFacts],
    *,
    interviewer_id: str,
    expected_competencies: int = 4,
    minimum: int = MIN_INTERVIEWS_FOR_SIGNAL,
) -> dict[str, Any]:
    """A proxy for feedback quality, named as a proxy everywhere.

    Length and competency coverage correlate with usefulness and are not it: a
    four-word review saying "no - cannot write SQL, tested twice" is better than
    four paragraphs of impressions, and this would score it lower.

    Incoherence is the part that is genuinely diagnostic. A recommendation that
    does not match the interviewer's own ratings means the form was completed
    after the decision, and that is worth a conversation whatever the word
    count.
    """
    mine = [f for f in feedback if f.interviewer_id == interviewer_id]
    if len(mine) < minimum:
        return {
            "interviewer_id": interviewer_id,
            "interviews": len(mine),
            "median_comment_length": None,
            "competency_coverage": None,
            "incoherent_count": None,
            "framing": COACHING_FRAMING,
            "note": f"{len(mine)} interview(s) - below the {minimum} needed.",
        }

    coverage = statistics.fmean(
        [
            min(1.0, len(f.competency_ratings) / expected_competencies)
            for f in mine
        ]
    )
    incoherent = [f for f in mine if f.is_incoherent]

    return {
        "interviewer_id": interviewer_id,
        "interviews": len(mine),
        "median_comment_length": round(
            statistics.median([f.comment_length for f in mine]), 1
        ),
        "competency_coverage": round(coverage, 2),
        "incoherent_count": len(incoherent),
        "incoherent_feedback_ids": [f.feedback_id for f in incoherent],
        "reading": (
            f"{len(incoherent)} of {len(mine)} reviews recommend something their "
            "own ratings do not support. That usually means the form was filled "
            "in after the decision, and it is the only signal here worth acting "
            "on directly."
            if incoherent
            else "No review contradicts its own ratings."
        ),
        "proxy_caveat": (
            "Length and coverage are **proxies** and poor ones. A four-word "
            "review saying 'no - cannot write SQL, tested twice' is better than "
            "four paragraphs of impressions, and this metric scores it lower. "
            "Use them to start a conversation, never to conclude one."
        ),
        "framing": COACHING_FRAMING,
    }
