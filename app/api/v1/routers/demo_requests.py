"""Demo requests: sent by the marketing website, read by the platform console."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.demo_request import DemoRequestStatus
from app.models.user import UserRole
from app.schemas.demo_request import DemoRequestCreate, DemoRequestUpdate
from app.services import demo_requests
from app.services.hiring.public_guard import Limit, refuse_honeypot, throttle

public_router = APIRouter(prefix="/public/demo-requests", tags=["demo-requests"])
platform_router = APIRouter(prefix="/platform/demo-requests", tags=["platform"])

#: Five requests an hour from one address is generous for a person and useless to a script.
SUBMIT_LIMIT = Limit("demo_request", 5, 3600)

SUPER_ONLY = [Depends(require_roles(UserRole.SUPER_ADMIN))]


@public_router.post("", status_code=status.HTTP_201_CREATED)
async def submit_demo_request(
    payload: DemoRequestCreate, session: DbSession, request: Request
) -> dict[str, str]:
    """A demo request from the website. No sign-in: the sender is not a customer yet."""
    throttle(request, SUBMIT_LIMIT)
    refuse_honeypot(payload.website)
    row = await demo_requests.submit(session, payload)
    return {"id": str(row.id), "status": "received"}


@platform_router.get("", dependencies=SUPER_ONLY)
async def list_demo_requests(
    user: CurrentUser, session: DbSession, status_filter: DemoRequestStatus | None = None
) -> list[dict[str, Any]]:
    return await demo_requests.list_requests(session, user, status_filter=status_filter)


@platform_router.patch("/{request_id}", dependencies=SUPER_ONLY)
async def update_demo_request(
    request_id: uuid.UUID,
    payload: DemoRequestUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await demo_requests.update(session, user, request_id, payload, request=request)


@platform_router.delete(
    "/{request_id}",
    dependencies=SUPER_ONLY,
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def erase_demo_request(
    request_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    """Delete an enquiry because the person who sent it asked."""
    await demo_requests.erase(session, user, request_id, request=request)
