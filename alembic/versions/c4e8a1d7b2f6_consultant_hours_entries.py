"""Consultant hours entries and their attachments, with no overlapping periods.

An exclusion constraint (needs btree_gist) refuses two non-rejected entries for
one consultant whose inclusive periods share a day.

Revision ID: c4e8a1d7b2f6
Revises: b3d7f1a9c2e5
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c4e8a1d7b2f6"
down_revision = "b3d7f1a9c2e5"
branch_labels = None
depends_on = None

_STATUS = postgresql.ENUM(
    "DRAFT", "SUBMITTED", "APPROVED", "REJECTED", name="consultant_hours_entry_status"
)


def _user(name: str) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    _STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "consultant_hours_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("total_billable_hours", sa.Numeric(7, 2), nullable=False),
        sa.Column("non_billable_hours", sa.Numeric(7, 2), nullable=True),
        sa.Column("days", postgresql.JSONB(), nullable=True),
        sa.Column("customer_reference", sa.String(length=120), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="consultant_hours_entry_status", create_type=False),
            nullable=False,
        ),
        _user("entered_by_user_id"),
        _user("last_edited_by_user_id"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        _user("decided_by_user_id"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_consultant_hours_employee",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_consultant_hours_project",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["entered_by_user_id"],
            ["users.id"],
            name="fk_consultant_hours_entered_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["last_edited_by_user_id"],
            ["users.id"],
            name="fk_consultant_hours_edited_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
            name="fk_consultant_hours_decided_by",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("period_end >= period_start", name="ck_consultant_hours_period_order"),
        sa.CheckConstraint(
            "period_end - period_start <= 30", name="ck_consultant_hours_period_length"
        ),
        sa.CheckConstraint("total_billable_hours > 0", name="ck_consultant_hours_total_positive"),
    )
    op.create_index(
        "ix_consultant_hours_entries_tenant_id", "consultant_hours_entries", ["tenant_id"]
    )
    op.create_index(
        "ix_consultant_hours_entries_employee_id", "consultant_hours_entries", ["employee_id"]
    )
    op.create_index("ix_consultant_hours_entries_status", "consultant_hours_entries", ["status"])
    op.execute(
        "ALTER TABLE consultant_hours_entries ADD CONSTRAINT ex_consultant_hours_no_overlap "
        "EXCLUDE USING gist (tenant_id WITH =, employee_id WITH =, "
        "daterange(period_start, period_end, '[]') WITH &&) WHERE (status <> 'REJECTED')"
    )

    op.create_table(
        "consultant_hours_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        _user("uploaded_by_user_id"),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["consultant_hours_entries.id"],
            name="fk_consultant_hours_attachment_entry",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name="fk_consultant_hours_attachment_by",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_consultant_hours_attachments_tenant_id", "consultant_hours_attachments", ["tenant_id"]
    )
    op.create_index(
        "ix_consultant_hours_attachments_entry_id", "consultant_hours_attachments", ["entry_id"]
    )


def downgrade() -> None:
    op.drop_table("consultant_hours_attachments")
    op.drop_table("consultant_hours_entries")
    _STATUS.drop(op.get_bind(), checkfirst=True)
