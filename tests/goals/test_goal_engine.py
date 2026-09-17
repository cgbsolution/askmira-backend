"""The goal engine: key result progress, goal progress, the goal score."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.goals import engine
from app.services.goals.engine import GoalError, GoalFacts, KeyResultFacts

D = Decimal


def kr(start="0", target="100", current="0", weight="1", kind="number", title="KR"):
    return KeyResultFacts(
        key_result_id=title,
        title=title,
        kind=kind,
        start=D(start),
        target=D(target),
        current=D(current),
        weight=D(weight),
    )


# ── Key results ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "target", "current", "expected"),
    [
        ("0", "50", "40", "80.00"),
        ("0", "50", "0", "0.00"),
        ("0", "50", "50", "100.00"),
        ("0", "50", "75", "100.00"),  # capped
        ("0", "50", "-5", "0.00"),  # floored
        ("8", "5", "6.5", "50.00"),  # a falling target
        ("8", "5", "9", "0.00"),
        ("10", "20", "13", "30.00"),
    ],
)
def test_key_result_progress(start, target, current, expected) -> None:
    assert engine.key_result_progress(
        kind="number", start=D(start), target=D(target), current=D(current)
    ) == D(expected)


def test_a_milestone_is_done_or_not() -> None:
    assert engine.key_result_progress(kind="milestone", start=D(0), target=D(1), current=D(0)) == D(
        0
    )
    assert engine.key_result_progress(kind="milestone", start=D(0), target=D(1), current=D(1)) == D(
        100
    )


def test_a_float_is_refused() -> None:
    with pytest.raises(GoalError):
        engine.key_result_progress(kind="number", start=D(0), target=D(1), current=0.5)


def test_a_measured_result_with_no_distance_is_refused() -> None:
    with pytest.raises(GoalError, match="milestone"):
        engine.validate_key_result(kind="number", start=D(5), target=D(5))
    engine.validate_key_result(kind="milestone", start=D(0), target=D(0))


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(GoalError):
        engine.validate_key_result(kind="vibes", start=D(0), target=D(1))


# ── Goal progress ───────────────────────────────────────────────────────────


def test_goal_progress_is_weighted_by_key_result() -> None:
    result = engine.goal_progress(
        [kr(current="100", weight="3", title="a"), kr(current="0", weight="1", title="b")]
    )
    assert result.progress == D("75.00")
    assert [c["progress"] for c in result.contributions] == ["100.00", "0.00"]


def test_a_goal_without_key_results_has_no_progress() -> None:
    result = engine.goal_progress([])
    assert result.progress is None
    assert "No key results" in result.note


# ── Goal score ──────────────────────────────────────────────────────────────


def test_goal_score_weights_goals_and_lists_every_contributor() -> None:
    score = engine.goal_score(
        [
            GoalFacts("g1", "Revenue", "active", D(60), [kr(current="50")]),
            GoalFacts("g2", "Quality", "completed", D(40), [kr(current="100")]),
        ]
    )
    assert score.score == D("70.00")
    assert score.measured_weight_percent == D("100.00")
    assert {c["goal_id"] for c in score.contributing} == {"g1", "g2"}
    assert score.contributing[0]["share_of_score"] == "60.00"


def test_a_cancelled_goal_is_excluded_not_zeroed() -> None:
    score = engine.goal_score(
        [
            GoalFacts("g1", "Kept", "active", D(50), [kr(current="80")]),
            GoalFacts("g2", "Dropped", "cancelled", D(50), [kr(current="0")]),
        ]
    )
    assert score.score == D("80.00")
    assert score.excluded[0]["goal_id"] == "g2"


def test_an_unmeasured_goal_is_excluded_and_coverage_says_so() -> None:
    score = engine.goal_score(
        [
            GoalFacts("g1", "Measured", "active", D(40), [kr(current="50")]),
            GoalFacts("g2", "Unmeasured", "active", D(60), []),
        ]
    )
    assert score.score == D("50.00")
    assert score.measured_weight_percent == D("40.00")
    assert any("scaled to 100" in n for n in score.notes)


def test_no_measured_goal_means_no_score() -> None:
    score = engine.goal_score([GoalFacts("g1", "Draft", "draft", D(100), [kr()])])
    assert score.score is None
    assert score.measured_weight_percent == D(0)


def test_unweighted_goals_count_equally() -> None:
    score = engine.goal_score(
        [
            GoalFacts("g1", "A", "active", D(0), [kr(current="100")]),
            GoalFacts("g2", "B", "active", D(0), [kr(current="0")]),
        ]
    )
    assert score.score == D("50.00")
    assert any("counted equally" in n for n in score.notes)


# ── Weight budget and alignment ─────────────────────────────────────────────


def test_weights_may_not_exceed_a_hundred() -> None:
    engine.validate_weight_budget(existing=D(60), proposed=D(40))
    with pytest.raises(GoalError, match="past 100"):
        engine.validate_weight_budget(existing=D(60), proposed=D("40.01"))
    with pytest.raises(GoalError):
        engine.validate_weight_budget(existing=D(0), proposed=D(-1))


def test_an_alignment_loop_is_refused() -> None:
    parents = {"b": "a", "a": None}
    engine.check_alignment(goal_id="c", parent_id="b", parents=parents)
    with pytest.raises(GoalError, match="itself"):
        engine.check_alignment(goal_id="a", parent_id="a", parents=parents)
    with pytest.raises(GoalError, match="loop"):
        engine.check_alignment(goal_id="a", parent_id="b", parents=parents)


def test_an_existing_loop_is_refused() -> None:
    with pytest.raises(GoalError, match="loop"):
        engine.check_alignment(goal_id="z", parent_id="x", parents={"x": "y", "y": "x"})
