"""Disbursement: salaries held out of a file

Revision ID: c4a8e2f6b1d9
Revises: b9e1c4d7f2a6
Create Date: 2026-10-22 20:00:00.000000

One employee with no bank account used to stop the whole salary file. HR can
now hold named salaries out of it, with a reason, and pay them later from a
supplementary batch; the batch records what it held.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4a8e2f6b1d9"
down_revision: str | None = "b9e1c4d7f2a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "disbursement_batches",
        sa.Column(
            "held",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("disbursement_batches", "held")
