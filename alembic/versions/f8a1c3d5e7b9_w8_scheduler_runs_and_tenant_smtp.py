"""W8 platform: scheduler job runs and tenant SMTP settings

Revision ID: f8a1c3d5e7b9
Revises: b62b0a40ce25
Create Date: 2026-09-16 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f8a1c3d5e7b9"
down_revision: str | None = "b62b0a40ce25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Created once with checkfirst and dropped explicitly on downgrade, so the
# migration round-trips (autogenerate leaves enum types behind).
_RUN_STATUS = postgresql.ENUM("SUCCEEDED", "FAILED", "SKIPPED", name="scheduler_run_status")


def upgrade() -> None:
    bind = op.get_bind()
    _RUN_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "scheduler_job_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("job_key", sa.String(length=96), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="scheduler_run_status", create_type=False),
            nullable=False,
        ),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("triggered_by_user_id", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(["triggered_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduler_run_job_tenant",
        "scheduler_job_runs",
        ["job_key", "tenant_id", "started_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scheduler_job_runs_job_key"), "scheduler_job_runs", ["job_key"], unique=False
    )
    op.create_index(
        op.f("ix_scheduler_job_runs_status"), "scheduler_job_runs", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_scheduler_job_runs_tenant_id"),
        "scheduler_job_runs",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "tenant_smtp_settings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("password_encrypted", sa.Text(), nullable=True),
        sa.Column("use_starttls", sa.Boolean(), nullable=False),
        sa.Column("use_ssl", sa.Boolean(), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("from_name", sa.String(length=128), nullable=True),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=True),
        sa.Column("configured_by_user_id", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(["configured_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_smtp_settings_tenant"),
    )
    op.create_index(
        op.f("ix_tenant_smtp_settings_tenant_id"),
        "tenant_smtp_settings",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_smtp_settings_tenant_id"), table_name="tenant_smtp_settings")
    op.drop_table("tenant_smtp_settings")
    op.drop_index(op.f("ix_scheduler_job_runs_tenant_id"), table_name="scheduler_job_runs")
    op.drop_index(op.f("ix_scheduler_job_runs_status"), table_name="scheduler_job_runs")
    op.drop_index(op.f("ix_scheduler_job_runs_job_key"), table_name="scheduler_job_runs")
    op.drop_index("ix_scheduler_run_job_tenant", table_name="scheduler_job_runs")
    op.drop_table("scheduler_job_runs")
    _RUN_STATUS.drop(op.get_bind(), checkfirst=True)
