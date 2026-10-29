"""Which company a person belongs to on a date: what decides who pays a month.

An employee carries one company at a time (`Employee.legal_entity_id`), and a
move between the companies of a group is filed in their lifecycle history with
its effective date: a transfer approved for 1 October, a move made today from
the directory, a change back-dated on their record. The record itself only
says where they are *now*.

Payroll asks who was in a company on the last day of the period, not who is in
it now. Otherwise a transfer dated 1 October, applied by the morning sweep,
would take the person out of the old company's September run (computed on
3 October) and put the whole of September on the new company's books.

The rule, said the same way on every screen: **one company pays a month — the
one the person belongs to on its last day.** A month is never split between
two companies; a move dated the 1st of a month changes companies cleanly.
Annual figures (statutory bonus, the TDS return's salary detail) follow the
company on the last day of the year.

The company on a date is the current record with every effected company
change dated after that day undone, newest first — the same reconstruction the
lifecycle as-of view (`lifecycle.service.state_on`) uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.lifecycle import LifecycleEvent, LifecycleStatus
from app.models.org import LegalEntity
from app.models.user import User

__all__ = ["CompanyMove", "companies_on", "members_on", "moves_between"]

_KEY = "legal_entity_id"


def _text(value: object) -> str | None:
    return None if value in (None, "") else str(value)


async def _changes_after(
    session: AsyncSession, user: User, on: date, *, employee_ids: set[uuid.UUID] | None = None
) -> list[LifecycleEvent]:
    """Effected company changes dated after `on`, newest first."""
    stmt = select(LifecycleEvent).where(
        LifecycleEvent.status == LifecycleStatus.EFFECTED,
        LifecycleEvent.effective_from > on,
        LifecycleEvent.new_values.has_key(_KEY),
    )
    if employee_ids is not None:
        stmt = stmt.where(LifecycleEvent.employee_id.in_(employee_ids))
    stmt = stmt.order_by(
        LifecycleEvent.effective_from.desc(),
        LifecycleEvent.effected_at.desc().nulls_last(),
        LifecycleEvent.created_at.desc(),
    )
    return list((await session.scalars(tenant_scoped(stmt, LifecycleEvent, user))).all())


def _undo(current: dict[uuid.UUID, str | None], changes: list[LifecycleEvent]) -> None:
    """Walk `current` back through `changes` (newest first), in place."""
    for change in changes:
        current[change.employee_id] = _text((change.previous_values or {}).get(_KEY))


async def companies_on(
    session: AsyncSession, user: User, employee_ids: set[uuid.UUID], on: date
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Each person's company on `on` (None for somebody in no company then)."""
    if not employee_ids:
        return {}
    rows = await session.execute(
        tenant_scoped(
            select(Employee.id, Employee.legal_entity_id).where(Employee.id.in_(employee_ids)),
            Employee,
            user,
        )
    )
    state = {row.id: _text(row.legal_entity_id) for row in rows}
    _undo(state, await _changes_after(session, user, on, employee_ids=set(state)))
    return {key: uuid.UUID(value) if value else None for key, value in state.items()}


async def members_on(
    session: AsyncSession, user: User, legal_entity_id: uuid.UUID, on: date
) -> set[uuid.UUID]:
    """Everybody who belongs to this company on `on`, whatever their record says today."""
    company = str(legal_entity_id)
    live = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(Employee.id).where(Employee.legal_entity_id == legal_entity_id),
                    Employee,
                    user,
                )
            )
        ).all()
    )
    changes = await _changes_after(session, user, on)
    if not changes:
        return live
    moved = {change.employee_id for change in changes}
    rows = await session.execute(
        tenant_scoped(
            select(Employee.id, Employee.legal_entity_id).where(Employee.id.in_(moved)),
            Employee,
            user,
        )
    )
    state = {row.id: _text(row.legal_entity_id) for row in rows}
    _undo(state, [change for change in changes if change.employee_id in state])
    return (live - moved) | {person for person, value in state.items() if value == company}


@dataclass(frozen=True, slots=True)
class CompanyMove:
    """One move between companies inside a period, as a run explains it."""

    employee_id: uuid.UUID
    on: date
    from_id: str | None
    to_id: str | None
    from_company: str
    to_company: str


async def moves_between(
    session: AsyncSession, user: User, legal_entity_id: uuid.UUID, start: date, end: date
) -> list[CompanyMove]:
    """Moves into or out of this company dated inside the period, oldest first."""
    company = str(legal_entity_id)
    stmt = (
        select(LifecycleEvent)
        .where(
            LifecycleEvent.status == LifecycleStatus.EFFECTED,
            LifecycleEvent.effective_from >= start,
            LifecycleEvent.effective_from <= end,
            LifecycleEvent.new_values.has_key(_KEY),
        )
        .order_by(LifecycleEvent.effective_from, LifecycleEvent.created_at)
    )
    changes = [
        change
        for change in (await session.scalars(tenant_scoped(stmt, LifecycleEvent, user))).all()
        if company
        in (_text((change.previous_values or {}).get(_KEY)), _text(change.new_values.get(_KEY)))
    ]
    if not changes:
        return []
    names = {
        str(row.id): row.name
        for row in (
            await session.execute(
                tenant_scoped(select(LegalEntity.id, LegalEntity.name), LegalEntity, user)
            )
        ).all()
    }

    def name(value: object) -> str:
        text = _text(value)
        return names.get(text, "another company") if text else "no company"

    return [
        CompanyMove(
            employee_id=change.employee_id,
            on=change.effective_from,
            from_id=_text((change.previous_values or {}).get(_KEY)),
            to_id=_text(change.new_values.get(_KEY)),
            from_company=name((change.previous_values or {}).get(_KEY)),
            to_company=name(change.new_values.get(_KEY)),
        )
        for change in changes
    ]
