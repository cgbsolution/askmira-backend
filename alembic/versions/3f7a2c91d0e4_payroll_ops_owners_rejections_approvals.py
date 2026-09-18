"""payroll ops: clearance owners, loan rejection, arrear second approval

Workstream W3. Three small additions to existing tables, no new tables:

- `clearance_templates` / `clearance_items`: `owner_role`, `owner_user_id` —
  who is expected to sign each clearance item off.
- `loan_accounts.decision_note`, and the `REJECTED` loan status.
- `arrear_batches`: `computed_by_user_id` (maker), and the second approval for a
  batch above the threshold (`additional_approved_by_user_id`,
  `additional_approved_at`).

Revision ID: 3f7a2c91d0e4
Revises: b62b0a40ce25
Create Date: 2026-09-16 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3f7a2c91d0e4"
down_revision: str | None = "b62b0a40ce25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A new enum label cannot be used inside the transaction that adds it, and
    # before PostgreSQL 12 it cannot be added inside a transaction at all.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE loan_status ADD VALUE IF NOT EXISTS 'REJECTED' AFTER 'APPROVED'")

    op.add_column("loan_accounts", sa.Column("decision_note", sa.Text(), nullable=True))

    for table in ("clearance_templates", "clearance_items"):
        op.add_column(
            table,
            sa.Column("owner_role", sa.String(length=32), server_default="hr", nullable=False),
        )
        op.add_column(
            table, sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            f"fk_{table}_owner_user_id_users",
            table,
            "users",
            ["owner_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    for column in ("computed_by_user_id", "additional_approved_by_user_id"):
        op.add_column(
            "arrear_batches", sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            f"fk_arrear_batches_{column}_users",
            "arrear_batches",
            "users",
            [column],
            ["id"],
            ondelete="SET NULL",
        )
    op.add_column(
        "arrear_batches",
        sa.Column("additional_approved_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("arrear_batches", "additional_approved_at")
    for column in ("additional_approved_by_user_id", "computed_by_user_id"):
        op.drop_constraint(
            f"fk_arrear_batches_{column}_users", "arrear_batches", type_="foreignkey"
        )
        op.drop_column("arrear_batches", column)

    for table in ("clearance_items", "clearance_templates"):
        op.drop_constraint(f"fk_{table}_owner_user_id_users", table, type_="foreignkey")
        op.drop_column(table, "owner_user_id")
        op.drop_column(table, "owner_role")

    # Rejected loans are closed rather than left pointing at a label that is
    # about to disappear. PostgreSQL cannot drop an enum label in place, so the
    # 'REJECTED' label itself stays on the type; nothing references it after this.
    op.execute(
        "UPDATE loan_accounts SET status = 'CLOSED', closed_at = COALESCE(closed_at, now()) "
        "WHERE status = 'REJECTED'"
    )
    op.drop_column("loan_accounts", "decision_note")
