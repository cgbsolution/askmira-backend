"""Ask Mira in Slack and Teams: the switch, and Slack's signing secret.

Revision ID: c8e2f4a6b1d3
Revises: b3f7d1e9a5c2
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c8e2f4a6b1d3"
down_revision = "b3f7d1e9a5c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_chat_apps",
        sa.Column("ask_mira", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "tenant_chat_apps", sa.Column("signing_secret_encrypted", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tenant_chat_apps", "signing_secret_encrypted")
    op.drop_column("tenant_chat_apps", "ask_mira")
