"""BI delivery: a connection's rows to the client's own bucket, or as a feed link.

Two ways out, because clients genuinely want different ones:

* **Their bucket.** Any S3-compatible store — Amazon S3, Cloudflare R2, Wasabi,
  MinIO, DigitalOcean Spaces, Google Cloud Storage's interoperability mode. Each
  delivery writes `<prefix>/<entity>/<YYYY-MM-DD>.csv` and replaces
  `<prefix>/<entity>/latest.csv`, on demand or every night at 02:00 India time.
  Requests are signed here (AWS Signature Version 4); the secret key is
  encrypted with the tenant key and never returned.
* **A feed link.** A private address a BI tool refreshes from (Power BI's Web
  connector, Excel's From Web, Looker Studio, Tableau). Its token is shown once;
  only its hash is kept; replacing it kills the old link.

Every delivery, either way, is an export run with its field list and an audit
row, exactly as a download is (service.run_export): the rows are compiled by the
report builder as the admin who set the connection up, so row scope and field
sensitivity are theirs, and an admin who has since lost the role delivers
nothing. A delivery that fails is recorded as failed, with the reason, and
changes nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.bi import (
    BiConnection,
    BiDestinationKind,
    BiExportKind,
    BiExportRun,
    BiExportStatus,
)
from app.models.user import User, UserRole
from app.services import audit
from app.services.bi import export, exposed_schema

__all__ = [
    "StorageTarget",
    "deliver",
    "feed_csv",
    "new_feed_token",
    "scheduled",
    "sign_v4",
    "storage_target",
]

TIMEOUT = 60
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z0-9-]{2,32}$")


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


# ═══════════════════════════════════════════════════════════════════════════
# Where: an S3-compatible bucket
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class StorageTarget:
    bucket: str
    region: str
    prefix: str
    access_key_id: str
    #: Empty for Amazon S3; the service's address for any other S3-compatible store.
    endpoint: str = ""

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}".rstrip("/")


def storage_target(
    *, bucket: str, region: str, prefix: str, access_key_id: str, endpoint: str
) -> StorageTarget:
    """Checked before anything is saved, so a typo fails here rather than at 02:00."""
    bucket, region = bucket.strip().lower(), (region.strip().lower() or "us-east-1")
    if not _BUCKET.match(bucket):
        raise _bad("The bucket name is 3 to 63 lowercase letters, digits, dots or hyphens.")
    if not _REGION.match(region):
        raise _bad("The region looks like ap-south-1 (or auto, for Cloudflare R2).")
    endpoint = endpoint.strip().rstrip("/")
    if endpoint:
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            raise _bad("The storage address must be an https:// URL.")
    key = access_key_id.strip()
    if not 8 <= len(key) <= 128 or not re.match(r"^[A-Za-z0-9/+=_\-.]+$", key):
        raise _bad("The access key id is the key's id, not its secret.")
    prefix = "/".join(part for part in prefix.strip().strip("/").split("/") if part)
    if any(part in (".", "..") for part in prefix.split("/")):
        raise _bad("The folder cannot contain . or .. parts.")
    return StorageTarget(bucket, region, prefix[:200], key, endpoint)


def _target_of(connection: BiConnection) -> StorageTarget:
    raw = connection.storage or {}
    return StorageTarget(
        bucket=str(raw.get("bucket") or ""),
        region=str(raw.get("region") or "us-east-1"),
        prefix=str(raw.get("prefix") or ""),
        access_key_id=str(raw.get("access_key_id") or ""),
        endpoint=str(raw.get("endpoint") or ""),
    )


def _url_for(target: StorageTarget, key: str) -> str:
    path = urllib.parse.quote(key, safe="/-_.~")
    if target.endpoint:
        # Any other S3-compatible store: path-style, which every one of them takes.
        return f"{target.endpoint}/{target.bucket}/{path}"
    return f"https://{target.bucket}.s3.{target.region}.amazonaws.com/{path}"


def sign_v4(
    *,
    method: str,
    url: str,
    region: str,
    access_key_id: str,
    secret_key: str,
    body: bytes,
    headers: dict[str, str],
    now: datetime,
    service: str = "s3",
) -> dict[str, str]:
    """The headers that sign one request with AWS Signature Version 4."""
    parsed = urllib.parse.urlparse(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    signed = {
        **{k.lower(): v.strip() for k, v in headers.items()},
        "host": parsed.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    names = sorted(signed)
    query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    )
    canonical = "\n".join(
        [
            method,
            parsed.path or "/",
            query,
            "".join(f"{name}:{signed[name]}\n" for name in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )

    def _hmac(key: bytes, text: str) -> bytes:
        return hmac.new(key, text.encode(), hashlib.sha256).digest()

    key = _hmac(f"AWS4{secret_key}".encode(), day)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        **{k: v for k, v in signed.items() if k != "host"},
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}"
        ),
    }


def _client():
    import httpx

    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)


async def _put(target: StorageTarget, secret: str, key: str, body: bytes, now: datetime) -> None:
    import httpx

    url = _url_for(target, key)
    headers = sign_v4(
        method="PUT",
        url=url,
        region=target.region,
        access_key_id=target.access_key_id,
        secret_key=secret,
        body=body,
        headers={"content-type": "text/csv; charset=utf-8"},
        now=now,
    )
    try:
        async with _client() as client:
            response = await client.put(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"The bucket could not be reached: {exc}") from exc
    if response.status_code >= 300:
        code = re.search(r"<Code>([^<]+)</Code>", response.text or "")
        words = {
            "AccessDenied": "The key may not write to this bucket.",
            "InvalidAccessKeyId": "The bucket does not know this access key id.",
            "SignatureDoesNotMatch": "The secret key does not match the access key id.",
            "NoSuchBucket": "There is no bucket by this name in this region.",
            "PermanentRedirect": "The bucket is in another region.",
            "AuthorizationHeaderMalformed": "The bucket is in another region.",
        }
        name = code.group(1) if code else str(response.status_code)
        raise RuntimeError(words.get(name, f"The bucket refused the file ({name})."))


# ═══════════════════════════════════════════════════════════════════════════
# What: the rows, compiled as the admin who set the connection up
# ═══════════════════════════════════════════════════════════════════════════


async def _rows(
    session: AsyncSession,
    user: User,
    connection: BiConnection,
    *,
    request: Request | None,
) -> tuple[export.ExportPlan, list[dict[str, Any]]]:
    from app.services.reporting import service as reporting
    from app.services.reporting.compiler import MAX_PAGE_SIZE, Definition, Selection

    entity = exposed_schema.exposed_entity(connection.entity_name)
    plan = export.plan_export(
        entity_name=connection.entity_name,
        role=connection.export_role,
        kind=export.ExportKind.FULL,
        destination=export.Destination(connection.destination.value),
        destination_ref=connection.destination_ref,
        requested_fields=connection.field_names,
    )
    by_source = {f.source: f.name for f in plan.fields}
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = await reporting.run_definition(
            session,
            user,
            Definition(
                entity_key=entity.entity_key,
                selections=tuple(Selection(field_key=f.source, label=f.name) for f in plan.fields),
                limit=MAX_PAGE_SIZE,
                offset=offset,
            ),
            request=request,
        )
        keys = [column["key"] for column in result["columns"]]
        for values in result["rows"]:
            rows.append({by_source.get(k, k): v for k, v in zip(keys, values, strict=False)})
        offset += len(result["rows"])
        if not result["rows"] or offset >= result["total_rows"]:
            break
    return plan, rows


def _csv(plan: export.ExportPlan, rows: list[dict[str, Any]]) -> tuple[str, int]:
    """The rows as CSV, projected onto the planned fields only, and how many."""
    from app.services.reporting import service as reporting

    projected = export.extract_rows(plan, source_rows=rows)
    content = reporting.to_csv(
        [{"label": name} for name in plan.field_names],
        [[row.get(name) for name in plan.field_names] for row in projected.rows],
    )
    return content, projected.row_count


async def _record(
    session: AsyncSession,
    user: User,
    connection: BiConnection,
    plan: export.ExportPlan,
    *,
    row_count: int,
    where: str,
    error: str | None,
    now: datetime,
    via: str,
    request: Request | None,
) -> BiExportRun:
    run = BiExportRun(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        entity_name=plan.entity_name,
        kind=BiExportKind.FULL,
        destination=connection.destination,
        destination_ref=where[:500],
        export_role=plan.role,
        schema_version=plan.schema_version,
        since=None,
        next_watermark=None,
        row_count=0 if error else row_count,
        field_names=list(plan.field_names),
        field_count=len(plan.fields),
        withheld_fields=list(plan.withheld_fields),
        carries_payroll_data=plan.carries_payroll_data,
        status=BiExportStatus.FAILED if error else BiExportStatus.SUCCEEDED,
        failure_reason=error,
        initiated_by_user_id=user.id,
        started_at=now,
    )
    session.add(run)
    connection.last_delivery_error = error
    if not error:
        connection.last_delivered_at = now
    await session.flush()
    await audit.record(
        session,
        tenant_id=connection.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="bi_export_run",
        entity_id=run.id,
        actor=user,
        summary=(
            f"{plan.entity_name} {'not delivered' if error else 'delivered'} to {where} "
            f"({via}): {0 if error else row_count} row(s), {len(plan.fields)} field(s)"
            + (f". {error}" if error else "")
        ),
        changes={
            "fields": list(plan.field_names),
            "carries_payroll_data": plan.carries_payroll_data,
            "via": via,
            "error": error,
        },
        request=request,
    )
    return run


async def _creator(session: AsyncSession, connection: BiConnection) -> User | None:
    """Who the rows are compiled as: the admin who set it up, while they still are one."""
    person = await session.get(User, connection.created_by_user_id)
    if (
        person is None
        or person.tenant_id != connection.tenant_id
        or not person.is_active
        or person.role not in (UserRole.ADMIN, UserRole.SUPER_ADMIN)
    ):
        return None
    person.has_reports = False  # type: ignore[attr-defined]
    return person


# ═══════════════════════════════════════════════════════════════════════════
# Delivering
# ═══════════════════════════════════════════════════════════════════════════


async def deliver(
    session: AsyncSession,
    user: User,
    *,
    connection_id: uuid.UUID,
    via: str = "on demand",
    now: datetime | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Write the connection's rows to its bucket now. Recorded whether it worked or not."""
    moment = now or datetime.now(UTC)
    connection = await session.scalar(
        tenant_scoped(
            select(BiConnection).where(BiConnection.id == connection_id), BiConnection, user
        )
    )
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such BI connection.")
    if connection.destination is not BiDestinationKind.OBJECT_STORAGE:
        raise _bad("Only a bucket connection is delivered; a feed link is read by your BI tool.")
    if not connection.is_active:
        raise _bad("This connection is switched off.")
    target = _target_of(connection)
    if not target.bucket or not connection.secret_encrypted:
        raise _bad("This connection has no bucket and key saved.")
    try:
        plan, rows = await _rows(session, user, connection, request=request)
    except (export.ExportError, exposed_schema.SchemaError) as exc:
        raise _bad(str(exc)) from exc
    content, count = _csv(plan, rows)
    body = content.encode("utf-8")
    base = "/".join(p for p in (target.prefix, plan.entity_name) if p)
    dated = f"{base}/{moment.astimezone(UTC):%Y-%m-%d}.csv"
    error = None
    try:
        secret = decrypt_secret(connection.secret_encrypted, connection.tenant_id) or ""
        await _put(target, secret, dated, body, moment)
        await _put(target, secret, f"{base}/latest.csv", body, moment)
    except RuntimeError as exc:
        error = str(exc)[:1000]
    run = await _record(
        session,
        user,
        connection,
        plan,
        row_count=count,
        where=f"s3://{target.bucket}/{dated}",
        error=error,
        now=moment,
        via=via,
        request=request,
    )
    await session.commit()
    return {
        "delivered": error is None,
        "export_run_id": str(run.id),
        "row_count": 0 if error else count,
        "files": []
        if error
        else [f"s3://{target.bucket}/{dated}", f"s3://{target.bucket}/{base}/latest.csv"],
        "error": error,
    }


def new_feed_token() -> tuple[str, str]:
    """(token, its hash). The token is shown once."""
    token = f"bif_{secrets.token_urlsafe(32)}"
    return token, hashlib.sha256(token.encode()).hexdigest()


async def feed_csv(
    session: AsyncSession, token: str, *, request: Request | None = None
) -> tuple[str, str]:
    """The feed link's CSV, compiled now. (filename, content)."""
    if not token.startswith("bif_") or len(token) > 100:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    digest = hashlib.sha256(token.encode()).hexdigest()
    connection = await session.scalar(
        select(BiConnection).where(BiConnection.feed_token_hash == digest)
    )
    # Compare once more in constant time, so a near-miss is not a timing oracle.
    if connection is None or not hmac.compare_digest(connection.feed_token_hash or "", digest):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    if not connection.is_active:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This feed is switched off.")
    person = await _creator(session, connection)
    if person is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="The admin who set this feed up is no longer an admin. Create it again.",
        )
    now = datetime.now(UTC)
    plan, rows = await _rows(session, person, connection, request=request)
    content, count = _csv(plan, rows)
    await _record(
        session,
        person,
        connection,
        plan,
        row_count=count,
        where="feed link",
        error=None,
        now=now,
        via="read by a BI tool",
        request=request,
    )
    await session.commit()
    return f"{connection.entity_name}.csv", content


async def scheduled(session: AsyncSession, actor: User | None, *, now: datetime) -> dict[str, Any]:
    """Every night: each active bucket connection set to daily, as the admin who set it up."""
    if actor is None:
        return {"delivered": 0, "failed": 0}
    rows = (
        await session.scalars(
            select(BiConnection).where(
                tenant_filter(BiConnection, actor.tenant_id),
                BiConnection.is_active.is_(True),
                BiConnection.schedule == "daily",
                BiConnection.destination == BiDestinationKind.OBJECT_STORAGE,
            )
        )
    ).all()
    delivered = failed = 0
    for connection in rows:
        person = await _creator(session, connection)
        if person is None:
            connection.last_delivery_error = (
                "Not delivered: the admin who set this connection up is no longer an admin. "
                "An admin needs to create it again."
            )
            await session.commit()
            failed += 1
            continue
        result = await deliver(
            session, person, connection_id=connection.id, via="every night", now=now
        )
        if result["delivered"]:
            delivered += 1
        else:
            failed += 1
    return {"delivered": delivered, "failed": failed}


def encrypt_storage_secret(secret: str, tenant_id: uuid.UUID) -> str:
    return encrypt_secret(secret.strip(), tenant_id)
