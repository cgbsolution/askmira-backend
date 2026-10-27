"""Demo requests from the marketing website

Revision ID: b7d3f9a1c5e2
Revises: a6c2e8f4b1d3
Create Date: 2026-10-27 10:00:00.000000

The website's "Book a demo" form posts here. Platform-level: a demo request
belongs to no workspace, and only the platform's super admin reads it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7d3f9a1c5e2"
down_revision: str | None = "a6c2e8f4b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = postgresql.ENUM(
    "NEW", "CONTACTED", "QUALIFIED", "CLOSED", name="demo_request_status", create_type=False
)


def upgrade() -> None:
    STATUS.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "demo_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.String(length=100), nullable=False),
        sa.Column("work_email", sa.String(length=254), nullable=False),
        sa.Column("company", sa.String(length=120), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("company_size", sa.String(length=20), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=True),
        sa.Column(
            "interests", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("topic", sa.String(length=20), nullable=False, server_default="demo"),
        sa.Column("source_path", sa.String(length=200), nullable=True),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", STATUS, nullable=False, server_default="NEW"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "handled_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_demo_requests_handled_by"),
            nullable=True,
        ),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_demo_requests_work_email", "demo_requests", ["work_email"])
    op.create_index("ix_demo_requests_status_created", "demo_requests", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_demo_requests_status_created", table_name="demo_requests")
    op.drop_index("ix_demo_requests_work_email", table_name="demo_requests")
    op.drop_table("demo_requests")
    STATUS.drop(op.get_bind(), checkfirst=True)
