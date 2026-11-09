"""A new lead's deal name: Saleinx shows it after the company's name, so the company (or a
name that is only the contact's) never goes into it twice."""

import pytest

from app.services.companion.sales_tools import _deal_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Kestrel Foods - Priya Nair", "SafeOps360"),
        ("Kestrel Foods – supplier portal", "supplier portal"),  # noqa: RUF001
        ("SafeOps360 for Kestrel Foods", "SafeOps360"),
        ("Supplier portal (Kestrel Foods)", "Supplier portal"),
        ("kestrel foods", "SafeOps360"),
        ("Kestrel Foods opportunity", "SafeOps360"),
        ("Priya Nair", "SafeOps360"),
        ("", "SafeOps360"),
        ("Safety audits for 3 plants", "Safety audits for 3 plants"),
        ("Kestrel Foodservice upgrade", "Kestrel Foodservice upgrade"),
    ],
)
def test_the_company_and_the_contact_stay_out_of_the_deal_name(name: str, expected: str) -> None:
    assert _deal_name(name, "Kestrel Foods", "Priya Nair", ["SafeOps360"]) == expected


def test_with_nothing_to_go_on_the_deal_is_a_new_opportunity() -> None:
    assert _deal_name("Kestrel Foods", "Kestrel Foods", None, []) == "New opportunity"
    assert _deal_name("", "Kestrel Foods", None, ["AnytimeHire", "Recruitment"]) == (
        "AnytimeHire, Recruitment"
    )
