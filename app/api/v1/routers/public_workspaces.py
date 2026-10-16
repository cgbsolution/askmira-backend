"""A company's public sign-in page: its name, logo and single sign-on.

No sign-in in front of these: they are what the sign-in page draws before
anybody has signed in. They reveal what the company's own website does.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.deps import DbSession
from app.services import branding
from app.services.hiring.public_guard import PORTAL_READ, throttle
from app.services.identity import directory

router = APIRouter(prefix="/public/workspaces", tags=["sign-in"])


@router.get("/{slug}")
async def workspace_page(slug: str, session: DbSession, request: Request) -> dict[str, Any]:
    throttle(request, PORTAL_READ)
    tenant = await directory.workspace_by_slug(session, slug)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such workspace.")
    if not tenant.is_active:
        raise directory.suspended()
    return (await directory.branding(session, tenant)).as_dict()


@router.get("/{slug}/logo", response_model=None)
async def workspace_logo(slug: str, session: DbSession) -> Response:
    data, media_type = await branding.read_logo(session, slug)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )
