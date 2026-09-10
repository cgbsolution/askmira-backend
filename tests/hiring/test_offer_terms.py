"""The three things an approver cannot see for themselves."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.hiring.offer_terms import (
    MATERIAL_TERMS,
    OfferTermsError,
    budget_deviation,
    canonical_terms,
    internal_parity,
    material_changes,
    terms_fingerprint,
)


def _terms(**overrides) -> dict:
    base = {
        "ctc_annual": Decimal("1800000"),
        "salary_structure_id": "3f2b1a00-0000-0000-0000-000000000001",
        "grade": "L5",
        "designation": "Senior Engineer",
        "joining_date": date(2026, 11, 3),
        "joining_location_id": "3f2b1a00-0000-0000-0000-000000000002",
        "probation_months": 6,
        "notice_period_days": 60,
        "offer_components": [],
        # Deliberately not material.
        "recruiter_note": "chased twice",
        "candidate_phone": "+91 90000 00000",
    }
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# Material terms and the fingerprint
# ═══════════════════════════════════════════════════════════════════════════


def test_the_fingerprint_ignores_everything_that_is_not_a_material_term() -> None:
    """A corrected phone number must not void a CFO's approval.

    If it did, everyone would learn to treat re-approval as noise — which is
    exactly how a real CTC change gets waved through the next time.
    """
    before = terms_fingerprint(_terms())
    after = terms_fingerprint(
        _terms(recruiter_note="chased four times", candidate_phone="+91 90000 11111")
    )
    assert before == after


def test_a_ctc_change_changes_the_fingerprint() -> None:
    assert terms_fingerprint(_terms()) != terms_fingerprint(
        _terms(ctc_annual=Decimal("2000000"))
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("ctc_annual", Decimal("2000000")),
        ("grade", "L6"),
        ("designation", "Staff Engineer"),
        ("joining_date", date(2026, 12, 1)),
        ("probation_months", 3),
        ("notice_period_days", 90),
        ("salary_structure_id", "3f2b1a00-0000-0000-0000-0000000000ff"),
    ],
)
def test_every_material_term_moves_the_fingerprint(field, value) -> None:
    assert terms_fingerprint(_terms()) != terms_fingerprint(_terms(**{field: value}))


def test_a_joining_bonus_added_after_approval_is_a_material_change() -> None:
    """The cheapest way to break an approval chain that otherwise works."""
    before = _terms()
    after = _terms(
        offer_components=[
            {"kind": "joining_bonus", "amount": Decimal("200000"), "counts_toward_ctc": False}
        ]
    )
    assert terms_fingerprint(before) != terms_fingerprint(after)
    assert material_changes(before, after) == ["offer_components"]


def test_the_same_money_written_differently_fingerprints_the_same() -> None:
    """`1800000` and `1800000.00` are one number.

    They arrive as the first from a request body and the second from a
    Numeric(14,2) column. Treating them as different would make an offer appear
    to have been edited by being saved.
    """
    assert terms_fingerprint(_terms(ctc_annual=Decimal("1800000"))) == terms_fingerprint(
        _terms(ctc_annual=Decimal("1800000.00"))
    )


def test_material_changes_names_the_terms_rather_than_counting_them() -> None:
    changed = material_changes(
        _terms(), _terms(ctc_annual=Decimal("2000000"), joining_date=date(2026, 12, 1))
    )
    assert changed == ["ctc_annual", "joining_date"]


def test_canonical_terms_drops_the_non_material_keys() -> None:
    canonical = canonical_terms(_terms())
    assert "recruiter_note" not in canonical
    assert set(canonical) <= MATERIAL_TERMS


def test_a_missing_term_is_not_the_same_as_a_present_one() -> None:
    """An offer payload that simply omits the CTC must not fingerprint as an
    offer that has one."""
    with_ctc = _terms()
    without = {k: v for k, v in with_ctc.items() if k != "ctc_annual"}
    assert terms_fingerprint(with_ctc) != terms_fingerprint(without)


# ═══════════════════════════════════════════════════════════════════════════
# Budget deviation
# ═══════════════════════════════════════════════════════════════════════════


def test_an_offer_over_the_band_produces_a_headline() -> None:
    deviation = budget_deviation(
        offered_ctc=Decimal("2200000"),
        band_min=Decimal("1500000"),
        band_max=Decimal("2000000"),
    )
    assert deviation.is_over_band
    assert deviation.requires_prominent_flag
    assert deviation.amount_over == Decimal("200000.00")
    assert deviation.percent_over == Decimal("10.00")
    assert "above the approved band" in deviation.headline


def test_an_offer_inside_the_band_flags_nothing() -> None:
    deviation = budget_deviation(
        offered_ctc=Decimal("1800000"),
        band_min=Decimal("1500000"),
        band_max=Decimal("2000000"),
    )
    assert not deviation.is_over_band
    assert not deviation.requires_prominent_flag
    assert deviation.headline == ""


def test_an_offer_exactly_at_the_ceiling_is_inside_the_band() -> None:
    """The band is inclusive of its own ceiling.

    A requisition budgeted "up to 20 lakh" means 20 lakh is allowed. Flagging
    it would make every approver at the top of their band think they had broken
    something.
    """
    deviation = budget_deviation(
        offered_ctc=Decimal("2000000"),
        band_min=Decimal("1500000"),
        band_max=Decimal("2000000"),
    )
    assert not deviation.is_over_band


def test_an_offer_below_the_floor_is_reported_but_not_flagged_as_a_breach() -> None:
    deviation = budget_deviation(
        offered_ctc=Decimal("1200000"),
        band_min=Decimal("1500000"),
        band_max=Decimal("2000000"),
    )
    assert deviation.is_under_band
    assert not deviation.is_over_band
    assert deviation.amount_over == Decimal("0.00")
    assert "below the requisition's" in deviation.headline


def test_no_ceiling_means_nothing_can_be_over_band() -> None:
    deviation = budget_deviation(
        offered_ctc=Decimal("9000000"), band_min=None, band_max=None
    )
    assert not deviation.is_over_band
    assert deviation.headline == ""


def test_a_float_ctc_is_refused() -> None:
    with pytest.raises(OfferTermsError, match="Decimal"):
        budget_deviation(offered_ctc=1800000.0, band_min=None, band_max=None)


# ═══════════════════════════════════════════════════════════════════════════
# Internal parity — the expensive one
# ═══════════════════════════════════════════════════════════════════════════


def test_an_offer_above_every_comparable_says_so_plainly() -> None:
    """The most expensive and most common offer mistake in the market."""
    parity = internal_parity(
        offered_ctc=Decimal("2400000"),
        comparable_ctcs=[
            Decimal("1800000"),
            Decimal("1900000"),
            Decimal("2000000"),
            Decimal("2100000"),
        ],
        grade="L5",
    )
    assert parity.above_every_comparable
    assert parity.maximum == Decimal("2100000.00")
    assert "above every one of the 4 employees at grade L5" in parity.headline


def test_the_median_of_an_even_number_of_comparables_is_the_midpoint() -> None:
    parity = internal_parity(
        offered_ctc=Decimal("1900000"),
        comparable_ctcs=[Decimal("1800000"), Decimal("2000000")],
        grade="L5",
    )
    assert parity.median == Decimal("1900000.00")
    assert parity.percent_above_median == Decimal("0.00")


def test_the_median_of_an_odd_number_is_the_middle_value() -> None:
    parity = internal_parity(
        offered_ctc=Decimal("2000000"),
        comparable_ctcs=[Decimal("1600000"), Decimal("1800000"), Decimal("2600000")],
        grade="L5",
    )
    assert parity.median == Decimal("1800000.00")
    assert parity.percent_above_median == Decimal("11.11")


def test_an_offer_at_the_median_is_not_reported_as_above_it() -> None:
    parity = internal_parity(
        offered_ctc=Decimal("1800000"),
        comparable_ctcs=[Decimal("1600000"), Decimal("1800000"), Decimal("2000000")],
        grade="L5",
    )
    assert not parity.above_every_comparable
    assert "At or below the median" in parity.headline


def test_two_comparables_are_labelled_indicative_rather_than_hidden() -> None:
    """Min/median/max over two people are two people's salaries wearing a
    statistical hat. Still shown — hiding them would be worse — but said so."""
    parity = internal_parity(
        offered_ctc=Decimal("2000000"),
        comparable_ctcs=[Decimal("1800000"), Decimal("1900000")],
        grade="L5",
    )
    assert parity.is_indicative_only
    assert parity.comparable_count == 2
    assert parity.median is not None


def test_four_comparables_are_not_labelled_indicative() -> None:
    parity = internal_parity(
        offered_ctc=Decimal("2000000"),
        comparable_ctcs=[Decimal(v) for v in (1600000, 1700000, 1800000, 1900000)],
        grade="L5",
    )
    assert not parity.is_indicative_only


def test_no_comparables_says_this_offer_sets_the_benchmark() -> None:
    """Not an error and not a silent pass. The first L7 hire genuinely has
    nobody to be compared against, and the approver should know that is why the
    panel is empty."""
    parity = internal_parity(offered_ctc=Decimal("4000000"), comparable_ctcs=[], grade="L7")
    assert parity.comparable_count == 0
    assert parity.median is None
    assert not parity.above_every_comparable
    assert "sets the internal benchmark" in parity.headline
    assert parity.is_indicative_only


def test_parity_serialises_every_number_as_a_string() -> None:
    """Money crosses to the browser as a string or it crosses as a float."""
    payload = internal_parity(
        offered_ctc=Decimal("2000000"),
        comparable_ctcs=[Decimal("1800000"), Decimal("1900000"), Decimal("2000000")],
        grade="L5",
    ).as_dict()
    for key in ("offered_ctc", "minimum", "median", "maximum", "percent_above_median"):
        assert isinstance(payload[key], str), key
