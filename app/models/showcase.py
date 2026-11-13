"""Showcase links: a QR code at an exhibition that opens the AskMiraa story.

The platform's super admin makes a link for an event (Platform → Showcase
links): a label, the workspace whose name and logo the story is told under,
the order the story runs (client, role, headcount, order value, team budget,
months), and the window it works in. It can be switched off at any moment;
the next request from any open page is refused.

**What a visitor gets.** Scanning the code opens `/showcase/<code>`: the order
win → hiring → laptops → cash story with Ask Miraa, run in a sandbox of its
own (`ShowcaseRun`). It is never a sign-in: a printed code is public, so it
reaches no workspace's records, people or settings, and nothing it does leaves
AskMiraa.

**Platform-level, not tenant data.** No `tenant_id` column on either table:
a link belongs to the platform, and `brand_tenant_id` only names whose brand
(name and logo) the story wears. Only the super admin reads or changes links; a run is read
by its own key.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.types import EncryptedText

__all__ = ["ShowcaseLink", "ShowcaseRun"]


class ShowcaseLink(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "showcase_links"

    label: Mapped[str] = mapped_column(String(120), nullable=False)
    #: sha256 of the code in the URL: how a scan finds its link.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: The code itself, encrypted, so the console can show the URL and QR again.
    code_encrypted: Mapped[str] = mapped_column(EncryptedText(), nullable=False)
    #: Whose name and logo the story is told under. Nothing else of it is read.
    brand_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL", name="fk_showcase_links_brand_tenant"),
        nullable=True,
    )

    # ── The order the story runs ─────────────────────────────────────────────
    client_name: Mapped[str] = mapped_column(String(80), nullable=False)
    role_title: Mapped[str] = mapped_column(String(60), nullable=False)
    headcount: Mapped[int] = mapped_column(Integer, nullable=False)
    months: Mapped[int] = mapped_column(Integer, nullable=False)
    order_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    #: What the team may cost over the contract: the order's margin floor follows from it.
    team_budget: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    #: One laptop at the lowest quote.
    laptop_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    #: Days after each invoice that the client pays: what puts cash below zero at the start.
    payment_days: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── When it works ────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Stories started from it, and the last one.
    visits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_showcase_links_created_by"),
        nullable=True,
    )


class ShowcaseRun(Base, UUIDMixin, TimestampMixin):
    """One visitor's story: its own sandbox, read and moved on with its own key."""

    __tablename__ = "showcase_runs"
    __table_args__ = (Index("ix_showcase_runs_link_updated", "link_id", "updated_at"),)

    link_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("showcase_links.id", ondelete="CASCADE", name="fk_showcase_runs_link"),
        nullable=False,
    )
    #: sha256 of the key the visitor's page holds.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The story so far (app.services.showcase.story): sample figures only.
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Messages the visitor has sent: a run stops taking more at the cap.
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
