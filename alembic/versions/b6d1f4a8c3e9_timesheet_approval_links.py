"""Timesheet approval links: single-use customer approval of a consultant week.

Revision ID: b6d1f4a8c3e9
Revises: a5c9e3f7b1d2
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b6d1f4a8c3e9"
down_revision = "a5c9e3f7b1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "timesheet_approval_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timesheet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("contents_hash", sa.String(length=64), nullable=False),
        sa.Column("sent_to_name", sa.String(length=200), nullable=True),
        sa.Column("sent_to_email", sa.String(length=255), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminders_sent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("decided_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["timesheet_id"],
            ["timesheets.id"],
            name="fk_timesheet_links_timesheet",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="uq_timesheet_approval_links_token_hash"),
    )
    op.create_index(
        "ix_timesheet_approval_links_tenant_id", "timesheet_approval_links", ["tenant_id"]
    )
    op.create_index(
        "ix_timesheet_approval_links_timesheet_id", "timesheet_approval_links", ["timesheet_id"]
    )


def downgrade() -> None:
    op.drop_table("timesheet_approval_links")
