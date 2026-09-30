"""onboarding: checklists, plans and tasks

Pre-boarding and the joining checklist. A tenant keeps checklists
(`onboarding_templates` with ordered `onboarding_template_tasks`); starting a
person's onboarding copies one into `onboarding_plans` / `onboarding_tasks`
with resolved due dates and assignees.

Revision ID: 0b0a4d000930
Revises: ae8b1d4d874e
Create Date: 2026-09-30 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0b0a4d000930"
down_revision: str | None = "ae8b1d4d874e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created once with checkfirst and dropped explicitly in downgrade(), so the
# migration round-trips. SQLAlchemy stores enum member NAMES, as elsewhere.
_OWNER_TYPE = postgresql.ENUM(
    "NEW_JOINER", "MANAGER", "HR", "IT", "BUDDY", "FINANCE", name="onboarding_owner_type"
)
_TASK_TYPE = postgresql.ENUM(
    "GENERAL",
    "DOCUMENT_UPLOAD",
    "FORM",
    "ACKNOWLEDGE_POLICY",
    "MEETING",
    name="onboarding_task_type",
)
_PLAN_STATUS = postgresql.ENUM(
    "IN_PROGRESS", "COMPLETED", "CANCELLED", name="onboarding_plan_status"
)
_TASK_STATUS = postgresql.ENUM("PENDING", "DONE", "SKIPPED", name="onboarding_task_status")


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (_OWNER_TYPE, _TASK_TYPE, _PLAN_STATUS, _TASK_STATUS):
        enum.create(bind, checkfirst=True)

    # ── Checklists ──────────────────────────────────────────────────────────
    op.create_table(
        "onboarding_templates",
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("department_id", sa.UUID(), nullable=True),
        sa.Column("location_id", sa.UUID(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_onboarding_template_name"),
    )
    op.create_index(
        op.f("ix_onboarding_templates_tenant_id"), "onboarding_templates", ["tenant_id"]
    )

    op.create_table(
        "onboarding_template_tasks",
        sa.Column("template_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_type", _enum("onboarding_owner_type"), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("due_offset_days", sa.Integer(), nullable=False),
        sa.Column("task_type", _enum("onboarding_task_type"), nullable=False),
        sa.Column("document_type", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_mandatory", sa.Boolean(), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(
            ["template_id"], ["onboarding_templates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_onboarding_template_tasks_template_id"),
        "onboarding_template_tasks",
        ["template_id"],
    )
    op.create_index(
        op.f("ix_onboarding_template_tasks_tenant_id"),
        "onboarding_template_tasks",
        ["tenant_id"],
    )

    # ── Plans ───────────────────────────────────────────────────────────────
    op.create_table(
        "onboarding_plans",
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("template_id", sa.UUID(), nullable=True),
        sa.Column("template_name", sa.String(length=160), nullable=False),
        sa.Column("joining_date", sa.Date(), nullable=False),
        sa.Column("buddy_employee_id", sa.UUID(), nullable=True),
        sa.Column("status", _enum("onboarding_plan_status"), nullable=False),
        sa.Column("started_by_user_id", sa.UUID(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        *_base(),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["template_id"], ["onboarding_templates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["buddy_employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_onboarding_plans_tenant_id"), "onboarding_plans", ["tenant_id"])
    op.create_index(
        op.f("ix_onboarding_plans_employee_id"), "onboarding_plans", ["employee_id"]
    )
    op.create_index(
        op.f("ix_onboarding_plans_joining_date"), "onboarding_plans", ["joining_date"]
    )
    op.create_index(op.f("ix_onboarding_plans_status"), "onboarding_plans", ["status"])
    # One live plan per person; a cancelled or completed one does not block a restart.
    op.create_index(
        "uq_onboarding_plan_in_progress",
        "onboarding_plans",
        ["tenant_id", "employee_id"],
        unique=True,
        postgresql_where=sa.text("status = 'IN_PROGRESS'"),
    )

    op.create_table(
        "onboarding_tasks",
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("template_task_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_type", _enum("onboarding_owner_type"), nullable=False),
        sa.Column("assignee_user_id", sa.UUID(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("task_type", _enum("onboarding_task_type"), nullable=False),
        sa.Column("document_type", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_mandatory", sa.Boolean(), nullable=False),
        sa.Column("status", _enum("onboarding_task_status"), nullable=False),
        sa.Column("completed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("overdue_notified_at", sa.DateTime(timezone=True), nullable=True),
        *_base(),
        sa.ForeignKeyConstraint(["plan_id"], ["onboarding_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["template_task_id"], ["onboarding_template_tasks.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["completed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["employee_documents.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_onboarding_tasks_tenant_id"), "onboarding_tasks", ["tenant_id"])
    op.create_index(op.f("ix_onboarding_tasks_plan_id"), "onboarding_tasks", ["plan_id"])
    op.create_index(op.f("ix_onboarding_tasks_due_date"), "onboarding_tasks", ["due_date"])
    op.create_index(op.f("ix_onboarding_tasks_status"), "onboarding_tasks", ["status"])
    op.create_index(
        "ix_onboarding_tasks_assignee_status",
        "onboarding_tasks",
        ["tenant_id", "assignee_user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_onboarding_tasks_assignee_status", table_name="onboarding_tasks")
    op.drop_index(op.f("ix_onboarding_tasks_status"), table_name="onboarding_tasks")
    op.drop_index(op.f("ix_onboarding_tasks_due_date"), table_name="onboarding_tasks")
    op.drop_index(op.f("ix_onboarding_tasks_plan_id"), table_name="onboarding_tasks")
    op.drop_index(op.f("ix_onboarding_tasks_tenant_id"), table_name="onboarding_tasks")
    op.drop_table("onboarding_tasks")

    op.drop_index("uq_onboarding_plan_in_progress", table_name="onboarding_plans")
    op.drop_index(op.f("ix_onboarding_plans_status"), table_name="onboarding_plans")
    op.drop_index(op.f("ix_onboarding_plans_joining_date"), table_name="onboarding_plans")
    op.drop_index(op.f("ix_onboarding_plans_employee_id"), table_name="onboarding_plans")
    op.drop_index(op.f("ix_onboarding_plans_tenant_id"), table_name="onboarding_plans")
    op.drop_table("onboarding_plans")

    op.drop_index(
        op.f("ix_onboarding_template_tasks_tenant_id"), table_name="onboarding_template_tasks"
    )
    op.drop_index(
        op.f("ix_onboarding_template_tasks_template_id"), table_name="onboarding_template_tasks"
    )
    op.drop_table("onboarding_template_tasks")

    op.drop_index(op.f("ix_onboarding_templates_tenant_id"), table_name="onboarding_templates")
    op.drop_table("onboarding_templates")

    bind = op.get_bind()
    for enum in (_TASK_STATUS, _PLAN_STATUS, _TASK_TYPE, _OWNER_TYPE):
        enum.drop(bind, checkfirst=True)
