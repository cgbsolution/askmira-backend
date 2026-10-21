"""Narrowing an HR operator: their rights by area, and the people they cover.

`RoleModulePermission` (in `models/audit.py`) narrows a role; `UserAreaRights`
narrows one person and wins over their role's row. `AdminScope` limits whose
records a sub-admin acts on — some departments, some locations — and is
enforced on every ORM query they make, not screen by screen.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

__all__ = ["AdminScope", "UserAreaRights"]


class UserAreaRights(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "user_area_rights"
    __table_args__ = (UniqueConstraint("user_id", "area", name="uq_user_area_rights"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    area: Mapped[str] = mapped_column(String(32), nullable=False)
    can_view: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_create: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_delete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_approve: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_export: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AdminScope(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The departments and locations a sub-admin covers. Either list may be empty."""

    __tablename__ = "admin_scopes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    department_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    location_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
