"""Slice 24 gates that need a real database.

The engines are proved in `tests/lifecycle/`. What only a database settles: that
a future-dated event genuinely does not apply early and does apply on the day,
that a failing cascade leaves *nothing* applied, that a manager change moves the
approvals the old manager was holding, and that the history view is complete.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cycle_config import ProbationOutcome, ProbationReview
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import (
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from app.models.lifecycle import (
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    ProbationPolicy,
    ProbationReminder,
    SilencePolicy,
)
from app.models.org import LegalEntity, Location
from app.models.payroll import (
    CalculationType,
    ComponentType,
    EmployeeSalary,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.lifecycle import service as lifecycle_service
from app.services.payroll import service as payroll_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

TODAY = date(2026, 9, 7)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


async def _location(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity,
    name: str, state: str,
) -> Location:
    location = Location(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id, name=name,
        code=name[:3].upper(), city=name, state_code=state, country_code="IN",
    )
    session.add(location)
    await session.commit()
    await session.refresh(location)
    return location


async def _employee(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, code: str,
    *, location_id=None, manager_id=None, grade: str = "L4",
    status: EmploymentStatus = EmploymentStatus.ACTIVE,
) -> Employee:
    person = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code, last_name="Person",
        display_name=f"{code} Person", work_email=f"{code.lower()}@acme.test",
        legal_entity_id=legal_entity.id, location_id=location_id,
        manager_id=manager_id, grade=grade, designation="Engineer", status=status,
        date_of_joining=date(2025, 4, 1),
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _with_salary(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee,
    ctc: str = "1800000",
) -> SalaryStructure:
    basic = SalaryComponent(
        tenant_id=tenant.id, code="BASIC", name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF, base_component_code="CTC",
        value=Decimal(40), display_order=1,
    )
    special = SalaryComponent(
        tenant_id=tenant.id, code="SPECIAL", name="Special",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE, display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()
    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=date(2025, 1, 1),
        applicable_grades=[], applicable_locations=[],
    )
    for index, component in enumerate((basic, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id, component_id=component.id,
                component=component, display_order=index,
            )
        )
    session.add(structure)
    await session.commit()

    await payroll_service.assign_salary(
        session, hr_user, employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id, ctc_annual=Decimal(ctc),
            effective_from=date(2025, 4, 1),
        ),
    )
    return structure


# ═══════════════════════════════════════════════════════════════════════════
# Future dating — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_future_dated_event_does_not_apply_early(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    A promotion approved in March effective April must not change the employee
    record in March.
    """
    person = await _employee(session, tenant, legal_entity, "FUT", grade="L4")
    event = await lifecycle_service.raise_event(
        session, hr_user,
        employee_id=person.id, event_type=LifecycleEventType.PROMOTION,
        effective_from=date(2026, 10, 1),
        new_values={"grade": "L5", "designation": "Senior Engineer"},
        reason="Strong year.", today=TODAY,
    )
    await lifecycle_service.approve_event(
        session, hr_user, event_id=event.id, today=TODAY
    )
    await session.commit()

    # The sweep on 7 September finds nothing.
    result = await lifecycle_service.effect_due_events(session, hr_user, on=TODAY)
    await session.commit()
    assert result["due"] == 0

    await session.refresh(person)
    assert person.grade == "L4", "the record has not moved"
    await session.refresh(event)
    assert event.status is LifecycleStatus.APPROVED


async def test_a_future_dated_event_applies_on_its_date_unattended(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The other half of the gate: it lands on 1 April whether or not anybody
    remembers."""
    person = await _employee(session, tenant, legal_entity, "FUT2", grade="L4")
    event = await lifecycle_service.raise_event(
        session, hr_user,
        employee_id=person.id, event_type=LifecycleEventType.PROMOTION,
        effective_from=date(2026, 10, 1),
        new_values={"grade": "L5"}, reason="Strong year.", today=TODAY,
    )
    await lifecycle_service.approve_event(
        session, hr_user, event_id=event.id, today=TODAY
    )
    await session.commit()

    result = await lifecycle_service.effect_due_events(
        session, hr_user, on=date(2026, 10, 1)
    )
    await session.commit()
    assert result["due"] == 1
    assert result["effected"] == 1

    await session.refresh(person)
    assert person.grade == "L5"
    await session.refresh(event)
    assert event.status is LifecycleStatus.EFFECTED
    assert event.effected_at is not None


async def test_effecting_before_the_date_is_refused_even_when_asked_directly(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The sweep is not the only caller. The refusal lives in the service."""
    person = await _employee(session, tenant, legal_entity, "EARLY")
    event = await lifecycle_service.raise_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.GRADE_CHANGE,
        effective_from=date(2026, 11, 1), new_values={"grade": "L5"},
        reason="Approved early.", today=TODAY,
    )
    await lifecycle_service.approve_event(
        session, hr_user, event_id=event.id, today=TODAY
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await lifecycle_service.effect_event(
            session, hr_user, event_id=event.id, on=TODAY
        )
    assert "has not arrived" in exc.value.detail


async def test_an_unapproved_event_cannot_be_effected(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    person = await _employee(session, tenant, legal_entity, "UNAPP")
    event = await lifecycle_service.raise_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.GRADE_CHANGE, effective_from=TODAY,
        new_values={"grade": "L5"}, reason="Not approved yet.", today=TODAY,
    )
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await lifecycle_service.effect_event(session, hr_user, event_id=event.id, on=TODAY)
    assert "Only an approved event" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Cascade atomicity — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_failing_cascade_leaves_nothing_applied(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: verified by inducing a mid-cascade failure.

    The employee record change happens first and the salary revision second. This
    event has a pay change and the employee has no salary to revise, so the second
    step fails — and the first must not survive it. A half-applied promotion is
    very hard to unpick, and worse, nobody knows it happened.
    """
    person = await _employee(session, tenant, legal_entity, "ATOM", grade="L4")
    # Deliberately no salary assigned: `_revise_salary` will raise.
    event = await lifecycle_service.raise_event(
        session, hr_user,
        employee_id=person.id, event_type=LifecycleEventType.PROMOTION,
        effective_from=TODAY,
        new_values={"grade": "L5", "designation": "Senior Engineer"},
        reason="Promotion with a pay rise.",
        new_ctc_annual=Decimal("2500000"), today=TODAY,
    )
    await lifecycle_service.approve_event(
        session, hr_user, event_id=event.id, today=TODAY
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await lifecycle_service.effect_event(
            session, hr_user, event_id=event.id, on=TODAY
        )
    await session.commit()
    assert "nothing was applied" in exc.value.detail

    # The grade change was rolled back with the rest.
    await session.refresh(person)
    assert person.grade == "L4", "the first step did not survive the second failing"
    assert person.designation == "Engineer"

    # And the failure is visible rather than silent.
    await session.refresh(event)
    assert event.status is LifecycleStatus.FAILED
    assert "no current salary" in event.failure_reason
    assert event.attempt_count == 1

    salaries = await session.scalar(
        select(func.count()).select_from(EmployeeSalary).where(
            EmployeeSalary.employee_id == person.id
        )
    )
    assert salaries == 0


async def test_a_successful_cascade_applies_every_part(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    person = await _employee(session, tenant, legal_entity, "GOOD", grade="L4")
    await _with_salary(session, tenant, hr_user, person, ctc="1800000")

    event = await lifecycle_service.raise_event(
        session, hr_user,
        employee_id=person.id, event_type=LifecycleEventType.PROMOTION,
        effective_from=TODAY, new_values={"grade": "L5"},
        reason="Promotion.", new_ctc_annual=Decimal("2500000"), today=TODAY,
    )
    await lifecycle_service.approve_event(
        session, hr_user, event_id=event.id, today=TODAY
    )
    await session.commit()

    result = await lifecycle_service.effect_event(
        session, hr_user, event_id=event.id, on=TODAY
    )
    await session.commit()

    assert "employee.grade" in result["applied"]
    assert "salary revision" in result["applied"]

    await session.refresh(person)
    assert person.grade == "L5"

    await session.refresh(event)
    assert event.salary_revision_id is not None
    live = await session.scalar(
        select(EmployeeSalary).where(
            EmployeeSalary.employee_id == person.id,
            EmployeeSalary.effective_to.is_(None),
        )
    )
    assert Decimal(str(live.ctc_annual)) == Decimal("2500000.00")


async def test_one_failing_event_does_not_take_the_sweep_with_it(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """A sweep effecting forty events must not lose thirty-nine because the
    fortieth had a bad salary structure."""
    good = await _employee(session, tenant, legal_entity, "SWEEP1", grade="L4")
    bad = await _employee(session, tenant, legal_entity, "SWEEP2", grade="L4")

    for person, ctc in ((good, None), (bad, Decimal("2500000"))):
        event = await lifecycle_service.raise_event(
            session, hr_user, employee_id=person.id,
            event_type=LifecycleEventType.GRADE_CHANGE, effective_from=TODAY,
            new_values={"grade": "L5"}, reason="Bulk grade change.",
            new_ctc_annual=ctc, today=TODAY,
        )
        await lifecycle_service.approve_event(
            session, hr_user, event_id=event.id, today=TODAY
        )
    await session.commit()

    result = await lifecycle_service.effect_due_events(session, hr_user, on=TODAY)
    await session.commit()

    assert result["due"] == 2
    assert result["effected"] == 1
    assert len(result["failed"]) == 1

    await session.refresh(good)
    await session.refresh(bad)
    assert good.grade == "L5", "the good one went through"
    assert bad.grade == "L4", "the bad one was rolled back entirely"


# ═══════════════════════════════════════════════════════════════════════════
# Cross-state consequences — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_cross_state_transfer_surfaces_its_consequences_at_approval(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: surfaced at approval and applied on effect."""
    bengaluru = await _location(session, tenant, legal_entity, "Bengaluru", "KA")
    mumbai = await _location(session, tenant, legal_entity, "Mumbai", "MH")
    person = await _employee(
        session, tenant, legal_entity, "XFER", location_id=bengaluru.id
    )

    preview = await lifecycle_service.preview_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.TRANSFER, effective_from=date(2026, 10, 1),
        new_values={"location_id": str(mumbai.id)},
        reason="Relocation.", today=TODAY,
    )
    kinds = {c["kind"] for c in preview["consequences"]}
    assert "professional_tax" in kinds
    assert "holiday_calendar" in kinds
    assert preview["payroll_consequences"] >= 1

    pt = next(c for c in preview["consequences"] if c["kind"] == "professional_tax")
    assert "KA" in pt["message"] and "MH" in pt["message"]


async def test_the_consequences_are_snapshotted_onto_the_event(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """"Did the approver know PT would change" has to be answerable about the
    moment they signed, not about what the code says today."""
    bengaluru = await _location(session, tenant, legal_entity, "Bengaluru", "KA")
    mumbai = await _location(session, tenant, legal_entity, "Mumbai", "MH")
    person = await _employee(
        session, tenant, legal_entity, "SNAP", location_id=bengaluru.id
    )

    event = await lifecycle_service.raise_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.TRANSFER, effective_from=date(2026, 10, 1),
        new_values={"location_id": str(mumbai.id)}, reason="Relocation.", today=TODAY,
    )
    await session.commit()

    kinds = {c["kind"] for c in event.consequences_shown}
    assert "professional_tax" in kinds


async def test_the_transfer_applies_the_new_location_on_effect(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    bengaluru = await _location(session, tenant, legal_entity, "Bengaluru", "KA")
    mumbai = await _location(session, tenant, legal_entity, "Mumbai", "MH")
    person = await _employee(
        session, tenant, legal_entity, "APPLY", location_id=bengaluru.id
    )

    event = await lifecycle_service.raise_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.TRANSFER, effective_from=TODAY,
        new_values={"location_id": str(mumbai.id)}, reason="Relocation.", today=TODAY,
    )
    await lifecycle_service.approve_event(
        session, hr_user, event_id=event.id, today=TODAY
    )
    await session.commit()
    await lifecycle_service.effect_event(
        session, hr_user, event_id=event.id, on=TODAY
    )
    await session.commit()

    await session.refresh(person)
    assert person.location_id == mumbai.id
    location = await session.get(Location, person.location_id)
    assert location.state_code == "MH", "the PT state has genuinely changed"


# ═══════════════════════════════════════════════════════════════════════════
# Manager change — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_manager_change_reassigns_pending_approvals(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: none orphaned.

    Left behind, those requests sit with somebody who no longer has any reason to
    look at them, and the employee waits for a decision nobody will make.
    """
    old_manager = await _employee(session, tenant, legal_entity, "OLDMGR")
    new_manager = await _employee(session, tenant, legal_entity, "NEWMGR")
    person = await _employee(
        session, tenant, legal_entity, "MOVED", manager_id=old_manager.id
    )

    leave_type = LeaveType(
        tenant_id=tenant.id, code="EL", name="Earned Leave", is_paid=True
    )
    session.add(leave_type)
    await session.flush()
    request = LeaveRequest(
        tenant_id=tenant.id, employee_id=person.id, leave_type_id=leave_type.id,
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 3),
        total_days=Decimal(3), status=LeaveRequestStatus.PENDING,
        approval_chain=[{"sequence": 1, "approver_employee_id": str(old_manager.id),
                         "decision": "pending"}],
    )
    session.add(request)
    await session.commit()

    preview = await lifecycle_service.preview_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.MANAGER_CHANGE, effective_from=TODAY,
        new_values={"manager_id": str(new_manager.id)},
        reason="Reorg.", today=TODAY,
    )
    pending = next(
        c for c in preview["consequences"] if c["kind"] == "pending_approvals"
    )
    assert "1 pending approval(s)" in pending["message"]

    event = await lifecycle_service.raise_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.MANAGER_CHANGE, effective_from=TODAY,
        new_values={"manager_id": str(new_manager.id)}, reason="Reorg.", today=TODAY,
    )
    await lifecycle_service.approve_event(
        session, hr_user, event_id=event.id, today=TODAY
    )
    await session.commit()
    result = await lifecycle_service.effect_event(
        session, hr_user, event_id=event.id, on=TODAY
    )
    await session.commit()

    assert any("reassigned 1 pending approval" in a for a in result["applied"])
    await session.refresh(person)
    assert person.manager_id == new_manager.id

    await session.refresh(request)
    [step] = request.approval_chain
    assert step["approver_employee_id"] == str(new_manager.id)
    assert step["reassigned_reason"] == "The employee's manager changed."


# ═══════════════════════════════════════════════════════════════════════════
# Bulk — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_bulk_raise_reports_per_employee_results(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    One employee failing validation must not fail the rest, and the one that
    failed has to be named — a count of successes leaves somebody reconciling two
    lists by hand.
    """
    people = [
        await _employee(session, tenant, legal_entity, f"BULK{i}", grade="L4")
        for i in range(5)
    ]

    rows = [
        {"employee_id": p.id, "new_values": {"grade": "L5"}} for p in people[:4]
    ]
    # The fifth is already L5, so nothing would change — a real validation
    # failure rather than a contrived one.
    people[4].grade = "L5"
    await session.commit()
    rows.append({"employee_id": people[4].id, "new_values": {"grade": "L5"}})

    result = await lifecycle_service.raise_bulk(
        session, hr_user,
        name="FY27 increment cycle",
        event_type=LifecycleEventType.GRADE_CHANGE,
        effective_from=date(2026, 10, 1),
        reason="Annual increment cycle.",
        rows=rows,
        today=TODAY,
    )
    await session.commit()

    assert result["raised_count"] == 4
    assert result["rejected_count"] == 1
    assert result["rejected"][0]["employee_id"] == str(people[4].id)
    assert "Nothing would change" in result["rejected"][0]["reason"]

    events = await session.scalar(
        select(func.count()).select_from(LifecycleEvent).where(
            LifecycleEvent.batch_id == uuid.UUID(result["batch_id"])
        )
    )
    assert events == 4


# ═══════════════════════════════════════════════════════════════════════════
# History — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_history_view_is_complete_including_what_failed(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    What an audit or a dispute asks for. "We tried and it did not go through" is
    part of the history, and a view that hid it would leave somebody looking at
    a gap.
    """
    person = await _employee(session, tenant, legal_entity, "HIST", grade="L4")

    effected = await lifecycle_service.raise_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.GRADE_CHANGE, effective_from=TODAY,
        new_values={"grade": "L5"}, reason="Promotion.", today=TODAY,
    )
    await lifecycle_service.approve_event(
        session, hr_user, event_id=effected.id, today=TODAY
    )
    await session.commit()
    await lifecycle_service.effect_event(
        session, hr_user, event_id=effected.id, on=TODAY
    )
    await session.commit()

    cancelled = await lifecycle_service.raise_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.REDESIGNATION,
        effective_from=date(2026, 11, 1),
        new_values={"designation": "Principal Engineer"},
        reason="Considered and withdrawn.", today=TODAY,
    )
    await lifecycle_service.cancel_event(
        session, hr_user, event_id=cancelled.id, reason="Decided against."
    )
    await session.commit()

    history = await lifecycle_service.employee_history(
        session, hr_user, employee_id=person.id
    )
    assert len(history["events"]) == 2
    statuses = {e["status"] for e in history["events"]}
    assert statuses == {"effected", "cancelled"}

    promotion = next(e for e in history["events"] if e["type"] == "grade_change")
    assert promotion["approved_by"] == str(hr_user.id)
    assert promotion["effected_at"] is not None
    assert promotion["changes"] == [{"field": "grade", "from": "L4", "to": "L5"}]


async def test_a_point_in_time_query_returns_the_historical_state(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Reconstructed by undoing effected events backwards, so there is no
    separate history table to fall out of step with the record."""
    person = await _employee(session, tenant, legal_entity, "PIT", grade="L3")

    for effective, grade in ((date(2026, 4, 1), "L4"), (date(2026, 7, 1), "L5")):
        event = await lifecycle_service.raise_event(
            session, hr_user, employee_id=person.id,
            event_type=LifecycleEventType.GRADE_CHANGE, effective_from=effective,
            new_values={"grade": grade}, reason="Promotion.", today=TODAY,
        )
        await lifecycle_service.approve_event(
            session, hr_user, event_id=event.id, today=TODAY
        )
        await session.commit()
        await lifecycle_service.effect_event(
            session, hr_user, event_id=event.id, on=TODAY
        )
        await session.commit()

    await session.refresh(person)
    assert person.grade == "L5"

    march = await lifecycle_service.state_on(
        session, hr_user, employee_id=person.id, on=date(2026, 3, 1)
    )
    assert march["state"]["grade"] == "L3"
    assert march["events_undone"] == 2

    may = await lifecycle_service.state_on(
        session, hr_user, employee_id=person.id, on=date(2026, 5, 1)
    )
    assert may["state"]["grade"] == "L4"
    assert may["events_undone"] == 1


async def test_an_effected_event_cannot_be_cancelled(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Cancelling would not undo the change."""
    person = await _employee(session, tenant, legal_entity, "DONE", grade="L4")
    event = await lifecycle_service.raise_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.GRADE_CHANGE, effective_from=TODAY,
        new_values={"grade": "L5"}, reason="Promotion.", today=TODAY,
    )
    await lifecycle_service.approve_event(
        session, hr_user, event_id=event.id, today=TODAY
    )
    await session.commit()
    await lifecycle_service.effect_event(session, hr_user, event_id=event.id, on=TODAY)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await lifecycle_service.cancel_event(
            session, hr_user, event_id=event.id, reason="Changed our minds."
        )
    assert "raise a reversing event" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Probation — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def _review(
    session: AsyncSession, tenant: Tenant, employee: Employee, *, due: date
) -> ProbationReview:
    review = ProbationReview(
        tenant_id=tenant.id, employee_id=employee.id,
        probation_end_date=due + timedelta(days=10), review_due_date=due,
    )
    session.add(review)
    await session.commit()
    await session.refresh(review)
    return review


async def _policy(
    session: AsyncSession, tenant: Tenant, hr_user: User, policy: SilencePolicy,
    grace: int = 0,
) -> ProbationPolicy:
    row = ProbationPolicy(
        tenant_id=tenant.id, silence_policy=policy, grace_days=grace,
        hr_escalation_user_id=hr_user.id, configured_by_user_id=hr_user.id,
    )
    session.add(row)
    await session.commit()
    return row


async def test_a_tenant_with_no_probation_policy_is_refused_not_defaulted(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: configuration explicit.

    A product that picked one would be making an employment decision on a
    client's behalf.
    """
    with pytest.raises(HTTPException) as exc:
        await lifecycle_service.resolve_probation_drift(session, hr_user, on=TODAY)
    assert "no default" in exc.value.detail
    assert "somebody has to choose" in exc.value.detail


async def test_the_silence_policy_decides_what_happens_to_a_drifting_review(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: default-on-silence follows tenant configuration."""
    person = await _employee(
        session, tenant, legal_entity, "DRIFT", status=EmploymentStatus.ACTIVE
    )
    await _review(session, tenant, person, due=date(2026, 8, 20))
    policy = await _policy(session, tenant, hr_user, SilencePolicy.REQUIRE_DECISION)

    strict = await lifecycle_service.resolve_probation_drift(session, hr_user, on=TODAY)
    assert strict["policy"] == "require_decision"
    assert strict["cases"][0]["auto_outcome"] is None
    assert "Nothing happens" in strict["cases"][0]["resolution"]

    # The same silence, the opposite outcome, under the other policy.
    policy.silence_policy = SilencePolicy.AUTO_CONFIRM
    await session.commit()

    lenient = await lifecycle_service.resolve_probation_drift(session, hr_user, on=TODAY)
    assert lenient["cases"][0]["auto_outcome"] == "confirm"
    assert "silence as consent" in lenient["cases"][0]["resolution"]


async def test_probation_reminders_escalate_and_are_not_sent_twice(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: reminders escalate."""
    manager = await _employee(session, tenant, legal_entity, "PMGR")
    manager_user = User(
        tenant_id=tenant.id, email="pmgr@acme.test", password_hash="x",
        full_name="Manager", role=hr_user.role, is_active=True,
        employee_id=manager.id,
    )
    session.add(manager_user)
    await session.flush()

    person = await _employee(
        session, tenant, legal_entity, "PROB", manager_id=manager.id
    )
    await _review(session, tenant, person, due=date(2026, 9, 20))
    await _policy(session, tenant, hr_user, SilencePolicy.REQUIRE_DECISION)

    first = await lifecycle_service.send_probation_reminders(
        session, hr_user, on=date(2026, 9, 7)
    )
    await session.commit()
    assert first["count"] == 1
    assert first["sent"][0]["step"] == "manager_first"

    # Running the sweep again the same day sends nothing.
    again = await lifecycle_service.send_probation_reminders(
        session, hr_user, on=date(2026, 9, 7)
    )
    await session.commit()
    assert again["count"] == 0

    # Closer to the date, it escalates.
    second = await lifecycle_service.send_probation_reminders(
        session, hr_user, on=date(2026, 9, 17)
    )
    await session.commit()
    assert second["sent"][0]["step"] == "manager_second"

    sent = await session.scalar(
        select(func.count()).select_from(ProbationReminder).where(
            ProbationReminder.tenant_id == tenant.id
        )
    )
    assert sent == 2


async def test_a_decided_review_stops_being_chased(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    person = await _employee(session, tenant, legal_entity, "DECIDED")
    review = await _review(session, tenant, person, due=date(2026, 8, 20))
    review.outcome = ProbationOutcome.CONFIRM
    await _policy(session, tenant, hr_user, SilencePolicy.AUTO_CONFIRM)
    await session.commit()

    reminders = await lifecycle_service.send_probation_reminders(
        session, hr_user, on=TODAY
    )
    drift = await lifecycle_service.resolve_probation_drift(session, hr_user, on=TODAY)
    await session.commit()
    assert reminders["count"] == 0
    assert drift["count"] == 0


async def test_lifecycle_events_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User,
    legal_entity: LegalEntity,
) -> None:
    person = await _employee(session, tenant, legal_entity, "ISO")
    event = await lifecycle_service.raise_event(
        session, hr_user, employee_id=person.id,
        event_type=LifecycleEventType.GRADE_CHANGE, effective_from=TODAY,
        new_values={"grade": "L5"}, reason="Promotion.", today=TODAY,
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await lifecycle_service.approve_event(
            session, other_hr_user, event_id=event.id, today=TODAY
        )
    assert exc.value.status_code == 404
