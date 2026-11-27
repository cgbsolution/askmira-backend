"""Project-wise holiday calendars.

A staffing company deploys people to clients' projects, and a client's people
keep the client's holidays, not the company's. Two columns:

- `holidays.project_id` — a holiday for one project's people only. Null: the
  company calendar, as before. It combines with the state, office and company
  scope already there. Deleted with its project.
- `projects.own_holidays_from` — from this date the project keeps its own
  calendar: the company's holidays on or after it no longer apply to its
  people, only the project's own. Before it they still do, so months already
  worked out keep the holidays they were paid for. Null: the company calendar
  plus any holidays added for the project.

Revision ID: a4d8c2e6f1b3
Revises: f3c8a1d6b9e2
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a4d8c2e6f1b3"
down_revision = "f3c8a1d6b9e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "holidays",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_holidays_project_id",
        "holidays",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_holidays_project_id", "holidays", ["project_id"])
    op.add_column("projects", sa.Column("own_holidays_from", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "own_holidays_from")
    op.drop_index("ix_holidays_project_id", table_name="holidays")
    op.drop_constraint("fk_holidays_project_id", "holidays", type_="foreignkey")
    op.drop_column("holidays", "project_id")
