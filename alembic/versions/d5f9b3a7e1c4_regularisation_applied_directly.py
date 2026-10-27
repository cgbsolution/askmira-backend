"""Attendance regularisation: applied_directly

Revision ID: d5f9b3a7e1c4
Revises: c4e8a2f6d0b3
Create Date: 2026-10-27 17:00:00.000000

Marks a correction HR made themselves (applied at once) apart from a request
HR approved, so the calendar can say "Marked manually" or "Missed punch
approved". Earlier direct corrections are found in the audit log, which has
recorded `applied_directly: true` on them all along.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5f9b3a7e1c4"
down_revision: str | None = "c4e8a2f6d0b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attendance_regularisations",
        sa.Column(
            "applied_directly", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.execute(
        """
        UPDATE attendance_regularisations AS r
        SET applied_directly = true
        FROM audit_logs AS a
        WHERE a.entity_type = 'attendance_regularisation'
          AND a.entity_id = r.id
          AND a.changes ->> 'applied_directly' = 'true'
        """
    )
    # Recompute picks the new wording up; until then the stored note is the
    # old one, which is still true.


def downgrade() -> None:
    op.drop_column("attendance_regularisations", "applied_directly")
