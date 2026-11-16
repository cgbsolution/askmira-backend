"""What a consultant's record accepts for GST and TDS."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.employee import EmploymentType
from app.schemas.employee import EmployeeCreate, consultant_billing_problem

# ── What the record accepts ─────────────────────────────────────────────────

C = EmploymentType.CONSULTANT


@pytest.mark.parametrize(
    ("kind", "gst_applicable", "ok"),
    [
        (C, True, False),  # GST applicable needs a rate and GSTIN
        (C, False, True),
        (EmploymentType.FULL_TIME, True, False),  # only a consultant has GST details
        (EmploymentType.FULL_TIME, False, True),
    ],
)
def test_consultant_billing_rules(kind, gst_applicable, ok) -> None:
    problem = consultant_billing_problem(
        kind,
        gst_applicable=gst_applicable,
        gst_rate=None,
        gstin=None,
        tds_section=None,
        route=None,
        contact_name=None,
        contact_email=None,
    )
    assert (problem is None) is ok


def test_create_refuses_gst_without_a_gstin() -> None:
    with pytest.raises(ValidationError, match="GSTIN"):
        EmployeeCreate(
            employee_code="C1",
            first_name="A",
            work_email="a@example.com",
            employment_type=C,
            gst_applicable=True,
        )
