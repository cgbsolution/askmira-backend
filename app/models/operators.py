"""Operator access: a payroll bureau working inside a client's workspace (PAY-09).

A grant lives in the **client's** tenant, created by the client's admin. It
names one person at the operator firm — by their own workspace and login — and
an end date. Without a live grant there is no way in; revoking it ends every
session it opened on the next request. The client's audit trail shows each
action the operator took, marked as an operator action.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class OperatorGrant(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One operator's access to this (the client's) workspace."""

    __tablename__ = "operator_grants"
    __table_args__ = (
        Index("ix_operator_grants_operator", "operator_user_id", "revoked_at"),
        CheckConstraint("expires_at > created_at", name="ck_operator_grant_expiry"),
    )

    #: The operator's own login, in the operator firm's workspace.
    operator_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Shown to the client, captured at grant time.
    operator_email: Mapped[str] = mapped_column(String(320), nullable=False)
    operator_firm: Mapped[str] = mapped_column(String(255), nullable=False)
    #: What the operator may do here. "payroll" is the only scope.
    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_entered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
