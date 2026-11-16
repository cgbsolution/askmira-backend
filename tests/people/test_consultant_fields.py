"""What a consultant's record must carry, and who may not carry it (no database)."""

from __future__ import annotations

from decimal import Decimal

from app.models.contractors import TdsSection
from app.models.employee import ApproverRoute, EmploymentType
from app.schemas.employee import consultant_billing_problem

C = EmploymentType.CONSULTANT


def _problem(kind=C, **overrides):
    fields = {
        "gst_applicable": False,
        "gst_rate": None,
        "gstin": None,
        "tds_section": TdsSection.S194J_PROFESSIONAL,
        "route": ApproverRoute.INTERNAL,
        "contact_name": None,
        "contact_email": None,
    }
    fields.update(overrides)
    return consultant_billing_problem(kind, **fields)


def test_non_consultant_cannot_carry_consultant_fields() -> None:
    assert _problem(EmploymentType.FULL_TIME, route=None) is not None


def test_non_consultant_with_nothing_set_is_valid() -> None:
    assert _problem(EmploymentType.FULL_TIME, tds_section=None, route=None) is None


def test_customer_route_needs_name_and_email() -> None:
    problem = _problem(route=ApproverRoute.CUSTOMER, contact_name="Asha", contact_email=None)
    assert problem is not None and "customer contact" in problem


def test_gst_needs_rate_and_gstin() -> None:
    assert _problem(gst_applicable=True, gst_rate=None, gstin="29ABCDE1234F1Z5") is not None
    assert _problem(gst_applicable=True, gst_rate=Decimal("18"), gstin=None) is not None
    assert _problem(gst_applicable=True, gst_rate=Decimal("18"), gstin="29ABCDE1234F1Z5") is None


def test_gst_details_need_gst_applicable() -> None:
    assert _problem(gst_applicable=False, gst_rate=Decimal("18")) is not None


def test_complete_consultant_is_valid() -> None:
    assert _problem() is None
