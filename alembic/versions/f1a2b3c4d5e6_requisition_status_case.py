"""Requisition status: REJECTED and FULFILLED in the case the code writes

`75504cb12d0e` added `rejected` and `fulfilled` to `requisition_status` in lower
case. The column stores enum member names, upper case like the type's other
labels, so rejecting a requisition and filling its last opening (releasing the
offer that fills it) both failed with "invalid input value for enum" — a 500 —
on every database built through migrations. Tests build the schema from the
models and never saw it.

The labels are renamed rather than added beside, so the type is left with one
spelling of each. A database that somehow already has the upper-case label is
left alone.

Revision ID: f1a2b3c4d5e6
Revises: e4c1b7a2d9f3
Create Date: 2026-10-07 03:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e4c1b7a2d9f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LABELS = (("rejected", "REJECTED"), ("fulfilled", "FULFILLED"))


def _rename(old: str, new: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
                WHERE t.typname = 'requisition_status' AND e.enumlabel = '{old}'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
                WHERE t.typname = 'requisition_status' AND e.enumlabel = '{new}'
            ) THEN
                ALTER TYPE requisition_status RENAME VALUE '{old}' TO '{new}';
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    for old, new in _LABELS:
        _rename(old, new)


def downgrade() -> None:
    for old, new in _LABELS:
        _rename(new, old)
