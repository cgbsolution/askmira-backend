"""Many documents into one employee's vault at once, as files or ZIPs of files.

Each file gets the single upload's checks (type from the bytes, size, HR only,
never HR's own record); a ZIP is opened and its files stored one by one; what
cannot be stored comes back with the reason.
"""

from __future__ import annotations

import io
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
            "Aadhaar_card.png": PNG,
            "certificates/Degree certificate.pdf": PDF,
            "photo.jpg": JPEG,
            "notes.txt": b"just text",
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
    assert [row["file"] for row in body["skipped"]] == ["onboarding.zip / notes.txt"]

    rows = (
        await session.scalars(
            select(EmployeeDocument).where(EmployeeDocument.employee_id == employee.id)
        )
    ).all()
    assert len(rows) == 4
    for row in rows:
        assert (_storage_root / row.file_url).exists()
    audits = (
        await session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "employee_document")
        )
    ).all()
    assert len([row for row in audits if row.changes.get("bulk")]) == 4


async def test_oversized_and_unreadable_files_are_reported_not_stored(
    client, session, hr_user, employee, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1000)
    archive = _zip({"big.pdf": PDF + b"\x00" * 5000, "small.pdf": PDF})
    response = await _upload(
        client,
        hr_user,
        employee.id,
        [
            ("bundle.zip", archive, "application/zip"),
            ("broken.zip", b"PK\x03\x04 not really a zip", "application/zip"),
            ("huge.pdf", PDF + b"\x00" * 5000, "application/pdf"),
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
    response = await _upload(
        client,
        hr_user,
        employee.id,
        [("one.pdf", PDF, "application/pdf"), ("two.pdf", PDF, "application/pdf")],
    )
    assert response.status_code == 503
    rows = (
        await session.scalars(
            select(EmployeeDocument).where(EmployeeDocument.employee_id == employee.id)
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
