"""Expense Genie claims paid with salary: the double-pay guard and the hand-back.

Revision ID: e5a9c3d7f1b8
Revises: d4f8b2c6e0a9
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e5a9c3d7f1b8"
down_revision = "d4f8b2c6e0a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "linked_claim_payments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("product", sa.String(32), nullable=False),
        sa.Column("reference", sa.String(128), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mark_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["payroll_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "product", "reference", name="uq_linked_claim_payment"),
    )
    op.create_index("ix_linked_claim_payments_tenant_id", "linked_claim_payments", ["tenant_id"])
    op.create_index("ix_linked_claim_payments_run_id", "linked_claim_payments", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_linked_claim_payments_run_id", table_name="linked_claim_payments")
    op.drop_index("ix_linked_claim_payments_tenant_id", table_name="linked_claim_payments")
    op.drop_table("linked_claim_payments")
