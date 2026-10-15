"""The arithmetic behind the people-analytics signals, without a database."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models.employee import Employee
from app.services.reporting.analytics import (
    _months_between,
    _pay_revision_signal,
    bradford_factor,
    count_spells,
    month_ends,
)


def d(day: int, month: int = 3) -> date:
    return date(2026, month, day)


def test_consecutive_absences_are_one_spell() -> None:
    assert count_spells([d(2), d(3), d(4)], worked=[]) == 1


def test_a_worked_day_between_absences_starts_a_new_spell() -> None:
    assert count_spells([d(2), d(4)], worked=[d(3)]) == 2


def test_a_weekend_between_absences_does_not_break_the_spell() -> None:
    # Friday and Monday off, nothing worked in between: one spell, as a long weekend is.
    assert count_spells([d(6), d(9)], worked=[d(5), d(10)]) == 1


def test_no_absence_is_no_spell() -> None:
    assert count_spells([], worked=[d(1)]) == 0


def test_bradford_punishes_frequency_over_length() -> None:
    # Ten one-day absences against one ten-day absence.
    assert bradford_factor(10, Decimal(10)) == Decimal(1000)
    assert bradford_factor(1, Decimal(10)) == Decimal(10)


def test_bradford_counts_half_days() -> None:
    assert bradford_factor(3, Decimal("2.5")) == Decimal(23)  # 22.5 rounds half up


def test_months_between_counts_whole_months() -> None:
    assert _months_between(date(2025, 4, 15), date(2026, 10, 14)) == 17
    assert _months_between(date(2025, 4, 15), date(2026, 10, 15)) == 18


def test_pay_signal_says_since_joining_when_never_revised() -> None:
    person = Employee(date_of_joining=date(2024, 1, 10))
    signal = _pay_revision_signal(person, date(2024, 1, 10), date(2026, 10, 13))
    assert signal is not None and "since joining" in signal["evidence"][0]


def test_pay_signal_quiet_after_a_recent_revision() -> None:
    person = Employee(date_of_joining=date(2020, 1, 10))
    assert _pay_revision_signal(person, date(2026, 4, 1), date(2026, 10, 13)) is None


def test_month_ends_cross_the_year() -> None:
    assert month_ends(date(2026, 2, 10), 3) == [
        date(2025, 12, 31),
        date(2026, 1, 31),
        date(2026, 2, 28),
    ]


def test_attrition_signals_are_facts_with_their_rule() -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.services.reporting.analytics import SignalRules, _attrition_signals, _Facts

    today = date(2026, 10, 13)
    person = Employee(date_of_joining=date(2021, 1, 4))
    facts = _Facts(
        manager_changed_on=date(2026, 9, 1),
        scores=[
            (datetime(2025, 4, 1, tzinfo=UTC), Decimal("4.0")),
            (datetime(2026, 4, 1, tzinfo=UTC), Decimal("2.5")),
        ],
    )
    kinds = {
        s["kind"]: s
        for s in _attrition_signals(
            person, facts, SignalRules(), [date(2026, 8, 1), date(2026, 9, 2)], today
        )
    }
    assert set(kinds) == {"team_exits", "manager_change", "rating_drop", "no_role_change"}
    assert "4 then 2.5" in kinds["rating_drop"]["evidence"][0]
    assert "since joining" in kinds["no_role_change"]["evidence"][0]

    # Zero switches a signal off.
    quiet = SignalRules(manager_change_days=0, rating_drop=Decimal(0), no_role_change_months=0)
    assert _attrition_signals(person, facts, quiet, None, today) == []
