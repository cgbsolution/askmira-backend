"""Zoho Books: connect with a Self Client code, then post an approved run's journal once.

Zoho is a double (httpx.MockTransport). Proved here: the code is swapped for a
refresh token and nothing is kept on a refusal, credentials never come back,
the journal posted is the same balanced journal the file exports produce, a
ledger Zoho does not have stops the post, and a run is never posted twice.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounting import LedgerMapping, LedgerNature, PayrollJournalPosting
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.payroll import PayrollRun, PayrollRunStatus, PayrollRunType, Payslip
from app.models.tenant import Tenant
from app.models.user import User
from app.services.connections import zoho_books
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


class FakeZoho:
    def __init__(self, organisations=None) -> None:
        self.organisations = organisations or [{"organization_id": "600", "name": "Acme Pvt Ltd"}]
        self.accounts = ["Salaries", "PF Payable", "Salary Payable"]
        self.journals: list[dict] = []
        self.reject_code = False
        self.reject_journal: str | None = None
        self.hosts: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.add(request.url.host)
        path = request.url.path
        if path == "/oauth/v2/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if form["grant_type"] == "authorization_code":
                if self.reject_code:
                    return httpx.Response(200, json={"error": "invalid_code"})
                return httpx.Response(
                    200,
                    json={
                        "access_token": "at-1",
                        "refresh_token": "rt-1",
                        "api_domain": "https://www.zohoapis.in",
                        "expires_in": 3600,
                    },
                )
            assert form["refresh_token"] == "rt-1"
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})
        assert request.headers["authorization"].startswith("Zoho-oauthtoken ")
        if path == "/books/v3/organizations":
            return httpx.Response(200, json={"code": 0, "organizations": self.organisations})
        if path == "/books/v3/chartofaccounts":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "chartofaccounts": [
                        {"account_id": f"A{i}", "account_name": name, "is_active": True}
                        for i, name in enumerate(self.accounts)
                    ],
                    "page_context": {"has_more_page": False},
                },
            )
        if path == "/books/v3/journals":
            if self.reject_journal:
                return httpx.Response(400, json={"code": 1001, "message": self.reject_journal})
            body = json.loads(request.content)
            body["organization_id"] = request.url.params.get("organization_id")
            self.journals.append(body)
            n = len(self.journals)
            return httpx.Response(
                201,
                json={"code": 0, "journal": {"journal_id": f"J{n}", "entry_number": f"JE-{n:04d}"}},
            )
        return httpx.Response(404, json={"code": 5, "message": "no such path"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


async def _approved_run(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, employee: Employee
) -> PayrollRun:
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=entity.id,
        period_year=2026,
        period_month=10,
        effective_date=date(2026, 10, 31),
        run_type=PayrollRunType.REGULAR,
        status=PayrollRunStatus.APPROVED,
    )
    session.add(run)
    await session.flush()
    session.add(
        Payslip(
            tenant_id=tenant.id,
            run_id=run.id,
            employee_id=employee.id,
            working_days=Decimal(31),
            paid_days=Decimal(31),
            gross_earnings=Decimal("50000.00"),
            total_deductions=Decimal("1800.00"),
            net_pay=Decimal("48200.00"),
            employer_cost=Decimal("50000.00"),
            line_items=[
                {"code": "BASIC", "name": "Basic", "type": "earning", "amount": "50000.00"},
                {"code": "PF_EMPLOYEE", "name": "PF", "type": "deduction", "amount": "1800.00"},
            ],
        )
    )
    for code, nature, debit, credit in (
        ("BASIC", LedgerNature.EARNING, "Salaries", None),
        ("PF_EMPLOYEE", LedgerNature.DEDUCTION, None, "PF Payable"),
        ("NET_PAY", LedgerNature.NET_PAY, None, "salary payable"),  # matched case-blind
    ):
        session.add(
            LedgerMapping(
                tenant_id=tenant.id,
                line_code=code,
                nature=nature,
                debit_ledger=debit,
                credit_ledger=credit,
            )
        )
    await session.commit()
    await session.refresh(run)
    return run


async def _connect(session, hr, zoho, **overrides):
    args = {
        "data_centre": "in",
        "client_id": "1000.CLIENT",
        "client_secret": "zoho-client-secret",
        "code": "1000.code.once",
    }
    args.update(overrides)
    result = await zoho_books.connect(session, hr, transport=zoho.transport, **args)
    await session.commit()
    return result


async def test_a_refused_code_keeps_nothing(session: AsyncSession, hr_user: User) -> None:
    hr = await as_caller(session, hr_user)
    zoho = FakeZoho()
    zoho.reject_code = True
    with pytest.raises(HTTPException) as caught:
        await _connect(session, hr, zoho)
    assert caught.value.status_code == 422 and "expired" in caught.value.detail
    assert (await zoho_books.status_for(session, hr))["connected"] is False

    with pytest.raises(HTTPException):
        await _connect(session, hr, zoho, data_centre="mars")


async def test_connect_then_post_once(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    hr = await as_caller(session, hr_user)
    zoho = FakeZoho()
    view = await _connect(session, hr, zoho)
    assert view["connected"] is True and view["needs_organisation"] is False
    assert view["account_label"] == "Acme Pvt Ltd"
    assert "zoho-client-secret" not in json.dumps(view) and "rt-1" not in json.dumps(view)
    assert zoho.hosts <= {"accounts.zoho.in", "www.zohoapis.in"}

    run = await _approved_run(session, tenant, legal_entity, employee)
    status_before = await zoho_books.run_posting_status(session, hr, run.id)
    assert status_before == {"connected": True, "organisation": "Acme Pvt Ltd", "posted": False}

    posted = await zoho_books.post_run(session, hr, run.id, transport=zoho.transport)
    assert posted["journal_number"] == "JE-0001" and posted["amount"] == "50000.00"

    [journal] = zoho.journals
    assert journal["organization_id"] == "600"
    assert journal["journal_date"] == "2026-10-31"
    assert journal["reference_number"].startswith("PAY-2026-10-")
    sides = {
        (li["account_id"], li["debit_or_credit"], li["amount"]) for li in journal["line_items"]
    }
    assert sides == {("A0", "debit", 50000.0), ("A1", "credit", 1800.0), ("A2", "credit", 48200.0)}

    # A second click: refused, and Zoho is not asked again.
    with pytest.raises(HTTPException) as again:
        await zoho_books.post_run(session, hr, run.id, transport=zoho.transport)
    assert again.value.status_code == 409 and "JE-0001" in again.value.detail
    assert len(zoho.journals) == 1
    count = await session.scalar(select(func.count()).select_from(PayrollJournalPosting))
    assert count == 1
    assert (await zoho_books.run_posting_status(session, hr, run.id))["posted"] is True


async def test_a_ledger_zoho_does_not_have_stops_the_post(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    hr = await as_caller(session, hr_user)
    zoho = FakeZoho()
    zoho.accounts = ["Salaries", "Salary Payable"]
    await _connect(session, hr, zoho)
    run = await _approved_run(session, tenant, legal_entity, employee)
    with pytest.raises(HTTPException) as caught:
        await zoho_books.post_run(session, hr, run.id, transport=zoho.transport)
    assert caught.value.status_code == 409 and "PF Payable" in caught.value.detail
    assert zoho.journals == []


async def test_zohos_refusal_is_shown_and_nothing_is_recorded(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    hr = await as_caller(session, hr_user)
    zoho = FakeZoho()
    await _connect(session, hr, zoho)
    run = await _approved_run(session, tenant, legal_entity, employee)
    zoho.reject_journal = "The journal date is in a locked period."
    with pytest.raises(HTTPException) as caught:
        await zoho_books.post_run(session, hr, run.id, transport=zoho.transport)
    assert caught.value.status_code == 502 and "locked period" in caught.value.detail
    assert await session.scalar(select(func.count()).select_from(PayrollJournalPosting)) == 0
    assert "locked period" in (await zoho_books.status_for(session, hr))["last_error"]


async def test_several_organisations_wait_for_a_choice(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    hr = await as_caller(session, hr_user)
    zoho = FakeZoho(
        organisations=[
            {"organization_id": "600", "name": "Acme Pvt Ltd"},
            {"organization_id": "700", "name": "Acme Exports LLP"},
        ]
    )
    view = await _connect(session, hr, zoho)
    assert view["needs_organisation"] is True
    run = await _approved_run(session, tenant, legal_entity, employee)
    with pytest.raises(HTTPException) as caught:
        await zoho_books.post_run(session, hr, run.id, transport=zoho.transport)
    assert caught.value.status_code == 409 and "organisation" in caught.value.detail

    with pytest.raises(HTTPException):
        await zoho_books.choose_organisation(session, hr, "999")
    chosen = await zoho_books.choose_organisation(session, hr, "700")
    await session.commit()
    assert chosen["organisation_id"] == "700" and chosen["account_label"] == "Acme Exports LLP"
    await zoho_books.post_run(session, hr, run.id, transport=zoho.transport)
    assert zoho.journals[0]["organization_id"] == "700"


async def test_an_unapproved_run_is_not_posted(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    hr = await as_caller(session, hr_user)
    zoho = FakeZoho()
    await _connect(session, hr, zoho)
    run = await _approved_run(session, tenant, legal_entity, employee)
    run.status = PayrollRunStatus.DRAFT
    await session.commit()
    with pytest.raises(HTTPException) as caught:
        await zoho_books.post_run(session, hr, run.id, transport=zoho.transport)
    assert caught.value.status_code in (404, 409)
    assert zoho.journals == []
