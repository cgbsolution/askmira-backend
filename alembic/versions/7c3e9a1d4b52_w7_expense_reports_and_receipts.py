"""W7: expense reports (claims with line items) and uploaded receipts

Revision ID: 7c3e9a1d4b52
Revises: b62b0a40ce25
Create Date: 2026-09-16 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7c3e9a1d4b52"
down_revision: str | None = "b62b0a40ce25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created once with checkfirst and dropped explicitly in downgrade(), so the
# migration round-trips. SQLAlchemy stores enum member NAMES, as elsewhere.
_REPORT_STATUS = postgresql.ENUM(
    "DRAFT",
    "SUBMITTED",
    "APPROVED",
    "PARTIALLY_APPROVED",
    "REJECTED",
    "CANCELLED",
    name="expense_report_status",
)


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    bind = op.get_bind()
    _REPORT_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "expense_reports",
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("travel_request_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="expense_report_status", create_type=False),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["travel_request_id"], ["travel_requests.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_expense_reports_employee",
        "expense_reports",
        ["tenant_id", "employee_id", "status"],
    )
    op.create_index(op.f("ix_expense_reports_employee_id"), "expense_reports", ["employee_id"])
    op.create_index(op.f("ix_expense_reports_tenant_id"), "expense_reports", ["tenant_id"])

    op.create_table(
        "expense_receipts",
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.UUID(), nullable=True),
        sa.Column("storage_key", sa.String(length=120), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("size_bytes > 0", name="ck_expense_receipt_size_positive"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_expense_receipt_storage_key"),
    )
    op.create_index(op.f("ix_expense_receipts_employee_id"), "expense_receipts", ["employee_id"])
    op.create_index(op.f("ix_expense_receipts_sha256"), "expense_receipts", ["sha256"])
    op.create_index(op.f("ix_expense_receipts_tenant_id"), "expense_receipts", ["tenant_id"])

    op.add_column("expense_claims", sa.Column("report_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_expense_claims_report_id",
        "expense_claims",
        "expense_reports",
        ["report_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_expense_claims_report_id"), "expense_claims", ["report_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_expense_claims_report_id"), table_name="expense_claims")
    op.drop_constraint("fk_expense_claims_report_id", "expense_claims", type_="foreignkey")
    op.drop_column("expense_claims", "report_id")

    op.drop_index(op.f("ix_expense_receipts_tenant_id"), table_name="expense_receipts")
    op.drop_index(op.f("ix_expense_receipts_sha256"), table_name="expense_receipts")
    op.drop_index(op.f("ix_expense_receipts_employee_id"), table_name="expense_receipts")
    op.drop_table("expense_receipts")

    op.drop_index(op.f("ix_expense_reports_tenant_id"), table_name="expense_reports")
    op.drop_index(op.f("ix_expense_reports_employee_id"), table_name="expense_reports")
    op.drop_index("ix_expense_reports_employee", table_name="expense_reports")
    op.drop_table("expense_reports")

    _REPORT_STATUS.drop(op.get_bind(), checkfirst=True)
