"""Fixed-monthly consultants: a pay basis on the record and a monthly fee history.

Revision ID: f7b2d4e8a1c3
Revises: e6a3c9d2f4b8
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f7b2d4e8a1c3"
down_revision = "e6a3c9d2f4b8"
branch_labels = None
depends_on = None

_pay_basis = postgresql.ENUM(
    "HOURLY", "FIXED_MONTHLY", name="consultant_pay_basis", create_type=False
)


def upgrade() -> None:
    _pay_basis.create(op.get_bind(), checkfirst=True)
    op.add_column("employees", sa.Column("pay_basis", _pay_basis, nullable=True))
    op.create_table(
        "consultant_monthly_fees",
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
        sa.CheckConstraint("amount > 0", name="ck_consultant_monthly_fee_positive"),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_consultant_monthly_fees_employee",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "employee_id", "effective_from", name="uq_consultant_monthly_fee_from"
        ),
    )
    op.create_index(
        op.f("ix_consultant_monthly_fees_employee_id"),
        "consultant_monthly_fees",
        ["employee_id"],
    )
    op.create_index(
        op.f("ix_consultant_monthly_fees_tenant_id"), "consultant_monthly_fees", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_consultant_monthly_fees_tenant_id"), table_name="consultant_monthly_fees"
    )
    op.drop_index(
        op.f("ix_consultant_monthly_fees_employee_id"), table_name="consultant_monthly_fees"
    )
    op.drop_table("consultant_monthly_fees")
    op.drop_column("employees", "pay_basis")
    sa.Enum(name="consultant_pay_basis").drop(op.get_bind(), checkfirst=True)
