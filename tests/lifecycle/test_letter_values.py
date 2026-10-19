"""What a lifecycle event's letter is given to say about a pay change."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services.lifecycle.service import _letter_manual_values


def _event(**overrides):
    base = {
        "new_values": {"grade": "P3", "designation": "Senior Accounts Executive"},
        "effective_from": date(2026, 8, 1),
        "reason": "Took over GST and TDS filings.",
        "new_ctc_annual": Decimal("720000"),
    }
    return SimpleNamespace(**{**base, **overrides})


def _values(row, **kwargs):
    # No department, location or manager in the change, so the session is unused.
    return asyncio.run(_letter_manual_values(None, None, row, **kwargs))


def test_a_promotion_with_a_raise_gives_the_increment_letter_its_required_values() -> None:
    """The standard increment letter requires `manual.previous_ctc` and
    `manual.increase_percentage`; without them effecting the promotion rolls back."""
    values = _values(_event(), previous_ctc=Decimal("600000"))
    assert values["previous_ctc"] == "600,000.00"
    assert values["increase_percentage"] == "20.00"
    assert values["new_ctc_annual"] == Decimal("720000")
    assert values["designation"] == "Senior Accounts Executive"


def test_no_previous_salary_means_no_invented_comparison() -> None:
    values = _values(_event())
    assert "previous_ctc" not in values
    assert "increase_percentage" not in values


def test_a_change_without_pay_quotes_no_ctc() -> None:
    values = _values(_event(new_ctc_annual=None), previous_ctc=Decimal("600000"))
    assert "new_ctc_annual" not in values
    assert "previous_ctc" not in values
