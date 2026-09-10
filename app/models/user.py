"""Login identity and role assignment (FRD 5.11, FS-ADM-01)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.employee import Employee
    from app.models.tenant import Tenant


class UserRole(str, enum.Enum):
    """Coarse role. Fine-grained per-module access lives in RoleModulePermission.

    PLATFORM_ADMIN is ours, not a customer's — it is the only role that is not
    scoped to a single tenant.
    """

    PLATFORM_ADMIN = "platform_admin"
    TENANT_ADMIN = "tenant_admin"
    HR_ADMIN = "hr_admin"
    HR_OPS = "hr_ops"
    MANAGER = "manager"
    EMPLOYEE = "employee"


class User(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    # Argon2id. Never logged, never serialised — no schema exposes this field.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.EMPLOYEE, nullable=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bumped on password change and forced logout; refresh tokens issued before
    # this instant are rejected.
    tokens_valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Not every user is an employee (an external auditor or an implementation
    # consultant has a login but no employee record), so this is optional.
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    employee: Mapped[Employee | None] = relationship(
        back_populates="user", foreign_keys=[employee_id]
    )

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role.value})>"
