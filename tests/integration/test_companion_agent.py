"""Ask Mira as a conversation: several steps a turn, offers the person confirms, trends.

What these hold:

- a leave request is checked (type, balance, days) and offered, and nothing is
  sent for approval until the person says yes in a later message or presses
  Confirm; then the leave module's own `apply` raises it, as them, with its
  approval chain and an audit row marked "via ask_mira";
- the model cannot confirm in the turn it made the offer, nor on a message that
  is not a yes; a decided, expired or someone else's offer cannot be confirmed;
- a manager approves a waiting request through Mira, and it is decided by the
  leave module's own `decide`;
- the model reads a tool's result only when the DPDP register lets the model
  provider receive every category in it, and the workspace has not switched
  reading off; otherwise it is only told the result is on screen;
- payroll and headcount trends add up to the payslips and the employee records.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.companion import CompanionAction, CompanionActionStatus
from app.models.dpdp import CrossBorderTransfer
from app.models.employee import Employee
from app.models.leave import LeavePolicy, LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.payroll import PayrollRun, PayrollRunStatus, PayrollRunType, Payslip
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.leave import LeaveRequestCreate
from app.services.ai.client import use_backend
from app.services.companion import config as companion_config
from app.services.companion import service as companion
from app.services.dpdp import transfers
from app.services.leave import service as leave_service
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    _ai_on,
    _bearer,
    _consent,
    _say,
    client,
    manager,
    me,
    text,
    tool_call,
)

pytestmark = requires_db

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _weekday_ahead(days: int) -> date:
    day = datetime.now(UTC).date() + timedelta(days=days)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _offer_id(messages: list[dict[str, Any]]) -> str:
    """The latest offer id the model was shown (in a tool result or the offers line)."""
    found = _UUID.findall(str(messages[-1]))
    assert found, "no offer id was shown to the model"
    return found[-1]


def _confirm(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [tool_call("confirm_action", action_id=_offer_id(messages))]


@pytest.fixture
async def casual(session: AsyncSession, tenant: Tenant) -> LeaveType:
    kind = LeaveType(tenant_id=tenant.id, name="Casual Leave", code="CL")
    session.add(kind)
    await session.flush()
    session.add(
        LeavePolicy(
            tenant_id=tenant.id,
            leave_type_id=kind.id,
            name="Casual leave - everyone",
            annual_quota_days=Decimal(12),
            accrual_frequency="annual",
            effective_from=date(2020, 1, 1),
        )
    )
    await session.commit()
    return kind


@pytest.fixture
def widened(monkeypatch: pytest.MonkeyPatch) -> None:
    """The register as it would read once a customer lets the model read HR records."""
    spec = transfers.SUB_PROCESSORS["anthropic"]
    monkeypatch.setitem(
        transfers.SUB_PROCESSORS,
        "anthropic",
        replace(
            spec,
            categories_permitted=(
                *spec.categories_permitted,
                "identity",
                "compensation",
                "leave",
                "attendance",
            ),
        ),
    )


async def _requests(session: AsyncSession, employee_id: uuid.UUID) -> list[LeaveRequest]:
    return list(
        (
            await session.scalars(
                select(LeaveRequest).where(LeaveRequest.employee_id == employee_id)
            )
        ).all()
    )


# ── Leave: check, offer, confirm ─────────────────────────────────────────────


async def test_leave_is_offered_and_sent_only_when_the_person_says_yes(
    session: AsyncSession, me: User, manager: Employee, casual: LeaveType
) -> None:
    day = _weekday_ahead(9)
    span = {"from": day.isoformat(), "to": day.isoformat()}
    model = FakeModel(
        [tool_call("apply_leave", **span)],
        "Which leave should it be?",
        [tool_call("apply_leave", leave_type="casual", **span)],
        "This is one day of Casual Leave. Shall I send it for approval?",
        _confirm,
        "Done: it is waiting for approval.",
    )
    with use_backend(model):
        first = await _say(session, me, f"I want to mark a leave on {day:%d %B}")
        conversation = first["conversation"]["id"]
        # No type given: the balances to choose from, and no offer yet.
        balances = first["messages"][1]["cards"][0]
        assert balances["type"] == "record"
        assert balances["table"]["rows"][0][:3] == ["Casual Leave", "CL", "12"]
        assert (await session.scalars(select(CompanionAction))).all() == []

        second = await _say(session, me, "casual", conversation)
        offer = second["messages"][1]["cards"][0]
        assert offer["type"] == "proposal" and offer["status"] == "proposed"
        figures = {f["label"]: f["value"] for f in offer["figures"]}
        assert figures["Leave"] == "Casual Leave"
        assert figures["Days counted"] == "1"
        assert figures["Available now"] == "12" and figures["Left after this"] == "11"
        # Offered, not applied.
        assert await _requests(session, me.employee_id) == []

        third = await _say(session, me, "yes please", conversation)

    done = third["messages"][1]["cards"][0]
    assert done["type"] == "proposal" and done["status"] == "done"
    assert "approval" in done["result"]["text"]
    [request] = await _requests(session, me.employee_id)
    assert request.status is LeaveRequestStatus.PENDING
    assert request.start_date == day and request.total_days == Decimal(1)
    action = (await session.scalars(select(CompanionAction))).one()
    assert action.status is CompanionActionStatus.DONE and action.decided_via == "message"
    # The leave module's own audit row, marked as raised through Ask Mira.
    row = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "leave_request"))
    ).one()
    assert row.changes["via"] == "ask_mira" and row.actor_user_id == me.id
    # Reopened, the offer shows what became of it.
    opened = await companion.get_conversation(session, me, uuid.UUID(conversation))
    cards = [c for m in opened["messages"] for c in m["cards"] if c["type"] == "proposal"]
    assert {c["status"] for c in cards} == {"done"}
    # The steps it took are on the reply.
    assert [s["tool"] for s in second["messages"][1]["steps"]] == ["apply_leave"]


async def test_the_model_cannot_confirm_in_the_turn_it_offered(
    session: AsyncSession, me: User, casual: LeaveType
) -> None:
    day = _weekday_ahead(9)
    model = FakeModel(
        [tool_call("apply_leave", leave_type="CL", **{"from": day.isoformat()})],
        _confirm,
        "Done.",
    )
    with use_backend(model):
        result = await _say(session, me, f"apply casual leave on {day:%d %b} and confirm it")
    cards = result["messages"][1]["cards"]
    assert cards[0]["type"] == "proposal"
    assert cards[1]["type"] == "notice" and "not answered this offer yet" in cards[1]["text"]
    assert await _requests(session, me.employee_id) == []
    action = (await session.scalars(select(CompanionAction))).one()
    assert action.status is CompanionActionStatus.PROPOSED


async def test_a_change_of_mind_is_not_a_yes(
    session: AsyncSession, me: User, casual: LeaveType
) -> None:
    day = _weekday_ahead(9)
    model = FakeModel(
        [tool_call("apply_leave", leave_type="CL", **{"from": day.isoformat()})],
        "Shall I send it?",
        _confirm,
        "Sorry.",
    )
    with use_backend(model):
        first = await _say(session, me, f"casual leave on {day:%d %b}")
        second = await _say(
            session, me, "no wait, make it the next day", first["conversation"]["id"]
        )
    refused = second["messages"][1]["cards"][0]
    assert refused["type"] == "notice" and "not a clear yes" in refused["text"]
    assert await _requests(session, me.employee_id) == []


async def test_leave_beyond_the_balance_is_not_offered(
    session: AsyncSession, me: User, casual: LeaveType
) -> None:
    start = _weekday_ahead(20)
    end = start + timedelta(days=27)
    model = FakeModel(
        [
            tool_call(
                "apply_leave",
                leave_type="CL",
                **{"from": start.isoformat(), "to": end.isoformat()},
            )
        ],
        "You do not have enough casual leave for that.",
    )
    with use_backend(model):
        result = await _say(session, me, "four weeks of casual leave")
    card = result["messages"][1]["cards"][0]
    assert card["type"] == "record" and card["title"] == "This leave cannot be applied"
    assert (await session.scalars(select(CompanionAction))).all() == []


async def test_confirm_and_cancel_buttons_over_http(
    client: httpx.AsyncClient, session: AsyncSession, me: User, hr_user: User, casual: LeaveType
) -> None:
    day = _weekday_ahead(9)
    later = _weekday_ahead(16)
    model = FakeModel(
        [tool_call("apply_leave", leave_type="CL", **{"from": day.isoformat()})],
        "Press Confirm to send it.",
        [tool_call("apply_leave", leave_type="CL", **{"from": later.isoformat()})],
        "Press Confirm to send it.",
    )
    with use_backend(model):
        first = await client.post(
            "/api/v1/companion/messages", json={"text": "leave"}, headers=_bearer(me)
        )
        conversation = first.json()["conversation"]["id"]
        second = await client.post(
            "/api/v1/companion/messages",
            json={"text": "and another", "conversation_id": conversation},
            headers=_bearer(me),
        )
    one = first.json()["messages"][1]["cards"][0]["action_id"]
    two = second.json()["messages"][1]["cards"][0]["action_id"]

    # Someone else cannot confirm it, even an admin.
    other = await client.post(f"/api/v1/companion/actions/{one}/confirm", headers=_bearer(hr_user))
    assert other.status_code == 404

    confirmed = await client.post(f"/api/v1/companion/actions/{one}/confirm", headers=_bearer(me))
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["action"]["status"] == "done"
    assert body["message"]["role"] == "assistant" and "approval" in body["message"]["text"]
    again = await client.post(f"/api/v1/companion/actions/{one}/confirm", headers=_bearer(me))
    assert again.status_code == 409

    declined = await client.post(f"/api/v1/companion/actions/{two}/cancel", headers=_bearer(me))
    assert declined.status_code == 200 and declined.json()["action"]["status"] == "cancelled"
    late = await client.post(f"/api/v1/companion/actions/{two}/confirm", headers=_bearer(me))
    assert late.status_code == 409

    requests = await _requests(session, me.employee_id)
    assert [r.start_date for r in requests] == [day]


async def test_an_expired_offer_is_asked_again(
    session: AsyncSession, me: User, casual: LeaveType
) -> None:
    day = _weekday_ahead(9)
    model = FakeModel(
        [tool_call("apply_leave", leave_type="CL", **{"from": day.isoformat()})], "Confirm?"
    )
    with use_backend(model):
        await _say(session, me, "leave")
    action = (await session.scalars(select(CompanionAction))).one()
    action.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()
    with pytest.raises(Exception) as refused:
        await companion.confirm_offer(session, me, action.id, request=None)
    assert getattr(refused.value, "status_code", None) == 409
    assert await _requests(session, me.employee_id) == []


# ── Approvals ───────────────────────────────────────────────────────────────


async def test_a_manager_approves_through_mira(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    manager: Employee,
    casual: LeaveType,
    widened: None,
) -> None:
    day = _weekday_ahead(9)
    boss_login = User(
        tenant_id=tenant.id,
        email="rohan.verma@acme.test",
        password_hash="x",
        full_name="Rohan Verma",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=manager.id,
    )
    session.add(boss_login)
    await session.commit()
    await _consent(session, tenant.id, manager.id)
    # Raised once Rohan has a login, so his is the approval it waits on.
    await leave_service.apply(
        session,
        me,
        LeaveRequestCreate(leave_type_id=casual.id, start_date=day, end_date=day),
    )
    boss = await as_caller(session, boss_login)

    def decide(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ref = re.search(r"leave:[0-9a-f-]{36}", str(messages[-1]))
        assert ref, "the approvals result carries each request's ref"
        return [tool_call("decide_approvals", refs=ref.group(0), decision="approve")]

    model = FakeModel(
        [tool_call("my_approvals", kind="leave")],
        decide,
        "Approve Anita's casual leave?",
        _confirm,
        "Approved.",
    )
    with use_backend(model):
        first = await _say(session, boss, "approve anita's leave")
        offer = first["messages"][1]["cards"][1]
        assert offer["type"] == "proposal" and offer["figures"][0]["label"] == "Anita Sharma"
        second = await _say(session, boss, "yes", first["conversation"]["id"])
    assert second["messages"][1]["cards"][0]["status"] == "done"
    [request] = await _requests(session, me.employee_id)
    assert request.status is LeaveRequestStatus.APPROVED


# ── What the model may read ──────────────────────────────────────────────────


async def _run_payroll(
    session: AsyncSession,
    tenant: Tenant,
    entity_id: uuid.UUID,
    month: date,
    slips: list[tuple[Employee, int]],
) -> None:
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=entity_id,
        period_year=month.year,
        period_month=month.month,
        effective_date=month,
        run_type=PayrollRunType.REGULAR,
        status=PayrollRunStatus.PAID,
    )
    session.add(run)
    await session.flush()
    for person, cost in slips:
        session.add(
            Payslip(
                tenant_id=tenant.id,
                run_id=run.id,
                employee_id=person.id,
                working_days=Decimal(30),
                paid_days=Decimal(30),
                gross_earnings=Decimal(cost - 2000),
                total_deductions=Decimal(3000),
                net_pay=Decimal(cost - 5000),
                employer_cost=Decimal(cost),
                pf_employee=Decimal(1800),
                pf_employer=Decimal(1800),
            )
        )
    await session.commit()


async def _hr(session: AsyncSession, hr_user: User) -> User:
    """The admin, with AI on for the workspace and their own consent given."""
    await _ai_on(session, hr_user)
    await _consent(session, hr_user.tenant_id, hr_user.id)
    return await as_caller(session, hr_user)


async def test_payroll_trend_adds_up_and_is_not_read_unless_registered(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    me: User,
    employee: Employee,
    manager: Employee,
    legal_entity: Any,
) -> None:
    await _run_payroll(session, tenant, legal_entity.id, date(2026, 7, 1), [(employee, 50000)])
    await _run_payroll(
        session, tenant, legal_entity.id, date(2026, 8, 1), [(employee, 50000), (manager, 90000)]
    )
    await _run_payroll(
        session, tenant, legal_entity.id, date(2026, 9, 1), [(employee, 55000), (manager, 90000)]
    )
    hr = await _hr(session, hr_user)
    ask = [tool_call("payroll_trend", **{"from": "2026-07-01", "to": "2026-09-30"})]
    model = FakeModel(ask, "Payroll cost rose in August when Rohan joined payroll.")
    with use_backend(model):
        result = await _say(session, hr, "payroll cost trend jul to sep")
    chart, record = result["messages"][1]["cards"]
    assert chart["type"] == "chart" and chart["x"] == ["Jul 2026", "Aug 2026", "Sep 2026"]
    assert chart["series"][0]["values"] == ["50000.00", "140000.00", "145000.00"]
    figures = {f["label"]: f["value"] for f in record["figures"]}
    assert figures["Total cost"] == "₹3,35,000.00"
    assert record["table"]["rows"][2][-1] == "+3.6%"
    # Pay is a category the model provider is not registered for: on screen only.
    tool_result = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "140000" not in tool_result and "You may not read" in tool_result
    sent = (await session.scalars(select(CrossBorderTransfer))).all()
    assert all("compensation" not in t.data_categories for t in sent)

    # An employee is not offered it at all.
    status = await companion.status_for(session, me)
    assert "payroll_trend" not in {t["key"] for t in status["tools"]}


async def test_with_the_register_widened_the_model_reads_the_figures(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: Any,
    widened: None,
) -> None:
    await _run_payroll(session, tenant, legal_entity.id, date(2026, 8, 1), [(employee, 50000)])
    await _run_payroll(session, tenant, legal_entity.id, date(2026, 9, 1), [(employee, 61000)])
    hr = await _hr(session, hr_user)
    model = FakeModel(
        [tool_call("payroll_trend", **{"from": "2026-08-01", "to": "2026-09-30"})],
        "Cost rose 22% in September.",
    )
    with use_backend(model):
        await _say(session, hr, "why did payroll go up in september")
    seen = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Aug 2026 50000.00, Sep 2026 61000.00" in seen
    transfer = (
        await session.scalars(
            select(CrossBorderTransfer).order_by(CrossBorderTransfer.created_at.desc())
        )
    ).first()
    assert "compensation" in transfer.data_categories


async def test_headcount_trend_is_read_and_reading_can_be_switched_off(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, manager: Employee
) -> None:
    manager.date_of_joining = date(2026, 8, 10)
    await session.commit()
    hr = await _hr(session, hr_user)
    ask = [tool_call("headcount_trend", **{"from": "2026-07-01", "to": "2026-09-30"})]
    model = FakeModel(ask, "One person joined in August.")
    with use_backend(model):
        result = await _say(session, hr, "headcount trend this quarter")
    record = result["messages"][1]["cards"][1]
    assert [row[2] for row in record["table"]["rows"]] == [0, 1, 0]
    seen = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Joined: Jul 2026 0, Aug 2026 1, Sep 2026 0" in seen

    await companion_config.set_config(
        session, hr, companion_config.CompanionConfig(ai_reads_results=False), request=None
    )
    model = FakeModel(ask, "It is below.")
    with use_backend(model):
        await _say(session, hr, "headcount trend this quarter")
    seen = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Aug 2026 1" not in seen and "You may not read" in seen


async def test_action_tools_need_ai_and_are_switched_per_workspace(
    session: AsyncSession, me: User, hr_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    with use_backend(FakeModel()):
        status = await companion.status_for(session, me)
    keys = {t["key"] for t in status["tools"]}
    assert {"apply_leave", "cancel_leave", "confirm_action", "cancel_action"} <= keys
    await companion_config.set_config(
        session,
        hr_user,
        companion_config.CompanionConfig(
            tools={
                "actions_leave": False,
                "actions_attendance": False,
                "actions_approvals": False,
                "actions_helpdesk": False,
            }
        ),
        request=None,
    )
    with use_backend(FakeModel()):
        keys = {t["key"] for t in (await companion.status_for(session, me))["tools"]}
    assert not keys & {"apply_leave", "confirm_action", "cancel_action", "decide_approvals"}

    from app.services.ai import client as ai_client

    monkeypatch.setattr(ai_client.settings, "ANTHROPIC_API_KEY", "")
    await companion_config.set_config(
        session, hr_user, companion_config.CompanionConfig(), request=None
    )
    direct = await companion.status_for(session, me)
    assert direct["mode"] == "direct"
    assert "apply_leave" not in {t["key"] for t in direct["tools"]}


async def test_an_employee_without_a_record_is_told_plainly(
    session: AsyncSession, tenant: Tenant, hr_user: User, casual: LeaveType
) -> None:
    hr = await _hr(session, hr_user)
    day = _weekday_ahead(9)
    model = FakeModel(
        [tool_call("apply_leave", leave_type="CL", **{"from": day.isoformat()})],
        "Your login has no employee record.",
    )
    with use_backend(model):
        result = await _say(session, hr, "leave for me")
    card = result["messages"][1]["cards"][0]
    assert card["type"] == "notice" and "not linked to an employee record" in card["text"]
    assert (await session.scalars(select(CompanionAction))).all() == []
