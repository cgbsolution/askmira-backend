"""Audit log insert-only

The audit trail was insert-only by convention: no code path updated or deleted
it, but nothing stopped one. This trigger makes the database refuse any UPDATE
or DELETE on audit_logs, except the ON DELETE SET NULL of actor_user_id when a
user row is removed (which changes nothing else on the row). The same DDL is
attached to the model, so a schema built from the metadata has it too.

Revision ID: c6f2a7b9d0e3
Revises: b5e1f6a8c9d2
Create Date: 2026-10-13 21:18:33.464522
"""

from collections.abc import Sequence

from alembic import op


revision: str = 'c6f2a7b9d0e3'
down_revision: str | None = 'b5e1f6a8c9d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = """
CREATE OR REPLACE FUNCTION audit_logs_insert_only() RETURNS trigger AS $$
DECLARE
    unlinked audit_logs%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.actor_user_id IS NOT NULL AND NEW.actor_user_id IS NULL THEN
        unlinked := OLD;
        unlinked.actor_user_id := NULL;
        IF unlinked IS NOT DISTINCT FROM NEW THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'audit_logs is insert-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql
"""

TRIGGER = """
CREATE TRIGGER audit_logs_insert_only
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_insert_only()
"""


def _raw(sql: str) -> None:
    # Straight to the driver: no parameter parsing, so % and := reach Postgres as written.
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def upgrade() -> None:
    _raw(FUNCTION)
    _raw("DROP TRIGGER IF EXISTS audit_logs_insert_only ON audit_logs")
    _raw(TRIGGER)


def downgrade() -> None:
    _raw("DROP TRIGGER IF EXISTS audit_logs_insert_only ON audit_logs")
    _raw("DROP FUNCTION IF EXISTS audit_logs_insert_only()")
