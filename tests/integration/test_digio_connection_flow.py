"""A workspace connects its own Digio account and letters go out for Aadhaar eSign on it.

Digio itself is a double (httpx.MockTransport behind the provider's client), so
what is proved here is ours: credentials checked before they are kept and never
returned, the signer told by Digio, the letter moving to Signed by webhook or by
asking Digio, and one workspace's webhook unable to touch another's letters.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.documents import (
    FieldSource,
    IssuedLetter,
    LetterCategory,
    LetterTemplate,
    LetterTemplateField,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.documents.signature import DigioProvider
from tests.integration.conftest import requires_db

pytestmark = requires_db

PDF = b"%PDF-1.4 signed\n%%EOF\n"


class FakeDigio:
    """Digio, as far as these tests need it."""

    def __init__(self) -> None:
        self.reject_credentials = False
        self.status = "requested"
        self.uploads: list[dict] = []
        self.bases: list[str] = []
        self.counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bases.append(f"{request.url.scheme}://{request.url.host}:{request.url.port}")
        if self.reject_credentials:
            # What Digio really sends for a wrong id or secret (seen 2026-11-09).
            return httpx.Response(
                500,
                json={
                    "details": "EX1",
                    "code": "SYSTEM_ERROR",
                    "message": "Invalid API credentials",
                },
            )
        path = request.url.path
        if path.endswith("/uploadpdf"):
            self.counter += 1
            self.uploads.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"DID{self.counter}", "signing_parties": [{}]})
        if path.endswith("/document/download"):
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        if "DIDASKMIRACHECK" in path:
            return httpx.Response(404, json={"message": "Document not found"})
        return httpx.Response(
            200, json={"id": path.rsplit("/", 1)[-1], "agreement_status": self.status}
        )


@pytest.fixture
def digio(monkeypatch) -> FakeDigio:
    fake = FakeDigio()

    def client(self, timeout: float = 30):
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            auth=(self._client_id, self._client_secret),
            transport=httpx.MockTransport(fake.handler),
        )

    monkeypatch.setattr(DigioProvider, "_client", client)
    return fake


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path / "store"))


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


async def _user(session: AsyncSession, tenant: Tenant, role: UserRole, tag: str) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{tag}@acme.test",
        password_hash="x",
        full_name=tag,
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _template(session: AsyncSession, tenant: Tenant) -> LetterTemplate:
    template = LetterTemplate(
        tenant_id=tenant.id,
        code="CONF",
        name="Confirmation",
        category=LetterCategory.CONFIRMATION,
        body_html="<p>Dear {{employee.first_name}}, you are confirmed.</p>",
        version=1,
        active=True,
    )
    template.fields.append(
        LetterTemplateField(
            tenant_id=tenant.id,
            token="employee.first_name",
            label="First name",
            source=FieldSource.EMPLOYEE,
        )
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


async def _connect(client, admin, **overrides):
    body = {
        "environment": "sandbox",
        "client_id": "AI1234",
        "client_secret": "digio-secret-value",
        "webhook_secret": "hook-secret-value",
    }
    body.update(overrides)
    return await client.put("/api/v1/integrations/digio", headers=_auth(admin), json=body)


async def _issue(client, hr, employee, template) -> str:
    issued = await client.post(
        "/api/v1/letters/issue",
        headers=_auth(hr),
        json={"employee_id": str(employee.id), "template_id": str(template.id)},
    )
    assert issued.status_code == 201, issued.text
    return issued.json()["id"]


async def test_refused_credentials_are_not_kept(client, session, tenant, hr_user, digio) -> None:
    admin = await _user(session, tenant, UserRole.ADMIN, "adm1")
    digio.reject_credentials = True
    refused = await _connect(client, admin)
    assert refused.status_code == 422
    assert "refused these credentials" in refused.json()["detail"]
    state = (await client.get("/api/v1/integrations/digio", headers=_auth(admin))).json()
    assert state["connected"] is False


async def test_only_an_admin_connects(client, session, tenant, hr_user, digio) -> None:
    sub = await _user(session, tenant, UserRole.SUB_ADMIN, "sub1")
    assert (await _connect(client, sub)).status_code == 403
    assert (
        await client.get("/api/v1/integrations/catalogue", headers=_auth(sub))
    ).status_code == 403


async def test_letters_go_out_on_the_workspaces_digio_and_come_back_signed(
    client, session, tenant, hr_user, employee, digio
) -> None:
    admin = await _user(session, tenant, UserRole.ADMIN, "adm2")
    connected = await _connect(client, admin)
    assert connected.status_code == 200, connected.text
    body = connected.text
    assert "digio-secret-value" not in body and "hook-secret-value" not in body
    view = connected.json()
    assert view["connected"] is True
    assert view["settings"] == {"environment": "sandbox", "client_id": "AI1234"}
    assert view["has_webhook_secret"] is True
    assert digio.bases[-1] == "https://ext.digio.in:444"

    status = (await client.get("/api/v1/letters/signature-provider", headers=_auth(hr_user))).json()
    assert status["connected"] is True and "sandbox" in status["detail"]

    template = await _template(session, tenant)

    # 1. Sent: the signer is told by Digio.
    letter_id = await _issue(client, hr_user, employee, template)
    sent = await client.post(
        f"/api/v1/letters/{letter_id}/send-for-signature", headers=_auth(hr_user), json={}
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "sent"
    upload = digio.uploads[-1]
    assert upload["notify_signers"] is True and upload["send_sign_link"] is True
    assert upload["signers"][0]["sign_type"] == "aadhaar"

    # 2. Not signed yet: asking changes nothing.
    still = await client.post(
        f"/api/v1/letters/{letter_id}/signature-status", headers=_auth(hr_user)
    )
    assert still.status_code == 200 and still.json()["status"] == "sent"

    # 3. Signed at Digio: asking moves it on and keeps the signed copy.
    digio.status = "completed"
    signed = await client.post(
        f"/api/v1/letters/{letter_id}/signature-status", headers=_auth(hr_user)
    )
    assert signed.json()["status"] == "signed"
    letter = await session.get(IssuedLetter, uuid.UUID(letter_id))
    await session.refresh(letter)
    assert letter.signed_pdf_storage_key

    # 4. A second letter, signed by webhook on this workspace's own address.
    digio.status = "requested"
    second_id = await _issue(client, hr_user, employee, template)
    await client.post(
        f"/api/v1/letters/{second_id}/send-for-signature", headers=_auth(hr_user), json={}
    )
    second = await session.get(IssuedLetter, uuid.UUID(second_id))
    await session.refresh(second)
    hook = (await client.get("/api/v1/integrations/digio", headers=_auth(admin))).json()[
        "webhook_path"
    ]
    payload = json.dumps(
        {
            "entities": {
                "document": {"id": second.signature_request_id, "agreement_status": "completed"}
            }
        }
    ).encode()

    forged = await client.post(
        hook,
        content=payload,
        headers={"x-digio-checksum": "0" * 64, "content-type": "application/json"},
    )
    assert forged.status_code == 401

    good = hmac.new(b"hook-secret-value", payload, hashlib.sha256).hexdigest()
    accepted = await client.post(
        hook,
        content=payload,
        headers={"x-digio-checksum": good, "content-type": "application/json"},
    )
    assert accepted.status_code == 200 and accepted.json()["status"] == "ok"
    await session.refresh(second)
    assert second.status.value == "signed"

    unknown = await client.post(
        f"/api/v1/webhooks/signature/digio/w/{uuid.uuid4()}",
        content=payload,
        headers={"x-digio-checksum": good},
    )
    assert unknown.status_code == 404


async def test_another_workspaces_webhook_cannot_sign_this_workspaces_letter(
    client, session, tenant, other_tenant, hr_user, other_hr_user, employee, digio
) -> None:
    admin = await _user(session, tenant, UserRole.ADMIN, "adm3")
    other_admin = await _user(session, other_tenant, UserRole.ADMIN, "adm4")
    assert (await _connect(client, admin)).status_code == 200
    other = await _connect(client, other_admin, webhook_secret="other-hook")
    assert other.status_code == 200
    other_hook = other.json()["webhook_path"]

    template = await _template(session, tenant)
    letter_id = await _issue(client, hr_user, employee, template)
    await client.post(
        f"/api/v1/letters/{letter_id}/send-for-signature", headers=_auth(hr_user), json={}
    )
    letter = await session.get(IssuedLetter, uuid.UUID(letter_id))
    await session.refresh(letter)

    payload = json.dumps(
        {
            "entities": {
                "document": {"id": letter.signature_request_id, "agreement_status": "completed"}
            }
        }
    ).encode()
    signed_by_other = hmac.new(b"other-hook", payload, hashlib.sha256).hexdigest()
    response = await client.post(
        other_hook, content=payload, headers={"x-digio-checksum": signed_by_other}
    )
    assert response.status_code == 200 and response.json()["status"] == "unknown_request"
    await session.refresh(letter)
    assert letter.status.value == "sent"


async def test_a_blank_secret_keeps_the_stored_one_and_disconnect_falls_back(
    client, session, tenant, hr_user, digio
) -> None:
    admin = await _user(session, tenant, UserRole.ADMIN, "adm5")
    assert (await _connect(client, admin)).status_code == 200
    moved = await _connect(
        client, admin, environment="production", client_secret="", webhook_secret=""
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["settings"]["environment"] == "production"
    assert digio.bases[-1].startswith("https://api.digio.in:")

    bad_env = await _connect(client, admin, environment="staging")
    assert bad_env.status_code == 422

    gone = await client.delete("/api/v1/integrations/digio", headers=_auth(admin))
    assert gone.status_code == 204
    status = (await client.get("/api/v1/letters/signature-provider", headers=_auth(hr_user))).json()
    assert status["connected"] is False
    again = await client.delete("/api/v1/integrations/digio", headers=_auth(admin))
    assert again.status_code == 404


async def test_the_catalogue_lists_every_integration_with_this_workspaces_state(
    client, session, tenant, hr_user, digio
) -> None:
    admin = await _user(session, tenant, UserRole.ADMIN, "adm6")
    rows = (await client.get("/api/v1/integrations/catalogue", headers=_auth(admin))).json()
    assert [r["key"] for r in rows] == [
        "banks",
        "accounting",
        "statutory",
        "devices",
        "identity",
        "job_boards",
        "calendars",
        "messaging",
        "chat",
        "esign",
        "mcp",
        "bi",
        "api",
    ]
    for row in rows:
        assert row["status"] in {"available", "configurable"}
        assert row["state"] in {"connected", "ready", "needs_setup", "platform_setup"}
        for field in ("direction", "frequency", "auth", "limitations", "href", "detail"):
            assert row[field], (row["key"], field)
    esign = next(r for r in rows if r["key"] == "esign")
    assert esign["state"] == "needs_setup"

    await _connect(client, admin)
    rows = (await client.get("/api/v1/integrations/catalogue", headers=_auth(admin))).json()
    esign = next(r for r in rows if r["key"] == "esign")
    assert esign["state"] == "connected" and "sandbox" in esign["detail"]


async def test_the_catalogue_follows_linked_products_and_switched_off_modules(
    client, session, tenant, hr_user
) -> None:
    """Found in the browser: with hiring run in AnytimeHire the row said "Add a board
    account" for a page that hands off to AnytimeHire. And a switched-off module's row
    must not say "set up" for a page nobody can open."""
    admin = await _user(session, tenant, UserRole.ADMIN, "adm7")
    tenant.settings = {**(tenant.settings or {}), "linked_products": {"interview": True}}
    tenant.enabled_modules = {**(tenant.enabled_modules or {}), "hiring": False, "payroll": False}
    await session.commit()

    rows = {
        r["key"]: r
        for r in (await client.get("/api/v1/integrations/catalogue", headers=_auth(admin))).json()
    }
    assert rows["job_boards"]["state"] == "ready"
    assert "AnytimeHire" in rows["job_boards"]["detail"]
    assert "AnytimeHire" in rows["calendars"]["detail"]
    for key in ("banks", "accounting", "statutory"):
        assert rows[key]["state"] == "needs_setup"
        assert "Payroll module is off" in rows[key]["detail"]
        assert rows[key]["href"] == "/settings/modules"
