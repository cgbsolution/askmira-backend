"""Employees on projects

Revision ID: c6e2a8f4b0d7
Revises: b4e8c2a6d0f1
Create Date: 2026-10-29 23:40:00.000000

A staffing company's people are employed by its legal entity and deployed to
clients' projects (DXC INET, JK Paper, UIDAI…), or work on its own ("Internal").
The timesheet projects become that list:

- `projects.legal_entity_id`: the company of the group that staffs a project.
- `employees.project_id`: the project somebody works on now.
- `payslips.project_id`: the project they were on when the run was computed,
  so a month's register keeps saying which project each person was on.
- `lifecycle_event_type` gains `PROJECT_CHANGE`, so a move between projects is
  in the person's history. (A Postgres enum value cannot be dropped; the
  downgrade leaves it.)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c6e2a8f4b0d7"
down_revision: str | None = "b4e8c2a6d0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE lifecycle_event_type ADD VALUE IF NOT EXISTS 'PROJECT_CHANGE'")

    op.add_column(
        "projects",
        sa.Column("legal_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_legal_entity_id",
        "projects",
        "legal_entities",
        ["legal_entity_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "employees",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_employees_project_id",
        "employees",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_employees_project_id", "employees", ["project_id"])

    op.add_column(
        "payslips",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_payslips_project_id",
        "payslips",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_payslips_project_id", "payslips", type_="foreignkey")
    op.drop_column("payslips", "project_id")
    op.drop_index("ix_employees_project_id", table_name="employees")
    op.drop_constraint("fk_employees_project_id", "employees", type_="foreignkey")
    op.drop_column("employees", "project_id")
    op.drop_constraint("fk_projects_legal_entity_id", "projects", type_="foreignkey")
    op.drop_column("projects", "legal_entity_id")
