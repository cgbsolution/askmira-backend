"""A sub-admin's department and location scope, enforced on every ORM query.

Screen-by-screen filtering would leave a hole in whichever screen was missed.
So once a request's caller is known to be scoped, `get_current_user` puts the
employee ids they cover in a context variable, and every ORM SELECT, UPDATE and
DELETE in that request is narrowed: `Employee` rows to those ids, and every
model with an `employee_id` to rows about those people (or about nobody — a
template, a policy). The caller's own records stay visible: a scoped admin is
still an employee who applies for leave.

Jobs, scripts and tests never set the variable, so they are never narrowed.
Raw SQL and Core-table queries are not narrowed by this; the report compiler,
which builds Core queries, narrows through its own scope instead.
"""

from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass
from functools import cache

from sqlalchemy import event, or_
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

__all__ = ["HrScope", "current_scope", "set_scope"]


@dataclass(frozen=True, slots=True)
class HrScope:
    #: Everybody the caller may act on, their own record included.
    employee_ids: tuple[uuid.UUID, ...]


_CURRENT: contextvars.ContextVar[HrScope | None] = contextvars.ContextVar(
    "askmira_hr_scope", default=None
)


def set_scope(scope: HrScope | None) -> None:
    _CURRENT.set(scope)


def current_scope() -> HrScope | None:
    return _CURRENT.get()


@cache
def _employee_keyed() -> tuple[type, ...]:
    from app.models.base import Base

    return tuple(
        mapper.class_
        for mapper in Base.registry.mappers
        if mapper.class_.__name__ != "Employee" and "employee_id" in mapper.columns
    )


@event.listens_for(Session, "do_orm_execute")
def _narrow(state: ORMExecuteState) -> None:
    scope = _CURRENT.get()
    if scope is None or not (state.is_select or state.is_update or state.is_delete):
        return
    from app.models.employee import Employee

    allowed = scope.employee_ids
    options = [
        with_loader_criteria(
            Employee, lambda cls: cls.id.in_(allowed), include_aliases=True
        )
    ]
    for model in _employee_keyed():
        options.append(
            with_loader_criteria(
                model,
                lambda cls: or_(cls.employee_id.is_(None), cls.employee_id.in_(allowed)),
                include_aliases=True,
            )
        )
    state.statement = state.statement.options(*options)
