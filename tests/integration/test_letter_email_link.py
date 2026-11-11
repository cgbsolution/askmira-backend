# ruff: noqa: F811 - pytest fixtures imported from another module are re-used by name
"""A letter read, signed and accepted from its email link, with no sign-in.

The link names one letter and the person it was issued to. It shows the letter
as page pictures, takes a typed name and a signature picture, prints the
signature into a signed copy that is kept, and does not keep the picture.
Employees see no letters in the portal: the link is their way in.
"""

from __future__ import annotations

import hashlib
import io
import uuid

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.core.config import settings
from app.core.security import create_access_token, create_letter_link_token
from app.models.audit import AuditLog
from app.models.letter_acknowledgement import LetterAcknowledgement
from app.models.user import UserRole
from app.services.documents import letter_link
from tests.integration.conftest import requires_db
from tests.integration.test_core_hr_documents import (  # noqa: F401 - fixtures
    _auth,
    _login_for,
    _storage_root,
    _template,
    client,
)

pytestmark = requires_db


def _signature() -> bytes:
    image = Image.new("RGB", (600, 200), (255, 255, 255))
    ImageDraw.Draw(image).line(
        [(40, 150), (150, 40), (260, 160), (420, 50)], fill=(0, 0, 0), width=6
    )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@pytest.fixture(autouse=True)
def _portal(monkeypatch):
    monkeypatch.setattr(settings, "WEB_APP_URL", "https://portal.example", raising=False)


async def _issued(client, session, tenant, hr_user, employee) -> str:
    template = await _template(session, tenant)
    issued = await client.post(
        "/api/v1/letters/issue",
        headers=_auth(hr_user),
        json={"employee_id": str(employee.id), "template_id": str(template.id)},
    )
    assert issued.status_code == 201, issued.text
    return issued.json()["id"]


def _token(tenant, letter_id: str, employee) -> str:
    url = letter_link.link_for(tenant.id, uuid.UUID(letter_id), employee.id)
    assert url and url.startswith("https://portal.example/letter/")
    return url.rsplit("/", 1)[1]


def _sign(client, token: str, *, name: str, accept: bool = True, signature: bytes | None = None):
    files = {
        "signature": ("sig.png", signature if signature is not None else _signature(), "image/png")
    }
    return client.post(
        f"/api/v1/public/letters/{token}/acknowledge",
        data={"typed_name": name, "accept": "true" if accept else "false"},
        files=files,
    )


async def test_the_link_shows_the_letter_and_accepts_it_with_a_signature(
    client, session, tenant, hr_user, employee, _storage_root
) -> None:
    letter_id = await _issued(client, session, tenant, hr_user, employee)
    token = _token(tenant, letter_id, employee)

    described = await client.get(f"/api/v1/public/letters/{token}")
    assert described.status_code == 200, described.text
    body = described.json()
    assert body["state"] == "waiting"
    assert body["employee_name"] == employee.full_name
    assert body["letter_name"] == "Confirmation" and body["page_count"] >= 1

    picture = await client.get(f"/api/v1/public/letters/{token}/pages/1")
    assert picture.status_code == 200 and picture.content.startswith(b"\x89PNG")
    assert picture.headers["cache-control"] == "private, no-store"
    assert (await client.get(f"/api/v1/public/letters/{token}/pages/99")).status_code == 404
    original = (await client.get(f"/api/v1/public/letters/{token}/pdf")).content
    assert original.startswith(b"%PDF")

    # Wrong name, no tick, not a picture: refused, nothing recorded.
    wrong = await _sign(client, token, name="Somebody Else")
    assert wrong.status_code == 422 and employee.full_name in wrong.json()["detail"]
    assert (await _sign(client, token, name=employee.full_name, accept=False)).status_code == 422
    not_image = await _sign(client, token, name=employee.full_name, signature=b"%PDF-1.4 nope")
    assert not_image.status_code == 422
    assert await session.scalar(select(LetterAcknowledgement)) is None

    files_before = {p for p in _storage_root.rglob("*") if p.is_file()}
    signed = await _sign(client, token, name=f"  {employee.full_name.upper()} ")
    assert signed.status_code == 200, signed.text
    assert signed.json()["state"] == "acknowledged"

    row = await session.scalar(select(LetterAcknowledgement))
    await session.refresh(row)
    assert row.channel == "email_link" and row.acknowledged_by_user_id is None
    assert row.signed_copy_storage_key and row.signed_copy_hash
    # Only the signed copy was written: the signature picture is kept nowhere.
    new_files = {p for p in _storage_root.rglob("*") if p.is_file()} - files_before
    assert len(new_files) == 1
    copy = new_files.pop().read_bytes()
    assert hashlib.sha256(copy).hexdigest() == row.signed_copy_hash
    assert copy != original

    again = await client.get(f"/api/v1/public/letters/{token}")
    assert again.json()["state"] == "acknowledged" and again.json()["has_signed_copy"] is True
    download = await client.get(f"/api/v1/public/letters/{token}/pdf")
    assert download.content == copy
    assert "-signed.pdf" in download.headers["content-disposition"]
    assert (await _sign(client, token, name=employee.full_name)).status_code == 409

    audit = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(letter_id)))
    ).all()
    accepted = [a for a in audit if a.actor_type == "employee_link"]
    assert len(accepted) == 1 and accepted[0].changes["signature"] == "uploaded image"

    # HR sees the signed copy on the letter.
    listed = await client.get("/api/v1/letters", headers=_auth(hr_user))
    mine = next(x for x in listed.json() if x["id"] == letter_id)
    assert mine["acknowledged_at"] and mine["has_signed_pdf"] is True
    hr_copy = await client.get(
        f"/api/v1/letters/{letter_id}/pdf?signed=true", headers=_auth(hr_user)
    )
    assert hr_copy.status_code == 200 and hr_copy.content == copy


async def test_employees_see_no_letters_in_the_portal(
    client, session, tenant, hr_user, employee
) -> None:
    letter_id = await _issued(client, session, tenant, hr_user, employee)
    me = await _login_for(session, tenant, employee, UserRole.EMPLOYEE, "anita-portal")
    listed = await client.get("/api/v1/letters", headers=_auth(me))
    assert listed.status_code == 200 and listed.json() == []
    for path in ("content", "pdf", "pages/1"):
        got = await client.get(f"/api/v1/letters/{letter_id}/{path}", headers=_auth(me))
        assert got.status_code == 404, path


async def test_a_withdrawn_letter_closes_its_link(
    client, session, tenant, hr_user, employee
) -> None:
    letter_id = await _issued(client, session, tenant, hr_user, employee)
    token = _token(tenant, letter_id, employee)
    revoked = await client.post(
        f"/api/v1/letters/{letter_id}/revoke", headers=_auth(hr_user), json={"reason": "Wrong CTC"}
    )
    assert revoked.status_code == 200, revoked.text
    assert (await client.get(f"/api/v1/public/letters/{token}")).json()["state"] == "withdrawn"
    assert (await client.get(f"/api/v1/public/letters/{token}/pages/1")).status_code == 410
    assert (await _sign(client, token, name=employee.full_name)).status_code == 409


async def test_a_bad_or_borrowed_token_opens_nothing(
    client, session, tenant, hr_user, employee
) -> None:
    letter_id = await _issued(client, session, tenant, hr_user, employee)
    access = create_access_token(user_id=str(hr_user.id), tenant_id=str(tenant.id), role="admin")
    other_person = create_letter_link_token(
        letter_id=letter_id, tenant_id=str(tenant.id), employee_id=str(uuid.uuid4())
    )
    other_tenant = create_letter_link_token(
        letter_id=letter_id, tenant_id=str(uuid.uuid4()), employee_id=str(employee.id)
    )
    for token in ("garbage", access, other_person, other_tenant):
        got = await client.get(f"/api/v1/public/letters/{token}")
        assert got.status_code == 410, token


async def test_asking_for_acknowledgement_puts_the_link_in_the_email(
    client, session, tenant, hr_user, employee
) -> None:
    letter_id = await _issued(client, session, tenant, hr_user, employee)
    me = await _login_for(session, tenant, employee, UserRole.EMPLOYEE, "anita-mail")
    someone = await _login_for(session, tenant, None, UserRole.EMPLOYEE, "someone-else")
    payload = letter_link.payload_for(
        type("L", (), {"id": uuid.UUID(letter_id), "employee_id": employee.id})()
    )
    (button,) = letter_link.buttons_for(me, payload)
    assert button.url.startswith("https://portal.example/letter/")
    assert letter_link.buttons_for(someone, payload) == ()


async def test_without_a_login_the_link_is_emailed_directly(
    client, session, tenant, hr_user, employee, monkeypatch
) -> None:
    letter_id = await _issued(client, session, tenant, hr_user, employee)
    sent: list[str] = []

    async def fake(session, *, letter, event_key, payload):
        sent.append(event_key)
        return 1

    monkeypatch.setattr(letter_link, "email_without_login", fake)
    asked = await client.post(
        f"/api/v1/letters/{letter_id}/request-acknowledgement", headers=_auth(hr_user)
    )
    assert asked.status_code == 200, asked.text
    assert sent == ["documents.letter.acknowledgement_requested"]
