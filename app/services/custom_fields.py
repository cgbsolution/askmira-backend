"""Custom fields on the employee record: the definitions, and checking values.

An admin defines a field once — a key, a label, a type — and every employee
record can then carry a value for it, shown on the profile and edited by HR.
The key and type never change after creation: records already hold values
under that key in that shape, and changing either would strand them. A field
that is no longer wanted is deactivated, not deleted; the values stay on the
records they were written to.

Every change to a definition is audited with the change, in one transaction.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import CustomFieldDefinition
from app.models.user import User
from app.schemas.custom_fields import CustomFieldCreate, CustomFieldUpdate, _options_fit
from app.services import audit

__all__ = [
    "MAX_TEXT",
    "check_values",
    "create_definition",
    "list_definitions",
    "update_definition",
]

ENTITY = "employee"
MAX_TEXT = 2000


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def list_definitions(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[CustomFieldDefinition]:
    """The definitions, in display order.

    Anybody may read the labels, since the profile is drawn from them, but only
    HR sees the sensitive ones: a person never reads a field kept from them, not
    even its name.
    """
    stmt = select(CustomFieldDefinition).where(CustomFieldDefinition.entity == ENTITY)
    hr = permissions.is_hr(user)
    if not (include_inactive and hr):
        stmt = stmt.where(CustomFieldDefinition.is_active.is_(True))
    if not hr:
        stmt = stmt.where(CustomFieldDefinition.is_sensitive.is_(False))
    stmt = stmt.order_by(CustomFieldDefinition.display_order, CustomFieldDefinition.label)
    return list((await session.scalars(tenant_scoped(stmt, CustomFieldDefinition, user))).all())


async def _get(
    session: AsyncSession, user: User, definition_id: uuid.UUID
) -> CustomFieldDefinition:
    row = await session.scalar(
        tenant_scoped(
            select(CustomFieldDefinition).where(CustomFieldDefinition.id == definition_id),
            CustomFieldDefinition,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Field not found")
    return row


async def create_definition(
    session: AsyncSession, user: User, payload: CustomFieldCreate, *, request: Request | None
) -> CustomFieldDefinition:
    taken = await session.scalar(
        tenant_scoped(
            select(CustomFieldDefinition.id).where(
                CustomFieldDefinition.entity == ENTITY,
                CustomFieldDefinition.key == payload.key,
            ),
            CustomFieldDefinition,
            user,
        )
    )
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A field with the key '{payload.key}' already exists. Reactivate it, or "
                "choose another key."
            ),
        )
    row = CustomFieldDefinition(tenant_id=user.tenant_id, entity=ENTITY, **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="custom_field_definition",
        entity_id=row.id,
        actor=user,
        changes={"after": payload.model_dump()},
        summary=f"Added the custom field '{row.label}' ({row.field_type})",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def update_definition(
    session: AsyncSession,
    user: User,
    definition_id: uuid.UUID,
    payload: CustomFieldUpdate,
    *,
    request: Request | None,
) -> CustomFieldDefinition:
    from app.schemas.custom_fields import clean_tab

    row = await _get(session, user, definition_id)
    changes = payload.model_dump(exclude_unset=True)
    for key in ("label", "is_required", "is_sensitive", "display_order", "is_active"):
        if key in changes and changes[key] is None:
            raise _unprocessable(f"'{key}' cannot be cleared.")
    if "tab" in changes:
        try:
            changes["tab"] = clean_tab(changes["tab"])
        except ValueError as exc:
            raise _unprocessable(str(exc)) from exc
    if "options" in changes:
        options = [o.strip() for o in (changes["options"] or []) if o.strip()] or None
        try:
            _options_fit(row.field_type, options)
        except ValueError as exc:
            raise _unprocessable(str(exc)) from exc
        changes["options"] = options
    diff = {
        key: {"from": getattr(row, key), "to": value}
        for key, value in changes.items()
        if getattr(row, key) != value
    }
    if not diff:
        return row
    for key, value in changes.items():
        setattr(row, key, value)
    summary = f"Changed the custom field '{row.label}'"
    if diff.get("is_active", {}).get("to") is False:
        summary = f"Deactivated the custom field '{row.label}'; values already saved are kept"
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="custom_field_definition",
        entity_id=row.id,
        actor=user,
        changes=diff,
        summary=summary,
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


def _check_one(definition: CustomFieldDefinition, value: Any) -> Any:
    """The value in its stored shape, or a 422 naming the field."""
    label = definition.label
    kind = definition.field_type
    if kind == "text":
        if not isinstance(value, str):
            raise _unprocessable(f"{label} must be text.")
        if len(value) > MAX_TEXT:
            raise _unprocessable(f"{label} is longer than {MAX_TEXT} characters.")
        return value.strip()
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _unprocessable(f"{label} must be a number.")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise _unprocessable(f"{label} must be yes or no.")
        return value
    if kind == "date":
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise _unprocessable(f"{label} must be a date (YYYY-MM-DD).") from exc
    options = definition.options or []
    if kind == "select":
        if value not in options:
            raise _unprocessable(f"{label} must be one of: {', '.join(options)}.")
        return value
    if kind == "multiselect":
        if not isinstance(value, list) or any(v not in options for v in value):
            raise _unprocessable(f"{label} must be chosen from: {', '.join(options)}.")
        return list(dict.fromkeys(value))
    raise _unprocessable(f"{label} has a type this version cannot store.")


async def check_values(
    session: AsyncSession,
    user: User,
    values: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check custom values against the definitions; return them in stored shape.

    Unknown keys are refused, and a required field left empty blocks the save
    (FS-CHR-01). A value already on the record under a field since deactivated
    may be sent back unchanged — the profile shows it, and saving the record
    must not fail because of it — but a new one may not be written.
    """
    definitions = {
        d.key: d
        for d in (
            await session.scalars(
                tenant_scoped(
                    select(CustomFieldDefinition).where(CustomFieldDefinition.entity == ENTITY),
                    CustomFieldDefinition,
                    user,
                )
            )
        ).all()
    }
    existing = existing or {}
    cleaned: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in values.items():
        definition = definitions.get(key)
        if definition is None or not definition.is_active:
            if key in existing and existing[key] == value:
                cleaned[key] = value
                continue
            unknown.append(key)
            continue
        if value in (None, "", []):
            continue
        cleaned[key] = _check_one(definition, value)
    if unknown:
        raise _unprocessable(f"Unknown custom field(s): {', '.join(sorted(unknown))}")

    missing = [
        d.label
        for d in definitions.values()
        if d.is_active and d.is_required and cleaned.get(d.key) in (None, "", [])
    ]
    if missing:
        raise _unprocessable(f"Required field(s) missing: {', '.join(missing)}")
    return cleaned
