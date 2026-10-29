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
lifecycle as-of view (`lifecycle.service.state_on`) uses. Two rules keep it
honest:

- A **first company** (somebody with none being given one) is filling in a
  missing value, not a move: it is never undone, so their earlier months
  belong to that company too. Otherwise putting an imported joiner into a
  company today would leave every month before today paid by nobody.
- A change may not be **back-dated before a later change that has already
  taken effect** (`check_no_later_change`): the history would contradict
  itself, and there would be no right answer for the months between. Each
  change's previous company is the one it actually replaced (the lifecycle
  sweep refreshes it when it applies a transfer).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.employee import Employee, EmploymentStatus
from app.models.lifecycle import LifecycleEvent, LifecycleStatus
from app.models.org import LegalEntity
from app.models.user import User

__all__ = [
    "CompanyMove",
    "check_no_later_change",
    "companies_on",
    "members_on",
    "moves_between",
    "unplaced_on",
]

_KEY = "legal_entity_id"


def _text(value: object) -> str | None:
    return None if value in (None, "") else str(value)


async def _changes_after(
    session: AsyncSession,
    user: User,
    on: date,
    *,
    employee_ids: set[uuid.UUID] | None = None,
    whole_workspace: bool = False,
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
    stmt = tenant_scoped(stmt, LifecycleEvent, user)
    if whole_workspace:
        stmt = stmt.execution_options(hr_scope_bypass=True)
    return list((await session.scalars(stmt)).all())


def _undo(current: dict[uuid.UUID, str | None], changes: list[LifecycleEvent]) -> None:
    """Walk `current` back through `changes` (newest first), in place.

    A first company (no company before it) is not undone: it applies from the
    start, so the months before it was given are that company's too.
    """
    for change in changes:
        previous = _text((change.previous_values or {}).get(_KEY))
        if previous is None:
            continue
        current[change.employee_id] = previous


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
    session: AsyncSession,
    user: User,
    legal_entity_id: uuid.UUID,
    on: date,
    *,
    whole_workspace: bool = False,
) -> set[uuid.UUID]:
    """Everybody who belongs to this company on `on`, whatever their record says today.

    `whole_workspace` looks past a department/location-scoped caller's view, for
    the check that their scope covers everybody a run would pay.
    """
    company = str(legal_entity_id)

    def scoped(stmt, model):
        stmt = tenant_scoped(stmt, model, user)
        return stmt.execution_options(hr_scope_bypass=True) if whole_workspace else stmt

    live = set(
        (
            await session.scalars(
                scoped(
                    select(Employee.id).where(Employee.legal_entity_id == legal_entity_id),
                    Employee,
                )
            )
        ).all()
    )
    changes = await _changes_after(session, user, on, whole_workspace=whole_workspace)
    if not changes:
        return live
    moved = {change.employee_id for change in changes}
    rows = await session.execute(
        scoped(
            select(Employee.id, Employee.legal_entity_id).where(Employee.id.in_(moved)), Employee
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
    # A first company is filling in a missing value, not a move: no notice.
    changes = [
        change
        for change in (await session.scalars(tenant_scoped(stmt, LifecycleEvent, user))).all()
        if _text((change.previous_values or {}).get(_KEY)) is not None
        and company
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


async def check_no_later_change(
    session: AsyncSession, user: User, employee_id: uuid.UUID, effective_from: date
) -> None:
    """Refuse a company change dated before a later one that has already taken effect.

    "X belongs to C from 1 November", recorded after "X moved to B on
    1 December" took effect, makes the history contradict itself. Date the
    change after the later one, or correct that one first.
    """
    later = await session.scalar(
        tenant_scoped(
            select(LifecycleEvent.effective_from)
            .where(
                LifecycleEvent.employee_id == employee_id,
                LifecycleEvent.status == LifecycleStatus.EFFECTED,
                LifecycleEvent.effective_from > effective_from,
                LifecycleEvent.new_values.has_key(_KEY),
            )
            .order_by(LifecycleEvent.effective_from.desc())
            .limit(1),
            LifecycleEvent,
            user,
        )
    )
    if later is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"They already changed company on {later:%d %b %Y}, after "
                f"{effective_from:%d %b %Y}. A company change cannot be dated before a later "
                "one that has taken effect: date it on or after that day."
            ),
        )


async def unplaced_on(
    session: AsyncSession, user: User, start: date, end: date
) -> list[str]:
    """Codes of people employed in the period who belong to no company on its last day.

    No run pays them; every run of the period says so, rather than leaving the
    gap to be found by the people who were not paid.
    """
    employed = and_(
        Employee.deleted_at.is_(None),
        or_(Employee.date_of_joining.is_(None), Employee.date_of_joining <= end),
        or_(
            Employee.status.in_(
                [EmploymentStatus.ACTIVE, EmploymentStatus.ON_NOTICE, EmploymentStatus.ON_LEAVE]
            ),
            and_(Employee.status == EmploymentStatus.EXITED, Employee.date_of_exit >= start),
        ),
    )
    moved_later = select(LifecycleEvent.employee_id).where(
        LifecycleEvent.status == LifecycleStatus.EFFECTED,
        LifecycleEvent.effective_from > end,
        LifecycleEvent.new_values.has_key(_KEY),
    )
    rows = (
        await session.execute(
            tenant_scoped(
                select(Employee.id, Employee.employee_code).where(
                    employed,
                    or_(
                        Employee.legal_entity_id.is_(None),
                        Employee.id.in_(tenant_scoped(moved_later, LifecycleEvent, user)),
                    ),
                ),
                Employee,
                user,
            )
        )
    ).all()
    if not rows:
        return []
    codes = {row.id: row.employee_code for row in rows}
    on_end = await companies_on(session, user, set(codes), end)
    return sorted(codes[person] for person, company in on_end.items() if company is None)
