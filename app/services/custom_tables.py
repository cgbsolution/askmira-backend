"""Custom tables: definitions by an admin, rows per person.

Columns are typed exactly as custom fields are, and a row's values are checked
column by column with the same rules. A table's key and its columns' keys and
types are fixed once rows exist; labels, options, order and the flags may
change. HR reads and writes every row; the person reads non-sensitive tables
about themselves and writes rows only where the table allows it.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.custom_tables import CustomTableDefinition, CustomTableRow
from app.models.employee import CustomFieldDefinition, Employee
from app.models.user import User
from app.schemas.custom_fields import KEY, FieldType, _options_fit
from app.services import audit
from app.services.custom_fields import _check_one

__all__ = [
    "ColumnIn",
    "TableIn",
    "TableUpdate",
    "add_row",
    "create_table",
    "delete_row",
    "list_tables",
    "tables_for",
    "update_row",
    "update_table",
]


class ColumnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=KEY)
    label: str = Field(min_length=1, max_length=120)
    field_type: FieldType
    options: list[str] | None = Field(default=None, max_length=100)
    is_required: bool = False

    @model_validator(mode="after")
    def _options(self) -> ColumnIn:
        if self.options is not None:
            self.options = [o.strip() for o in self.options if o.strip()]
        _options_fit(self.field_type, self.options)
        return self


class TableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=KEY)
    label: str = Field(min_length=2, max_length=160)
    columns: list[ColumnIn] = Field(min_length=1, max_length=20)
    is_sensitive: bool = False
    employee_editable: bool = False
    display_order: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def _unique(self) -> TableIn:
        if len({c.key for c in self.columns}) != len(self.columns):
            raise ValueError("Each column needs its own key.")
        return self


class TableUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=2, max_length=160)
    columns: list[ColumnIn] | None = Field(default=None, min_length=1, max_length=20)
    is_sensitive: bool | None = None
    employee_editable: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=10_000)
    active: bool | None = None


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _definition_read(row: CustomTableDefinition) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "key": row.key,
        "label": row.label,
        "columns": list(row.columns or []),
        "is_sensitive": row.is_sensitive,
        "employee_editable": row.employee_editable,
        "display_order": row.display_order,
        "active": row.active,
    }


async def _table(session: AsyncSession, user: User, table_id: uuid.UUID) -> CustomTableDefinition:
    row = await session.scalar(
        tenant_scoped(
            select(CustomTableDefinition).where(CustomTableDefinition.id == table_id),
            CustomTableDefinition,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Table not found")
    return row


async def list_tables(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    stmt = select(CustomTableDefinition).order_by(
        CustomTableDefinition.display_order, CustomTableDefinition.label
    )
    hr = permissions.is_hr(user)
    if not (include_inactive and hr):
        stmt = stmt.where(CustomTableDefinition.active.is_(True))
    if not hr:
        stmt = stmt.where(CustomTableDefinition.is_sensitive.is_(False))
    rows = (await session.scalars(tenant_scoped(stmt, CustomTableDefinition, user))).all()
    return [_definition_read(r) for r in rows]


async def create_table(
    session: AsyncSession, user: User, payload: TableIn, *, request: Request | None
) -> dict[str, Any]:
    taken = await session.scalar(
        tenant_scoped(
            select(CustomTableDefinition.id).where(CustomTableDefinition.key == payload.key),
            CustomTableDefinition,
            user,
        )
    )
    clash = await session.scalar(
        tenant_scoped(
            select(CustomFieldDefinition.id).where(CustomFieldDefinition.key == payload.key),
            CustomFieldDefinition,
            user,
        )
    )
    if taken is not None or clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The key '{payload.key}' is already used by a custom field or table.",
        )
    values = payload.model_dump()
    row = CustomTableDefinition(tenant_id=user.tenant_id, **values)
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="custom_table_definition",
        entity_id=row.id,
        actor=user,
        changes={"after": _definition_read(row)},
        summary=f"Added the custom table '{row.label}'",
        request=request,
    )
    await session.commit()
    return _definition_read(row)


async def update_table(
    session: AsyncSession,
    user: User,
    table_id: uuid.UUID,
    payload: TableUpdate,
    *,
    request: Request | None,
) -> dict[str, Any]:
    row = await _table(session, user, table_id)
    before = _definition_read(row)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("columns") is not None:
        has_rows = await session.scalar(
            tenant_scoped(
                select(func.count())
                .select_from(CustomTableRow)
                .where(CustomTableRow.table_id == row.id),
                CustomTableRow,
                user,
            )
        )
        old = {c["key"]: c for c in row.columns or []}
        new = {c["key"]: c for c in changes["columns"]}
        if has_rows:
            for key, column in old.items():
                if key not in new:
                    raise _unprocessable(
                        f"Column '{column['label']}' holds values; it cannot be removed. "
                        "Switch the table off instead."
                    )
                if new[key]["field_type"] != column["field_type"]:
                    raise _unprocessable(
                        f"Column '{column['label']}' holds values; its type cannot change."
                    )
    for key, value in changes.items():
        if value is None and key in (
            "label",
            "columns",
            "is_sensitive",
            "employee_editable",
            "display_order",
            "active",
        ):
            continue
        setattr(row, key, value)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="custom_table_definition",
        entity_id=row.id,
        actor=user,
        changes={"before": before, "after": _definition_read(row)},
        summary=f"Changed the custom table '{row.label}'",
        request=request,
    )
    await session.commit()
    return _definition_read(row)


# ── Rows ────────────────────────────────────────────────────────────────────


class _Column:
    """Adapts a column dict to what `custom_fields._check_one` reads."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.key = spec["key"]
        self.label = spec["label"]
        self.field_type = spec["field_type"]
        self.options = spec.get("options")
        self.is_required = bool(spec.get("is_required"))


def _clean(table: CustomTableDefinition, values: dict[str, Any]) -> dict[str, Any]:
    columns = {c["key"]: _Column(c) for c in table.columns or []}
    unknown = set(values) - set(columns)
    if unknown:
        raise _unprocessable(f"Unknown column(s): {', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    for key, column in columns.items():
        value = values.get(key)
        if value in (None, "", []):
            if column.is_required:
                raise _unprocessable(f"{column.label} is required.")
            continue
        cleaned[key] = _check_one(column, value)  # type: ignore[arg-type]
    return cleaned


async def _may_write(
    session: AsyncSession, user: User, table: CustomTableDefinition, employee_id: uuid.UUID
) -> None:
    if permissions.is_hr(user):
        return
    if user.employee_id == employee_id and table.employee_editable and not table.is_sensitive:
        return
    raise permissions.forbidden("HR keeps this table.")


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    found = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return found


async def tables_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Each table with this person's rows, as the caller may see them."""
    hr = permissions.is_hr(user)
    if not hr and user.employee_id != employee_id:
        raise permissions.forbidden("Custom tables are for the person and HR.")
    await _employee(session, user, employee_id)
    tables = await list_tables(session, user)
    if not tables:
        return []
    rows = (
        await session.scalars(
            tenant_scoped(
                select(CustomTableRow)
                .where(CustomTableRow.employee_id == employee_id)
                .order_by(CustomTableRow.created_at),
                CustomTableRow,
                user,
            )
        )
    ).all()
    by_table: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_table.setdefault(str(r.table_id), []).append(
            {"id": str(r.id), "values": dict(r.values or {})}
        )
    return [
        {
            **t,
            "rows": by_table.get(t["id"], []),
            "can_edit": hr or (t["employee_editable"] and not t["is_sensitive"]),
        }
        for t in tables
    ]


async def add_row(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    table_id: uuid.UUID,
    values: dict[str, Any],
    *,
    request: Request | None,
) -> dict[str, Any]:
    table = await _table(session, user, table_id)
    if not table.active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This table is off.")
    if not permissions.is_hr(user):
        await _may_write(session, user, table, employee_id)
    employee = await _employee(session, user, employee_id)
    row = CustomTableRow(
        tenant_id=user.tenant_id,
        table_id=table.id,
        employee_id=employee.id,
        values=_clean(table, values),
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="custom_table_row",
        entity_id=row.id,
        actor=user,
        changes={"table": table.key, "values": row.values},
        summary=f"Added a row to '{table.label}' for {employee.employee_code}",
        request=request,
    )
    await session.commit()
    return {"id": str(row.id), "values": row.values}


async def _row(session: AsyncSession, user: User, row_id: uuid.UUID) -> CustomTableRow:
    row = await session.scalar(
        tenant_scoped(
            select(CustomTableRow).where(CustomTableRow.id == row_id), CustomTableRow, user
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Row not found")
    return row


async def update_row(
    session: AsyncSession,
    user: User,
    row_id: uuid.UUID,
    values: dict[str, Any],
    *,
    request: Request | None,
) -> dict[str, Any]:
    row = await _row(session, user, row_id)
    table = await _table(session, user, row.table_id)
    if not permissions.is_hr(user):
        await _may_write(session, user, table, row.employee_id)
    before = dict(row.values or {})
    row.values = _clean(table, values)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="custom_table_row",
        entity_id=row.id,
        actor=user,
        changes={"before": before, "after": row.values},
        summary=f"Changed a row in '{table.label}'",
        request=request,
    )
    await session.commit()
    return {"id": str(row.id), "values": row.values}


async def delete_row(
    session: AsyncSession, user: User, row_id: uuid.UUID, *, request: Request | None
) -> None:
    row = await _row(session, user, row_id)
    table = await _table(session, user, row.table_id)
    if not permissions.is_hr(user):
        await _may_write(session, user, table, row.employee_id)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="custom_table_row",
        entity_id=row.id,
        actor=user,
        changes={"table": table.key, "values": dict(row.values or {})},
        summary=f"Removed a row from '{table.label}'",
        request=request,
    )
    await session.delete(row)
    await session.commit()
