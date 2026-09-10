"""Audit log and per-module RBAC (FRD 5.11, 6, FS-ADM-01, FS-AIT-01).

The audit log is insert-only. There is no update or delete path for it in the
service layer, and none should be added: FRD 6 requires an immutable log for
statutory-impacting actions, AI-assisted actions and Your Voice case handling.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class AuditAction(str, enum.Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    APPROVE = "approve"
    REJECT = "reject"
    LOGIN = "login"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    EXPORT = "export"
    # Reading restricted data without downloading it. Distinct from EXPORT
    # because "who looked at payroll" and "who took a copy of payroll" are
    # different questions, and a report showing 500 salaries on screen is the
    # first without being the second.
    SENSITIVE_READ = "sensitive_read"
    PERMISSION_CHANGE = "permission_change"
    # Every AI Companion / MCP action lands here (FS-AIT-01).
    AI_ACTION = "ai_action"
    # A statutory rule version being created or approved.
    RULE_CHANGE = "rule_change"


class AuditLog(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "audit_logs"

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # "user" | "system" | "ai_companion" | "mcp_agent" — an AI action must be
    # distinguishable from a human one in the log, not inferred.
    actor_type: Mapped[str] = mapped_column(String(32), default="user", nullable=False)

    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name="audit_action"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    # Field-level before/after. Sensitive values (password hashes, bank
    # details) are redacted by the audit service before they reach here.
    changes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # For AI actions: model, prompt id, token counts, and whether a human
    # approved the outcome.
    ai_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class RoleModulePermission(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Per-module access, configurable per tenant (FS-ADM-01).

    The coarse `User.role` decides what a user is; this table decides what that
    role may do in each module. A user can never grant a permission to their
    own role — that check lives in the admin service, and every change here is
    audit-logged.
    """

    __tablename__ = "role_module_permissions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "role", "module", name="uq_role_module_once"),
    )

    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # core_hr | leave | payroll | culture | hiring | analytics | admin | benefits
    module: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    can_view: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_create: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_delete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_approve: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_export: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Field-level restrictions for sensitive payroll/compliance data (FRD 6),
    # e.g. {"deny": ["salary", "bank_account_number"]}.
    field_restrictions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
