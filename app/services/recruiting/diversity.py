"""Funnel by diversity dimension, and where representation actually drops.

═══════════════════════════════════════════════════════════════════════════════
THE ACTIONABLE INSIGHT IS RARELY AT THE APPLICATION STAGE
═══════════════════════════════════════════════════════════════════════════════

Almost every diversity report a company sees is "we received 22% applications
from women", which is a sourcing problem somebody has already heard about and
cannot fix this quarter. The useful finding is the *stage where representation
falls*: 30% at application, 29% at screen, 12% after the technical round is a
statement about one interview loop, and it can be acted on next week.

So `stage_representation` reports the share at each stage and names the largest
fall, which is a different question from the largest drop-off overall.

═══════════════════════════════════════════════════════════════════════════════
THIS DATA ONLY EXISTS WHERE SOMEBODY CONSENTED TO GIVE IT
═══════════════════════════════════════════════════════════════════════════════

Diversity attributes are among the most sensitive categories the DPDP framework
recognises, and the lawful basis is consent (slice 25). So a candidate with no
recorded attribute is **not** counted as "unknown and therefore probably
majority" — they are excluded from the denominator and the coverage rate is
reported beside every figure. A 30% figure computed from 40% of candidates is a
different claim from a 30% figure computed from 95%, and presenting them
identically is how a company convinces itself of something untrue.

═══════════════════════════════════════════════════════════════════════════════
AND SUPPRESSION IS NOT OPTIONAL
═══════════════════════════════════════════════════════════════════════════════

A stage with four candidates in a group identifies them. Below the threshold no
figure is produced — not a rounded one, not a range. The same rule as the survey
threshold in slice 32, for the same reason.

Pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from app.services.recruiting.funnel import CandidateJourney, Stage

__all__ = [
    "MIN_GROUP",
    "DiversityError",
    "coverage",
    "stage_representation",
]


class DiversityError(ValueError):
    """A diversity figure this module refuses to produce."""


#: Below this many candidates in a group at a stage, no figure is produced. Not
#: a rounded one, not a range. The same floor as the survey threshold.
MIN_GROUP = 5


@dataclass(frozen=True, slots=True)
class DisclosedAttribute:
    """One candidate's self-disclosed attribute, where they gave it.

    Absence means they did not disclose, and that is a different thing from any
    value the attribute can take. It is never imputed.
    """

    candidate_id: str
    dimension: str
    value: str


def coverage(
    journeys: list[CandidateJourney],
    *,
    disclosures: list[DisclosedAttribute],
    dimension: str,
) -> dict[str, Any]:
    """What share of candidates disclosed this attribute at all.

    Reported beside every figure. A 30% representation figure computed from 40%
    of candidates is a different claim from one computed from 95%, and
    presenting them identically is how a company convinces itself of something
    untrue.
    """
    disclosed = {d.candidate_id for d in disclosures if d.dimension == dimension}
    total = len(journeys)
    known = sum(1 for j in journeys if j.candidate_id in disclosed)
    rate = round(known / total, 3) if total else None

    return {
        "dimension": dimension,
        "candidates": total,
        "disclosed": known,
        "coverage_rate": rate,
        "note": (
            "Candidates who did not disclose are excluded from the denominator "
            "rather than counted as a default value. This attribute is "
            "collected under consent and an absent answer is not an answer."
            + (
                " **Coverage is low enough that these figures describe the "
                "people who answered, not the pipeline.**"
                if rate is not None and rate < 0.5
                else ""
            )
        ),
    }


def stage_representation(
    journeys: list[CandidateJourney],
    *,
    disclosures: list[DisclosedAttribute],
    dimension: str,
    focus_value: str,
    min_group: int = MIN_GROUP,
) -> dict[str, Any]:
    """Share of the focus group at each funnel stage, and where it falls.

    The largest **fall in share** is a different question from the largest
    drop-off overall: every group shrinks through a funnel, and the finding is
    the stage where one shrinks faster than the rest.
    """
    by_candidate = {d.candidate_id: d.value for d in disclosures if d.dimension == dimension}
    known = [j for j in journeys if j.candidate_id in by_candidate]

    rows: list[dict[str, Any]] = []
    for stage in Stage:
        at_stage = [j for j in known if j.reached(stage)]
        focus = [j for j in at_stage if by_candidate[j.candidate_id] == focus_value]
        others = len(at_stage) - len(focus)
        suppressed = len(at_stage) < min_group or len(focus) < min_group or others < min_group
        rows.append(
            {
                "stage": stage.value,
                "disclosed_at_stage": len(at_stage),
                "share": (None if suppressed else round(len(focus) / len(at_stage), 3)),
                "suppressed": suppressed,
                "note": (
                    ""
                    if not suppressed
                    else f"Fewer than {min_group} in a group at this stage. No "
                    "figure is produced - a stage with four candidates in a "
                    "group identifies them."
                ),
            }
        )

    visible = [r for r in rows if r["share"] is not None]
    biggest_fall = None
    for earlier, later in pairwise(visible):
        fall = earlier["share"] - later["share"]
        if biggest_fall is None or fall > biggest_fall["fall"]:
            biggest_fall = {
                "from_stage": earlier["stage"],
                "to_stage": later["stage"],
                "from_share": earlier["share"],
                "to_share": later["share"],
                "fall": round(fall, 3),
            }

    return {
        "dimension": dimension,
        "focus_value": focus_value,
        "coverage": coverage(journeys, disclosures=disclosures, dimension=dimension),
        "stages": rows,
        "largest_fall": (
            {
                **biggest_fall,
                "reading": (
                    f"Representation falls from "
                    f"{round(biggest_fall['from_share'] * 100)}% to "
                    f"{round(biggest_fall['to_share'] * 100)}% between "
                    f"{biggest_fall['from_stage']} and "
                    f"{biggest_fall['to_stage']}. That is a statement about one "
                    "part of the process and it can be looked at next week - "
                    "unlike the application-stage figure, which is a sourcing "
                    "problem somebody has already heard about."
                ),
            }
            if biggest_fall and biggest_fall["fall"] > 0
            else None
        ),
        "note": (
            "Every group shrinks through a funnel. The finding is the stage "
            "where one shrinks faster than the rest, which is rarely the "
            "application stage."
        ),
    }
