"""Culture layer endpoints: the feed and clubs (FRD 5.3, FS-CUL-01).

Thin by design: parse, gate, delegate to `app.services.culture.feed`, serialise.
Who may see, react to, delete or pin a shoutout - and who may read a club's
members - is decided in the service, in the query.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.schemas.common import Page
from app.schemas.culture import (
    ClubCreate,
    ClubMemberRead,
    ClubParticipationRead,
    ClubRead,
    CompanyValueRead,
    ReactionSet,
    ReactionSummary,
    ShoutoutCreate,
    ShoutoutPin,
    ShoutoutRead,
)
from app.schemas.engagement import EventCreate, PollCreate, PollVoteIn, RsvpIn
from app.services.culture import engagement, feed, milestones

router = APIRouter(prefix="/culture", tags=["culture"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]


# ── Shoutouts ───────────────────────────────────────────────────────────────


@router.get("/shoutouts", response_model=Page[ShoutoutRead])
async def list_shoutouts(
    user: CurrentUser,
    session: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Page[ShoutoutRead]:
    """The feed. Pinned first, then newest; the total counts what is visible."""
    items, total = await feed.list_feed(session, user, page=page, page_size=page_size)
    return Page[ShoutoutRead](items=items, total=total, page=page, page_size=page_size)


@router.post("/shoutouts", response_model=ShoutoutRead, status_code=status.HTTP_201_CREATED)
async def create_shoutout(
    payload: ShoutoutCreate, user: CurrentUser, session: DbSession, request: Request
) -> ShoutoutRead:
    """Post a shoutout from the caller's own record. Never in somebody else's name."""
    return await feed.create_shoutout(session, user, payload, request=request)


@router.delete(
    "/shoutouts/{shoutout_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_shoutout(
    shoutout_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await feed.delete_shoutout(session, user, shoutout_id, request=request)


@router.put("/shoutouts/{shoutout_id}/pin", response_model=ShoutoutRead, dependencies=HR_ONLY)
async def pin_shoutout(
    shoutout_id: uuid.UUID,
    payload: ShoutoutPin,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ShoutoutRead:
    return await feed.pin_shoutout(
        session, user, shoutout_id, is_pinned=payload.is_pinned, request=request
    )


@router.put("/shoutouts/{shoutout_id}/reactions", response_model=list[ReactionSummary])
async def set_reaction(
    shoutout_id: uuid.UUID,
    payload: ReactionSet,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> list[ReactionSummary]:
    """Set the caller's reaction on or off. Idempotent."""
    return await feed.set_reaction(
        session, user, shoutout_id, emoji=payload.emoji, active=payload.active, request=request
    )


# ── Values (the chip on a shoutout) ─────────────────────────────────────────


@router.get("/values", response_model=list[CompanyValueRead])
async def list_values(
    user: CurrentUser,
    session: DbSession,
    include_inactive: bool = False,
) -> list[CompanyValueRead]:
    """The company's values. Inactive ones only for HR, who maintain them."""
    return await feed.list_values(session, user, include_inactive=include_inactive)


# ── Clubs ───────────────────────────────────────────────────────────────────


@router.get("/clubs", response_model=list[ClubRead])
async def list_clubs(user: CurrentUser, session: DbSession) -> list[ClubRead]:
    return await feed.list_clubs(session, user)


@router.get("/clubs/participation", response_model=ClubParticipationRead, dependencies=HR_ONLY)
async def club_participation(user: CurrentUser, session: DbSession) -> ClubParticipationRead:
    """Participation in aggregate. Never who joined what."""
    return await feed.club_participation(session, user)


@router.post("/clubs", response_model=ClubRead, status_code=status.HTTP_201_CREATED)
async def create_club(
    payload: ClubCreate, user: CurrentUser, session: DbSession, request: Request
) -> ClubRead:
    return await feed.create_club(session, user, payload, request=request)


@router.post("/clubs/{club_id}/join", response_model=ClubRead)
async def join_club(
    club_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> ClubRead:
    return await feed.join_club(session, user, club_id, request=request)


@router.post("/clubs/{club_id}/leave", response_model=ClubRead | None)
async def leave_club(
    club_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> ClubRead | None:
    """Leave a club. Returns null when you were its last member and it closed."""
    return await feed.leave_club(session, user, club_id, request=request)


class ClubPostIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.get("/clubs/{club_id}/posts")
async def club_posts(club_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[dict]:
    """What the club's members have posted, newest first."""
    return await feed.club_posts(session, user, club_id)


@router.post("/clubs/{club_id}/posts", status_code=status.HTTP_201_CREATED)
async def add_club_post(
    club_id: uuid.UUID,
    payload: ClubPostIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict:
    return await feed.add_club_post(session, user, club_id, payload.body, request=request)


@router.delete("/club-posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_club_post(
    post_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await feed.delete_club_post(session, user, post_id, request=request)


@router.get("/clubs/{club_id}/members", response_model=list[ClubMemberRead])
async def club_members(
    club_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[ClubMemberRead]:
    return await feed.club_members(session, user, club_id)


# Your Voice needs a schema review with the compliance SME first - FS-CUL-02
# requires that a submitter's identity is never stored anywhere a case handler
# can retrieve it, which is a data-model decision, not an access-control one.


# ── Work anniversaries ──────────────────────────────────────────────────────


class MilestoneRuleIn(BaseModel):
    years: int = Field(ge=1, le=60)
    points: int | None = Field(default=None, ge=0, le=1_000_000)
    message: str | None = Field(default=None, max_length=1000)
    active: bool = True


class MilestoneRulesIn(BaseModel):
    rules: list[MilestoneRuleIn] = Field(default_factory=list, max_length=30)


@router.get("/milestones", dependencies=HR_ONLY)
async def list_milestone_rules(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Which work anniversaries the company marks, with what message and points."""
    return await milestones.list_rules(session, user)


@router.put("/milestones", dependencies=HR_ONLY)
async def save_milestone_rules(
    payload: MilestoneRulesIn, user: CurrentUser, session: DbSession, request: Request
) -> list[dict[str, Any]]:
    result = await milestones.save_rules(
        session, user, [r.model_dump() for r in payload.rules], request=request
    )
    await session.commit()
    return result


# ── Polls and events ────────────────────────────────────────────────────────


@router.get("/polls")
async def list_polls(user: CurrentUser, session: DbSession) -> list[dict]:
    """Polls for the caller's audience, newest first, with results where they may be seen."""
    return await engagement.list_polls(session, user)


@router.post("/polls", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY)
async def create_poll(
    payload: PollCreate, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    return await engagement.create_poll(session, user, payload, request=request)


@router.post("/polls/{poll_id}/vote")
async def vote_in_poll(
    poll_id: uuid.UUID, payload: PollVoteIn, user: CurrentUser, session: DbSession
) -> dict:
    """Vote, or change your vote while the poll is open."""
    return await engagement.vote(session, user, poll_id, payload.option_index)


@router.post("/polls/{poll_id}/close", dependencies=HR_ONLY)
async def close_poll(
    poll_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    return await engagement.close_poll(session, user, poll_id, request=request)


@router.get("/events")
async def list_company_events(
    user: CurrentUser, session: DbSession, include_past: bool = False
) -> list[dict]:
    """Upcoming company events for the caller's audience."""
    return await engagement.list_events(session, user, include_past=include_past)


@router.post("/events", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY)
async def create_company_event(
    payload: EventCreate, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    return await engagement.create_event(session, user, payload, request=request)


@router.post("/events/{event_id}/rsvp")
async def rsvp_to_event(
    event_id: uuid.UUID, payload: RsvpIn, user: CurrentUser, session: DbSession
) -> dict:
    """Going, maybe or not going. A full event refuses another going."""
    return await engagement.rsvp(session, user, event_id, payload.response)


@router.post("/events/{event_id}/cancel", dependencies=HR_ONLY)
async def cancel_company_event(
    event_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    return await engagement.cancel_event(session, user, event_id, request=request)
