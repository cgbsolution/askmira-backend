"""Choosing the AI provider, and asking any figure of AskMira's data, on Postgres.

What these hold:

- the data tool's month, quarter and financial-year grouping runs on Postgres,
  leaves cancelled payroll runs out, and keeps groups of fewer than five people
  out of what the model reads while the person sees them all;
- an employee is answered from their own rows only, and never sees pay;
- a workspace's own API key is stored encrypted, shown back only as its last
  four characters, never written to the audit log, and answers in place of the
  server's; only an admin can set it;
- a workspace may name only a public https Ollama server;
- the transfer log names the provider that actually received the prompt.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.dpdp import CrossBorderTransfer
from app.models.employee import Employee, EmploymentStatus
from app.models.grounding import AgentInvocation
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.org import Department
from app.models.payroll import PayrollRun, PayrollRunStatus, PayrollRunType, Payslip
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.ai import client, governance
from app.services.ai.client import Completion, use_backend
from app.services.companion import data_tools, views
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    _ai_on,
    _consent,
    _say,
    text,
    tool_call,
)

pytestmark = requires_db

TODAY = date(2026, 10, 31)


async def _department(session: AsyncSession, tenant: Tenant, name: str) -> Department:
    row = Department(tenant_id=tenant.id, name=name, code=name[:3].upper())
    session.add(row)
    await session.flush()
    return row


async def _people(
    session: AsyncSession, tenant: Tenant, department: Department, count: int, prefix: str
) -> list[Employee]:
    people = [
        Employee(
            tenant_id=tenant.id,
            employee_code=f"{prefix}{i:03d}",
            first_name=f"{prefix}{i}",
            last_name="Test",
            work_email=f"{prefix.lower()}{i}@acme.test",
            department_id=department.id,
            status=EmploymentStatus.ACTIVE,
        )
        for i in range(count)
    ]
    session.add_all(people)
    await session.flush()
    return people


async def _payroll(
    session: AsyncSession,
    tenant: Tenant,
    entity_id: uuid.UUID,
    month: date,
    people: list[Employee],
    cost: int,
    status: PayrollRunStatus = PayrollRunStatus.PAID,
    run_type: PayrollRunType = PayrollRunType.REGULAR,
) -> None:
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=entity_id,
        period_year=month.year,
        period_month=month.month,
        effective_date=month,
        run_type=run_type,
        status=status,
    )
    session.add(run)
    await session.flush()
    for person in people:
        session.add(
            Payslip(
                tenant_id=tenant.id,
                run_id=run.id,
                employee_id=person.id,
                working_days=Decimal(30),
                paid_days=Decimal(30),
                gross_earnings=Decimal(cost),
                total_deductions=Decimal(0),
                net_pay=Decimal(cost),
                employer_cost=Decimal(cost),
                pf_employee=Decimal(0),
                pf_employer=Decimal(0),
            )
        )
    await session.flush()


@pytest.fixture
async def company(session: AsyncSession, tenant: Tenant, legal_entity: Any) -> dict[str, Any]:
    """Six engineers and one founder, paid in August and September; October's run cancelled."""
    engineering = await _department(session, tenant, "Engineering")
    founders = await _department(session, tenant, "Founders")
    engineers = await _people(session, tenant, engineering, 6, "ENG")
    [founder] = await _people(session, tenant, founders, 1, "FND")
    everyone = [*engineers, founder]
    await _payroll(session, tenant, legal_entity.id, date(2026, 8, 1), everyone, 50000)
    await _payroll(session, tenant, legal_entity.id, date(2026, 9, 1), everyone, 60000)
    await _payroll(
        session,
        tenant,
        legal_entity.id,
        date(2026, 10, 1),
        everyone,
        999999,
        status=PayrollRunStatus.CANCELLED,
    )
    await session.commit()
    return {"engineers": engineers, "founder": founder}


async def _ask(session: AsyncSession, user: User, **inputs: Any) -> list[dict[str, Any]]:
    return await data_tools.query_data(session, user, inputs, today=TODAY)


# ── The data tool on Postgres ────────────────────────────────────────────────


async def test_payroll_cost_by_month_leaves_cancelled_runs_out(
    session: AsyncSession, hr_user: User, company: dict[str, Any]
) -> None:
    hr = await as_caller(session, hr_user)
    chart, table = await _ask(
        session, hr, dataset="payslip", measures=["employer_cost:sum"], bucket="month", months=6
    )
    assert chart["type"] == "chart" and chart["chart"] == "line"
    # October's run was cancelled: not payroll, so not on the chart.
    assert chart["x"] == ["Aug 2026", "Sep 2026"]
    assert chart["series"][0]["values"] == ["350000.00", "420000.00"]
    assert table["rows"] == [
        ["Aug 2026", "₹3,50,000.00", "7"],
        ["Sep 2026", "₹4,20,000.00", "7"],
    ]
    seen = views.view_of([chart, table], allowed=True)
    assert "Aug 2026 | 350000.00 | 7" in seen and "changed by +20.0%" in seen
    assert "999999" not in seen


async def test_by_department_the_founders_pay_is_never_read(
    session: AsyncSession, hr_user: User, company: dict[str, Any]
) -> None:
    hr = await as_caller(session, hr_user)
    cards = await _ask(
        session,
        hr,
        dataset="payslip",
        measures=["employer_cost:sum"],
        group_by=["department"],
        chart="pie",
        **{"from": "2026-09-01", "to": "2026-09-30"},
    )
    chart, table = cards
    assert chart["chart"] == "pie" and set(chart["x"]) == {"Engineering", "Founders"}
    seen = views.view_of(cards, allowed=True)
    assert "Engineering | 360000.00 | 6" in seen
    assert "Founders" not in seen and "60000.00" not in seen.replace("360000.00", "")
    assert "1 group(s) cover fewer than 5 people" in seen
    # The admin sees both.
    assert {row[0] for row in table["rows"]} == {"Engineering", "Founders"}


async def test_quarters_and_financial_years_group_on_postgres(
    session: AsyncSession, hr_user: User, company: dict[str, Any]
) -> None:
    hr = await as_caller(session, hr_user)
    chart, _ = await _ask(
        session,
        hr,
        dataset="payslip",
        measures=["employer_cost:sum"],
        bucket="quarter",
        **{"from": "2026-04-01", "to": "2027-03-31"},
    )
    assert chart["x"] == ["Jul-Sep 2026"] and chart["series"][0]["values"] == ["770000.00"]
    chart, _ = await _ask(
        session,
        hr,
        dataset="payslip",
        measures=["employer_cost:sum"],
        bucket="financial_year",
        **{"from": "2026-04-01", "to": "2027-03-31"},
    )
    assert chart["x"] == ["FY 2026-27"]


async def test_a_mistake_in_the_question_is_said_plainly(
    session: AsyncSession, hr_user: User, company: dict[str, Any]
) -> None:
    hr = await as_caller(session, hr_user)
    [card] = await _ask(session, hr, dataset="payslip", measures=["shoe_size:sum"])
    assert card["type"] == "notice" and "not a field" in card["text"]
    [card] = await _ask(session, hr, dataset="nonsense")
    assert "Choose a dataset" in card["text"]
    [card] = await _ask(
        session,
        hr,
        dataset="leave_request",
        filters=[{"field": "leave_status", "op": "equals", "value": "maybe"}],
    )
    assert "Choose from" in card["text"]
    # The turn's transaction is still usable after a refused query.
    chart, _ = await _ask(session, hr, dataset="payslip", bucket="month", months=3)
    assert chart["type"] == "chart"


async def test_an_employee_gets_their_own_rows_and_never_pay(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    company: dict[str, Any],
) -> None:
    kind = LeaveType(tenant_id=tenant.id, name="Casual Leave", code="CL")
    session.add(kind)
    await session.flush()
    for person, day in ((employee, 3), (company["founder"], 4), (company["founder"], 10)):
        session.add(
            LeaveRequest(
                tenant_id=tenant.id,
                employee_id=person.id,
                leave_type_id=kind.id,
                start_date=date(2026, 9, day),
                end_date=date(2026, 9, day),
                total_days=Decimal(1),
                status=LeaveRequestStatus.APPROVED,
            )
        )
    user = User(
        tenant_id=tenant.id,
        email="anita@acme.test",
        password_hash="x",
        full_name="Anita Sharma",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    me = await as_caller(session, user)
    cards = await _ask(
        session, me, dataset="leave_request", measures=["total_days:sum"], bucket="month", months=3
    )
    chart = cards[0]
    assert chart["series"][0]["values"] == ["1.00"]  # her one day, not the founder's two
    assert "your own record" in chart["caption"]
    # One person's figures are hers to see, not the model's to read.
    assert "1.00" not in views.view_of(cards, allowed=True)
    [refused] = await _ask(session, me, dataset="payslip", measures=["employer_cost:sum"])
    assert refused["type"] == "notice" and "cannot see" in refused["text"]


# ── The workspace's provider and key ─────────────────────────────────────────


async def test_a_workspace_key_is_encrypted_masked_and_used(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    admin = await as_caller(session, hr_user)
    secret = "sk-proj-THIS-IS-SECRET-9876"
    await governance.set_settings(
        session,
        admin,
        governance.AiSettings(enabled=True, provider="openai", model="gpt-5-mini"),
        request=None,
    )
    saved = await governance.save_key(session, admin, "openai", secret, request=None)
    assert saved == {"provider": "openai", "workspace_key": "…9876"}

    await session.refresh(tenant)
    stored = tenant.settings["ai_keys"]["openai"]
    assert secret not in json.dumps(tenant.settings) and stored != secret

    route = await governance.route_for(session, admin)
    assert route.provider == "openai" and route.model == "gpt-5-mini"
    assert route.api_key == secret and route.key_source == "workspace"

    status = await governance.status_for(session, admin)
    assert status["configured"] and status["provider"] == "openai"
    assert secret not in json.dumps(status)
    openai = next(p for p in status["providers"] if p["key"] == "openai")
    assert openai["workspace_key"] == "…9876"

    audits = (await session.scalars(select(AuditLog))).all()
    assert all(secret not in json.dumps(a.changes or {}) + (a.summary or "") for a in audits)

    await governance.remove_key(session, admin, "openai", request=None)
    route = await governance.route_for(session, admin)
    assert route.key_source == "server"


async def test_only_an_admin_sets_a_key_and_only_a_public_ollama(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    admin = await as_caller(session, hr_user)
    user = User(
        tenant_id=tenant.id,
        email="anita@acme.test",
        password_hash="x",
        full_name="Anita",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    me = await as_caller(session, user)
    with pytest.raises(HTTPException) as refused:
        await governance.save_key(session, me, "openai", "sk-abcdefghijk", request=None)
    assert refused.value.status_code == 403
    for address in ("http://ollama.local", "https://192.168.1.20", "https://localhost:11434"):
        with pytest.raises(HTTPException) as bad:
            await governance.set_settings(
                session,
                admin,
                governance.AiSettings(enabled=True, provider="ollama", base_url=address),
                request=None,
            )
        assert bad.value.status_code == 422
    with pytest.raises(HTTPException, match="only for Ollama"):
        await governance.set_settings(
            session,
            admin,
            governance.AiSettings(enabled=True, provider="openai", base_url="https://8.8.8.8"),
            request=None,
        )
    with pytest.raises(ValueError):
        governance.AiSettings(provider="mystery")
    with pytest.raises(ValueError):
        governance.AiSettings(model="gpt 5; drop")


class _Ping:
    async def complete(self, **_: Any) -> Completion:
        return Completion(text='{"ok": true}', model="gpt-5-mini", input_tokens=9, output_tokens=2)


async def test_the_connection_test_asks_the_chosen_provider(
    session: AsyncSession, hr_user: User
) -> None:
    admin = await as_caller(session, hr_user)
    await governance.set_settings(
        session, admin, governance.AiSettings(enabled=True, provider="openai"), request=None
    )
    await governance.save_key(session, admin, "openai", "sk-abcdefghijkl", request=None)
    with use_backend(_Ping()):
        result = await governance.test_connection(session, admin)
    assert result["ok"] and result["provider"] == "openai" and "answered" in result["message"]


async def test_the_transfer_log_names_the_provider_that_answered(
    session: AsyncSession, tenant: Tenant, hr_user: User, company: dict[str, Any]
) -> None:
    admin = await as_caller(session, hr_user)
    await _ai_on(session, admin)
    await governance.set_settings(
        session, admin, governance.AiSettings(enabled=True, provider="gemini"), request=None
    )
    await governance.save_key(session, admin, "gemini", "AIza-test-key-123", request=None)
    await _consent(session, tenant.id, hr_user.id)
    admin = await as_caller(session, hr_user)
    model = FakeModel(
        [
            tool_call(
                "query_data",
                dataset="payslip",
                measures=["employer_cost:sum"],
                bucket="month",
                months=6,
            )
        ],
        "Payroll rose 20% in September.",
    )
    with use_backend(model):
        result = await _say(session, admin, "payroll cost for the last 6 months")
    cards = result["messages"][1]["cards"]
    assert cards[0]["type"] == "chart" and cards[0]["x"] == ["Aug 2026", "Sep 2026"]
    # The model read the totals over seven people, as organisational figures.
    seen = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Sep 2026 | 420000.00 | 7" in seen and "ENG1" not in seen
    transfer = (
        await session.scalars(
            select(CrossBorderTransfer).order_by(CrossBorderTransfer.created_at.desc())
        )
    ).first()
    assert transfer.sub_processor_key == "google_ai"
    assert set(transfer.data_categories) <= {"ai_interaction", "organisational"}
    invocation = (
        await session.scalars(select(AgentInvocation).order_by(AgentInvocation.created_at.desc()))
    ).first()
    assert invocation.sub_processor_key == "google_ai"
    client.set_route(None)


async def test_a_sub_admin_is_told_plainly_what_their_role_cannot_see(
    session: AsyncSession, tenant: Tenant, company: dict[str, Any]
) -> None:
    user = User(
        tenant_id=tenant.id,
        email="hrexec@acme.test",
        password_hash="x",
        full_name="HR Executive",
        role=UserRole.SUB_ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    sub_admin = await as_caller(session, user)
    [refused] = await _ask(
        session,
        sub_admin,
        dataset="payslip",
        measures=["employer_cost:sum"],
        group_by=["department"],
    )
    assert refused["type"] == "notice" and refused["text"] == "Your role cannot see employer cost."
    cards = await _ask(
        session,
        sub_admin,
        dataset="payslip",
        measures=["headcount:count_distinct", "employer_cost:sum"],
        group_by=["department"],
    )
    chart = cards[0]
    assert chart["type"] == "chart" and chart["series"][0]["name"] == "Headcount"
    assert any(
        "Left out, as your role cannot see them: employer cost." in c.get("text", "") for c in cards
    )


async def test_how_many_employees_counts_people_employed_now(
    session: AsyncSession, tenant: Tenant, hr_user: User, company: dict[str, Any]
) -> None:
    gone = company["engineers"][0]
    gone.status = EmploymentStatus.EXITED
    gone.date_of_joining = date(2026, 5, 4)
    gone.date_of_exit = date(2026, 9, 30)
    await session.commit()
    hr = await as_caller(session, hr_user)
    chart, table, note = await _ask(session, hr, dataset="employee", group_by=["department"])
    counts = {row[0]: row[1] for row in table["rows"]}
    assert counts["Engineering"] == "5" and counts["Founders"] == "1"
    assert "employed now" in note["text"]
    # Joiners by quarter still count the person who has since left.
    trend, _ = await _ask(
        session,
        hr,
        dataset="employee",
        bucket="quarter",
        time_field="date_of_joining",
        **{"from": "2026-04-01", "to": "2026-06-30"},
    )
    assert trend["x"] == ["Apr-Jun 2026"] and trend["series"][0]["values"] == ["1"]


async def test_joiners_and_exits_are_read_as_counts_never_names(
    session: AsyncSession, tenant: Tenant, hr_user: User, company: dict[str, Any]
) -> None:
    from app.services.companion import hr_tools

    for index, person in enumerate(company["engineers"]):
        person.date_of_joining = date(2026, 5, 4 + index)
    left = company["engineers"][0]
    left.status = EmploymentStatus.EXITED
    left.date_of_exit = date(2026, 9, 30)
    await session.commit()
    hr = await as_caller(session, hr_user)
    cards, _ = await hr_tools.people_changes(
        session, hr, {"from": "2026-04-01", "to": "2026-10-31"}, today=TODAY
    )
    seen = views.view_of(cards, allowed=True)
    assert "Joined: 6, of whom 5 are still employed and 1 have left or are leaving." in seen
    assert "Left: 1." in seen and "Engineering 6, 1" in seen
    # Nobody's name reaches the model; the person sees the lists.
    assert "ENG0" not in seen and "Test" not in seen
    assert any(card.get("title") == "Joiners" for card in cards)


async def test_the_payroll_on_no_account_is_read_as_a_company_total(
    session: AsyncSession, tenant: Tenant, hr_user: User, company: dict[str, Any]
) -> None:
    from app.models.accounts import AccountEntry, ClientAccount
    from app.services.companion import analysis_tools

    account = ClientAccount(tenant_id=tenant.id, code="ACME-01", name="Acme Bank")
    session.add(account)
    await session.flush()
    session.add(
        AccountEntry(
            tenant_id=tenant.id,
            account_id=account.id,
            kind="billing",
            period_year=2026,
            period_month=9,
            amount=Decimal("450000"),
        )
    )
    await session.commit()
    hr = await as_caller(session, hr_user)
    cards, _ = await analysis_tools.account_pnl(
        session, hr, {"from": "2026-08-01", "to": "2026-09-30"}, today=TODAY
    )
    seen = views.view_of(cards, allowed=True)
    assert "Acme Bank (ACME-01) | ₹4,50,000.00" in seen
    assert "Payroll cost in the period: ₹7,70,000.00 (7 people paid)" in seen
    assert "₹7,70,000.00 is mapped to no account" in seen


async def test_timestamps_group_by_the_day_it_was_in_india(
    session: AsyncSession, tenant: Tenant, hr_user: User, company: dict[str, Any]
) -> None:
    from app.models.culture import Shoutout, ShoutoutVisibility

    to = company["engineers"][1]
    # 20:00 UTC on 30 Sep is 01:30 on 1 Oct in India: an October shoutout.
    for moment in (
        datetime(2026, 9, 30, 20, 0, tzinfo=UTC),
        datetime(2026, 9, 15, 6, 0, tzinfo=UTC),
    ):
        session.add(
            Shoutout(
                tenant_id=tenant.id,
                from_employee_id=company["engineers"][2].id,
                to_employee_id=to.id,
                message="Great release",
                visibility=ShoutoutVisibility.COMPANY,
                created_at=moment,
            )
        )
    await session.commit()
    hr = await as_caller(session, hr_user)
    chart, _ = await _ask(
        session,
        hr,
        dataset="shoutout",
        measures=["shoutout_count:count"],
        bucket="month",
        **{"from": "2026-09-01", "to": "2026-10-31"},
    )
    assert chart["x"] == ["Sep 2026", "Oct 2026"]
    assert chart["series"][0]["values"] == ["1", "1"]
