"""A person's second factor: an authenticator-app secret and recovery codes.

The secret is encrypted with the tenant key, never stored or logged in the
clear. Recovery codes are stored only as hashes and each works once. A row
with `enabled_at` unset is a set-up in progress: the secret has been shown but
not yet confirmed with a code, so it does not yet guard anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["UserMfa"]


class UserMfa(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "user_mfa"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The last thirty-second step a code was accepted for; anything at or before it is a replay.
    last_used_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: SHA-256 of each unused recovery code.
    recovery_code_hashes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
