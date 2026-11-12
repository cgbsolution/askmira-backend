"""Platform → Tools: the platform's key per product, and how a product link was made.

Revision ID: a7d3f1c9e5b2
Revises: e1c5a9d3f7b2
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a7d3f1c9e5b2"
down_revision = "e1c5a9d3f7b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_product_keys",
        sa.Column("product", sa.String(32), primary_key=True),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "set_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_platform_product_keys_set_by"),
            nullable=True,
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_ok", sa.Boolean(), nullable=True),
        sa.Column("last_check_message", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("product_links", sa.Column("via", sa.String(16), nullable=True))
    # Links that already hold a secret of their own were made from Connectors.
    op.execute("UPDATE product_links SET via = 'connectors' WHERE secret_encrypted IS NOT NULL")


def downgrade() -> None:
    op.drop_column("product_links", "via")
    op.drop_table("platform_product_keys")
