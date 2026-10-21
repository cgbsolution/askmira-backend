"""A workspace's link to one of our separate products (the interview tool, the
expense tool).

The product keeps its own copy of the company and its people; this row says
which of its organisations is this workspace, and how the last people sync went.
While a link is active, the product replaces the matching AskMira module for
that workspace — the portal shows a launcher instead of the built-in pages.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin
from app.models.types import EncryptedText


class ProductLink(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "product_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "product", name="uq_product_links_tenant_product"),
    )

    #: "interview" or "expense" — the keys of app.services.products.catalogue.PRODUCTS.
    product: Mapped[str] = mapped_column(String(32), nullable=False)
    #: "active" or "disabled". A disabled link keeps its external_ref for relinking.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: The product's own id for this company (its organisation id or tenant slug).
    external_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    linked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_product_links_linked_by"),
        nullable=True,
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The product's answer to the last sync: counts, and who it skipped and why.
    last_sync: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: Why the last call to the product failed, when it did.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # A connection the workspace's admin made from Connectors (products/connectors.py):
    # the product's addresses as they were then, and the secret it issued for this
    # workspace alone. Empty for a link made the older way, with the server-wide
    # INTERVIEW_TOOL_… / EXPENSE_TOOL_… settings.
    api_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    app_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    secret_encrypted: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    #: The organisation in the product this workspace is connected to, as it calls it.
    organization_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
