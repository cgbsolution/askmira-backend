"""Drop employees.fixed_tds_amount: consultant TDS is worked out per payout line.

`gst_amount` stays as a hint for HR when setting the GST rate.

Revision ID: e4b8c2a7d9f1
Revises: c7e2a9f4d1b6
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e4b8c2a7d9f1"
down_revision = "c7e2a9f4d1b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("employees", "fixed_tds_amount")


def downgrade() -> None:
    op.add_column("employees", sa.Column("fixed_tds_amount", sa.Numeric(14, 2), nullable=True))
