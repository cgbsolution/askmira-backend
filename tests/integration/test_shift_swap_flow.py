"""Shift swaps — asked, agreed, approved, applied — and the coverage view."""

from __future__ import annotations

import uuid
from datetime import date, time

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Shift, ShiftAssignment
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.attendance import ShiftCreate
from app.services.attendance import service as attendance_service
from app.services.attendance import setup as attendance_setup
from app.services.attendance import shift_swaps
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

TODAY = date(2026, 10, 14)
#: A Thursday: a working day for both shifts below.
SWAP_DAY = date(2026, 10, 22)


async def _shift(session: AsyncSession, hr_user: User, code: str, start: int, **extra) -> Shift:
    return await attendance_setup.create_shift(
        session,
        hr_user,
        ShiftCreate(
            code=code, name=f"{code} shift", start_time=time(start, 0),
            end_time=time(start + 8, 0), half_day_threshold_minutes=240,
            full_day_threshold_minutes=420, weekly_off_days=[7], **extra,
        ),
    )


async def _person(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, code: str, **extra
) -> tuple[Employee, User]:
    person = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com", legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE, date_of_joining=date(2025, 1, 1), **extra,
    )
    session.add(person)
    await session.flush()
    login = User(
        tenant_id=tenant.id, email=f"{code.lower()}.login@acme-demo.com", password_hash="x",
        full_name=code.title(), role=UserRole.EMPLOYEE, is_active=True, employee_id=person.id,
    )
    session.add(login)
    await session.commit()
    await session.refresh(person)
    await session.refresh(login)
    return person, await as_caller(session, login)


async def _assign(session: AsyncSession, tenant: Tenant, person: Employee, shift: Shift) -> None:
    session.add(
        ShiftAssignment(
            tenant_id=tenant.id, employee_id=person.id, shift_id=shift.id,
            effective_from=date(2026, 1, 1),
        )
    )
    await session.commit()


def swap_id(row: dict) -> uuid.UUID:
    return uuid.UUID(row["id"])


async def test_an_agreed_and_approved_swap_changes_both_schedules_for_that_day_only(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    morning = await _shift(session, hr_user, "MOR", 6)
    evening = await _shift(session, hr_user, "EVE", 14)
    lead, lead_login = await _person(session, tenant, legal_entity, "LEAD")
    asha, asha_login = await _person(session, tenant, legal_entity, "ASHA", manager_id=lead.id)
    ravi, ravi_login = await _person(session, tenant, legal_entity, "RAVI", manager_id=lead.id)
    await _assign(session, tenant, asha, morning)
    await _assign(session, tenant, ravi, evening)
    lead_login = await as_caller(session, lead_login)

    swap = await shift_swaps.request_swap(
        session, asha_login, partner_employee_id=ravi.id, swap_date=SWAP_DAY,
        reason="Doctor's appointment in the morning", today=TODAY,
    )
    assert swap["status"] == "awaiting_partner"
    assert swap["requester_shift"]["name"] == "MOR shift"

    # Only the colleague named answers; the manager cannot decide before they agree.
    with pytest.raises(HTTPException):
        await shift_swaps.respond_to_swap(session, lead_login, swap_id(swap), accept=True)
    with pytest.raises(HTTPException):
        await shift_swaps.decide_swap(
            session, lead_login, swap_id(swap), approve=True, today=TODAY
        )

    agreed = await shift_swaps.respond_to_swap(session, ravi_login, swap_id(swap), accept=True)
    assert agreed["status"] == "awaiting_approval"
    assert [p["id"] for p in await shift_swaps.pending_for_approver(session, lead_login)] == [
        swap["id"]
    ]
    # Neither party decides their own swap.
    with pytest.raises(HTTPException):
        await shift_swaps.decide_swap(
            session, asha_login, swap_id(swap), approve=True, today=TODAY
        )

    done = await shift_swaps.decide_swap(
        session, lead_login, swap_id(swap), approve=True, today=TODAY
    )
    assert done["status"] == "approved"

    on_day = await attendance_service.resolve_shift_plan(session, hr_user, asha.id, SWAP_DAY)
    assert on_day.shift is not None and on_day.shift.id == evening.id
    on_day = await attendance_service.resolve_shift_plan(session, hr_user, ravi.id, SWAP_DAY)
    assert on_day.shift is not None and on_day.shift.id == morning.id
    next_day = await attendance_service.resolve_shift_plan(
        session, hr_user, asha.id, date(2026, 10, 23)
    )
    assert next_day.shift is not None and next_day.shift.id == morning.id


async def test_a_swap_needs_two_different_working_shifts_on_a_future_day(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    morning = await _shift(session, hr_user, "MOR", 6)
    asha, asha_login = await _person(session, tenant, legal_entity, "ASHA")
    ravi, _ = await _person(session, tenant, legal_entity, "RAVI")
    await _assign(session, tenant, asha, morning)
    await _assign(session, tenant, ravi, morning)

    with pytest.raises(HTTPException) as same:
        await shift_swaps.request_swap(
            session, asha_login, partner_employee_id=ravi.id, swap_date=SWAP_DAY,
            reason="Swap please", today=TODAY,
        )
    assert "nothing to swap" in same.value.detail
    with pytest.raises(HTTPException):
        await shift_swaps.request_swap(
            session, asha_login, partner_employee_id=ravi.id, swap_date=TODAY,
            reason="Swap please", today=TODAY,
        )
    # A Sunday is this shift's weekly off: there is no shift to trade.
    with pytest.raises(HTTPException) as off:
        await shift_swaps.request_swap(
            session, asha_login, partner_employee_id=ravi.id, swap_date=date(2026, 10, 25),
            reason="Swap please", today=TODAY,
        )
    assert "week off" in off.value.detail


async def test_coverage_counts_scheduled_and_on_leave_against_the_minimum(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    morning = await _shift(session, hr_user, "MOR", 6, min_headcount=2)
    asha, _ = await _person(session, tenant, legal_entity, "ASHA")
    ravi, _ = await _person(session, tenant, legal_entity, "RAVI")
    await _assign(session, tenant, asha, morning)
    await _assign(session, tenant, ravi, morning)
    leave_type = LeaveType(tenant_id=tenant.id, code="CL", name="Casual leave", is_paid=True)
    session.add(leave_type)
    await session.flush()
    session.add(
        LeaveRequest(
            tenant_id=tenant.id, employee_id=ravi.id, leave_type_id=leave_type.id,
            start_date=SWAP_DAY, end_date=SWAP_DAY, total_days=1, reason="Away",
            status=LeaveRequestStatus.APPROVED,
        )
    )
    await session.commit()

    report = await shift_swaps.coverage(session, hr_user, start=SWAP_DAY, end=date(2026, 10, 25))
    by_day = {d["date"]: {s["shift_id"]: s for s in d["shifts"]} for d in report["days"]}
    thursday = by_day[SWAP_DAY.isoformat()][str(morning.id)]
    assert (thursday["scheduled"], thursday["on_leave"], thursday["available"]) == (2, 1, 1)
    assert thursday["short_by"] == 1
    friday = by_day["2026-10-23"][str(morning.id)]
    assert friday["short_by"] == 0
    # Sunday is the weekly off: nobody is scheduled.
    assert by_day["2026-10-25"] == {}

    shifts = (await session.scalars(select(Shift))).all()
    assert any(s.min_headcount == 2 for s in shifts)


async def test_the_schedule_agent_proposes_cover_and_an_approval_puts_them_on(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    from datetime import timedelta

    from app.models.autonomy import AgentProposal
    from app.services.autonomy import agents
    from app.services.autonomy import service as autonomy

    today = date.today()
    horizon = [today + timedelta(days=n) for n in range(1, 8)]
    monday = next(day for day in horizon if day.isoweekday() == 1)
    morning = await _shift(session, hr_user, "MOR", 6, min_headcount=2)
    # Not the workspace default, so people without an assignment are unscheduled.
    shift_row = await session.get(Shift, morning.id)
    shift_row.is_default = False
    await session.commit()
    asha, _ = await _person(session, tenant, legal_entity, "ASHA")
    kiran, _ = await _person(session, tenant, legal_entity, "KIRAN")
    ravi, _ = await _person(session, tenant, legal_entity, "RAVI")
    await _assign(session, tenant, asha, morning)
    leave_type = LeaveType(tenant_id=tenant.id, code="CL", name="Casual leave", is_paid=True)
    session.add(leave_type)
    await session.flush()
    session.add(
        LeaveRequest(
            tenant_id=tenant.id, employee_id=kiran.id, leave_type_id=leave_type.id,
            start_date=monday, end_date=monday, total_days=1, reason="Away",
            status=LeaveRequestStatus.APPROVED,
        )
    )
    await session.commit()

    outcome = await agents.schedule_agent(session, hr_user, today=today)
    # Six working days short by one each (Sunday is the shift's weekly off).
    assert outcome["short_slots"] == 6 and outcome["proposed"] == 6
    proposals = (await session.scalars(select(AgentProposal))).all()
    by_day = {p.proposed_change["date"]: p for p in proposals}
    assert by_day[monday.isoformat()].proposed_change["employee_id"] == str(ravi.id), (
        "Kiran is on leave that Monday, so Ravi is asked"
    )
    assert "not on leave" in by_day[monday.isoformat()].reasoning

    again = await agents.schedule_agent(session, hr_user, today=today)
    assert again["proposed"] == 0, "a slot with a proposal is not proposed again"

    await autonomy.decide_proposal(
        session, hr_user, proposal_id=by_day[monday.isoformat()].id, approve=True
    )
    await session.commit()
    cover = await session.scalar(
        select(ShiftAssignment).where(ShiftAssignment.employee_id == ravi.id)
    )
    assert (cover.effective_from, cover.effective_to) == (monday, monday)
    view = await shift_swaps.coverage(session, hr_user, start=monday, end=monday)
    assert view["days"][0]["shifts"][0]["short_by"] == 0
