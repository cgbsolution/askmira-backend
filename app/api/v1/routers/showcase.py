"""Showcase links: the super admin's exhibition QR codes, and the story a scan opens.

`platform_router` is the console (super admin only). `public_router` has no
sign-in by design: it is what a scanned QR code opens. A visitor's story is
carried on by the run's key in the `X-Showcase-Key` header, never a session.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.showcase import (
    ShowcaseDecideIn,
    ShowcaseLinkCreate,
    ShowcaseLinkRead,
    ShowcaseLinkUpdate,
    ShowcaseSayIn,
)
from app.services.showcase import showcase_links

platform_router = APIRouter(prefix="/platform/showcase-links", tags=["platform"])
public_router = APIRouter(prefix="/public/showcase", tags=["showcase"])

SUPER_ONLY = [Depends(require_roles(UserRole.SUPER_ADMIN))]
_PRIVATE = {"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"}


# ── The console ──────────────────────────────────────────────────────────────


@platform_router.get("", dependencies=SUPER_ONLY, response_model=list[ShowcaseLinkRead])
async def list_links(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await showcase_links.list_links(session, user)


@platform_router.post(
    "",
    dependencies=SUPER_ONLY,
    response_model=ShowcaseLinkRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_link(
    body: ShowcaseLinkCreate, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await showcase_links.create_link(session, user, body, request=request)


@platform_router.patch("/{link_id}", dependencies=SUPER_ONLY, response_model=ShowcaseLinkRead)
async def update_link(
    link_id: uuid.UUID,
    body: ShowcaseLinkUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await showcase_links.update_link(session, user, link_id, body, request=request)


@platform_router.delete(
    "/{link_id}",
    dependencies=SUPER_ONLY,
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_link(
    link_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await showcase_links.delete_link(session, user, link_id, request=request)


@platform_router.get("/{link_id}/qr", dependencies=SUPER_ONLY, response_model=None)
async def qr_code(
    link_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    format: Literal["svg", "png"] = "svg",
) -> Response:
    data, media, filename = await showcase_links.qr_code(session, user, link_id, format)
    return Response(
        content=data,
        media_type=media,
        headers={**_PRIVATE, "Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── A visitor ────────────────────────────────────────────────────────────────


@public_router.get("/{code}")
async def describe(
    code: str, session: DbSession, request: Request, response: Response
) -> dict[str, Any]:
    response.headers.update(_PRIVATE)
    return await showcase_links.describe(session, code, request)


@public_router.post("/{code}/runs", status_code=status.HTTP_201_CREATED)
async def start_run(
    code: str, session: DbSession, request: Request, response: Response
) -> dict[str, Any]:
    response.headers.update(_PRIVATE)
    return await showcase_links.start_run(session, code, request)


@public_router.get("/{code}/runs/{run_id}")
async def read_run(
    code: str,
    run_id: uuid.UUID,
    session: DbSession,
    request: Request,
    response: Response,
    x_showcase_key: str | None = Header(default=None, max_length=128),
) -> dict[str, Any]:
    response.headers.update(_PRIVATE)
    return await showcase_links.read_run(session, code, run_id, x_showcase_key, request)


@public_router.post("/{code}/runs/{run_id}/say")
async def say(
    code: str,
    run_id: uuid.UUID,
    body: ShowcaseSayIn,
    session: DbSession,
    request: Request,
    response: Response,
    x_showcase_key: str | None = Header(default=None, max_length=128),
) -> dict[str, Any]:
    response.headers.update(_PRIVATE)
    return await showcase_links.say(session, code, run_id, x_showcase_key, body.text, request)


@public_router.post("/{code}/runs/{run_id}/decide")
async def decide(
    code: str,
    run_id: uuid.UUID,
    body: ShowcaseDecideIn,
    session: DbSession,
    request: Request,
    response: Response,
    x_showcase_key: str | None = Header(default=None, max_length=128),
) -> dict[str, Any]:
    response.headers.update(_PRIVATE)
    return await showcase_links.decide(
        session, code, run_id, x_showcase_key, body.action_id, body.confirm, request
    )
