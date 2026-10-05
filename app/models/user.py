"""Login identity and role assignment (FRD 5.11, FS-ADM-01)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint, false
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.employee import Employee
    from app.models.tenant import Tenant


class UserRole(str, enum.Enum):
    """Coarse role. Fine-grained per-module access lives in RoleModulePermission.

    - SUPER_ADMIN is ours, not a customer's — it manages the tenants on the
      platform and is the only role that is not scoped to a single tenant.
    - ADMIN runs one company's workspace: settings, users, approvals, money.
    - SUB_ADMIN does the HR work in that workspace but cannot change its
      settings, its users or sign off money.
    - EMPLOYEE is everybody else.

    Managing people is not a role. Anybody with a direct report acts for their
    reporting line whatever their role — see `User.has_reports`.
    """

    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    SUB_ADMIN = "sub_admin"
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
    # Set when an admin issues a temporary password; the portal sends the
    # person to change it before anything else, and a change clears it.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

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

    # Not a column. Whether somebody reports to this login's employee record,
    # worked out from the org chart by `get_current_user` on every request so
    # it can never lag a reorg. False until then, so a User loaded any other
    # way fails closed on manager-only actions.
    has_reports = False

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role.value})>"


class PasswordResetToken(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A single-use "forgot password" link.

    Only a SHA-256 of the token is stored: the database never holds anything
    that could reset a password if it leaked. Used or expired rows stay, as the
    record that a reset was asked for.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
