"""Slack and Microsoft Teams as notification channels.

Revision ID: c2e6a8d4f0b7
Revises: b9d3f5a7c1e4
Create Date: 2026-11-05

- `notification_channel` gains SLACK and TEAMS.
- `tenant_chat_apps`: a workspace's own Slack app or Teams bot (secret encrypted).
- `chat_recipients`: where each person's chat messages go, once found by email.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c2e6a8d4f0b7"
down_revision = "b9d3f5a7c1e4"
branch_labels = None
depends_on = None

CHANNEL = postgresql.ENUM(name="notification_channel", create_type=False)


def upgrade() -> None:
    # ADD VALUE cannot be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE notification_channel ADD VALUE IF NOT EXISTS 'SLACK'")
        op.execute("ALTER TYPE notification_channel ADD VALUE IF NOT EXISTS 'TEAMS'")

    events = postgresql.ENUM("APPROVALS", "ALL", name="chat_events")
    events.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tenant_chat_apps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("provider", CHANNEL, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "events",
            postgresql.ENUM(name="chat_events", create_type=False),
            nullable=False,
            server_default="APPROVALS",
        ),
        sa.Column("secret_encrypted", sa.Text(), nullable=True),
        sa.Column("app_id", sa.String(64), nullable=True),
        sa.Column("directory_tenant_id", sa.String(64), nullable=True),
        sa.Column("connected_as", sa.String(255), nullable=True),
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
        sa.UniqueConstraint("tenant_id", "provider", name="uq_tenant_chat_app"),
    )
    op.create_index("ix_tenant_chat_apps_tenant_id", "tenant_chat_apps", ["tenant_id"])

    op.create_table(
        "chat_recipients",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("provider", CHANNEL, nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("address", sa.String(512), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "provider", "user_id", name="uq_chat_recipient"),
    )
    op.create_index("ix_chat_recipients_tenant_id", "chat_recipients", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_recipients_tenant_id", table_name="chat_recipients")
    op.drop_table("chat_recipients")
    op.drop_index("ix_tenant_chat_apps_tenant_id", table_name="tenant_chat_apps")
    op.drop_table("tenant_chat_apps")
    postgresql.ENUM(name="chat_events").drop(op.get_bind(), checkfirst=True)
    # Postgres cannot drop an enum value; SLACK and TEAMS stay on notification_channel.
