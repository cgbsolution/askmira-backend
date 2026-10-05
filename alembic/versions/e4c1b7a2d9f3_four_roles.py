"""Four roles: super admin, admin, sub-admin, employee

Six login roles become four:

    platform_admin           -> super_admin
    tenant_admin, hr_admin   -> admin
    hr_ops                   -> sub_admin
    manager, employee        -> employee

`manager` stops being a role. Whoever has a direct report acts for their
reporting line whatever their role, worked out from the org chart on every
request (`User.has_reports`), so the old promotion job is gone with it.

Role names stored as data move too: approval chain steps, letter template
approvers and signatories, loan and agent approver lists, award juries, the BI
connection's export role and the per-module permission table. Chain steps named
`manager` or `hiring_manager` are relationships, not roles, and stay. A role
list that held only `manager` becomes `["admin"]` rather than empty, because an
empty approver list means "no restriction" and the move must never widen who
may approve.

Run logs (`report_runs.run_as_role`, `bi_export_runs.export_role`) are left as
they were: they record what the role was called when the run happened.

Downgrade maps back to tenant_admin / hr_ops / platform_admin, and gives the
manager role back to every employee login somebody reports to. It cannot tell
a former hr_admin from a former tenant_admin.

Revision ID: e4c1b7a2d9f3
Revises: c07e5a1a0934
Create Date: 2026-10-03 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e4c1b7a2d9f3"
down_revision: str | None = "c07e5a1a0934"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UP = {
    "platform_admin": "super_admin",
    "tenant_admin": "admin",
    "hr_admin": "admin",
    "hr_ops": "sub_admin",
}
_DOWN = {
    "super_admin": "platform_admin",
    "admin": "tenant_admin",
    "sub_admin": "hr_ops",
}

#: Single role names: (table, column).
_SCALARS = (
    ("approval_chain_rules", "approver_role"),
    ("requisition_approvals", "approver_role"),
    ("offer_approvals", "approver_role"),
    ("bi_connections", "export_role"),
    ("letter_templates", "signatory_role"),
)
#: Lists of role names held as JSONB arrays.
_JSONB_LISTS = (
    ("agent_autonomy_configs", "approver_roles"),
    ("loan_schemes", "approver_roles"),
    ("award_programmes", "jury_roles"),
)
#: Lists of role names held as varchar(32)[].
_ARRAY_LISTS = (("letter_templates", "approver_roles"),)


def _case(expr: str, mapping: dict[str, str]) -> str:
    whens = " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in mapping.items())
    return f"CASE {expr} {whens} ELSE {expr} END"


def _in(mapping: dict[str, str], *extra: str) -> str:
    return ", ".join(f"'{name}'" for name in (*mapping, *extra))


def _move_scalars(mapping: dict[str, str]) -> None:
    for table, column in _SCALARS:
        op.execute(
            f"UPDATE {table} SET {column} = {_case(f'lower(trim({column}))', mapping)} "
            f"WHERE lower(trim({column})) IN ({_in(mapping)})"
        )


def _move_lists(mapping: dict[str, str], *, drop: str | None) -> None:
    """Rename each element, drop `drop`, de-duplicate; never leave a list empty."""
    keep = f"WHERE e <> '{drop}'" if drop else ""
    touched = _in(mapping, *([drop] if drop else []))
    for table, column in _JSONB_LISTS:
        op.execute(
            f"""
            UPDATE {table} SET {column} = COALESCE(
                (SELECT jsonb_agg(DISTINCT {_case("e", mapping)})
                   FROM jsonb_array_elements_text({column}) AS e {keep}),
                '["admin"]'::jsonb
            )
            WHERE jsonb_typeof({column}) = 'array'
              AND {column} ?| array[{touched}]
            """
        )
    for table, column in _ARRAY_LISTS:
        op.execute(
            f"""
            UPDATE {table} SET {column} = COALESCE(
                (SELECT array_agg(DISTINCT {_case("e", mapping)})::varchar(32)[]
                   FROM unnest({column}) AS e {keep}),
                ARRAY['admin']::varchar(32)[]
            )
            WHERE {column} && ARRAY[{touched}]::varchar(32)[]
            """
        )


def upgrade() -> None:
    # ── users.role: the enum is stored by member name ───────────────────────
    op.execute(
        "CREATE TYPE user_role_new AS ENUM ('SUPER_ADMIN', 'ADMIN', 'SUB_ADMIN', 'EMPLOYEE')"
    )
    op.execute(
        """
        ALTER TABLE users ALTER COLUMN role TYPE user_role_new USING (
            CASE role::text
                WHEN 'PLATFORM_ADMIN' THEN 'SUPER_ADMIN'
                WHEN 'TENANT_ADMIN' THEN 'ADMIN'
                WHEN 'HR_ADMIN' THEN 'ADMIN'
                WHEN 'HR_OPS' THEN 'SUB_ADMIN'
                ELSE 'EMPLOYEE'
            END
        )::user_role_new
        """
    )
    op.execute("DROP TYPE user_role")
    op.execute("ALTER TYPE user_role_new RENAME TO user_role")

    # ── Per-module permissions: one row per (tenant, role, module) ──────────
    # A manager row has no role left to belong to. Where a workspace configured
    # both admin roles for a module, the tenant admin's row is the one kept.
    op.execute("DELETE FROM role_module_permissions WHERE role = 'manager'")
    op.execute(
        """
        DELETE FROM role_module_permissions h
         WHERE h.role = 'hr_admin'
           AND EXISTS (
               SELECT 1 FROM role_module_permissions t
                WHERE t.tenant_id = h.tenant_id
                  AND t.module = h.module
                  AND t.role = 'tenant_admin'
           )
        """
    )
    op.execute(
        f"UPDATE role_module_permissions SET role = {_case('role', _UP)} "
        f"WHERE role IN ({_in(_UP)})"
    )

    _move_scalars(_UP)
    _move_lists(_UP, drop="manager")


def downgrade() -> None:
    _move_lists(_DOWN, drop=None)
    _move_scalars(_DOWN)
    op.execute(
        f"UPDATE role_module_permissions SET role = {_case('role', _DOWN)} "
        f"WHERE role IN ({_in(_DOWN)})"
    )

    op.execute(
        "CREATE TYPE user_role_old AS ENUM "
        "('PLATFORM_ADMIN', 'TENANT_ADMIN', 'HR_ADMIN', 'HR_OPS', 'MANAGER', 'EMPLOYEE')"
    )
    op.execute(
        """
        ALTER TABLE users ALTER COLUMN role TYPE user_role_old USING (
            CASE role::text
                WHEN 'SUPER_ADMIN' THEN 'PLATFORM_ADMIN'
                WHEN 'ADMIN' THEN 'TENANT_ADMIN'
                WHEN 'SUB_ADMIN' THEN 'HR_OPS'
                ELSE 'EMPLOYEE'
            END
        )::user_role_old
        """
    )
    op.execute("DROP TYPE user_role")
    op.execute("ALTER TYPE user_role_old RENAME TO user_role")
    op.execute(
        """
        UPDATE users u SET role = 'MANAGER'
         WHERE u.role = 'EMPLOYEE'
           AND u.employee_id IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM employees e
                WHERE e.tenant_id = u.tenant_id
                  AND e.manager_id = u.employee_id
                  AND e.deleted_at IS NULL
           )
        """
    )
