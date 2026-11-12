"""The platform's own key for each of our products (Platform → Tools).

The platform's super admin sets it once per product: the same value is set on
the product as ASKMIRA_PLATFORM_SECRET. With it AskMiraa may ask the product to
create a new workspace's organisation and its admin, or to attach an
organisation the product already has, and gets back a secret for that one
workspace (kept on its ProductLink, exactly like a Connectors connection).

The key never reaches a workspace's data: every later call for a workspace —
people sync, a launch, an Ask Miraa question — is signed with that workspace's
own secret. No tenant column: the key belongs to the platform, not to a
workspace. Rows are read by primary key only (`session.get`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.types import EncryptedText


class PlatformProductKey(Base, TimestampMixin):
    __tablename__ = "platform_product_keys"

    #: A key of app.services.products.catalogue.PRODUCTS.
    product: Mapped[str] = mapped_column(String(32), primary_key=True)
    secret_encrypted: Mapped[str] = mapped_column(EncryptedText(), nullable=False)
    set_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_platform_product_keys_set_by"),
        nullable=True,
    )
    #: The last "Check" against the product: when, whether it took this key, what it said.
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_check_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
