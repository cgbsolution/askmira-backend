"""Audit trail writer.

Insert-only, by design (FRD 6). The write joins the caller's transaction rather
than opening its own, so an audit entry can never survive a change that rolled
back — and a change can never commit without its audit entry.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditLog
from app.models.user import User

# Never written to the audit trail, whatever the caller passes.
REDACTED_FIELDS = frozenset(
    {
        "password",
        "password_hash",
        "new_password",
        "current_password",
        "bank_account_number",
        "bank_ifsc",
        "aadhaar",
        "aadhaar_last4",
        "pan",
    }
)
REDACTED = "[redacted]"


def _redact(changes: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (REDACTED if key.lower() in REDACTED_FIELDS else value)
        for key, value in changes.items()
    }


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: AuditAction,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    actor: User | None = None,
    # Scalar alternative to `actor`, for callers that have rolled back since
    # loading the user. A rollback expires every instance in the session, and
    # reading `actor.id` afterwards is a lazy load — which raises
    # MissingGreenlet when it happens outside an awaited session call.
    actor_user_id: uuid.UUID | None = None,
    actor_type: str = "user",
    changes: dict[str, Any] | None = None,
    summary: str | None = None,
    request: Request | None = None,
    ai_context: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one audit entry to the caller's open transaction.

    Flushed but not committed — the caller's commit is what makes both the
    change and its audit record durable, together.
    """
    entry = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id if actor_user_id is not None else (actor.id if actor else None),
        actor_type=actor_type,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        changes=_redact(changes or {}),
        summary=summary,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        ai_context=ai_context,
    )
    session.add(entry)
    await session.flush()
    return entry


async def record_ai_action(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
    summary: str,
    model: str,
    actor: User | None = None,
    entity_id: uuid.UUID | None = None,
    human_approved: bool = False,
    **context: Any,
) -> AuditLog:
    """Log an AI Companion or MCP action (FS-AIT-01).

    `actor_type` stays "ai_companion" so an AI-initiated change is never
    indistinguishable from a human one in an export.
    """
    return await record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.AI_ACTION,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        actor_type="ai_companion",
        summary=summary,
        ai_context={"model": model, "human_approved": human_approved, **context},
    )
