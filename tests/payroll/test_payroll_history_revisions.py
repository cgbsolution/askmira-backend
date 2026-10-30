"""Salary revisions from factoHR registers. Pure: no database.

Gates:
  * The same pay with a CTC a few paise apart from month to month (the 2023
    registers' CTC columns are formulas) is ONE revision, carrying the CTC most
    months show; the reason names the others.
  * Any change of a salary part, of the gross, or of the CTC by more than a
    rupee is a new revision.
  * A kept revision dated from the registers no longer says its date is still
    to be corrected.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.payroll import history_import as hi

D = Decimal


def _segment(ctcs: list[str]) -> hi.Segment:
    rate = (D(ctcs[0]), D("11751.00"), D("5876.00"), D("5845.00"), D("0"), D("23472.00"))
    return hi.Segment(
        rate=rate,
        start=hi.date(2023, 9, 1),
        months=[f"2023-{9 + i:02d}" for i in range(len(ctcs))],
        refs=[],
        ctcs=[D(c) for c in ctcs],
    )


def test_paise_of_ctc_are_not_a_revision():
    a = (D("24999.63"), D("11751"), D("5876"), D("5845"), D("0"), D("23472"))
    b = (D("25000.00"), D("11751"), D("5876"), D("5845"), D("0"), D("23472"))
    assert hi._same_pay(a, b)
    assert hi._same_pay(b, a)


def test_a_rupee_more_of_ctc_or_any_other_change_is_a_revision():
    a = (D("25000.00"), D("11751"), D("5876"), D("5845"), D("0"), D("23472"))
    assert not hi._same_pay(a, (D("25001.01"), *a[1:]))
    assert not hi._same_pay(a, (a[0], D("11752"), *a[2:]))
    assert not hi._same_pay(a, (*a[:5], D("23473")))
    assert not hi._same_pay(a, (a[0], a[1], a[2], a[3], D("1"), a[5]))


def test_one_ctc_is_kept_and_the_others_are_named():
    segment = _segment(["24999.63", "25000.00", "25000.00"])
    hi._settle_ctc(segment)
    assert segment.rate[0] == D("25000.00")
    assert segment.notes == [
        "CTC as typed varied by under 1.00 a month between these registers "
        "(24999.63 to 25000.00); 25000.00 kept"
    ]


def test_on_a_tie_the_latest_ctc_is_kept():
    segment = _segment(["24999.63", "25000.00"])
    hi._settle_ctc(segment)
    assert segment.rate[0] == D("25000.00")


def test_one_ctc_throughout_adds_no_note():
    segment = _segment(["25000.00", "25000.00"])
    hi._settle_ctc(segment)
    assert segment.rate[0] == D("25000.00")
    assert segment.notes == []


def test_a_redated_revision_says_where_its_date_came_from():
    old = (
        "From factoHR Employee Detail 28 Oct 26 (yearly figures). Start date is the sheet's WEF; "
        "to be corrected from payroll history."
    )
    new = hi._redated_reason(old, "Start date from the factoHR payroll registers: first paid at "
                             "this rate in Dec 2025.")
    assert new == (
        "From factoHR Employee Detail 28 Oct 26 (yearly figures). Start date from the factoHR "
        "payroll registers: first paid at this rate in Dec 2025."
    )
    assert "to be corrected" not in new
    assert len(hi._redated_reason("x" * 300, "y")) == 255
