"""The actual rows held about one person, across every module. DB-facing.

The classification in `schema_map.py` says which tables hold whose personal
data. This module turns that into the contents of an access export: for every
classified table that carries a link to the person — their employee record, or
their login — it reads the rows that are theirs, inside their tenant, and
nothing else.

Two things are never exported, even to the person themselves: credential
material (password and token hashes, encrypted secrets) and one-time protocol
state. They are named in the export as withheld, with the reason, rather than
silently dropped.

A table whose rows reach the person only through another table (a settlement
line through its settlement) is not traversed. Its parent is exported, and the
export's completeness note says so.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Table, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 — registers every table on the metadata before it is walked
from app.models.base import Base
from app.models.user import User
from app.services.dpdp.categories import CATEGORIES
from app.services.dpdp.schema_map import TABLES

__all__ = ["ROW_CAP", "collect_personal_data", "person_predicate", "withheld_column"]

#: Rows per table in one export. A person with more attendance days than this
#: gets the first thousand and a `truncated` flag, never a silent cut.
ROW_CAP = 1000

_WITHHELD_NAMES = frozenset({"password_hash", "token_hash", "code_verifier", "nonce", "state"})
_WITHHELD_SUFFIXES = ("_hash", "_encrypted")

WITHHELD_REASON = (
    "Credential material — password and token hashes, encrypted secrets and one-time "
    "sign-in state — is not exported to anybody, including the person it belongs to. "
    "It identifies nothing about you and would weaken your account if a copy leaked."
)


def withheld_column(name: str) -> bool:
    return name in _WITHHELD_NAMES or name.endswith(_WITHHELD_SUFFIXES)


def person_predicate(
    table: Table, *, employee_id: uuid.UUID | None, user_id: uuid.UUID | None
) -> ColumnElement | None:
    """The condition that selects this person's rows in `table`, or None."""
    if table.name == "employees":
        return table.c.id == employee_id if employee_id is not None else None
    if table.name == "users":
        return table.c.id == user_id if user_id is not None else None
    if "employee_id" in table.c and employee_id is not None:
        return table.c.employee_id == employee_id
    for key in ("user_id", "recipient_user_id"):
        if key in table.c and user_id is not None:
            return table.c[key] == user_id
    return None


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, list | tuple | set):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, bytes | bytearray | memoryview):
        return None
    return str(value)


def personal_tables() -> list[Table]:
    """Classified tables that hold somebody's personal data and carry a tenant."""
    out = []
    for table in Base.metadata.sorted_tables:
        classification = TABLES.get(table.name)
        if classification is None or classification.principal is None:
            continue
        if "tenant_id" not in table.c:
            continue
        out.append(table)
    return out


async def collect_personal_data(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    row_cap: int = ROW_CAP,
) -> list[dict[str, Any]]:
    """Every row held about this person, grouped by table, in the caller's tenant."""
    sections: list[dict[str, Any]] = []
    for table in personal_tables():
        predicate = person_predicate(table, employee_id=employee_id, user_id=user_id)
        if predicate is None:
            continue
        stmt = (
            select(table)
            .where(table.c.tenant_id == user.tenant_id, predicate)
            .limit(row_cap + 1)
        )
        rows = (await session.execute(stmt)).mappings().all()
        if not rows:
            continue

        classification = TABLES[table.name]
        exported = [c.name for c in table.columns if not withheld_column(c.name)]
        withheld = [c.name for c in table.columns if withheld_column(c.name)]
        category_keys = sorted(
            {
                key
                for key in (classification.category_for(name) for name in exported)
                if key and key in CATEGORIES
            }
        )
        sections.append(
            {
                "table": table.name,
                "label": table.name.replace("_", " ").capitalize(),
                "categories": [
                    {"key": key, "name": CATEGORIES[key].name} for key in category_keys
                ],
                "row_count": min(len(rows), row_cap),
                "truncated": len(rows) > row_cap,
                "withheld_columns": withheld,
                "rows": [{name: _plain(row[name]) for name in exported} for row in rows[:row_cap]],
            }
        )
    return sections
