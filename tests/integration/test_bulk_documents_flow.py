"""Many documents into one employee's vault at once, as files or ZIPs of files.

Each file gets the single upload's checks (type from the bytes, size, HR only,
never HR's own record); a ZIP is opened and its files stored one by one; what
cannot be stored comes back with the reason.
"""

from __future__ import annotations

import io
import os
import uuid
import zipfile
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.employee import Employee, EmployeeDocument
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from tests.integration.conftest import requires_db

pytestmark = requires_db

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32

API = "/api/v1/documents/bulk"


def _real(kind: str) -> bytes:
    """A real file of the kind: what is inside a ZIP is opened and checked in full."""
    from PIL import Image
    from pypdf import PdfWriter

    out = io.BytesIO()
    if kind == "pdf":
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(out)
    else:
        Image.new("RGB", (40, 30), (120, 40, 40)).save(out, "PNG" if kind == "png" else "JPEG")
    return out.getvalue()


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


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


async def _upload(client, user, employee_id, files, *, category="other", visible=True):
    return await client.post(
        API,
        headers=_auth(user),
        data={
            "employee_id": str(employee_id),
            "category": category,
            "is_employee_visible": "true" if visible else "false",
        },
        files=[("files", item) for item in files],
    )


async def test_files_and_a_zip_land_in_the_vault(
    client, session, tenant, hr_user, employee, _storage_root
) -> None:
    archive = _zip(
        {
            "Aadhaar_card.png": _real("png"),
            "certificates/Degree certificate.pdf": _real("pdf"),
            "photo.jpg": _real("jpeg"),
            "notes.txt": b"just text",
            "invoice.pdf": b"MZ\x90\x00 a program renamed .pdf",
            "__MACOSX/._photo.jpg": b"resource fork",
            "certificates/": b"",
        }
    )
    response = await _upload(
        client,
        hr_user,
        employee.id,
        [
            ("PAN card.pdf", PDF, "application/pdf"),
            ("onboarding.zip", archive, "application/zip"),
        ],
        category="id_proof",
        visible=False,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    titles = sorted(row["title"] for row in body["stored"])
    assert titles == ["Aadhaar card", "Degree certificate", "PAN card", "photo"]
    assert all(row["category"] == "id_proof" for row in body["stored"])
    assert all(row["is_employee_visible"] is False for row in body["stored"])
    skipped = {row["file"]: row["reason"] for row in body["skipped"]}
    assert set(skipped) == {"onboarding.zip / notes.txt", "onboarding.zip / invoice.pdf"}
    # Every file inside a ZIP gets the same checks as a ZIP kept whole.
    assert skipped["onboarding.zip / invoice.pdf"] == "It is a program, not a document."

    rows = (
        await session.scalars(
            select(EmployeeDocument).where(EmployeeDocument.employee_id == employee.id)
        )
    ).all()
    assert len(rows) == 4
    for row in rows:
        assert (_storage_root / row.file_url).exists()
    audits = (
        await session.scalars(select(AuditLog).where(AuditLog.entity_type == "employee_document"))
    ).all()
    assert len([row for row in audits if row.changes.get("bulk")]) == 4


async def test_oversized_and_unreadable_files_are_reported_not_stored(
    client, session, tenant, hr_user, employee
) -> None:
    # The workspace's upload limit holds for every file, inside a ZIP or not.
    tenant.settings = {**(tenant.settings or {}), "max_upload_mb": 1}
    await session.commit()
    big = PDF + b"\x00" * (1024 * 1024)
    archive = _zip({"big.pdf": big, "small.pdf": _real("pdf")})
    response = await _upload(
        client,
        hr_user,
        employee.id,
        [
            ("bundle.zip", archive, "application/zip"),
            ("broken.zip", b"PK\x03\x04 not really a zip", "application/zip"),
            ("huge.pdf", big, "application/pdf"),
        ],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["title"] for row in body["stored"]] == ["small"]
    reasons = {row["file"]: row["reason"] for row in body["skipped"]}
    assert "limit" in reasons["bundle.zip / big.pdf"]
    assert "ZIP" in reasons["broken.zip"]
    assert "limit" in reasons["huge.pdf"]


async def test_nothing_is_kept_when_the_upload_fails_part_way(
    client, session, hr_user, employee, _storage_root, monkeypatch
) -> None:
    from app.services.storage import LocalFileStorage, StorageError

    original = LocalFileStorage.put
    calls = {"n": 0}

    async def failing(self, key, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise StorageError("The storage service could not be reached.")
        return await original(self, key, data)

    monkeypatch.setattr(LocalFileStorage, "put", failing)
    # Read now: the failed upload rolls the shared session back, which expires
    # `employee`, and reading an expired attribute afterwards is a lazy load
    # outside the event loop (MissingGreenlet).
    employee_id = employee.id
    response = await _upload(
        client,
        hr_user,
        employee_id,
        [("one.pdf", PDF, "application/pdf"), ("two.pdf", PDF, "application/pdf")],
    )
    assert response.status_code == 503
    rows = (
        await session.scalars(
            select(EmployeeDocument).where(EmployeeDocument.employee_id == employee_id)
        )
    ).all()
    assert rows == []
    stored = [path for path in _storage_root.rglob("*") if path.is_file()]
    assert stored == []


async def test_only_hr_and_never_onto_their_own_record(
    client, session, tenant: Tenant, hr_user, employee: Employee
) -> None:
    worker = User(
        tenant_id=tenant.id,
        email=f"worker-{uuid.uuid4().hex[:6]}@acme.test",
        password_hash="x",
        full_name="Worker",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(worker)
    hr_user.employee_id = employee.id
    await session.commit()

    refused = await _upload(client, worker, employee.id, [("a.pdf", PDF, "application/pdf")])
    assert refused.status_code == 403
    own = await _upload(client, hr_user, employee.id, [("a.pdf", PDF, "application/pdf")])
    assert own.status_code == 403


async def test_too_many_files_are_refused(client, hr_user, employee) -> None:
    archive = _zip({f"doc-{n}.pdf": PDF for n in range(101)})
    response = await _upload(
        client, hr_user, employee.id, [("lots.zip", archive, "application/zip")]
    )
    assert response.status_code == 422
    assert "at most 100" in response.json()["detail"]


# ── One ZIP kept whole (2026-11-12) ──────────────────────────────────────────


async def _upload_one(client, user, employee_id, name, data, *, category="all_documents"):
    return await client.post(
        "/api/v1/documents",
        headers=_auth(user),
        data={
            "employee_id": str(employee_id),
            "category": category,
            "title": "All documents",
        },
        files={"file": (name, data, "application/zip")},
    )


async def test_hr_attaches_one_zip_of_documents_kept_whole(
    client, session, hr_user, employee, _storage_root
) -> None:
    archive = _zip(
        {
            "PAN.pdf": _real("pdf"),
            "Aadhaar.png": _real("png"),
            "certificates/Degree.jpg": _real("jpeg"),
            "__MACOSX/._PAN.pdf": b"resource fork",
        }
    )
    response = await _upload_one(client, hr_user, employee.id, "papers.zip", archive)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mime_type"] == "application/zip"
    assert body["category"] == "all_documents"

    row = await session.get(EmployeeDocument, uuid.UUID(body["id"]))
    stored = (_storage_root / row.file_url).read_bytes()
    with zipfile.ZipFile(io.BytesIO(stored)) as kept:
        # Rebuilt from the checked files: the Mac clutter did not come along.
        assert sorted(kept.namelist()) == ["Aadhaar.png", "PAN.pdf", "certificates/Degree.jpg"]
    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "employee_document", AuditLog.entity_id == row.id
        )
    )
    assert sorted(audit.changes["zip_files"]) == [
        "Aadhaar.png",
        "PAN.pdf",
        "certificates/Degree.jpg",
    ]

    download = await client.get(f"/api/v1/documents/{row.id}/download", headers=_auth(hr_user))
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert 'filename="All documents.zip"' in download.headers["content-disposition"]


async def test_a_zip_with_one_bad_file_stores_nothing(
    client, session, hr_user, employee, _storage_root
) -> None:
    employee_id = employee.id
    archive = _zip({"PAN.pdf": _real("pdf"), "update.pdf": b"MZ\x90\x00 program"})
    response = await _upload_one(client, hr_user, employee_id, "papers.zip", archive)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("The ZIP was not stored.")
    assert "update.pdf is a program" in detail
    rows = (
        await session.scalars(
            select(EmployeeDocument).where(EmployeeDocument.employee_id == employee_id)
        )
    ).all()
    assert rows == []
    assert [path for path in _storage_root.rglob("*") if path.is_file()] == []


async def test_a_zip_is_at_most_5_mb_or_the_workspace_limit_when_lower(
    client, session, tenant, hr_user, employee
) -> None:
    # Stored, not compressed: 6 MB of noise stays 6 MB.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("scan.pdf", os.urandom(6 * 1024 * 1024))
    response = await _upload_one(client, hr_user, employee.id, "big.zip", buffer.getvalue())
    assert response.status_code == 413
    assert "at most 5 MB" in response.json()["detail"]

    tenant.settings = {**(tenant.settings or {}), "max_upload_mb": 2}
    await session.commit()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("scan.pdf", os.urandom(3 * 1024 * 1024))
    response = await _upload_one(client, hr_user, employee.id, "big.zip", buffer.getvalue())
    assert response.status_code == 413


async def test_an_employee_cannot_upload_a_zip_to_their_own_record(
    client, session, tenant: Tenant, employee: Employee
) -> None:
    worker = User(
        tenant_id=tenant.id,
        email=f"zip-{uuid.uuid4().hex[:6]}@acme.test",
        password_hash="x",
        full_name="Worker",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(worker)
    await session.commit()
    archive = _zip({"PAN.pdf": _real("pdf")})
    response = await client.post(
        "/api/v1/documents/mine",
        headers=_auth(worker),
        data={"category": "id_proof", "title": "Papers"},
        files={"file": ("papers.zip", archive, "application/zip")},
    )
    assert response.status_code == 415
    # And the ZIP category is HR's: not among what an employee may upload.
    allowed = await client.get("/api/v1/documents/categories/self-upload", headers=_auth(worker))
    assert "all_documents" not in [row["value"] for row in allowed.json()]
