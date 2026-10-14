"""Quick polls and company events with RSVPs.

A poll asks one question with a few options, to everyone or to an audience
(the same department / location / grade rule announcements use). A vote is
kept per person so nobody votes twice; an anonymous poll never shows who
voted, to anyone, including HR — the results are counts, and a breakdown is
withheld until enough people have answered that no single vote stands out.

A company event is a date, a place and an optional capacity. People answer
going, maybe or not going; once the capacity is reached further "going"
answers are refused rather than over-booked.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class Poll(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "polls"

    question: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Two to ten option labels, in order. A vote stores the option's index.
    options: Mapped[list] = mapped_column(JSONB, nullable=False)
    #: Same shape as an announcement audience. Empty means everybody.
    audience: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_polls_created_by"),
        nullable=True,
    )


class PollVote(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "poll_votes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "poll_id", "employee_id", name="uq_poll_vote_once"),
        CheckConstraint("option_index >= 0", name="ck_poll_vote_option"),
    )

    poll_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("polls.id", ondelete="CASCADE", name="fk_poll_votes_poll_id"),
        nullable=False,
        index=True,
    )
    #: Kept so a person votes once. Never returned for an anonymous poll.
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_poll_votes_employee_id"),
        nullable=False,
    )
    option_index: Mapped[int] = mapped_column(Integer, nullable=False)


class RsvpResponse(str, enum.Enum):
    GOING = "going"
    MAYBE = "maybe"
    NOT_GOING = "not_going"


class TeamEvent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "team_events"
    __table_args__ = (
        CheckConstraint("ends_at IS NULL OR ends_at >= starts_at", name="ck_event_ends_after"),
        CheckConstraint("capacity IS NULL OR capacity > 0", name="ck_event_capacity"),
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(300), nullable=True)
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rsvp_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audience: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_team_events_created_by"),
        nullable=True,
    )


class EventRsvp(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "event_rsvps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", "employee_id", name="uq_event_rsvp_once"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("team_events.id", ondelete="CASCADE", name="fk_event_rsvps_event_id"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_event_rsvps_employee_id"),
        nullable=False,
    )
    response: Mapped[RsvpResponse] = mapped_column(
        Enum(RsvpResponse, name="rsvp_response"), nullable=False
    )
