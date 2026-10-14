"""Talent pools: candidates kept for roles that are not open yet.

Keeping somebody on file after their application is a separate purpose under
DPDP, and a candidate agrees to it — or does not — on its own checkbox
(`ConsentPurpose.TALENT_POOL_RETENTION`). So a pool admits only candidates
whose latest word on that purpose is yes, and shows anybody who has since
withdrawn as withdrawn rather than as a live prospect.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["TalentPool", "TalentPoolMember"]


class TalentPool(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A named list — "Backend engineers, Pune", "Campus 2026 silver medallists"."""

    __tablename__ = "talent_pools"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_talent_pool_name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class TalentPoolMember(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One candidate in one pool, with who put them there and why."""

    __tablename__ = "talent_pool_members"
    __table_args__ = (
        UniqueConstraint("pool_id", "candidate_id", name="uq_talent_pool_member"),
    )

    pool_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("talent_pools.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
