"""Connectors: each workspace's own connection to a product, on its product link

Revision ID: f6c9e3a5b7d2
Revises: e5b8d2f4a6c1
Create Date: 2026-10-21 18:30:00.000000

A workspace admin connects a product from Settings → Connectors; the product
issues a secret for that workspace alone, kept (encrypted) on the workspace's
product link with the product's addresses. The server-wide connection made
with a pasted key (product_connections) is gone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6c9e3a5b7d2"
down_revision: str | None = "e5b8d2f4a6c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product_links", sa.Column("api_url", sa.String(length=500), nullable=True))
    op.add_column("product_links", sa.Column("app_url", sa.String(length=500), nullable=True))
    op.add_column("product_links", sa.Column("secret_encrypted", sa.Text(), nullable=True))
    op.add_column(
        "product_links", sa.Column("organization_name", sa.String(length=200), nullable=True)
    )
    op.drop_table("product_connections")


def downgrade() -> None:
    op.create_table(
        "product_connections",
        sa.Column("product", sa.String(length=32), nullable=False),
        sa.Column("api_url", sa.String(length=500), nullable=False),
        sa.Column("app_url", sa.String(length=500), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("connected_by_user_id", sa.UUID(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["connected_by_user_id"],
            ["users.id"],
            name="fk_product_connections_connected_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("product"),
    )
    for column in ("organization_name", "secret_encrypted", "app_url", "api_url"):
        op.drop_column("product_links", column)
