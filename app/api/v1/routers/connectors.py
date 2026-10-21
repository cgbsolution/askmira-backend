"""Settings → Connectors: a workspace's admin connects AnytimeHire and ExpenseGenie."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services.products import connectors

router = APIRouter(
    prefix="/connectors",
    tags=["connectors"],
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=20, max_length=200)
    state: str = Field(min_length=10, max_length=4000)


@router.get("")
async def list_connectors(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Each product: whether this workspace is connected, to which organisation, last sync."""
    return await connectors.overview(session, user)


@router.post("/{product}/start")
async def start(product: str, user: CurrentUser, session: DbSession) -> dict[str, str]:
    """Where to send the browser to sign in to the product and press Allow."""
    return await connectors.start(session, user, product)


@router.post("/{product}/complete")
async def complete(
    product: str, payload: CompleteIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Back from Allow: swap the one-time code for this workspace's own secret, then sync."""
    return await connectors.complete(
        session, user, product, code=payload.code, state=payload.state, request=request
    )


@router.post("/{product}/sync")
async def sync(
    product: str, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await connectors.sync_now(session, user, product, request=request)


@router.delete("/{product}")
async def disconnect(
    product: str, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await connectors.disconnect(session, user, product, request=request)
