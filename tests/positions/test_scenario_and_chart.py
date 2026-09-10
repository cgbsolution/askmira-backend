"""Scenario isolation, vacancy ageing, and a chart with the holes in it."""

from __future__ import annotations

import ast
import inspect
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.positions import scenario as scenario_engine
from app.services.positions.headcount import (
    AssignmentFacts,
    AssignmentType,
    PositionFacts,
    PositionStatus,
    occupancy_on,
)
from app.services.positions.orgchart import (
    OrgChartError,
    build_chart,
    find_cycles,
    flatten,
)
from app.services.positions.scenario import (
    ChangeKind,
    ScenarioChange,
    ScenarioError,
    apply_changes,
    compare,
)
from app.services.positions.vacancy import age_buckets, ageing, budget_gap

TODAY = date(2026, 9, 7)


def _position(
    position_id: str,
    *,
    status: PositionStatus = PositionStatus.FILLED,
    department: str = "Engineering",
    reports_to: str | None = None,
    fte: str = "1.0",
    budget: str | None = "2000000",
    effective_from: date = date(2025, 1, 1),
    **overrides,
) -> PositionFacts:
    return PositionFacts(
        position_id=position_id,
        code=position_id.upper(),
        title="Engineer",
        status=status,
        fte=Decimal(fte),
        department_name=department,
        reports_to_position_id=reports_to,
        budgeted_ctc_max=Decimal(budget) if budget else None,
        effective_from=effective_from,
        **overrides,
    )


def _assignment(position_id: str, employee_id: str, **overrides) -> AssignmentFacts:
    return AssignmentFacts(
        assignment_id=f"{position_id}-{employee_id}",
        position_id=position_id,
        employee_id=employee_id,
        from_date=date(2025, 1, 1),
        employee_label=employee_id.title(),
        **overrides,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Scenario isolation — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_the_scenario_engine_cannot_reach_the_database_at_all() -> None:
    """The checklist gate, stated as something the code can be checked for.

    The guarantee is not "we remember to work on a copy". The module has no
    session and no model imports, so no amount of carelessness inside it can
    reach a row.
    """
    # Checked from the imports rather than from the text. The module's own
    # docstring explains that it has no session, and a substring search on the
    # source finds that sentence — which is the check passing on its own prose
    # rather than on the code.
    tree = ast.parse(inspect.getsource(scenario_engine))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {
        name
        for name in imported
        if name.startswith(("sqlalchemy", "app.models", "app.core.deps", "fastapi"))
    }
    assert not forbidden, (
        f"The scenario engine now imports {sorted(forbidden)}. Its isolation is "
        "structural — the moment it can reach a session, it is only isolated by "
        "somebody remembering to work on a copy."
    )
    assert imported <= {
        "__future__", "enum", "dataclasses", "datetime", "decimal", "typing",
        "app.services.positions.headcount",
    }, f"Unexpected imports in the scenario engine: {sorted(imported)}"


def test_a_scenario_leaves_the_live_positions_untouched() -> None:
    """The checklist gate: fully isolated until commit."""
    positions = [_position("p1"), _position("p2", department="Sales")]
    assignments = [_assignment("p1", "anita")]
    before = [
        (p.position_id, p.status, p.department_name, p.fte) for p in positions
    ]

    apply_changes(
        positions=positions,
        assignments=assignments,
        changes=[
            ScenarioChange(
                kind=ChangeKind.MOVE,
                position_id="p1",
                values={"department_name": "Sales"},
            ),
            ScenarioChange(
                kind=ChangeKind.ABOLISH, position_id="p2", reason="Role no longer needed."
            ),
        ],
        on=TODAY,
    )

    after = [(p.position_id, p.status, p.department_name, p.fte) for p in positions]
    assert before == after, "the input list was mutated"


def test_the_projection_reflects_the_changes() -> None:
    positions = [_position("p1"), _position("p2", department="Sales")]
    result = apply_changes(
        positions=positions,
        assignments=[],
        changes=[
            ScenarioChange(
                kind=ChangeKind.MOVE, position_id="p1", values={"department_name": "Sales"}
            )
        ],
        on=TODAY,
    )
    projected = {p.position_id: p.department_name for p in result.positions}
    assert projected["p1"] == "Sales"


def test_creating_a_position_adds_it_only_to_the_projection() -> None:
    positions = [_position("p1")]
    result = apply_changes(
        positions=positions,
        assignments=[],
        changes=[
            ScenarioChange(
                kind=ChangeKind.CREATE,
                values={
                    "code": "NEW1", "title": "Staff Engineer",
                    "budgeted_ctc_max": "3000000", "department_name": "Engineering",
                },
            )
        ],
        on=TODAY,
    )
    assert len(positions) == 1
    assert len(result.positions) == 2
    created = next(p for p in result.positions if p.code == "NEW1")
    assert created.status is PositionStatus.APPROVED
    # A synthetic id, clearly marked so it cannot be mistaken for a real one.
    assert created.position_id.startswith("scenario:")


def test_a_new_position_needs_a_title_and_a_budget() -> None:
    """A seat with no cost makes the whole growth case look free."""
    with pytest.raises(ScenarioError, match="budget ceiling"):
        apply_changes(
            positions=[],
            assignments=[],
            changes=[
                ScenarioChange(kind=ChangeKind.CREATE, values={"code": "X", "title": "Y"})
            ],
            on=TODAY,
        )
    with pytest.raises(ScenarioError, match="no title"):
        apply_changes(
            positions=[],
            assignments=[],
            changes=[
                ScenarioChange(
                    kind=ChangeKind.CREATE,
                    values={"code": "X", "budgeted_ctc_max": "1"},
                )
            ],
            on=TODAY,
        )


def test_a_move_cannot_quietly_change_the_grade_or_the_budget() -> None:
    """A move that also changed the pay band would hide a pay rise inside a
    reorg."""
    with pytest.raises(ScenarioError, match="hide a pay rise"):
        apply_changes(
            positions=[_position("p1")],
            assignments=[],
            changes=[
                ScenarioChange(
                    kind=ChangeKind.MOVE,
                    position_id="p1",
                    values={"grade": "L7", "budgeted_ctc_max": Decimal("9000000")},
                )
            ],
            on=TODAY,
        )


def test_a_freeze_and_an_abolition_both_need_a_reason() -> None:
    """A decision somebody has to defend later. "Reorg" written at the time is
    worth more than a reconstruction six months on."""
    for kind in (ChangeKind.FREEZE, ChangeKind.ABOLISH):
        with pytest.raises(ScenarioError, match="needs a reason"):
            apply_changes(
                positions=[_position("p1")],
                assignments=[],
                changes=[ScenarioChange(kind=kind, position_id="p1")],
                on=TODAY,
            )


def test_abolishing_an_occupied_seat_is_modelled_and_names_the_person() -> None:
    """A reorg that abolishes an occupied seat is a redundancy — a real plan
    somebody makes on purpose. It must arrive at the review with the person
    named."""
    result = apply_changes(
        positions=[_position("p1")],
        assignments=[_assignment("p1", "anita")],
        changes=[
            ScenarioChange(
                kind=ChangeKind.ABOLISH, position_id="p1", reason="Function outsourced."
            )
        ],
        on=TODAY,
    )
    assert result.warnings
    assert "Anita" in result.warnings[0]
    assert "redundancy" in result.warnings[0]


def test_a_change_to_a_position_that_is_not_in_the_baseline_is_refused() -> None:
    with pytest.raises(ScenarioError, match="not in this scenario's baseline"):
        apply_changes(
            positions=[_position("p1")],
            assignments=[],
            changes=[
                ScenarioChange(kind=ChangeKind.FREEZE, position_id="ghost", reason="x")
            ],
            on=TODAY,
        )


def test_unfreezing_returns_a_seat_to_the_right_open_state() -> None:
    """A seat that was emptied returns to VACANT; one never filled returns to
    APPROVED. They are different things to a recruiter."""
    never_filled = _position("new", status=PositionStatus.FROZEN)
    was_filled = _position(
        "old", status=PositionStatus.FROZEN, vacated_on=date(2026, 5, 1)
    )
    result = apply_changes(
        positions=[never_filled, was_filled],
        assignments=[],
        changes=[
            ScenarioChange(kind=ChangeKind.UNFREEZE, position_id="new"),
            ScenarioChange(kind=ChangeKind.UNFREEZE, position_id="old"),
        ],
        on=TODAY,
    )
    by_id = {p.position_id: p.status for p in result.positions}
    assert by_id["new"] is PositionStatus.APPROVED
    assert by_id["old"] is PositionStatus.VACANT


def test_a_hiring_freeze_shows_up_as_fte_that_cannot_be_filled() -> None:
    """A freeze is "do not fill vacant positions", modelled rather than
    described."""
    positions = [
        _position("p1", status=PositionStatus.VACANT),
        _position("p2", status=PositionStatus.VACANT),
    ]
    result = apply_changes(
        positions=positions,
        assignments=[],
        changes=[
            ScenarioChange(kind=ChangeKind.FREEZE, position_id=p.position_id,
                           reason="Q3 hiring freeze.")
            for p in positions
        ],
        on=TODAY,
    )
    totals = result.totals()["all"]
    assert totals["frozen_fte"] == "2.00"
    assert totals["approved_fte"] == "2.00"


def test_the_comparison_shows_what_the_scenario_changes() -> None:
    """A planning tool that cannot show you what it changed is one nobody signs
    off."""
    positions = [
        _position("p1", department="Engineering"),
        _position("p2", department="Engineering"),
    ]
    live = occupancy_on(positions=positions, assignments=[], on=TODAY)
    result = apply_changes(
        positions=positions,
        assignments=[],
        changes=[
            ScenarioChange(
                kind=ChangeKind.ABOLISH, position_id="p2", reason="Duplicated role."
            )
        ],
        on=TODAY,
    )
    [engineering] = [c for c in compare(live_snapshots=live, result=result)
                     if c.scope == "Engineering"]
    assert engineering.fte_delta == Decimal("-1.0")
    assert engineering.cost_delta == Decimal("-2000000")


def test_changes_apply_in_the_order_they_were_recorded() -> None:
    """Moving a position and then abolishing it is a different plan from doing
    the reverse."""
    positions = [_position("p1", department="Engineering")]
    result = apply_changes(
        positions=positions,
        assignments=[],
        changes=[
            ScenarioChange(
                kind=ChangeKind.MOVE, position_id="p1", values={"department_name": "Sales"}
            ),
            ScenarioChange(kind=ChangeKind.ABOLISH, position_id="p1", reason="Cut."),
        ],
        on=TODAY,
    )
    [projected] = result.positions
    assert projected.department_name == "Sales"
    assert projected.status is PositionStatus.ABOLISHED


# ═══════════════════════════════════════════════════════════════════════════
# Vacancy ageing — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_vacancy_ageing_computes_from_the_vacate_date() -> None:
    """The checklist gate.

    Not from when a requisition was raised. The gap between those two is usually
    where the delay actually is — a seat empty four months with a requisition
    raised last week reads as one week old on the wrong measure.
    """
    positions = [
        _position(
            "p1", status=PositionStatus.VACANT, vacated_on=TODAY - timedelta(days=120)
        )
    ]
    [vacancy] = ageing(occupancy_on(positions=positions, assignments=[], on=TODAY),
                       as_of=TODAY)
    assert vacancy.days_vacant == 120
    assert vacancy.bucket == "3-6 months"


def test_a_seat_never_filled_ages_from_when_it_was_approved() -> None:
    """A new role that has never been recruited for is a different thing from a
    backfill, but both are a seat somebody approved and nobody is in."""
    positions = [
        _position(
            "p1", status=PositionStatus.APPROVED,
            effective_from=TODAY - timedelta(days=45), vacated_on=None,
        )
    ]
    [vacancy] = ageing(occupancy_on(positions=positions, assignments=[], on=TODAY),
                       as_of=TODAY)
    assert vacancy.days_vacant == 45
    assert vacancy.bucket == "1-3 months"


def test_a_filled_seat_is_not_in_the_vacancy_report() -> None:
    positions = [_position("p1")]
    assert ageing(
        occupancy_on(positions=positions, assignments=[_assignment("p1", "a")], on=TODAY),
        as_of=TODAY,
    ) == []


def test_a_covered_seat_is_still_a_vacancy_and_names_who_is_covering() -> None:
    """The cover is often exactly why nobody has noticed how long."""
    positions = [
        _position("p1", status=PositionStatus.VACANT, vacated_on=TODAY - timedelta(days=200))
    ]
    assignments = [_assignment("p1", "priya", assignment_type=AssignmentType.ACTING)]
    [vacancy] = ageing(
        occupancy_on(positions=positions, assignments=assignments, on=TODAY), as_of=TODAY
    )
    assert vacancy.covered_by == ("Priya",)
    assert vacancy.is_stale


def test_a_frozen_seat_is_aged_too() -> None:
    """Knowing a freeze has been in place for seven months is what makes it
    reviewable rather than permanent by inertia."""
    positions = [
        _position("p1", status=PositionStatus.FROZEN, vacated_on=TODAY - timedelta(days=210))
    ]
    [vacancy] = ageing(occupancy_on(positions=positions, assignments=[], on=TODAY),
                       as_of=TODAY)
    assert vacancy.days_vacant == 210
    assert vacancy.bucket == "Over 6 months"


def test_vacancies_are_returned_oldest_first() -> None:
    """The list is read from the top and the top is where the problem is."""
    positions = [
        _position(f"p{days}", status=PositionStatus.VACANT,
                  vacated_on=TODAY - timedelta(days=days))
        for days in (10, 200, 60)
    ]
    vacancies = ageing(occupancy_on(positions=positions, assignments=[], on=TODAY),
                       as_of=TODAY)
    assert [v.days_vacant for v in vacancies] == [200, 60, 10]


def test_the_ageing_buckets_add_up() -> None:
    positions = [
        _position(f"p{days}", status=PositionStatus.VACANT,
                  vacated_on=TODAY - timedelta(days=days))
        for days in (5, 45, 120, 400)
    ]
    vacancies = ageing(occupancy_on(positions=positions, assignments=[], on=TODAY),
                       as_of=TODAY)
    buckets = age_buckets(vacancies)
    assert buckets["Under a month"]["count"] == 1
    assert buckets["1-3 months"]["count"] == 1
    assert buckets["3-6 months"]["count"] == 1
    assert buckets["Over 6 months"]["count"] == 1
    assert sum(b["count"] for b in buckets.values()) == 4


# ═══════════════════════════════════════════════════════════════════════════
# Budget gap
# ═══════════════════════════════════════════════════════════════════════════


def test_the_budget_gap_explains_which_kind_of_under_budget_it_is() -> None:
    """Under budget with vacancies is money not yet spent. Under budget with
    everything filled means the bands are wrong or people are underpaid."""
    positions = [_position("p1", budget="2000000"), _position("p2", budget="2000000")]
    assignments = [_assignment("p1", "a"), _assignment("p2", "b")]
    [gap] = budget_gap(
        occupancy_on(positions=positions, assignments=assignments, on=TODAY),
        actual_cost_by_employee={"a": Decimal("1800000"), "b": Decimal("1700000")},
    )
    assert gap.gap == Decimal("500000")
    assert "every seat filled" in gap.headline
    assert "underpaid" in gap.headline or "below their own band" in gap.headline


def test_a_vacancy_explains_the_under_spend() -> None:
    positions = [_position("p1"), _position("p2", status=PositionStatus.VACANT)]
    [gap] = budget_gap(
        occupancy_on(positions=positions, assignments=[_assignment("p1", "a")], on=TODAY),
        actual_cost_by_employee={"a": Decimal("2000000")},
    )
    assert gap.vacant_fte == Decimal("1.0")
    assert "will be spent once the seats are filled" in gap.headline


def test_the_budget_is_prorated_by_fte() -> None:
    """A half-time seat is half the budget."""
    positions = [_position("p1", fte="0.5", budget="2000000")]
    [gap] = budget_gap(
        occupancy_on(positions=positions, assignments=[], on=TODAY),
        actual_cost_by_employee={},
    )
    assert gap.budgeted_cost == Decimal("1000000.0")


def test_a_filled_seat_with_no_known_salary_is_not_treated_as_free() -> None:
    """A caller without payroll access sees the vacancy picture with the cost
    half honestly blank rather than showing a team as costing nothing."""
    positions = [_position("p1")]
    [gap] = budget_gap(
        occupancy_on(positions=positions, assignments=[_assignment("p1", "a")], on=TODAY),
        actual_cost_by_employee={},
    )
    assert gap.actual_cost == Decimal(0)
    assert gap.filled_fte == Decimal("1.0")
    assert gap.gap == Decimal("2000000")


# ═══════════════════════════════════════════════════════════════════════════
# The org chart — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_the_chart_shows_vacant_seats() -> None:
    """The checklist gate.

    A team of eight showing as six looks understaffed. Showing as six of eight
    tells you it is two short and which two.
    """
    positions = [
        _position("boss"),
        _position("filled", reports_to="boss"),
        _position("empty", reports_to="boss", status=PositionStatus.VACANT),
    ]
    assignments = [_assignment("boss", "anita"), _assignment("filled", "rohit")]

    roots, problems = build_chart(
        occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    )
    assert problems == []
    [root] = roots
    assert root.holder_name == "Anita"
    assert len(root.children) == 2
    assert root.vacant_below() == 1
    vacant = next(c for c in root.children if c.is_vacant)
    assert vacant.code == "EMPTY"
    assert vacant.holder_name is None


def test_the_structure_survives_a_managers_exit() -> None:
    """The practical argument for positions. In an employee chart the reports
    are orphaned; here the seat is simply empty and the tree is unchanged."""
    positions = [
        _position("boss", status=PositionStatus.VACANT, vacated_on=date(2026, 8, 1)),
        _position("r1", reports_to="boss"),
        _position("r2", reports_to="boss"),
    ]
    assignments = [_assignment("r1", "a"), _assignment("r2", "b")]

    roots, problems = build_chart(
        occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    )
    assert problems == []
    [root] = roots
    assert root.is_vacant
    assert len(root.children) == 2, "the team is still under the empty seat"
    assert root.descendants() == 2


def test_a_covering_person_is_shown_as_covering_not_as_the_holder() -> None:
    """"Priya (acting)" over an empty box is the true picture. "Priya" is not."""
    positions = [_position("boss", status=PositionStatus.VACANT)]
    assignments = [
        _assignment("boss", "priya", assignment_type=AssignmentType.ADDITIONAL_CHARGE)
    ]
    [root], _ = build_chart(
        occupancy_on(positions=positions, assignments=assignments, on=TODAY)
    )
    assert root.holder_name is None
    assert root.covering_names == ("Priya",)
    assert root.is_vacant


def test_a_reporting_loop_is_reported_rather_than_recursed_into() -> None:
    """Somebody eventually sets two positions as reporting to each other, usually
    during a reorg with two people editing at once."""
    positions = [
        _position("a", reports_to="b"),
        _position("b", reports_to="a"),
        _position("c"),
    ]
    snapshots = occupancy_on(positions=positions, assignments=[], on=TODAY)

    assert find_cycles(snapshots)
    roots, problems = build_chart(snapshots)
    assert any("report to each other in a loop" in p for p in problems)
    assert [r.code for r in roots] == ["C"], "the rest of the chart still renders"


def test_a_position_reporting_to_one_that_is_gone_becomes_a_root() -> None:
    """Losing a whole subtree because its parent was abolished is the failure
    this guards against."""
    positions = [
        _position("orphan", reports_to="abolished-id"),
        _position("child", reports_to="orphan"),
    ]
    roots, problems = build_chart(
        occupancy_on(positions=positions, assignments=[], on=TODAY)
    )
    assert any("not on the chart" in p for p in problems)
    assert [r.code for r in roots] == ["ORPHAN"]
    assert roots[0].descendants() == 1


def test_an_abolished_seat_is_off_the_chart() -> None:
    positions = [
        _position("live"),
        _position("gone", status=PositionStatus.ABOLISHED),
    ]
    roots, _ = build_chart(occupancy_on(positions=positions, assignments=[], on=TODAY))
    assert [r.code for r in roots] == ["LIVE"]


def test_a_subtree_can_be_requested_on_its_own() -> None:
    positions = [
        _position("ceo"),
        _position("vp", reports_to="ceo"),
        _position("eng", reports_to="vp"),
    ]
    roots, _ = build_chart(
        occupancy_on(positions=positions, assignments=[], on=TODAY),
        root_position_id="vp",
    )
    assert [r.code for r in roots] == ["VP"]
    assert roots[0].descendants() == 1


def test_asking_for_a_root_that_is_not_on_the_chart_says_why() -> None:
    positions = [_position("gone", status=PositionStatus.ABOLISHED)]
    with pytest.raises(OrgChartError, match="abolished"):
        build_chart(
            occupancy_on(positions=positions, assignments=[], on=TODAY),
            root_position_id="gone",
        )


def test_the_chart_flattens_with_its_depth_intact() -> None:
    """So the structure is not lost the moment somebody wants it in a
    spreadsheet."""
    positions = [
        _position("ceo"),
        _position("vp", reports_to="ceo"),
        _position("eng", reports_to="vp"),
    ]
    roots, _ = build_chart(occupancy_on(positions=positions, assignments=[], on=TODAY))
    rows = flatten(roots)
    assert [r["depth"] for r in rows] == [0, 1, 2]
    assert rows[-1]["path"] == "CEO / VP / ENG"
