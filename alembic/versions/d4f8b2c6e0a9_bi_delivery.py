"""BI connections deliver: to an S3-compatible bucket, or as a feed link.

Revision ID: d4f8b2c6e0a9
Revises: c2e6a8d4f0b7
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d4f8b2c6e0a9"
down_revision = "c2e6a8d4f0b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE bi_destination_kind ADD VALUE IF NOT EXISTS 'HTTPS_FEED'")
    op.add_column("bi_connections", sa.Column("storage", postgresql.JSONB(), nullable=True))
    op.add_column("bi_connections", sa.Column("secret_encrypted", sa.Text(), nullable=True))
    op.add_column("bi_connections", sa.Column("feed_token_hash", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_bi_connections_feed_token_hash", "bi_connections", ["feed_token_hash"]
    )
    op.add_column(
        "bi_connections",
        sa.Column("schedule", sa.String(16), nullable=False, server_default="manual"),
    )
    op.add_column(
        "bi_connections",
        sa.Column("last_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("bi_connections", sa.Column("last_delivery_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("bi_connections", "last_delivery_error")
    op.drop_column("bi_connections", "last_delivered_at")
    op.drop_column("bi_connections", "schedule")
    op.drop_constraint("uq_bi_connections_feed_token_hash", "bi_connections", type_="unique")
    op.drop_column("bi_connections", "feed_token_hash")
    op.drop_column("bi_connections", "secret_encrypted")
    op.drop_column("bi_connections", "storage")
    # HTTPS_FEED stays on bi_destination_kind: Postgres cannot drop an enum value.
