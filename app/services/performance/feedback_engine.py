"""The arithmetic of 360 feedback, with no database: who may see what, and the summary.

Pure, so the anonymity rule is testable on its own. The service loads the rows
and asks this module two questions: what may this viewer see, and what does the
anonymised summary say.

The summary is deliberately plain — an average per competency, the average of
the optional overall rating, and the written answers in no particular order.
The written answers are sorted by their text rather than kept in the order
they arrived: submission order plus "who did I nag on Tuesday" is enough to
work out who wrote what.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "ANONYMITY_FLOOR",
    "MAX_REVIEWERS",
    "Aggregate",
    "Answer",
    "aggregate",
    "subject_view",
]

#: The fewest answers an anonymised summary is shown on. Below this the
#: person the feedback is about sees how many answered and nothing else.
ANONYMITY_FLOOR = 3
#: Colleagues asked in one request. More than this is a survey, not 360.
MAX_REVIEWERS = 8

_TWO = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Answer:
    """One submitted response, as the engine needs it."""

    ratings: dict[str, Decimal | None]
    comments: dict[str, str]
    strengths: str | None
    improvements: str | None
    overall: Decimal | None


@dataclass(slots=True)
class Aggregate:
    responses: int
    overall_average: Decimal | None
    overall_count: int
    competencies: list[dict[str, Any]] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(_TWO, ROUND_HALF_UP)


def _texts(values: Iterable[str | None]) -> list[str]:
    return sorted({v.strip() for v in values if v and v.strip()}, key=str.casefold)


def aggregate(answers: Sequence[Answer], questions: Sequence[dict[str, Any]]) -> Aggregate:
    """Averages and the written answers, with nothing that says who wrote them."""
    competencies = []
    for question in questions:
        key = str(question["competency_id"])
        ratings = [a.ratings[key] for a in answers if a.ratings.get(key) is not None]
        competencies.append(
            {
                "competency_id": key,
                "name": question.get("name", ""),
                "average": _mean(ratings),  # type: ignore[arg-type]
                "responses": len(ratings),
                "comments": _texts(a.comments.get(key) for a in answers),
            }
        )
    overall = [a.overall for a in answers if a.overall is not None]
    return Aggregate(
        responses=len(answers),
        overall_average=_mean(overall),
        overall_count=len(overall),
        competencies=competencies,
        strengths=_texts(a.strengths for a in answers),
        improvements=_texts(a.improvements for a in answers),
    )


def subject_view(visibility: str, submitted: int, threshold: int = ANONYMITY_FLOOR) -> str:
    """What the person the feedback is about may see.

    - ``named``: every answer with its writer's name;
    - ``summary``: the anonymised aggregate;
    - ``waiting``: counts only, because fewer than the threshold answered;
    - ``none``: counts only, because the request is for the manager line.
    """
    if visibility == "named":
        return "named"
    if visibility == "manager_only":
        return "none"
    return "summary" if submitted >= max(threshold, ANONYMITY_FLOOR) else "waiting"
