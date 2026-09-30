"""HR helpdesk: categories, tickets, their comments and attachments

Revision ID: b31bd0000930
Revises: ae8b1d4d874e
Create Date: 2026-09-30 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b31bd0000930"
down_revision: str | None = "ae8b1d4d874e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created once with checkfirst and dropped explicitly in downgrade(), so the
# migration round-trips. SQLAlchemy stores enum member NAMES, as elsewhere.
_REQUEST_KIND = postgresql.ENUM(
    "GENERAL",
    "LETTER",
    "ID_CARD",
    "ASSET",
    "IT",
    "PAYROLL",
    "POLICY",
    "GRIEVANCE",
    name="helpdesk_request_kind",
)
_PRIORITY = postgresql.ENUM("LOW", "MEDIUM", "HIGH", "URGENT", name="helpdesk_priority")
_STATUS = postgresql.ENUM(
    "OPEN",
    "IN_PROGRESS",
    "WAITING_ON_EMPLOYEE",
    "RESOLVED",
    "CLOSED",
    "CANCELLED",
    name="helpdesk_ticket_status",
)


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
    _REQUEST_KIND.create(bind, checkfirst=True)
    _PRIORITY.create(bind, checkfirst=True)
    _STATUS.create(bind, checkfirst=True)

    op.create_table(
        "helpdesk_categories",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("sla_hours", sa.Integer(), nullable=False),
        sa.Column("is_confidential", sa.Boolean(), nullable=False),
        sa.Column(
            "request_kind",
            postgresql.ENUM(name="helpdesk_request_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        *_base(),
        sa.CheckConstraint("sla_hours > 0", name="ck_helpdesk_category_sla_positive"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_helpdesk_category_name"),
    )
    op.create_index(
        op.f("ix_helpdesk_categories_tenant_id"), "helpdesk_categories", ["tenant_id"]
    )

    op.create_table(
        "helpdesk_sequences",
        sa.Column("last_value", sa.Integer(), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_helpdesk_sequence_tenant"),
    )
    op.create_index(
        op.f("ix_helpdesk_sequences_tenant_id"), "helpdesk_sequences", ["tenant_id"]
    )

    op.create_table(
        "helpdesk_tickets",
        sa.Column("ticket_seq", sa.Integer(), nullable=False),
        sa.Column("ticket_number", sa.String(length=16), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("raised_by_user_id", sa.UUID(), nullable=True),
        sa.Column("category_id", sa.UUID(), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "priority",
            postgresql.ENUM(name="helpdesk_priority", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="helpdesk_ticket_status", create_type=False),
            nullable=False,
        ),
        sa.Column("assignee_user_id", sa.UUID(), nullable=True),
        sa.Column("is_confidential", sa.Boolean(), nullable=False),
        sa.Column("sla_hours", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopened_count", sa.Integer(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("satisfaction_rating", sa.SmallInteger(), nullable=True),
        sa.Column("satisfaction_comment", sa.Text(), nullable=True),
        sa.Column("rated_at", sa.DateTime(timezone=True), nullable=True),
        *_base(),
        sa.CheckConstraint(
            "satisfaction_rating IS NULL OR (satisfaction_rating BETWEEN 1 AND 5)",
            name="ck_helpdesk_ticket_rating_range",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["raised_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["category_id"], ["helpdesk_categories.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "ticket_seq", name="uq_helpdesk_ticket_seq"),
    )
    op.create_index(op.f("ix_helpdesk_tickets_tenant_id"), "helpdesk_tickets", ["tenant_id"])
    op.create_index(
        op.f("ix_helpdesk_tickets_employee_id"), "helpdesk_tickets", ["employee_id"]
    )
    op.create_index(
        op.f("ix_helpdesk_tickets_category_id"), "helpdesk_tickets", ["category_id"]
    )
    op.create_index(
        op.f("ix_helpdesk_tickets_assignee_user_id"), "helpdesk_tickets", ["assignee_user_id"]
    )
    op.create_index(
        "ix_helpdesk_tickets_tenant_status", "helpdesk_tickets", ["tenant_id", "status"]
    )

    op.create_table(
        "helpdesk_comments",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(["ticket_id"], ["helpdesk_tickets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_helpdesk_comments_tenant_id"), "helpdesk_comments", ["tenant_id"])
    op.create_index(op.f("ix_helpdesk_comments_ticket_id"), "helpdesk_comments", ["ticket_id"])

    op.create_table(
        "helpdesk_attachments",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("comment_id", sa.UUID(), nullable=True),
        sa.Column("uploaded_by_user_id", sa.UUID(), nullable=True),
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("file_name", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        *_base(),
        sa.CheckConstraint("size_bytes > 0", name="ck_helpdesk_attachment_size_positive"),
        sa.ForeignKeyConstraint(["ticket_id"], ["helpdesk_tickets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["comment_id"], ["helpdesk_comments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_helpdesk_attachment_storage_key"),
    )
    op.create_index(
        op.f("ix_helpdesk_attachments_tenant_id"), "helpdesk_attachments", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_helpdesk_attachments_ticket_id"), "helpdesk_attachments", ["ticket_id"]
    )
    op.create_index(
        op.f("ix_helpdesk_attachments_comment_id"), "helpdesk_attachments", ["comment_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_helpdesk_attachments_comment_id"), table_name="helpdesk_attachments")
    op.drop_index(op.f("ix_helpdesk_attachments_ticket_id"), table_name="helpdesk_attachments")
    op.drop_index(op.f("ix_helpdesk_attachments_tenant_id"), table_name="helpdesk_attachments")
    op.drop_table("helpdesk_attachments")

    op.drop_index(op.f("ix_helpdesk_comments_ticket_id"), table_name="helpdesk_comments")
    op.drop_index(op.f("ix_helpdesk_comments_tenant_id"), table_name="helpdesk_comments")
    op.drop_table("helpdesk_comments")

    op.drop_index("ix_helpdesk_tickets_tenant_status", table_name="helpdesk_tickets")
    op.drop_index(op.f("ix_helpdesk_tickets_assignee_user_id"), table_name="helpdesk_tickets")
    op.drop_index(op.f("ix_helpdesk_tickets_category_id"), table_name="helpdesk_tickets")
    op.drop_index(op.f("ix_helpdesk_tickets_employee_id"), table_name="helpdesk_tickets")
    op.drop_index(op.f("ix_helpdesk_tickets_tenant_id"), table_name="helpdesk_tickets")
    op.drop_table("helpdesk_tickets")

    op.drop_index(op.f("ix_helpdesk_sequences_tenant_id"), table_name="helpdesk_sequences")
    op.drop_table("helpdesk_sequences")

    op.drop_index(op.f("ix_helpdesk_categories_tenant_id"), table_name="helpdesk_categories")
    op.drop_table("helpdesk_categories")

    bind = op.get_bind()
    _STATUS.drop(bind, checkfirst=True)
    _PRIORITY.drop(bind, checkfirst=True)
    _REQUEST_KIND.drop(bind, checkfirst=True)
