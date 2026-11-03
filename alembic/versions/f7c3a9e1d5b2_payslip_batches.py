"""Payslips downloaded or emailed in bulk

Revision ID: f7c3a9e1d5b2
Revises: e5b9d2f7a1c3
Create Date: 2026-11-02 18:00:00.000000

- `payslip_batches`: HR asks for the payslips of chosen people over the last 1,
  3, 6 or 12 months, as one download or as an email to each person. The work
  runs in the background; the row carries its progress, each person's outcome
  and, for a download, the prepared file (kept for a day).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f7c3a9e1d5b2"
down_revision: str | None = "e5b9d2f7a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payslip_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("employee_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("send_to", sa.String(length=16), nullable=True),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total_people", sa.Integer(), nullable=False),
        sa.Column("total_payslips", sa.Integer(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("succeeded", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("file_key", sa.String(length=255), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("file_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payslip_batches_tenant_id", "payslip_batches", ["tenant_id"])
    op.create_index(
        "ix_payslip_batches_tenant_created", "payslip_batches", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_payslip_batches_tenant_created", table_name="payslip_batches")
    op.drop_index("ix_payslip_batches_tenant_id", table_name="payslip_batches")
    op.drop_table("payslip_batches")
