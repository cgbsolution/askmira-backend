"""Showcase links: a QR code at an exhibition that opens a workspace's Ask Miraa.

The platform's super admin makes a link for an event (Platform → Showcase
links): a label, the workspace whose Ask Miraa it opens, the admin of that
workspace it answers as, the window it works in, an optional stand PIN and a
daily message cap. It can be switched off at any moment; the next request from
any open page is refused.

**What a visitor gets.** Scanning the code opens `/showcase/<code>`: the
workspace's Ask Miraa chat, answering as the chosen admin, over the
workspace's real records, in a conversation of the visitor's own
(`ShowcaseRun`). No portal session reaches the browser: the page can only
chat, and everything it does runs through Ask Miraa's own service (tenant
scoped, audited, offers done only on Confirm).

**Platform-level rows.** Neither table has a `tenant_id`: a link belongs to the
platform and only the super admin reads or changes it; `workspace_id` names
the workspace it opens. A run is read by its own key.

**One QR per workspace, for good.** A workspace has at most one link, and
its code never changes: deleting the link only switches it off
(`deleted_at`), and making a link for the same workspace again brings back
the same code, so a printed QR works again.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.types import EncryptedText

__all__ = ["ShowcaseLink", "ShowcaseRun"]


class ShowcaseLink(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "showcase_links"
    #: One per workspace: its printed QR is its only one.
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_showcase_links_workspace"),)

    label: Mapped[str] = mapped_column(String(120), nullable=False)
    #: sha256 of the code in the URL: how a scan finds its link.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: The code itself, encrypted, so the console can show the URL and QR again.
    code_encrypted: Mapped[str] = mapped_column(EncryptedText(), nullable=False)

    #: The workspace whose Ask Miraa the link opens.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE", name="fk_showcase_links_workspace"),
        nullable=False,
    )
    #: The admin of that workspace the chat answers as: their access, their audit trail.
    acting_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_showcase_links_acting_user"),
        nullable=False,
    )
    #: Typed at the stand before the chat opens. None: the scan alone opens it.
    pin_encrypted: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    #: Messages all of the link's visitors may send in one day (IST).
    daily_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    messages_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    messages_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── When it works ────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Chats started from it, and the last one.
    visits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_showcase_links_created_by"),
        nullable=True,
    )
    #: Deleted from the console: the QR says it is switched off, and the code is
    #: kept so making the workspace's link again revives the printed QR.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ShowcaseRun(Base, UUIDMixin, TimestampMixin):
    """One visitor's chat: their Ask Miraa conversation, reached with their own key."""

    __tablename__ = "showcase_runs"
    __table_args__ = (Index("ix_showcase_runs_link_updated", "link_id", "updated_at"),)

    link_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("showcase_links.id", ondelete="CASCADE", name="fk_showcase_runs_link"),
        nullable=False,
    )
    #: sha256 of the key the visitor's page holds.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The Ask Miraa conversation this visitor's messages go to (none before the first).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "companion_conversations.id",
            ondelete="SET NULL",
            name="fk_showcase_runs_conversation",
        ),
        nullable=True,
    )
    #: Messages this visitor has sent: a run stops taking more at the cap.
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
