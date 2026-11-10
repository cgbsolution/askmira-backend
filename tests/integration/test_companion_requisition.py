"""Ask Mira raises a hiring requisition ("Raise the second requisition").

* HR's offer names the department, seats, the CTC ceiling, the budget check and
  the approvers; nothing is written until the yes. The yes raises it and sends it
  for approval through the hiring module's own services, with their audit rows.
* Missing details are asked for, never invented; an unknown department lists
  the real ones; with no approval rules it says so and offers nothing.
* An employee with no team is never offered it; a manager raises only for their
  own department; a requisition past the headcount budget says so in the offer.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.hiring import Requisition, RequisitionStatus
from app.models.offer import ApprovalChainRule, ApprovalScope, RequisitionApproval
from app.models.org import Department
from app.models.position import HeadcountBudget
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.ai.client import use_backend
from app.services.companion import service as companion
from app.services.reporting.ranges import financial_year_bounds, financial_year_label
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_companion_flow import FakeModel, _ai_on, _consent, plan

pytestmark = requires_db


@pytest.fixture
async def hiring(session: AsyncSession, tenant: Tenant) -> dict[str, Department]:
    tenant.enabled_modules = {**(tenant.enabled_modules or {}), "hiring": True}
    sales = Department(tenant_id=tenant.id, name="Sales West", code="SW")
    ops = Department(tenant_id=tenant.id, name="Operations", code="OPS")
    session.add_all([sales, ops])
    await session.flush()
    session.add(
        ApprovalChainRule(
            tenant_id=tenant.id,
            scope=ApprovalScope.REQUISITION,
            sequence=1,
            approver_role="hr",
            label="HR head",
            ctc_min=0,
            ctc_max=None,
            grades=[],
            only_when_over_band=False,
            is_active=True,
        )
    )
    await session.commit()
    return {"sales": sales, "ops": ops}


@pytest.fixture
async def hr(session: AsyncSession, tenant: Tenant, hr_user: User) -> User:
    await _ai_on(session, hr_user)
    await _consent(session, tenant.id, hr_user.id)
    return await as_caller(session, hr_user)


async def _person(
    session: AsyncSession, tenant: Tenant, department: Department, code: str, email: str
) -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code,
        last_name="Test",
        work_email=email,
        designation="Staff",
        status=EmploymentStatus.ACTIVE,
        department_id=department.id,
    )
    session.add(person)
    await session.flush()
    return person


async def _employee_user(
    session: AsyncSession, tenant: Tenant, hr_user: User, person: Employee, email: str
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=email,
        password_hash="x",
        full_name=person.first_name,
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=person.id,
    )
    session.add(user)
    await session.commit()
    await _ai_on(session, hr_user)
    await _consent(session, tenant.id, person.id)
    return await as_caller(session, user)


async def _say(
    session: AsyncSession, user: User, words: str, model: FakeModel, conversation=None
) -> dict[str, Any]:
    with use_backend(model):
        return await companion.send(
            session,
            user,
            companion.MessageIn(text=words, conversation_id=conversation),
            request=None,
        )


def _cards(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result["messages"][1]["cards"]


def _offers(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in _cards(result) if c.get("type") == "proposal"]


async def _tools(session: AsyncSession, user: User) -> set[str]:
    # Actions need a model: offered as they would be with the workspace's AI on.
    with use_backend(FakeModel()):
        status = await companion.status_for(session, user)
    return {tool["key"] for tool in status["tools"]}


SECOND = {
    "role_title": "Sales Executive",
    "department": "sales west",
    "openings": "1",
    "ctc_max": "10 lakh",
    "justification": "Second seat for the Acme order, approved by the regional head.",
}


async def test_hr_raises_a_requisition_and_it_goes_for_approval_only_on_yes(
    session: AsyncSession, tenant: Tenant, hr: User, hiring: dict[str, Department]
) -> None:
    assert "raise_requisition" in await _tools(session, hr)
    model = FakeModel(plan("raise_requisition", "Here it is.", **SECOND))
    result = await _say(session, hr, "Raise the second requisition", model)
    [offer] = _offers(result)
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Role"] == "Sales Executive"
    assert figures["Department"] == "Sales West"
    assert figures["Seats"] == "1"
    assert figures["CTC ceiling"] == "₹10,00,000 a year each"
    assert any(n == "Approval path: HR head." for n in offer["notes"])
    assert any("no headcount budget" in n for n in offer["notes"])
    # What the approvers will be told is said before the yes.
    assert any(
        n.startswith("The approvers will also be told:") and "not against an approved position" in n
        for n in offer["notes"]
    )
    # Nothing is written before the yes.
    assert await session.scalar(select(Requisition.id)) is None

    done = await companion.confirm_offer(session, hr, uuid.UUID(offer["action_id"]), request=None)
    assert done["action"]["status"] == "done", done
    requisition = await session.scalar(select(Requisition))
    assert requisition is not None
    assert requisition.status is RequisitionStatus.PENDING_APPROVAL
    assert requisition.department_id == hiring["sales"].id
    assert requisition.budget_ctc_max == Decimal("1000000.00")
    assert requisition.raised_by_user_id == hr.id
    steps = (await session.scalars(select(RequisitionApproval))).all()
    assert [s.label for s in steps] == ["HR head"]
    assert requisition.code in done["action"]["result"]["text"]
    summaries = [
        row.summary
        for row in await session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "requisition")
        )
    ]
    assert any("raised as a draft" in s for s in summaries)
    assert any("sent for approval" in s for s in summaries)


async def test_missing_details_are_asked_for_and_nothing_is_offered(
    session: AsyncSession, hr: User, hiring: dict[str, Department]
) -> None:
    asked = {**SECOND, "ctc_max": "", "justification": ""}
    result = await _say(
        session, hr, "Raise a requisition", FakeModel(plan("raise_requisition", "", **asked))
    )
    assert not _offers(result)
    text = " ".join(c.get("text", "") for c in _cards(result))
    assert "yearly CTC ceiling" in text and "why the role is needed" in text


async def test_an_unknown_department_lists_the_real_ones(
    session: AsyncSession, hr: User, hiring: dict[str, Department]
) -> None:
    wrong = {**SECOND, "department": "Marketing"}
    result = await _say(session, hr, "raise it", FakeModel(plan("raise_requisition", "", **wrong)))
    assert not _offers(result)
    text = " ".join(c.get("text", "") for c in _cards(result))
    assert "no department called “Marketing”" in text
    assert "Sales West" in text and "Operations" in text


async def test_without_approval_rules_nothing_is_offered(
    session: AsyncSession, tenant: Tenant, hr: User, hiring: dict[str, Department]
) -> None:
    for rule in await session.scalars(select(ApprovalChainRule)):
        rule.is_active = False
    await session.commit()
    result = await _say(session, hr, "raise", FakeModel(plan("raise_requisition", "", **SECOND)))
    assert not _offers(result)
    assert "can't be sent for approval yet" in _cards(result)[0]["text"]


async def test_over_budget_is_said_in_the_offer(
    session: AsyncSession, tenant: Tenant, hr: User, hiring: dict[str, Department]
) -> None:
    year = financial_year_label(financial_year_bounds(date.today())[0])
    session.add(
        HeadcountBudget(
            tenant_id=tenant.id,
            financial_year=year,
            department_id=hiring["sales"].id,
            approved_positions=1,
            approved_cost=Decimal("500000"),
        )
    )
    await session.commit()
    result = await _say(session, hr, "raise", FakeModel(plan("raise_requisition", "", **SECOND)))
    [offer] = _offers(result)
    over = [n for n in offer["notes"] if n.startswith("Over Sales West's FY")]
    assert over, offer["notes"]
    # Said as HR's own check: the approvers are shown the flags, not this.
    assert "Only you see this check" in over[0]


async def test_an_employee_with_no_team_is_never_offered_it(
    session: AsyncSession, tenant: Tenant, hr_user: User, hiring: dict[str, Department]
) -> None:
    person = await _person(session, tenant, hiring["sales"], "EMP1", "emp1@acme.test")
    me = await _employee_user(session, tenant, hr_user, person, "emp1@acme.test")
    assert "raise_requisition" not in await _tools(session, me)


async def test_a_manager_raises_for_their_own_department_only(
    session: AsyncSession, tenant: Tenant, hr_user: User, hiring: dict[str, Department]
) -> None:
    boss = await _person(session, tenant, hiring["sales"], "MGR1", "mgr1@acme.test")
    report = await _person(session, tenant, hiring["sales"], "EMP2", "emp2@acme.test")
    report.manager_id = boss.id
    await session.commit()
    manager = await _employee_user(session, tenant, hr_user, boss, "mgr1@acme.test")
    assert "raise_requisition" in await _tools(session, manager)

    other = {**SECOND, "department": "Operations"}
    refused = await _say(
        session, manager, "raise", FakeModel(plan("raise_requisition", "", **other))
    )
    assert not _offers(refused)
    assert "own team" in _cards(refused)[0]["text"]

    own = {**SECOND, "department": ""}
    result = await _say(session, manager, "raise", FakeModel(plan("raise_requisition", "", **own)))
    [offer] = _offers(result)
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Department"] == "Sales West"
    # A manager is not shown the department's budget, and is not told a check they can't see.
    assert not any("budget" in n.lower() and "approvers see" in n.lower() for n in offer["notes"])
    assert not any(n.startswith(("Inside ", "Over ")) for n in offer["notes"])


async def test_requisitions_already_raised_count_against_the_budget(
    session: AsyncSession, tenant: Tenant, hr: User, hiring: dict[str, Department]
) -> None:
    year = financial_year_label(financial_year_bounds(date.today())[0])
    session.add(
        HeadcountBudget(
            tenant_id=tenant.id,
            financial_year=year,
            department_id=hiring["sales"].id,
            approved_positions=2,
            approved_cost=Decimal("4000000"),
        )
    )
    await session.commit()
    first = _offers(
        await _say(session, hr, "raise", FakeModel(plan("raise_requisition", "", **SECOND)))
    )
    assert any(n.startswith("Inside Sales West's FY") for n in first[0]["notes"]), first[0]["notes"]
    await companion.confirm_offer(session, hr, uuid.UUID(first[0]["action_id"]), request=None)
    two_more = {**SECOND, "role_title": "Area Manager", "openings": "2"}
    result = await _say(session, hr, "raise", FakeModel(plan("raise_requisition", "", **two_more)))
    [offer] = _offers(result)
    [over] = [n for n in offer["notes"] if n.startswith("Over Sales West's FY")]
    assert "counting the 1 seat already asked for in requisitions" in over

    # The budget answer counts them too, and says so.
    from app.services.companion import budget_tools

    cards, _ = await budget_tools.department_budget(
        session, hr, {"query": "Sales West", "hires": 2, "amount": "10 lakh"}, today=date.today()
    )
    record = next(c for c in cards if c.get("type") == "record")
    figures = {f["label"]: f["value"] for f in record["figures"]}
    assert figures["Asked for in requisitions"] == "1 seat(s)"
    assert "already asked for in requisitions" in (record["note"] or "")


async def test_a_department_without_a_budget_is_named_and_others_are_listed(
    session: AsyncSession, tenant: Tenant, hr: User, hiring: dict[str, Department]
) -> None:
    from app.services.companion import budget_tools

    year = financial_year_label(financial_year_bounds(date.today())[0])
    session.add(
        HeadcountBudget(
            tenant_id=tenant.id,
            financial_year=year,
            department_id=hiring["sales"].id,
            approved_positions=4,
            approved_cost=Decimal("4000000"),
        )
    )
    await session.commit()
    people = next(d for d in hiring.values() if d.id != hiring["sales"].id)
    cards, _ = await budget_tools.department_budget(
        session, hr, {"query": people.name}, today=date.today()
    )
    assert cards[0]["text"] == (
        f"{people.name} has no headcount budget for {year}. Budgets are set for: Sales West."
    )
    cards, _ = await budget_tools.department_budget(
        session, hr, {"query": "Marketing"}, today=date.today()
    )
    assert cards[0]["text"] == "There is no department called “Marketing”."


async def test_a_changed_request_replaces_the_earlier_offer(
    session: AsyncSession, tenant: Tenant, hr: User, hiring: dict[str, Department]
) -> None:
    from app.models.companion import CompanionAction, CompanionActionStatus

    first = await _say(session, hr, "raise", FakeModel(plan("raise_requisition", "", **SECOND)))
    [old] = _offers(first)
    conversation = uuid.UUID(first["conversation"]["id"])
    dearer = {**SECOND, "ctc_max": "12 lakh"}
    again = await _say(
        session, hr, "yes, but at 12 lakh", FakeModel(plan("raise_requisition", "", **dearer)),
        conversation,
    )  # fmt: skip
    [new] = _offers(again)
    assert new["action_id"] != old["action_id"]
    row = await session.get(CompanionAction, uuid.UUID(old["action_id"]))
    assert row is not None and row.status is CompanionActionStatus.CANCELLED
    assert row.decided_via == "replaced"
    # A different role in the same conversation is its own offer and stays.
    other = {**SECOND, "role_title": "Inside Sales Lead"}
    third = await _say(
        session, hr, "and one more", FakeModel(plan("raise_requisition", "", **other)), conversation
    )
    [lead] = _offers(third)
    still = await session.get(CompanionAction, uuid.UUID(new["action_id"]))
    assert still is not None and still.status is CompanionActionStatus.PROPOSED
    assert lead["action_id"] != new["action_id"]


@pytest.mark.parametrize("ctc", ["nan", "NaN lakh", "0.001", "inf", "-5 lakh"])
async def test_a_ctc_that_is_not_an_amount_is_asked_again(
    session: AsyncSession, tenant: Tenant, hr: User, hiring: dict[str, Department], ctc: str
) -> None:
    bad = {**SECOND, "ctc_max": ctc}
    result = await _say(session, hr, "raise", FakeModel(plan("raise_requisition", "", **bad)))
    assert not _offers(result)
    assert "yearly CTC ceiling" in _cards(result)[0]["text"]


def test_the_model_is_told_an_offer_carried_out_by_a_typed_yes_is_done() -> None:
    from app.services.companion import views

    done = {"type": "proposal", "action_id": "a-1", "status": "done", "title": "Requisition"}
    told = views.view_of([done], allowed=False)
    assert "Done: offer a-1 was carried out" in told and "do not ask them to confirm" in told
    waiting = {**done, "status": "proposed"}
    assert "Not done yet" in views.view_of([waiting], allowed=False)
    refused = {"type": "notice", "tone": "warning", "text": "Which requests?"}
    assert "Do not say anything was done" in views.view_of([refused], allowed=False)
