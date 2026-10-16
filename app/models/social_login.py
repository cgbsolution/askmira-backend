"""A "Continue with Google / Microsoft" sign-in in flight.

**Not tenant-scoped**: nobody knows which workspace the person belongs to until
the provider says who they are. The row holds only the random values the
callback must match — state, nonce and the PKCE verifier — kept server-side
because comparing a value the browser sent against another value the same
browser sent proves nothing. It is used once and expires in minutes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class SocialLoginAttempt(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "social_login_attempts"

    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
