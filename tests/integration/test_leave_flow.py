"""Time off against a real database.

Apply, approve, decline, cancel; who may do each; what the ledger says after;
what the team calendar reveals about whom.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import (
    Holiday,
    LeaveBalance,
    LeavePolicy,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.leave import LeaveRequestCreate
from app.services.leave import service as leave_service
from tests.integration.conftest import requires_db

pytestmark = requires_db


def _next_monday(after: date) -> date:
    return after + timedelta(days=(7 - after.weekday()) % 7 or 7)


START = _next_monday(date.today() + timedelta(days=14))


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    location: Location,
    *,
    code: str,
    name: str,
    role: UserRole,
    manager: Employee | None = None,
) -> tuple[Employee, User]:
    first, _, last = name.partition(" ")
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=first,
        last_name=last or None,
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=entity.id,
        location_id=location.id,
        manager_id=manager.id if manager else None,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2020, 1, 1),
    )
    session.add(employee)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}@acme.test",
        password_hash="not-a-real-hash",
        full_name=name,
        role=role,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(employee)
    await session.refresh(user)
    return employee, user


@pytest.fixture
async def org(session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, location: Location):
    hr_emp, hr = await _person(
        session, tenant, legal_entity, location, code="HR1", name="Priya Ramesh",
        role=UserRole.ADMIN,
    )
    boss_emp, boss = await _person(
        session, tenant, legal_entity, location, code="MGR1", name="Vikram Shah",
        role=UserRole.EMPLOYEE,
    )
    meera_emp, meera = await _person(
        session, tenant, legal_entity, location, code="EMP1", name="Meera Nair",
        role=UserRole.EMPLOYEE, manager=boss_emp,
    )
    ravi_emp, ravi = await _person(
        session, tenant, legal_entity, location, code="EMP2", name="Ravi Kumar",
        role=UserRole.EMPLOYEE, manager=hr_emp,
    )

    earned = LeaveType(tenant_id=tenant.id, name="Earned Leave", code="EL")
    session.add(earned)
    await session.flush()
    session.add(
        LeavePolicy(
            tenant_id=tenant.id,
            leave_type_id=earned.id,
            name="Earned leave - everyone",
            annual_quota_days=Decimal(24),
            accrual_frequency="annual",
            effective_from=date(2020, 1, 1),
        )
    )
    await session.commit()
    await session.refresh(earned)
    return {
        "hr": hr, "boss": boss, "meera": meera, "meera_emp": meera_emp,
        "ravi": ravi, "ravi_emp": ravi_emp, "earned": earned, "tenant": tenant,
    }


def _payload(org, *, start: date = START, end: date | None = None, **extra) -> LeaveRequestCreate:
    return LeaveRequestCreate(
        leave_type_id=org["earned"].id,
        start_date=start,
        end_date=end or start + timedelta(days=4),
        reason="Family wedding",
        **extra,
    )


async def _ledger(session: AsyncSession, org, year: int) -> LeaveBalance | None:
    return await session.scalar(
        select(LeaveBalance).where(
            LeaveBalance.employee_id == org["meera_emp"].id,
            LeaveBalance.leave_type_id == org["earned"].id,
            LeaveBalance.year == year,
        )
    )


async def test_apply_routes_to_the_line_manager_and_audits_in_the_same_commit(
    session: AsyncSession, org
) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))

    assert created.status is LeaveRequestStatus.PENDING
    assert created.total_days == Decimal(5)
    assert created.approval_chain[0].approver_name == "Vikram Shah"
    assert created.can_cancel and not created.can_decide

    entry = await session.scalar(select(AuditLog).where(AuditLog.entity_id == created.id))
    assert entry is not None and entry.entity_type == "leave_request"


async def test_manager_approval_moves_the_ledger(session: AsyncSession, org) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))

    inbox = await leave_service.approvals_inbox(session, org["boss"])
    assert [r.id for r in inbox] == [created.id]
    assert inbox[0].can_decide

    decided = await leave_service.decide(
        session, org["boss"], created.id, approve=True, note=None
    )
    assert decided.status is LeaveRequestStatus.APPROVED
    assert decided.approval_chain[0].decided_by_name == "Vikram Shah"

    row = await _ledger(session, org, START.year)
    assert row is not None and Decimal(str(row.used)) == Decimal(5)
    assert Decimal(str(row.accrued)) == Decimal(24)


async def test_nobody_decides_their_own_leave_not_even_hr(session: AsyncSession, org) -> None:
    hr_emp = await session.get(Employee, org["hr"].employee_id)
    assert hr_emp is not None
    own = await leave_service.apply(session, org["hr"], _payload(org))
    with pytest.raises(HTTPException) as refused:
        await leave_service.decide(session, org["hr"], own.id, approve=True, note=None)
    assert refused.value.status_code == 403


async def test_a_colleague_who_is_not_the_approver_cannot_decide(
    session: AsyncSession, org
) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))
    with pytest.raises(HTTPException) as refused:
        await leave_service.decide(session, org["ravi"], created.id, approve=True, note=None)
    assert refused.value.status_code == 403
    assert await leave_service.approvals_inbox(session, org["ravi"]) == []


async def test_hr_can_decide_any_pending_request_as_the_escalation_path(
    session: AsyncSession, org
) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))
    assert created.id in [r.id for r in await leave_service.approvals_inbox(session, org["hr"])]
    decided = await leave_service.decide(
        session, org["hr"], created.id, approve=False, note="Quarter close"
    )
    assert decided.status is LeaveRequestStatus.REJECTED
    assert await _ledger(session, org, START.year) is None


async def test_declining_needs_a_reason(session: AsyncSession, org) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))
    with pytest.raises(HTTPException) as refused:
        await leave_service.decide(session, org["boss"], created.id, approve=False, note="  ")
    assert refused.value.status_code == 422


async def test_a_request_beyond_the_balance_is_refused(session: AsyncSession, org) -> None:
    with pytest.raises(HTTPException) as refused:
        await leave_service.apply(
            session, org["meera"], _payload(org, end=START + timedelta(days=40))
        )
    assert refused.value.status_code == 422
    assert "available" in refused.value.detail


async def test_pending_requests_count_against_the_next_one(session: AsyncSession, org) -> None:
    await leave_service.apply(
        session, org["meera"], _payload(org, end=START + timedelta(days=18))  # 15 days
    )
    later = START + timedelta(days=28)
    preview = await leave_service.preview(
        session, org["meera"], _payload(org, start=later, end=later + timedelta(days=11))
    )
    assert preview.pending_days == Decimal(15)
    assert not preview.allowed


async def test_overlapping_requests_are_refused(session: AsyncSession, org) -> None:
    await leave_service.apply(session, org["meera"], _payload(org))
    with pytest.raises(HTTPException) as refused:
        await leave_service.apply(
            session, org["meera"], _payload(org, start=START + timedelta(days=2))
        )
    assert refused.value.status_code == 422


async def test_a_holiday_in_the_range_is_not_charged(session: AsyncSession, org) -> None:
    session.add(
        Holiday(
            tenant_id=org["tenant"].id, name="Karnataka Rajyotsava",
            holiday_date=START + timedelta(days=1), year=START.year, state_code="KA",
        )
    )
    session.add(
        Holiday(
            tenant_id=org["tenant"].id, name="Maharashtra Day",
            holiday_date=START + timedelta(days=2), year=START.year, state_code="MH",
        )
    )
    await session.commit()
    preview = await leave_service.preview(session, org["meera"], _payload(org))
    assert preview.total_days == Decimal(4)
    assert [h.name for h in preview.holidays] == ["Karnataka Rajyotsava"]


async def test_cancelling_approved_future_leave_restores_the_ledger(
    session: AsyncSession, org
) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))
    await leave_service.decide(session, org["boss"], created.id, approve=True, note=None)

    cancelled = await leave_service.cancel(session, org["meera"], created.id)
    assert cancelled.status is LeaveRequestStatus.CANCELLED
    row = await _ledger(session, org, START.year)
    assert row is not None and Decimal(str(row.used)) == Decimal(0)


async def test_started_leave_can_only_be_cancelled_by_hr(session: AsyncSession, org) -> None:
    yesterday = date.today() - timedelta(days=1)
    leave = LeaveRequest(
        tenant_id=org["tenant"].id,
        employee_id=org["meera_emp"].id,
        leave_type_id=org["earned"].id,
        start_date=yesterday,
        end_date=yesterday,
        total_days=Decimal(1),
        status=LeaveRequestStatus.APPROVED,
        approval_chain=[],
    )
    session.add(leave)
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await leave_service.cancel(session, org["meera"], leave.id)
    assert refused.value.status_code == 403
    cancelled = await leave_service.cancel(session, org["hr"], leave.id)
    assert cancelled.status is LeaveRequestStatus.CANCELLED


async def test_a_colleague_cannot_cancel_somebody_elses_leave(session: AsyncSession, org) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))
    with pytest.raises(HTTPException) as refused:
        await leave_service.cancel(session, org["ravi"], created.id)
    assert refused.value.status_code == 403


async def test_an_employee_cannot_apply_on_somebody_elses_behalf(
    session: AsyncSession, org
) -> None:
    with pytest.raises(HTTPException) as refused:
        await leave_service.apply(
            session, org["ravi"], _payload(org, employee_id=org["meera_emp"].id)
        )
    assert refused.value.status_code == 403


async def test_the_calendar_shows_colleagues_out_but_not_why(session: AsyncSession, org) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))
    window = {"start": START, "end": START + timedelta(days=10)}

    # Pending: invisible to a colleague, visible with its type to the manager.
    assert await leave_service.who_is_out(session, org["ravi"], **window) == []
    for_boss = await leave_service.who_is_out(session, org["boss"], **window)
    assert for_boss[0].leave_type_name == "Earned Leave"

    await leave_service.decide(session, org["boss"], created.id, approve=True, note=None)
    for_ravi = await leave_service.who_is_out(session, org["ravi"], **window)
    assert [e.employee_name for e in for_ravi] == ["Meera Nair"]
    assert for_ravi[0].leave_type_name is None


async def test_a_colleagues_balances_are_refused(session: AsyncSession, org) -> None:
    mine = await leave_service.balances(
        session, org["meera"], employee_id=None, year=START.year
    )
    assert [b.leave_type_code for b in mine] == ["EL"]
    with pytest.raises(HTTPException) as refused:
        await leave_service.balances(
            session, org["ravi"], employee_id=org["meera_emp"].id, year=START.year
        )
    assert refused.value.status_code == 403


async def test_another_tenant_cannot_see_or_decide_the_request(
    session: AsyncSession, org, other_hr_user: User
) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))
    with pytest.raises(HTTPException) as refused:
        await leave_service.decide(session, other_hr_user, created.id, approve=True, note=None)
    assert refused.value.status_code == 404
    assert await leave_service.approvals_inbox(session, other_hr_user) == []


async def test_without_a_manager_login_the_request_goes_to_hr(
    session: AsyncSession, org
) -> None:
    ravi_boss = await session.get(Employee, org["ravi_emp"].manager_id)
    assert ravi_boss is not None
    # Ravi reports to the HR admin, who can sign in; detach to force the fallback.
    org["ravi_emp"].manager_id = None
    await session.commit()
    created = await leave_service.apply(session, org["ravi"], _payload(org))
    assert created.approval_chain[0].role == "hr"
    assert created.id in [r.id for r in await leave_service.approvals_inbox(session, org["hr"])]


async def test_accrual_run_is_idempotent(session: AsyncSession, org) -> None:
    first = await leave_service.run_accruals(session, org["hr"], year=START.year)
    second = await leave_service.run_accruals(session, org["hr"], year=START.year)
    assert first.balances_updated == second.balances_updated == 4
    rows = (await session.scalars(select(LeaveBalance))).all()
    assert len(rows) == 4
    assert {Decimal(str(r.accrued)) for r in rows} == {Decimal(24)}


# ── Balances for whoever decides, manages or runs HR ────────────────────────


async def test_the_approver_sees_the_requesters_balance(session: AsyncSession, org) -> None:
    first = await leave_service.apply(session, org["meera"], _payload(org))  # 5 days
    later = START + timedelta(days=14)
    await leave_service.apply(
        session, org["meera"], _payload(org, start=later, end=later + timedelta(days=2))
    )

    inbox = {r.id: r for r in await leave_service.approvals_inbox(session, org["boss"])}
    balance = inbox[first.id].balance
    assert balance is not None
    assert balance.available == Decimal(24)
    # The other request is pending too; this one is not counted twice.
    assert balance.other_pending == Decimal(3)
    assert balance.after_approval == Decimal(19)


async def test_unpaid_leave_reaches_the_approver_without_a_balance(
    session: AsyncSession, org
) -> None:
    unpaid = LeaveType(
        tenant_id=org["tenant"].id, name="Leave Without Pay", code="LWP", is_paid=False
    )
    session.add(unpaid)
    await session.commit()
    created = await leave_service.apply(
        session,
        org["meera"],
        LeaveRequestCreate(
            leave_type_id=unpaid.id, start_date=START, end_date=START, reason="Personal"
        ),
    )
    inbox = await leave_service.approvals_inbox(session, org["boss"])
    assert [(r.id, r.balance) for r in inbox] == [(created.id, None)]


async def test_team_balances_follow_the_reporting_line(session: AsyncSession, org) -> None:
    from app.services.leave import balance_report

    created = await leave_service.apply(session, org["meera"], _payload(org))
    await leave_service.decide(session, org["boss"], created.id, approve=True, note=None)

    team = await balance_report.team_balances(session, org["boss"], year=START.year)
    assert [row.employee_code for row in team.items] == ["EMP1"]
    assert [c.code for c in team.leave_types] == ["EL"]
    cell = team.items[0].balances[0]
    own = await leave_service.balances(session, org["meera"], employee_id=None, year=START.year)
    # The manager's table and Meera's own card are one computation.
    assert (cell.entitled, cell.used, cell.available) == (Decimal(24), Decimal(5), own[0].available)

    everybody = await balance_report.team_balances(session, org["hr"], year=START.year)
    assert everybody.total == 4
    found = await balance_report.team_balances(session, org["hr"], year=START.year, q="ravi")
    assert [row.employee_code for row in found.items] == ["EMP2"]

    with pytest.raises(HTTPException) as refused:
        await balance_report.team_balances(session, org["meera"], year=START.year)
    assert refused.value.status_code == 403


async def test_the_balance_export_is_formula_safe_and_audited(session: AsyncSession, org) -> None:
    from app.services.leave import balance_report

    org["meera_emp"].display_name = "=HYPERLINK(1)"
    await session.commit()
    content = await balance_report.export_csv(session, org["boss"], year=START.year)
    await session.commit()

    lines = content.strip().splitlines()
    assert lines[0].startswith("employee_code,employee_name,department")
    assert len(lines) == 2 and lines[1].startswith("EMP1,'=HYPERLINK(1)")
    entry = await session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "leave_balance")
    )
    assert entry is not None and entry.changes["people"] == 1
