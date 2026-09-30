"""Leave depth: balance ledger, comp-off, optional holidays, half days, sandwich, gender

Revision ID: 1ea7e0000930
Revises: ae8b1d4d874e
Create Date: 2026-09-30 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1ea7e0000930"
down_revision: str | None = "ae8b1d4d874e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Created once with checkfirst and dropped explicitly on downgrade, so the
# migration round-trips (autogenerate leaves enum types behind).
_COMP_OFF_STATUS = postgresql.ENUM(
    "PENDING", "APPROVED", "REJECTED", "CANCELLED", name="comp_off_status"
)


def _timestamps() -> list[sa.Column]:
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
    bind = op.get_bind()
    _COMP_OFF_STATUS.create(bind, checkfirst=True)

    # ── Columns on existing tables ──────────────────────────────────────────
    op.add_column(
        "leave_types",
        sa.Column("is_comp_off", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "leave_policies",
        sa.Column(
            "sandwich_weekends_holidays",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "leave_policies",
        sa.Column("applies_to_gender", sa.String(length=8), nullable=True),
    )
    op.create_check_constraint(
        "ck_leave_policies_applies_to_gender",
        "leave_policies",
        "applies_to_gender IS NULL OR applies_to_gender IN ('female', 'male')",
    )
    op.add_column(
        "leave_balances",
        sa.Column(
            "adjusted",
            sa.Numeric(precision=6, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "leave_requests",
        sa.Column("half_day_session", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_leave_requests_half_day_session",
        "leave_requests",
        "half_day_session IS NULL OR half_day_session IN ('first_half', 'second_half')",
    )

    # ── The balance ledger ──────────────────────────────────────────────────
    op.create_table(
        "leave_balance_adjustments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("leave_type_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("days", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["leave_type_id"], ["leave_types.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_leave_balance_adjustments_tenant_id"),
        "leave_balance_adjustments",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_leave_adjustments_slot",
        "leave_balance_adjustments",
        ["tenant_id", "employee_id", "leave_type_id", "year"],
        unique=False,
    )

    # ── Optional holidays chosen ────────────────────────────────────────────
    op.create_table(
        "optional_holiday_selections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("holiday_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("selected_by_user_id", sa.UUID(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["holiday_id"], ["holidays.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["selected_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "employee_id", "holiday_id", name="uq_optional_holiday_selection"
        ),
    )
    op.create_index(
        op.f("ix_optional_holiday_selections_tenant_id"),
        "optional_holiday_selections",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_optional_holiday_selections_year"),
        "optional_holiday_selections",
        ["year"],
        unique=False,
    )

    # ── Comp-off claims ─────────────────────────────────────────────────────
    op.create_table(
        "comp_off_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("worked_date", sa.Date(), nullable=False),
        sa.Column("days", sa.Numeric(precision=3, scale=1), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="comp_off_status", create_type=False),
            nullable=False,
        ),
        sa.Column("approver_user_id", sa.UUID(), nullable=True),
        sa.Column("approver_name", sa.String(length=255), nullable=False),
        sa.Column("decided_by_user_id", sa.UUID(), nullable=True),
        sa.Column("decided_by_name", sa.String(length=255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("leave_type_id", sa.UUID(), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approver_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["leave_type_id"], ["leave_types.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_comp_off_requests_tenant_id"), "comp_off_requests", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_comp_off_requests_worked_date"),
        "comp_off_requests",
        ["worked_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_comp_off_requests_status"), "comp_off_requests", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_comp_off_requests_status"), table_name="comp_off_requests")
    op.drop_index(op.f("ix_comp_off_requests_worked_date"), table_name="comp_off_requests")
    op.drop_index(op.f("ix_comp_off_requests_tenant_id"), table_name="comp_off_requests")
    op.drop_table("comp_off_requests")

    op.drop_index(
        op.f("ix_optional_holiday_selections_year"), table_name="optional_holiday_selections"
    )
    op.drop_index(
        op.f("ix_optional_holiday_selections_tenant_id"),
        table_name="optional_holiday_selections",
    )
    op.drop_table("optional_holiday_selections")

    op.drop_index("ix_leave_adjustments_slot", table_name="leave_balance_adjustments")
    op.drop_index(
        op.f("ix_leave_balance_adjustments_tenant_id"), table_name="leave_balance_adjustments"
    )
    op.drop_table("leave_balance_adjustments")

    op.drop_constraint(
        "ck_leave_requests_half_day_session", "leave_requests", type_="check"
    )
    op.drop_column("leave_requests", "half_day_session")
    op.drop_column("leave_balances", "adjusted")
    op.drop_constraint(
        "ck_leave_policies_applies_to_gender", "leave_policies", type_="check"
    )
    op.drop_column("leave_policies", "applies_to_gender")
    op.drop_column("leave_policies", "sandwich_weekends_holidays")
    op.drop_column("leave_types", "is_comp_off")

    _COMP_OFF_STATUS.drop(op.get_bind(), checkfirst=True)
