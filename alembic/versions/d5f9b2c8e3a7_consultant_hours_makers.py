"""Every person who touched a consultant hours entry, so none of them can approve it.

Revision ID: d5f9b2c8e3a7
Revises: c4e8a1d7b2f6
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d5f9b2c8e3a7"
down_revision = "c4e8a1d7b2f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "consultant_hours_entries",
        sa.Column(
            "maker_user_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("consultant_hours_entries", "maker_user_ids")
