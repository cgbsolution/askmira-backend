"""Opening a linked product (the interview tool, the expense tool) from the portal, and
reading its records as portal pages."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, DbSession
from app.services.products import service as products
from app.services.products import views

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
