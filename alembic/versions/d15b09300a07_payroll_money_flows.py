"""payroll money flows: expense reimbursement, manager stage, proration, F&F payment

No new tables. Additions to existing ones, so money that was approved actually
reaches the person it is owed to, once:

- `expense_claims`: the line manager's stage (`manager_decided_*`), the payroll
  run that paid the claim (`reimbursed_in_run_id`, `reimbursed_at`) and the
  travel-advance settlement it was offset against (`advance_settlement_id`).
- `expense_claim_status` / `travel_request_status`: `PENDING_MANAGER`, the stage
  before finance. Existing `SUBMITTED` rows keep meaning "with finance".
- `advance_settlements.payroll_refund_ref`: the run that paid a balance owed to
  the employee, mirroring `payroll_recovery_ref` for one owed by them.
- `payslips.not_employed_days`: days of the period before joining or after
  leaving, unpaid and shown apart from loss of pay.
- `settlements`: how a full & final payment was made (`paid_on`,
  `payment_reference`, `paid_by_user_id`).

Revision ID: d15b09300a07
Revises: ae8b1d4d874e
Create Date: 2026-09-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d15b09300a07"
down_revision: str | None = "ae8b1d4d874e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # A new enum label cannot be used inside the transaction that adds it, and
    # before PostgreSQL 12 it cannot be added inside a transaction at all.
    # SQLAlchemy stores enum member NAMES, so the label is the upper-case name.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE expense_claim_status ADD VALUE IF NOT EXISTS 'PENDING_MANAGER' "
            "AFTER 'DRAFT'"
        )
        op.execute(
            "ALTER TYPE travel_request_status ADD VALUE IF NOT EXISTS 'PENDING_MANAGER' "
            "AFTER 'DRAFT'"
        )

    # ── expense_claims ──────────────────────────────────────────────────────
    op.add_column("expense_claims", sa.Column("manager_decided_by_user_id", _UUID, nullable=True))
    op.add_column(
        "expense_claims",
        sa.Column("manager_decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("expense_claims", sa.Column("manager_decision_note", sa.Text(), nullable=True))
    op.add_column("expense_claims", sa.Column("reimbursed_in_run_id", _UUID, nullable=True))
    op.add_column(
        "expense_claims", sa.Column("reimbursed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("expense_claims", sa.Column("advance_settlement_id", _UUID, nullable=True))
    op.create_foreign_key(
        "fk_expense_claims_manager_decided_by_user_id_users",
        "expense_claims",
        "users",
        ["manager_decided_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_expense_claims_reimbursed_in_run_id_payroll_runs",
        "expense_claims",
        "payroll_runs",
        ["reimbursed_in_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_expense_claims_advance_settlement_id_advance_settlements",
        "expense_claims",
        "advance_settlements",
        ["advance_settlement_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── advance_settlements ─────────────────────────────────────────────────
    op.add_column(
        "advance_settlements",
        sa.Column("payroll_refund_ref", sa.String(length=120), nullable=True),
    )

    # ── payslips ────────────────────────────────────────────────────────────
    op.add_column(
        "payslips",
        sa.Column(
            "not_employed_days",
            sa.Numeric(precision=5, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    # ── settlements ─────────────────────────────────────────────────────────
    op.add_column("settlements", sa.Column("paid_on", sa.Date(), nullable=True))
    op.add_column(
        "settlements", sa.Column("payment_reference", sa.String(length=120), nullable=True)
    )
    op.add_column("settlements", sa.Column("paid_by_user_id", _UUID, nullable=True))
    op.create_foreign_key(
        "fk_settlements_paid_by_user_id_users",
        "settlements",
        "users",
        ["paid_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_settlements_paid_by_user_id_users", "settlements", type_="foreignkey")
    op.drop_column("settlements", "paid_by_user_id")
    op.drop_column("settlements", "payment_reference")
    op.drop_column("settlements", "paid_on")

    op.drop_column("payslips", "not_employed_days")

    op.drop_column("advance_settlements", "payroll_refund_ref")

    for name in (
        "fk_expense_claims_advance_settlement_id_advance_settlements",
        "fk_expense_claims_reimbursed_in_run_id_payroll_runs",
        "fk_expense_claims_manager_decided_by_user_id_users",
    ):
        op.drop_constraint(name, "expense_claims", type_="foreignkey")
    for column in (
        "advance_settlement_id",
        "reimbursed_at",
        "reimbursed_in_run_id",
        "manager_decision_note",
        "manager_decided_at",
        "manager_decided_by_user_id",
    ):
        op.drop_column("expense_claims", column)

    # Anything waiting on a line manager goes back to finance rather than
    # pointing at a label that no code knows any more. PostgreSQL cannot drop
    # an enum label in place, so 'PENDING_MANAGER' stays on the types; nothing
    # references it after this.
    op.execute(
        "UPDATE expense_claims SET status = 'SUBMITTED' WHERE status = 'PENDING_MANAGER'"
    )
    op.execute(
        "UPDATE travel_requests SET status = 'SUBMITTED' WHERE status = 'PENDING_MANAGER'"
    )
