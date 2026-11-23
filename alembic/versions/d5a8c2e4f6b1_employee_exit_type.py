"""Employees: how a leaver left (`exit_type`), so an absconder shows as Absconding.

HR can now mark somebody Exited or Absconding straight from the record's
Status. The status stays `exited` for both — every rule for a leaver (no login,
no pay after the exit date, no leave) already keys on it — and this column says
which. Leavers whose exit went through People > Exits take the exit's type.

Revision ID: d5a8c2e4f6b1
Revises: c9f1d3b7a2e4
Create Date: 2026-11-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d5a8c2e4f6b1"
down_revision = "c9f1d3b7a2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("exit_type", sa.String(length=24), nullable=True))
    op.execute(
        """
        UPDATE employees AS e
           SET exit_type = lower(x.exit_type::text)
          FROM exit_records AS x
         WHERE x.employee_id = e.id
           AND x.tenant_id = e.tenant_id
           AND x.access_revoked_at IS NOT NULL
           AND e.status = 'EXITED'
        """
    )


def downgrade() -> None:
    op.drop_column("employees", "exit_type")
