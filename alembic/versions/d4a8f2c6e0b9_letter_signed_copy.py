"""Letter acknowledgement from the email link: the signed copy it produces.

Revision ID: d4a8f2c6e0b9
Revises: c8e2f4a6b1d3
Create Date: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4a8f2c6e0b9"
down_revision = "c8e2f4a6b1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "letter_acknowledgements",
        sa.Column("signed_copy_storage_key", sa.String(512), nullable=True),
    )
    op.add_column(
        "letter_acknowledgements", sa.Column("signed_copy_hash", sa.String(64), nullable=True)
    )
    op.add_column("letter_acknowledgements", sa.Column("channel", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("letter_acknowledgements", "channel")
    op.drop_column("letter_acknowledgements", "signed_copy_hash")
    op.drop_column("letter_acknowledgements", "signed_copy_storage_key")
