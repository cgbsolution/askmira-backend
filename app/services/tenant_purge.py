"""After a workspace is deleted: hand its data back, then purge it.

DPDP s.8(7)(b): when the customer (the Data Fiduciary) stops using AskMira, its
processor must not keep the employees' data. Deleting a workspace closes it at
once (`platform.delete`); its data then stays for `PURGE_GRACE_DAYS` so the
super admin can download it and give it to the customer — who has to keep its
own payroll and statutory records — and is then purged by a nightly platform
job.

What the purge removes: every row of every table that carries the workspace's
id, and every uploaded file under its storage prefix.

What it keeps, and why:

- **The audit trail** (`audit_logs`). It is insert-only by design (CLAUDE.md
  rule 3, enforced by a database trigger) and is the record that the workspace
  existed, was deleted and was purged. Its rows hold names in their summaries;
  this is the documented exception, listed in the Trust Center for counsel.
- **The tenant row itself**, renamed "Deleted workspace" with its settings
  cleared — the audit trail's rows point at it.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 — registers every table before the metadata is walked
from app.core.deps import tenant_filter
from app.models.audit import AuditAction
from app.models.base import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.dpdp.contents import _plain, withheld_column
from app.services.storage import StorageError, get_storage
from app.services.storage.cleanup import keys_in

__all__ = [
    "KEPT_TABLES",
    "PURGE_GRACE_DAYS",
    "deleted_workspaces",
    "export_workspace",
    "purge_due",
    "purge_workspace",
]

#: How long a deleted workspace's data waits to be handed back.
PURGE_GRACE_DAYS = 90

#: Never purged — see the module docstring for why.
KEPT_TABLES: dict[str, str] = {
    "audit_logs": "Insert-only: the record of what was done, including this purge.",
    "tenants": "The audit trail points at it; kept renamed, with its settings cleared.",
}


def _require_super(user: User) -> None:
    from app.services.platform import _require_super as require_platform_super

    require_platform_super(user)


def _tenant_tables() -> list[Any]:
    """Tables holding a workspace's rows, children before parents."""
    return [
        table
        for table in reversed(Base.metadata.sorted_tables)
        if "tenant_id" in table.c and table.name not in KEPT_TABLES
    ]


async def _deleted_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.deleted_at is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such deleted workspace."
        )
    return tenant


async def deleted_workspaces(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Deleted workspaces with their purge dates, for the platform console."""
    _require_super(user)
    rows = (
        await session.scalars(
            select(Tenant).where(Tenant.deleted_at.is_not(None)).order_by(Tenant.deleted_at.desc())
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
            "purge_after": row.purge_after.isoformat() if row.purge_after else None,
            "purged_at": row.purged_at.isoformat() if row.purged_at else None,
            "can_export": row.purged_at is None,
        }
        for row in rows
    ]


async def export_workspace(
    session: AsyncSession, user: User, tenant_id: uuid.UUID, *, request: Request | None = None
) -> tuple[str, str]:
    """A zip of everything the workspace holds: one JSON file per table, and its files.

    For handing back to the customer before the purge. Credential material
    (password and token hashes, encrypted secrets) is left out, as in every
    export. Written to a temporary file the caller streams and then removes.
    """
    _require_super(user)
    tenant = await _deleted_tenant(session, tenant_id)
    if tenant.purged_at is not None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="This workspace has already been purged."
        )

    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)  # noqa: SIM115
    handle.close()
    # The company logo sits on the tenant row, which is not exported as a table.
    files: set[str] = set(keys_in(tenant.logo_key, tenant.id))
    tables_written = rows_written = 0
    with zipfile.ZipFile(handle.name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for table in reversed(_tenant_tables()):
            rows = (
                (await session.execute(select(table).where(tenant_filter(table, tenant.id))))
                .mappings()
                .all()
            )
            if not rows:
                continue
            columns = [c.name for c in table.columns if not withheld_column(c.name)]
            payload = [{name: _plain(row[name]) for name in columns} for row in rows]
            for row in payload:
                files |= keys_in(row, tenant.id)
            archive.writestr(
                f"data/{table.name}.json", json.dumps(payload, ensure_ascii=False, indent=1)
            )
            tables_written += 1
            rows_written += len(payload)

        storage = get_storage()
        with_files = 0
        for key in sorted(files):
            try:
                data = await storage.get(key)
            except StorageError:
                data = None
            if data is None:
                continue
            archive.writestr(f"files/{key}", data)
            with_files += 1
        archive.writestr(
            "README.txt",
            io.StringIO(
                f"AskMira export of the workspace '{tenant.name}' ({tenant.id}).\n"
                f"Made {datetime.now(UTC):%Y-%m-%d %H:%M} UTC, before its scheduled purge.\n\n"
                f"data/  one JSON file per table: {tables_written} tables, {rows_written} rows.\n"
                f"files/ {with_files} uploaded files, at the storage key the rows refer to.\n\n"
                "Passwords, sign-in tokens and integration secrets are not included.\n"
            ).getvalue(),
        )

    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.EXPORT,
        entity_type="tenant",
        entity_id=tenant.id,
        actor_user_id=user.id,
        actor_type="platform",
        changes={"tables": tables_written, "rows": rows_written, "files": with_files},
        summary="Workspace data exported by the platform before its purge",
        request=request,
    )
    await session.commit()
    return handle.name, f"askmira-workspace-{tenant.id}.zip"


async def purge_workspace(
    session: AsyncSession, tenant_id: uuid.UUID, *, now: datetime | None = None
) -> dict[str, Any]:
    """Delete every row and file of a deleted workspace. Commits."""
    moment = now or datetime.now(UTC)
    tenant = await _deleted_tenant(session, tenant_id)
    if tenant.purged_at is not None:
        return {"already_purged": True}

    tables = _tenant_tables()
    files: set[str] = set(keys_in(tenant.logo_key, tenant.id))
    for table in tables:
        rows = (
            (await session.execute(select(table).where(tenant_filter(table, tenant.id))))
            .mappings()
            .all()
        )
        for row in rows:
            files |= keys_in(dict(row), tenant.id)

    removed: dict[str, int] = {}
    pending = list(tables)
    # Parents can be reached before a child that points at them across a cycle;
    # a table that refuses is tried again after the others, up to five rounds.
    for _ in range(5):
        if not pending:
            break
        refused = []
        for table in pending:
            try:
                async with session.begin_nested():
                    result = await session.execute(
                        delete(table).where(tenant_filter(table, tenant.id))
                    )
            except IntegrityError:
                refused.append(table)
                continue
            if result.rowcount:
                removed[table.name] = int(result.rowcount)
        pending = refused
    not_purged = sorted(table.name for table in pending)

    storage = get_storage()
    with contextlib.suppress(StorageError):
        files |= set(await storage.list_keys(f"{tenant.id}/"))
    deleted_files = failed_files = 0
    for key in sorted(files):
        try:
            if await storage.delete(key):
                deleted_files += 1
        except (StorageError, OSError):
            failed_files += 1

    tenant.name = "Deleted workspace"
    tenant.settings = {}
    tenant.enabled_modules = {}
    tenant.logo_key = None
    tenant.purged_at = moment
    summary = {
        "rows_deleted": sum(removed.values()),
        "tables": len(removed),
        "files_deleted": deleted_files,
        "files_not_deleted": failed_files,
        "tables_not_purged": not_purged,
        "kept": list(KEPT_TABLES),
    }
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.DELETE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor_type="system",
        changes=summary,
        summary=(
            f"Workspace data purged after its grace period: {summary['rows_deleted']} rows "
            f"from {summary['tables']} tables and {deleted_files} files. The audit trail is kept."
        ),
    )
    await session.commit()
    return summary


async def purge_due(session: AsyncSession, *, today: date | None = None) -> dict[str, Any]:
    """The nightly platform job: purge every deleted workspace past its date."""
    as_of = today or datetime.now(UTC).date()
    due = (
        await session.scalars(
            select(Tenant.id).where(
                Tenant.deleted_at.is_not(None),
                Tenant.purged_at.is_(None),
                Tenant.purge_after.is_not(None),
                Tenant.purge_after <= as_of,
            )
        )
    ).all()
    purged = 0
    for tenant_id in due:
        await purge_workspace(session, tenant_id)
        purged += 1
    return {"workspaces_purged": purged}


def purge_date(deleted_on: date) -> date:
    return deleted_on + timedelta(days=PURGE_GRACE_DAYS)
