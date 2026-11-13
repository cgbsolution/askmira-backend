"""Showcase links: the super admin's exhibition QR codes, and the Ask Miraa chat a scan opens.

`platform_router` is the console (super admin only). `public_router` has no
sign-in by design: it is what a scanned QR code opens. A visitor's chat is
carried on by the run's key in the `X-Showcase-Key` header, never a session.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.showcase import (
    ShowcaseLinkCreate,
    ShowcaseLinkRead,
    ShowcaseLinkUpdate,
    ShowcaseSayIn,
    ShowcaseStartIn,
)
from app.services import uploads
from app.services.companion import attachments
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

Key = Annotated[str | None, Header(alias="X-Showcase-Key", max_length=128)]


@public_router.get("/{code}")
async def describe(
    code: str, session: DbSession, request: Request, response: Response
) -> dict[str, Any]:
    response.headers.update(_PRIVATE)
    return await showcase_links.describe(session, code, request)


@public_router.post("/{code}/runs", status_code=status.HTTP_201_CREATED)
async def start_run(
    code: str, body: ShowcaseStartIn, session: DbSession, request: Request, response: Response
) -> dict[str, Any]:
    response.headers.update(_PRIVATE)
    return await showcase_links.start_run(session, code, body.pin, request)


@public_router.get("/{code}/runs/{run_id}")
async def read_run(
    code: str,
    run_id: uuid.UUID,
    session: DbSession,
    request: Request,
    response: Response,
    x_showcase_key: Key = None,
) -> dict[str, Any]:
    response.headers.update(_PRIVATE)
    return await showcase_links.read_run(session, code, run_id, x_showcase_key, request)


@public_router.post("/{code}/runs/{run_id}/messages")
async def say(
    code: str,
    run_id: uuid.UUID,
    body: ShowcaseSayIn,
    session: DbSession,
    request: Request,
    response: Response,
    x_showcase_key: Key = None,
) -> dict[str, Any]:
    """One message to the workspace's Ask Miraa, in this visitor's own conversation."""
    response.headers.update(_PRIVATE)
    return await showcase_links.say(session, code, run_id, x_showcase_key, body, request)


@public_router.post("/{code}/runs/{run_id}/messages/files")
async def say_with_files(
    code: str,
    run_id: uuid.UUID,
    session: DbSession,
    request: Request,
    response: Response,
    files: Annotated[list[UploadFile], File()],
    text: Annotated[str, Form(max_length=2000)] = "",
    client_ref: Annotated[
        str | None, Form(min_length=8, max_length=64, pattern=r"^[\w-]+$")
    ] = None,
    x_showcase_key: Key = None,
) -> dict[str, Any]:
    """A message with pictures or PDFs (a visiting card, a LinkedIn profile)."""
    response.headers.update(_PRIVATE)
    if len(files) > attachments.MAX_FILES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Attach up to {attachments.MAX_FILES} files to one message.",
        )
    workspace_id = await showcase_links.workspace_of(session, code)
    raw = [
        (file.filename or "file", await uploads.read_file(file, session, workspace_id))
        for file in files
    ]
    body = ShowcaseSayIn(text=text, client_ref=client_ref)
    return await showcase_links.say(session, code, run_id, x_showcase_key, body, request, files=raw)


@public_router.post("/{code}/runs/{run_id}/actions/{action_id}/{choice}")
async def decide(
    code: str,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    choice: Literal["confirm", "cancel"],
    session: DbSession,
    request: Request,
    response: Response,
    x_showcase_key: Key = None,
) -> dict[str, Any]:
    """Confirm or Cancel on an offer Ask Miraa made in this visitor's chat."""
    response.headers.update(_PRIVATE)
    return await showcase_links.decide(
        session, code, run_id, x_showcase_key, action_id, choice == "confirm", request
    )
