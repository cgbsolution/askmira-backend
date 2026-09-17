"""Culture layer: shoutouts, clubs (FRD 5.3, FS-CUL-01).

Your Voice (anonymous POSH-framed reporting) and pulse surveys are V2 and are
deliberately not modelled here yet — FS-CUL-02 requires that a submitter's
identity is never stored anywhere retrievable by a case handler, which is a
schema decision worth making with the compliance SME rather than guessing now.
"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    pass


class ShoutoutVisibility(str, enum.Enum):
    COMPANY = "company"
    DEPARTMENT = "department"
    PRIVATE = "private"


class Shoutout(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Peer recognition, visible on the home feed (FS-CUL-01).

    `points_awarded` is populated in V2 when the Rewards points engine lands;
    the column exists in V1 so the feed does not need a migration to start
    carrying point awards.
    """

    __tablename__ = "shoutouts"
    __table_args__ = (
        # A peer shoutout has a sender. An award announcement has no sender -
        # it is the company's post, recorded against the award result - and it
        # must never be attributed to whichever HR person clicked "declare".
        CheckConstraint(
            "from_employee_id IS NOT NULL OR award_result_id IS NOT NULL",
            name="ck_shoutout_has_author",
        ),
    )

    #: Null only for an award announcement, which is the company's post.
    from_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=True
    )
    #: Set when this feed post announces an award result (slice 32).
    award_result_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("award_results.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )
    to_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )

    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Which company value this recognises, e.g. "ownership", "craft".
    value_tag: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    visibility: Mapped[ShoutoutVisibility] = mapped_column(
        Enum(ShoutoutVisibility, name="shoutout_visibility"),
        default=ShoutoutVisibility.COMPANY,
        nullable=False,
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    points_awarded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    reactions: Mapped[list[ShoutoutReaction]] = relationship(
        back_populates="shoutout", cascade="all, delete-orphan"
    )


class ShoutoutReaction(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "shoutout_reactions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "shoutout_id", "employee_id", "emoji", name="uq_reaction_once"
        ),
    )

    shoutout_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("shoutouts.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    emoji: Mapped[str] = mapped_column(String(16), nullable=False)

    shoutout: Mapped[Shoutout] = relationship(back_populates="reactions")


class Club(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An interest group, deliberately independent of the org structure.

    Participation is surfaced to HR as an aggregate signal — never as
    "who joined what" — so the club stays a safe social space (FRD 5.3).
    """

    __tablename__ = "clubs"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_clubs_tenant_slug"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    members: Mapped[list[ClubMember]] = relationship(
        back_populates="club", cascade="all, delete-orphan"
    )


class ClubMember(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "club_members"
    __table_args__ = (
        UniqueConstraint("tenant_id", "club_id", "employee_id", name="uq_club_member_once"),
    )

    club_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    # member | organiser
    role: Mapped[str] = mapped_column(String(16), default="member", nullable=False)

    club: Mapped[Club] = relationship(back_populates="members")
