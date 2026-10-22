"""CGB Helpdesk in the portal: one ticket's page, the Raise a ticket form, replying.

When a workspace connects CGB Helpdesk, its Helpdesk and Assets sections show
the helpdesk's own records. The lists — the person's tickets, the queues, the
assets they hold, the asset register — are views (views.py), read through Ask
Mira's lookups. This module covers what a list can't: one ticket with its
conversation, the choices the Raise a ticket form offers, and the two things a
person does from here, raising a ticket and replying on one.

Both happen in the helpdesk, as the person (data.act, signed use=act); the
helpdesk keeps the ticket and does its own notifying, exactly as when it is
raised there. AskMira writes an audit entry for each once the helpdesk has
done it — the change itself is the helpdesk's, so there is no AskMira
transaction to share. Nothing is copied into AskMira, and no subject or
message text goes into the audit entry.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction
from app.models.user import User
from app.services import audit
from app.services.products import data as products_data
from app.services.products.catalogue import PRODUCTS

__all__ = ["options", "raise_ticket", "reply", "ticket"]

PRODUCT = "helpdesk"
BRAND = PRODUCTS[PRODUCT].brand


def _refused(exc: products_data.ProductUnavailableError) -> HTTPException:
    """The helpdesk's refusal as an answer the portal can show."""
    code = {
        403: status.HTTP_403_FORBIDDEN,
        404: status.HTTP_404_NOT_FOUND,
        409: status.HTTP_409_CONFLICT,
        422: status.HTTP_422_UNPROCESSABLE_ENTITY,
    }.get(exc.status or 0, status.HTTP_502_BAD_GATEWAY)
    return HTTPException(status_code=code, detail=str(exc))


async def _ask(session: AsyncSession, user: User, kind: str, params: dict[str, Any]) -> dict:
    try:
        return await products_data.ask(session, user, PRODUCT, kind, params)
    except products_data.ProductUnavailableError as exc:
        raise _refused(exc) from exc


async def _act(session: AsyncSession, user: User, kind: str, params: dict[str, Any]) -> dict:
    try:
        return await products_data.act(session, user, PRODUCT, kind, params)
    except products_data.ProductUnavailableError as exc:
        raise _refused(exc) from exc


def _id(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _ticket_id(value: str) -> str:
    parsed = _id(value)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="There is no such ticket."
        )
    return str(parsed)


def _open_in_helpdesk(ticket_id: str) -> str:
    """The ticket in the helpdesk itself, opened through AskMira's sign-in."""
    return f"/launch/{PRODUCT}?to=/ticket/{ticket_id}"


async def ticket(session: AsyncSession, user: User, ticket_id: str) -> dict[str, Any]:
    """One ticket, its conversation and what happened to it, if the person may see it."""
    tid = _ticket_id(ticket_id)
    answer = await _ask(session, user, "ticket", {"id": tid})
    detail = answer.get("ticket")
    if not isinstance(detail, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{BRAND} sent no ticket."
        )
    return {
        "source": BRAND,
        "ticket": detail,
        "messages": [m for m in answer.get("messages") or [] if isinstance(m, dict)],
        "activity": [a for a in answer.get("activity") or [] if isinstance(a, dict)],
        "open": _open_in_helpdesk(tid),
    }


async def options(session: AsyncSession, user: User) -> dict[str, Any]:
    """What the Raise a ticket form offers this person: categories, priorities, their assets."""
    answer = await _ask(session, user, "options", {})
    return {
        "source": BRAND,
        "categories": [c for c in answer.get("categories") or [] if isinstance(c, dict)],
        "priorities": [str(p) for p in answer.get("priorities") or []],
        "contact": answer.get("contact"),
        "assets": [a for a in answer.get("assets") or [] if isinstance(a, dict)],
    }


async def raise_ticket(
    session: AsyncSession, user: User, fields: dict[str, Any], *, request: Request | None
) -> dict[str, Any]:
    """Raise a ticket in the helpdesk as the person; it is worked there like any other."""
    answer = await _act(session, user, "create_ticket", fields)
    created = answer.get("ticket")
    if not isinstance(created, dict) or not created.get("id"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{BRAND} did not say which ticket."
        )
    ref = str(created.get("ref") or "")[:12]
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="helpdesk_ticket",
        entity_id=_id(created.get("id")),
        actor=user,
        changes={
            "product": PRODUCT,
            "ticket": ref,
            "category": fields.get("category"),
            "priority": fields.get("priority"),
            "asset_linked": bool(fields.get("asset_id")),
        },
        summary=f"Raised {BRAND} ticket #{ref}",
        request=request,
    )
    await session.commit()
    return {"ticket": created, "page": f"/linked/{PRODUCT}/tickets/{created['id']}"}


async def reply(
    session: AsyncSession, user: User, ticket_id: str, message: str, *, request: Request | None
) -> dict[str, Any]:
    """Write on a ticket's conversation as the person; the other side hears of it there."""
    tid = _ticket_id(ticket_id)
    answer = await _act(session, user, "reply", {"ticket_id": tid, "message": message})
    written = answer.get("message")
    if not isinstance(written, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{BRAND} did not keep the message."
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="helpdesk_reply",
        entity_id=_id(tid),
        actor=user,
        changes={"product": PRODUCT, "ticket": tid[:8].upper(), "length": len(message)},
        summary=f"Replied on {BRAND} ticket #{tid[:8].upper()}",
        request=request,
    )
    await session.commit()
    return {"message": written}
