"""The 360 feedback engine: who sees what, and the anonymised summary."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.employee import Employee
from app.services.documents.acknowledgement import names_match
from app.services.performance import feedback_engine as engine

D = Decimal

QUESTIONS = [
    {"competency_id": "c1", "name": "Collaboration"},
    {"competency_id": "c2", "name": "Ownership"},
]


def answer(c1=None, c2=None, overall=None, strengths=None, improvements=None, comment=None):
    return engine.Answer(
        ratings={"c1": D(c1) if c1 else None, "c2": D(c2) if c2 else None},
        comments={"c1": comment} if comment else {},
        strengths=strengths,
        improvements=improvements,
        overall=D(overall) if overall else None,
    )


@pytest.mark.parametrize(
    ("visibility", "submitted", "expected"),
    [
        ("anonymised", 0, "waiting"),
        ("anonymised", 2, "waiting"),
        ("anonymised", 3, "summary"),
        ("anonymised", 7, "summary"),
        ("named", 1, "named"),
        ("manager_only", 5, "none"),
    ],
)
def test_the_subject_sees_a_summary_only_from_three_answers(visibility, submitted, expected):
    assert engine.subject_view(visibility, submitted) == expected


def test_a_threshold_below_the_floor_cannot_lower_it():
    """A request stored with threshold 1 still waits for three."""
    assert engine.subject_view("anonymised", 2, threshold=1) == "waiting"
    assert engine.subject_view("anonymised", 4, threshold=5) == "waiting"


def test_the_summary_averages_only_the_answers_that_rated():
    result = engine.aggregate(
        [
            answer(c1="4", c2="3", overall="5"),
            answer(c1="3", overall="4"),
            answer(c1="5"),
        ],
        QUESTIONS,
    )
    assert result.responses == 3
    by_id = {c["competency_id"]: c for c in result.competencies}
    assert by_id["c1"]["average"] == D("4.00") and by_id["c1"]["responses"] == 3
    assert by_id["c2"]["average"] == D("3.00") and by_id["c2"]["responses"] == 1
    assert result.overall_average == D("4.50") and result.overall_count == 2


def test_written_answers_carry_no_order_that_identifies_the_writer():
    result = engine.aggregate(
        [
            answer(strengths="Unblocks people", comment="zeal"),
            answer(strengths="calm under pressure", comment="Alpha"),
            answer(strengths="Unblocks people"),
            answer(strengths="  "),
        ],
        QUESTIONS,
    )
    # Sorted by text, blanks dropped, duplicates merged.
    assert result.strengths == ["calm under pressure", "Unblocks people"]
    assert result.competencies[0]["comments"] == ["Alpha", "zeal"]
    assert result.improvements == []


def test_an_empty_summary_says_nothing_rather_than_zero():
    result = engine.aggregate([], QUESTIONS)
    assert result.responses == 0
    assert result.overall_average is None
    assert all(c["average"] is None for c in result.competencies)


@pytest.mark.parametrize(
    ("typed", "ok"),
    [
        ("Anita Rao", True),
        ("  anita   RAO ", True),
        ("Ani Rao", False),
        ("", False),
    ],
)
def test_a_letter_is_accepted_only_with_the_employees_own_name(typed, ok):
    person = Employee(first_name="Anita", last_name="Rao")
    assert names_match(typed, person) is ok
