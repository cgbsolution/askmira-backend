"""Saleinx and ProcureFlow connectors, order wins, prospective P&L

Revision ID: a1c5e9b3d7f2
Revises: f7c3a9e1d5b2
Create Date: 2026-11-03 14:00:00.000000

- `product_links.member_roles`: for Saleinx and ProcureFlow, whose roles are
  their own, each linked person's role there as the product said at the last
  people sync.
- `account_forecasts` / `account_forecast_lines`: the prospective P&L an order
  is expected to make, month by month, beside the actual entries (which still
  refuse future months). One live forecast per order reference.
- `companion_runs`: a multi-step job Ask Mira carried out across products
  (an order win), step by step, so a failure undoes what came before it. One
  live run per business key (the PO number), so the same order is never done
  twice.
- `companion_messages.client_ref`: a message the browser sends twice is
  answered once.

Additive only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1c5e9b3d7f2"
down_revision: str | None = "f7c3a9e1d5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
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
    op.add_column(
        "product_links",
        sa.Column("member_roles", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "companion_messages", sa.Column("client_ref", sa.String(length=64), nullable=True)
    )
    op.create_index(
        "uq_companion_messages_client_ref",
        "companion_messages",
        ["user_id", "client_ref"],
        unique=True,
        postgresql_where=sa.text("client_ref IS NOT NULL"),
    )

    op.create_table(
        "account_forecasts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("reference", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=120), nullable=True),
        sa.Column("start_month", sa.Date(), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("order_value", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_stamps(),
        sa.CheckConstraint("status IN ('active', 'withdrawn')", name="ck_account_forecast_status"),
        sa.CheckConstraint("months BETWEEN 1 AND 120", name="ck_account_forecast_months"),
        sa.CheckConstraint("extract(day from start_month) = 1", name="ck_account_forecast_start"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["client_accounts.id"],
            name="fk_account_forecasts_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_account_forecasts_creator",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_account_forecasts_tenant_id", "account_forecasts", ["tenant_id"])
    op.create_index(
        "ix_account_forecasts_account",
        "account_forecasts",
        ["tenant_id", "account_id", "start_month"],
    )
    op.create_index(
        "uq_account_forecasts_live_reference",
        "account_forecasts",
        ["tenant_id", "reference"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "account_forecast_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("forecast_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("period_year", sa.Integer(), nullable=False),
        sa.Column("period_month", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        *_stamps(),
        sa.CheckConstraint(
            "kind IN ('billing', 'people_cost', 'recruitment', 'other_cost')",
            name="ck_account_forecast_line_kind",
        ),
        sa.CheckConstraint("period_month BETWEEN 1 AND 12", name="ck_account_forecast_line_month"),
        sa.CheckConstraint("amount > 0", name="ck_account_forecast_line_amount"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["forecast_id"],
            ["account_forecasts.id"],
            name="fk_account_forecast_lines_forecast",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_account_forecast_lines_tenant_id", "account_forecast_lines", ["tenant_id"])
    op.create_index(
        "ix_account_forecast_lines_period",
        "account_forecast_lines",
        ["tenant_id", "forecast_id", "period_year", "period_month"],
    )

    run_status = postgresql.ENUM(
        "RUNNING", "DONE", "UNDONE", "STUCK", name="companion_run_status", create_type=False
    )
    run_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "companion_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("business_key", sa.String(length=160), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", run_status, nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_companion_runs_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["companion_actions.id"],
            name="fk_companion_runs_action",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companion_runs_tenant_id", "companion_runs", ["tenant_id"])
    op.create_index("ix_companion_runs_user", "companion_runs", ["user_id", "created_at"])
    op.create_index(
        "uq_companion_runs_live_key",
        "companion_runs",
        ["tenant_id", "business_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('RUNNING', 'DONE', 'STUCK')"),
    )


def downgrade() -> None:
    op.drop_index("uq_companion_runs_live_key", table_name="companion_runs")
    op.drop_index("ix_companion_runs_user", table_name="companion_runs")
    op.drop_index("ix_companion_runs_tenant_id", table_name="companion_runs")
    op.drop_table("companion_runs")
    postgresql.ENUM(name="companion_run_status").drop(op.get_bind(), checkfirst=True)
    op.drop_index("ix_account_forecast_lines_period", table_name="account_forecast_lines")
    op.drop_index("ix_account_forecast_lines_tenant_id", table_name="account_forecast_lines")
    op.drop_table("account_forecast_lines")
    op.drop_index("uq_account_forecasts_live_reference", table_name="account_forecasts")
    op.drop_index("ix_account_forecasts_account", table_name="account_forecasts")
    op.drop_index("ix_account_forecasts_tenant_id", table_name="account_forecasts")
    op.drop_table("account_forecasts")
    op.drop_index("uq_companion_messages_client_ref", table_name="companion_messages")
    op.drop_column("companion_messages", "client_ref")
    op.drop_column("product_links", "member_roles")
