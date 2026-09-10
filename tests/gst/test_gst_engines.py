"""Slice 34 — GSTIN validation, ITC eligibility, GSTR-2B and duplicates.

Organised by the way each control fails in a real input-credit capture: a
format check that passes a transposed character, a hotel bill claimed in the
wrong state, a blocked category decided either way without a person, an invoice
number that fails to match because of a slash, and a duplicate that only shows
up when you look across employees.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.gst import eligibility, gstin, register

D = Decimal

#: Published example GSTINs whose checksums are correct.
VALID = "27AAPFU0939F1ZV"
VALID_KA = "29AAGCB7383J1Z4"


# ══════════════════════════════════════════════════════════════════════════════
# GSTIN
# ══════════════════════════════════════════════════════════════════════════════


def test_the_checksum_reproduces_a_known_good_gstin():
    assert gstin.checksum_char(VALID[:14]) == VALID[14]
    assert gstin.checksum_char(VALID_KA[:14]) == VALID_KA[14]


def test_a_valid_gstin_validates():
    verdict = gstin.validate_gstin(VALID)
    assert verdict.is_valid
    assert verdict.state_name == "Maharashtra"
    assert verdict.pan == "AAPFU0939F"


def test_a_single_changed_character_is_caught():
    """A length-and-regex check passes this, and it is exactly what a
    transcription error looks like."""
    verdict = gstin.validate_gstin(VALID[:14] + "Z")
    assert not verdict.is_valid
    assert verdict.problem is gstin.GstinProblem.CHECKSUM_FAILED


def test_the_wrong_length_is_named_as_such():
    verdict = gstin.validate_gstin(VALID[:14])
    assert verdict.problem is gstin.GstinProblem.WRONG_LENGTH
    assert "fifteen characters" in verdict.message


def test_a_malformed_shape_is_distinguished_from_a_bad_checksum():
    verdict = gstin.validate_gstin("5ZAAPFU0939F1ZV")
    assert verdict.problem is gstin.GstinProblem.BAD_SHAPE


def test_an_empty_gstin_says_the_expense_still_stands():
    verdict = gstin.validate_gstin("")
    assert verdict.problem is gstin.GstinProblem.EMPTY
    assert "The expense stands" in verdict.message


def test_validation_never_blocks_the_expense():
    """The expense is real and reimbursable whatever the GSTIN says. Only the
    input credit is in question."""
    assert gstin.validate_gstin("nonsense").blocks_the_expense is False
    assert gstin.validate_gstin(VALID).blocks_the_expense is False


def test_whitespace_and_case_are_tolerated():
    assert gstin.validate_gstin(f"  {VALID.lower()} ").is_valid


def test_the_portal_status_defaults_to_not_checked():
    """No portal lookup is built, and a structurally valid GSTIN belonging to a
    cancelled registration is a credit that will be denied."""
    verdict = gstin.validate_gstin(VALID)
    assert verdict.portal_status is gstin.PortalStatus.NOT_CHECKED
    assert "not been checked against the GST portal" in verdict.message


def test_a_cancelled_registration_is_not_valid_for_credit():
    verdict = gstin.validate_gstin(
        VALID, portal_status=gstin.PortalStatus.CANCELLED
    )
    assert not verdict.is_valid
    assert "cancelled" in verdict.message


def test_other_territory_and_centre_codes_are_recognised():
    """Routinely missing from hand-rolled validators, which then reject
    legitimate invoices."""
    assert "97" in gstin.STATE_CODES
    assert "99" in gstin.STATE_CODES


# ══════════════════════════════════════════════════════════════════════════════
# Place of supply
# ══════════════════════════════════════════════════════════════════════════════


def invoice(**kw) -> eligibility.InvoiceFacts:
    base = {
        "category_key": "office_supplies",
        "vendor_gstin": VALID,
        "invoice_number": "INV-1",
        "taxable_value": D("10000"),
        "cgst": D("900"),
        "sgst": D("900"),
        "igst": D("0"),
        "supplier_state_code": "27",
        "place_of_supply_state_code": "27",
        "recipient_state_code": "27",
    }
    base.update(kw)
    return eligibility.InvoiceFacts(**base)


def test_an_intra_state_supply_in_our_state_is_cgst_sgst():
    assert (
        eligibility.determine_tax_split(
            supplier_state_code="27",
            place_of_supply_state_code="27",
            recipient_state_code="27",
        )
        is eligibility.TaxSplit.CGST_SGST
    )


def test_an_inter_state_supply_is_igst():
    assert (
        eligibility.determine_tax_split(
            supplier_state_code="27",
            place_of_supply_state_code="29",
            recipient_state_code="29",
        )
        is eligibility.TaxSplit.IGST
    )


def test_an_intra_state_supply_in_another_state_is_its_own_case():
    """The hotel bill. A Karnataka company's Delhi hotel is an intra-state
    supply in Delhi and the CGST/SGST belongs to a state it is not registered
    in."""
    assert (
        eligibility.determine_tax_split(
            supplier_state_code="07",
            place_of_supply_state_code="07",
            recipient_state_code="29",
        )
        is eligibility.TaxSplit.CGST_SGST_OTHER_STATE
    )


def test_a_missing_state_code_raises_rather_than_being_guessed():
    with pytest.raises(eligibility.EligibilityError) as exc:
        eligibility.determine_tax_split(
            supplier_state_code="27",
            place_of_supply_state_code="",
            recipient_state_code="29",
        )
    assert "Guessing it" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
# Eligibility
# ══════════════════════════════════════════════════════════════════════════════


def test_an_ordinary_business_expense_is_eligible():
    verdict = eligibility.assess(invoice())
    assert verdict.eligibility is eligibility.Eligibility.ELIGIBLE
    assert verdict.claimable_amount == D("1800.00")


def test_a_blocked_category_with_no_exceptions_is_ineligible():
    verdict = eligibility.assess(invoice(category_key="club_membership"))
    assert verdict.eligibility is eligibility.Eligibility.INELIGIBLE
    assert "17(5)" in verdict.reference
    assert verdict.claimable_amount == D("0.00")


def test_a_blocked_category_with_exceptions_goes_to_review():
    """Guessing eligible claims credit the company may not be entitled to;
    guessing ineligible leaks money."""
    verdict = eligibility.assess(invoice(category_key="food_and_beverage"))
    assert verdict.eligibility is eligibility.Eligibility.UNCERTAIN
    assert verdict.needs_review
    assert verdict.amount_at_stake == D("1800.00")


def test_a_motor_vehicle_is_uncertain_rather_than_blanket_ineligible():
    """A blanket 'ineligible' costs a logistics company real money."""
    verdict = eligibility.assess(invoice(category_key="motor_vehicle"))
    assert verdict.eligibility is eligibility.Eligibility.UNCERTAIN
    assert "further supply of vehicles" in verdict.reason


def test_the_hotel_bill_case_is_ineligible_with_the_reason_stated():
    verdict = eligibility.assess(
        invoice(
            supplier_state_code="07",
            place_of_supply_state_code="07",
            recipient_state_code="29",
        )
    )
    assert verdict.eligibility is eligibility.Eligibility.INELIGIBLE
    assert verdict.tax_split is eligibility.TaxSplit.CGST_SGST_OTHER_STATE
    assert "most commonly claimed credit that should not have been" in verdict.reason


def test_an_invalid_gstin_makes_the_credit_ineligible_not_the_expense():
    verdict = eligibility.assess(invoice(gstin_is_valid=False))
    assert verdict.eligibility is eligibility.Eligibility.INELIGIBLE
    assert "The expense itself is unaffected" in verdict.reason


def test_igst_expected_but_cgst_charged_goes_to_review():
    verdict = eligibility.assess(
        invoice(
            supplier_state_code="27",
            place_of_supply_state_code="29",
            recipient_state_code="29",
        )
    )
    assert verdict.eligibility is eligibility.Eligibility.UNCERTAIN
    assert "should carry IGST" in verdict.reason


def test_cgst_expected_but_igst_charged_goes_to_review():
    verdict = eligibility.assess(
        invoice(cgst=D("0"), sgst=D("0"), igst=D("1800"))
    )
    assert verdict.eligibility is eligibility.Eligibility.UNCERTAIN


def test_a_proper_igst_invoice_is_eligible():
    verdict = eligibility.assess(
        invoice(
            supplier_state_code="27",
            place_of_supply_state_code="29",
            recipient_state_code="29",
            cgst=D("0"),
            sgst=D("0"),
            igst=D("1800"),
        )
    )
    assert verdict.eligibility is eligibility.Eligibility.ELIGIBLE
    assert verdict.claimable_amount == D("1800.00")


def test_an_unknown_place_of_supply_goes_to_review():
    verdict = eligibility.assess(invoice(place_of_supply_state_code=""))
    assert verdict.eligibility is eligibility.Eligibility.UNCERTAIN
    assert verdict.needs_review


def test_cess_is_excluded_from_the_claimable_figure():
    """Cess credit is generally usable only against cess, so folding it in
    overstates what is available against GST."""
    verdict = eligibility.assess(invoice(cess=D("500")))
    assert verdict.claimable_amount == D("1800.00")
    assert "Cess of 500.00 is excluded" in verdict.reason


def test_an_invoice_with_no_tax_carries_no_credit():
    verdict = eligibility.assess(invoice(cgst=D("0"), sgst=D("0")))
    assert verdict.eligibility is eligibility.Eligibility.INELIGIBLE
    assert "exempt or composition supply" in verdict.reason


def test_an_uncertain_line_contributes_nothing_claimable():
    """A figure nobody has decided must not appear in a total finance reads as
    claimable."""
    verdict = eligibility.assess(invoice(category_key="food_and_beverage"))
    assert verdict.claimable_amount == D("0.00")
    assert verdict.amount_at_stake > 0


def test_every_shipped_blocked_rule_is_unreviewed():
    assert all(not r.reviewed for r in eligibility.DEFAULT_BLOCKED_CATEGORIES)


def test_a_rule_with_no_reason_is_refused():
    """A line marked ineligible with no explanation is one finance cannot
    challenge and will eventually stop trusting."""
    with pytest.raises(eligibility.EligibilityError):
        eligibility.BlockedCategoryRule(category_key="x", reason="  ")


def test_a_reviewed_rule_needs_a_named_reviewer():
    with pytest.raises(eligibility.EligibilityError):
        eligibility.BlockedCategoryRule(
            category_key="x", reason="Blocked.", reviewed=True
        )


# ══════════════════════════════════════════════════════════════════════════════
# GSTR-2B reconciliation
# ══════════════════════════════════════════════════════════════════════════════


C = register.CapturedInvoice
L = register.Gstr2bLine


def test_invoice_numbers_normalise_to_one_key():
    """`INV/2026/0042` from OCR and `inv-2026-0042` from a portal download are
    the same document; matching the raw strings chases a vendor who filed."""
    assert (
        register.normalise_invoice_number("INV/2026/0042")
        == register.normalise_invoice_number("inv-2026-0042")
        == register.normalise_invoice_number("INV 2026 0042")
    )


def test_leading_zeros_are_kept():
    """`INV-042` and `INV-42` are different documents often enough that
    collapsing them would create false matches."""
    assert register.normalise_invoice_number("INV-042") != (
        register.normalise_invoice_number("INV-42")
    )


def test_all_three_match_states_are_reported():
    captured = [
        C("c1", VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800")),
        C("c2", VALID_KA, "BILL-77", date(2026, 9, 3), D("5000"), D("900")),
    ]
    portal = [
        L(VALID, "inv-1", date(2026, 9, 1), D("10000"), D("1800")),
        L("33AABCT1332L1ZM", "MISC-9", date(2026, 9, 7), D("1000"), D("180")),
    ]
    out = register.reconcile_2b(
        captured=captured, portal=portal, period_label="2026-09"
    )
    assert out["counts"]["matched"] == 1
    assert out["counts"]["missing_in_2b"] == 1
    assert out["counts"]["missing_in_register"] == 1


def test_the_rupee_gap_is_the_headline():
    captured = [C("c1", VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800"))]
    out = register.reconcile_2b(captured=captured, portal=[], period_label="2026-09")
    assert out["at_risk_from_unfiled_vendors"] == "1800.00"
    assert "the vendor has not filed" in out["note"]


def test_an_ineligible_line_does_not_inflate_the_at_risk_figure():
    """Inflating it with blocked categories is how a finance team stops
    believing the number."""
    captured = [
        C("c1", VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800")),
        C(
            "c2",
            VALID_KA,
            "INV/2",
            date(2026, 9, 1),
            D("2000"),
            D("360"),
            eligibility="ineligible",
        ),
    ]
    out = register.reconcile_2b(captured=captured, portal=[], period_label="2026-09")
    assert out["missing_in_2b_tax"] == "2160.00"
    assert out["at_risk_from_unfiled_vendors"] == "1800.00"


def test_a_tax_mismatch_is_its_own_state():
    captured = [C("c1", VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800"))]
    portal = [L(VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1500"))]
    out = register.reconcile_2b(
        captured=captured, portal=portal, period_label="2026-09"
    )
    assert out["counts"]["value_mismatch"] == 1
    assert out["mismatched_portal_tax"] == "1500.00"


def test_a_rounding_difference_still_matches():
    captured = [C("c1", VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800.00"))]
    portal = [L(VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800.50"))]
    out = register.reconcile_2b(
        captured=captured, portal=portal, period_label="2026-09"
    )
    assert out["counts"]["matched"] == 1


def test_two_portal_lines_for_one_invoice_are_summed():
    captured = [C("c1", VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800"))]
    portal = [
        L(VALID, "INV/1", date(2026, 9, 1), D("5000"), D("900")),
        L(VALID, "INV/1", date(2026, 9, 1), D("5000"), D("900")),
    ]
    out = register.reconcile_2b(
        captured=captured, portal=portal, period_label="2026-09"
    )
    assert out["counts"]["matched"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# Duplicates
# ══════════════════════════════════════════════════════════════════════════════


def test_a_duplicate_across_two_employees_is_detected():
    """The case a per-employee check cannot see."""
    rows = [
        C("d1", VALID, "INV/1", date(2026, 9, 1), D("1000"), D("180"), employee_id="e1"),
        C("d2", VALID, "inv 1", date(2026, 9, 1), D("1000"), D("180"), employee_id="e2"),
    ]
    found = register.duplicates(rows)
    assert len(found) == 1
    assert found[0]["across_employees"] is True
    assert found[0]["employee_ids"] == ["e1", "e2"]


def test_a_resubmission_by_one_person_is_reported_differently():
    rows = [
        C("d1", VALID, "INV/1", date(2026, 9, 1), D("1000"), D("180"), employee_id="e1"),
        C("d2", VALID, "INV/1", date(2026, 9, 1), D("1000"), D("180"), employee_id="e1"),
    ]
    found = register.duplicates(rows)
    assert found[0]["across_employees"] is False
    assert "resubmission" in found[0]["detail"]


def test_different_vendors_with_the_same_invoice_number_are_not_duplicates():
    rows = [
        C("d1", VALID, "INV/1", date(2026, 9, 1), D("1000"), D("180"), employee_id="e1"),
        C("d2", VALID_KA, "INV/1", date(2026, 9, 1), D("1000"), D("180"), employee_id="e2"),
    ]
    assert register.duplicates(rows) == []


# ══════════════════════════════════════════════════════════════════════════════
# The dashboard
# ══════════════════════════════════════════════════════════════════════════════


def test_the_dashboard_separates_confirmed_from_at_risk():
    captured = [
        C("c1", VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800")),
        C("c2", VALID_KA, "INV/2", date(2026, 9, 1), D("5000"), D("900")),
    ]
    portal = [L(VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800"))]
    recon = register.reconcile_2b(
        captured=captured, portal=portal, period_label="2026-09"
    )
    dashboard = register.value_dashboard(
        captured=captured, reconciliation=recon, period_label="2026-09"
    )
    assert dashboard["confirmed_claimable_tax"] == "1800.00"
    assert dashboard["at_risk_from_unfiled_vendors"] == "900.00"


def test_an_unreconciled_dashboard_claims_nothing_is_confirmed():
    """A number that turns out to be optimistic destroys the conversation
    permanently."""
    captured = [C("c1", VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800"))]
    dashboard = register.value_dashboard(captured=captured)
    assert dashboard["reconciled"] is False
    assert dashboard["confirmed_claimable_tax"] == "0.00"
    assert "Nothing has been reconciled" in dashboard["note"]


def test_uncertain_lines_are_counted_as_neither():
    captured = [
        C("c1", VALID, "INV/1", date(2026, 9, 1), D("10000"), D("1800")),
        C(
            "c2",
            VALID_KA,
            "INV/2",
            date(2026, 9, 1),
            D("5000"),
            D("900"),
            eligibility="uncertain",
        ),
    ]
    dashboard = register.value_dashboard(captured=captured)
    assert dashboard["eligible_tax"] == "1800.00"
    assert dashboard["uncertain_tax"] == "900.00"
    assert dashboard["uncertain_lines"] == 1
