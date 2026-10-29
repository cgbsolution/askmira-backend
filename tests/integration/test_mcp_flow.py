"""The MCP server: the handshake a client does, scoped tools, reads only, audited."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.dpdp import ConsentCapturedVia, DataPrincipalKind
from app.models.employee import Employee
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.tenant import Tenant
from app.models.user import User
from app.services import integrations
from app.services.dpdp import service as dpdp_service
from tests.integration.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def _key(session: AsyncSession, user: User, scopes: list[str]) -> str:
    created = await integrations.create_key(
        session, user, integrations.ApiKeyIn(name="Claude", scopes=scopes), request=None
    )
    return created["key"]


async def _ai_on(session: AsyncSession, actor: User, *people: Employee) -> None:
    """What an outside assistant needs: AI on for the workspace, and each
    person's agreement to AI reading their record (DPDP purpose ai_assistance)."""
    tenant = await session.get(Tenant, actor.tenant_id)
    tenant.settings = {**(tenant.settings or {}), "ai": {"enabled": True}}
    for person in people:
        await dpdp_service.record_consent(
            session,
            actor,
            principal_id=person.id,
            principal_kind=DataPrincipalKind.EMPLOYEE,
            purpose_key="ai_assistance",
            granted=True,
            captured_via=ConsentCapturedVia.SELF_SERVICE,
        )
    await session.commit()


def _rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


async def test_a_client_can_connect_list_and_call_only_what_its_key_allows(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    client: httpx.AsyncClient,
) -> None:
    key = await _key(session, hr_user, ["employees:read"])
    headers = {"Authorization": f"Bearer {key}"}
    await _ai_on(session, hr_user, employee)

    hello = await client.post(
        "/api/v1/mcp",
        json=_rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
        headers=headers,
    )
    assert hello.status_code == 200
    assert hello.json()["result"]["protocolVersion"] == "2025-06-18"
    assert hello.json()["result"]["capabilities"]["tools"] == {"listChanged": False}

    note = await client.post(
        "/api/v1/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert note.status_code == 202

    tools = await client.post("/api/v1/mcp", json=_rpc("tools/list", {}), headers=headers)
    names = [t["name"] for t in tools.json()["result"]["tools"]]
    assert names == ["search_employees"]
    assert tools.json()["result"]["tools"][0]["annotations"]["readOnlyHint"] is True

    found = await client.post(
        "/api/v1/mcp",
        json=_rpc("tools/call", {"name": "search_employees", "arguments": {"query": "Anita"}}),
        headers=headers,
    )
    result = found.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["people"][0]["employee_code"] == employee.employee_code

    refused = await client.post(
        "/api/v1/mcp",
        json=_rpc(
            "tools/call",
            {"name": "who_is_out", "arguments": {"from": "2026-10-01", "to": "2026-10-02"}},
        ),
        headers=headers,
    )
    assert refused.json()["error"]["code"] == -32602

    audited = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "mcp_tool"))
    ).all()
    assert len(audited) == 1
    assert audited[0].actor_type == "mcp_agent"


async def test_without_a_key_nothing_answers(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/mcp", json=_rpc("initialize", {}))
    assert response.status_code == 401
    stream = await client.get("/api/v1/mcp")
    assert stream.status_code in (401, 405)


async def test_who_is_out_never_carries_the_reason_and_stays_in_its_workspace(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    other_hr_user: User,
    client: httpx.AsyncClient,
) -> None:
    kind = LeaveType(tenant_id=tenant.id, name="Sick leave", code="SL")
    session.add(kind)
    await session.flush()
    session.add(
        LeaveRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            leave_type_id=kind.id,
            start_date=date(2026, 10, 6),
            end_date=date(2026, 10, 7),
            total_days=2,
            reason="Fever and a doctor's visit",
            status=LeaveRequestStatus.APPROVED,
        )
    )
    await session.commit()

    mine = await _key(session, hr_user, ["leave:read"])
    theirs = await _key(session, other_hr_user, ["leave:read"])
    await _ai_on(session, hr_user, employee)
    await _ai_on(session, other_hr_user)
    call = _rpc(
        "tools/call",
        {"name": "who_is_out", "arguments": {"from": "2026-10-01", "to": "2026-10-31"}},
    )

    own = await client.post("/api/v1/mcp", json=call, headers={"Authorization": f"Bearer {mine}"})
    leave = own.json()["result"]["structuredContent"]["leave"]
    assert [row["leave_type"] for row in leave] == ["Sick leave"]
    assert "Fever" not in own.text

    other = await client.post(
        "/api/v1/mcp", json=call, headers={"Authorization": f"Bearer {theirs}"}
    )
    assert other.json()["result"]["structuredContent"]["leave"] == []


async def test_a_bad_range_is_a_tool_error_not_a_crash(
    session: AsyncSession, hr_user: User, client: httpx.AsyncClient
) -> None:
    key = await _key(session, hr_user, ["attendance:read"])
    await _ai_on(session, hr_user)
    response = await client.post(
        "/api/v1/mcp",
        json=_rpc(
            "tools/call",
            {"name": "attendance_summary", "arguments": {"from": "2026-01-01", "to": "2026-12-31"}},
        ),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.json()["result"]["isError"] is True


async def test_a_connector_signs_a_person_in_with_oauth_and_reads_as_them(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    client: httpx.AsyncClient,
) -> None:
    import base64
    import hashlib
    import secrets
    from urllib.parse import parse_qs, urlsplit

    from app.core.security import create_access_token

    unauthenticated = await client.post("/api/v1/mcp", json=_rpc("tools/list"))
    assert unauthenticated.status_code == 401
    challenge = unauthenticated.headers["www-authenticate"]
    assert 'resource_metadata="' in challenge
    metadata_path = urlsplit(challenge.split('"')[1]).path
    resource = (await client.get(metadata_path)).json()
    assert resource["resource"].endswith("/api/v1/mcp")
    server = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert server["code_challenge_methods_supported"] == ["S256"]

    bad = await client.post(
        "/api/v1/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}
    )
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"
    registered = await client.post(
        "/api/v1/oauth/register",
        json={
            "client_name": "Claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        },
    )
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]

    verifier = secrets.token_urlsafe(48)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    jwt = create_access_token(
        user_id=str(hr_user.id), tenant_id=str(hr_user.tenant_id), role=hr_user.role.value
    )
    as_person = {"Authorization": f"Bearer {jwt}"}
    redirect = "https://claude.ai/api/mcp/auth_callback"
    details = await client.get(
        "/api/v1/oauth/authorize/details",
        params={"client_id": client_id, "redirect_uri": redirect},
        headers=as_person,
    )
    assert details.json()["client_name"] == "Claude"
    approved = await client.post(
        "/api/v1/oauth/authorize",
        json={
            "client_id": client_id,
            "redirect_uri": redirect,
            "code_challenge": code_challenge,
            "scope": "employees:read org:read",
            "state": "xyz",
        },
        headers=as_person,
    )
    query = parse_qs(urlsplit(approved.json()["redirect_to"]).query)
    assert query["state"] == ["xyz"]
    code = query["code"][0]

    wrong_verifier = await client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect,
            "code_verifier": "x" * 50,
        },
    )
    assert wrong_verifier.status_code == 400
    tokens = await client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
    )
    assert tokens.status_code == 200, tokens.text
    access = tokens.json()["access_token"]
    assert access.startswith("amo_") and tokens.json()["scope"] == "employees:read org:read"

    bearer = {"Authorization": f"Bearer {access}"}
    listed = await client.post("/api/v1/mcp", json=_rpc("tools/list"), headers=bearer)
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert "search_employees" in names and "attendance_summary" not in names

    refreshed = await client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.json()["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refreshed.status_code == 200
    stale = await client.post("/api/v1/mcp", json=_rpc("tools/list"), headers=bearer)
    assert stale.status_code == 401, "a refresh rotates the access token"

    replay = await client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
    )
    assert replay.status_code == 400
    fresh = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    revoked = await client.post("/api/v1/mcp", json=_rpc("tools/list"), headers=fresh)
    assert revoked.status_code == 401, "a replayed code revokes what it produced"
