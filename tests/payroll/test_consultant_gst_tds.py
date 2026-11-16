"""What a consultant's record accepts for GST and TDS."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.employee import EmploymentType
from app.schemas.employee import EmployeeCreate, consultant_billing_problem

# ── What the record accepts ─────────────────────────────────────────────────

C = EmploymentType.CONSULTANT


@pytest.mark.parametrize(
    ("kind", "amount", "ok"),
    [
        (C, Decimal("1000"), True),
        (C, None, True),
        (EmploymentType.FULL_TIME, Decimal("1000"), False),  # only a consultant has a GST amount
        (EmploymentType.FULL_TIME, None, True),
    ],
)
def test_consultant_billing_rules(kind, amount, ok) -> None:
    problem = consultant_billing_problem(
        kind,
        gst_applicable=False,
        gst_rate=None,
        gstin=None,
        tds_section=None,
        route=None,
        contact_name=None,
        contact_email=None,
        gst_amount=amount,
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
