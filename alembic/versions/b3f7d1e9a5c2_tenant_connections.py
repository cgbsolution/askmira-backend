"""Tenant connections, payroll journal postings and calendar feed links.

`tenant_connections`: one row per (workspace, kind) — Digio for Aadhaar eSign,
Zoho Books for posting the payroll journal. Non-secret settings in `settings`;
credentials encrypted with the tenant key in `secret_encrypted`, never returned.

`payroll_journal_postings`: an approved run's journal posted straight into the
books, one per (run, books), so a second click cannot post a month twice.

`calendar_feed_tokens`: a person's private .ics subscription link (holidays and
approved leave); only the token's SHA-256 is kept.

Revision ID: b3f7d1e9a5c2
Revises: f9b3d7e1a5c8
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b3f7d1e9a5c2"
down_revision = "f9b3d7e1a5c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("secret_encrypted", sa.Text(), nullable=True),
        sa.Column("account_label", sa.String(255), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("configured_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["configured_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "kind", name="uq_tenant_connection_kind"),
    )
    op.create_index("ix_tenant_connections_tenant_id", "tenant_connections", ["tenant_id"])

    op.create_table(
        "payroll_journal_postings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("target", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("external_number", sa.String(64), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("posted_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["payroll_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["posted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "run_id", "target", name="uq_payroll_journal_posting"),
    )
    op.create_index(
        "ix_payroll_journal_postings_tenant_id", "payroll_journal_postings", ["tenant_id"]
    )

    op.create_table(
        "calendar_feed_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("include_team", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_calendar_feed_token_hash"),
    )
    op.create_index("ix_calendar_feed_tokens_tenant_id", "calendar_feed_tokens", ["tenant_id"])
    op.create_index("ix_calendar_feed_tokens_user_id", "calendar_feed_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_calendar_feed_tokens_user_id", table_name="calendar_feed_tokens")
    op.drop_index("ix_calendar_feed_tokens_tenant_id", table_name="calendar_feed_tokens")
    op.drop_table("calendar_feed_tokens")
    op.drop_index("ix_payroll_journal_postings_tenant_id", table_name="payroll_journal_postings")
    op.drop_table("payroll_journal_postings")
    op.drop_index("ix_tenant_connections_tenant_id", table_name="tenant_connections")
    op.drop_table("tenant_connections")
