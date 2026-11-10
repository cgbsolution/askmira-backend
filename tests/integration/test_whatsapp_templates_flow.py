"""An admin registers the workspace's Meta-approved WhatsApp templates and links them to events.

Meta itself is a double (httpx.MockTransport). What is proved here is ours:
Meta's naming rules enforced before a template is kept, nothing sent with a
template Meta has not approved, a linked template not deletable, the test send
carrying the template's own language and a digits-only number, employees and
other workspaces kept out, and opt-in falling back to the person's own mobile.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.notifications import dispatch
from app.services.notifications.adapters import WhatsAppCloudAdapter
from tests.integration.conftest import requires_db

pytestmark = requires_db

BASE = "/api/v1/integrations/whatsapp"
EVENT = sorted(dispatch.DEFAULT_TEMPLATES)[0]


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


class FakeMeta:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.error: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "graph.facebook.com"
        if self.error is not None:
            return httpx.Response(400, json={"error": self.error})
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(self.sent)}"}]})


@pytest.fixture
def meta(monkeypatch) -> FakeMeta:
    fake = FakeMeta()
    real = httpx.AsyncClient

    class ToMeta(real):  # the adapter opens its own client; point it at the double
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(fake.handler))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", ToMeta)
    return fake


@pytest.fixture
def connected(monkeypatch):
    """WhatsApp switched on for the workspace with its own business number."""

    async def adapter_for(_session, _tenant_id):
        return WhatsAppCloudAdapter("1093847561", "qa-token")

    monkeypatch.setattr(dispatch, "whatsapp_adapter_for", adapter_for)


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def _user(
    session: AsyncSession, tenant: Tenant, role: UserRole, tag: str, phone: str | None = None
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{tag}@{tenant.slug}.test",
        password_hash="x",
        full_name=tag,
        role=role,
        is_active=True,
        phone=phone,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _other_tenant(session: AsyncSession) -> Tenant:
    other = Tenant(name="Other Co", slug="other-wa-co")
    session.add(other)
    await session.commit()
    await session.refresh(other)
    return other


async def test_templates_follow_metas_rules_and_only_approved_ones_send(
    client, session, tenant, meta, connected
) -> None:
    admin = await _user(session, tenant, UserRole.ADMIN, "admin")
    h = _auth(admin)

    for bad, why in (
        ({"meta_name": "Payslip Ready"}, "exact Meta name"),
        ({"meta_name": "payslip_ready", "locale": "english"}, "language code"),
        ({"meta_name": "payslip_ready", "category": "PROMO"}, "category"),
        ({"meta_name": "payslip_ready", "parameter_names": ["First Name"]}, "Parameters"),
    ):
        r = await client.post(f"{BASE}/templates", json=bad, headers=h)
        assert r.status_code == 422 and why in r.text, (bad, r.text)

    r = await client.post(
        f"{BASE}/templates",
        json={
            "meta_name": "payslip_ready",
            "locale": "en_US",
            "category": "utility",
            "parameter_names": ["employee.first_name", "period"],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    template = r.json()
    assert template["status"] == "pending" and template["category"] == "UTILITY"
    again = await client.post(
        f"{BASE}/templates", json={"meta_name": "payslip_ready", "locale": "en_US"}, headers=h
    )
    assert again.status_code == 409, "the same name and language registered twice"

    link = await client.put(
        f"{BASE}/events/{EVENT}", json={"template_id": template["id"]}, headers=h
    )
    assert link.status_code == 422 and "not approved" in link.text

    r = await client.patch(f"{BASE}/templates/{template['id']}", json={"approved": True}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    link = await client.put(
        f"{BASE}/events/{EVENT}", json={"template_id": template["id"]}, headers=h
    )
    assert link.status_code == 200, link.text
    overview = link.json()
    assert overview["connected"] is True
    [row] = [e for e in overview["events"] if e["event_key"] == EVENT]
    assert row["template_id"] == template["id"]
    assert [t["events"] for t in overview["templates"]] == [[EVENT]]

    r = await client.delete(f"{BASE}/templates/{template['id']}", headers=h)
    assert r.status_code == 409 and EVENT in r.text, "a template still sending an event stays"

    wrong_count = await client.post(
        f"{BASE}/templates/{template['id']}/test",
        json={"phone": "+91 98450 11008", "values": ["Meera"]},
        headers=h,
    )
    assert wrong_count.status_code == 422 and "takes 2 value(s)" in wrong_count.text
    bad_phone = await client.post(
        f"{BASE}/templates/{template['id']}/test",
        json={"phone": "98450-110", "values": ["Meera", "Oct"]},
        headers=h,
    )
    assert bad_phone.status_code == 422 and "country code" in bad_phone.text

    sent = await client.post(
        f"{BASE}/templates/{template['id']}/test",
        json={"phone": "98450 11008", "values": ["Meera", "October 2026"]},
        headers=h,
    )
    assert sent.status_code == 200, sent.text
    [body] = meta.sent
    assert body["to"] == "919845011008", "digits with India's code, no plus or spaces"
    assert body["template"]["name"] == "payslip_ready"
    assert body["template"]["language"] == {"code": "en_US"}, "Meta matches on the exact code"
    params = body["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["Meera", "October 2026"]

    meta.error = {"code": 132001, "message": "Template name does not exist in the translation"}
    refused = await client.post(
        f"{BASE}/templates/{template['id']}/test",
        json={"phone": "+919845011008", "values": ["Meera", "Oct"]},
        headers=h,
    )
    assert refused.status_code == 502 and "does not exist" in refused.text, "Meta's own answer"

    unlinked = await client.put(f"{BASE}/events/{EVENT}", json={"template_id": None}, headers=h)
    assert unlinked.status_code == 200
    r = await client.delete(f"{BASE}/templates/{template['id']}", headers=h)
    assert r.status_code == 204
    assert (await client.get(f"{BASE}/templates", headers=h)).json()["templates"] == []


async def test_a_test_send_needs_whatsapp_switched_on(client, session, tenant, meta) -> None:
    admin = await _user(session, tenant, UserRole.ADMIN, "admin")
    h = _auth(admin)
    template = (
        await client.post(
            f"{BASE}/templates", json={"meta_name": "hello", "approved": True}, headers=h
        )
    ).json()
    r = await client.post(
        f"{BASE}/templates/{template['id']}/test", json={"phone": "+919845011008"}, headers=h
    )
    assert r.status_code == 409 and "Turn WhatsApp on" in r.text
    assert meta.sent == []


async def test_employees_and_other_workspaces_are_kept_out(client, session, tenant) -> None:
    admin = await _user(session, tenant, UserRole.ADMIN, "admin")
    employee = await _user(session, tenant, UserRole.EMPLOYEE, "emp")
    template = (
        await client.post(
            f"{BASE}/templates", json={"meta_name": "leave_approved"}, headers=_auth(admin)
        )
    ).json()

    for method, path, body in (
        ("GET", f"{BASE}/templates", None),
        ("POST", f"{BASE}/templates", {"meta_name": "mine"}),
        ("PATCH", f"{BASE}/templates/{template['id']}", {"approved": True}),
        ("PUT", f"{BASE}/events/{EVENT}", {"template_id": template["id"]}),
    ):
        r = await client.request(method, path, json=body, headers=_auth(employee))
        assert r.status_code == 403, (method, path, r.status_code)

    other = await _user(session, await _other_tenant(session), UserRole.ADMIN, "other-admin")
    seen = await client.get(f"{BASE}/templates", headers=_auth(other))
    assert seen.status_code == 200 and seen.json()["templates"] == []
    r = await client.patch(
        f"{BASE}/templates/{template['id']}", json={"approved": True}, headers=_auth(other)
    )
    assert r.status_code == 404, "another workspace cannot approve this one's template"
    r = await client.put(
        f"{BASE}/events/{EVENT}", json={"template_id": template["id"]}, headers=_auth(other)
    )
    assert r.status_code == 404


async def test_opting_in_uses_the_mobile_on_the_persons_own_record(client, session, tenant) -> None:
    with_phone = await _user(session, tenant, UserRole.EMPLOYEE, "meera", phone="98450 11008")
    without = await _user(session, tenant, UserRole.EMPLOYEE, "ravi")

    r = await client.put(
        "/api/v1/notifications/whatsapp/consent", json={"opted_in": True}, headers=_auth(without)
    )
    assert r.status_code == 422 and "mobile number to your profile" in r.text

    r = await client.put(
        "/api/v1/notifications/whatsapp/consent",
        json={"opted_in": True},
        headers=_auth(with_phone),
    )
    assert r.status_code == 200, r.text
    assert r.json()["phone_e164"] == "+919845011008" and r.json()["opted_in"] is True

    # Opting out later keeps the number it was given, with the time it ended.
    r = await client.put(
        "/api/v1/notifications/whatsapp/consent",
        json={"opted_in": False},
        headers=_auth(with_phone),
    )
    assert r.status_code == 200
    assert r.json()["opted_in"] is False and r.json()["phone_e164"] == "+919845011008"

    # A number typed in explicitly wins over the profile.
    r = await client.put(
        "/api/v1/notifications/whatsapp/consent",
        json={"opted_in": True, "phone_e164": "+447700900123"},
        headers=_auth(with_phone),
    )
    assert r.status_code == 200 and r.json()["phone_e164"] == "+447700900123"
