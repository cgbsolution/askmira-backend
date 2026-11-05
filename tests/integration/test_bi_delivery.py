"""BI delivery to the client's own bucket, and the feed link, against a real database.

The bucket is a fake S3 endpoint (httpx MockTransport) that checks each request
carries a Signature Version 4 header for the saved key; the signing itself is
checked against AWS's published examples. Everything on AskMira's side is real.

* A delivery writes a dated CSV and latest.csv, as the admin who set it up,
  records an export run with the field list and an audit row, and keeps no
  secret anywhere it can be read back.
* A bucket that refuses is recorded as a failed run with the bucket's reason.
* The nightly job delivers the nightly connections and not the others.
* A feed link returns the CSV without sign-in, is audited, dies when replaced,
  and stops when its admin is no longer one.
* A database role is no longer offered; another workspace reaches nothing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.bi import BiConnection, BiDestinationKind, BiExportRun, BiExportStatus
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.bi import delivery
from app.services.bi import service as bi
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import make_person

pytestmark = requires_db

KEY_ID = "AKIAASKMIRATEST01"
SECRET = "s3cr3t/askmira+test/key0123456789"


def test_signing_matches_aws_published_examples() -> None:
    """AWS's own Signature Version 4 examples for S3 (PUT Object and GET Object)."""
    now = datetime(2013, 5, 24, tzinfo=UTC)
    put = delivery.sign_v4(
        method="PUT",
        url="https://examplebucket.s3.amazonaws.com/test%24file.text",
        region="us-east-1",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        body=b"Welcome to Amazon S3.",
        headers={"date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
        now=now,
    )
    assert put["Authorization"].endswith(
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )
    get = delivery.sign_v4(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/test.txt",
        region="us-east-1",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        body=b"",
        headers={"range": "bytes=0-9"},
        now=now,
    )
    assert get["Authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


class FakeBucket:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.refuse: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        assert auth.startswith(f"AWS4-HMAC-SHA256 Credential={KEY_ID}/"), auth
        assert request.method == "PUT"
        if self.refuse:
            return httpx.Response(
                403, text=f"<Error><Code>{self.refuse}</Code><Message>no</Message></Error>"
            )
        self.files[f"{request.url.host}{request.url.path}"] = request.content
        return httpx.Response(200)


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBucket]:
    fake = FakeBucket()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        delivery, "_client", lambda: real(transport=httpx.MockTransport(fake.handler), timeout=5)
    )
    yield fake


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


async def _org(session: AsyncSession, tenant: Tenant) -> User:
    modules = dict(tenant.enabled_modules or {})
    modules["analytics"] = True
    tenant.enabled_modules = modules
    await session.commit()
    admin = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    for name in ("Ravi Kumar", "Meera Nair"):
        person = await make_person(session, tenant, role=UserRole.EMPLOYEE, name=name)
        employee = await session.get(Employee, person.employee_id)
        employee.display_name = name
    await session.commit()
    return admin


async def _bucket_connection(
    session: AsyncSession, admin: User, *, schedule: str = "manual", endpoint: str = ""
) -> str:
    made = await bi.create_connection(
        session,
        admin,
        name=f"Warehouse {uuid.uuid4().hex[:4]}",
        entity_name="dim_employee",
        destination=BiDestinationKind.OBJECT_STORAGE,
        destination_ref="",
        export_role="admin",
        storage={
            "bucket": "acme-hr-data",
            "region": "ap-south-1",
            "prefix": "askmira/",
            "access_key_id": KEY_ID,
            "endpoint": endpoint,
        },
        secret=SECRET,
        schedule=schedule,
    )
    return made["connection_id"]


async def test_a_delivery_writes_dated_and_latest_files_and_is_recorded(
    session, tenant, bucket
) -> None:
    admin = await _org(session, tenant)
    connection_id = await _bucket_connection(session, admin)

    result = await delivery.deliver(session, admin, connection_id=uuid.UUID(connection_id))
    assert result["delivered"] is True, result
    assert result["row_count"] >= 3
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    host = "acme-hr-data.s3.ap-south-1.amazonaws.com"
    dated = bucket.files[f"{host}/askmira/dim_employee/{day}.csv"]
    assert bucket.files[f"{host}/askmira/dim_employee/latest.csv"] == dated
    text = dated.decode("utf-8")
    assert "Ravi" in text and "Meera" in text

    run = await session.scalar(select(BiExportRun).where(BiExportRun.status == BiExportStatus.SUCCEEDED))
    assert run.field_names and run.row_count == result["row_count"]
    assert await session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "bi_export_run", AuditLog.entity_id == run.id)
    )
    listed = await bi.list_connections(session, admin)
    assert SECRET not in json.dumps(listed)
    row = await session.get(BiConnection, uuid.UUID(connection_id))
    assert row.last_delivered_at and row.last_delivery_error is None
    assert SECRET not in (row.secret_encrypted or "")


async def test_another_s3_store_is_addressed_path_style(session, tenant, bucket) -> None:
    admin = await _org(session, tenant)
    connection_id = await _bucket_connection(
        session, admin, endpoint="https://acct.r2.cloudflarestorage.com"
    )
    result = await delivery.deliver(session, admin, connection_id=uuid.UUID(connection_id))
    assert result["delivered"]
    assert any(k.startswith("acct.r2.cloudflarestorage.com/acme-hr-data/askmira/") for k in bucket.files)


async def test_a_refusing_bucket_is_recorded_with_its_reason(session, tenant, bucket) -> None:
    admin = await _org(session, tenant)
    connection_id = await _bucket_connection(session, admin)
    bucket.refuse = "AccessDenied"
    result = await delivery.deliver(session, admin, connection_id=uuid.UUID(connection_id))
    assert result["delivered"] is False
    assert result["error"] == "The key may not write to this bucket."
    run = await session.scalar(select(BiExportRun))
    assert run.status is BiExportStatus.FAILED and run.failure_reason == result["error"]
    row = await session.get(BiConnection, uuid.UUID(connection_id))
    assert row.last_delivery_error == result["error"] and row.last_delivered_at is None


async def test_the_nightly_job_delivers_only_nightly_connections(session, tenant, bucket) -> None:
    admin = await _org(session, tenant)
    nightly = await _bucket_connection(session, admin, schedule="daily")
    await _bucket_connection(session, admin, schedule="manual")
    outcome = await delivery.scheduled(session, admin, now=datetime.now(UTC))
    assert outcome == {"delivered": 1, "failed": 0}
    runs = (await session.scalars(select(BiExportRun))).all()
    assert {str(r.connection_id) for r in runs} == {nightly}

    # An admin who is no longer one delivers nothing.
    admin.role = UserRole.EMPLOYEE
    await session.commit()
    outcome = await delivery.scheduled(session, admin, now=datetime.now(UTC))
    assert outcome == {"delivered": 0, "failed": 1}
    row = await session.get(BiConnection, uuid.UUID(nightly))
    assert "no longer an admin" in row.last_delivery_error


async def test_the_feed_link_is_read_without_sign_in_audited_and_replaceable(
    client, session, tenant
) -> None:
    admin = await _org(session, tenant)
    made = await bi.create_connection(
        session,
        admin,
        name="Power BI",
        entity_name="dim_employee",
        destination=BiDestinationKind.HTTPS_FEED,
        destination_ref="",
        export_role="admin",
    )
    token = made["feed_token"]
    assert token.startswith("bif_")
    row = await session.get(BiConnection, uuid.UUID(made["connection_id"]))
    assert row.feed_token_hash and token not in row.feed_token_hash

    read = await client.get(f"/api/v1/public/bi-feed/{token}.csv")
    assert read.status_code == 200, read.text
    assert read.headers["content-type"].startswith("text/csv")
    assert "Ravi" in read.text
    assert await session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "bi_export_run")
    )
    assert (await client.get("/api/v1/public/bi-feed/bif_wrong")).status_code == 404

    fresh = await client.post(
        f"/api/v1/bi/connections/{made['connection_id']}/feed-link", headers=_auth(admin)
    )
    assert fresh.status_code == 200, fresh.text
    assert (await client.get(f"/api/v1/public/bi-feed/{token}")).status_code == 404
    new_token = fresh.json()["feed_token"]
    assert (await client.get(f"/api/v1/public/bi-feed/{new_token}")).status_code == 200

    admin.role = UserRole.EMPLOYEE
    await session.commit()
    assert (await client.get(f"/api/v1/public/bi-feed/{new_token}")).status_code == 410


async def test_refusals(session, tenant, bucket) -> None:
    admin = await _org(session, tenant)
    with pytest.raises(HTTPException) as role:
        await bi.create_connection(
            session, admin, name="Role", entity_name="dim_employee",
            destination=BiDestinationKind.POSTGRES_ROLE, destination_ref="reader",
            export_role="admin",
        )  # fmt: skip
    assert "not offered" in role.value.detail
    with pytest.raises(HTTPException) as bad_bucket:
        await bi.create_connection(
            session, admin, name="Bad", entity_name="dim_employee",
            destination=BiDestinationKind.OBJECT_STORAGE, destination_ref="",
            export_role="admin", secret=SECRET,
            storage={"bucket": "Bad_Bucket!", "region": "ap-south-1", "access_key_id": KEY_ID},
        )  # fmt: skip
    assert "bucket name" in bad_bucket.value.detail
    with pytest.raises(HTTPException) as no_secret:
        await bi.create_connection(
            session, admin, name="NoKey", entity_name="dim_employee",
            destination=BiDestinationKind.OBJECT_STORAGE, destination_ref="",
            export_role="admin",
            storage={"bucket": "acme-hr-data", "region": "ap-south-1", "access_key_id": KEY_ID},
        )  # fmt: skip
    assert "secret access key" in no_secret.value.detail

    connection_id = await _bucket_connection(session, admin)
    other = Tenant(name="Other", slug=f"o-{uuid.uuid4().hex[:6]}", enabled_modules={"analytics": True}, settings={})
    session.add(other)
    await session.commit()
    stranger = await make_person(session, other, role=UserRole.ADMIN, name="Olu Other")
    with pytest.raises(HTTPException) as gone:
        await delivery.deliver(session, stranger, connection_id=uuid.UUID(connection_id))
    assert gone.value.status_code == 404
    assert bucket.files == {}
