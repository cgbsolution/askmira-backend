"""Configurable approval chains: who approves, in order, when — and when away.

What these hold, against a real database:

- with no rules, leave and expenses route exactly as before (the line manager);
- a rule for long leave adds the skip-level manager after the line manager,
  and each sees the request only when it is their turn;
- an expense rule over an amount adds a named approver before finance;
- an approver away hands over to a delegate, who may decide in their place;
- a step waiting past its rule's limit goes up a level, and says so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.expenses import CityTierEnum, ExpenseClaimStatusEnum
from app.models.leave import LeavePolicy, LeaveRequestStatus, LeaveType
from app.models.user import UserRole
from app.services.approvals import rules
from app.services.expenses import service as expenses
from app.services.leave import service as leave_service
from tests.integration.conftest import requires_db
from tests.integration.test_leave_flow import START, _payload, _person

pytestmark = requires_db


@pytest.fixture
async def chain_org(session, tenant, legal_entity, location):
    _hr_emp, hr = await _person(
        session, tenant, legal_entity, location, code="HRA", name="Asha Admin",
        role=UserRole.ADMIN,
    )
    director_emp, director = await _person(
        session, tenant, legal_entity, location, code="DIR", name="Dev Director",
        role=UserRole.EMPLOYEE,
    )
    boss_emp, boss = await _person(
        session, tenant, legal_entity, location, code="MGR", name="Mala Manager",
        role=UserRole.EMPLOYEE, manager=director_emp,
    )
    meera_emp, meera = await _person(
        session, tenant, legal_entity, location, code="EMP", name="Meera Nair",
        role=UserRole.EMPLOYEE, manager=boss_emp,
    )
    _finance_emp, finance = await _person(
        session, tenant, legal_entity, location, code="FIN", name="Farah Finance",
        role=UserRole.EMPLOYEE,
    )
    _deputy_emp, deputy = await _person(
        session, tenant, legal_entity, location, code="DEP", name="Deepa Deputy",
        role=UserRole.EMPLOYEE,
    )
    earned = LeaveType(tenant_id=tenant.id, name="Earned Leave", code="EL")
    session.add(earned)
    await session.flush()
    session.add(
        LeavePolicy(
            tenant_id=tenant.id, leave_type_id=earned.id, name="Everyone",
            annual_quota_days=Decimal(24), accrual_frequency="annual",
            effective_from=date(2020, 1, 1),
        )
    )
    await session.commit()
    return {
        "hr": hr, "director": director, "boss": boss, "meera": meera, "meera_emp": meera_emp,
        "finance": finance, "deputy": deputy, "earned": earned,
    }


async def test_without_rules_leave_goes_to_the_line_manager(session, chain_org) -> None:
    created = await leave_service.apply(session, chain_org["meera"], _payload(chain_org))
    assert [s.approver_name for s in created.approval_chain] == ["Mala Manager"]


async def test_long_leave_also_needs_the_skip_level_manager(session, chain_org) -> None:
    hr, boss, director = chain_org["hr"], chain_org["boss"], chain_org["director"]
    await rules.create_rule(
        session, hr,
        {"process": "leave", "name": "A week or more", "priority": 10, "min_days": "5",
         "steps": [{"kind": "line_manager"}, {"kind": "skip_level"}]},
    )
    await session.commit()

    short = await leave_service.apply(
        session, chain_org["meera"], _payload(chain_org, start=START, end=START + timedelta(days=1))
    )
    assert len(short.approval_chain) == 1, "under five days: the line manager alone"
    await leave_service.cancel(session, chain_org["meera"], short.id)

    long = await leave_service.apply(
        session, chain_org["meera"],
        _payload(chain_org, start=START + timedelta(days=14), end=START + timedelta(days=18)),
    )
    assert [s.approver_name for s in long.approval_chain] == ["Mala Manager", "Dev Director"]

    # Not the director's turn yet.
    assert all(r.id != long.id for r in await leave_service.approvals_inbox(session, director))
    with pytest.raises(HTTPException):
        await leave_service.decide(session, director, long.id, approve=True, note=None)

    advanced = await leave_service.decide(session, boss, long.id, approve=True, note=None)
    assert advanced.status is LeaveRequestStatus.PENDING

    # Now it is, though the requester is not in the director's direct line.
    waiting = await leave_service.approvals_inbox(session, director)
    assert [r.id for r in waiting] == [long.id]
    done = await leave_service.decide(session, director, long.id, approve=True, note=None)
    assert done.status is LeaveRequestStatus.APPROVED


async def test_a_delegate_decides_while_the_approver_is_away(session, chain_org) -> None:
    boss, deputy = chain_org["boss"], chain_org["deputy"]
    today = datetime.now(UTC).date()
    await rules.create_delegation(
        session, boss, to_user_id=deputy.id, starts_on=today, ends_on=today + timedelta(days=7),
        reason="On leave",
    )
    await session.commit()
    created = await leave_service.apply(session, chain_org["meera"], _payload(chain_org))
    assert [r.id for r in await leave_service.approvals_inbox(session, deputy)] == [created.id]
    decided = await leave_service.decide(session, deputy, created.id, approve=True, note=None)
    assert decided.status is LeaveRequestStatus.APPROVED
    assert decided.approval_chain[0].decided_by_name == "Deepa Deputy"

    with pytest.raises(HTTPException) as exc:
        await rules.create_delegation(
            session, chain_org["meera"], to_user_id=deputy.id, starts_on=today,
            ends_on=today, reason=None, from_user_id=boss.id,
        )
    assert exc.value.status_code == 403, "only HR hands over somebody else's approvals"


async def test_a_large_claim_needs_a_named_approver_before_finance(session, chain_org) -> None:
    hr, boss, finance = chain_org["hr"], chain_org["boss"], chain_org["finance"]
    await rules.create_rule(
        session, hr,
        {"process": "expense", "name": "Big claims", "min_amount": "50000",
         "steps": [{"kind": "line_manager"}, {"kind": "user", "user_id": str(finance.id)}]},
    )
    await session.commit()

    async def claim(amount: str):
        captured = await expenses.capture_claim(
            session, chain_org["meera"], employee_id=chain_org["meera_emp"].id,
            category_key="hotel", city_tier=CityTierEnum.A, amount=Decimal(amount),
            incurred_on=date(2026, 9, 7), has_receipt=True,
        )
        return await expenses.submit_claim(
            session, chain_org["meera"], claim_id=uuid.UUID(str(captured["claim_id"]))
        )

    small = await claim("4000")
    assert [s["approver_name"] for s in small.approval_chain] == ["Mala Manager"]

    big = await claim("60000")
    assert [s["approver_name"] for s in big.approval_chain] == ["Mala Manager", "Farah Finance"]
    assert big.status is ExpenseClaimStatusEnum.PENDING_MANAGER

    with pytest.raises(HTTPException):
        await expenses.decide_claim_as_manager(
            session, finance, claim_id=big.id, approve=True, note=None
        )
    after_manager = await expenses.decide_claim_as_manager(
        session, boss, claim_id=big.id, approve=True
    )
    assert after_manager.status is ExpenseClaimStatusEnum.PENDING_MANAGER
    after_named = await expenses.decide_claim_as_manager(
        session, finance, claim_id=big.id, approve=True
    )
    assert after_named.status is ExpenseClaimStatusEnum.SUBMITTED, "now with finance"


async def test_a_step_waiting_too_long_goes_up_a_level(session, chain_org) -> None:
    hr = chain_org["hr"]
    await rules.create_rule(
        session, hr,
        {"process": "leave", "name": "Quick answers", "escalate_after_hours": 24,
         "steps": [{"kind": "line_manager"}]},
    )
    await session.commit()
    created = await leave_service.apply(session, chain_org["meera"], _payload(chain_org))

    assert (await rules.escalate_due(session, hr))["escalated"] == 0, "not yet"
    later = datetime.now(UTC) + timedelta(hours=30)
    assert (await rules.escalate_due(session, hr, now=later))["escalated"] == 1

    waiting = await leave_service.approvals_inbox(session, chain_org["director"])
    assert [r.id for r in waiting] == [created.id]
    step = waiting[0].approval_chain[0]
    assert step.approver_name == "Dev Director"
    assert (await rules.escalate_due(session, hr, now=later))["escalated"] == 0, "once only"
