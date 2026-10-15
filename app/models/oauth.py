"""OAuth 2.1 for the MCP server, so AskMira can be added as a connector.

A connector (claude.ai, ChatGPT) registers itself as a public client, sends the
person to AskMira to sign in and approve, and gets tokens that act as that
person, limited to the read scopes they approved and their role allows.

Only hashes of codes and tokens are stored. A client is not tenant-scoped: it
registers before anyone has signed in, and the same connector serves every
workspace; what it receives is always one person's, in one workspace.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class OAuthClient(Base, UUIDMixin, TimestampMixin):
    """A registered connector (RFC 7591 dynamic registration). Public: PKCE, no secret."""

    __tablename__ = "oauth_clients"
    __table_args__ = (UniqueConstraint("client_id", name="uq_oauth_client_id"),)

    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    redirect_uris: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)


class OAuthAuthorizationCode(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A one-time code, bound to its client, redirect, PKCE challenge and person."""

    __tablename__ = "oauth_authorization_codes"
    __table_args__ = (UniqueConstraint("code_hash", name="uq_oauth_code_hash"),)

    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    redirect_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthGrant(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A person's approval of a connector: its current access and refresh tokens.

    Refreshing rotates both; revoking (by the person, or an admin deactivating
    them) ends the grant and both tokens with it.
    """

    __tablename__ = "oauth_grants"
    __table_args__ = (
        UniqueConstraint("access_token_hash", name="uq_oauth_access_token"),
        UniqueConstraint("refresh_token_hash", name="uq_oauth_refresh_token"),
    )

    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
