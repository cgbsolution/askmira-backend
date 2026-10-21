"""Slice 23 gates that need a real database.

The engines are proved in `tests/positions/`. What only a database settles: that
an exit really does leave the seat behind, that a committed scenario changes live
rows and an uncommitted one does not, that a requisition without a position is
flagged at approval, and that the point-in-time query survives a round trip.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.hiring import Requisition, RequisitionStatus
from app.models.offer import ApprovalChainRule, ApprovalScope
from app.models.org import Department, LegalEntity
from app.models.position import (
    AssignmentType,
    Position,
    PositionAssignment,
    PositionScenario,
    PositionStatus,
    ScenarioChange,
    ScenarioStatus,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services.hiring import requisitions as requisition_service
from app.services.positions import service as position_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

TODAY = date(2026, 9, 7)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


async def _department(session: AsyncSession, tenant: Tenant, name: str) -> Department:
    department = Department(tenant_id=tenant.id, name=name, code=name[:3].upper())
    session.add(department)
    await session.commit()
    await session.refresh(department)
    return department


async def _position(
    session: AsyncSession,
    tenant: Tenant,
    code: str,
    *,
    status: PositionStatus = PositionStatus.APPROVED,
    department_id=None,
    reports_to=None,
    fte: str = "1.0",
    budget: str = "2000000",
    effective_from: date = date(2025, 1, 1),
    vacated_on: date | None = None,
) -> Position:
    position = Position(
        tenant_id=tenant.id,
        code=code,
        title=f"{code} role",
        status=status,
        department_id=department_id,
        reports_to_position_id=reports_to,
        fte=Decimal(fte),
        budgeted_ctc_max=Decimal(budget),
        effective_from=effective_from,
        vacated_on=vacated_on,
    )
    session.add(position)
    await session.commit()
    await session.refresh(position)
    return position


async def _employee(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, code: str
) -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code,
        last_name="Person",
        display_name=f"{code} Person",
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=legal_entity.id,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


# ═══════════════════════════════════════════════════════════════════════════
# Occupancy — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_point_in_time_query_returns_the_historical_occupant(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate, through a real round trip."""
    position = await _position(session, tenant, "P1")
    anita = await _employee(session, tenant, legal_entity, "ANITA")
    rohit = await _employee(session, tenant, legal_entity, "ROHIT")

    await position_service.assign(
        session,
        hr_user,
        position_id=position.id,
        employee_id=anita.id,
        from_date=date(2025, 1, 1),
    )
    await session.commit()
    await position_service.vacate(session, hr_user, position_id=position.id, on=date(2026, 3, 31))
    await session.commit()
    await position_service.assign(
        session,
        hr_user,
        position_id=position.id,
        employee_id=rohit.id,
        from_date=date(2026, 4, 1),
    )
    await session.commit()

    march = await position_service.snapshots_on(session, hr_user, on=date(2026, 3, 1))
    assert march[0].primary_holder.employee_label == "ANITA Person"

    today = await position_service.snapshots_on(session, hr_user, on=TODAY)
    assert today[0].primary_holder.employee_label == "ROHIT Person"

    between = await position_service.snapshots_on(session, hr_user, on=date(2026, 3, 31))
    assert between[0].primary_holder.employee_label == "ANITA Person", "the last day is inclusive"


async def test_an_exit_vacates_the_position_rather_than_deleting_it(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate, and the whole point of the exit path.

    Deleting the seat is how an organisation loses track of what it is meant to
    be staffed at, and how a backfill becomes a new approval nobody remembers
    granting.
    """
    position = await _position(session, tenant, "P1")
    person = await _employee(session, tenant, legal_entity, "LEAVER")
    await position_service.assign(
        session,
        hr_user,
        position_id=position.id,
        employee_id=person.id,
        from_date=date(2025, 1, 1),
    )
    await session.commit()

    result = await position_service.vacate(
        session,
        hr_user,
        position_id=position.id,
        on=date(2026, 8, 31),
        reason="Resigned.",
    )
    await session.commit()

    still_there = await session.get(Position, position.id)
    assert still_there is not None, "the seat survives"
    assert still_there.status is PositionStatus.VACANT
    assert still_there.vacated_on == date(2026, 8, 31)
    assert still_there.budgeted_ctc_max == Decimal("2000000.00"), "still budgeted"
    assert "still exists and is still budgeted" in result["note"]

    # And the assignment is closed rather than removed, so the history holds.
    assignment = await session.scalar(
        select(PositionAssignment).where(PositionAssignment.position_id == position.id)
    )
    assert assignment is not None
    assert assignment.to_date == date(2026, 8, 31)


async def test_a_second_primary_assignment_to_an_occupied_seat_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Two people cannot both hold one seat, and letting them would double the
    headcount."""
    position = await _position(session, tenant, "P1")
    first = await _employee(session, tenant, legal_entity, "FIRST")
    second = await _employee(session, tenant, legal_entity, "SECOND")

    await position_service.assign(
        session,
        hr_user,
        position_id=position.id,
        employee_id=first.id,
        from_date=date(2026, 1, 1),
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await position_service.assign(
            session,
            hr_user,
            position_id=position.id,
            employee_id=second.id,
            from_date=date(2026, 6, 1),
        )
    assert "double the headcount" in exc.value.detail


async def test_additional_charge_is_allowed_alongside_a_primary(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """That is exactly what it is for. It does not count toward headcount."""
    position = await _position(session, tenant, "P1")
    holder = await _employee(session, tenant, legal_entity, "HOLDER")
    cover = await _employee(session, tenant, legal_entity, "COVER")

    await position_service.assign(
        session,
        hr_user,
        position_id=position.id,
        employee_id=holder.id,
        from_date=date(2026, 1, 1),
    )
    await session.commit()
    await position_service.assign(
        session,
        hr_user,
        position_id=position.id,
        employee_id=cover.id,
        from_date=date(2026, 6, 1),
        assignment_type=AssignmentType.ACTING,
    )
    await session.commit()

    snapshots = await position_service.snapshots_on(session, hr_user, on=TODAY)
    from app.services.positions.headcount import aggregate

    totals = aggregate(snapshots)["all"]
    assert totals.filled_fte == Decimal("1.0")
    assert totals.people == 1


async def test_a_frozen_seat_cannot_be_quietly_filled(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Filling a frozen seat quietly is how a hiring freeze stops meaning
    anything."""
    position = await _position(session, tenant, "FROZEN", status=PositionStatus.FROZEN)
    person = await _employee(session, tenant, legal_entity, "SOMEONE")
    with pytest.raises(HTTPException) as exc:
        await position_service.assign(
            session,
            hr_user,
            position_id=position.id,
            employee_id=person.id,
            from_date=TODAY,
        )
    assert "Unfreeze it first" in exc.value.detail


async def test_an_abolished_seat_cannot_be_filled(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    position = await _position(session, tenant, "GONE", status=PositionStatus.ABOLISHED)
    person = await _employee(session, tenant, legal_entity, "SOMEONE")
    with pytest.raises(HTTPException) as exc:
        await position_service.assign(
            session,
            hr_user,
            position_id=position.id,
            employee_id=person.id,
            from_date=TODAY,
        )
    assert "a seat that exists" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Views
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_budget_view_separates_approved_filled_and_vacant(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    engineering = await _department(session, tenant, "Engineering")
    filled = await _position(session, tenant, "F1", department_id=engineering.id)
    await _position(
        session,
        tenant,
        "V1",
        department_id=engineering.id,
        status=PositionStatus.VACANT,
    )
    await _position(
        session,
        tenant,
        "H1",
        department_id=engineering.id,
        fte="0.5",
        status=PositionStatus.VACANT,
    )
    person = await _employee(session, tenant, legal_entity, "PERSON")
    await position_service.assign(
        session,
        hr_user,
        position_id=filled.id,
        employee_id=person.id,
        from_date=date(2026, 1, 1),
    )
    await session.commit()

    view = await position_service.budget_view(session, hr_user, on=TODAY)
    totals = view["totals"]["Engineering"]
    assert totals["approved_fte"] == "2.50"
    assert totals["filled_fte"] == "1.00"
    assert totals["vacant_fte"] == "1.50"
    assert view["cost"] is None
    assert "salary leak" in view["cost_note"]


async def test_the_cost_view_needs_payroll_access_to_be_requested(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """A cost column of zeroes reads as "this team is free", which is worse than
    a refusal. The service returns real numbers; the router does the gating."""
    from app.models.payroll import (
        CalculationType,
        ComponentType,
        SalaryComponent,
        SalaryStructure,
        SalaryStructureComponent,
    )
    from app.schemas.payroll import EmployeeSalaryCreate
    from app.services.payroll import service as payroll_service

    basic = SalaryComponent(
        tenant_id=tenant.id,
        code="BASIC",
        name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="CTC",
        value=Decimal(40),
        display_order=1,
    )
    special = SalaryComponent(
        tenant_id=tenant.id,
        code="SPECIAL",
        name="Special",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE,
        display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()
    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard",
        effective_from=date(2025, 1, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for index, component in enumerate((basic, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id,
                component_id=component.id,
                component=component,
                display_order=index,
            )
        )
    session.add(structure)
    await session.commit()

    engineering = await _department(session, tenant, "Engineering")
    position = await _position(
        session, tenant, "F1", department_id=engineering.id, budget="2000000"
    )
    person = await _employee(session, tenant, legal_entity, "PAID")
    await payroll_service.assign_salary(
        session,
        hr_user,
        person.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1800000"),
            effective_from=date(2026, 1, 1),
        ),
    )
    await position_service.assign(
        session,
        hr_user,
        position_id=position.id,
        employee_id=person.id,
        from_date=date(2026, 1, 1),
    )
    await session.commit()

    view = await position_service.budget_view(session, hr_user, on=TODAY, include_cost=True)
    [gap] = [g for g in view["cost"] if g["scope"] == "Engineering"]
    assert gap["budgeted_cost"] == "2000000.00"
    assert gap["actual_cost"] == "1800000.00"
    assert gap["gap"] == "200000.00"


async def test_vacancy_ageing_measures_from_the_vacate_date(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate, through a real round trip."""
    await _position(
        session,
        tenant,
        "OLD",
        status=PositionStatus.VACANT,
        vacated_on=TODAY - timedelta(days=200),
    )
    await _position(
        session,
        tenant,
        "NEW",
        status=PositionStatus.VACANT,
        vacated_on=TODAY - timedelta(days=10),
    )

    report = await position_service.vacancy_report(session, hr_user, on=TODAY)
    assert [v["code"] for v in report["vacancies"]] == ["OLD", "NEW"]
    assert report["vacancies"][0]["days_vacant"] == 200
    assert report["stale_count"] == 1
    assert report["buckets"]["Over 6 months"]["count"] == 1


async def test_the_org_chart_renders_vacant_seats(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate."""
    boss = await _position(session, tenant, "BOSS")
    await _position(session, tenant, "R1", reports_to=boss.id)
    await _position(session, tenant, "R2", reports_to=boss.id, status=PositionStatus.VACANT)

    person = await _employee(session, tenant, legal_entity, "BOSSY")
    await position_service.assign(
        session,
        hr_user,
        position_id=boss.id,
        employee_id=person.id,
        from_date=date(2026, 1, 1),
    )
    await session.commit()

    chart = await position_service.org_chart(session, hr_user, on=TODAY)
    assert chart["problems"] == []
    assert chart["vacant_seats"] == 2
    assert chart["total_seats"] == 3

    [root] = chart["roots"]
    assert root["code"] == "BOSS"
    assert root["holder"] == "BOSSY Person"
    assert root["vacant_below"] == 2
    assert {c["code"]: c["vacant"] for c in root["children"]} == {"R1": True, "R2": True}


async def test_the_chart_survives_the_managers_exit(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The practical argument for positions, demonstrated end to end."""
    boss = await _position(session, tenant, "BOSS")
    await _position(session, tenant, "R1", reports_to=boss.id)
    await _position(session, tenant, "R2", reports_to=boss.id)

    person = await _employee(session, tenant, legal_entity, "BOSSY")
    await position_service.assign(
        session,
        hr_user,
        position_id=boss.id,
        employee_id=person.id,
        from_date=date(2026, 1, 1),
    )
    await session.commit()
    await position_service.vacate(
        session, hr_user, position_id=boss.id, on=date(2026, 8, 31), reason="Left."
    )
    await session.commit()

    chart = await position_service.org_chart(session, hr_user, on=TODAY)
    [root] = chart["roots"]
    assert root["vacant"] is True
    assert len(root["children"]) == 2, "the team is still under the empty seat"


# ═══════════════════════════════════════════════════════════════════════════
# Requisition linkage — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def _chain(session: AsyncSession, tenant: Tenant) -> None:
    session.add(
        ApprovalChainRule(
            tenant_id=tenant.id,
            scope=ApprovalScope.REQUISITION,
            sequence=1,
            approver_role="hiring_manager",
            ctc_min=Decimal(0),
            ctc_max=None,
        )
    )
    await session.commit()


async def _requisition(
    session: AsyncSession, tenant: Tenant, *, position_id=None, ctc_max="2000000"
) -> Requisition:
    requisition = Requisition(
        tenant_id=tenant.id,
        code=f"REQ-{uuid.uuid4().hex[:6]}",
        title="Engineer",
        status=RequisitionStatus.DRAFT,
        justification="Team growth.",
        budget_ctc_max=Decimal(ctc_max),
        position_id=position_id,
        approved_headcount=10,
    )
    session.add(requisition)
    await session.commit()
    await session.refresh(requisition)
    return requisition


async def test_a_requisition_with_no_position_is_flagged_as_off_budget(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate.

    Flagged, not refused. Companies genuinely hire ahead of an approval, and a
    product that blocked it would be worked around within a week — at which point
    the flag is gone too.
    """
    await _chain(session, tenant)
    requisition = await _requisition(session, tenant, position_id=None)

    result = await requisition_service.submit_requisition(
        session, hr_user, requisition_id=requisition.id
    )
    await session.commit()

    off_budget = [f for f in result["flags"] if f["kind"] == "off_budget"]
    assert len(off_budget) == 1
    assert off_budget[0]["severity"] == "blocking_attention"
    assert "no position approval covers" in off_budget[0]["message"]
    # And it went through: flagged, not blocked.
    assert result["status"] == RequisitionStatus.PENDING_APPROVAL.value


async def test_a_requisition_against_an_approved_position_is_not_flagged(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _chain(session, tenant)
    position = await _position(session, tenant, "P1", budget="2500000")
    requisition = await _requisition(session, tenant, position_id=position.id, ctc_max="2000000")

    result = await requisition_service.submit_requisition(
        session, hr_user, requisition_id=requisition.id
    )
    await session.commit()
    kinds = {f["kind"] for f in result["flags"]}
    assert "off_budget" not in kinds
    assert "over_position_band" not in kinds


async def test_a_requisition_above_the_positions_band_is_flagged(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _chain(session, tenant)
    position = await _position(session, tenant, "P1", budget="1500000")
    requisition = await _requisition(session, tenant, position_id=position.id, ctc_max="2200000")

    result = await requisition_service.submit_requisition(
        session, hr_user, requisition_id=requisition.id
    )
    await session.commit()
    [flag] = [f for f in result["flags"] if f["kind"] == "over_position_band"]
    assert "700000.00 is unbudgeted" in flag["message"]


async def test_recruiting_against_an_occupied_seat_is_flagged(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Either a replacement nobody has told the incumbent about, or the wrong
    position was picked."""
    await _chain(session, tenant)
    position = await _position(session, tenant, "P1")
    person = await _employee(session, tenant, legal_entity, "INCUMBENT")
    await position_service.assign(
        session,
        hr_user,
        position_id=position.id,
        employee_id=person.id,
        from_date=date(2026, 1, 1),
    )
    await session.commit()

    requisition = await _requisition(session, tenant, position_id=position.id)
    result = await requisition_service.submit_requisition(
        session, hr_user, requisition_id=requisition.id
    )
    await session.commit()
    [flag] = [f for f in result["flags"] if f["kind"] == "position_occupied"]
    assert "told the incumbent" in flag["message"]


async def test_recruiting_against_a_frozen_seat_is_flagged(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _chain(session, tenant)
    position = await _position(session, tenant, "P1", status=PositionStatus.FROZEN)
    position.status_reason = "Q3 hiring freeze."
    await session.commit()

    requisition = await _requisition(session, tenant, position_id=position.id)
    result = await requisition_service.submit_requisition(
        session, hr_user, requisition_id=requisition.id
    )
    await session.commit()
    [flag] = [f for f in result["flags"] if f["kind"] == "position_frozen"]
    assert "Q3 hiring freeze" in flag["message"]
    assert "overrides a freeze" in flag["message"]


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def _scenario(
    session: AsyncSession, tenant: Tenant, hr_user: User, changes: list[dict]
) -> PositionScenario:
    scenario = PositionScenario(
        tenant_id=tenant.id,
        name=f"Scenario {uuid.uuid4().hex[:6]}",
        as_of=TODAY,
        created_by_user_id=hr_user.id,
    )
    session.add(scenario)
    await session.flush()
    for index, change in enumerate(changes, start=1):
        session.add(
            ScenarioChange(tenant_id=tenant.id, scenario_id=scenario.id, sequence=index, **change)
        )
    await session.commit()
    await session.refresh(scenario)
    return scenario


async def test_projecting_a_scenario_leaves_live_positions_untouched(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: fully isolated until commit."""
    engineering = await _department(session, tenant, "Engineering")
    sales = await _department(session, tenant, "Sales")
    position = await _position(session, tenant, "P1", department_id=engineering.id)

    scenario = await _scenario(
        session,
        tenant,
        hr_user,
        [
            {
                "kind": "move",
                "position_id": position.id,
                "values": {"department_id": str(sales.id), "department_name": "Sales"},
            },
            {
                "kind": "create",
                "values": {
                    "code": "NEW1",
                    "title": "Staff Engineer",
                    "budgeted_ctc_max": "3000000",
                    "department_name": "Engineering",
                },
            },
        ],
    )

    projection = await position_service.project_scenario(session, hr_user, scenario_id=scenario.id)
    await session.commit()

    assert projection["status"] == "draft"
    assert len(projection["changes"]) == 2

    # Nothing moved.
    live = await session.get(Position, position.id)
    await session.refresh(live)
    assert live.department_id == engineering.id
    count = await session.scalar(
        select(func.count()).select_from(Position).where(Position.tenant_id == tenant.id)
    )
    assert count == 1, "the created position exists only in the projection"


async def test_committing_a_scenario_applies_it_to_live_positions(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    engineering = await _department(session, tenant, "Engineering")
    sales = await _department(session, tenant, "Sales")
    moving = await _position(session, tenant, "MOVE1", department_id=engineering.id)
    cutting = await _position(session, tenant, "CUT1", department_id=engineering.id)

    scenario = await _scenario(
        session,
        tenant,
        hr_user,
        [
            {
                "kind": "move",
                "position_id": moving.id,
                "values": {"department_id": str(sales.id)},
            },
            {
                "kind": "abolish",
                "position_id": cutting.id,
                "reason": "Duplicated role.",
            },
            {
                "kind": "create",
                "values": {
                    "code": "NEW1",
                    "title": "Staff Engineer",
                    "budgeted_ctc_max": "3000000",
                    "department_id": str(engineering.id),
                },
            },
        ],
    )

    result = await position_service.commit_scenario(session, hr_user, scenario_id=scenario.id)
    await session.commit()

    assert len(result["applied"]) == 3
    await session.refresh(moving)
    await session.refresh(cutting)
    assert moving.department_id == sales.id
    assert cutting.status is PositionStatus.ABOLISHED
    assert cutting.effective_to == TODAY
    assert cutting.status_reason == "Duplicated role."

    created = await session.scalar(
        select(Position).where(Position.tenant_id == tenant.id, Position.code == "NEW1")
    )
    assert created is not None
    assert created.status is PositionStatus.APPROVED
    assert created.budgeted_ctc_max == Decimal("3000000.00")

    await session.refresh(scenario)
    assert scenario.status is ScenarioStatus.COMMITTED
    assert scenario.committed_at is not None


async def test_a_committed_scenario_records_which_position_each_create_produced(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Without it, "what did this reorg actually do" is a reconstruction."""
    scenario = await _scenario(
        session,
        tenant,
        hr_user,
        [
            {
                "kind": "create",
                "values": {"code": "NEW1", "title": "Engineer", "budgeted_ctc_max": "1000000"},
            }
        ],
    )
    await position_service.commit_scenario(session, hr_user, scenario_id=scenario.id)
    await session.commit()

    change = await session.scalar(
        select(ScenarioChange).where(ScenarioChange.scenario_id == scenario.id)
    )
    assert change.applied_position_id is not None
    created = await session.get(Position, change.applied_position_id)
    assert created.code == "NEW1"


async def test_a_scenario_cannot_be_committed_twice(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Committing the same reorg on top of itself."""
    position = await _position(session, tenant, "P1")
    scenario = await _scenario(
        session,
        tenant,
        hr_user,
        [{"kind": "freeze", "position_id": position.id, "reason": "Freeze."}],
    )
    await position_service.commit_scenario(session, hr_user, scenario_id=scenario.id)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await position_service.commit_scenario(session, hr_user, scenario_id=scenario.id)
    assert "on top of itself" in exc.value.detail


async def test_a_scenario_that_does_not_hold_together_writes_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Projected first: if the plan is invalid, no part of it is applied."""
    position = await _position(session, tenant, "P1")
    scenario = await _scenario(
        session,
        tenant,
        hr_user,
        [
            {"kind": "freeze", "position_id": position.id, "reason": "Valid."},
            # No reason: the engine refuses it.
            {"kind": "abolish", "position_id": position.id, "reason": ""},
        ],
    )

    with pytest.raises(HTTPException) as exc:
        await position_service.commit_scenario(session, hr_user, scenario_id=scenario.id)
    assert "needs a reason" in exc.value.detail

    await session.refresh(position)
    assert position.status is PositionStatus.APPROVED, "the valid change was not applied"


async def test_discarding_a_draft_marks_it_rather_than_deleting_it(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A reorg somebody modelled and decided against is a decision worth being
    able to answer for a year later."""
    scenario = await _scenario(session, tenant, hr_user, [])
    await position_service.discard_scenario(session, hr_user, scenario_id=scenario.id)
    await session.commit()

    await session.refresh(scenario)
    assert scenario.status is ScenarioStatus.DISCARDED
    assert scenario.discarded_at is not None
    assert await session.get(PositionScenario, scenario.id) is not None


async def test_a_committed_scenario_cannot_be_discarded(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    position = await _position(session, tenant, "P1")
    scenario = await _scenario(
        session,
        tenant,
        hr_user,
        [{"kind": "freeze", "position_id": position.id, "reason": "Freeze."}],
    )
    await position_service.commit_scenario(session, hr_user, scenario_id=scenario.id)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await position_service.discard_scenario(session, hr_user, scenario_id=scenario.id)
    assert "would not undo the changes" in exc.value.detail


async def test_a_freeze_scenario_shows_the_fte_it_would_hold_back(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A hiring freeze modelled as "do not fill vacant positions", with the
    number attached."""
    engineering = await _department(session, tenant, "Engineering")
    vacancies = [
        await _position(
            session,
            tenant,
            f"V{i}",
            department_id=engineering.id,
            status=PositionStatus.VACANT,
        )
        for i in range(3)
    ]
    scenario = await _scenario(
        session,
        tenant,
        hr_user,
        [{"kind": "freeze", "position_id": p.id, "reason": "Q3 freeze."} for p in vacancies],
    )

    projection = await position_service.project_scenario(session, hr_user, scenario_id=scenario.id)
    await session.commit()
    assert projection["projected_totals"]["Engineering"]["frozen_fte"] == "3.00"
    assert projection["live_totals"]["Engineering"]["frozen_fte"] == "0.00"


async def test_positions_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User
) -> None:
    position = await _position(session, tenant, "OURS")
    with pytest.raises(HTTPException) as exc:
        await position_service.vacate(session, other_hr_user, position_id=position.id, on=TODAY)
    assert exc.value.status_code == 404
