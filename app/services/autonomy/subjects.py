"""Applying and undoing an approved proposal on the record it is about. DB-facing.

A proposal names a subject (`subject_type`, `subject_id`) and a change. For the
subject types registered here, approving the proposal writes the change to that
record and captures the values it replaced, read from the record itself rather
than trusted from the proposal; reverting writes those values back.

Each type lists the only columns a proposal may touch. A proposal that names any
other column is refused at approval, so an approved proposal can never become a
route to changing, say, an employee's salary.

A subject type not registered here is approved and reverted as a record of the
decision only, and the response says the record itself was not touched.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import AttendanceRegularisation
from app.models.expenses import ExpenseClaim
from app.models.hiring import Candidate
from app.models.lifecycle import LifecycleEvent
from app.models.user import User

__all__ = ["SUBJECTS", "apply_change", "is_registered", "restore"]


@dataclass(frozen=True, slots=True)
class SubjectType:
    model: type
    columns: frozenset[str]


SUBJECTS: dict[str, SubjectType] = {
    "attendance_regularisation": SubjectType(
        AttendanceRegularisation, frozenset({"status", "decision_note"})
    ),
    "expense_claim": SubjectType(ExpenseClaim, frozenset({"status", "decision_note"})),
    "candidate": SubjectType(Candidate, frozenset({"stage"})),
    "lifecycle_event": SubjectType(LifecycleEvent, frozenset({"status", "cancelled_reason"})),
}


def is_registered(subject_type: str) -> bool:
    return subject_type in SUBJECTS


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, enum.Enum) else value


def _coerce(model: type, column: str, value: Any) -> Any:
    enum_class = getattr(model.__table__.c[column].type, "enum_class", None)
    if enum_class is not None and value is not None:
        try:
            return enum_class(value)
        except ValueError as exc:
            raise _bad_request(f"{value!r} is not a valid {column} for this record.") from exc
    return value


async def _load(session: AsyncSession, user: User, spec: SubjectType, subject_id: uuid.UUID):
    row = await session.scalar(
        tenant_scoped(select(spec.model).where(spec.model.id == subject_id), spec.model, user)
    )
    if row is None:
        raise _bad_request("The record this proposal is about no longer exists in this workspace.")
    return row


def _check_columns(spec: SubjectType, subject_type: str, keys: set[str]) -> None:
    stray = sorted(keys - spec.columns)
    if stray:
        raise _bad_request(
            f"A proposal about a {subject_type.replace('_', ' ')} may only change "
            f"{', '.join(sorted(spec.columns))}; it names {', '.join(stray)}."
        )


async def apply_change(
    session: AsyncSession,
    user: User,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    change: dict[str, Any],
) -> dict[str, Any] | None:
    """Write the change; return the values it replaced. None if not registered."""
    spec = SUBJECTS.get(subject_type)
    if spec is None:
        return None
    if not change:
        raise _bad_request("An approved proposal with no change has nothing to apply.")
    _check_columns(spec, subject_type, set(change))
    row = await _load(session, user, spec, subject_id)
    previous = {column: _plain(getattr(row, column)) for column in change}
    for column, value in change.items():
        setattr(row, column, _coerce(spec.model, column, value))
    return previous


async def restore(
    session: AsyncSession,
    user: User,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    previous: dict[str, Any],
) -> bool:
    """Write the captured values back. False if the type is not registered."""
    spec = SUBJECTS.get(subject_type)
    if spec is None or not previous:
        return False
    _check_columns(spec, subject_type, set(previous))
    row = await _load(session, user, spec, subject_id)
    for column, value in previous.items():
        setattr(row, column, _coerce(spec.model, column, value))
    return True
