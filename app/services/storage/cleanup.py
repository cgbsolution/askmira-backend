"""Deleting the files that belong to rows being deleted.

A row that points at an uploaded file — a profile photo, a payslip PDF, an ID
scan, a punch selfie — holds the file's storage key in a column. Erasure and
retention delete or blank such rows; without this, the file itself stayed in the
bucket, outside every later access export and erasure (DPDP s.8(7)).

Keys are recognised by shape rather than by column name, because they live in
columns with a dozen different names and sometimes inside JSON: every key this
product generates starts with the tenant's id, is a path of safe segments, and
ends in a file extension (`storage.base`). A key can only belong to the tenant
whose id starts it, so a row in one tenant can never cause another tenant's
file to be deleted.

Files are deleted **after** the database commit, by the caller: a rolled-back
erasure must not have removed a file the rows still point at. A file that
cannot be deleted is counted and reported, not retried forever.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import JSON, ColumnElement, String, Table, Text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter
from app.services.storage import StorageError, get_storage

__all__ = ["delete_files", "keys_in", "keys_in_rows"]

_SEGMENTS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*(/[A-Za-z0-9][A-Za-z0-9_\-.]*)+$")
_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}$")


def _is_key(value: str, prefix: str) -> bool:
    return (
        len(value) <= 600
        and value.startswith(prefix)
        and ".." not in value
        and bool(_SEGMENTS.match(value))
        and bool(_EXTENSION.search(value))
    )


def keys_in(value: Any, tenant_id: uuid.UUID) -> set[str]:
    """Every storage key of this tenant found in a value, however nested."""
    prefix = f"{tenant_id}/"
    found: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, str):
            if _is_key(item, prefix):
                found.add(item)
        elif isinstance(item, dict):
            for inner in item.values():
                walk(inner)
        elif isinstance(item, list | tuple | set):
            for inner in item:
                walk(inner)

    walk(value)
    return found


def _file_columns(table: Table) -> list[Any]:
    return [
        column for column in table.columns if isinstance(column.type, String | Text | JSON | JSONB)
    ]


async def keys_in_rows(
    session: AsyncSession,
    table: Table,
    tenant_id: uuid.UUID,
    where: ColumnElement[bool],
    *,
    only: Iterable[str] | None = None,
) -> set[str]:
    """The storage keys held by the rows of `table` matching `where`.

    `only` limits the search to those columns — for a row that is kept with a
    few fields cleared, where only the cleared fields' files should go.
    """
    wanted = set(only) if only is not None else None
    columns = [c for c in _file_columns(table) if wanted is None or c.name in wanted]
    if not columns:
        return set()
    # The tenant is re-applied here whatever `where` says: a key found in
    # another tenant's row must never reach `delete_files`.
    rows = (
        await session.execute(select(*columns).where(tenant_filter(table, tenant_id), where))
    ).all()
    found: set[str] = set()
    for row in rows:
        found |= keys_in(list(row), tenant_id)
    return found


async def delete_files(keys: Iterable[str]) -> dict[str, int]:
    """Delete each file; call after commit. Counts of removed and failed."""
    storage = get_storage()
    removed = failed = 0
    for key in sorted(set(keys)):
        try:
            if await storage.delete(key):
                removed += 1
        except (StorageError, OSError):
            failed += 1
    return {"files_deleted": removed, "files_not_deleted": failed}
