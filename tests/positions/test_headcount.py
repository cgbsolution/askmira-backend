"""Counting seats and people without counting anybody twice."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.positions.headcount import (
    AssignmentFacts,
    AssignmentType,
    HeadcountError,
    PositionFacts,
    PositionStatus,
    aggregate,
    occupancy_on,
    people_holding_more_than_one_position,
)

TODAY = date(2026, 9, 7)


def _position(
    position_id: str,
    *,
    status: PositionStatus = PositionStatus.FILLED,
    fte: str = "1.0",
    department: str = "Engineering",
    **overrides,
) -> PositionFacts:
    return PositionFacts(
        position_id=position_id,
        code=position_id.upper(),
        title="Engineer",
        status=status,
        fte=Decimal(fte),
        department_name=department,
        **overrides,
    )


def _assignment(
    position_id: str,
    employee_id: str,
    *,
    kind: AssignmentType = AssignmentType.PRIMARY,
    from_date: date = date(2026, 1, 1),
    to_date: date | None = None,
) -> AssignmentFacts:
    return AssignmentFacts(
        assignment_id=f"{position_id}-{employee_id}",
        position_id=position_id,
        employee_id=employee_id,
        from_date=from_date,
        to_date=to_date,
        assignment_type=kind,
        employee_label=employee_id.title(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Additional charge — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_additional_charge_does_not_count_toward_headcount() -> None:
    """The checklist gate.

    One human being doing two jobs. A headcount that counts them twice reports a
    team as fully staffed at exactly the moment it is short a manager, which is
    the one moment the number is being looked at.
    """
    positions = [
        _position("p1", status=PositionStatus.FILLED),
        _position("p2", status=PositionStatus.VACANT),
    ]
    assignments = [
        _assignment("p1", "priya"),
        # Priya also holds the vacant manager seat while it is backfilled.
        _assignment("p2", "priya", kind=AssignmentType.ADDITIONAL_CHARGE),
    ]

    snapshots = occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    totals = aggregate(snapshots)["all"]

    assert totals.filled_fte == Decimal("1.0"), "one seat filled, not two"
    assert totals.vacant_fte == Decimal("1.0"), "the covered seat is still vacant"
    assert totals.people == 1, "Priya is one person"
    assert totals.covered_only == 1


def test_an_acting_assignment_does_not_fill_the_seat_either() -> None:
    positions = [_position("p1", status=PositionStatus.VACANT)]
    assignments = [_assignment("p1", "rohit", kind=AssignmentType.ACTING)]

    [snapshot] = occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    assert not snapshot.is_occupied
    assert snapshot.covered_only
    assert snapshot.primary_holder is None
    assert snapshot.as_dict()["covering"] == ["Rohit"]


def test_a_covered_seat_is_reported_separately_from_a_plainly_empty_one() -> None:
    """The state a headcount most needs to distinguish and most often does not."""
    positions = [
        _position("covered", status=PositionStatus.VACANT),
        _position("empty", status=PositionStatus.VACANT),
    ]
    assignments = [_assignment("covered", "anita", kind=AssignmentType.ACTING)]

    snapshots = occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    totals = aggregate(snapshots)["all"]
    assert totals.vacant_fte == Decimal("2.0")
    assert totals.covered_only == 1


def test_one_person_holding_two_primary_seats_counts_as_one_person() -> None:
    """The same double-count arriving by a different route. `people` is a set,
    not a counter."""
    positions = [_position("p1"), _position("p2")]
    assignments = [_assignment("p1", "vikram"), _assignment("p2", "vikram")]

    totals = aggregate(
        occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    )["all"]
    assert totals.people == 1
    assert totals.filled_fte == Decimal("2.0"), "two seats are filled, by one person"


def test_people_holding_two_seats_are_surfaced_by_name() -> None:
    """Additional charge is invisible in a headcount by design. Somebody who has
    been holding a second position for five months is a backfill nobody has got
    round to."""
    positions = [_position("p1"), _position("p2"), _position("p3")]
    assignments = [
        _assignment("p1", "priya"),
        _assignment("p2", "priya", kind=AssignmentType.ADDITIONAL_CHARGE),
        _assignment("p3", "rohit"),
    ]
    stretched = people_holding_more_than_one_position(
        occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    )
    assert stretched == {"priya": ["P1", "P2"]}


# ═══════════════════════════════════════════════════════════════════════════
# FTE — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_fte_fractions_aggregate_correctly() -> None:
    """The checklist gate.

    Two people job-sharing are two employees and one FTE. Counting rows instead
    would overstate capacity by however much part-time work the client does.
    """
    positions = [
        _position("full", fte="1.0"),
        _position("half1", fte="0.5"),
        _position("half2", fte="0.5"),
        _position("quarter", fte="0.25", status=PositionStatus.VACANT),
    ]
    assignments = [
        _assignment("full", "a"),
        _assignment("half1", "b"),
        _assignment("half2", "c"),
    ]

    totals = aggregate(
        occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    )["all"]
    assert totals.approved_fte == Decimal("2.25")
    assert totals.filled_fte == Decimal("2.0")
    assert totals.vacant_fte == Decimal("0.25")
    assert totals.people == 3, "three people in two FTE of part-time seats plus one"
    assert totals.positions == 4


def test_fte_arithmetic_stays_exact() -> None:
    """Three thirds are one, not 0.9999999999999999. FTE lands in a budget
    number and a budget number computed in floating point disagrees with
    finance's."""
    positions = [_position(f"p{i}", fte="0.33") for i in range(3)]
    totals = aggregate(occupancy_on(positions=positions, assignments=[], on=TODAY))["all"]
    assert totals.approved_fte == Decimal("0.99")
    assert isinstance(totals.approved_fte, Decimal)


def test_a_frozen_seat_is_counted_as_approved_and_as_frozen() -> None:
    """A freeze keeps the seat and its budget. It is still part of what the
    organisation is approved for."""
    positions = [
        _position("live", status=PositionStatus.VACANT),
        _position("frozen", status=PositionStatus.FROZEN),
    ]
    totals = aggregate(occupancy_on(positions=positions, assignments=[], on=TODAY))["all"]
    assert totals.approved_fte == Decimal("2.0")
    assert totals.frozen_fte == Decimal("1.0")


def test_an_abolished_seat_is_not_counted_at_all() -> None:
    """An abolition takes the seat and its budget away."""
    positions = [
        _position("live"),
        _position("gone", status=PositionStatus.ABOLISHED),
    ]
    snapshots = occupancy_on(positions=positions, assignments=[], on=TODAY)
    assert len(snapshots) == 1
    assert aggregate(snapshots)["all"].positions == 1


# ═══════════════════════════════════════════════════════════════════════════
# Point in time — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_querying_a_past_date_returns_the_historical_occupant() -> None:
    """The checklist gate.

    Most of the reason to model positions: people move and the seats do not.
    """
    positions = [_position("p1")]
    assignments = [
        _assignment("p1", "anita", from_date=date(2025, 1, 1), to_date=date(2026, 3, 31)),
        _assignment("p1", "rohit", from_date=date(2026, 4, 1)),
    ]

    march = occupancy_on(positions=positions, assignments=assignments, on=date(2026, 3, 1))
    assert march[0].primary_holder.employee_label == "Anita"

    september = occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    assert september[0].primary_holder.employee_label == "Rohit"


def test_the_last_day_of_an_assignment_is_included() -> None:
    """That is how an HR system records "their last day was the 31st". Treating
    it as exclusive shows every leaver as gone a day early."""
    positions = [_position("p1")]
    assignments = [
        _assignment("p1", "anita", from_date=date(2026, 1, 1), to_date=date(2026, 3, 31))
    ]

    on_last_day = occupancy_on(
        positions=positions, assignments=assignments, on=date(2026, 3, 31)
    )
    assert on_last_day[0].is_occupied

    next_day = occupancy_on(
        positions=positions, assignments=assignments, on=date(2026, 4, 1)
    )
    assert not next_day[0].is_occupied


def test_the_first_day_of_an_assignment_is_included() -> None:
    positions = [_position("p1")]
    assignments = [_assignment("p1", "rohit", from_date=date(2026, 4, 1))]
    assert occupancy_on(
        positions=positions, assignments=assignments, on=date(2026, 4, 1)
    )[0].is_occupied
    assert not occupancy_on(
        positions=positions, assignments=assignments, on=date(2026, 3, 31)
    )[0].is_occupied


def test_a_seat_that_does_not_exist_yet_is_not_on_the_chart() -> None:
    """A position created for next quarter should not appear in this quarter's
    numbers."""
    positions = [
        _position("future", effective_from=date(2027, 1, 1), status=PositionStatus.APPROVED)
    ]
    assert occupancy_on(positions=positions, assignments=[], on=TODAY) == []
    assert len(occupancy_on(positions=positions, assignments=[], on=date(2027, 2, 1))) == 1


def test_a_seat_abolished_last_year_is_not_in_this_years_numbers() -> None:
    positions = [
        _position(
            "gone",
            status=PositionStatus.ABOLISHED,
            effective_from=date(2024, 1, 1),
            effective_to=date(2025, 12, 31),
        )
    ]
    assert occupancy_on(positions=positions, assignments=[], on=TODAY) == []


def test_the_primary_holder_is_not_order_dependent() -> None:
    """A seat can have a primary and a cover at the same time — somebody on
    additional charge while the primary is on long leave — and reporting the
    first row found would depend on insertion order."""
    positions = [_position("p1")]
    assignments = [
        _assignment("p1", "cover", kind=AssignmentType.ACTING),
        _assignment("p1", "real"),
    ]
    [snapshot] = occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    assert snapshot.primary_holder.employee_id == "real"

    reversed_order = occupancy_on(
        positions=positions, assignments=list(reversed(assignments)), on=TODAY
    )
    assert reversed_order[0].primary_holder.employee_id == "real"


# ═══════════════════════════════════════════════════════════════════════════
# Grouping
# ═══════════════════════════════════════════════════════════════════════════


def test_totals_group_by_department() -> None:
    positions = [
        _position("e1", department="Engineering"),
        _position("e2", department="Engineering", status=PositionStatus.VACANT),
        _position("s1", department="Sales"),
    ]
    assignments = [_assignment("e1", "a"), _assignment("s1", "b")]

    grouped = aggregate(
        occupancy_on(positions=positions, assignments=assignments, on=TODAY),
        by="department",
    )
    assert grouped["Engineering"].approved_fte == Decimal("2.0")
    assert grouped["Engineering"].vacant_fte == Decimal("1.0")
    assert grouped["Sales"].filled_fte == Decimal("1.0")


def test_a_position_with_no_department_gets_a_named_bucket_not_a_blank() -> None:
    """A blank key in a chart legend is a bug report waiting to happen."""
    positions = [_position("p1", department="")]
    grouped = aggregate(
        occupancy_on(positions=positions, assignments=[], on=TODAY), by="department"
    )
    assert "(no department)" in grouped


def test_grouping_by_something_positions_do_not_have_is_refused() -> None:
    with pytest.raises(HeadcountError, match="not something positions"):
        aggregate([], by="favourite_colour")


def test_an_empty_organisation_produces_no_buckets_rather_than_zeroes() -> None:
    assert aggregate([]) == {}
