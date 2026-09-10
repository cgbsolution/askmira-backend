"""Slice 16 gate: recognition surfaces against the matching competency.

The differentiator — and the line it does not cross. Recognition is evidence a
manager reads, never a score that computes itself.
"""

from __future__ import annotations

from datetime import date

from app.services.appraisal.evidence import (
    CompetencyEvidence,
    RecognitionItem,
    gather_evidence,
)

PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2027, 3, 31)


def shoutout(**overrides) -> RecognitionItem:
    base = {
        "recognition_id": "r1",
        "value_code": "COLLABORATION",
        "from_name": "Rahul Iyer",
        "message": "Stayed late to unblock the payments migration for our team.",
        "given_on": date(2026, 9, 12),
    }
    base.update(overrides)
    return RecognitionItem(**base)


def evidence(**overrides) -> CompetencyEvidence:
    base = {
        "competency_code": "COLLAB",
        "competency_name": "Collaboration",
        "linked_value_codes": ["COLLABORATION"],
        "recognitions": [shoutout()],
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
    }
    base.update(overrides)
    return gather_evidence(**base)


# ═══════════════════════════════════════════════════════════════════════════
# The differentiator working
# ═══════════════════════════════════════════════════════════════════════════


def test_recognition_surfaces_against_the_matching_competency() -> None:
    """The checklist gate.

    A manager rating "Collaboration" sees the shoutouts for collaboration.
    """
    result = evidence()

    assert result.has_evidence is True
    assert len(result.items) == 1
    assert result.items[0].message.startswith("Stayed late")
    assert result.items[0].from_name == "Rahul Iyer"


def test_recognition_for_a_different_value_does_not_surface() -> None:
    """A shoutout for "Ownership" is not evidence about collaboration."""
    result = evidence(recognitions=[shoutout(value_code="OWNERSHIP")])
    assert result.has_evidence is False


def test_a_competency_can_map_to_several_values() -> None:
    result = evidence(
        linked_value_codes=["COLLABORATION", "TEAMWORK"],
        recognitions=[shoutout(), shoutout(recognition_id="r2", value_code="TEAMWORK")],
    )
    assert len(result.items) == 2


def test_recognition_is_ordered_most_recent_first() -> None:
    result = evidence(
        recognitions=[
            shoutout(recognition_id="old", given_on=date(2026, 5, 1)),
            shoutout(recognition_id="new", given_on=date(2027, 1, 15)),
        ]
    )
    assert [item.recognition_id for item in result.items] == ["new", "old"]


def test_distinct_recognisers_are_counted() -> None:
    """Ten shoutouts from one person is a different signal from ten people."""
    result = evidence(
        recognitions=[
            shoutout(recognition_id="a", from_name="Rahul"),
            shoutout(recognition_id="b", from_name="Rahul"),
            shoutout(recognition_id="c", from_name="Priya"),
        ]
    )
    assert len(result.items) == 3
    assert result.distinct_recognisers == 2


# ═══════════════════════════════════════════════════════════════════════════
# The line it does not cross
# ═══════════════════════════════════════════════════════════════════════════


def test_the_evidence_carries_no_score() -> None:
    """Recognition converted into a rating is a popularity metric wearing a
    competency's name.

    It rewards visibility over contribution, it is gamed the moment anyone
    works out it counts, and it punishes the person doing unglamorous work
    well — who is exactly the person a manager is supposed to notice.
    """
    payload = evidence().as_dict()

    assert "score" not in payload
    assert "rating" not in payload
    assert not any("score" in key for key in payload)
    assert "they do not score anything on their own" in payload["note"]


def test_an_absence_of_recognition_is_reported_as_an_absence_of_evidence() -> None:
    """Not as a negative signal. Plenty of excellent people are never shouted
    about."""
    result = evidence(recognitions=[])

    assert result.has_evidence is False
    assert "absence of evidence, not evidence of absence" in result.note
    assert "should not count against them" in result.note


# ═══════════════════════════════════════════════════════════════════════════
# The review period
# ═══════════════════════════════════════════════════════════════════════════


def test_recognition_before_the_period_is_excluded() -> None:
    """A shoutout from three years ago is not evidence about this year, and
    letting old recognition accumulate would reward tenure."""
    result = evidence(recognitions=[shoutout(given_on=date(2024, 6, 1))])
    assert result.has_evidence is False


def test_recognition_after_the_period_is_excluded() -> None:
    result = evidence(recognitions=[shoutout(given_on=date(2027, 6, 1))])
    assert result.has_evidence is False


def test_recognition_on_the_period_boundaries_is_included() -> None:
    """Inclusive on both ends — a shoutout on the first day of the year under
    review is about that year."""
    first = evidence(recognitions=[shoutout(given_on=PERIOD_START)])
    last = evidence(recognitions=[shoutout(given_on=PERIOD_END)])
    assert first.has_evidence is True
    assert last.has_evidence is True


# ═══════════════════════════════════════════════════════════════════════════
# Configuration gaps
# ═══════════════════════════════════════════════════════════════════════════


def test_an_unlinked_competency_says_how_to_fix_it() -> None:
    """Rather than silently showing nothing, which looks identical to having
    no recognition."""
    result = evidence(linked_value_codes=[], recognitions=[shoutout()])

    assert result.has_evidence is False
    assert "not linked to any company value" in result.note
    assert "Link it to a value" in result.note


def test_the_evidence_is_json_serialisable() -> None:
    import json

    payload = json.loads(json.dumps(evidence().as_dict()))
    assert payload["recognition_count"] == 1
    assert payload["items"][0]["given_on"] == "2026-09-12"
