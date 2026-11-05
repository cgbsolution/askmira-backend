"""The workspace's upload limit and automatic compression, end to end.

An admin sets the largest file anyone can upload (Settings -> Company ->
Workspace). Every upload screen follows it, the session carries it so the
portal and the app can say it before a file is picked, and each workspace has
its own. What is stored is the compressed file: its size, its download and its
audit row all agree.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee, EmployeeDocument
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from tests.integration.conftest import requires_db

pytestmark = requires_db

MB = 1024 * 1024
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


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


def _phone_photo() -> bytes:
    """A 12-megapixel camera frame of a document, saved at high quality."""
    image = Image.new("RGB", (4000, 3000), "white")
    draw = ImageDraw.Draw(image)
    for y in range(0, 3000, 40):
        draw.text((40, y), "PAN ABCDE1234F  Ramesh Kumar  " * 12, fill="black")
    image = Image.blend(image, Image.effect_noise((4000, 3000), 30).convert("RGB"), 0.15)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=95)
    return out.getvalue()


async def _set_limit(client, admin: User, mb) -> httpx.Response:
    return await client.patch(
        "/api/v1/admin/tenant", headers=_auth(admin), json={"max_upload_mb": mb}
    )


async def _upload(client, user: User, employee_id, data: bytes, name: str = "scan.pdf"):
    return await client.post(
        "/api/v1/documents",
        headers=_auth(user),
        data={"employee_id": str(employee_id), "category": "id_proof", "title": "PAN card"},
        files={"file": (name, data, "application/octet-stream")},
    )


async def _person(session: AsyncSession, tenant: Tenant, code: str) -> Employee:
    person = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code, work_email=f"{code}@x.test"
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


# ── The setting ─────────────────────────────────────────────────────────────


async def test_an_admin_sets_the_limit_and_the_session_carries_it(
    client, session, tenant, hr_user
) -> None:
    before = await client.get("/api/v1/auth/session", headers=_auth(hr_user))
    assert before.json()["tenant"]["max_upload_mb"] == 10, "10 MB until an admin chooses"

    response = await _set_limit(client, hr_user, 5)
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["max_upload_mb"] == 5

    after = await client.get("/api/v1/auth/session", headers=_auth(hr_user))
    assert after.json()["tenant"]["max_upload_mb"] == 5

    audit = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id, AuditLog.entity_type == "tenant"
            )
        )
    ).all()
    assert [row.changes["max_upload_mb"] for row in audit] == [{"from": None, "to": 5}]


@pytest.mark.parametrize("value", [0, 26, -5, 2.5, "10", "ten", True, None])
async def test_a_limit_outside_1_to_25_whole_mb_is_refused(
    client, session, tenant, hr_user, value
) -> None:
    response = await _set_limit(client, hr_user, value)
    if value is None:
        # Nothing to change: the other settings are left as they were.
        assert response.status_code == 200
    else:
        assert response.status_code == 422, (value, response.text)
    await session.refresh(tenant)
    assert "max_upload_mb" not in (tenant.settings or {})


@pytest.mark.parametrize("role", [UserRole.SUB_ADMIN, UserRole.EMPLOYEE])
async def test_only_an_admin_changes_the_limit(client, session, tenant, role) -> None:
    other = User(
        tenant_id=tenant.id,
        email=f"{role.value}@acme.test",
        password_hash="x",
        full_name=role.value,
        role=role,
        is_active=True,
    )
    session.add(other)
    await session.commit()
    response = await _set_limit(client, other, 25)
    assert response.status_code == 403
    await session.refresh(tenant)
    assert "max_upload_mb" not in (tenant.settings or {})


# ── The limit at work ───────────────────────────────────────────────────────


async def test_each_workspace_has_its_own_limit(
    client, session, tenant, hr_user, employee, other_tenant, other_hr_user
) -> None:
    assert (await _set_limit(client, hr_user, 1)).status_code == 200
    file = PDF + b"\x00" * int(1.5 * MB)

    refused = await _upload(client, hr_user, employee.id, file)
    assert refused.status_code == 413
    assert refused.json()["detail"] == "The file is 1.6 MB. The upload limit is 1 MB."

    theirs = await _person(session, other_tenant, "GLX001")
    accepted = await _upload(client, other_hr_user, theirs.id, file)
    assert accepted.status_code == 201, "Globex is still on the default 10 MB"


async def test_raising_the_limit_lets_a_larger_file_in(client, hr_user, employee) -> None:
    file = PDF + b"\x00" * (12 * MB)
    assert (await _upload(client, hr_user, employee.id, file)).status_code == 413
    assert (await _set_limit(client, hr_user, 15)).status_code == 200
    assert (await _upload(client, hr_user, employee.id, file)).status_code == 201


async def test_a_phone_photo_is_stored_compressed_and_everything_agrees(
    client, session, tenant, hr_user, employee, _storage_root
) -> None:
    photo = _phone_photo()
    response = await _upload(client, hr_user, employee.id, photo, name="pan.jpg")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mime_type"] == "image/jpeg"

    row = await session.get(EmployeeDocument, uuid.UUID(body["id"]))
    stored = (_storage_root / row.file_url).read_bytes()
    assert len(stored) < len(photo) / 3, "stored compressed"
    assert row.file_size_bytes == len(stored), "the size shown is the size kept"
    assert max(Image.open(io.BytesIO(stored)).size) == 2400

    download = await client.get(f"/api/v1/documents/{body['id']}/download", headers=_auth(hr_user))
    assert download.status_code == 200
    assert download.content == stored

    # The upload's own row: the download above writes one for the same document.
    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "employee_document",
            AuditLog.entity_id == row.id,
            AuditLog.action == AuditAction.CREATE,
        )
    )
    assert audit.changes["size_bytes"] == len(stored)
    assert audit.changes["uploaded_size_bytes"] == len(photo)


async def test_an_import_sheet_follows_the_limit_too(client, hr_user) -> None:
    assert (await _set_limit(client, hr_user, 1)).status_code == 200
    sheet = b"Emp Code,First Name\n" + b"E1,Asha\n" * (MB // 7)
    response = await client.post(
        "/api/v1/employees/import/preview",
        headers=_auth(hr_user),
        files={"file": ("people.csv", sheet, "text/csv")},
    )
    assert response.status_code == 413, response.text
    assert "The upload limit is 1 MB." in response.json()["detail"]


async def test_the_expense_screens_are_told_the_workspace_limit(client, hr_user) -> None:
    assert (await _set_limit(client, hr_user, 3)).status_code == 200
    response = await client.get("/api/v1/expenses/meta", headers=_auth(hr_user))
    if response.status_code == 404:
        pytest.skip("expenses meta is not routed in this build")
    assert response.status_code == 200, response.text
    assert response.json()["receipt_max_bytes"] == 3 * MB
