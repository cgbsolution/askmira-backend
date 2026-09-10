"""What agents must not infer across, even from data they may read.

═══════════════════════════════════════════════════════════════════════════════
READING IS NOT THE SAME AS INFERRING
═══════════════════════════════════════════════════════════════════════════════

`retrieval.py` decides what an agent may *read*. This module decides what it may
*conclude*, and the two are different questions with different answers.

An HR admin may legitimately read that somebody took nine days of sick leave, has
a below-expectations rating this cycle, and has an open grievance. Every one of
those is in scope for them. The agent joining all three into "Priya appears to be
struggling and may not be a fit" is a conclusion nobody asked for, drawn across
categories that were never meant to be combined, about a real person, delivered
to their manager.

The harm is not disclosure — the reader could see each fact already. The harm is
the **inference**, presented with the authority of a system and none of the
context a person would have brought. So `assert_no_cross_topic_inference` fails
a response that draws on more than one sensitive topic in a single conclusion,
even when every source was readable.

═══════════════════════════════════════════════════════════════════════════════
YOUR VOICE IS NOT HERE AT ALL
═══════════════════════════════════════════════════════════════════════════════

Confidential reporting is handled one layer down, in `retrieval.FORBIDDEN_SOURCES`
— it is never retrieved, so there is nothing here to reason about. This module
covers the topics an agent *can* read and must not join, which is a strictly
weaker guarantee and belongs at a different layer.

Pure.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "SensitiveTopic",
    "TopicFinding",
    "assert_no_cross_topic_inference",
    "topics_in",
]


class SensitiveTopic(str, enum.Enum):
    """Categories an agent must not join into a conclusion about a person."""

    HEALTH = "health"
    PERFORMANCE_CONCERN = "performance_concern"
    DISCIPLINARY = "disciplinary"
    GRIEVANCE = "grievance"
    #: Pregnancy, adoption, bereavement, caregiving. Protected in ways that make
    #: an inference drawn from them a discrimination question, not a data one.
    FAMILY_CIRCUMSTANCE = "family_circumstance"
    FINANCIAL_DIFFICULTY = "financial_difficulty"

    @property
    def why_it_matters(self) -> str:
        return {
            SensitiveTopic.HEALTH: (
                "an inference from health data about somebody's suitability is "
                "the shape of a disability discrimination claim"
            ),
            SensitiveTopic.PERFORMANCE_CONCERN: (
                "a rating is one manager's judgement in one cycle, and a system "
                "restating it as a characteristic makes it permanent"
            ),
            SensitiveTopic.DISCIPLINARY: (
                "a disciplinary record names a complainant as well as a "
                "respondent, and neither consented to it becoming a signal"
            ),
            SensitiveTopic.GRIEVANCE: (
                "somebody who raised a grievance must not find it counting "
                "against them in an unrelated assessment"
            ),
            SensitiveTopic.FAMILY_CIRCUMSTANCE: (
                "maternity, adoption and caregiving are protected, and an "
                "inference drawn from them is a discrimination question"
            ),
            SensitiveTopic.FINANCIAL_DIFFICULTY: (
                "a salary advance or a loan request says something about "
                "somebody's life that has no bearing on their work"
            ),
        }[self]


#: What marks a piece of text as touching a topic. Matched on the response, not
#: on the source data — the question is what the agent *said*, and an agent can
#: reach a health inference from an attendance pattern without ever reading a
#: medical record.
_TOPIC_MARKERS: dict[SensitiveTopic, re.Pattern[str]] = {
    SensitiveTopic.HEALTH: re.compile(
        r"\b(sick|illness|ill health|medical|hospital|therapy|mental health|"
        r"disability|disabled|condition|surgery|recovery|unwell)\b",
        re.IGNORECASE,
    ),
    SensitiveTopic.PERFORMANCE_CONCERN: re.compile(
        r"\b(underperform\w*|below expectations|poor performance|pip|"
        r"performance improvement|struggling|not meeting)\b",
        re.IGNORECASE,
    ),
    SensitiveTopic.DISCIPLINARY: re.compile(
        r"\b(disciplinary|warning letter|misconduct|enquiry|show cause|posh)\b",
        re.IGNORECASE,
    ),
    SensitiveTopic.GRIEVANCE: re.compile(
        r"\b(grievance|complaint|raised a concern|escalated an issue)\b",
        re.IGNORECASE,
    ),
    SensitiveTopic.FAMILY_CIRCUMSTANCE: re.compile(
        r"\b(maternity|paternity|pregnan\w*|adoption|bereavement|caregiv\w*|"
        r"child care|elder care)\b",
        re.IGNORECASE,
    ),
    SensitiveTopic.FINANCIAL_DIFFICULTY: re.compile(
        r"\b(salary advance|loan request|garnish\w*|financial difficulty|"
        r"unable to repay)\b",
        re.IGNORECASE,
    ),
}

#: Language that turns observations into a conclusion about a person. The
#: combination of two topics *and* one of these is what gets refused — an agent
#: listing facts across categories because it was asked for a summary of the
#: record is doing its job.
_CONCLUSION_MARKERS = re.compile(
    r"\b(suggests?|indicates?|appears? to be|seems? to be|likely|probably|"
    r"may not be|not a (good )?fit|risk of|concern about|pattern of|"
    r"recommend\w*|should be (managed|moved|exited|reviewed))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TopicFinding:
    """One sensitive topic present in a piece of text, and where."""

    topic: SensitiveTopic
    matched: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic.value,
            "matched": self.matched,
            "why_it_matters": self.topic.why_it_matters,
        }


def topics_in(text: str) -> list[TopicFinding]:
    """Which sensitive topics this text touches."""
    found: list[TopicFinding] = []
    for topic, pattern in _TOPIC_MARKERS.items():
        match = pattern.search(text)
        if match:
            found.append(TopicFinding(topic=topic, matched=match.group(0)))
    return found


def assert_no_cross_topic_inference(text: str, *, subject: str = "this person") -> None:
    """Refuse a conclusion drawn across two or more sensitive topics.

    Raises. The three conditions all have to hold — two topics, and language
    that draws a conclusion — because a factual answer that happens to mention
    both sick leave and a rating is legitimate and common. It is the joining
    that is refused, not the mentioning.
    """
    findings = topics_in(text)
    if len(findings) < 2:
        return
    if not _CONCLUSION_MARKERS.search(text):
        return

    topics = ", ".join(f.topic.value for f in findings)
    reasons = "; ".join(f.topic.why_it_matters for f in findings)
    raise ValueError(
        f"This draws a conclusion about {subject} across {len(findings)} "
        f"sensitive topics ({topics}). Each fact may be readable on its own; "
        "joining them into a judgement is not something an agent does. "
        f"Specifically: {reasons}. State the facts with their sources and let a "
        "person draw the conclusion, with the context they have and this system "
        "does not."
    )


def safe_summary_note() -> str:
    """The line an agent adds when it declines to join the dots.

    Written to be shown to the person who asked, not logged. Somebody who asked
    for a summary and got facts instead is owed an explanation of why.
    """
    return (
        "I have listed what the record says, with sources, rather than drawing a "
        "conclusion from it. These facts sit in categories that should not be "
        "combined into a judgement about a person by a system — that is a "
        "decision for somebody who knows the context."
    )
