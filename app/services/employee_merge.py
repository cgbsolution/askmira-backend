"""Folding a duplicate employee record into the real one.

How a duplicate comes about: a workspace is made with an employee record for
its admin (EMP0001), then the old HR system's export is imported with that
same person under their own code. One login, two records — the login on the
bare one, everything from the old system on the other.

`merge` moves whatever points at the record being dropped — the login, people
who report to it, punches, leave, anything — to the record being kept, then
deletes the dropped record, with an audit row, in the caller's transaction.
The references are found from the database's own foreign keys, not the
models, so a table the models know but this database does not yet have (a
migration not applied) is not touched. A reference that cannot move — the
kept record already has its own leave balance for that year, say — stops the
merge and is listed; nothing is moved in that case.

Only two records with the same work email are merged, and only inside one
workspace. This is an operator's tool: `scripts/merge_duplicate_employee.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction
from app.models.employee import Employee
from app.services import audit

__all__ = ["MergeError", "MergeReport", "duplicates", "merge"]

_REFERENCES = text(
    """
    select n.nspname as schema, r.relname as tbl, a.attname as col
    from pg_constraint c
    join pg_class r on r.oid = c.conrelid
    join pg_namespace n on n.oid = r.relnamespace
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
    where c.contype = 'f'
      and c.confrelid = 'employees'::regclass
      and array_length(c.conkey, 1) = 1
    order by r.relname, a.attname
    """
)


class MergeError(ValueError):
    pass


@dataclass
class MergeReport:
    keep_code: str
    drop_code: str
    #: "table.column" -> rows moved to the kept record.
    moved: dict[str, int] = field(default_factory=dict)
    #: "table.column" -> why its rows could not move.
    blocked: dict[str, str] = field(default_factory=dict)


async def duplicates(session: AsyncSession, tenant_id: uuid.UUID) -> list[list[Employee]]:
    """The workspace's records that share a work email, oldest first in each group."""
    shared = (
        select(func.lower(Employee.work_email))
        .where(Employee.tenant_id == tenant_id)
        .group_by(func.lower(Employee.work_email))
        .having(func.count() > 1)
    )
    rows = (
        await session.scalars(
            select(Employee)
            .where(Employee.tenant_id == tenant_id, func.lower(Employee.work_email).in_(shared))
            .order_by(func.lower(Employee.work_email), Employee.created_at)
        )
    ).all()
    groups: dict[str, list[Employee]] = {}
    for employee in rows:
        groups.setdefault(employee.work_email.lower(), []).append(employee)
    return list(groups.values())


async def _by_code(session: AsyncSession, tenant_id: uuid.UUID, code: str) -> Employee:
    employee = await session.scalar(
        select(Employee).where(
            Employee.tenant_id == tenant_id, func.upper(Employee.employee_code) == code.upper()
        )
    )
    if employee is None:
        raise MergeError(f"No employee {code} in this workspace.")
    return employee


async def merge(
    session: AsyncSession, tenant_id: uuid.UUID, *, keep_code: str, drop_code: str
) -> MergeReport:
    """Move everything from `drop_code` to `keep_code` and delete `drop_code`.

    Does not commit, and expires the session's objects (rows moved under them).
    Raises MergeError, having moved nothing it can be trusted
    to have moved, when a reference cannot move: the caller rolls back.
    """
    keep = await _by_code(session, tenant_id, keep_code)
    drop = await _by_code(session, tenant_id, drop_code)
    if keep.id == drop.id:
        raise MergeError("Keep and drop are the same record.")
    if keep.work_email.lower() != drop.work_email.lower():
        raise MergeError(
            f"{keep.employee_code} ({keep.work_email}) and {drop.employee_code} "
            f"({drop.work_email}) have different emails; only one person's two records merge."
        )
    report = MergeReport(keep_code=keep.employee_code, drop_code=drop.employee_code)
    preparer = session.bind.dialect.identifier_preparer
    keep_id, drop_id = keep.id, drop.id

    # The kept record reporting to the dropped one would report to itself.
    for column in ("manager_id", "dotted_line_manager_id"):
        if getattr(keep, column, None) == drop_id:
            setattr(keep, column, None)
    await session.flush()

    for ref in (await session.execute(_REFERENCES)).mappings().all():
        name = f"{ref['tbl']}.{ref['col']}"
        table = f"{preparer.quote(ref['schema'])}.{preparer.quote(ref['tbl'])}"
        column = preparer.quote(ref["col"])
        try:
            async with session.begin_nested():
                moved = await session.execute(
                    text(f"update {table} set {column} = :keep where {column} = :drop"),
                    {"keep": keep_id, "drop": drop_id},
                )
        except DBAPIError as exc:
            report.blocked[name] = str(exc.orig).splitlines()[0]
            continue
        if moved.rowcount:
            report.moved[name] = moved.rowcount
    if report.blocked:
        raise MergeError(
            "These could not move to the kept record, so nothing was merged:\n"
            + "\n".join(f"  {name}: {why}" for name, why in report.blocked.items())
        )

    session.expire_all()
    drop = await session.get(Employee, drop_id)
    await session.delete(drop)
    await session.flush()
    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.DELETE,
        entity_type="employee",
        entity_id=drop_id,
        actor_type="system",
        changes={"merged_into": report.keep_code, "kept_id": str(keep_id), "moved": report.moved},
        summary=f"Merged duplicate record {report.drop_code} into {report.keep_code}",
    )
    return report
