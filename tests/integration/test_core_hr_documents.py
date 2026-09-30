"""Core HR documents, through HTTP: the vault, persisted letter PDFs, template authoring.

These go through the real app so the role gates, the access-policy rules and the
multipart parsing are exercised as a browser would exercise them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.documents import FieldSource, LetterCategory, LetterTemplate, LetterTemplateField
from app.models.employee import Employee, EmployeeDocument
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.documents import service as letter_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path / "store"))
    return tmp_path / "store"


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


async def _login_for(
    session: AsyncSession, tenant: Tenant, employee: Employee | None, role: UserRole, tag: str
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{tag}@acme.test",
        password_hash="x",
        full_name=tag,
        role=role,
        is_active=True,
        employee_id=employee.id if employee else None,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _colleague(session: AsyncSession, tenant: Tenant, code: str) -> Employee:
    person = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code, work_email=f"{code}@acme.test"
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _upload(client, hr, employee_id, *, data=PDF, visible=True, title="Passport"):
    return await client.post(
        "/api/v1/documents",
        headers=_auth(hr),
        data={
            "employee_id": str(employee_id),
            "category": "id_proof",
            "title": title,
            "is_employee_visible": "true" if visible else "false",
            "valid_until": "2030-01-01",
        },
        files={"file": ("../../evil.exe", data, "text/html")},
    )


# ═══════════════════════════════════════════════════════════════════════════
# Vault
# ═══════════════════════════════════════════════════════════════════════════


async def test_hr_uploads_and_the_employee_downloads_as_an_attachment(
    client, session, tenant, hr_user, employee, _storage_root
) -> None:
    response = await _upload(client, hr_user, employee.id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mime_type"] == "application/pdf"
    assert "file_url" not in body

    row = await session.get(EmployeeDocument, uuid.UUID(body["id"]))
    assert "evil" not in row.file_url
    assert row.file_url.startswith(f"{tenant.id}/documents/")
    assert (_storage_root / row.file_url).read_bytes() == PDF

    me = await _login_for(session, tenant, employee, UserRole.EMPLOYEE, "anita")
    listed = await client.get("/api/v1/documents", headers=_auth(me))
    assert [d["id"] for d in listed.json()] == [body["id"]]

    download = await client.get(f"/api/v1/documents/{body['id']}/download", headers=_auth(me))
    assert download.status_code == 200
    assert download.content == PDF
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["content-type"] == "application/pdf"


async def test_content_is_sniffed_not_trusted(client, hr_user, employee) -> None:
    html = await _upload(client, hr_user, employee.id, data=b"<html><script>x</script>")
    assert html.status_code == 415
    png = await _upload(client, hr_user, employee.id, data=PNG)
    assert png.status_code == 201
    assert png.json()["mime_type"] == "image/png"


async def test_an_oversized_file_is_refused(client, hr_user, employee, monkeypatch) -> None:
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 64)
    response = await _upload(client, hr_user, employee.id, data=PDF + b"\x00" * 100)
    assert response.status_code == 413


async def test_a_hidden_document_is_a_404_to_its_employee(
    client, session, tenant, hr_user, employee
) -> None:
    hidden = (await _upload(client, hr_user, employee.id, visible=False)).json()
    me = await _login_for(session, tenant, employee, UserRole.EMPLOYEE, "anita2")
    assert (await client.get("/api/v1/documents", headers=_auth(me))).json() == []
    response = await client.get(f"/api/v1/documents/{hidden['id']}/download", headers=_auth(me))
    assert response.status_code == 404


async def test_a_colleague_cannot_list_or_download_somebody_elses_documents(
    client, session, tenant, hr_user, employee
) -> None:
    doc = (await _upload(client, hr_user, employee.id)).json()
    other = await _colleague(session, tenant, "COL1")
    colleague = await _login_for(session, tenant, other, UserRole.MANAGER, "col")

    listed = await client.get(
        f"/api/v1/documents?employee_id={employee.id}", headers=_auth(colleague)
    )
    assert listed.status_code == 403
    download = await client.get(
        f"/api/v1/documents/{doc['id']}/download", headers=_auth(colleague)
    )
    assert download.status_code == 404
    upload = await _upload(client, colleague, employee.id)
    assert upload.status_code == 403


async def test_another_tenant_sees_nothing(client, hr_user, other_hr_user, employee) -> None:
    doc = (await _upload(client, hr_user, employee.id)).json()
    assert (await client.get("/api/v1/documents", headers=_auth(other_hr_user))).json() == []
    for method, path in (
        ("GET", f"/api/v1/documents/{doc['id']}/download"),
        ("DELETE", f"/api/v1/documents/{doc['id']}"),
    ):
        response = await client.request(method, path, headers=_auth(other_hr_user))
        assert response.status_code == 404
    upload = await _upload(client, other_hr_user, employee.id)
    assert upload.status_code == 404


async def test_hr_cannot_manage_their_own_vault(client, session, tenant, employee) -> None:
    own_hr = await _login_for(session, tenant, employee, UserRole.HR_ADMIN, "selfhr")
    response = await _upload(client, own_hr, employee.id)
    assert response.status_code == 403


async def test_edit_and_delete_are_audited_and_delete_removes_the_file(
    client, session, hr_user, employee, _storage_root
) -> None:
    doc = (await _upload(client, hr_user, employee.id)).json()
    row = await session.get(EmployeeDocument, uuid.UUID(doc["id"]))
    key = row.file_url

    edited = await client.patch(
        f"/api/v1/documents/{doc['id']}",
        headers=_auth(hr_user),
        json={"title": "Passport (renewed)", "is_employee_visible": False},
    )
    assert edited.status_code == 200
    assert edited.json()["is_employee_visible"] is False

    deleted = await client.delete(f"/api/v1/documents/{doc['id']}", headers=_auth(hr_user))
    assert deleted.status_code == 204
    assert not (_storage_root / key).exists()

    actions = (
        await session.scalars(
            select(AuditLog.action).where(AuditLog.entity_type == "employee_document")
        )
    ).all()
    assert {a.value for a in actions} >= {"create", "update", "delete"}


async def test_expiring_documents_are_listed_for_hr_only(
    client, session, tenant, hr_user, employee
) -> None:
    await client.post(
        "/api/v1/documents",
        headers=_auth(hr_user),
        data={
            "employee_id": str(employee.id),
            "category": "id_proof",
            "title": "Visa",
            "valid_until": date.today().isoformat(),
        },
        files={"file": ("v.pdf", PDF, "application/pdf")},
    )
    response = await client.get("/api/v1/documents/expiring", headers=_auth(hr_user))
    assert [d["title"] for d in response.json()] == ["Visa"]
    me = await _login_for(session, tenant, employee, UserRole.EMPLOYEE, "anita3")
    assert (await client.get("/api/v1/documents/expiring", headers=_auth(me))).status_code == 403


async def test_number_remark_and_dependant_are_kept_and_dates_are_checked(
    client, hr_user, employee
) -> None:
    response = await client.post(
        "/api/v1/documents",
        headers=_auth(hr_user),
        data={
            "employee_id": str(employee.id),
            "category": "medical",
            "title": "Birth certificate",
            "document_number": "  BC-2019-44  ",
            "remark": "Daughter, for medical cover",
            "dependant_name": "Riya",
            "issued_on": "2019-05-01",
        },
        files={"file": ("b.pdf", PDF, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["document_number"] == "BC-2019-44"
    assert body["dependant_name"] == "Riya"
    assert body["remark"] == "Daughter, for medical cover"

    backwards = await client.patch(
        f"/api/v1/documents/{body['id']}",
        headers=_auth(hr_user),
        json={"valid_until": "2018-01-01"},
    )
    assert backwards.status_code == 422

    cleared = await client.patch(
        f"/api/v1/documents/{body['id']}",
        headers=_auth(hr_user),
        json={"dependant_name": "   ", "remark": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["dependant_name"] is None
    assert cleared.json()["remark"] is None


# ═══════════════════════════════════════════════════════════════════════════
# Company documents: policies and handbooks
# ═══════════════════════════════════════════════════════════════════════════


async def _company_doc(client, hr, *, title="Leave policy 2026", published=True):
    return await client.post(
        "/api/v1/documents/company",
        headers=_auth(hr),
        data={
            "document_type": "Leave policy",
            "title": title,
            "document_number": "HR-POL-004",
            "issued_on": "2026-04-01",
            "is_employee_visible": "true" if published else "false",
        },
        files={"file": ("policy.pdf", PDF, "application/pdf")},
    )


async def test_employees_read_published_company_documents_only(
    client, session, tenant, hr_user, employee
) -> None:
    published = await _company_doc(client, hr_user)
    assert published.status_code == 201, published.text
    draft = (await _company_doc(client, hr_user, title="Draft PIP policy", published=False)).json()

    hr_titles = [
        d["title"]
        for d in (await client.get("/api/v1/documents/company", headers=_auth(hr_user))).json()
    ]
    assert sorted(hr_titles) == ["Draft PIP policy", "Leave policy 2026"]

    me = await _login_for(session, tenant, employee, UserRole.EMPLOYEE, "reader")
    mine = (await client.get("/api/v1/documents/company", headers=_auth(me))).json()
    assert [d["title"] for d in mine] == ["Leave policy 2026"]
    assert "file_url" not in mine[0]

    download = await client.get(
        f"/api/v1/documents/company/{mine[0]['id']}/download", headers=_auth(me)
    )
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    hidden = await client.get(
        f"/api/v1/documents/company/{draft['id']}/download", headers=_auth(me)
    )
    assert hidden.status_code == 404

    # Reading is open to everyone; writing is HR's.
    refused = await client.post(
        "/api/v1/documents/company",
        headers=_auth(me),
        data={"document_type": "IT policy", "title": "Mine"},
        files={"file": ("p.pdf", PDF, "application/pdf")},
    )
    assert refused.status_code == 403
    assert (
        await client.delete(f"/api/v1/documents/company/{mine[0]['id']}", headers=_auth(me))
    ).status_code == 403


async def test_company_documents_are_tenant_scoped_and_audited(
    client, session, hr_user, other_hr_user, _storage_root
) -> None:
    doc = (await _company_doc(client, hr_user)).json()
    assert (
        await client.get("/api/v1/documents/company", headers=_auth(other_hr_user))
    ).json() == []
    for method, path in (
        ("GET", f"/api/v1/documents/company/{doc['id']}/download"),
        ("DELETE", f"/api/v1/documents/company/{doc['id']}"),
    ):
        response = await client.request(method, path, headers=_auth(other_hr_user))
        assert response.status_code == 404

    edited = await client.patch(
        f"/api/v1/documents/company/{doc['id']}",
        headers=_auth(hr_user),
        json={"is_employee_visible": False, "valid_until": "2027-03-31"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["is_employee_visible"] is False
    backwards = await client.patch(
        f"/api/v1/documents/company/{doc['id']}",
        headers=_auth(hr_user),
        json={"valid_until": "2020-01-01"},
    )
    assert backwards.status_code == 422

    assert (await client.get("/api/v1/documents/company", headers=_auth(hr_user))).json()[0][
        "valid_until"
    ] == "2027-03-31"
    deleted = await client.delete(
        f"/api/v1/documents/company/{doc['id']}", headers=_auth(hr_user)
    )
    assert deleted.status_code == 204
    assert not any(path.is_file() for path in _storage_root.rglob("*"))

    actions = (
        await session.scalars(
            select(AuditLog.action).where(AuditLog.entity_type == "company_document")
        )
    ).all()
    assert {a.value for a in actions} >= {"create", "update", "delete"}


# ═══════════════════════════════════════════════════════════════════════════
# Letters: persisted PDFs, template edit and preview
# ═══════════════════════════════════════════════════════════════════════════


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


async def test_an_issued_pdf_survives_a_restart(
    client, session, tenant, hr_user, employee, _storage_root
) -> None:
    """The in-memory cache is gone: the PDF is on disk under a generated key."""
    template = await _template(session, tenant)
    issued = await client.post(
        "/api/v1/letters/issue",
        headers=_auth(hr_user),
        json={"employee_id": str(employee.id), "template_id": str(template.id)},
    )
    assert issued.status_code == 201, issued.text
    letter_id = issued.json()["id"]

    letter, _ = await letter_service.verify_letter(session, hr_user, uuid.UUID(letter_id))
    stored = (_storage_root / letter.pdf_storage_key).read_bytes()
    assert stored.startswith(b"%PDF")
    assert not hasattr(letter_service, "_PDF_CACHE")

    me = await _login_for(session, tenant, employee, UserRole.EMPLOYEE, "anita4")
    pdf = await client.get(f"/api/v1/letters/{letter_id}/pdf", headers=_auth(me))
    assert pdf.status_code == 200
    assert pdf.content == stored
    assert pdf.headers["content-disposition"].startswith("attachment;")

    # Lose the file: the verified content is re-rendered to the same bytes.
    (_storage_root / letter.pdf_storage_key).unlink()
    again = await client.get(f"/api/v1/letters/{letter_id}/pdf", headers=_auth(hr_user))
    assert again.status_code == 200
    assert again.content == stored


async def test_a_colleague_cannot_open_somebody_elses_letter(
    client, session, tenant, hr_user, employee
) -> None:
    template = await _template(session, tenant)
    letter_id = (
        await client.post(
            "/api/v1/letters/issue",
            headers=_auth(hr_user),
            json={"employee_id": str(employee.id), "template_id": str(template.id)},
        )
    ).json()["id"]
    other = await _colleague(session, tenant, "COL2")
    colleague = await _login_for(session, tenant, other, UserRole.EMPLOYEE, "col2")
    for suffix in ("pdf", "content", "verify"):
        response = await client.get(
            f"/api/v1/letters/{letter_id}/{suffix}", headers=_auth(colleague)
        )
        assert response.status_code == 404, suffix


async def test_editing_a_template_creates_a_version_and_refuses_bad_tokens(
    client, session, tenant, hr_user
) -> None:
    template = await _template(session, tenant)
    body = {
        "name": "Confirmation",
        "category": "confirmation",
        "body_html": "<p>Dear {{employee.frist_name}}</p>",
        "fields": [],
    }
    bad = await client.put(
        f"/api/v1/letter-templates/{template.id}", headers=_auth(hr_user), json=body
    )
    assert bad.status_code == 422
    assert "frist_name" in bad.json()["detail"]

    body["body_html"] = "<p>Dear {{employee.first_name}}, welcome aboard.</p>"
    good = await client.put(
        f"/api/v1/letter-templates/{template.id}", headers=_auth(hr_user), json=body
    )
    assert good.status_code == 200, good.text
    assert good.json()["version"] == 2
    await session.refresh(template)
    assert template.active is False
    assert "you are confirmed" in template.body_html

    versions = await client.get(
        f"/api/v1/letter-templates/{template.id}/versions", headers=_auth(hr_user)
    )
    assert [v["version"] for v in versions.json()] == [2, 1]


async def test_preview_consumes_no_reference_number(
    client, session, tenant, hr_user, employee
) -> None:
    template = await _template(session, tenant)
    real = await client.post(
        f"/api/v1/letter-templates/{template.id}/preview",
        headers=_auth(hr_user),
        json={"employee_id": str(employee.id)},
    )
    assert real.status_code == 200
    assert "Anita" in real.json()["html"]
    assert real.json()["is_sample"] is False

    sample = await client.post(
        "/api/v1/letter-templates/preview",
        headers=_auth(hr_user),
        json={"body_html": "<p>{{employee.first_name}} earns {{salary.ctc_annual}}</p>"},
    )
    assert sample.status_code == 200
    data = sample.json()
    assert data["is_sample"] is True
    assert "[first name]" in data["html"]
    assert any("salary.ctc_annual" in p for p in data["problems"])

    from app.models.documents import IssuedLetter, LetterSequence

    assert await session.scalar(select(func.count(IssuedLetter.id))) == 0
    assert await session.scalar(select(func.count(LetterSequence.id))) == 0


async def test_letter_approval_needs_somebody_other_than_the_issuer(
    client, session, tenant, hr_user, employee
) -> None:
    template = await _template(session, tenant)
    template.requires_approval = True
    await session.commit()
    letter_id = (
        await client.post(
            "/api/v1/letters/issue",
            headers=_auth(hr_user),
            json={"employee_id": str(employee.id), "template_id": str(template.id)},
        )
    ).json()["id"]
    own = await client.post(f"/api/v1/letters/{letter_id}/approve", headers=_auth(hr_user))
    assert own.status_code == 403

    second = await _login_for(session, tenant, None, UserRole.TENANT_ADMIN, "admin2")
    approved = await client.post(f"/api/v1/letters/{letter_id}/approve", headers=_auth(second))
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    revoked = await client.post(
        f"/api/v1/letters/{letter_id}/revoke",
        headers=_auth(second),
        json={"reason": "Issued in error"},
    )
    assert revoked.json()["status"] == "revoked"


async def test_the_signature_provider_is_reported_honestly(client, hr_user) -> None:
    response = await client.get("/api/v1/letters/signature-provider", headers=_auth(hr_user))
    assert response.status_code == 200
    assert response.json()["provider"] == "noop"
    assert response.json()["connected"] is False
    assert "not connected" in response.json()["detail"]
