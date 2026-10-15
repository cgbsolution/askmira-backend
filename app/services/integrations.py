"""API keys, webhook endpoints, and the pump that turns audit entries into deliveries.

**Keys.** `amk_` followed by 40 random characters; only the SHA-256 is stored.
A key acts as a read-only system identity inside its workspace and may read
only what its scopes name.

**Webhooks.** Every minute the pump reads each active endpoint's audit entries
since its cursor — minus a ten-minute overlap, because a long transaction
commits audit rows stamped earlier than rows already seen — and adds a
delivery for each new one; `(endpoint, audit entry)` is unique, so the overlap
never sends twice. Deliveries are POSTed with an HMAC-SHA256 signature of the
body, retried with growing gaps up to `MAX_ATTEMPTS`, then marked failed.

**Where a delivery may go.** https only, and never to a private, loopback or
link-local address — checked when the endpoint is saved and again, after DNS,
when each delivery is sent, so a hostname that later points inside the network
is still refused.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction, AuditLog
from app.models.integrations import (
    ApiKey,
    DeliveryStatus,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookFormat,
)
from app.models.user import User, UserRole
from app.services import audit

__all__ = [
    "SCOPES",
    "ApiKeyIn",
    "EndpointIn",
    "authenticate_key",
    "create_endpoint",
    "create_key",
    "deliver_due",
    "key_identity",
    "list_deliveries",
    "list_endpoints",
    "list_keys",
    "pump_events",
    "revoke_key",
    "set_endpoint_active",
]

SCOPES = (
    "employees:read",
    "employees:write",
    "leave:read",
    "attendance:read",
    "attendance:write",
    "org:read",
    "hiring:read",
)
MAX_ATTEMPTS = 8
OVERLAP = timedelta(minutes=10)
BATCH = 200
TIMEOUT_SECONDS = 10


class ApiKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=120)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


class EndpointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=120)
    url: str = Field(min_length=12, max_length=1000)
    entity_types: list[str] = Field(default_factory=list, max_length=100)
    format: WebhookFormat = WebhookFormat.ASKMIRA


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _require_admin(user: User) -> None:
    if not permissions.is_admin(user):
        raise permissions.forbidden("API keys and webhooks are managed by an admin.")


# ── API keys ────────────────────────────────────────────────────────────────


def _key_read(row: ApiKey) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "key_hint": row.key_hint,
        "scopes": list(row.scopes or []),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def create_key(
    session: AsyncSession, user: User, payload: ApiKeyIn, *, request: Request | None
) -> dict[str, Any]:
    _require_admin(user)
    unknown = set(payload.scopes) - set(SCOPES)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown scope(s): {', '.join(sorted(unknown))}.",
        )
    plaintext = f"amk_{secrets.token_urlsafe(30)}"
    row = ApiKey(
        tenant_id=user.tenant_id,
        name=payload.name.strip(),
        key_hash=_hash(plaintext),
        key_hint=f"{plaintext[:8]}…{plaintext[-4:]}",
        scopes=sorted(set(payload.scopes)),
        created_by_user_id=user.id,
        expires_at=payload.expires_at,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="api_key",
        entity_id=row.id,
        actor=user,
        changes={"name": row.name, "scopes": row.scopes},
        summary=f"Created the API key {row.name}",
        request=request,
    )
    await session.commit()
    return {**_key_read(row), "key": plaintext}


async def list_keys(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    _require_admin(user)
    rows = (
        await session.scalars(
            tenant_scoped(select(ApiKey).order_by(ApiKey.created_at.desc()), ApiKey, user)
        )
    ).all()
    return [_key_read(r) for r in rows]


async def revoke_key(
    session: AsyncSession, user: User, key_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    _require_admin(user)
    row = await session.scalar(
        tenant_scoped(select(ApiKey).where(ApiKey.id == key_id), ApiKey, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="api_key",
            entity_id=row.id,
            actor=user,
            changes={"revoked": True},
            summary=f"Revoked the API key {row.name}",
            request=request,
        )
        await session.commit()
    return _key_read(row)


async def key_identity(session: AsyncSession, key: str) -> tuple[User, frozenset[str]]:
    """The identity a key acts as and every scope it holds, or 401.

    For a caller that decides per operation which scope it needs — the MCP
    server lists only the tools a key may call.
    """
    row = await session.scalar(select(ApiKey).where(ApiKey.key_hash == _hash(key)))
    now = datetime.now(UTC)
    if (
        row is None
        or row.revoked_at is not None
        or (row.expires_at is not None and row.expires_at <= now)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown, revoked or expired key."
        )
    row.last_used_at = now
    await session.commit()
    identity = User(
        tenant_id=row.tenant_id,
        email="api-key@system.askmira.invalid",
        full_name=f"API key {row.name}",
        role=UserRole.SUB_ADMIN,
        is_active=True,
        password_hash="!",
        employee_id=None,
    )
    return identity, frozenset(row.scopes or [])


async def authenticate_key(session: AsyncSession, key: str, scope: str) -> User:
    """The identity a key acts as, or 401/403. It can do only what its scopes name.

    Not tenant-scoped: the key establishes the tenant, looked up by its unique
    hash. The identity returned is never persisted.
    """
    row = await session.scalar(select(ApiKey).where(ApiKey.key_hash == _hash(key)))
    now = datetime.now(UTC)
    if (
        row is None
        or row.revoked_at is not None
        or (row.expires_at is not None and row.expires_at <= now)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown, revoked or expired key."
        )
    if scope not in (row.scopes or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"This key does not have {scope}."
        )
    row.last_used_at = now
    await session.commit()
    acting = User(
        tenant_id=row.tenant_id,
        email="api-key@system.askmira.invalid",
        full_name=f"API key {row.name}",
        role=UserRole.SUB_ADMIN,
        is_active=True,
        password_hash="!",
        employee_id=None,
    )
    # Audited as the key, never as a person (see audit.record).
    acting.api_key_name = row.name
    acting.has_reports = False
    return acting


# ── Webhook endpoints ───────────────────────────────────────────────────────


def _public_address(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return True


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Use an https:// address."
        )
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That address is inside a private network.",
        )


def _endpoint_read(row: WebhookEndpoint) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "url": row.url,
        "entity_types": list(row.entity_types or []),
        "format": row.format.value,
        "active": row.active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def create_endpoint(
    session: AsyncSession, user: User, payload: EndpointIn, *, request: Request | None
) -> dict[str, Any]:
    _require_admin(user)
    _check_url(payload.url)
    secret = secrets.token_hex(32)
    row = WebhookEndpoint(
        tenant_id=user.tenant_id,
        name=payload.name.strip(),
        url=payload.url.strip(),
        secret_encrypted=secret,
        entity_types=sorted(set(payload.entity_types)),
        format=payload.format,
        cursor_at=datetime.now(UTC),
        created_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="webhook_endpoint",
        entity_id=row.id,
        actor=user,
        changes={"name": row.name, "url": row.url, "entity_types": row.entity_types},
        summary=f"Added the webhook {row.name}",
        request=request,
    )
    await session.commit()
    return {**_endpoint_read(row), "secret": secret}


async def list_endpoints(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    _require_admin(user)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(WebhookEndpoint).order_by(WebhookEndpoint.created_at.desc()),
                WebhookEndpoint,
                user,
            )
        )
    ).all()
    return [_endpoint_read(r) for r in rows]


async def set_endpoint_active(
    session: AsyncSession,
    user: User,
    endpoint_id: uuid.UUID,
    active: bool,
    *,
    request: Request | None,
) -> dict[str, Any]:
    _require_admin(user)
    row = await session.scalar(
        tenant_scoped(
            select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint_id), WebhookEndpoint, user
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    row.active = active
    if active:
        # Switched back on: from now, not a flood of everything missed.
        row.cursor_at = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="webhook_endpoint",
        entity_id=row.id,
        actor=user,
        changes={"active": active},
        summary=f"Webhook {row.name} {'on' if active else 'off'}",
        request=request,
    )
    await session.commit()
    return _endpoint_read(row)


async def list_deliveries(
    session: AsyncSession, user: User, endpoint_id: uuid.UUID
) -> list[dict[str, Any]]:
    _require_admin(user)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(WebhookDelivery)
                .where(WebhookDelivery.endpoint_id == endpoint_id)
                .order_by(WebhookDelivery.created_at.desc())
                .limit(100),
                WebhookDelivery,
                user,
            )
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "event": r.event,
            "status": r.status.value,
            "attempts": r.attempts,
            "last_status_code": r.last_status_code,
            "last_error": r.last_error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        }
        for r in rows
    ]


# ── The pump and the courier ────────────────────────────────────────────────


def _event_name(entry: AuditLog) -> str:
    action = entry.action.value if hasattr(entry.action, "value") else str(entry.action)
    return f"{entry.entity_type}.{action}"


async def pump_events(
    session: AsyncSession, tenant_id: uuid.UUID, *, now: datetime | None = None
) -> dict[str, Any]:
    """Turn each active endpoint's new audit entries into pending deliveries."""
    moment = now or datetime.now(UTC)
    endpoints = (
        await session.scalars(
            select(WebhookEndpoint).where(
                tenant_filter(WebhookEndpoint, tenant_id), WebhookEndpoint.active.is_(True)
            )
        )
    ).all()
    queued = 0
    for endpoint in endpoints:
        since = (endpoint.cursor_at or moment) - OVERLAP
        stmt = select(AuditLog).where(
            tenant_filter(AuditLog, tenant_id),
            AuditLog.created_at > since,
            AuditLog.created_at <= moment,
            AuditLog.created_at >= endpoint.created_at,
        )
        if endpoint.entity_types:
            stmt = stmt.where(AuditLog.entity_type.in_(endpoint.entity_types))
        entries = (await session.scalars(stmt.order_by(AuditLog.created_at).limit(BATCH))).all()
        seen = (
            set(
                (
                    await session.scalars(
                        select(WebhookDelivery.audit_log_id).where(
                            tenant_filter(WebhookDelivery, tenant_id),
                            WebhookDelivery.endpoint_id == endpoint.id,
                            WebhookDelivery.audit_log_id.in_([e.id for e in entries]),
                        )
                    )
                ).all()
            )
            if entries
            else set()
        )
        for entry in entries:
            if entry.id in seen or entry.entity_type in ("webhook_endpoint", "api_key"):
                continue
            session.add(
                WebhookDelivery(
                    tenant_id=tenant_id,
                    endpoint_id=endpoint.id,
                    audit_log_id=entry.id,
                    event=_event_name(entry),
                    payload={
                        "id": str(entry.id),
                        "event": _event_name(entry),
                        "occurred_at": entry.created_at.isoformat(),
                        "data": {
                            "entity_type": entry.entity_type,
                            "entity_id": str(entry.entity_id) if entry.entity_id else None,
                            "summary": entry.summary,
                        },
                    },
                    next_attempt_at=moment,
                )
            )
            queued += 1
        endpoint.cursor_at = entries[-1].created_at if len(entries) == BATCH else moment
    await session.commit()
    return {"queued": queued, "endpoints": len(endpoints)}


def _body(endpoint: WebhookEndpoint, delivery: WebhookDelivery) -> bytes:
    if endpoint.format is WebhookFormat.CHAT:
        data = delivery.payload.get("data") or {}
        text = data.get("summary") or delivery.event
        return json.dumps({"text": f"AskMira: {text}"}).encode("utf-8")
    return json.dumps(delivery.payload, separators=(",", ":")).encode("utf-8")


async def _send(endpoint: WebhookEndpoint, delivery: WebhookDelivery, body: bytes) -> int:
    import httpx

    host = urlparse(endpoint.url).hostname or ""
    if not await asyncio.to_thread(_public_address, host):
        raise RuntimeError("The address resolves inside a private network; not sent.")
    signature = hmac.new(
        endpoint.secret_encrypted.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        response = await client.post(
            endpoint.url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AskMira-Webhooks/1",
                "X-AskMira-Event": delivery.event,
                "X-AskMira-Delivery": str(delivery.id),
                "X-AskMira-Signature": f"sha256={signature}",
            },
        )
    return response.status_code


async def deliver_due(
    session: AsyncSession, tenant_id: uuid.UUID, *, now: datetime | None = None, sender=None
) -> dict[str, Any]:
    """Send pending deliveries that are due. `sender` replaces the HTTP call in tests."""
    moment = now or datetime.now(UTC)
    send = sender or _send
    rows = (
        await session.execute(
            select(WebhookDelivery, WebhookEndpoint)
            .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
            .where(
                tenant_filter(WebhookDelivery, tenant_id),
                WebhookDelivery.status == DeliveryStatus.PENDING,
                WebhookDelivery.next_attempt_at <= moment,
                WebhookEndpoint.active.is_(True),
            )
            .order_by(WebhookDelivery.created_at)
            .limit(BATCH)
        )
    ).all()
    sent = failed = 0
    for delivery, endpoint in rows:
        delivery.attempts += 1
        try:
            code = await send(endpoint, delivery, _body(endpoint, delivery))
            delivery.last_status_code = code
            ok = 200 <= code < 300
            delivery.last_error = None if ok else f"HTTP {code}"
        except Exception as exc:
            ok = False
            delivery.last_error = str(exc)[:500]
        if ok:
            delivery.status = DeliveryStatus.SENT
            delivery.sent_at = moment
            sent += 1
        elif delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = DeliveryStatus.FAILED
            failed += 1
        else:
            delivery.next_attempt_at = moment + timedelta(minutes=2**delivery.attempts)
    await session.commit()
    return {"sent": sent, "failed": failed, "tried": len(rows)}
