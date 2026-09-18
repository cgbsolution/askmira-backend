"""The settlement statement template (slice 13 §4)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.services.documents.letter_merge import MergeContext, extract_tokens, render_template
from app.services.exit_workflow import statement


def _values(net: str, lines=None) -> dict[str, str]:
    return statement.manual_values(
        lines=lines
        if lines is not None
        else [
            ("GRATUITY", Decimal("150000"), "6 completed years at 15 days per year."),
            ("LOAN", Decimal("47500.5"), "Outstanding principal on the staff loan."),
        ],
        total_earnings=Decimal("150000"),
        total_recoveries=Decimal("47500.50"),
        net_amount=Decimal(net),
        separation="Resignation; resigned 2026-06-01",
        notes=[],
    )


def test_every_template_token_has_a_value() -> None:
    template = statement.seed_template(uuid.uuid4())
    manual_tokens = {t.split(".", 1)[1] for t in extract_tokens(template.body_html)
                     if t.startswith("manual.")}
    assert manual_tokens == set(_values("102499.50"))
    declared = {field.token for field in template.fields}
    assert set(extract_tokens(template.body_html)) <= declared


def test_every_head_appears_with_its_basis_or_not_applicable() -> None:
    values = _values("102499.50")
    assert values["gratuity_amount"] == "1,50,000.00"
    assert values["gratuity_basis"].startswith("6 completed years")
    assert values["loan_amount"] == "47,500.50"
    assert values["assets_amount"] == "—"
    assert values["assets_basis"] == statement.NOT_APPLICABLE
    assert values["net_label"] == "Amount payable to the employee"


def test_a_negative_net_is_worded_as_a_demand_not_floored() -> None:
    values = _values("-12000.00")
    assert values["net_label"] == "Amount due from the employee to the company"
    assert values["net_amount"] == "12,000.00"


def test_the_statement_renders_and_escapes_values() -> None:
    template = statement.seed_template(uuid.uuid4())
    values = _values("1.00", lines=[("OTHER_RECOVERY", Decimal(1), "<script>x</script>")])
    html = render_template(
        body_html=template.body_html,
        context=MergeContext(
            as_of=__import__("datetime").date(2026, 9, 1),
            employee={"employee_code": "ACM001", "first_name": "Anita", "last_name": "S",
                      "display_name": None},
            salary={},
            tenant={"name": "Acme", "slug": "acme"},
            manual=values,
        ),
        required_tokens={field.token for field in template.fields if field.required},
    )
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "Full and Final Settlement Statement" in html
