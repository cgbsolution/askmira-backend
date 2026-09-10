"""Rule-set fixtures for the tax engine.

═══════════════════════════════════════════════════════════════════════════════
THESE VALUES ARE TEST FIXTURES, NOT AN AUTHORITY ON INDIAN TAX
═══════════════════════════════════════════════════════════════════════════════

They resemble published Indian rules closely enough for the tests to be
meaningful, and they are deliberately *not* a source of truth. Production
values come from the Compliance SME through an approved `PayrollRuleSet`.

Several tests use obviously synthetic numbers — round slabs, a 10% cess — to
prove the engine reads configuration rather than recognising familiar figures.
If a test passes only with realistic values, the engine has a constant in it.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def rules() -> dict[str, Any]:
    """A rule set shaped like one the SME would supply."""
    return {
        "regimes": {
            "old": {
                "slabs": [
                    {"up_to": 250000, "rate": 0},
                    {"up_to": 500000, "rate": 5},
                    {"up_to": 1000000, "rate": 20},
                    {"up_to": None, "rate": 30},
                ],
                "standard_deduction": 50000,
                "rebate": {"income_limit": 500000, "max_rebate": 12500},
                "deductions_allowed": [
                    "80C", "80D", "80CCD1B", "80CCD2", "24B", "HRA", "PT",
                ],
            },
            "new": {
                "slabs": [
                    {"up_to": 300000, "rate": 0},
                    {"up_to": 700000, "rate": 5},
                    {"up_to": 1000000, "rate": 10},
                    {"up_to": 1200000, "rate": 15},
                    {"up_to": 1500000, "rate": 20},
                    {"up_to": None, "rate": 30},
                ],
                "standard_deduction": 75000,
                "rebate": {"income_limit": 700000, "max_rebate": 25000},
                # The new regime allows far fewer. That list is configuration,
                # so a Budget widening it is a rule-set edit.
                "deductions_allowed": ["80CCD2"],
            },
        },
        "section_limits": {
            "80C": {"ceiling": 150000},
            "80D": {
                "ceiling_by_age": [
                    {"max_age": 59, "ceiling": 25000},
                    {"max_age": None, "ceiling": 50000},
                ]
            },
            "80CCD1B": {"ceiling": 50000},
            "24B": {"ceiling": 200000},
        },
        "hra": {
            "metro_percent": 50,
            "non_metro_percent": 40,
            "rent_less_basic_percent": 10,
        },
        "landlord_pan_threshold_annual": 100000,
        "cess_percent": 4,
        "surcharge_bands": [
            {"above": 5000000, "up_to": 10000000, "rate": 10},
            {"above": 10000000, "up_to": None, "rate": 15},
        ],
        "regime_switches_per_year": 1,
        "financial_year_start_month": 4,
    }


@pytest.fixture
def synthetic_rules() -> dict[str, Any]:
    """Deliberately unrealistic values.

    Nothing here resembles a real Indian tax rule. A computation that produces
    the right answer against these is reading configuration; one that produces
    a familiar-looking answer has a constant in it.
    """
    return {
        "regimes": {
            "old": {
                "slabs": [
                    {"up_to": 100000, "rate": 0},
                    {"up_to": None, "rate": 50},
                ],
                "standard_deduction": 11111,
                "deductions_allowed": ["80C", "HRA"],
            },
            "new": {
                "slabs": [{"up_to": None, "rate": 10}],
                "standard_deduction": 0,
                "deductions_allowed": [],
            },
        },
        "section_limits": {"80C": {"ceiling": 7777}},
        "hra": {
            "metro_percent": 90,
            "non_metro_percent": 80,
            "rent_less_basic_percent": 70,
        },
        "cess_percent": 10,
        "landlord_pan_threshold_annual": 1,
    }
