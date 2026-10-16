"""Opening a linked product (the interview tool, the expense tool) from the portal."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentUser, DbSession
from app.services.products import service as products

router = APIRouter(prefix="/products", tags=["products"])


@router.post("/{product}/launch")
async def launch(product: str, user: CurrentUser, session: DbSession) -> dict[str, str]:
    """A one-time link, good for 60 seconds, that signs the caller in to the product."""
    return await products.launch(session, user, product)
