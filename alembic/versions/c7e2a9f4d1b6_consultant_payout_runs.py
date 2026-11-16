"""Consultant payout runs: runs, one line per consultant, the weeks each pays, TDS openings.

`consultant_payout_weeks.timesheet_id` is unique: a week is in at most one run.

Revision ID: c7e2a9f4d1b6
Revises: b6d1f4a8c3e9
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c7e2a9f4d1b6"
down_revision = "b6d1f4a8c3e9"
branch_labels = None
depends_on = None

_STATUS = postgresql.ENUM("DRAFT", "APPROVED", "PAID", name="consultant_payout_run_status")
_SECTION = postgresql.ENUM(
    "S194C", "S194J_PROFESSIONAL", "S194J_TECHNICAL", name="tds_section", create_type=False
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _money(name: str) -> sa.Column:
    return sa.Column(name, sa.Numeric(14, 2), nullable=False)


def upgrade() -> None:
    _STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "consultant_payout_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("legal_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cut_off", sa.Date(), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="consultant_payout_run_status", create_type=False),
            nullable=False,
        ),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_on", sa.Date(), nullable=True),
        sa.Column("payment_reference", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["legal_entity_id"],
            ["legal_entities.id"],
            name="fk_consultant_payout_runs_entity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_consultant_payout_runs_created_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name="fk_consultant_payout_runs_approved_by",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_consultant_payout_runs_tenant_id", "consultant_payout_runs", ["tenant_id"])
    op.create_index("ix_consultant_payout_runs_pay_date", "consultant_payout_runs", ["pay_date"])
    op.create_index("ix_consultant_payout_runs_status", "consultant_payout_runs", ["status"])

    op.create_table(
        "consultant_payout_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hours", sa.Numeric(8, 2), nullable=False),
        _money("gross"),
        sa.Column("gst_rate", sa.Numeric(5, 2), nullable=False),
        _money("gst_amount"),
        sa.Column("section", _SECTION, nullable=False),
        _money("tds_base"),
        sa.Column("tds_rate", sa.Numeric(6, 3), nullable=False),
        _money("tds_amount"),
        _money("net_payable"),
        sa.Column("tds_note", sa.Text(), nullable=True),
        sa.Column("rule_set_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "problems",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("excluded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("excluded_reason", sa.Text(), nullable=True),
        sa.Column("challan_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["consultant_payout_runs.id"],
            name="fk_consultant_payout_lines_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_consultant_payout_lines_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rule_set_id"],
            ["payroll_rule_sets.id"],
            name="fk_consultant_payout_lines_rule_set",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["challan_id"],
            ["tds_challans.id"],
            name="fk_consultant_payout_lines_challan",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("run_id", "employee_id", name="uq_consultant_payout_line_person"),
    )
    for column in ("tenant_id", "run_id", "employee_id", "challan_id"):
        op.create_index(f"ix_consultant_payout_lines_{column}", "consultant_payout_lines", [column])

    op.create_table(
        "consultant_payout_weeks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timesheet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hours", sa.Numeric(8, 2), nullable=False),
        _money("gross"),
        sa.Column(
            "breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
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

    op.create_table(
        "consultant_tds_openings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fy_start", sa.Date(), nullable=False),
        sa.Column("section", _SECTION, nullable=False),
        _money("fees_paid"),
        _money("taxed_base"),
        _money("tds_deducted"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employees.id"],
            name="fk_consultant_tds_openings_employee",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id", "employee_id", "fy_start", "section", name="uq_consultant_tds_opening"
        ),
    )
    for column in ("tenant_id", "employee_id"):
        op.create_index(f"ix_consultant_tds_openings_{column}", "consultant_tds_openings", [column])


def downgrade() -> None:
    op.drop_table("consultant_tds_openings")
    op.drop_table("consultant_payout_weeks")
    op.drop_table("consultant_payout_lines")
    op.drop_table("consultant_payout_runs")
    _STATUS.drop(op.get_bind(), checkfirst=True)
