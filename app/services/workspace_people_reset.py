"""Clearing a workspace's people before a fresh import.

When a customer has trial-loaded its people and wants to start again from a
clean export, every employee record and login goes except the admin the
platform created with the workspace — and with them everything that belongs
to people: punches and attendance days, attendance locks, leave requests and
balances, profiles, bank accounts, salaries (the salary master), payroll runs
and payslips, documents, lifecycle events, onboarding and exit records,
notifications and Ask Mira chats. The kept admin's own punches, chats and
notifications go too: the workspace starts empty.

Settings stay: companies, locations, projects, shifts, leave types and
policies, salary components and structures, payroll rule sets, letter and
notification templates, connectors. A link from a setting to a removed person
(who approved a rule set, say) is emptied by its foreign key; the setting
itself is unchanged, and a rule set stays approved — approval is its date.

The audit trail stays, as it must (CLAUDE.md rule 3): it is insert-only, and
its rows keep their summaries; only their link to a removed login is emptied,
which its trigger allows.

Every table is accounted for. A table holding the workspace's rows that is in
none of the lists below stops the reset before anything is touched — a
module used since this was written must be classified, not guessed. After the
deletes, every kept table must hold exactly the rows it held before, or the
reset raises and the caller rolls back.

Raw SQL by table name, not the models: the live database can be a migration
behind or ahead of this code. Nothing is committed here; uploaded files are
returned for the caller to delete after its commit. This is an operator's
tool: `scripts/reset_workspace_people.py`.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction
from app.services import audit
from app.services.storage.cleanup import keys_in

__all__ = [
    "FOLLOW_TABLES",
    "KEEP_TABLES",
    "PEOPLE_TABLES",
    "WIPE_TABLES",
    "ResetError",
    "ResetReport",
    "reset_people",
]

#: Emptied for the workspace: rows that belong to people, the kept admin's too.
WIPE_TABLES: frozenset[str] = frozenset(
    {
        "attendance_days",
        "attendance_period_locks",
        "attendance_punches",
        "companion_conversations",
        "companion_messages",
        "employee_bank_accounts",
        "employee_documents",
        "employee_profiles",
        "employee_salaries",
        "employee_salary_components",
        "exit_records",
        "leave_balance_adjustments",
        "leave_balances",
        "leave_requests",
        "lifecycle_events",
        "notifications",
        "onboarding_plans",
        "onboarding_tasks",
        "payroll_runs",
        "payslips",
        "probation_reviews",
    }
)

#: Every row but the kept admin's.
PEOPLE_TABLES: tuple[str, ...] = ("users", "employees")

#: A login's own sign-in records: they go with a removed login (foreign-key
#: cascade); the kept admin's stay.
FOLLOW_TABLES: frozenset[str] = frozenset({"password_reset_tokens", "step_up_challenges"})

#: Settings and records that are not about people. Never deleted from.
KEEP_TABLES: frozenset[str] = frozenset(
    {
        "audit_logs",
        "departments",
        "holidays",
        "leave_policies",
        "leave_types",
        "legal_entities",
        "letter_template_fields",
        "letter_templates",
        "locations",
        "notification_policies",
        "notification_templates",
        "onboarding_template_tasks",
        "onboarding_templates",
        "payroll_rule_sets",
        "product_links",
        "projects",
        "salary_components",
        "salary_structure_components",
        "salary_structures",
        "scheduler_job_runs",
        "shifts",
    }
)

#: Kept tables not searched for leftover mentions of removed people: the audit
#: trail names them by design.
_NOT_SCANNED = frozenset({"audit_logs"})

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_TENANT_TABLES = text(
    """
    select c.table_name
    from information_schema.columns c
    join information_schema.tables t
      on t.table_schema = c.table_schema and t.table_name = c.table_name
    where c.table_schema = 'public' and c.column_name = 'tenant_id'
      and t.table_type = 'BASE TABLE'
    order by c.table_name
    """
)

#: Single-column foreign keys from a table to users or employees.
_PEOPLE_LINKS = text(
    """
    select r.relname as tbl, a.attname as col, f.relname as target
    from pg_constraint c
    join pg_class r on r.oid = c.conrelid
    join pg_class f on f.oid = c.confrelid
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
    where c.contype = 'f' and array_length(c.conkey, 1) = 1
      and f.relname in ('users', 'employees')
      and r.relnamespace = 'public'::regnamespace
      and f.relnamespace = 'public'::regnamespace
    order by r.relname, a.attname
    """
)


class ResetError(ValueError):
    pass


@dataclass
class ResetReport:
    kept_email: str
    kept_code: str | None
    #: table -> rows removed.
    removed: dict[str, int] = field(default_factory=dict)
    #: table -> rows kept (unchanged in number).
    kept: dict[str, int] = field(default_factory=dict)
    #: "table.column" -> kept rows whose link to a removed person was emptied.
    links_cleared: dict[str, int] = field(default_factory=dict)
    #: kept table -> rows that still mention a removed person inside their data.
    mentions_left: dict[str, int] = field(default_factory=dict)
    #: storage keys of the removed rows' files, to delete after the commit.
    files: set[str] = field(default_factory=set)


def _quote(session: AsyncSession, name: str) -> str:
    return session.bind.dialect.identifier_preparer.quote(name)


async def _counts(session: AsyncSession, tenant_id: uuid.UUID, tables: list[str]) -> dict[str, int]:
    if not tables:
        return {}
    parts = [
        f"select '{name}' as tbl, (select count(*) from {_quote(session, name)} "
        f"where tenant_id = :t) as n"
        for name in tables
    ]
    rows = (await session.execute(text(" union all ".join(parts)), {"t": tenant_id})).all()
    return {row.tbl: int(row.n) for row in rows}


async def _rows_as_json(
    session: AsyncSession, table: str, tenant_id: uuid.UUID, extra: str = "", **params
) -> list[str]:
    rows = await session.execute(
        text(
            f"select row_to_json(x)::text from {_quote(session, table)} x "
            f"where x.tenant_id = :t {extra}"
        ),
        {"t": tenant_id, **params},
    )
    return [row[0] for row in rows]


async def reset_people(
    session: AsyncSession, tenant_id: uuid.UUID, *, keep_user_id: uuid.UUID
) -> ResetReport:
    """Remove the workspace's people but the kept admin; keep its settings.

    Does not commit. Raises ResetError, before touching anything, when a table
    is unclassified or the kept login is not an admin of this workspace, and
    after the deletes when a kept table lost rows: the caller rolls back.
    """
    kept = (
        await session.execute(
            text(
                "select u.id, u.email, u.role::text as role, u.employee_id, e.employee_code "
                "from users u left join employees e on e.id = u.employee_id "
                "where u.id = :u and u.tenant_id = :t"
            ),
            {"u": keep_user_id, "t": tenant_id},
        )
    ).one_or_none()
    if kept is None:
        raise ResetError("The login to keep is not in this workspace.")
    if kept.role.upper() != "ADMIN":
        raise ResetError(
            f"{kept.email} is not an admin; nobody would be left to run the workspace."
        )

    tables = list((await session.scalars(_TENANT_TABLES)).all())
    before = await _counts(session, tenant_id, tables)
    known = WIPE_TABLES | FOLLOW_TABLES | KEEP_TABLES | set(PEOPLE_TABLES)
    unknown = sorted(name for name, n in before.items() if n and name not in known)
    if unknown:
        raise ResetError(
            "These tables hold this workspace's rows but are not classified as people data "
            "or settings, so nothing was done: " + ", ".join(unknown)
        )

    removed_users = set(
        (
            await session.scalars(
                text("select id from users where tenant_id = :t and id <> :keep"),
                {"t": tenant_id, "keep": keep_user_id},
            )
        ).all()
    )
    employee_sql = "select id from employees where tenant_id = :t"
    employee_params: dict = {"t": tenant_id}
    if kept.employee_id is not None:
        employee_sql += " and id <> :keep"
        employee_params["keep"] = kept.employee_id
    removed_employees = set((await session.scalars(text(employee_sql), employee_params)).all())
    removed_ids = {str(i) for i in removed_users | removed_employees}

    report = ResetReport(kept_email=kept.email, kept_code=kept.employee_code)

    # Links from kept rows to removed people: their foreign keys empty them.
    for link in (await session.execute(_PEOPLE_LINKS)).mappings().all():
        if link["tbl"] not in KEEP_TABLES | {"tenants"} or link["tbl"] == "audit_logs":
            continue
        ids = removed_users if link["target"] == "users" else removed_employees
        if not ids:
            continue
        where = "tenant_id = :t and " if link["tbl"] != "tenants" else "id = :t and "
        n = await session.scalar(
            text(
                f"select count(*) from {_quote(session, link['tbl'])} "
                f"where {where}{_quote(session, link['col'])} = any(:ids)"
            ),
            {"t": tenant_id, "ids": list(ids)},
        )
        if n:
            report.links_cleared[f"{link['tbl']}.{link['col']}"] = int(n)

    # The removed rows' files, found by shape in every text and JSON column.
    for name in sorted(WIPE_TABLES):
        if before.get(name):
            for row in await _rows_as_json(session, name, tenant_id):
                report.files |= keys_in(json.loads(row), tenant_id)
    for name, ids in (("users", removed_users), ("employees", removed_employees)):
        if ids:
            for row in await _rows_as_json(
                session, name, tenant_id, "and x.id = any(:ids)", ids=list(ids)
            ):
                report.files |= keys_in(json.loads(row), tenant_id)

    # People data first, children before parents: a table refused because a
    # row still points at it is tried again after the others.
    pending = sorted(name for name in WIPE_TABLES if before.get(name))
    for _ in range(len(pending) + 1):
        if not pending:
            break
        refused = []
        for name in pending:
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(f"delete from {_quote(session, name)} where tenant_id = :t"),
                        {"t": tenant_id},
                    )
            except IntegrityError:
                refused.append(name)
        pending = refused
    if pending:
        raise ResetError("Could not empty: " + ", ".join(pending))

    # Then the logins and the records. A login's own sign-in rows cascade; the
    # audit trail's link to it is emptied.
    if removed_users:
        await session.execute(
            text("delete from users where tenant_id = :t and id = any(:ids)"),
            {"t": tenant_id, "ids": list(removed_users)},
        )
    if removed_employees:
        await session.execute(
            text("delete from employees where tenant_id = :t and id = any(:ids)"),
            {"t": tenant_id, "ids": list(removed_employees)},
        )

    after = await _counts(session, tenant_id, [name for name, n in before.items() if n])
    for name, n in sorted(before.items()):
        if not n:
            continue
        gone = n - after.get(name, 0)
        if name in KEEP_TABLES:
            if gone:
                raise ResetError(f"{name} would lose {gone} of {n} rows; nothing was saved.")
            report.kept[name] = n
        elif gone:
            report.removed[name] = gone

    # Mentions of removed people inside kept rows' own data (a JSON setting,
    # say): not changed here, reported so they can be looked at.
    scanned: dict[str, list[str]] = {
        name: await _rows_as_json(session, name, tenant_id)
        for name in sorted(KEEP_TABLES - _NOT_SCANNED)
        if before.get(name)
    }
    scanned["tenants"] = list(
        (
            await session.scalars(
                text("select row_to_json(x)::text from tenants x where x.id = :t"),
                {"t": tenant_id},
            )
        ).all()
    )
    for name, rows in scanned.items():
        hits = sum(1 for row in rows if removed_ids & set(_UUID.findall(row)))
        if hits:
            report.mentions_left[name] = hits

    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.DELETE,
        entity_type="tenant",
        entity_id=tenant_id,
        actor_type="system",
        changes={
            "reset": "people",
            "kept_login": report.kept_email,
            "kept_employee_code": report.kept_code,
            "removed": report.removed,
            "links_cleared": report.links_cleared,
            "files": len(report.files),
        },
        summary=(
            f"People data cleared before a fresh import: "
            f"{report.removed.get('employees', 0)} employee records and "
            f"{report.removed.get('users', 0)} logins removed with their attendance, leave, "
            f"salaries, payroll runs, payslips and documents. Kept: the admin "
            f"{report.kept_email} and the workspace's settings."
        ),
    )
    return report
