"""Attendance: default shift, request kinds, overtime

A workspace default shift for anyone with no assignment; regularisation kinds
(missed punch, status change, on duty, work from home); derived overtime
minutes on each attendance day.

Revision ID: c07e5a1a0930
Revises: ae8b1d4d874e
Create Date: 2026-09-30 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c07e5a1a0930"
down_revision: str | None = "ae8b1d4d874e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KIND = postgresql.ENUM(
    "MISSED_PUNCH", "STATUS_CHANGE", "ON_DUTY", "WORK_FROM_HOME", name="regularisation_kind"
)


def upgrade() -> None:
    bind = op.get_bind()
    _KIND.create(bind, checkfirst=True)

    op.add_column(
        "shifts",
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    # At most one default shift per workspace.
    op.create_index(
        "uq_shifts_one_default",
        "shifts",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.add_column(
        "attendance_days",
        sa.Column("overtime_minutes", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "attendance_regularisations",
        sa.Column(
            "kind",
            postgresql.ENUM(name="regularisation_kind", create_type=False),
            server_default="STATUS_CHANGE",
            nullable=False,
        ),
    )
    # Existing corrections that carried a time were fixing a missed punch.
    op.execute(
        "UPDATE attendance_regularisations SET kind = 'MISSED_PUNCH' "
        "WHERE requested_first_in IS NOT NULL OR requested_last_out IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("attendance_regularisations", "kind")
    op.drop_column("attendance_days", "overtime_minutes")
    op.drop_index("uq_shifts_one_default", table_name="shifts")
    op.drop_column("shifts", "is_default")
    _KIND.drop(op.get_bind(), checkfirst=True)
