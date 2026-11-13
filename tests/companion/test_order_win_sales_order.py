"""An order win's draft sales order for Finance: one line per role, billing to the paisa."""

from decimal import Decimal

from app.services.companion.order_win import business_key, sales_order_params


def _plan() -> dict:
    return {
        "po_number": "KAC-PO-2291",
        "deal_id": "d-1",
        "deal_title": "Kaveri data platform",
        "account": "Kaveri Agro Ltd",
        "start_date": "2026-12-01",
        "months": 6,
        "roles": [
            {"title": "Data engineer", "count": 3, "allocation": "100", "bill_rate": "210000.00"},
            {"title": "Project manager", "count": 1, "allocation": "50", "bill_rate": "333333.33"},
        ],
    }


def test_each_role_is_a_line_of_months_at_its_monthly_billing():
    params = sales_order_params(_plan())
    assert params["key"] == business_key("KAC-PO-2291")
    assert params["customer"] == "Kaveri Agro Ltd"
    first, second = params["lines"]
    assert (first["name"], first["quantity"], first["rate"]) == ("Data engineer", "6", "630000.00")
    # 50% of 3,33,333.33 rounds half up to the paisa.
    assert (second["quantity"], second["rate"]) == ("6", "166666.67")
    assert "1 person at 50%" in second["description"]
    assert "6 months from 01 Dec 2026" in first["description"]


def test_the_order_total_matches_the_teams_billing():
    params = sales_order_params(_plan())
    total = sum(Decimal(ln["quantity"]) * Decimal(ln["rate"]) for ln in params["lines"])
    assert total == Decimal("630000.00") * 6 + Decimal("166666.67") * 6
    assert all(isinstance(ln["rate"], str) for ln in params["lines"])  # never a float on the wire
