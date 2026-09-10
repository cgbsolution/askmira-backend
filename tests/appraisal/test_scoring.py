"""Slice 16 gate: weighted scoring, exclusions, and band boundaries.

Two failure modes this file is mostly about, both of which quietly penalise
people:

- a non-applicable competency scored as zero, dragging down an individual
  contributor for "manages a team";
- a band boundary that belongs to neither side or to both, so a score of
  exactly 3.5 either has no rating or two.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.appraisal.scoring import (
    Band,
    CompetencyRating,
    ScoringError,
    WeightSplit,
    compute_score,
    map_to_band,
    validate_bands,
    validate_weights,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


SEVENTY_THIRTY = WeightSplit(goal_weight=D(70), competency_weight=D(30))
WITH_VALUES = WeightSplit(
    goal_weight=D(60), competency_weight=D(25), values_weight=D(15)
)


def competency(**overrides) -> CompetencyRating:
    base = {
        "code": "COLLAB",
        "name": "Collaboration",
        "weight": D(50),
        "rating": D(4),
    }
    base.update(overrides)
    return CompetencyRating(**base)


def score(**overrides):
    base = {
        "split": SEVENTY_THIRTY,
        "goal_score": D(80),
        "competency_ratings": [
            competency(),
            competency(code="DELIVERY", name="Delivery", rating=D(3)),
        ],
        "scale_max": D(5),
    }
    base.update(overrides)
    return compute_score(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Weight validation — at save
# ═══════════════════════════════════════════════════════════════════════════


def test_weights_summing_to_a_hundred_are_accepted() -> None:
    validate_weights(SEVENTY_THIRTY)
    validate_weights(WITH_VALUES)


def test_weights_not_summing_to_a_hundred_are_rejected() -> None:
    """The checklist gate: at save, not at computation.

    At computation time the cycle has launched and managers have started
    rating; the template that cannot be scored is discovered by the person
    trying to submit rather than the person who configured it.
    """
    with pytest.raises(ScoringError, match="sum to 90, not 100"):
        validate_weights(WeightSplit(goal_weight=D(70), competency_weight=D(20)))


def test_weights_summing_above_a_hundred_are_rejected() -> None:
    with pytest.raises(ScoringError, match="sum to 110"):
        validate_weights(WeightSplit(goal_weight=D(80), competency_weight=D(30)))


def test_a_negative_weight_is_rejected() -> None:
    with pytest.raises(ScoringError, match="cannot be negative"):
        validate_weights(WeightSplit(goal_weight=D(110), competency_weight=D(-10)))


def test_a_float_weight_is_rejected() -> None:
    with pytest.raises(ScoringError, match="must be a Decimal"):
        validate_weights(WeightSplit(goal_weight=70.0, competency_weight=D(30)))


def test_computing_a_score_validates_the_weights_too() -> None:
    """Belt and braces — a template saved before the check existed still
    cannot produce a wrong number."""
    with pytest.raises(ScoringError, match="sum to"):
        score(split=WeightSplit(goal_weight=D(70), competency_weight=D(20)))


# ═══════════════════════════════════════════════════════════════════════════
# The weighted score
# ═══════════════════════════════════════════════════════════════════════════


def test_goals_and_competencies_combine_in_the_configured_ratio() -> None:
    """Competencies: (4x50 + 3x50) / 100 = 3.5 of 5 = 70%.
    Total: 80x0.7 + 70x0.3 = 56 + 21 = 77."""
    result = score()

    assert result.competency_score == D(70)
    assert result.weighted_total == D(77)


def test_the_values_dimension_contributes_when_it_has_weight() -> None:
    result = score(split=WITH_VALUES, values_score=D(90))
    # 80x0.6 + 70x0.25 + 90x0.15 = 48 + 17.5 + 13.5 = 79
    assert result.weighted_total == D(79)
    assert result.values_score == D(90)


def test_a_values_score_with_no_values_weight_is_refused() -> None:
    """Silently dropping it would hide a configuration mistake."""
    with pytest.raises(ScoringError, match="gives values no weight"):
        score(values_score=D(90))


def test_missing_a_values_score_when_it_carries_weight_is_refused() -> None:
    with pytest.raises(ScoringError, match="no values score was supplied"):
        score(split=WITH_VALUES)


def test_the_scale_maximum_normalises_across_scales() -> None:
    """A 4 on a 5-point scale and a 4 on a 4-point scale are different
    performances, and the score has to reflect that."""
    five_point = score(scale_max=D(5), competency_ratings=[competency(rating=D(4))])
    four_point = score(scale_max=D(4), competency_ratings=[competency(rating=D(4))])

    assert five_point.competency_score == D(80)
    assert four_point.competency_score == D(100)


def test_competency_weights_are_respected() -> None:
    heavy_on_delivery = score(
        competency_ratings=[
            competency(weight=D(20), rating=D(5)),
            competency(code="DELIVERY", name="Delivery", weight=D(80), rating=D(2)),
        ]
    )
    # (5x20 + 2x80) / 100 = 2.6 of 5 = 52%
    assert heavy_on_delivery.competency_score == D(52)


def test_the_unrounded_total_is_kept_alongside_the_rounded_one() -> None:
    """So the rounding is visible rather than assumed."""
    result = score(goal_score=D("83.333"))
    assert result.unrounded_total != result.weighted_total
    assert "half-up to 2 decimal place(s)" in result.rounding


def test_the_precision_is_configurable() -> None:
    coarse = score(goal_score=D("83.333"), precision=0)
    fine = score(goal_score=D("83.333"), precision=4)
    assert coarse.weighted_total == coarse.weighted_total.quantize(D(1))
    assert fine.weighted_total.as_tuple().exponent == -4


def test_the_breakdown_is_json_serialisable() -> None:
    import json

    json.dumps(score().as_dict())


# ═══════════════════════════════════════════════════════════════════════════
# Non-applicable competencies — the gate
# ═══════════════════════════════════════════════════════════════════════════


def test_a_non_applicable_competency_is_excluded_not_zero_scored() -> None:
    """The checklist gate.

    "Manages a team" does not apply to an individual contributor. Scoring it
    zero drags their score down for a thing they were never expected to do, and
    it is invisible because the competency is on the template.
    """
    excluded = score(
        competency_ratings=[
            competency(rating=D(4)),
            competency(
                code="LEADS", name="Manages a team", rating=None,
                is_applicable=False,
                not_applicable_reason="Individual contributor with no reports.",
            ),
        ]
    )
    assert excluded.competency_score == D(80), "4 of 5, on the one that applies"

    zero_scored = score(
        competency_ratings=[
            competency(rating=D(4)),
            competency(code="LEADS", name="Manages a team", rating=D(0)),
        ]
    )
    assert zero_scored.competency_score == D(40), "which is the wrong answer"
    assert excluded.weighted_total > zero_scored.weighted_total


def test_the_exclusion_is_reported_with_its_weight_and_reason() -> None:
    result = score(
        competency_ratings=[
            competency(rating=D(4)),
            competency(
                code="LEADS", name="Manages a team", weight=D(50), rating=None,
                is_applicable=False,
                not_applicable_reason="Individual contributor with no reports.",
            ),
        ]
    )
    assert len(result.excluded_competencies) == 1
    assert result.excluded_competencies[0]["code"] == "LEADS"
    assert result.excluded_competencies[0]["weight"] == "50"
    assert "Individual contributor" in result.excluded_competencies[0]["reason"]
    assert any("renormalised" in note for note in result.notes)


def test_remaining_weights_renormalise_rather_than_shrinking_the_score() -> None:
    """One competency of three, at full marks, scores 100 — not 33."""
    result = score(
        competency_ratings=[
            competency(weight=D(34), rating=D(5)),
            competency(code="A", name="A", weight=D(33), is_applicable=False),
            competency(code="B", name="B", weight=D(33), is_applicable=False),
        ]
    )
    assert result.competency_score == D(100)


def test_every_competency_being_inapplicable_is_refused() -> None:
    """Either the template is wrong for this role, or the competency weight
    should be zero — both are decisions, not defaults."""
    with pytest.raises(ScoringError, match="nothing to score"):
        score(
            competency_ratings=[
                competency(is_applicable=False),
                competency(code="B", name="B", is_applicable=False),
            ]
        )


def test_an_applicable_but_unrated_competency_is_refused() -> None:
    """Not the same as one that does not apply. Scoring it zero would penalise
    the employee for the rater's omission."""
    with pytest.raises(ScoringError, match="applicable but unrated"):
        score(
            competency_ratings=[
                competency(rating=D(4)),
                competency(code="DELIVERY", name="Delivery", rating=None),
            ]
        )


def test_applicable_competencies_carrying_no_weight_are_refused() -> None:
    with pytest.raises(ScoringError, match="no weight between them"):
        score(competency_ratings=[competency(weight=D(0), rating=D(4))])


def test_competencies_rated_on_a_zero_weight_template_do_not_affect_the_score() -> None:
    result = score(
        split=WeightSplit(goal_weight=D(100), competency_weight=D(0)),
        competency_ratings=[competency(rating=D(1))],
    )
    assert result.weighted_total == D(80), "the goal score alone"
    assert any("carry no weight" in note for note in result.notes)


# ═══════════════════════════════════════════════════════════════════════════
# Band boundaries — the gate
# ═══════════════════════════════════════════════════════════════════════════

BANDS = [
    Band("Below expectations", D(0), D(50)),
    Band("Partially meets", D(50), D(65)),
    Band("Meets expectations", D(65), D(80)),
    Band("Exceeds expectations", D(80), D(92)),
    Band("Outstanding", D(92), D(100)),
]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", "Below expectations"),
        ("49.99", "Below expectations"),
        ("50", "Partially meets"),
        ("64.99", "Partially meets"),
        ("65", "Meets expectations"),
        ("79.99", "Meets expectations"),
        ("80", "Exceeds expectations"),
        ("91.99", "Exceeds expectations"),
        ("92", "Outstanding"),
        ("100", "Outstanding"),
    ],
)
def test_the_band_mapping_is_correct_on_both_sides_of_every_boundary(
    value: str, expected: str
) -> None:
    """The checklist gate.

    Half-open bands: the lower bound belongs to the band, the upper bound to
    the next one. A score of exactly 80 is "Exceeds", not "Meets".
    """
    assert map_to_band(D(value), BANDS).label == expected


def test_a_perfect_score_lands_in_the_top_band() -> None:
    """The top band is closed at its upper bound, so 100 has somewhere to go."""
    assert map_to_band(D(100), BANDS).label == "Outstanding"


def test_band_order_in_the_list_does_not_matter() -> None:
    shuffled = [BANDS[3], BANDS[0], BANDS[4], BANDS[1], BANDS[2]]
    assert map_to_band(D(70), shuffled).label == "Meets expectations"


def test_a_score_outside_every_band_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ScoringError, match="falls outside every band"):
        map_to_band(D(150), BANDS)


def test_no_bands_at_all_is_refused() -> None:
    with pytest.raises(ScoringError, match="No score bands"):
        map_to_band(D(70), [])


def test_an_inverted_band_is_refused() -> None:
    with pytest.raises(ScoringError, match="not a range"):
        map_to_band(D(70), [Band("Backwards", D(80), D(20))])


# ═══════════════════════════════════════════════════════════════════════════
# Band configuration
# ═══════════════════════════════════════════════════════════════════════════


def test_contiguous_bands_validate() -> None:
    validate_bands(BANDS)


def test_overlapping_bands_are_refused() -> None:
    """Otherwise the rating depends on which band happened to be checked
    first."""
    overlapping = [Band("A", D(0), D(60)), Band("B", D(50), D(100))]
    with pytest.raises(ScoringError, match="overlap between"):
        validate_bands(overlapping)


def test_a_gap_between_bands_is_refused() -> None:
    """A score in the gap would have no rating."""
    gapped = [Band("A", D(0), D(50)), Band("B", D(60), D(100))]
    with pytest.raises(ScoringError, match="gap between"):
        validate_bands(gapped)


def test_no_bands_is_refused_at_validation_too() -> None:
    with pytest.raises(ScoringError, match="At least one band"):
        validate_bands([])
