"""Slice 34 — GST input credit against a real database.

The checklist gates:

- eligibility rules reviewed by a tax advisor before shipping
- uncertain cases route to review, never auto-decided
- GSTR-2B reconciliation reports the rupee gap
- cross-employee duplicate invoice detection works
- dashboard states captured vs at-risk credit in rupees

And the prompt's own test list: the GSTIN checksum catching a malformed number,
a blocked category marked ineligible with its reason cited, and IGST versus
CGST/SGST determined by place of supply.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.employee import Employee, EmploymentStatus
from app.models.gst import (
    GstCreditReview,
    GstEligibilityEnum,
    GstinPortalStatusEnum,
    GstInputCredit,
    Gstr2bMatchEnum,
    GstReconciliation,
    GstTaxSplitEnum,
)
from app.services.gst import service as gst
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

D = Decimal
TODAY = date(2026, 9, 30)

#: Published example GSTINs with correct checksums.
MH = "27AAPFU0939F1ZV"
KA = "29AAGCB7383J1Z4"


# ── Helpers ─────────────────────────────────────────────────────────────────


async def capture(session, hr_user, **kw):
    body = {
        "vendor_gstin": MH,
        "invoice_number": "INV/2026/0001",
        "invoice_date": date(2026, 9, 3),
        "category_key": "office_supplies",
        "taxable_value": D("10000"),
        "cgst": D("900"),
        "sgst": D("900"),
        "supplier_state_code": "27",
        "place_of_supply_state_code": "27",
    }
    body.update(kw)
    return await gst.capture_credit(session, hr_user, **body)


async def make_employee(session, tenant, legal_entity, location, code):
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code,
        last_name="Test",
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=legal_entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2024, 1, 1),
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


# ══════════════════════════════════════════════════════════════════════════════
# GSTIN validation, and capture that never refuses
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_valid_invoice_captures_as_eligible(session, hr_user):
    out = await capture(session, hr_user)
    assert out["gstin"]["is_valid"] is True
    assert out["assessment"]["eligibility"] == "eligible"
    assert out["assessment"]["claimable_amount"] == "1800.00"
    assert out["expense_recorded"] is True


async def test_a_malformed_gstin_is_captured_and_flagged(session, hr_user):
    """The expense stands. Only the input credit is in question."""
    out = await capture(session, hr_user, vendor_gstin=MH[:14] + "Z")
    assert out["gstin"]["is_valid"] is False
    assert out["gstin"]["problem"] == "checksum_failed"
    assert out["gstin"]["blocks_the_expense"] is False
    assert out["assessment"]["eligibility"] == "ineligible"

    row = (
        await session.execute(
            select(GstInputCredit).where(GstInputCredit.id == uuid.UUID(out["credit_id"]))
        )
    ).scalar_one()
    assert row.gstin_is_valid is False
    assert "The expense itself is unaffected" in row.eligibility_reason


async def test_an_invoice_with_no_gstin_is_still_captured(session, hr_user):
    out = await capture(session, hr_user, vendor_gstin="")
    assert out["gstin"]["problem"] == "empty"
    assert out["credit_id"]


async def test_the_portal_status_is_recorded_as_not_checked(session, hr_user):
    out = await capture(session, hr_user)
    row = (
        await session.execute(
            select(GstInputCredit).where(GstInputCredit.id == uuid.UUID(out["credit_id"]))
        )
    ).scalar_one()
    assert row.gstin_portal_status is GstinPortalStatusEnum.NOT_CHECKED


async def test_the_companys_own_bad_gstin_is_refused(session, hr_user, legal_entity):
    """This one *is* worth blocking: a wrong entity GSTIN misclassifies the
    place of supply on every invoice that follows."""
    with pytest.raises(HTTPException) as exc:
        await gst.register_entity_gstin(
            session, hr_user, legal_entity_id=legal_entity.id, gstin=MH[:14] + "Z"
        )
    assert "misclassifies the place of supply" in exc.value.detail


async def test_the_companys_registration_records_its_state(session, hr_user, legal_entity):
    row = await gst.register_entity_gstin(
        session, hr_user, legal_entity_id=legal_entity.id, gstin=KA
    )
    assert row.state_code == "29"
    assert row.state_name == "Karnataka"


# ══════════════════════════════════════════════════════════════════════════════
# Gate: IGST versus CGST/SGST by place of supply
# ══════════════════════════════════════════════════════════════════════════════


async def test_an_intra_state_supply_in_our_state_is_claimable(session, hr_user, legal_entity):
    await gst.register_entity_gstin(session, hr_user, legal_entity_id=legal_entity.id, gstin=MH)
    out = await capture(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        supplier_state_code="27",
        place_of_supply_state_code="27",
    )
    assert out["assessment"]["tax_split"] == "cgst_sgst"
    assert out["assessment"]["eligibility"] == "eligible"


async def test_the_hotel_bill_case_is_ineligible_with_the_reason(session, hr_user, legal_entity):
    """A Karnataka-registered company's Delhi hotel: real CGST and SGST, and no
    credit, on an invoice that looks exactly like a claimable one."""
    await gst.register_entity_gstin(session, hr_user, legal_entity_id=legal_entity.id, gstin=KA)
    out = await capture(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        category_key="hotel",
        supplier_state_code="07",
        place_of_supply_state_code="07",
    )
    assert out["assessment"]["tax_split"] == "cgst_sgst_other_state"
    assert out["assessment"]["eligibility"] == "ineligible"
    assert out["assessment"]["claimable_amount"] == "0.00"
    assert "this entity is registered in 29" in out["assessment"]["reason"]


async def test_a_proper_inter_state_invoice_is_eligible(session, hr_user, legal_entity):
    await gst.register_entity_gstin(session, hr_user, legal_entity_id=legal_entity.id, gstin=KA)
    out = await capture(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        supplier_state_code="27",
        place_of_supply_state_code="29",
        cgst=D("0"),
        sgst=D("0"),
        igst=D("1800"),
    )
    assert out["assessment"]["tax_split"] == "igst"
    assert out["assessment"]["eligibility"] == "eligible"


async def test_the_tax_split_is_stored_on_the_row(session, hr_user, legal_entity):
    await gst.register_entity_gstin(session, hr_user, legal_entity_id=legal_entity.id, gstin=MH)
    out = await capture(session, hr_user, legal_entity_id=legal_entity.id)
    row = (
        await session.execute(
            select(GstInputCredit).where(GstInputCredit.id == uuid.UUID(out["credit_id"]))
        )
    ).scalar_one()
    assert row.tax_split is GstTaxSplitEnum.CGST_SGST


async def test_an_unknown_place_of_supply_goes_to_review(session, hr_user):
    out = await capture(session, hr_user, place_of_supply_state_code=None)
    assert out["assessment"]["eligibility"] == "uncertain"
    assert out["assessment"]["needs_review"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Gate: blocked categories and the review queue
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_blocked_category_is_ineligible_with_the_section_cited(session, hr_user):
    out = await capture(session, hr_user, category_key="club_membership")
    assert out["assessment"]["eligibility"] == "ineligible"
    assert "17(5)" in out["assessment"]["reference"]
    assert out["assessment"]["claimable_amount"] == "0.00"


async def test_a_category_with_exceptions_routes_to_review(session, hr_user):
    """Guessing eligible claims credit the company may not be entitled to;
    guessing ineligible leaks money."""
    out = await capture(session, hr_user, category_key="food_and_beverage")
    assert out["assessment"]["eligibility"] == "uncertain"

    queue = await gst.review_queue(session, hr_user)
    assert queue["count"] == 1
    assert queue["total_at_stake"] == "1800.00"


async def test_the_review_queue_is_ordered_by_money(session, hr_user):
    """Worked through by a person with limited time; the expensive lines are
    the ones that matter."""
    await capture(
        session,
        hr_user,
        category_key="food_and_beverage",
        invoice_number="SMALL-1",
        cgst=D("90"),
        sgst=D("90"),
    )
    await capture(
        session,
        hr_user,
        category_key="motor_vehicle",
        invoice_number="BIG-1",
        cgst=D("9000"),
        sgst=D("9000"),
    )
    queue = await gst.review_queue(session, hr_user)
    assert queue["lines"][0]["invoice_number"] == "BIG-1"
    assert queue["lines"][0]["amount_at_stake"] == "18000.00"


async def test_a_review_records_the_person_and_the_rationale(session, hr_user):
    """In an assessment the question is who decided and why."""
    out = await capture(session, hr_user, category_key="food_and_beverage")
    decided = await gst.review_credit(
        session,
        hr_user,
        credit_id=uuid.UUID(out["credit_id"]),
        decision=GstEligibilityEnum.ELIGIBLE,
        rationale="Client entertainment onward-supplied; advisor confirmed.",
        decided_on=TODAY,
    )
    assert decided["eligibility"] == "eligible"
    assert decided["claimable_amount"] == "1800.00"

    review = (
        await session.execute(
            select(GstCreditReview).where(GstCreditReview.credit_id == uuid.UUID(out["credit_id"]))
        )
    ).scalar_one()
    assert review.decided_by_user_id == hr_user.id
    assert "advisor confirmed" in review.rationale


async def test_a_review_needs_a_rationale(session, hr_user):
    out = await capture(session, hr_user, category_key="food_and_beverage")
    with pytest.raises(HTTPException) as exc:
        await gst.review_credit(
            session,
            hr_user,
            credit_id=uuid.UUID(out["credit_id"]),
            decision=GstEligibilityEnum.INELIGIBLE,
            rationale="   ",
            decided_on=TODAY,
        )
    assert "needs a rationale" in exc.value.detail


async def test_a_review_cannot_leave_the_line_uncertain(session, hr_user):
    out = await capture(session, hr_user, category_key="food_and_beverage")
    with pytest.raises(HTTPException) as exc:
        await gst.review_credit(
            session,
            hr_user,
            credit_id=uuid.UUID(out["credit_id"]),
            decision=GstEligibilityEnum.UNCERTAIN,
            rationale="Still not sure.",
            decided_on=TODAY,
        )
    assert "has to reach a decision" in exc.value.detail


async def test_a_settled_line_cannot_be_overridden_by_a_review(session, hr_user):
    """Overriding one the rules settled would depart from Section 17(5) with
    nothing recording that a departure was made."""
    out = await capture(session, hr_user, category_key="club_membership")
    with pytest.raises(HTTPException) as exc:
        await gst.review_credit(
            session,
            hr_user,
            credit_id=uuid.UUID(out["credit_id"]),
            decision=GstEligibilityEnum.ELIGIBLE,
            rationale="We want the credit.",
            decided_on=TODAY,
        )
    assert "nothing recording that a departure was made" in exc.value.detail


async def test_a_reviewed_ineligible_line_claims_nothing(session, hr_user):
    out = await capture(session, hr_user, category_key="motor_vehicle")
    decided = await gst.review_credit(
        session,
        hr_user,
        credit_id=uuid.UUID(out["credit_id"]),
        decision=GstEligibilityEnum.INELIGIBLE,
        rationale="Passenger vehicle for employee use; no exception applies.",
        decided_on=TODAY,
    )
    assert decided["claimable_amount"] == "0.00"


async def test_a_tenant_rule_overrides_the_shipped_default(session, hr_user):
    """Once an advisor has been through it, a category can be settled outright
    rather than routed to review every month."""
    await gst.set_blocked_category(
        session,
        hr_user,
        category_key="food_and_beverage",
        reason="Advisor: no onward supply in this business; blocked outright.",
        has_exceptions=False,
        reviewed=True,
        reviewed_by="Advisor LLP",
        reviewed_on=date(2026, 4, 1),
    )
    out = await capture(session, hr_user, category_key="food_and_beverage")
    assert out["assessment"]["eligibility"] == "ineligible"
    assert "Advisor" in out["assessment"]["reason"]


async def test_a_rule_marked_reviewed_needs_a_named_reviewer(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await gst.set_blocked_category(
            session,
            hr_user,
            category_key="stationery",
            reason="Blocked.",
            reviewed=True,
        )
    assert "name who reviewed it" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: GSTR-2B reconciliation reports the rupee gap
# ══════════════════════════════════════════════════════════════════════════════


async def test_reconciling_without_an_import_is_refused(session, hr_user):
    """Reconciling against nothing reports every line as unfiled, which is a
    frightening number that means nothing."""
    with pytest.raises(HTTPException) as exc:
        await gst.reconcile_period(
            session,
            hr_user,
            period_label="2026-09",
            period_start=date(2026, 9, 1),
            period_end=TODAY,
        )
    assert "frightening number that means nothing" in exc.value.detail


async def test_all_three_match_states_are_reported(session, hr_user):
    await capture(session, hr_user, invoice_number="INV/2026/0001")
    await capture(session, hr_user, vendor_gstin=KA, invoice_number="BILL-77")

    await gst.import_gstr2b(
        session,
        hr_user,
        period_label="2026-09",
        imported_on=TODAY,
        lines=[
            {
                "vendor_gstin": MH,
                "invoice_number": "inv-2026-0001",
                "invoice_date": date(2026, 9, 3),
                "taxable_value": D("10000"),
                "cgst": D("900"),
                "sgst": D("900"),
            },
            {
                "vendor_gstin": "33AABCT1332L1ZM",
                "invoice_number": "MISC-9",
                "invoice_date": date(2026, 9, 7),
                "taxable_value": D("1000"),
                "igst": D("180"),
            },
        ],
    )
    report = await gst.reconcile_period(
        session,
        hr_user,
        period_label="2026-09",
        period_start=date(2026, 9, 1),
        period_end=TODAY,
    )
    assert report["counts"]["matched"] == 1
    assert report["counts"]["missing_in_2b"] == 1
    assert report["counts"]["missing_in_register"] == 1
    assert report["at_risk_from_unfiled_vendors"] == "1800.00"


async def test_a_differently_punctuated_invoice_number_still_matches(session, hr_user):
    """`INV/2026/0001` from OCR and `inv-2026-0001` from the portal are the same
    document; matching the raw strings chases a vendor who filed."""
    await capture(session, hr_user, invoice_number="INV/2026/0001")
    await gst.import_gstr2b(
        session,
        hr_user,
        period_label="2026-09",
        imported_on=TODAY,
        lines=[
            {
                "vendor_gstin": MH,
                "invoice_number": "inv 2026 0001",
                "invoice_date": date(2026, 9, 3),
                "taxable_value": D("10000"),
                "cgst": D("900"),
                "sgst": D("900"),
            }
        ],
    )
    report = await gst.reconcile_period(
        session,
        hr_user,
        period_label="2026-09",
        period_start=date(2026, 9, 1),
        period_end=TODAY,
    )
    assert report["counts"]["matched"] == 1
    assert report["at_risk_from_unfiled_vendors"] == "0.00"


async def test_the_match_state_is_written_back_to_the_row(session, hr_user):
    out = await capture(session, hr_user)
    await gst.import_gstr2b(session, hr_user, period_label="2026-09", imported_on=TODAY, lines=[])
    await gst.reconcile_period(
        session,
        hr_user,
        period_label="2026-09",
        period_start=date(2026, 9, 1),
        period_end=TODAY,
    )
    row = (
        await session.execute(
            select(GstInputCredit).where(GstInputCredit.id == uuid.UUID(out["credit_id"]))
        )
    ).scalar_one()
    assert row.reconciliation_status is Gstr2bMatchEnum.MISSING_IN_2B


async def test_an_ineligible_line_does_not_inflate_the_at_risk_figure(session, hr_user):
    """Inflating it with blocked categories is how a finance team stops
    believing the number."""
    await capture(session, hr_user, invoice_number="GOOD-1")
    await capture(session, hr_user, invoice_number="CLUB-1", category_key="club_membership")
    await gst.import_gstr2b(session, hr_user, period_label="2026-09", imported_on=TODAY, lines=[])
    report = await gst.reconcile_period(
        session,
        hr_user,
        period_label="2026-09",
        period_start=date(2026, 9, 1),
        period_end=TODAY,
    )
    assert report["missing_in_2b_tax"] == "3600.00"
    assert report["at_risk_from_unfiled_vendors"] == "1800.00"


async def test_importing_the_same_period_twice_is_refused(session, hr_user):
    await gst.import_gstr2b(session, hr_user, period_label="2026-09", imported_on=TODAY, lines=[])
    with pytest.raises(HTTPException) as exc:
        await gst.import_gstr2b(
            session, hr_user, period_label="2026-09", imported_on=TODAY, lines=[]
        )
    assert "double every line" in exc.value.detail


async def test_a_reconciliation_is_recorded(session, hr_user):
    await gst.import_gstr2b(session, hr_user, period_label="2026-09", imported_on=TODAY, lines=[])
    await gst.reconcile_period(
        session,
        hr_user,
        period_label="2026-09",
        period_start=date(2026, 9, 1),
        period_end=TODAY,
    )
    row = (await session.execute(select(GstReconciliation))).scalar_one()
    assert row.period_label == "2026-09"


async def test_a_period_cannot_be_reconciled_twice(session, hr_user):
    await gst.import_gstr2b(session, hr_user, period_label="2026-09", imported_on=TODAY, lines=[])
    await gst.reconcile_period(
        session,
        hr_user,
        period_label="2026-09",
        period_start=date(2026, 9, 1),
        period_end=TODAY,
    )
    with pytest.raises(HTTPException) as exc:
        await gst.reconcile_period(
            session,
            hr_user,
            period_label="2026-09",
            period_start=date(2026, 9, 1),
            period_end=TODAY,
        )
    assert "who chased it" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: cross-employee duplicate detection
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_same_invoice_from_two_employees_is_detected(
    session, hr_user, tenant, legal_entity, location
):
    """The case a per-employee check cannot see."""
    first = await make_employee(session, tenant, legal_entity, location, "DUP1")
    second = await make_employee(session, tenant, legal_entity, location, "DUP2")

    await capture(session, hr_user, invoice_number="INV/2026/0042", employee_id=first.id)
    out = await capture(session, hr_user, invoice_number="inv-2026-0042", employee_id=second.id)
    assert out["duplicate_of"]
    assert out["duplicate_across_employees"] is True

    report = await gst.duplicate_report(session, hr_user)
    assert report["duplicate_groups"] == 1
    assert report["across_employees"] == 1


async def test_two_different_vendors_are_not_a_duplicate(session, hr_user):
    await capture(session, hr_user, vendor_gstin=MH, invoice_number="INV/1")
    out = await capture(session, hr_user, vendor_gstin=KA, invoice_number="INV/1")
    assert out["duplicate_of"] == []

    report = await gst.duplicate_report(session, hr_user)
    assert report["duplicate_groups"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the dashboard in rupees
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_dashboard_confirms_nothing_before_reconciliation(session, hr_user):
    """A figure that turns out to be optimistic destroys the conversation this
    dashboard exists to have."""
    await capture(session, hr_user)
    dashboard = await gst.credit_dashboard(session, hr_user)
    assert dashboard["eligible_tax"] == "1800.00"
    assert dashboard["confirmed_claimable_tax"] == "0.00"
    assert dashboard["reconciled"] is False
    assert "Nothing has been reconciled" in dashboard["note"]


async def test_the_dashboard_separates_confirmed_from_at_risk(session, hr_user):
    await capture(session, hr_user, invoice_number="FILED-1")
    await capture(session, hr_user, vendor_gstin=KA, invoice_number="UNFILED-1")
    await gst.import_gstr2b(
        session,
        hr_user,
        period_label="2026-09",
        imported_on=TODAY,
        lines=[
            {
                "vendor_gstin": MH,
                "invoice_number": "FILED-1",
                "invoice_date": date(2026, 9, 3),
                "taxable_value": D("10000"),
                "cgst": D("900"),
                "sgst": D("900"),
            }
        ],
    )
    await gst.reconcile_period(
        session,
        hr_user,
        period_label="2026-09",
        period_start=date(2026, 9, 1),
        period_end=TODAY,
    )
    dashboard = await gst.credit_dashboard(session, hr_user, period_label="2026-09")
    assert dashboard["confirmed_claimable_tax"] == "1800.00"
    assert dashboard["at_risk_from_unfiled_vendors"] == "1800.00"


async def test_uncertain_credit_is_counted_as_neither(session, hr_user):
    await capture(session, hr_user, invoice_number="OK-1")
    await capture(
        session,
        hr_user,
        invoice_number="FOOD-1",
        category_key="food_and_beverage",
    )
    dashboard = await gst.credit_dashboard(session, hr_user)
    assert dashboard["eligible_tax"] == "1800.00"
    assert dashboard["uncertain_tax"] == "1800.00"
    assert dashboard["uncertain_lines"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# The register export
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_export_lists_uncertain_lines_separately(session, hr_user):
    """A CA who is not told about them cannot ask, and a line quietly omitted is
    the leak this module exists to close."""
    await capture(session, hr_user, invoice_number="OK-1")
    await capture(
        session,
        hr_user,
        invoice_number="FOOD-1",
        category_key="food_and_beverage",
    )
    await capture(
        session,
        hr_user,
        invoice_number="CLUB-1",
        category_key="club_membership",
    )
    export = await gst.register_export(
        session, hr_user, period_start=date(2026, 9, 1), period_end=TODAY
    )
    assert len(export["eligible"]) == 1
    assert len(export["awaiting_review"]) == 1
    assert export["eligible_total_tax"] == "1800.00"
    assert "cannot ask" in export["note"]


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════


async def test_another_tenant_sees_none_of_this_register(session, hr_user, other_hr_user):
    await capture(session, hr_user)
    dashboard = await gst.credit_dashboard(session, other_hr_user)
    assert dashboard["captured_tax"] == "0.00"


async def test_a_duplicate_is_not_detected_across_tenants(session, hr_user, other_hr_user):
    """Two different companies legitimately hold invoices from the same vendor
    with the same numbering."""
    await capture(session, hr_user, invoice_number="INV/1")
    await capture(session, other_hr_user, invoice_number="INV/1")
    assert (await gst.duplicate_report(session, hr_user))["duplicate_groups"] == 0
    assert (await gst.duplicate_report(session, other_hr_user))["duplicate_groups"] == 0


async def test_another_tenant_cannot_review_this_line(session, hr_user, other_hr_user):
    out = await capture(session, hr_user, category_key="food_and_beverage")
    with pytest.raises(HTTPException) as exc:
        await gst.review_credit(
            session,
            other_hr_user,
            credit_id=uuid.UUID(out["credit_id"]),
            decision=GstEligibilityEnum.ELIGIBLE,
            rationale="Not mine.",
            decided_on=TODAY,
        )
    assert exc.value.status_code == 404
