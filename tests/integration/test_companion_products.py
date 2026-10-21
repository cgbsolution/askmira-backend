"""Ask Mira reading the linked products: AnytimeHire (hiring) and ExpenseGenie (expenses).

What these hold: a product tool is offered only where the workspace has the
product linked and configured and the person has a role in it; the product is
asked for the caller's own workspace and as the caller, with a signed `query`
token bound to the body; HR-only views are refused before anything is asked;
what the product sends is shown to the person and never sent to the model;
judgement-like interview statuses are shown as observations; and a product
that can't answer is said plainly.
"""

from __future__ import annotations

import hashlib
import json
import types
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User
from app.services.ai.client import use_backend
from app.services.companion import service as companion
from app.services.products import client as product_client
from app.services.products import settings as product_settings
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    _consent,
    me,
    plan,
)

pytestmark = requires_db

SECRETS = {
    "interview": "interview-secret-0123456789abcdef0123456789",
    "expense": "expense-secret-0123456789abcdef0123456789ab",
}
PRODUCT_TOOLS = {
    "hiring_overview",
    "hiring_jobs",
    "hiring_candidates",
    "hiring_interviews",
    "expense_claims",
    "expense_summary",
}
IST = ZoneInfo("Asia/Kolkata")


class FakeProducts:
    """Both products' query endpoint, checking every call as a product must."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: dict[tuple[str, str], Any] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        product = "interview" if request.url.host == "interview-api.test" else "expense"
        assert request.url.path == "/integrations/askmira/query"
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token,
            SECRETS[product],
            algorithms=["HS256"],
            audience=f"askmira:{product}",
            issuer="askmira",
        )
        assert claims["use"] == "query"
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        body = json.loads(request.content)
        assert body["tenant_id"] == claims["sub"]
        self.calls.append({"product": product, "body": body})
        answer = self.answers.get((product, body["kind"]), {})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={"kind": body["kind"], **answer})


@pytest.fixture
def products(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProducts]:
    for product, prefix in (("interview", "INTERVIEW_TOOL"), ("expense", "EXPENSE_TOOL")):
        monkeypatch.setenv(f"{prefix}_API_URL", f"https://{product}-api.test")
        monkeypatch.setenv(f"{prefix}_APP_URL", f"https://{product}.test")
        monkeypatch.setenv(f"{prefix}_SECRET", SECRETS[product])
    product_settings.reset_cache()
    fake = FakeProducts()
    real = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        product_client,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=client_factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout
        ),
    )
    yield fake
    product_settings.reset_cache()


async def _link(session: AsyncSession, tenant: Tenant, *keys: str, status: str = "active") -> None:
    for key in keys:
        session.add(
            ProductLink(tenant_id=tenant.id, product=key, status=status, external_ref=f"{key}-1")
        )
    settings = dict(tenant.settings or {})
    settings["linked_products"] = {key: True for key in keys} if status == "active" else {}
    tenant.settings = settings
    await session.commit()


async def _offered(session: AsyncSession, user: User) -> set[str]:
    status = await companion.status_for(session, user)
    return {tool["key"] for tool in status["tools"]} & PRODUCT_TOOLS


async def _say(session: AsyncSession, user: User, text: str) -> dict[str, Any]:
    result = await companion.send(session, user, companion.MessageIn(text=text), request=None)
    return result["messages"][1]


@pytest.fixture
async def hr(session: AsyncSession, hr_user: User, me: User) -> User:  # noqa: F811
    """HR, with Ask Mira on (the `me` fixture switches it on) and consent given."""
    await _consent(session, hr_user.tenant_id, hr_user.id)
    return await as_caller(session, hr_user)


# ── What is offered ─────────────────────────────────────────────────────────


async def test_product_tools_follow_the_link_and_the_role(
    session: AsyncSession,
    tenant: Tenant,
    employee: Employee,
    me: User,  # noqa: F811
    hr: User,
    products: FakeProducts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with use_backend(FakeModel()):
        assert await _offered(session, me) == set()  # nothing linked yet

        await _link(session, tenant, "interview", "expense")
        # Everybody files expenses; hiring is for HR and hiring managers.
        assert await _offered(session, me) == {"expense_claims", "expense_summary"}
        assert await _offered(session, hr) == PRODUCT_TOOLS

        # Someone now reports to Anita: she hires for her team.
        session.add(
            Employee(
                tenant_id=tenant.id,
                employee_code="ACM777",
                first_name="Kiran",
                last_name="Das",
                work_email="kiran@acme.test",
                status=EmploymentStatus.ACTIVE,
                manager_id=employee.id,
            )
        )
        await session.commit()
        manager = await as_caller(session, me)
        assert await _offered(session, manager) == PRODUCT_TOOLS

        # Not configured on this server: never offered, never asked.
        for prefix in ("INTERVIEW_TOOL", "EXPENSE_TOOL"):
            monkeypatch.delenv(f"{prefix}_SECRET")
        product_settings.reset_cache()
        assert await _offered(session, hr) == set()
    assert products.calls == []


async def test_a_switched_off_link_or_another_workspace_gets_nothing(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,
    other_hr_user: User,
    products: FakeProducts,
) -> None:
    await _link(session, tenant, "interview", "expense")
    other = await as_caller(session, other_hr_user)
    with use_backend(FakeModel()):
        # Globex never linked anything: Acme's link does not reach it.
        from app.services.companion import tools

        config = await companion.get_config(session, other)
        offered = {tool.key for tool in await tools.offered(session, other, config)}
        assert offered & PRODUCT_TOOLS == set()

        rows = (await session.scalars(select(ProductLink))).all()
        for row in rows:
            row.status = "disabled"
        await session.commit()
        assert await _offered(session, hr) == set()


# ── Asking, as the person ───────────────────────────────────────────────────


async def test_claims_are_read_as_the_person_and_never_reach_the_model(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    products: FakeProducts,
) -> None:
    await _link(session, tenant, "expense")
    products.answers[("expense", "claims")] = {
        "scope": "mine",
        "total": 1,
        "claims": [
            {
                "reference": "EXP-1",
                "title": "Taxi to airport",
                "submitter": "Anita Sharma",
                "status": "Pending",
                "payment": "Unpaid",
                "amount": "125000.50",
                "currency": "INR",
                "category": "Travel",
                "submitted_at": "2026-10-19T05:00:00+00:00",
            }
        ],
    }
    model = FakeModel(
        plan("expense_claims", "Here are your pending claims.", scope="mine", status="pending")
    )
    with use_backend(model):
        reply = await _say(session, me, "what claims of mine are still pending?")

    call = products.calls[0]
    assert call["product"] == "expense"
    body = call["body"]
    # The tenant is the caller's own, and the product is asked as the caller.
    assert body["tenant_id"] == str(me.tenant_id)
    assert body["actor"] == {"id": str(me.id), "email": "anita@acme.test", "role": "employee"}
    assert body["kind"] == "claims"
    assert body["params"]["scope"] == "mine" and body["params"]["status"] == "pending"

    assert reply["tool"] == "expense_claims"
    card = reply["cards"][0]
    assert card["type"] == "table" and card["title"] == "Your claims"
    assert card["source"] == "ExpenseGenie" and card["open"] == "/launch/expense"
    assert card["rows"][0] == [
        "19 Oct 2026", "Taxi to airport", "Travel", "₹1,25,000.50", "Pending", "Unpaid"
    ]  # fmt: skip

    sent = model.everything_sent()
    assert "Taxi" not in sent and "125000" not in sent and "1,25,000" not in sent

    audit_row = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "companion_message"))
    ).one()
    assert audit_row.ai_context["tool"] == "expense_claims"


async def test_the_whole_companys_claims_are_for_hr_only(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    hr: User,
    products: FakeProducts,
) -> None:
    await _link(session, tenant, "expense")
    with use_backend(FakeModel(plan("expense_claims", "Here they are.", scope="company"))):
        reply = await _say(session, me, "show me everyone's expense claims")
    assert reply["cards"][0]["type"] == "notice"
    assert "Only HR" in reply["cards"][0]["text"]
    assert products.calls == []  # refused here, before the product was asked

    products.answers[("expense", "summary")] = {
        "scope": "company",
        "by_status": [
            {"status": "Approved", "currency": "INR", "count": 2, "amount": "4500.25"},
            {"status": "Pending", "currency": "INR", "count": 1, "amount": "1250.50"},
        ],
        "by_category": [
            {"category": "Lodging", "currency": "INR", "count": 1, "amount": "4000.00"}
        ],
        "approved_unpaid": [{"currency": "INR", "count": 1, "amount": "500.25"}],
    }
    with use_backend(
        FakeModel(plan("expense_summary", "Here are this month's totals.", scope="company"))
    ):
        reply = await _say(session, hr, "how much has the company claimed this month?")
    body = products.calls[0]["body"]
    assert body["actor"]["role"] == "admin" and body["params"]["scope"] == "company"
    # Both ends empty means this month so far, as midnights in India.
    today = datetime.now(IST).date()
    start = datetime.combine(today.replace(day=1), time.min, IST).astimezone(UTC)
    assert datetime.fromisoformat(body["params"]["from"]) == start
    status_card, category_card, unpaid = reply["cards"]
    assert status_card["rows"] == [["Pending", 1, "₹1,250.50"], ["Approved", 2, "₹4,500.25"]]
    assert category_card["rows"] == [["Lodging", 1, "₹4,000.00"]]
    assert unpaid["text"] == "Approved but not yet paid: 1 claim, ₹500.25."


async def test_interview_outcomes_are_observations_never_findings(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,
    products: FakeProducts,
) -> None:
    await _link(session, tenant, "interview")
    products.answers[("interview", "interviews")] = {
        "total": 2,
        "interviews": [
            {
                "candidate": "Ravi Kumar",
                "job": "Backend Engineer",
                "at": "2026-10-21T09:00:00+00:00",
                "status": "Terminated – Cheating Detected",  # noqa: RUF001  (the product's own label)
            },
            {
                "candidate": "Priya Nair",
                "job": "Backend Engineer",
                "at": "2026-10-22T04:30:00+00:00",
                "status": "Scheduled",
            },
        ],
    }
    with use_backend(FakeModel(plan("hiring_interviews", "Here are the interviews."))):
        reply = await _say(session, hr, "which interviews are coming up?")
    params = products.calls[0]["body"]["params"]
    today = datetime.now(IST).date()
    assert datetime.fromisoformat(params["start"]) == datetime.combine(today, time.min, IST)
    assert datetime.fromisoformat(params["end"]) == datetime.combine(
        today + timedelta(days=14), time.min, IST
    )
    rows = reply["cards"][0]["rows"]
    assert rows[0] == ["Wed 21 Oct 2026, 2:30 pm", "Ravi Kumar", "Backend Engineer", "Ended early"]
    assert rows[1][3] == "Scheduled"
    assert "cheat" not in json.dumps(reply).lower()
    assert reply["cards"][0]["source"] == "AnytimeHire"


async def test_a_product_that_cannot_answer_is_said_plainly(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,
    products: FakeProducts,
) -> None:
    await _link(session, tenant, "interview")
    products.answers[("interview", "jobs")] = httpx.Response(503, text="down")
    with use_backend(FakeModel(plan("hiring_jobs", "Here are the open jobs.", status="open"))):
        reply = await _say(session, hr, "which jobs are open?")
    assert reply["cards"][0]["tone"] == "warning"
    assert "AnytimeHire could not be reached" in reply["cards"][0]["text"]

    refusal = "You don't have access to hiring data in the interview tool yet."
    products.answers[("interview", "jobs")] = httpx.Response(403, json={"detail": refusal})
    with use_backend(FakeModel(plan("hiring_jobs", "Here are the jobs."))):
        reply = await _say(session, hr, "list the jobs")
    assert reply["cards"][0]["text"] == refusal

    # A date range the person can't ask for is said before anything is sent.
    calls = len(products.calls)
    with use_backend(
        FakeModel(plan("hiring_interviews", "Here.", **{"from": "2026-01-01", "to": "2026-12-31"}))
    ):
        reply = await _say(session, hr, "all interviews this year")
    assert "at most 93 days" in reply["cards"][0]["text"]
    assert len(products.calls) == calls


def test_every_status_and_scope_is_a_choice_the_model_is_given() -> None:
    from app.services.companion import tools

    assert set(tools.INPUTS["scope"]["enum"]) == {"none", "mine", "approvals", "company"}
    assert {"open", "applied", "pending", "approved", "none"} <= set(tools.INPUTS["status"]["enum"])


async def test_each_candidate_row_opens_its_job_in_the_product(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,
    products: FakeProducts,
) -> None:
    await _link(session, tenant, "interview")
    products.answers[("interview", "candidates")] = {
        "total": 2,
        "candidates": [
            {"name": "Asha", "job": "Python developer", "status": "Applied", "job_ref": "Jb7_k-2"},
            # A ref that is not a plain token gives no link, never a crafted one.
            {"name": "Ravi", "job": "Designer", "status": "Applied", "job_ref": "../../x?y"},
        ],
    }
    with use_backend(FakeModel(plan("hiring_candidates", "Here are the candidates."))):
        reply = await _say(session, hr, "candidates")
    card = reply["cards"][0]
    assert card["links"] == ["/launch/interview?to=/job-openings/Jb7_k-2/candidates", None]


# ── The same records as portal pages (Expenses / Hiring when linked) ───────


async def test_a_linked_products_pages_read_live_as_the_person(
    session: AsyncSession,
    tenant: Tenant,
    me: User,  # noqa: F811
    products: FakeProducts,
) -> None:
    from fastapi import HTTPException

    from app.services.products import views

    await _link(session, tenant, "expense")
    products.answers[("expense", "claims")] = {"scope": "mine", "total": 0, "claims": []}
    page = await views.render(
        session, me, "expense", "mine", {"status": "pending", "scope": "company", "x": "y"}
    )
    body = products.calls[-1]["body"]
    # The page fixes whose claims; asking for the company's on "mine" changes nothing.
    assert body["params"]["scope"] == "mine" and body["params"]["status"] == "pending"
    assert body["actor"]["role"] == "employee"
    assert page["cards"][0]["type"] == "notice"

    # An employee asking for the company's claims gets the product's refusal, not the data.
    company = await views.render(session, me, "expense", "company", {})
    assert company["cards"][0]["type"] == "notice"

    # Hiring is not this employee's: AnytimeHire is never asked.
    before = len(products.calls)
    await _link(session, tenant, "interview")
    jobs = await views.render(session, me, "interview", "jobs", {})
    assert jobs["cards"][0]["tone"] == "warning" and len(products.calls) == before

    bad = await views.render(session, me, "expense", "mine", {"from": "yesterday"})
    assert bad["cards"][0]["tone"] == "warning"

    with pytest.raises(HTTPException) as missing:
        await views.render(session, me, "expense", "payroll", {})
    assert missing.value.status_code == 404
