"""Slice 17 gate: distribution, enforcement modes, and small-group exclusion.

The small-group case is the one that matters most. A manager with three reports
cannot hit a 10% target — ten percent of three is 0.3 people — and enforcing it
means rating someone by arithmetic rather than performance.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.appraisal.calibration import (
    CalibrationError,
    EnforcementMode,
    Target,
    analyse_distribution,
    detect_outliers,
    enforcement_blockers,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


# Outstanding 10% ±2, Exceeds 20% ±5, Meets 55% ±10, Below 15% ±5.
TARGETS = [
    Target("Outstanding", D(10), D(2)),
    Target("Exceeds", D(20), D(5)),
    Target("Meets", D(55), D(10)),
    Target("Below", D(15), D(5)),
]


def ratings(outstanding=0, exceeds=0, meets=0, below=0) -> list[str]:
    return (
        ["Outstanding"] * outstanding
        + ["Exceeds"] * exceeds
        + ["Meets"] * meets
        + ["Below"] * below
    )


def analyse(**overrides):
    base = {
        "scope": "department",
        "scope_value": "Engineering",
        "ratings": ratings(outstanding=10, exceeds=20, meets=55, below=15),
        "targets": TARGETS,
        "min_group_size": 8,
    }
    base.update(overrides)
    return analyse_distribution(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Distribution against target
# ═══════════════════════════════════════════════════════════════════════════


def test_a_perfectly_distributed_group_is_within_tolerance() -> None:
    result = analyse()
    assert result.population == 100
    assert result.is_within_tolerance is True
    assert result.breaches == []


def test_the_actual_percentages_are_computed_per_rating() -> None:
    result = analyse()
    by_rating = {c.rating: c for c in result.counts}
    assert by_rating["Outstanding"].actual_percent == D(10)
    assert by_rating["Meets"].actual_percent == D(55)
    assert by_rating["Outstanding"].gap_percent == D(0)


def test_a_group_inside_tolerance_but_off_target_still_passes() -> None:
    """The tolerance is the point — 12% Outstanding against a 10% ±2 target."""
    result = analyse(ratings=ratings(outstanding=12, exceeds=20, meets=53, below=15))
    outstanding = next(c for c in result.counts if c.rating == "Outstanding")

    assert outstanding.actual_percent == D(12)
    assert outstanding.gap_percent == D(2)
    assert outstanding.is_within_tolerance is True
    assert result.is_within_tolerance is True


def test_a_group_outside_tolerance_is_flagged_with_the_gap() -> None:
    # Exceeds, Meets and Below all sit exactly on a tolerance bound here, so
    # Outstanding is the only breach and the assertion is unambiguous.
    result = analyse(ratings=ratings(outstanding=25, exceeds=15, meets=45, below=15))
    outstanding = next(c for c in result.counts if c.rating == "Outstanding")

    assert outstanding.is_within_tolerance is False
    assert outstanding.gap_percent == D(15)
    assert result.is_within_tolerance is False
    assert [c.rating for c in result.breaches] == ["Outstanding"]


def test_the_tolerance_boundary_is_inclusive_on_both_sides() -> None:
    """Exactly 12% against 10% ±2 is in; 13% is out."""
    at_bound = analyse(ratings=ratings(outstanding=12, exceeds=20, meets=53, below=15))
    past_bound = analyse(ratings=ratings(outstanding=13, exceeds=20, meets=52, below=15))

    assert at_bound.is_within_tolerance is True
    assert past_bound.is_within_tolerance is False


def test_a_rating_nobody_was_given_still_appears_as_a_gap() -> None:
    """Absent from the table would hide the very thing calibration looks for."""
    result = analyse(ratings=ratings(exceeds=20, meets=65, below=15))
    outstanding = next(c for c in result.counts if c.rating == "Outstanding")

    assert outstanding.count == 0
    assert outstanding.actual_percent == D(0)
    assert outstanding.is_within_tolerance is False


def test_a_rating_with_no_target_is_counted_but_not_measured() -> None:
    result = analyse(
        ratings=[*ratings(outstanding=10, exceeds=20, meets=55, below=10), *["Unrated"] * 5]
    )
    unrated = next(c for c in result.counts if c.rating == "Unrated")

    assert unrated.count == 5
    assert unrated.target_percent is None
    assert unrated.is_within_tolerance is True
    assert any("no distribution target" in note for note in result.notes)


def test_the_result_is_json_serialisable() -> None:
    import json

    json.dumps(analyse().as_dict())


# ═══════════════════════════════════════════════════════════════════════════
# Small groups — the gate
# ═══════════════════════════════════════════════════════════════════════════


def test_a_group_below_the_minimum_is_excluded_from_enforcement() -> None:
    """The checklist gate.

    A manager with three reports cannot express a 10% target. Forcing it means
    rating someone by arithmetic rather than performance.
    """
    result = analyse(
        scope="manager", scope_value="Anita Sharma",
        ratings=ratings(exceeds=1, meets=2), min_group_size=8,
    )

    assert result.population == 3
    assert result.is_excluded_as_small is True
    assert result.breaches == []
    assert result.is_within_tolerance is True, "excluded, so it does not block"


def test_the_exclusion_explains_itself_in_the_group_s_own_numbers() -> None:
    """Silently passing would be as unhelpful as silently failing."""
    result = analyse(
        scope="manager", scope_value="Anita Sharma",
        ratings=ratings(exceeds=1, meets=2), min_group_size=8,
    )
    assert "3 employee(s) in scope" in result.exclusion_reason
    assert "One person here is 33.33%" in result.exclusion_reason
    assert "rating someone by arithmetic" in result.exclusion_reason


def test_a_group_exactly_at_the_minimum_is_assessed() -> None:
    """The boundary. `min_group_size` is a floor, not an exclusive bound."""
    result = analyse(
        ratings=ratings(outstanding=4, exceeds=4), min_group_size=8
    )
    assert result.is_excluded_as_small is False
    assert result.is_within_tolerance is False, "50% Outstanding is well out"


def test_an_empty_scope_is_excluded_rather_than_dividing_by_zero() -> None:
    result = analyse(ratings=[])
    assert result.population == 0
    assert result.is_excluded_as_small is True
    assert "Nobody in this scope has a rating yet" in result.exclusion_reason


def test_a_minimum_group_size_below_one_is_refused() -> None:
    with pytest.raises(CalibrationError, match="at least one"):
        analyse(min_group_size=0)


# ═══════════════════════════════════════════════════════════════════════════
# Enforcement modes — all three
# ═══════════════════════════════════════════════════════════════════════════


def out_of_tolerance():
    """One breach only: 25% Outstanding against an 8-12% band."""
    return analyse(ratings=ratings(outstanding=25, exceeds=15, meets=45, below=15))


def test_none_mode_never_blocks() -> None:
    assert enforcement_blockers(
        mode=EnforcementMode.NONE, distributions=[out_of_tolerance()]
    ) == []


def test_advisory_mode_shows_the_gap_but_permits_closure() -> None:
    """The checklist gate. The gap is still computed and visible — advisory
    means it does not stop you, not that it is hidden."""
    distribution = out_of_tolerance()
    assert distribution.is_within_tolerance is False, "the gap is still visible"
    assert enforcement_blockers(
        mode=EnforcementMode.ADVISORY, distributions=[distribution]
    ) == []


def test_enforced_mode_blocks_and_names_the_scope_and_the_overshoot() -> None:
    """The checklist gate.

    A facilitator needs to know what to change, not just that something is
    wrong.
    """
    blockers = enforcement_blockers(
        mode=EnforcementMode.ENFORCED, distributions=[out_of_tolerance()]
    )
    assert len(blockers) == 1

    blocker = blockers[0]
    assert blocker["scope_value"] == "Engineering"
    assert blocker["rating"] == "Outstanding"
    assert blocker["count"] == 25
    assert "13.00 points above" in blocker["message"]
    assert "8 to 12%" in blocker["message"]


def test_enforced_mode_permits_a_compliant_distribution() -> None:
    assert enforcement_blockers(
        mode=EnforcementMode.ENFORCED, distributions=[analyse()]
    ) == []


def test_enforced_mode_does_not_block_on_an_excluded_small_group() -> None:
    """The exclusion has to hold under enforcement, or it achieves nothing."""
    small = analyse(
        scope="manager", scope_value="Anita Sharma",
        ratings=ratings(outstanding=1, meets=2), min_group_size=8,
    )
    assert enforcement_blockers(
        mode=EnforcementMode.ENFORCED, distributions=[small]
    ) == []


def test_enforced_mode_reports_every_breaching_scope() -> None:
    blockers = enforcement_blockers(
        mode=EnforcementMode.ENFORCED,
        distributions=[
            out_of_tolerance(),
            analyse(
                scope_value="Sales",
                ratings=ratings(outstanding=10, exceeds=20, meets=30, below=40),
            ),
        ],
    )
    scopes = {b["scope_value"] for b in blockers}
    assert scopes == {"Engineering", "Sales"}


def test_an_unknown_enforcement_mode_is_refused_with_the_options() -> None:
    with pytest.raises(CalibrationError, match="advisory"):
        enforcement_blockers(mode="strict", distributions=[])


def test_the_module_does_not_editorialise_about_forced_distribution() -> None:
    """Clients decided this before they bought the software, and they disagree
    strongly with each other. A product that argues with its users about their
    own HR policy in an error message is not helping."""
    blockers = enforcement_blockers(
        mode=EnforcementMode.ENFORCED, distributions=[out_of_tolerance()]
    )
    text = " ".join(b["message"] for b in blockers).lower()
    for opinion in ("should not", "bad practice", "we recommend", "unfair", "harmful"):
        assert opinion not in text


# ═══════════════════════════════════════════════════════════════════════════
# Outlier managers
# ═══════════════════════════════════════════════════════════════════════════


def manager(manager_id: str, name: str, scores: list[int]) -> dict:
    return {
        "manager_id": manager_id,
        "manager_name": name,
        "scores": [D(s) for s in scores],
    }


def test_a_deliberately_lenient_manager_is_identified() -> None:
    """The checklist gate, against a fixture built to be lenient."""
    profiles = detect_outliers(
        managers=[
            manager("m1", "Lenient Lakshmi", [92, 95, 90, 94, 93]),
            manager("m2", "Ordinary Om", [70, 75, 68, 72, 74]),
            manager("m3", "Ordinary Uma", [72, 69, 75, 71, 70]),
        ],
        threshold=D(10),
    )
    lenient = next(p for p in profiles if p.manager_id == "m1")

    assert lenient.classification == "lenient"
    assert lenient.deviation > D(10)
    assert "may be leniency or it may be a genuinely strong team" in lenient.reason


def test_a_severe_manager_is_identified() -> None:
    profiles = detect_outliers(
        managers=[
            manager("m1", "Severe Sanjay", [45, 50, 48, 44, 46]),
            manager("m2", "Ordinary Om", [70, 75, 68, 72, 74]),
            manager("m3", "Ordinary Uma", [72, 69, 75, 71, 70]),
        ],
        threshold=D(10),
    )
    severe = next(p for p in profiles if p.manager_id == "m1")
    assert severe.classification == "severe"
    assert severe.deviation < D(-10)


def test_typical_managers_are_not_flagged() -> None:
    profiles = detect_outliers(
        managers=[
            manager("m1", "A", [70, 72, 68, 74]),
            manager("m2", "B", [71, 69, 73, 70]),
        ],
        threshold=D(10),
    )
    assert {p.classification for p in profiles} == {"typical"}


def test_a_manager_with_too_few_reports_is_not_assessed() -> None:
    """A mean over two people says nothing about the person who set it."""
    profiles = detect_outliers(
        managers=[
            manager("m1", "Two reports", [100, 100]),
            manager("m2", "Ordinary", [70, 72, 68, 74]),
            manager("m3", "Ordinary", [71, 69, 73, 70]),
        ],
        threshold=D(10),
        min_group_size=4,
    )
    tiny = next(p for p in profiles if p.manager_id == "m1")

    assert tiny.classification == "not_assessed"
    assert tiny.deviation == D(0)
    assert "below the 4 needed" in tiny.reason


def test_a_small_manager_does_not_drag_the_peer_baseline() -> None:
    """The peer mean is over assessable managers only, so one two-report
    manager rating everyone 100 does not make everyone else look severe."""
    profiles = detect_outliers(
        managers=[
            manager("extreme", "Two reports at 100", [100, 100]),
            manager("m2", "Ordinary", [70, 72, 68, 74]),
            manager("m3", "Ordinary", [71, 69, 73, 70]),
        ],
        threshold=D(10),
        min_group_size=4,
    )
    ordinary = [p for p in profiles if p.manager_id in {"m2", "m3"}]
    assert all(p.classification == "typical" for p in ordinary)


def test_when_nobody_is_assessable_everyone_says_so() -> None:
    profiles = detect_outliers(
        managers=[manager("m1", "A", [90]), manager("m2", "B", [50])],
        min_group_size=4,
    )
    assert {p.classification for p in profiles} == {"not_assessed"}


def test_no_managers_returns_nothing() -> None:
    assert detect_outliers(managers=[]) == []


def test_a_float_score_is_refused() -> None:
    with pytest.raises(CalibrationError, match="must be Decimals"):
        detect_outliers(
            managers=[{"manager_id": "m1", "manager_name": "A", "scores": [70.0] * 4}]
        )


def test_a_zero_threshold_is_refused() -> None:
    with pytest.raises(CalibrationError, match="greater than zero"):
        detect_outliers(managers=[manager("m1", "A", [70] * 4)], threshold=D(0))


def test_the_profile_is_json_serialisable() -> None:
    import json

    profiles = detect_outliers(managers=[manager("m1", "A", [70, 72, 68, 74])])
    json.dumps([p.as_dict() for p in profiles])


def test_outlier_detection_is_a_prompt_not_a_verdict() -> None:
    """The data cannot tell leniency from a genuinely strong team, and the
    language should not pretend otherwise."""
    profiles = detect_outliers(
        managers=[
            manager("m1", "Lenient", [95, 92, 94, 93]),
            manager("m2", "A", [70, 72, 68, 74]),
            manager("m3", "B", [71, 69, 73, 70]),
        ],
        threshold=D(10),
    )
    lenient = next(p for p in profiles if p.classification == "lenient")
    assert "Worth asking about" in lenient.reason
    assert "cannot tell those apart" in lenient.reason
