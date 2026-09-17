"""performance: goals, key results and check-ins; appraisal template resolution and
escalation columns; PIP employee statements

Revision ID: a5f2c91e7d30
Revises: b62b0a40ce25
Create Date: 2026-09-16 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a5f2c91e7d30"
down_revision: str | None = "b62b0a40ce25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Created once with checkfirst and dropped in downgrade(), so the migration
# round-trips instead of failing on "type already exists" the second time.
_GOAL_LEVEL = postgresql.ENUM("INDIVIDUAL", "TEAM", "COMPANY", name="goal_level")
_GOAL_STATUS = postgresql.ENUM("DRAFT", "ACTIVE", "COMPLETED", "CANCELLED", name="goal_status")
_GOAL_VISIBILITY = postgresql.ENUM("SELF", "MANAGER_LINE", "COMPANY", name="goal_visibility")
_KEY_RESULT_KIND = postgresql.ENUM(
    "NUMBER", "PERCENT", "CURRENCY", "MILESTONE", name="key_result_kind"
)
_CHECK_IN_CONFIDENCE = postgresql.ENUM(
    "ON_TRACK", "AT_RISK", "OFF_TRACK", name="goal_check_in_confidence"
)

_ENUM_TYPES = (
    _GOAL_LEVEL,
    _GOAL_STATUS,
    _GOAL_VISIBILITY,
    _KEY_RESULT_KIND,
    _CHECK_IN_CONFIDENCE,
)


def _enum(values: tuple[str, ...], name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in _ENUM_TYPES:
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "goals",
        sa.Column("owner_employee_id", sa.UUID(), nullable=True),
        sa.Column(
            "level", _enum(("INDIVIDUAL", "TEAM", "COMPANY"), "goal_level"), nullable=False
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("weight", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            _enum(("DRAFT", "ACTIVE", "COMPLETED", "CANCELLED"), "goal_status"),
            nullable=False,
        ),
        sa.Column(
            "visibility",
            _enum(("SELF", "MANAGER_LINE", "COMPANY"), "goal_visibility"),
            nullable=False,
        ),
        sa.Column("parent_goal_id", sa.UUID(), nullable=True),
        sa.Column("appraisal_cycle_id", sa.UUID(), nullable=True),
        sa.Column("progress_percent", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_base_columns(),
        sa.CheckConstraint("weight >= 0 AND weight <= 100", name="ck_goal_weight_range"),
        sa.ForeignKeyConstraint(["owner_employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_goal_id"], ["goals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["appraisal_cycle_id"], ["appraisal_cycles.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_goals_tenant_id"), "goals", ["tenant_id"], unique=False)
    op.create_index(
        "ix_goals_owner", "goals", ["tenant_id", "owner_employee_id", "status"], unique=False
    )
    op.create_index("ix_goals_cycle", "goals", ["tenant_id", "appraisal_cycle_id"], unique=False)

    op.create_table(
        "goal_key_results",
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "kind",
            _enum(("NUMBER", "PERCENT", "CURRENCY", "MILESTONE"), "key_result_kind"),
            nullable=False,
        ),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("start_value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("target_value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("current_value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("weight", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        *_base_columns(),
        sa.CheckConstraint("weight > 0", name="ck_key_result_weight_positive"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_goal_key_results_tenant_id"), "goal_key_results", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_goal_key_results_goal", "goal_key_results", ["tenant_id", "goal_id"], unique=False
    )

    op.create_table(
        "goal_check_ins",
        sa.Column("goal_id", sa.UUID(), nullable=False),
        sa.Column("key_result_id", sa.UUID(), nullable=True),
        sa.Column("previous_value", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("new_value", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("progress_after", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column(
            "confidence",
            _enum(("ON_TRACK", "AT_RISK", "OFF_TRACK"), "goal_check_in_confidence"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recorded_by_user_id", sa.UUID(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        *_base_columns(),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["key_result_id"], ["goal_key_results.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["recorded_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_goal_check_ins_tenant_id"), "goal_check_ins", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_goal_check_ins_goal",
        "goal_check_ins",
        ["tenant_id", "goal_id", "recorded_at"],
        unique=False,
    )

    op.create_table(
        "pip_employee_statements",
        sa.Column("pip_id", sa.UUID(), nullable=False),
        sa.Column("check_in_id", sa.UUID(), nullable=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        *_base_columns(),
        sa.ForeignKeyConstraint(["pip_id"], ["pips.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["check_in_id"], ["pip_check_ins.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pip_employee_statements_tenant_id"),
        "pip_employee_statements",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_pip_employee_statements_pip",
        "pip_employee_statements",
        ["tenant_id", "pip_id", "recorded_at"],
        unique=False,
    )

    op.add_column("appraisals", sa.Column("competency_template_id", sa.UUID(), nullable=True))
    op.add_column(
        "appraisals", sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("appraisals", sa.Column("escalated_to_user_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_appraisals_competency_template_id",
        "appraisals",
        "competency_templates",
        ["competency_template_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_appraisals_escalated_to_user_id",
        "appraisals",
        "users",
        ["escalated_to_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_appraisals_escalated_to_user_id", "appraisals", type_="foreignkey")
    op.drop_constraint("fk_appraisals_competency_template_id", "appraisals", type_="foreignkey")
    op.drop_column("appraisals", "escalated_to_user_id")
    op.drop_column("appraisals", "escalated_at")
    op.drop_column("appraisals", "competency_template_id")

    op.drop_index("ix_pip_employee_statements_pip", table_name="pip_employee_statements")
    op.drop_index(
        op.f("ix_pip_employee_statements_tenant_id"), table_name="pip_employee_statements"
    )
    op.drop_table("pip_employee_statements")

    op.drop_index("ix_goal_check_ins_goal", table_name="goal_check_ins")
    op.drop_index(op.f("ix_goal_check_ins_tenant_id"), table_name="goal_check_ins")
    op.drop_table("goal_check_ins")

    op.drop_index("ix_goal_key_results_goal", table_name="goal_key_results")
    op.drop_index(op.f("ix_goal_key_results_tenant_id"), table_name="goal_key_results")
    op.drop_table("goal_key_results")

    op.drop_index("ix_goals_cycle", table_name="goals")
    op.drop_index("ix_goals_owner", table_name="goals")
    op.drop_index(op.f("ix_goals_tenant_id"), table_name="goals")
    op.drop_table("goals")

    bind = op.get_bind()
    for enum_type in reversed(_ENUM_TYPES):
        enum_type.drop(bind, checkfirst=True)
