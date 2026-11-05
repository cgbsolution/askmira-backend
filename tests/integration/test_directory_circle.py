"""Who sees whom in the people directory, against a real database.

HR sees the whole company. Anybody else sees only their own circle — their
manager and dotted-line manager, the colleagues who share their manager, and
their reporting line — never people who have left, never another company. A
colleague outside the circle is found only by typing their name, and the
answer is a name and title, not a record.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import mcp
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def _login(
    session: AsyncSession, tenant: Tenant, person: Employee, role: UserRole = UserRole.EMPLOYEE
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{person.employee_code.lower()}@login.test",
        password_hash="x",
        full_name=person.first_name,
        role=role,
        is_active=True,
        employee_id=person.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@dataclass
class Org:
    boss: Employee
    dotted: Employee
    me: Employee
    peer: Employee
    left: Employee
    report: Employee
    grand: Employee
    stranger: Employee
    elsewhere: Employee
    foreign: Employee


@pytest.fixture
async def org(
    session: AsyncSession, tenant: Tenant, other_tenant: Tenant, legal_entity: LegalEntity
) -> Org:
    sister = LegalEntity(tenant_id=tenant.id, name="Sister Co Pvt Ltd", code="SIS")
    session.add(sister)
    await session.commit()

    async def person(
        code: str,
        first: str,
        *,
        manager: Employee | None = None,
        company: LegalEntity | None = legal_entity,
        within: Tenant = tenant,
        **extra,
    ) -> Employee:
        row = Employee(
            tenant_id=within.id,
            employee_code=code,
            first_name=first,
            last_name="Test",
            work_email=f"{code.lower()}@acme.test",
            designation=f"{first} title",
            legal_entity_id=company.id if company else None,
            manager_id=manager.id if manager else None,
            status=extra.pop("status", EmploymentStatus.ACTIVE),
            **extra,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    boss = await person("C001", "Bharat")
    dotted = await person("C002", "Divya")
    me = await person("C003", "Meera", manager=boss, dotted_line_manager_id=dotted.id)
    peer = await person("C004", "Pooja", manager=boss)
    left = await person("C005", "Lalit", manager=boss, status=EmploymentStatus.EXITED)
    report = await person("C006", "Rohan", manager=me)
    grand = await person("C007", "Gita", manager=report)
    stranger = await person("C008", "Sanjay")
    elsewhere = await person("C009", "Ravi", company=sister)
    foreign = await person("X001", "Farhan", company=None, within=other_tenant)
    return Org(boss, dotted, me, peer, left, report, grand, stranger, elsewhere, foreign)


async def _listed(client: httpx.AsyncClient, user: User, query: str = "") -> set[str]:
    response = await client.get(f"/api/v1/employees?page_size=200{query}", headers=_auth(user))
    assert response.status_code == 200, response.text
    return {row["employee_code"] for row in response.json()["items"]}


# ── The list ────────────────────────────────────────────────────────────────


async def test_an_employee_lists_only_their_own_circle(client, session, tenant, org) -> None:
    me = await _login(session, tenant, org.me)

    assert await _listed(client, me) == {"C001", "C002", "C003", "C004", "C006", "C007"}


async def test_filters_never_widen_the_circle(client, session, tenant, org, legal_entity) -> None:
    me = await _login(session, tenant, org.me)
    sister = org.elsewhere.legal_entity_id

    assert await _listed(client, me, "&status=exited") == set()
    assert await _listed(client, me, f"&legal_entity_id={sister}") == set()
    assert await _listed(client, me, "&search=Sanjay") == set()
    assert await _listed(client, me, "&search=Ravi") == set()
    assert await _listed(client, me, f"&manager_id={org.boss.id}") == {"C003", "C004"}


async def test_an_employee_with_no_reports_sees_their_manager_and_team(
    client, session, tenant, org
) -> None:
    peer = await _login(session, tenant, org.peer)

    # Not Meera's reports, not Meera's dotted-line manager, not the leaver.
    assert await _listed(client, peer) == {"C001", "C003", "C004"}


async def test_a_login_without_an_employee_record_lists_nobody(
    client, session, tenant, org
) -> None:
    lone = User(
        tenant_id=tenant.id,
        email="lone@login.test",
        password_hash="x",
        full_name="Lone",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    session.add(lone)
    await session.commit()
    await session.refresh(lone)

    assert await _listed(client, lone) == set()


async def test_hr_lists_the_whole_company(client, session, hr_user, org) -> None:
    everyone = await _listed(client, hr_user)

    assert {"C001", "C005", "C008", "C009"} <= everyone
    assert "X001" not in everyone


# ── Picking somebody to act for (start an exit) ─────────────────────────────


async def test_a_manager_acts_only_for_their_reporting_line(client, session, tenant, org) -> None:
    me = await _login(session, tenant, org.me)

    # Never their manager, dotted-line manager or the colleagues beside them.
    assert await _listed(client, me, "&reporting_line=true") == {"C003", "C006", "C007"}
    assert await _listed(client, me, "&reporting_line=true&search=Pooja") == set()


async def test_without_reports_a_person_acts_for_nobody_else(client, session, tenant, org) -> None:
    peer = await _login(session, tenant, org.peer)

    assert await _listed(client, peer, "&reporting_line=true") == {"C004"}


async def test_hr_acts_for_anybody(client, session, hr_user, org) -> None:
    everyone = await _listed(client, hr_user, "&reporting_line=true")

    assert {"C001", "C004", "C008", "C009"} <= everyone


# ── One record ──────────────────────────────────────────────────────────────


async def test_a_record_outside_the_circle_is_not_found(client, session, tenant, org) -> None:
    me = await _login(session, tenant, org.me)

    for outside in (org.stranger, org.elsewhere, org.foreign):
        response = await client.get(f"/api/v1/employees/{outside.id}", headers=_auth(me))
        assert response.status_code == 404, outside.employee_code

    for inside in (org.boss, org.dotted, org.peer, org.report, org.grand):
        response = await client.get(f"/api/v1/employees/{inside.id}", headers=_auth(me))
        assert response.status_code == 200, inside.employee_code
        assert response.json()["phone"] is None


async def test_a_team_member_cannot_open_their_colleagues_reports(
    client, session, tenant, org
) -> None:
    peer = await _login(session, tenant, org.peer)

    response = await client.get(f"/api/v1/employees/{org.report.id}", headers=_auth(peer))
    assert response.status_code == 404
    response = await client.get(f"/api/v1/employees/{org.me.id}", headers=_auth(peer))
    assert response.status_code == 200


async def test_hr_opens_anybodys_record(client, session, hr_user, org) -> None:
    for person in (org.stranger, org.elsewhere, org.left):
        response = await client.get(f"/api/v1/employees/{person.id}", headers=_auth(hr_user))
        assert response.status_code == 200, person.employee_code
    response = await client.get(f"/api/v1/employees/{org.foreign.id}", headers=_auth(hr_user))
    assert response.status_code == 404


# ── Picking a colleague by name ─────────────────────────────────────────────


async def _colleagues(client: httpx.AsyncClient, user: User, search: str) -> list[dict]:
    response = await client.get(
        "/api/v1/employees/colleagues", params={"search": search}, headers=_auth(user)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_colleague_is_found_by_name_with_names_and_titles_only(
    client, session, tenant, org
) -> None:
    me = await _login(session, tenant, org.me)

    found = await _colleagues(client, me, "sanj")
    assert [row["employee_code"] for row in found] == ["C008"]
    assert set(found[0]) == {
        "id",
        "employee_code",
        "first_name",
        "last_name",
        "display_name",
        "designation",
        "photo_url",
    }
    assert [row["employee_code"] for row in await _colleagues(client, me, "c008")] == ["C008"]
    assert [row["employee_code"] for row in await _colleagues(client, me, "Sanjay Test")] == [
        "C008"
    ]


async def test_the_colleague_search_needs_two_letters(client, session, tenant, org) -> None:
    me = await _login(session, tenant, org.me)

    assert await _colleagues(client, me, "") == []
    assert await _colleagues(client, me, "a") == []
    assert await _colleagues(client, me, "  s ") == []


async def test_the_colleague_search_stays_in_the_company_and_skips_leavers(
    client, session, tenant, org, hr_user
) -> None:
    me = await _login(session, tenant, org.me)

    assert await _colleagues(client, me, "Ravi") == []
    assert await _colleagues(client, me, "Lalit") == []
    assert await _colleagues(client, me, "Farhan") == []
    # HR picks across the companies of the workspace, still never a leaver.
    assert [row["employee_code"] for row in await _colleagues(client, hr_user, "Ravi")] == ["C009"]
    assert await _colleagues(client, hr_user, "Lalit") == []


async def test_the_colleague_search_returns_at_most_twenty(client, session, tenant, org) -> None:
    for n in range(25):
        session.add(
            Employee(
                tenant_id=tenant.id,
                employee_code=f"N{n:03d}",
                first_name=f"Naman{n}",
                work_email=f"naman{n}@acme.test",
                legal_entity_id=org.me.legal_entity_id,
                status=EmploymentStatus.ACTIVE,
            )
        )
    await session.commit()
    me = await _login(session, tenant, org.me)

    assert len(await _colleagues(client, me, "naman")) == 20
    response = await client.get(
        "/api/v1/employees/colleagues",
        params={"search": "naman", "limit": 500},
        headers=_auth(me),
    )
    assert response.status_code == 422


# ── Outside assistants (MCP) read no wider than the app ─────────────────────


async def test_an_assistant_searches_only_the_callers_circle(session, tenant, org) -> None:
    me = await as_caller(session, await _login(session, tenant, org.me))

    out = await mcp.run_tool(session, me, "search_employees", {"query": "", "limit": 100})
    assert {p["employee_code"] for p in out["people"]} == {
        "C001",
        "C002",
        "C003",
        "C004",
        "C006",
        "C007",
    }
    leavers = await mcp.run_tool(
        session, me, "search_employees", {"query": "Lalit", "include_exited": True}
    )
    assert leavers["people"] == []


async def test_an_assistant_acting_for_hr_searches_everyone(session, hr_user, org) -> None:
    hr = await as_caller(session, hr_user)

    out = await mcp.run_tool(session, hr, "search_employees", {"query": "", "limit": 100})
    assert {"C008", "C009"} <= {p["employee_code"] for p in out["people"]}
