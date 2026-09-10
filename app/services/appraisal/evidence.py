"""Recognition surfaced as evidence against the competency it relates to.

┌───────────────────────────────────────────────────────────────────────────┐
│ THE DIFFERENTIATOR, AND THE LINE IT DOES NOT CROSS                        │
│                                                                           │
│ A manager rating someone on "Collaboration" sees the shoutouts that       │
│ person received for collaboration, during the review period, with who     │
│ wrote them and what they said. That is culture data informing             │
│ performance, concretely, rather than as a claim.                          │
│                                                                           │
│ What it deliberately does **not** do is turn that into a score.           │
│ Recognition converted into a rating is a popularity metric wearing a      │
│ competency's name:                                                        │
│                                                                           │
│   - it rewards visibility over contribution — the people who work with    │
│     the most colleagues collect the most shoutouts, which is not the      │
│     same as collaborating best;                                           │
│   - it is trivially gamed the moment anyone works out it counts, and      │
│     someone always works it out;                                          │
│   - it punishes the person doing unglamorous work well, who is exactly    │
│     the person a manager is supposed to notice and a metric never will.   │
│                                                                           │
│ So this returns evidence a manager reads and weighs. The absence of       │
│ recognition is reported as an absence of evidence, explicitly — never as  │
│ a negative signal, because plenty of excellent people are never shouted   │
│ about.                                                                    │
└───────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

__all__ = [
    "CompetencyEvidence",
    "RecognitionItem",
    "gather_evidence",
]


@dataclass(frozen=True, slots=True)
class RecognitionItem:
    """One shoutout, as the appraisal screen shows it."""

    recognition_id: str
    value_code: str
    from_name: str
    message: str
    given_on: date

    def as_dict(self) -> dict[str, Any]:
        return {
            "recognition_id": self.recognition_id,
            "value_code": self.value_code,
            "from_name": self.from_name,
            "message": self.message,
            "given_on": self.given_on.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CompetencyEvidence:
    """What the manager sees beside one competency.

    There is no score on this object, and that is not an oversight. See the
    module docstring.
    """

    competency_code: str
    competency_name: str
    linked_value_codes: list[str]
    items: list[RecognitionItem] = field(default_factory=list)
    # Distinct colleagues who recognised this person for a linked value.
    distinct_recognisers: int = 0
    note: str = ""

    @property
    def has_evidence(self) -> bool:
        return bool(self.items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "competency_code": self.competency_code,
            "competency_name": self.competency_name,
            "linked_value_codes": self.linked_value_codes,
            "has_evidence": self.has_evidence,
            "recognition_count": len(self.items),
            "distinct_recognisers": self.distinct_recognisers,
            "items": [item.as_dict() for item in self.items],
            "note": self.note,
        }


def gather_evidence(
    *,
    competency_code: str,
    competency_name: str,
    linked_value_codes: list[str],
    recognitions: list[RecognitionItem],
    period_start: date,
    period_end: date,
) -> CompetencyEvidence:
    """Recognition for this competency's values, within the review period.

    Bounded by the period on purpose. A shoutout from three years ago is not
    evidence about this year, and letting old recognition accumulate against a
    rating would reward tenure rather than the period being reviewed.
    """
    if not linked_value_codes:
        return CompetencyEvidence(
            competency_code=competency_code,
            competency_name=competency_name,
            linked_value_codes=[],
            note=(
                f"'{competency_name}' is not linked to any company value, so there is no "
                "recognition to show against it. Link it to a value to see the evidence "
                "here."
            ),
        )

    wanted = set(linked_value_codes)
    matching = sorted(
        (
            item
            for item in recognitions
            if item.value_code in wanted and period_start <= item.given_on <= period_end
        ),
        key=lambda item: item.given_on,
        reverse=True,
    )

    if not matching:
        return CompetencyEvidence(
            competency_code=competency_code,
            competency_name=competency_name,
            linked_value_codes=sorted(wanted),
            note=(
                "No recognition was recorded against this competency's values during the "
                "review period. That is an absence of evidence, not evidence of absence "
                "— plenty of people doing this well are never shouted about, and it "
                "should not count against them."
            ),
        )

    distinct = len({item.from_name for item in matching})
    return CompetencyEvidence(
        competency_code=competency_code,
        competency_name=competency_name,
        linked_value_codes=sorted(wanted),
        items=matching,
        distinct_recognisers=distinct,
        note=(
            f"{len(matching)} recognition(s) from {distinct} colleague(s) during the "
            "review period. Read them; they do not score anything on their own."
        ),
    )
