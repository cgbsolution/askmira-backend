"""Work weeks, salary days and legal-entity holidays

Revision ID: e7a3c9f1b5d2
Revises: d5f9b3a7e1c4
Create Date: 2026-10-28 18:00:00.000000

- `shifts.alternate_off_weeks`: weekdays off only in some weeks of the month
  (2nd and 4th Saturday off).
- `legal_entities.default_shift_id`: the work week for that company's people
  with no shift of their own, before the workspace default.
- `legal_entities.salary_days_basis` / `salary_fixed_days`: how a month's
  salary is divided into days (calendar, fixed 26/30, scheduled working days).
  Every existing entity stays on `calendar`, which is what payroll did before.
- `holidays.legal_entity_id`: a holiday for one company of the group only.
- `holidays.is_paid`: an unpaid holiday is a day off that is loss of pay.
  Every existing holiday stays paid.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7a3c9f1b5d2"
down_revision: str | None = "d5f9b3a7e1c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shifts",
        sa.Column(
            "alternate_off_weeks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.add_column(
        "legal_entities",
        sa.Column("default_shift_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_legal_entities_default_shift_id",
        "legal_entities",
        "shifts",
        ["default_shift_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "legal_entities",
        sa.Column(
            "salary_days_basis",
            sa.String(length=16),
            server_default=sa.text("'calendar'"),
            nullable=False,
        ),
    )
    op.add_column("legal_entities", sa.Column("salary_fixed_days", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_legal_entities_salary_days_basis",
        "legal_entities",
        "salary_days_basis IN ('calendar', 'fixed', 'working_days')",
    )

    op.add_column(
        "holidays",
        sa.Column("legal_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_holidays_legal_entity_id",
        "holidays",
        "legal_entities",
        ["legal_entity_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_holidays_legal_entity_id", "holidays", ["legal_entity_id"])
    op.add_column(
        "holidays",
        sa.Column("is_paid", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("holidays", "is_paid")
    op.drop_index("ix_holidays_legal_entity_id", table_name="holidays")
    op.drop_constraint("fk_holidays_legal_entity_id", "holidays", type_="foreignkey")
    op.drop_column("holidays", "legal_entity_id")
    op.drop_constraint("ck_legal_entities_salary_days_basis", "legal_entities", type_="check")
    op.drop_column("legal_entities", "salary_fixed_days")
    op.drop_column("legal_entities", "salary_days_basis")
    op.drop_constraint("fk_legal_entities_default_shift_id", "legal_entities", type_="foreignkey")
    op.drop_column("legal_entities", "default_shift_id")
    op.drop_column("shifts", "alternate_off_weeks")
