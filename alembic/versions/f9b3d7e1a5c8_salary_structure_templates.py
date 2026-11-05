"""Salary structure templates: a rule per line and employer contributions inside the CTC.

Payroll → Salary structures builds a structure as a template HR fills in
percentages of the CTC; the Salary master then splits a CTC by it.
`salary_structure_components.rule` holds each line's rule and
`salary_structures.template` the employer contributions kept inside the CTC.
Both are null on a structure of the earlier kind, which keeps working as before.

Revision ID: f9b3d7e1a5c8
Revises: d8f2b6c4a1e9, f6b0d4e8a2c9 (also merges the two heads)
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f9b3d7e1a5c8"
down_revision = ("d8f2b6c4a1e9", "f6b0d4e8a2c9")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "salary_structures",
        sa.Column("template", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "salary_structure_components",
        sa.Column("rule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("salary_structure_components", "rule")
    op.drop_column("salary_structures", "template")
