"""Document number, remark and dependant; the company document library

`employee_documents` gains `document_number`, `remark` and `dependant_name`
(a document that belongs to one of the employee's dependants), and
`company_documents` holds the company's own policies and handbooks.

Revision ID: c07e5a1a0934
Revises: c07e5a1a0933
Create Date: 2026-09-30 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c07e5a1a0934"
down_revision: str | None = "c07e5a1a0933"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "employee_documents", sa.Column("document_number", sa.String(length=64), nullable=True)
    )
    op.add_column("employee_documents", sa.Column("remark", sa.Text(), nullable=True))
    op.add_column(
        "employee_documents", sa.Column("dependant_name", sa.String(length=128), nullable=True)
    )

    op.create_table(
        "company_documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("document_number", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("file_url", sa.String(length=1024), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("issued_on", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column(
            "is_employee_visible", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("uploaded_by_user_id", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_company_documents_tenant_id", "company_documents", ["tenant_id"])
    op.create_index(
        "ix_company_documents_document_type", "company_documents", ["document_type"]
    )


def downgrade() -> None:
    op.drop_index("ix_company_documents_document_type", table_name="company_documents")
    op.drop_index("ix_company_documents_tenant_id", table_name="company_documents")
    op.drop_table("company_documents")
    op.drop_column("employee_documents", "dependant_name")
    op.drop_column("employee_documents", "remark")
    op.drop_column("employee_documents", "document_number")
