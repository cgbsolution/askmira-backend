"""payroll run lifecycle (maker-checker, publish, paid), input lines, TDS working,
rule-set creator

Revision ID: c3a9e2f4d801
Revises: b62b0a40ce25
Create Date: 2026-09-16 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3a9e2f4d801"
down_revision: str | None = "b62b0a40ce25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _user_fk(table: str, column: str) -> None:
    op.add_column(table, sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        f"fk_{table}_{column}", table, "users", [column], ["id"], ondelete="SET NULL"
    )


def upgrade() -> None:
    # ── payroll_rule_sets ───────────────────────────────────────────────────
    _user_fk("payroll_rule_sets", "created_by_user_id")

    # ── payroll_runs ────────────────────────────────────────────────────────
    _user_fk("payroll_runs", "computed_by_user_id")
    op.add_column(
        "payroll_runs", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )
    _user_fk("payroll_runs", "published_by_user_id")
    op.add_column("payroll_runs", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    _user_fk("payroll_runs", "paid_by_user_id")
    op.add_column(
        "payroll_runs",
        sa.Column(
            "input_notices",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # ── payslips ────────────────────────────────────────────────────────────
    op.add_column(
        "payslips",
        sa.Column(
            "input_lines",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "payslips",
        sa.Column("tds_working", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "payslips", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("payslips", "published_at")
    op.drop_column("payslips", "tds_working")
    op.drop_column("payslips", "input_lines")

    op.drop_column("payroll_runs", "input_notices")
    for column in ("paid_by_user_id", "published_by_user_id", "computed_by_user_id"):
        op.drop_constraint(f"fk_payroll_runs_{column}", "payroll_runs", type_="foreignkey")
        op.drop_column("payroll_runs", column)
    op.drop_column("payroll_runs", "paid_at")
    op.drop_column("payroll_runs", "published_at")

    op.drop_constraint(
        "fk_payroll_rule_sets_created_by_user_id", "payroll_rule_sets", type_="foreignkey"
    )
    op.drop_column("payroll_rule_sets", "created_by_user_id")
