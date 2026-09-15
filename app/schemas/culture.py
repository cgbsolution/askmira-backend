"""Shoutouts, reactions, clubs and company values (FRD 5.3, FS-CUL-01)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.culture import ShoutoutVisibility

#: The reactions a shoutout accepts. A closed set, because a free-text emoji
#: field is a free-text field, and the feed is visible to the whole company.
ALLOWED_REACTIONS: tuple[str, ...] = ("👏", "❤️", "🎉", "🙌", "🚀", "😄")


# ── Shoutouts ───────────────────────────────────────────────────────────────


class ShoutoutCreate(BaseModel):
    to_employee_id: uuid.UUID
    message: str = Field(min_length=1, max_length=2000)
    value_tag: str | None = Field(default=None, max_length=64)
    visibility: ShoutoutVisibility = ShoutoutVisibility.COMPANY


class ShoutoutPin(BaseModel):
    is_pinned: bool


class ReactionSummary(BaseModel):
    emoji: str
    count: int
    #: Whether the caller is one of the people who reacted.
    reacted: bool


class PersonRef(BaseModel):
    employee_id: uuid.UUID
    name: str
    photo_url: str | None
    designation: str | None


class ShoutoutRead(BaseModel):
    id: uuid.UUID
    from_employee_id: uuid.UUID
    to_employee_id: uuid.UUID
    sender: PersonRef
    recipient: PersonRef
    message: str
    value_tag: str | None
    value_name: str | None
    visibility: ShoutoutVisibility
    is_pinned: bool
    points_awarded: int
    created_at: datetime
    reactions: list[ReactionSummary]
    can_delete: bool
    can_pin: bool


# ── Clubs ───────────────────────────────────────────────────────────────────


class ClubCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=16)
    is_private: bool = False


class ClubRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    icon: str | None
    is_private: bool
    member_count: int
    is_member: bool
    #: "member" or "organiser" when the caller belongs; otherwise None.
    my_role: str | None
    created_at: datetime


class ClubMemberRead(BaseModel):
    employee_id: uuid.UUID
    name: str
    photo_url: str | None
    designation: str | None
    role: str
    joined_at: datetime


# ── Values ──────────────────────────────────────────────────────────────────


class CompanyValueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    description: str
    icon: str | None
    colour: str | None
