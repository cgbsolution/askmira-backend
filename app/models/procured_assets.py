"""Equipment delivered through ProcureFlow and added to an asset register from AskMiraa.

One row per unit added: the ProcureFlow goods-receipt line and the unit's number
(`source_ref`, unique per workspace), which register it went to (AskMiraa's own,
or CGB Helpdesk's when that is linked) and the asset it became there. So a
delivery shows as added, and the same unit is never added twice.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ProcuredAsset(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "procured_assets"
    __table_args__ = (UniqueConstraint("tenant_id", "source_ref", name="uq_procured_asset_source"),)

    #: "<receipt line ref>:<unit number>".
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    #: "askmira" or "helpdesk".
    register: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The asset's id in that register.
    asset_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_code: Mapped[str] = mapped_column(String(64), nullable=False)
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
