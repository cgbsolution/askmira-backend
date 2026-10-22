"""Payroll runs: cancel a run opened in error

Revision ID: a7d4f2c8e1b3
Revises: f6c9e3a5b7d2
Create Date: 2026-10-22 12:00:00.000000

A draft, failed or pending-review run can be discarded: its payslips go, the
row stays with who cancelled it and why. The one-regular-run-per-month index
stops counting cancelled runs, so the month can be opened again correctly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7d4f2c8e1b3"
down_revision: str | None = "f6c9e3a5b7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payroll_runs", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "payroll_runs",
        sa.Column("cancelled_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_payroll_runs_cancelled_by_user_id",
        "payroll_runs",
        "users",
        ["cancelled_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("payroll_runs", sa.Column("cancel_reason", sa.Text(), nullable=True))
    op.drop_index("uq_payroll_run_regular_period", table_name="payroll_runs")
    op.create_index(
        "uq_payroll_run_regular_period",
        "payroll_runs",
        ["tenant_id", "legal_entity_id", "period_year", "period_month"],
        unique=True,
        postgresql_where=sa.text("run_type = 'REGULAR' AND status <> 'CANCELLED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_payroll_run_regular_period", table_name="payroll_runs")
    op.create_index(
        "uq_payroll_run_regular_period",
        "payroll_runs",
        ["tenant_id", "legal_entity_id", "period_year", "period_month"],
        unique=True,
        postgresql_where=sa.text("run_type = 'REGULAR'"),
    )
    op.drop_column("payroll_runs", "cancel_reason")
    op.drop_constraint("fk_payroll_runs_cancelled_by_user_id", "payroll_runs", type_="foreignkey")
    op.drop_column("payroll_runs", "cancelled_by_user_id")
    op.drop_column("payroll_runs", "cancelled_at")
