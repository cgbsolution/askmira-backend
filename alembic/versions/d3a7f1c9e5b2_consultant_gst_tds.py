"""Consultant billing on the employee record: GST applicable, GST amount, fixed TDS.

Read by payroll only for a consultant: the GST amount is paid every month as a
non-taxable payslip line, and the fixed TDS replaces the projected salary tax.

Revision ID: d3a7f1c9e5b2
Revises: c9f1d3b7a2e4
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d3a7f1c9e5b2"
down_revision = "c9f1d3b7a2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("gst_applicable", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("employees", sa.Column("gst_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("employees", sa.Column("fixed_tds_amount", sa.Numeric(14, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("employees", "fixed_tds_amount")
    op.drop_column("employees", "gst_amount")
    op.drop_column("employees", "gst_applicable")
