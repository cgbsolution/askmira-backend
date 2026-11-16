"""Consultant payout lines: TDS larger than the payment is carried forward.

`tds_due` is all the TDS a line owes (its own plus any carried in);
`tds_carried_out` is what it could not deduct. Existing lines deducted all
they owed, so they backfill to tds_due = tds_amount and nothing carried.

Revision ID: b3d7f1a9c2e5
Revises: a6d2e8c4f1b7
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b3d7f1a9c2e5"
down_revision = "a6d2e8c4f1b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("tds_due", "tds_carried_out"):
        op.add_column(
            "consultant_payout_lines",
            sa.Column(name, sa.Numeric(14, 2), nullable=False, server_default="0"),
        )
    op.execute("UPDATE consultant_payout_lines SET tds_due = tds_amount, tds_carried_out = 0")
    for name in ("tds_due", "tds_carried_out"):
        op.alter_column("consultant_payout_lines", name, server_default=None)


def downgrade() -> None:
    op.drop_column("consultant_payout_lines", "tds_carried_out")
    op.drop_column("consultant_payout_lines", "tds_due")
