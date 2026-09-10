"""Anchoring protection, the mandatory-feedback gate, and keeping disagreement."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.interviews.feedback import (
    FeedbackError,
    PanelFeedback,
    Recommendation,
    consolidate,
    missing_feedback,
    validate_feedback,
    visible_feedback_for,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _entry(
    panelist_id: str,
    recommendation: Recommendation = Recommendation.YES,
    *,
    submitted: bool = True,
    ratings: dict[str, str] | None = None,
    concerns: str = "",
) -> PanelFeedback:
    return PanelFeedback(
        panelist_id=panelist_id,
        interview_id="i1",
        recommendation=recommendation,
        ratings={k: Decimal(v) for k, v in (ratings or {}).items()},
        concerns=concerns,
        submitted_at=NOW if submitted else None,
        panelist_label=panelist_id.title(),
    )


ROSTER = [("alice", "Alice", True), ("bob", "Bob", True), ("carol", "Carol", True)]


# ═══════════════════════════════════════════════════════════════════════════
# Anchoring
# ═══════════════════════════════════════════════════════════════════════════


def test_a_panelist_who_has_not_submitted_sees_nobody_elses_feedback() -> None:
    """The checklist gate.

    Reading "STRONG_YES, brilliant" before writing your own review measurably
    changes what you write, and four opinions that anchored on the first are one
    opinion.
    """
    entries = [
        _entry("alice", Recommendation.STRONG_YES),
        _entry("bob", submitted=False),
    ]
    visible = visible_feedback_for(viewer_panelist_id="bob", all_feedback=entries)
    assert [f.panelist_id for f in visible] == ["bob"]


def test_a_panelist_who_has_submitted_sees_the_rest() -> None:
    entries = [_entry("alice", Recommendation.STRONG_YES), _entry("bob")]
    visible = visible_feedback_for(viewer_panelist_id="bob", all_feedback=entries)
    assert {f.panelist_id for f in visible} == {"alice", "bob"}


def test_a_submitted_panelist_still_does_not_see_unsubmitted_drafts() -> None:
    entries = [_entry("alice"), _entry("bob", submitted=False)]
    visible = visible_feedback_for(viewer_panelist_id="alice", all_feedback=entries)
    assert [f.panelist_id for f in visible] == ["alice"]


def test_a_hiring_manager_on_the_panel_is_not_exempt() -> None:
    """Their review anchors just as easily, and it anchors everyone else's too."""
    entries = [
        _entry("alice", Recommendation.STRONG_YES),
        _entry("manager", submitted=False),
    ]
    visible = visible_feedback_for(
        viewer_panelist_id="manager", all_feedback=entries, viewer_is_hiring_manager=True
    )
    assert [f.panelist_id for f in visible] == ["manager"]


def test_a_hiring_manager_not_on_the_panel_sees_what_has_been_submitted() -> None:
    """They have nothing to anchor: they are not writing a review."""
    entries = [_entry("alice"), _entry("bob")]
    visible = visible_feedback_for(
        viewer_panelist_id="outsider", all_feedback=entries, viewer_is_hiring_manager=True
    )
    assert {f.panelist_id for f in visible} == {"alice", "bob"}


def test_a_tenant_can_turn_anchoring_protection_off() -> None:
    """Some clients run deliberately collaborative panels. The default is on,
    because the default is what most tenants will run."""
    entries = [_entry("alice"), _entry("bob", submitted=False)]
    visible = visible_feedback_for(
        viewer_panelist_id="bob", all_feedback=entries, hide_until_submitted=False
    )
    assert len(visible) == 2


# ═══════════════════════════════════════════════════════════════════════════
# The mandatory gate
# ═══════════════════════════════════════════════════════════════════════════


def test_outstanding_feedback_is_named_not_counted() -> None:
    """"2 outstanding" makes a recruiter open four records to find out who to
    chase."""
    entries = [_entry("alice"), _entry("bob", submitted=False)]
    missing = missing_feedback(panelists=ROSTER, feedback=entries)
    assert missing == ["Bob", "Carol"]


def test_an_observer_does_not_owe_feedback() -> None:
    """An observer required to file a review would either stop observing or
    start filing empty ones."""
    roster = [("alice", "Alice", True), ("observer", "Observer", False)]
    missing = missing_feedback(panelists=roster, feedback=[_entry("alice")])
    assert missing == []


def test_nothing_is_outstanding_once_everybody_has_submitted() -> None:
    entries = [_entry("alice"), _entry("bob"), _entry("carol")]
    assert missing_feedback(panelists=ROSTER, feedback=entries) == []


# ═══════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════


def test_a_partial_form_is_refused() -> None:
    """Structured feedback is what makes two interviewers comparable; a partial
    form is two essays again."""
    with pytest.raises(FeedbackError, match="not rated"):
        validate_feedback(
            recommendation=Recommendation.YES,
            ratings={"comm": Decimal(8)},
            required_competencies=["comm", "problem_solving"],
            concerns="",
        )


def test_a_negative_recommendation_needs_a_stated_concern() -> None:
    """"No" with nothing behind it cannot be explained to the candidate, cannot
    be examined for bias, and cannot be defended if questioned."""
    with pytest.raises(FeedbackError, match="cannot be explained"):
        validate_feedback(
            recommendation=Recommendation.NO,
            ratings={"comm": Decimal(4)},
            required_competencies=["comm"],
            concerns="   ",
        )


def test_a_positive_recommendation_does_not_require_prose() -> None:
    validate_feedback(
        recommendation=Recommendation.YES,
        ratings={"comm": Decimal(8)},
        required_competencies=["comm"],
        concerns="",
    )


def test_a_negative_recommendation_with_a_concern_is_accepted() -> None:
    validate_feedback(
        recommendation=Recommendation.STRONG_NO,
        ratings={"comm": Decimal(3)},
        required_competencies=["comm"],
        concerns="Could not explain their own design decisions.",
    )


# ═══════════════════════════════════════════════════════════════════════════
# Consolidation — disagreement is kept, not averaged
# ═══════════════════════════════════════════════════════════════════════════


def test_a_split_panel_is_reported_as_split() -> None:
    entries = [
        _entry("alice", Recommendation.YES),
        _entry("bob", Recommendation.NO, concerns="Weak on system design."),
        _entry("carol", Recommendation.YES),
    ]
    result = consolidate(feedback=entries, panelists=ROSTER)
    assert not result.is_unanimous
    assert "2 for, 1 against" in result.headline


def test_strong_dissent_is_named_and_never_averaged_away() -> None:
    """STRONG_NO and STRONG_YES do not cancel out to neutral. They mean the
    panel disagrees, which is the most informative thing that could have
    happened.
    """
    entries = [
        _entry("alice", Recommendation.STRONG_YES),
        _entry("bob", Recommendation.STRONG_NO, concerns="Fabricated a project."),
        _entry("carol", Recommendation.YES),
    ]
    result = consolidate(feedback=entries, panelists=ROSTER)
    assert result.has_strong_dissent
    assert "Alice" in result.headline
    assert "Bob" in result.headline
    assert "worth a conversation" in result.headline
    assert "neutral" not in result.headline.lower()


def test_a_unanimous_panel_says_so() -> None:
    entries = [_entry(pid) for pid in ("alice", "bob", "carol")]
    result = consolidate(feedback=entries, panelists=ROSTER)
    assert result.is_unanimous
    assert "All 3 panelists recommend proceeding" in result.headline


def test_a_unanimous_rejection_says_so_too() -> None:
    entries = [
        _entry(pid, Recommendation.NO, concerns="Not enough depth.")
        for pid in ("alice", "bob", "carol")
    ]
    result = consolidate(feedback=entries, panelists=ROSTER)
    assert result.is_unanimous
    assert "not proceeding" in result.headline


def test_the_consolidation_names_who_is_still_outstanding() -> None:
    entries = [_entry("alice"), _entry("bob", submitted=False)]
    result = consolidate(feedback=entries, panelists=ROSTER)
    assert not result.is_complete
    assert "Bob" in result.headline and "Carol" in result.headline


def test_no_feedback_at_all_says_nothing_rather_than_looking_neutral() -> None:
    result = consolidate(feedback=[_entry("alice", submitted=False)], panelists=ROSTER)
    assert result.submitted == 0
    assert "Nothing here says anything" in result.headline


def test_a_competency_reports_its_spread_rather_than_a_mean() -> None:
    """Two interviewers scoring 2 and 8 is not a 5. It is a disagreement about
    the same person."""
    entries = [
        _entry("alice", ratings={"system_design": "8"}),
        _entry("bob", Recommendation.NO, ratings={"system_design": "2"},
               concerns="Struggled with scale."),
    ]
    result = consolidate(feedback=entries, panelists=ROSTER)
    row = result.per_competency["system_design"]
    assert row["lowest"] == "2"
    assert row["highest"] == "8"
    assert row["spread"] == "6"
    assert row["raters"] == 2
    assert "mean" not in row and "average" not in row


def test_unsubmitted_feedback_never_reaches_the_consolidation() -> None:
    entries = [
        _entry("alice", Recommendation.STRONG_YES),
        _entry("bob", Recommendation.STRONG_NO, submitted=False),
    ]
    result = consolidate(feedback=entries, panelists=ROSTER)
    assert result.submitted == 1
    assert not result.has_strong_dissent


def test_the_recommendation_scale_has_no_neutral_middle() -> None:
    """A five-point scale with a neutral centre collects neutrals. Somebody who
    genuinely cannot decide should say so where a person will read it."""
    assert len(list(Recommendation)) == 4
    assert sum(1 for r in Recommendation if r.is_positive) == 2
