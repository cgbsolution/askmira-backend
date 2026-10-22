"""Opening a linked product (the interview tool, the expense tool, the helpdesk) from the
portal, reading its records as portal pages, and raising and answering helpdesk tickets."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession
from app.services.products import helpdesk, views
from app.services.products import service as products

router = APIRouter(prefix="/products", tags=["products"])


@router.post("/{product}/launch")
async def launch(
    product: str, user: CurrentUser, session: DbSession, to: str | None = None
) -> dict[str, str]:
    """A one-time link, good for 60 seconds, that signs the caller in to the product.

    `to`: a page inside the product to land on, such as a job's candidates.
    """
    return await products.launch(session, user, product, to=to)


@router.get("/{product}/views/{view}")
async def product_view(
    product: str,
    view: str,
    user: CurrentUser,
    session: DbSession,
    status: str | None = None,
    query: str | None = None,
    scope: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> dict[str, Any]:
    """One page of a linked product's records (claims, jobs…), read live as the caller."""
    params = {"status": status, "query": query, "scope": scope, "from": from_, "to": to}
    return await views.render(
        session, user, product, view, {k: v for k, v in params.items() if v is not None}
    )


# ── CGB Helpdesk: one ticket, the Raise a ticket form, replies ───────────────


class HelpdeskTicketCreate(BaseModel):
    """A ticket raised from the portal, as the helpdesk's own New Ticket form takes it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject: str = Field(min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    category: str = Field(min_length=1, max_length=200)
    sub_category: str | None = Field(default=None, max_length=200)
    priority: Literal["low", "medium", "high"]
    location: str | None = Field(default=None, max_length=200)
    contact: str | None = Field(default=None, max_length=30)
    link: str | None = Field(default=None, max_length=500)
    asset_id: str | None = Field(default=None, max_length=64)


class HelpdeskReplyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=4000)


@router.get("/helpdesk/ticket-options")
async def helpdesk_ticket_options(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """What the Raise a ticket form offers the caller: categories, priorities, their assets."""
    return await helpdesk.options(session, user)


@router.post("/helpdesk/tickets", status_code=201)
async def raise_helpdesk_ticket(
    payload: HelpdeskTicketCreate, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Raise a ticket in the helpdesk as the caller."""
    return await helpdesk.raise_ticket(
        session, user, payload.model_dump(exclude_none=True), request=request
    )


@router.get("/helpdesk/tickets/{ticket_id}")
async def helpdesk_ticket(ticket_id: str, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """One ticket and its conversation, read live from the helpdesk as the caller."""
    return await helpdesk.ticket(session, user, ticket_id)


@router.post("/helpdesk/tickets/{ticket_id}/replies", status_code=201)
async def reply_on_helpdesk_ticket(
    ticket_id: str,
    payload: HelpdeskReplyCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Write on a ticket's conversation as the caller."""
    return await helpdesk.reply(session, user, ticket_id, payload.message, request=request)
