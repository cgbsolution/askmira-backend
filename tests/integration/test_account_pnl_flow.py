"""Client accounts and their P&L, end to end.

What these hold: an account's P&L is what was billed minus the payroll cost
mapped to it and its other direct costs; a person's cost is shared by the days
each mapping covers; a person is never mapped beyond 100%; billing is recorded
once it has happened, a cost is never negative, and nothing is zero; an
account with history cannot be deleted; an upload saves every row or none;
everything stays inside the tenant; and only admins and sub-admins with
payroll rights reach it.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from openpyxl import Workbook
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.payroll import PayrollRun, PayrollRunStatus, PayrollRunType, Payslip
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.accounts import (
    AccountEntryCreate,
    AllocationCreate,
    AllocationEnd,
    ClientAccountCreate,
    ClientAccountUpdate,
)
from app.services.accounts import service as accounts
from app.services.ai.client import use_backend
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    _bearer,
    _consent,
    _say,
    client,
    me,
    tool_call,
)

pytestmark = requires_db

SEP = date(2026, 9, 1)


@pytest.fixture
async def people(session: AsyncSession, tenant: Tenant, legal_entity: Any) -> dict[str, Employee]:
    out = {}
    for code, first in (("ACM101", "Kiran"), ("ACM102", "Lata")):
        person = Employee(
            tenant_id=tenant.id,
            employee_code=code,
            first_name=first,
            last_name="Rao",
            work_email=f"{first.lower()}@acme.test",
            legal_entity_id=legal_entity.id,
            status=EmploymentStatus.ACTIVE,
            date_of_joining=date(2025, 4, 1),
        )
        session.add(person)
        out[first.lower()] = person
    await session.commit()
    return out


async def _payroll(
    session: AsyncSession, tenant: Tenant, entity: Any, month: date, costs: dict[Employee, int]
) -> None:
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=entity.id,
        period_year=month.year,
        period_month=month.month,
        effective_date=month,
        run_type=PayrollRunType.REGULAR,
        status=PayrollRunStatus.PAID,
    )
    session.add(run)
    await session.flush()
    for person, cost in costs.items():
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
            )
        )
    await session.commit()


async def _account(session: AsyncSession, hr: User, code: str, name: str):
    return await accounts.create_account(
        session, hr, ClientAccountCreate(code=code, name=name), request=None
    )


async def test_the_pnl_shares_cost_by_the_days_each_mapping_covers(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: Any,
    people: dict[str, Employee],
) -> None:
    hr = await as_caller(session, hr_user)
    a = await _account(session, hr, "a-01", "Alpha Bank")
    b = await _account(session, hr, "B-02", "Beta Retail")
    assert a.code == "A-01"
    kiran, lata = people["kiran"], people["lata"]
    for account in (a, b):
        await accounts.create_allocation(
            session,
            hr,
            AllocationCreate(
                employee_id=kiran.id,
                account_id=account.id,
                percent=Decimal(50),
                effective_from=SEP,
            ),
            request=None,
        )
    # Lata joins Alpha from the 16th: 15 of September's 30 days.
    await accounts.create_allocation(
        session,
        hr,
        AllocationCreate(
            employee_id=lata.id,
            account_id=a.id,
            percent=Decimal(100),
            effective_from=date(2026, 9, 16),
        ),
        request=None,
    )
    await _payroll(session, tenant, legal_entity, SEP, {kiran: 100000, lata: 60000})
    for kind, amount, reference in (
        ("billing", "200000", "INV-901"),
        ("cost", "10000", "Travel"),
    ):
        await accounts.create_entry(
            session,
            hr,
            AccountEntryCreate(
                account_id=a.id,
                kind=kind,
                month="2026-09",
                amount=Decimal(amount),
                reference=reference,
            ),
            request=None,
        )

    result = await accounts.pnl(session, hr, start="2026-09", end="2026-09")
    rows = {r.code: r for r in result.accounts}
    alpha, beta = rows["A-01"], rows["B-02"]
    assert alpha.billed == Decimal("200000.00")
    assert alpha.people_cost == Decimal("80000.00")  # 50% of 1,00,000 + 15/30 of 60,000
    assert alpha.other_cost == Decimal("10000.00")
    assert alpha.margin == Decimal("110000.00") and alpha.margin_pct == Decimal("55.0")
    assert beta.people_cost == Decimal("50000.00") and beta.margin == Decimal("-50000.00")
    assert beta.margin_pct is None
    assert result.payroll_cost == Decimal("160000.00")
    assert result.unmapped_cost == Decimal("30000.00")  # Lata's first half of the month

    detail = await accounts.pnl_detail(session, hr, a.id, start="2026-09", end="2026-09")
    shares = {p.name: (p.share_pct, p.cost) for p in detail.people}
    assert shares["Kiran Rao"] == (Decimal("50.0"), Decimal("50000.00"))
    assert shares["Lata Rao"] == (Decimal("50.0"), Decimal("30000.00"))
    assert [m.month for m in detail.months] == ["2026-09"]

    created = (
        await session.scalars(select(AuditLog.entity_type).where(AuditLog.tenant_id == tenant.id))
    ).all()
    assert {"client_account", "account_allocation", "account_entry"} <= set(created)


async def test_the_rules_hold(
    session: AsyncSession, tenant: Tenant, hr_user: User, people: dict[str, Employee]
) -> None:
    hr = await as_caller(session, hr_user)
    a = await _account(session, hr, "A-01", "Alpha Bank")
    kiran = people["kiran"]
    with pytest.raises(HTTPException) as taken:
        await _account(session, hr, "a-01", "Alpha again")
    assert taken.value.status_code == 409

    await accounts.create_allocation(
        session,
        hr,
        AllocationCreate(
            employee_id=kiran.id, account_id=a.id, percent=Decimal(80), effective_from=SEP
        ),
        request=None,
    )
    b = await _account(session, hr, "B-02", "Beta Retail")
    with pytest.raises(HTTPException) as over:
        await accounts.create_allocation(
            session,
            hr,
            AllocationCreate(
                employee_id=kiran.id,
                account_id=b.id,
                percent=Decimal(30),
                effective_from=date(2026, 10, 1),
            ),
            request=None,
        )
    assert over.value.status_code == 422 and "110%" in over.value.detail
    # Ending the first mapping makes room from the next day.
    [first] = await accounts.list_allocations(session, hr, employee_id=kiran.id)
    await accounts.end_allocation(
        session, hr, first.id, AllocationEnd(last_day=date(2026, 9, 30)), request=None
    )
    moved = await accounts.create_allocation(
        session,
        hr,
        AllocationCreate(
            employee_id=kiran.id,
            account_id=b.id,
            percent=Decimal(30),
            effective_from=date(2026, 10, 1),
        ),
        request=None,
    )
    assert moved.percent == Decimal(30)
    with pytest.raises(HTTPException) as early:
        await accounts.create_allocation(
            session,
            hr,
            AllocationCreate(
                employee_id=kiran.id,
                account_id=a.id,
                percent=Decimal(10),
                effective_from=date(2024, 1, 1),
            ),
            request=None,
        )
    assert early.value.status_code == 422 and "joined" in early.value.detail

    # Money: nothing zero, no third decimal, no negative cost, no month ahead.
    for amount, kind in (("0", "billing"), ("10.005", "billing"), ("-5", "cost")):
        with pytest.raises(ValidationError):
            AccountEntryCreate(account_id=a.id, kind=kind, month="2026-09", amount=Decimal(amount))
    credit = AccountEntryCreate(
        account_id=a.id, kind="billing", month="2026-09", amount=Decimal("-2500.50")
    )
    assert credit.amount == Decimal("-2500.50")
    ahead = datetime.now(UTC).date().replace(day=1)
    ahead = ahead.replace(year=ahead.year + 1)
    with pytest.raises(HTTPException) as future:
        await accounts.create_entry(
            session,
            hr,
            AccountEntryCreate(
                account_id=a.id, kind="billing", month=f"{ahead:%Y-%m}", amount=Decimal(100)
            ),
            request=None,
        )
    assert future.value.status_code == 422
    with pytest.raises(ValidationError):
        AllocationCreate(
            employee_id=kiran.id, account_id=a.id, percent=Decimal(101), effective_from=SEP
        )

    # An account with history is kept: inactive, not deleted.
    with pytest.raises(HTTPException) as kept:
        await accounts.delete_account(session, hr, a.id, request=None)
    assert kept.value.status_code == 409
    updated = await accounts.update_account(
        session, hr, a.id, ClientAccountUpdate(is_active=False), request=None
    )
    assert updated.is_active is False
    empty = await _account(session, hr, "C-03", "Gamma")
    await accounts.delete_account(session, hr, empty.id, request=None)


def _sheet(rows: list[list[Any]]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.append(["Account code", "Type", "Month", "Amount", "Invoice no. / cost head"])
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


async def test_an_upload_saves_every_row_or_none(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    hr = await as_caller(session, hr_user)
    await _account(session, hr, "A-01", "Alpha Bank")
    bad = _sheet(
        [
            ["A-01", "Billing", "Sep 2026", "1,25,000", "INV-1"],
            ["Z-99", "Billing", "2026-09", "100", "INV-2"],
            ["A-01", "Cost", "2026-09", "abc", "Travel"],
            [None, None, None, None, None],
        ]
    )
    checked = await accounts.import_entries(
        session, hr, bad, "billing.xlsx", apply=False, request=None
    )
    assert [r.status for r in checked.rows] == ["ready", "error", "error", "skipped"]
    assert checked.rows[0].amount == Decimal("125000") and checked.rows[0].month == "2026-09"
    assert "No account has the code Z-99" in checked.rows[1].message
    with pytest.raises(HTTPException) as refused:
        await accounts.import_entries(session, hr, bad, "billing.xlsx", apply=True, request=None)
    assert refused.value.status_code == 422
    assert await accounts.list_entries(session, hr) == []

    good = _sheet(
        [
            ["A-01", "Billing", "Sep 2026", "1,25,000", "INV-1"],
            ["a-01", "cost", "09/2026", 4000.5, "Travel"],
        ]
    )
    saved = await accounts.import_entries(
        session, hr, good, "billing.xlsx", apply=True, request=None
    )
    assert saved.saved == 2
    entries = await accounts.list_entries(session, hr)
    assert sorted(e.amount for e in entries) == [Decimal("4000.50"), Decimal("125000.00")]
    with pytest.raises(HTTPException):
        await accounts.import_entries(
            session, hr, b"not a sheet", "billing.pdf", apply=False, request=None
        )
    template = await accounts.template(session, hr)
    assert template[:2] == b"PK"


async def test_accounts_stay_inside_the_tenant_and_behind_payroll_roles(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    me: User,
) -> None:
    hr = await as_caller(session, hr_user)
    mine = await _account(session, hr, "A-01", "Alpha Bank")
    listed = await client.get("/api/v1/accounts", headers=_bearer(hr_user))
    assert listed.status_code == 200 and [a["code"] for a in listed.json()] == ["A-01"]

    theirs = await client.get("/api/v1/accounts", headers=_bearer(other_hr_user))
    assert theirs.status_code == 200 and theirs.json() == []
    peek = await client.get(f"/api/v1/accounts/{mine.id}/pnl", headers=_bearer(other_hr_user))
    assert peek.status_code == 404
    poke = await client.patch(
        f"/api/v1/accounts/{mine.id}", json={"name": "Mine now"}, headers=_bearer(other_hr_user)
    )
    assert poke.status_code == 404

    employee = await client.get("/api/v1/accounts/pnl", headers=_bearer(me))
    assert employee.status_code == 403
    pnl = await client.get(
        "/api/v1/accounts/pnl",
        params={"start": "2026-04", "end": "2026-09"},
        headers=_bearer(hr_user),
    )
    assert pnl.status_code == 200 and pnl.json()["months"][0] == "2026-04"
    reversed_span = await client.get(
        "/api/v1/accounts/pnl",
        params={"start": "2026-09", "end": "2026-04"},
        headers=_bearer(hr_user),
    )
    assert reversed_span.status_code == 422
    made = await client.post(
        "/api/v1/accounts/entries",
        json={"account_id": str(mine.id), "kind": "billing", "month": "2026-09", "amount": "5e3"},
        headers=_bearer(hr_user),
    )
    assert made.status_code == 201, made.text
    assert made.json()["amount"] in ("5000.00", "5000")
    nonsense = await client.post(
        "/api/v1/accounts/entries",
        json={
            "account_id": str(uuid.uuid4()),
            "kind": "billing",
            "month": "2026-09",
            "amount": "10",
        },
        headers=_bearer(hr_user),
    )
    assert nonsense.status_code == 404


async def test_mira_answers_an_account_pnl(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: Any,
    people: dict[str, Employee],
) -> None:
    hr = await as_caller(session, hr_user)
    a = await _account(session, hr, "A-01", "Alpha Bank")
    await accounts.create_allocation(
        session,
        hr,
        AllocationCreate(
            employee_id=people["kiran"].id,
            account_id=a.id,
            percent=Decimal(100),
            effective_from=SEP,
        ),
        request=None,
    )
    await _payroll(session, tenant, legal_entity, SEP, {people["kiran"]: 90000})
    await accounts.create_entry(
        session,
        hr,
        AccountEntryCreate(
            account_id=a.id, kind="billing", month="2026-09", amount=Decimal(150000)
        ),
        request=None,
    )
    await _consent(session, tenant.id, hr_user.id)
    from app.services.ai import governance

    await governance.set_settings(session, hr, governance.AiSettings(enabled=True), request=None)
    model = FakeModel(
        [tool_call("account_pnl", query="alpha", **{"from": "2026-09-01", "to": "2026-09-30"})],
        "Alpha Bank made a 40% margin in September.",
    )
    with use_backend(model):
        result = await _say(session, hr, "P&L for Alpha Bank in September")
    chart, record, table = result["messages"][1]["cards"]
    figures = {f["label"]: f["value"] for f in record["figures"]}
    assert figures["Billed"] == "₹1,50,000.00" and figures["People cost"] == "₹90,000.00"
    assert figures["Margin"] == "₹60,000.00" and figures["Margin %"] == "40.0%"
    assert table["rows"][0][:2] == ["Kiran Rao", "ACM101"]
