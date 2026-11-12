"""Birthday and work-anniversary cards: templates, and the card on the feed post.

Revision ID: e1c5a9d3f7b2
Revises: d4a8f2c6e0b9
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e1c5a9d3f7b2"
down_revision = "d4a8f2c6e0b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "celebration_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "design",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('birthday', 'work_anniversary')", name="ck_celebration_template_kind"
        ),
    )
    op.create_index(
        "uq_celebration_template_in_use",
        "celebration_templates",
        ["tenant_id", "kind"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.add_column("shoutouts", sa.Column("celebration", sa.String(20), nullable=True))
    op.add_column("shoutouts", sa.Column("celebration_year", sa.Integer(), nullable=True))
    op.add_column("shoutouts", sa.Column("card_key", sa.String(512), nullable=True))
    op.drop_constraint("ck_shoutout_has_author", "shoutouts", type_="check")
    op.create_check_constraint(
        "ck_shoutout_has_author",
        "shoutouts",
        "from_employee_id IS NOT NULL OR award_result_id IS NOT NULL "
        "OR milestone_years IS NOT NULL OR celebration IS NOT NULL",
    )
    op.create_index(
        "uq_shoutout_celebration_once",
        "shoutouts",
        ["tenant_id", "to_employee_id", "celebration", "celebration_year"],
        unique=True,
        postgresql_where=sa.text("celebration IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_shoutout_celebration_once", table_name="shoutouts")
    op.execute("DELETE FROM shoutouts WHERE celebration = 'birthday'")
    op.drop_constraint("ck_shoutout_has_author", "shoutouts", type_="check")
    op.create_check_constraint(
        "ck_shoutout_has_author",
        "shoutouts",
        "from_employee_id IS NOT NULL OR award_result_id IS NOT NULL "
        "OR milestone_years IS NOT NULL",
    )
    op.drop_column("shoutouts", "card_key")
    op.drop_column("shoutouts", "celebration_year")
    op.drop_column("shoutouts", "celebration")
    op.drop_index("uq_celebration_template_in_use", table_name="celebration_templates")
    op.drop_table("celebration_templates")
