"""Punch location label; a General shift for workspaces with none

`attendance_punches.location_label` keeps the nearest work location's name as
it was when the punch was made. Every workspace that has no shift at all gets
an editable General shift (09:30-18:30, Saturday and Sunday off) as its
default: without any shift no day can be judged, and every employee's month
read "needs regularisation".

Revision ID: c07e5a1a0932
Revises: 9a7e0930f0ff
Create Date: 2026-09-30 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c07e5a1a0932"
down_revision: str | None = "9a7e0930f0ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attendance_punches",
        sa.Column("location_label", sa.String(length=160), nullable=True),
    )
    op.execute(
        """
        INSERT INTO shifts (
            id, tenant_id, code, name, start_time, end_time, break_minutes,
            grace_in_minutes, grace_out_minutes, half_day_threshold_minutes,
            full_day_threshold_minutes, is_night_shift, weekly_off_days, active,
            is_default, created_at, updated_at
        )
        SELECT
            gen_random_uuid(), t.id, 'GEN', 'General shift', TIME '09:30', TIME '18:30', 60,
            15, 15, 240, 420, false, ARRAY[6, 7], true, true, now(), now()
        FROM tenants t
        WHERE NOT EXISTS (SELECT 1 FROM shifts s WHERE s.tenant_id = t.id)
        """
    )


def downgrade() -> None:
    # The General shifts stay: days may already have been judged against them,
    # and removing a shift an attendance day points at would orphan the record.
    op.drop_column("attendance_punches", "location_label")
