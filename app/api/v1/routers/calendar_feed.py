"""My calendar link: holidays and approved leave in any calendar app, by subscription."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict

from app.core.deps import CurrentUser, DbSession
from app.services import calendar_feed

router = APIRouter(tags=["calendar-feed"])


class FeedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_team: bool = False


@router.get("/me/calendar-feed")
async def my_feed(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await calendar_feed.feed_status(session, user)


@router.post("/me/calendar-feed", status_code=status.HTTP_201_CREATED)
async def make_feed(
    payload: FeedIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """A new private link, shown once. An earlier link stops working."""
    return await calendar_feed.create_feed(
        session, user, include_team=payload.include_team, request=request
    )


@router.delete("/me/calendar-feed", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def stop_feed(user: CurrentUser, session: DbSession, request: Request) -> None:
    await calendar_feed.revoke_feed(session, user, request=request)


@router.get("/public/calendar/{token}.ics", include_in_schema=False)
async def feed(token: str, session: DbSession) -> Response:
    """The .ics a calendar app subscribes to. The token in the address is the credential."""
    body = await calendar_feed.render_feed(session, token[:128])
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "private, max-age=900"},
    )
