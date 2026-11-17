"""Consultant payout runs pay approved hours entries, not timesheet weeks.

Drops `consultant_payout_weeks` and creates `consultant_payout_entries`: one row
per hours entry a line pays, `entry_id` unique so an entry is in at most one run.
The feature is unreleased, so no week rows exist to carry over.

Revision ID: e6a3c9d2f4b8
Revises: d5f9b2c8e3a7
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e6a3c9d2f4b8"
down_revision = "d5f9b2c8e3a7"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _breakdown() -> sa.Column:
    return sa.Column(
        "breakdown",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.drop_table("consultant_payout_weeks")
    op.create_table(
        "consultant_payout_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hours", sa.Numeric(8, 2), nullable=False),
        sa.Column("gross", sa.Numeric(14, 2), nullable=False),
        _breakdown(),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["consultant_payout_runs.id"],
            name="fk_consultant_payout_entries_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["line_id"],
            ["consultant_payout_lines.id"],
            name="fk_consultant_payout_entries_line",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["consultant_hours_entries.id"],
            name="fk_consultant_payout_entries_entry",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("entry_id", name="consultant_payout_entries_entry_id_key"),
    )
    for column in ("tenant_id", "run_id", "line_id"):
        op.create_index(
            f"ix_consultant_payout_entries_{column}", "consultant_payout_entries", [column]
        )


def downgrade() -> None:
    op.drop_table("consultant_payout_entries")
    op.create_table(
        "consultant_payout_weeks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timesheet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hours", sa.Numeric(8, 2), nullable=False),
        sa.Column("gross", sa.Numeric(14, 2), nullable=False),
        _breakdown(),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["consultant_payout_runs.id"],
            name="fk_consultant_payout_weeks_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["line_id"],
            ["consultant_payout_lines.id"],
            name="fk_consultant_payout_weeks_line",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["timesheet_id"],
            ["timesheets.id"],
            name="fk_consultant_payout_weeks_timesheet",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("timesheet_id", name="uq_consultant_payout_weeks_timesheet_id"),
    )
    for column in ("tenant_id", "run_id", "line_id"):
        op.create_index(f"ix_consultant_payout_weeks_{column}", "consultant_payout_weeks", [column])
