"""Holiday, Week off and Comp off in every workspace's Request time off list.

`leave_types.marks_day_as` ("holiday" | "week_off") makes a type a day off of
that kind rather than leave: a request needs no balance and takes none, and
once approved its days are marked Holiday or Week off in attendance — a paid
day off. One of each per workspace (a partial unique index).

Every workspace gets "Holiday" (HO) and "Week off" (WO), and "Comp off" (CO)
when no active type takes comp-off credits yet. A code already in use moves to
the next one (HOL, WOFF, COFF…).

Revision ID: b7e2d4f9a1c6
Revises: a4d8c2e6f1b3
Create Date: 2026-11-27
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "b7e2d4f9a1c6"
down_revision = "a4d8c2e6f1b3"
branch_labels = None
depends_on = None

_CODES = {
    "holiday": ("HO", "HOL", "HOLIDAY"),
    "week_off": ("WO", "WOFF", "WEEKOFF"),
    "comp_off": ("CO", "COFF", "COMPOFF"),
}


def upgrade() -> None:
    op.add_column("leave_types", sa.Column("marks_day_as", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_leave_types_marks_day_as",
        "leave_types",
        "marks_day_as IS NULL OR marks_day_as IN ('holiday', 'week_off')",
    )
    op.create_index(
        "uq_leave_types_tenant_marks_day_as",
        "leave_types",
        ["tenant_id", "marks_day_as"],
        unique=True,
        postgresql_where=sa.text("marks_day_as IS NOT NULL"),
    )

    bind = op.get_bind()
    tenants = [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants")).all()]
    insert = sa.text(
        "INSERT INTO leave_types (id, tenant_id, name, code, is_paid, allows_half_day, "
        "requires_document, is_statutory, is_comp_off, marks_day_as, is_active) VALUES "
        "(:id, :tenant_id, :name, :code, true, :half, false, false, :comp_off, :marks, true)"
    )
    for tenant_id in tenants:
        rows = bind.execute(
            sa.text("SELECT code, is_comp_off, is_active FROM leave_types WHERE tenant_id = :t"),
            {"t": tenant_id},
        ).all()
        codes = {row[0] for row in rows}
        wanted = [
            ("holiday", "Holiday", False, False, "holiday"),
            ("week_off", "Week off", False, False, "week_off"),
        ]
        if not any(row[1] and row[2] for row in rows):
            wanted.append(("comp_off", "Comp off", True, True, None))
        for key, name, half, comp_off, marks in wanted:
            code = next((c for c in _CODES[key] if c not in codes), None)
            if code is None:
                continue
            codes.add(code)
            bind.execute(
                insert,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "name": name,
                    "code": code,
                    "half": half,
                    "comp_off": comp_off,
                    "marks": marks,
                },
            )


def downgrade() -> None:
    # The Holiday and Week off types go if nothing was ever asked for with
    # them; Comp off stays (comp-off credits may already sit on it).
    op.execute(
        "DELETE FROM leave_types WHERE marks_day_as IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM leave_requests WHERE leave_requests.leave_type_id = leave_types.id)"
    )
    op.drop_index("uq_leave_types_tenant_marks_day_as", table_name="leave_types")
    op.drop_constraint("ck_leave_types_marks_day_as", "leave_types", type_="check")
    op.drop_column("leave_types", "marks_day_as")
