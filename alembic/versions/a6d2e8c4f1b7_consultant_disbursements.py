"""Disbursement batches for consultant payout runs.

A batch now pays either a payroll run or a consultant payout run: exactly one.

Revision ID: a6d2e8c4f1b7
Revises: f2c9d4b8e1a3
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a6d2e8c4f1b7"
down_revision = "f2c9d4b8e1a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("disbursement_batches", "payroll_run_id", nullable=True)
    op.add_column(
        "disbursement_batches",
        sa.Column("consultant_payout_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_disbursement_batches_consultant_payout_run",
        "disbursement_batches",
        "consultant_payout_runs",
        ["consultant_payout_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_disbursement_batches_consultant_payout_run_id",
        "disbursement_batches",
        ["consultant_payout_run_id"],
    )
    op.create_check_constraint(
        "ck_disbursement_batch_one_run",
        "disbursement_batches",
        "(payroll_run_id IS NULL) <> (consultant_payout_run_id IS NULL)",
    )

    op.add_column(
        "disbursement_lines",
        sa.Column("consultant_payout_line_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_disbursement_lines_consultant_payout_line",
        "disbursement_lines",
        "consultant_payout_lines",
        ["consultant_payout_line_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_disbursement_lines_consultant_payout_line", "disbursement_lines", type_="foreignkey"
    )
    op.drop_column("disbursement_lines", "consultant_payout_line_id")

    # A consultant payout file has no payroll run, so it cannot survive the
    # column going back to NOT NULL.
    op.execute(
        "DELETE FROM disbursement_downloads WHERE batch_id IN "
        "(SELECT id FROM disbursement_batches WHERE consultant_payout_run_id IS NOT NULL)"
    )
    op.execute("DELETE FROM disbursement_batches WHERE consultant_payout_run_id IS NOT NULL")
    op.drop_constraint("ck_disbursement_batch_one_run", "disbursement_batches", type_="check")
    op.drop_index(
        "ix_disbursement_batches_consultant_payout_run_id", table_name="disbursement_batches"
    )
    op.drop_constraint(
        "fk_disbursement_batches_consultant_payout_run",
        "disbursement_batches",
        type_="foreignkey",
    )
    op.drop_column("disbursement_batches", "consultant_payout_run_id")
    op.alter_column("disbursement_batches", "payroll_run_id", nullable=False)
