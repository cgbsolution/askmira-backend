"""Product connections: a linked product's address and shared secret, set from the console

Revision ID: e5b8d2f4a6c1
Revises: d1a7c3e9f5b2
Create Date: 2026-10-21 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b8d2f4a6c1"
down_revision: str | None = "d1a7c3e9f5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_connections",
        sa.Column("product", sa.String(length=32), nullable=False),
        sa.Column("api_url", sa.String(length=500), nullable=False),
        sa.Column("app_url", sa.String(length=500), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("connected_by_user_id", sa.UUID(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["connected_by_user_id"],
            ["users.id"],
            name="fk_product_connections_connected_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("product"),
    )


def downgrade() -> None:
    op.drop_table("product_connections")
