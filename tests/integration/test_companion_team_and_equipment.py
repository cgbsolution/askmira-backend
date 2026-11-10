"""Ask Mira: a manager's team's IT tickets, and joiners still without equipment.

Real database, rule-based planner (no AI). CGB Helpdesk is a fake answering
``tickets`` (scope team) and ``holdings``.

* "Tickets affecting my team" sends the helpdesk only the asker's own direct
  reports' emails; someone with no reports is told so and nothing is asked.
* "Which employees still need equipment?" lists this month's joiners with what
  they hold: from the helpdesk's register when it is linked (asked by email),
  else AskMira's own assignments. Only HR; the model reads counts, not names.
"""

from __future__ import annotations

import hashlib
import json
import types
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest

from app.models.assets import Asset, AssetAssignment, AssetCategory, AssetCondition, AssetStatus
from app.models.employee import Employee
from app.models.user import UserRole
from app.services.ai import client as ai_client
from app.services.companion import service as companion
from app.services.companion import views
from app.services.products import client as product_client
from app.services.products import settings as product_settings
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import make_person
from tests.integration.test_sales_procurement_link import _link

pytestmark = requires_db

SECRET = "helpdesk-secret-0123456789abcdef0123456789"
TODAY = companion._today()


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_client.settings, "ANTHROPIC_API_KEY", "")


class FakeDesk:
    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.holdings: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            SECRET,
            algorithms=["HS256"],
            audience="askmira:helpdesk",
            issuer="askmira",
        )
        body = json.loads(request.content)
        assert hashlib.sha256(request.content).hexdigest()
        if request.url.path.endswith("users/sync"):
            return httpx.Response(200, json={"created": 0, "updated": 0, "deactivated": 0})
        self.asked.append(body)
        if body["kind"] == "tickets":
            return httpx.Response(200, json={"kind": "tickets", "scope": "team", "total": 1,
                "offset": 0, "tickets": [{"id": str(uuid.uuid4()), "ref": "1A2B3C4D",
                "subject": "VPN down", "category": "Network", "priority": "high", "status": "open",
                "requester": "Ravi", "assignee": None, "created_at": "2026-11-01T10:00:00Z",
                "sla_breached": False}]})  # fmt: skip
        if body["kind"] == "holdings":
            return httpx.Response(200, json={"kind": "holdings", "holdings": [
                {"email": e, "has_account": True, "assets": self.holdings.get(e, 0)}
                for e in body["params"]["emails"]]})  # fmt: skip
        return httpx.Response(422, json={"error": "unexpected"})


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDesk]:
    monkeypatch.setenv("HELPDESK_TOOL_API_URL", "https://helpdesk.test/api")
    monkeypatch.setenv("HELPDESK_TOOL_APP_URL", "https://helpdesk.test")
    monkeypatch.setenv("HELPDESK_TOOL_SECRET", SECRET)
    product_settings.reset_cache()
    fake = FakeDesk()
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        product_client,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout
        ),
    )
    yield fake
    product_settings.reset_cache()


async def _ask(session, user, text: str) -> dict[str, Any]:
    result = await companion.send(session, user, companion.MessageIn(text=text), request=None)
    return result["messages"][1]


async def test_team_tickets_send_only_the_managers_own_reports(session, tenant, desk) -> None:
    boss = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Mala Manager")
    boss_emp = await session.get(Employee, boss.employee_id)
    ravi = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Ravi Rep", manager=boss_emp
    )
    await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Other Person")
    await _link(session, tenant, "helpdesk", None)
    boss = await as_caller(session, boss)

    reply = await _ask(session, boss, "Show the open support tickets affecting our team")
    [asked] = [a for a in desk.asked if a["kind"] == "tickets"]
    ravi_email = (await session.get(Employee, ravi.employee_id)).work_email.lower()
    assert asked["params"]["scope"] == "team"
    assert asked["params"]["team_emails"] == [ravi_email]
    assert any(c.get("type") == "table" for c in reply["cards"])

    loner = await as_caller(
        session, await make_person(session, tenant, role=UserRole.EMPLOYEE, name="No Reports")
    )
    desk.asked.clear()
    reply = await _ask(session, loner, "my team tickets")
    assert "Nobody reports to you" in json.dumps(reply["cards"])
    assert desk.asked == []


async def _joiner(session, tenant, name: str, days: int) -> Employee:
    person = await make_person(session, tenant, role=UserRole.EMPLOYEE, name=name)
    employee = await session.get(Employee, person.employee_id)
    employee.date_of_joining = TODAY + timedelta(days=days)
    employee.display_name = name
    await session.commit()
    return employee


async def test_joiners_without_equipment_from_askmiras_register(session, tenant) -> None:
    hr = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    kit = await _joiner(session, tenant, "Kiran Kit", -3)
    await _joiner(session, tenant, "Nina Nothing", 5)
    await _joiner(session, tenant, "Old Timer", -400)
    category = AssetCategory(tenant_id=tenant.id, code="LAP", name="Laptop")
    session.add(category)
    await session.flush()
    laptop = Asset(tenant_id=tenant.id, category_id=category.id, asset_tag="LAP-1", name="ThinkPad",
                   status=AssetStatus.ASSIGNED)  # fmt: skip
    session.add(laptop)
    await session.flush()
    session.add(AssetAssignment(tenant_id=tenant.id, asset_id=laptop.id, employee_id=kit.id,
                                assigned_at=datetime.now(UTC), condition_out=AssetCondition.NEW))  # fmt: skip
    await session.commit()

    reply = await _ask(session, hr, "Which employees still need equipment assigned?")
    [table] = [c for c in reply["cards"] if c.get("type") == "table"]
    status = {row[0]: row[4] for row in table["rows"]}
    assert status == {"Nina Nothing": "Needs equipment", "Kiran Kit": "Has equipment"}
    assert table["rows"][0][0] == "Nina Nothing", "who needs equipment comes first"
    model = views.view_of([table], allowed=True)
    assert "Nina" not in model and "1 have no equipment" in model

    staff = await as_caller(
        session, await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Eve Emp")
    )
    refused = await _ask(session, staff, "which employees still need equipment assigned")
    assert not [c for c in refused["cards"] if c.get("type") == "table"]


async def test_joiners_equipment_from_the_helpdesk_register(session, tenant, desk) -> None:
    hr = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    kit = await _joiner(session, tenant, "Kiran Kit", -3)
    await _joiner(session, tenant, "Nina Nothing", 5)
    await _link(session, tenant, "helpdesk", None)
    desk.holdings = {kit.work_email.lower(): 2}

    reply = await _ask(session, hr, "new joiners without laptops")
    [asked] = [a for a in desk.asked if a["kind"] == "holdings"]
    assert kit.work_email.lower() in asked["params"]["emails"]
    assert len(asked["params"]["emails"]) == 2, "only this window's joiners are asked about"
    [table] = [c for c in reply["cards"] if c.get("type") == "table"]
    assert {row[0]: row[3] for row in table["rows"]}["Kiran Kit"] == "2"
    assert "CGB Helpdesk" in table["caption"]


# ── Joiners' purchases in ProcureFlow ───────────────────────────────────────

PF_SECRET = "procure-secret-0123456789abcdef0123456789"


class FakeProcureFlow:
    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.company_refused = False
        self.requisitions: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            PF_SECRET,
            algorithms=["HS256"],
            audience="askmira:procurement",
            issuer="askmira",
        )
        body = json.loads(request.content)
        self.asked.append(body)
        if body["kind"] == "requisitions":
            if body["params"].get("scope") == "company" and self.company_refused:
                return httpx.Response(
                    403,
                    json={"detail": "Your ProcureFlow role shows you your own requisitions only."},
                )
            return httpx.Response(200, json={"kind": "requisitions", "scope": "company",
                "total": len(self.requisitions), "requisitions": self.requisitions})  # fmt: skip
        return httpx.Response(422, json={"detail": "unexpected"})


@pytest.fixture
def pf(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProcureFlow]:
    monkeypatch.setenv("PROCUREMENT_TOOL_API_URL", "https://pf.test/api/v1")
    monkeypatch.setenv("PROCUREMENT_TOOL_APP_URL", "https://pf.test")
    monkeypatch.setenv("PROCUREMENT_TOOL_SECRET", PF_SECRET)
    product_settings.reset_cache()
    fake = FakeProcureFlow()
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        product_client,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout
        ),
    )
    yield fake
    product_settings.reset_cache()


def _req(number: str, title: str, status: str) -> dict[str, Any]:
    return {"ref": number, "number": number, "title": title, "status": status,
            "requester": "Asha", "cost_center": "IT", "amount": "85000.00", "currency": "INR"}  # fmt: skip


async def test_joiners_show_their_procureflow_purchase_and_whether_it_is_approved(
    session, tenant, pf
) -> None:
    from app.services.companion.equipment_tools import joiners_equipment

    hr = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    await _joiner(session, tenant, "Kiran Kit", -3)
    nina = await _joiner(session, tenant, "Nina Nothing", 5)
    await _joiner(session, tenant, "Omar Open", 7)
    await _link(session, tenant, "procurement", {hr.email: "TENANT_ADMIN"})
    pf.requisitions = [
        _req("PR-0012", "Laptop for Kiran Kit", "APPROVED"),
        _req("PR-0013", f"Laptop for new joiner {nina.employee_code}", "AWAITING_APPROVAL"),
        _req("PR-0014", "Office chairs", "APPROVED"),
        # Not Omar's: a part of his name, or someone else's code with his name.
        _req("PR-0015", "Laptop for Omar Openshaw", "APPROVED"),
        _req("PR-0016", "Laptop replacement - Omar Open (EMP0311)", "APPROVED"),
    ]
    cards, _used = await joiners_equipment(session, hr, {}, today=TODAY)
    [table] = cards
    assert table["columns"][-1] == "Purchase (ProcureFlow)"
    purchase = {row[0]: row[5] for row in table["rows"]}
    # By name only, and said so; by the joiner's code, settled.
    assert purchase["Kiran Kit"] == "PR-0012 · Approved (approved) · matched by name"
    assert purchase["Nina Nothing"] == "PR-0013 · Awaiting approval (waiting for approval)"
    assert purchase["Omar Open"] == "No requisition found"
    assert "1 joiner(s) have an approved purchase, 1 wait for approval" in table["model_view"]
    assert "Kiran" not in table["model_view"]
    [asked] = [a for a in pf.asked if a["kind"] == "requisitions"]
    assert asked["params"]["scope"] == "company"


async def test_a_role_that_sees_only_its_own_requisitions_is_said_so(session, tenant, pf) -> None:
    from app.services.companion.equipment_tools import joiners_equipment

    hr = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    await _joiner(session, tenant, "Kiran Kit", -3)
    await _link(session, tenant, "procurement", {hr.email: "REQUESTER"})
    pf.company_refused = True
    pf.requisitions = [_req("PR-0012", "Laptop for Kiran Kit", "DRAFT")]
    [table], _used = await joiners_equipment(session, hr, {}, today=TODAY)
    assert "from your own ProcureFlow requisitions" in table["caption"]
    shown = {row[0]: row[5] for row in table["rows"]}["Kiran Kit"]
    assert shown == "PR-0012 · Draft (not sent for approval) · matched by name"
    # A draft is not waiting for approval.
    assert "0 wait for approval, 1 are drafts not sent" in table["model_view"]


async def test_a_refused_purchase_is_no_live_requisition(session, tenant, pf) -> None:
    from app.services.companion.equipment_tools import joiners_equipment

    hr = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    kiran = await _joiner(session, tenant, "Kiran Kit", -3)
    await _link(session, tenant, "procurement", {hr.email: "TENANT_ADMIN"})
    pf.requisitions = [_req("PR-0020", f"Laptop for {kiran.employee_code}", "REJECTED")]
    [table], _used = await joiners_equipment(session, hr, {}, today=TODAY)
    assert {row[0]: row[5] for row in table["rows"]}["Kiran Kit"] == (
        "PR-0020 · Rejected (no live purchase)"
    )
    assert "0 wait for approval" in table["model_view"]
    assert (
        "1 were refused or withdrawn; 1 without equipment have no live requisition"
        in (table["model_view"])
    )
