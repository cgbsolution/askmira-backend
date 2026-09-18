"""Goals: who may see and change them, and the goal score an appraisal reads.

The arithmetic lives in `engine`; this module loads what it needs, decides who
may do what through `app.core.permissions`, and writes each change with its
audit row in one transaction.

Visibility, as the reporting line sees it:

- the owner always sees their own goals;
- `MANAGER_LINE` goals are seen by everybody above the owner in the reporting
  line and everybody who reports to the owner — a report has to see the goal
  they are aligning to;
- `COMPANY` goals are seen by everybody;
- HR sees everything.

Editing and recording progress: the owner, anybody above them in the line, and
HR. Company goals belong to HR.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.appraisal import AppraisalCycle
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.goals import (
    Goal,
    GoalCheckIn,
    GoalLevel,
    GoalStatus,
    GoalVisibility,
    KeyResult,
)
from app.models.user import User
from app.schemas.goals import (
    GoalCheckInCreate,
    GoalCheckInRead,
    GoalCreate,
    GoalRead,
    GoalScoreRead,
    GoalUpdate,
    KeyResultCreate,
    KeyResultRead,
)
from app.services import audit
from app.services.goals import engine
from app.services.performance import notices

__all__ = [
    "add_key_result",
    "alignable_parents",
    "check_in",
    "create_goal",
    "employee_goal_score",
    "get_goal",
    "goal_score_for",
    "list_check_ins",
    "list_goals",
    "update_goal",
]

MAX_LINE_DEPTH = permissions.MAX_REPORTING_DEPTH


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _d(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


# ── Loading ─────────────────────────────────────────────────────────────────


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == employee_id, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )
    if employee is None:
        raise _not_found("Employee not found")
    return employee


async def _goal(session: AsyncSession, user: User, goal_id: uuid.UUID) -> Goal:
    goal = await session.scalar(tenant_scoped(select(Goal).where(Goal.id == goal_id), Goal, user))
    if goal is None:
        raise _not_found("Goal not found")
    return goal


async def _key_results(
    session: AsyncSession, user: User, goal_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[KeyResult]]:
    if not goal_ids:
        return {}
    rows = (
        await session.scalars(
            tenant_scoped(
                select(KeyResult)
                .where(KeyResult.goal_id.in_(goal_ids))
                .order_by(KeyResult.display_order, KeyResult.created_at),
                KeyResult,
                user,
            )
        )
    ).all()
    grouped: dict[uuid.UUID, list[KeyResult]] = {gid: [] for gid in goal_ids}
    for row in rows:
        grouped.setdefault(row.goal_id, []).append(row)
    return grouped


async def _ancestors(
    session: AsyncSession, user: User, employee_id: uuid.UUID | None
) -> list[uuid.UUID]:
    """Everybody above this person in the reporting line, nearest first."""
    chain: list[uuid.UUID] = []
    current = employee_id
    for _ in range(MAX_LINE_DEPTH):
        if current is None:
            break
        manager_id = await session.scalar(
            tenant_scoped(select(Employee.manager_id).where(Employee.id == current), Employee, user)
        )
        if manager_id is None or manager_id == current or manager_id in chain:
            break
        if manager_id == employee_id:
            break
        chain.append(manager_id)
        current = manager_id
    return chain


def _facts(krs: list[KeyResult]) -> list[engine.KeyResultFacts]:
    return [
        engine.KeyResultFacts(
            key_result_id=str(kr.id),
            title=kr.title,
            kind=kr.kind.value,
            start=_d(kr.start_value),
            target=_d(kr.target_value),
            current=_d(kr.current_value),
            weight=_d(kr.weight),
        )
        for kr in krs
    ]


def _progress(krs: list[KeyResult]) -> Decimal:
    result = engine.goal_progress(_facts(krs))
    return result.progress if result.progress is not None else Decimal("0.00")


class _Viewer:
    """What one caller's reporting line looks like, computed once per request."""

    def __init__(self, user: User, below: set[uuid.UUID], above: list[uuid.UUID]) -> None:
        self.user = user
        self.is_hr = permissions.is_hr(user)
        self.below = below
        self.above = set(above)

    def can_view(self, goal: Goal) -> bool:
        if self.is_hr or goal.visibility is GoalVisibility.COMPANY:
            return True
        owner = goal.owner_employee_id
        if owner is None:
            return False
        if owner == self.user.employee_id:
            return True
        if goal.visibility is GoalVisibility.MANAGER_LINE:
            return owner in self.below or owner in self.above
        return False

    def can_edit(self, goal: Goal) -> bool:
        if self.is_hr:
            return True
        owner = goal.owner_employee_id
        if owner is None:
            return False
        return owner == self.user.employee_id or owner in self.below


async def _viewer(session: AsyncSession, user: User) -> _Viewer:
    below = await permissions.reporting_line(session, user)
    above = await _ancestors(session, user, user.employee_id)
    return _Viewer(user, below, above)


async def _names(
    session: AsyncSession, user: User, employee_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not employee_ids:
        return {}
    rows = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.id.in_(employee_ids)), Employee, user)
        )
    ).all()
    return {row.id: row.full_name for row in rows}


async def _read_many(
    session: AsyncSession, user: User, goals: list[Goal], viewer: _Viewer
) -> list[GoalRead]:
    krs = await _key_results(session, user, [g.id for g in goals])
    names = await _names(session, user, {g.owner_employee_id for g in goals if g.owner_employee_id})
    parent_ids = {g.parent_goal_id for g in goals if g.parent_goal_id}
    parents: dict[uuid.UUID, Goal] = {}
    if parent_ids:
        parents = {
            p.id: p
            for p in (
                await session.scalars(
                    tenant_scoped(select(Goal).where(Goal.id.in_(parent_ids)), Goal, user)
                )
            ).all()
        }
    out: list[GoalRead] = []
    for goal in goals:
        parent = parents.get(goal.parent_goal_id) if goal.parent_goal_id else None
        out.append(
            GoalRead(
                id=goal.id,
                owner_employee_id=goal.owner_employee_id,
                owner_name=names.get(goal.owner_employee_id) if goal.owner_employee_id else None,
                level=goal.level,
                title=goal.title,
                description=goal.description,
                weight=_d(goal.weight),
                start_date=goal.start_date,
                due_date=goal.due_date,
                status=goal.status,
                visibility=goal.visibility,
                parent_goal_id=goal.parent_goal_id,
                # A parent the caller may not see is named as hidden, not leaked.
                parent_title=(
                    parent.title
                    if parent is not None and viewer.can_view(parent)
                    else ("A goal you cannot see" if goal.parent_goal_id else None)
                ),
                appraisal_cycle_id=goal.appraisal_cycle_id,
                progress_percent=_d(goal.progress_percent),
                key_results=[
                    KeyResultRead(
                        id=kr.id,
                        title=kr.title,
                        kind=kr.kind,
                        unit=kr.unit,
                        start_value=_d(kr.start_value),
                        target_value=_d(kr.target_value),
                        current_value=_d(kr.current_value),
                        weight=_d(kr.weight),
                        progress=engine.key_result_progress(
                            kind=kr.kind.value,
                            start=_d(kr.start_value),
                            target=_d(kr.target_value),
                            current=_d(kr.current_value),
                        ),
                    )
                    for kr in krs.get(goal.id, [])
                ],
                created_at=goal.created_at,
                updated_at=goal.updated_at,
                can_edit=viewer.can_edit(goal) and goal.status is not GoalStatus.CANCELLED,
            )
        )
    return out


# ── Reading ─────────────────────────────────────────────────────────────────


async def list_goals(
    session: AsyncSession,
    user: User,
    *,
    scope: str = "mine",
    owner_employee_id: uuid.UUID | None = None,
    cycle_id: uuid.UUID | None = None,
    goal_status: GoalStatus | None = None,
) -> list[GoalRead]:
    """Goals the caller may see, narrowed by scope."""
    viewer = await _viewer(session, user)
    stmt = tenant_scoped(select(Goal), Goal, user).order_by(Goal.due_date, Goal.created_at)

    if owner_employee_id is not None:
        await _employee(session, user, owner_employee_id)
        await permissions.require_manager_or_hr(
            session,
            user,
            owner_employee_id,
            detail="You can see goals for yourself and for people in your reporting line.",
            allow_self=True,
        )
        stmt = stmt.where(Goal.owner_employee_id == owner_employee_id)
    elif scope == "mine":
        stmt = stmt.where(Goal.owner_employee_id == permissions.own_employee_id(user))
    elif scope == "team":
        if viewer.is_hr:
            stmt = stmt.where(Goal.owner_employee_id.isnot(None))
        elif not viewer.below:
            return []
        else:
            stmt = stmt.where(Goal.owner_employee_id.in_(viewer.below))
    elif scope == "company":
        stmt = stmt.where(Goal.level == GoalLevel.COMPANY)

    if cycle_id is not None:
        stmt = stmt.where(Goal.appraisal_cycle_id == cycle_id)
    if goal_status is not None:
        stmt = stmt.where(Goal.status == goal_status)

    rows = [g for g in (await session.scalars(stmt)).all() if viewer.can_view(g)]
    return await _read_many(session, user, rows, viewer)


async def get_goal(session: AsyncSession, user: User, goal_id: uuid.UUID) -> GoalRead:
    goal = await _goal(session, user, goal_id)
    viewer = await _viewer(session, user)
    if not viewer.can_view(goal):
        raise permissions.forbidden("This goal is not shared with you.")
    return (await _read_many(session, user, [goal], viewer))[0]


async def alignable_parents(
    session: AsyncSession, user: User, *, owner_employee_id: uuid.UUID | None = None
) -> list[GoalRead]:
    """Goals a new goal for this owner may align to: company goals, and goals
    owned by anybody above the owner in the reporting line."""
    owner = owner_employee_id or permissions.own_employee_id(user)
    await _employee(session, user, owner)
    await permissions.require_manager_or_hr(
        session, user, owner, detail="You can only set goals in your own line.", allow_self=True
    )
    above = await _ancestors(session, user, owner)
    viewer = await _viewer(session, user)
    conditions = [Goal.level == GoalLevel.COMPANY]
    if above:
        conditions.append(Goal.owner_employee_id.in_(above))
    rows = (
        await session.scalars(
            tenant_scoped(
                select(Goal).where(
                    or_(*conditions),
                    Goal.status.in_([GoalStatus.ACTIVE, GoalStatus.DRAFT]),
                ),
                Goal,
                user,
            ).order_by(Goal.level, Goal.title)
        )
    ).all()
    visible = [g for g in rows if g.visibility is not GoalVisibility.SELF or viewer.is_hr]
    return await _read_many(session, user, visible, viewer)


async def list_check_ins(
    session: AsyncSession, user: User, goal_id: uuid.UUID
) -> list[GoalCheckInRead]:
    goal = await _goal(session, user, goal_id)
    viewer = await _viewer(session, user)
    if not viewer.can_view(goal):
        raise permissions.forbidden("This goal is not shared with you.")
    rows = (
        await session.execute(
            tenant_scoped(
                select(GoalCheckIn, KeyResult.title, User.full_name)
                .outerjoin(KeyResult, KeyResult.id == GoalCheckIn.key_result_id)
                .outerjoin(User, User.id == GoalCheckIn.recorded_by_user_id)
                .where(GoalCheckIn.goal_id == goal.id)
                .order_by(GoalCheckIn.recorded_at.desc()),
                GoalCheckIn,
                user,
            )
        )
    ).all()
    return [
        GoalCheckInRead(
            id=row.id,
            key_result_id=row.key_result_id,
            key_result_title=kr_title,
            previous_value=_d(row.previous_value) if row.previous_value is not None else None,
            new_value=_d(row.new_value) if row.new_value is not None else None,
            progress_after=_d(row.progress_after),
            confidence=row.confidence,
            note=row.note,
            recorded_by_name=actor_name,
            recorded_at=row.recorded_at,
        )
        for row, kr_title, actor_name in rows
    ]


# ── Writing ─────────────────────────────────────────────────────────────────


async def _cycle_exists(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> None:
    found = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle.id).where(AppraisalCycle.id == cycle_id), AppraisalCycle, user
        )
    )
    if found is None:
        raise _not_found("Appraisal cycle not found")


async def _weight_in_use(
    session: AsyncSession,
    user: User,
    *,
    owner_employee_id: uuid.UUID,
    cycle_id: uuid.UUID | None,
    due_date: date,
    exclude: uuid.UUID | None = None,
) -> Decimal:
    """The weight this person's other live goals already carry in the same review."""
    stmt = tenant_scoped(
        select(Goal).where(
            Goal.owner_employee_id == owner_employee_id,
            Goal.status != GoalStatus.CANCELLED,
        ),
        Goal,
        user,
    )
    if cycle_id is not None:
        stmt = stmt.where(Goal.appraisal_cycle_id == cycle_id)
    else:
        stmt = stmt.where(Goal.appraisal_cycle_id.is_(None))
    rows = (await session.scalars(stmt)).all()
    return sum(
        (
            _d(g.weight)
            for g in rows
            if g.id != exclude and (cycle_id is not None or g.due_date.year == due_date.year)
        ),
        Decimal(0),
    )


async def _check_parent(
    session: AsyncSession,
    user: User,
    viewer: _Viewer,
    *,
    goal_id: uuid.UUID | None,
    owner_employee_id: uuid.UUID | None,
    parent_goal_id: uuid.UUID,
) -> Goal:
    parent = await _goal(session, user, parent_goal_id)
    if not viewer.can_view(parent):
        raise permissions.forbidden("You cannot align to a goal that is not shared with you.")
    if parent.status is GoalStatus.CANCELLED:
        raise _unprocessable("That goal has been cancelled, so nothing can align to it.")
    if parent.level is not GoalLevel.COMPANY:
        above = await _ancestors(session, user, owner_employee_id)
        if parent.owner_employee_id not in above:
            raise _unprocessable(
                "A goal aligns to a company goal or to a goal owned by somebody above its "
                "owner in the reporting line."
            )
    # Walk the chain upwards to refuse a loop.
    parents: dict[str, str | None] = {}
    current: Goal | None = parent
    for _ in range(engine.MAX_ALIGNMENT_DEPTH + 1):
        if current is None:
            break
        parents[str(current.id)] = str(current.parent_goal_id) if current.parent_goal_id else None
        if current.parent_goal_id is None or str(current.parent_goal_id) in parents:
            break
        current = await session.scalar(
            tenant_scoped(select(Goal).where(Goal.id == current.parent_goal_id), Goal, user)
        )
    try:
        engine.check_alignment(
            goal_id=str(goal_id) if goal_id else None,
            parent_id=str(parent.id),
            parents=parents,
        )
    except engine.GoalError as exc:
        raise _unprocessable(str(exc)) from exc
    return parent


def _validate_key_results(key_results: list[KeyResultCreate]) -> None:
    for kr in key_results:
        try:
            engine.validate_key_result(
                kind=kr.kind.value, start=kr.start_value, target=kr.target_value
            )
        except engine.GoalError as exc:
            raise _unprocessable(f"'{kr.title}': {exc}") from exc


async def create_goal(
    session: AsyncSession, user: User, payload: GoalCreate, *, request: Request | None = None
) -> GoalRead:
    viewer = await _viewer(session, user)
    owner_id: uuid.UUID | None
    if payload.level is GoalLevel.COMPANY:
        permissions.require_hr(user, detail="Company goals are set by HR.")
        owner_id = None
    else:
        if payload.owner_employee_id is None:
            owner_id = permissions.own_employee_id(user)
        else:
            owner_id = payload.owner_employee_id
            await _employee(session, user, owner_id)
            await permissions.require_manager_or_hr(
                session,
                user,
                owner_id,
                detail="You can set goals for yourself and for people in your reporting line.",
                allow_self=True,
            )
        if not payload.key_results:
            raise _unprocessable(
                "A goal needs at least one key result. Without one it has no measured "
                "progress and cannot count towards a review."
            )

    if payload.start_date and payload.start_date > payload.due_date:
        raise _unprocessable("The goal is due before it starts.")
    if payload.appraisal_cycle_id is not None:
        await _cycle_exists(session, user, payload.appraisal_cycle_id)
    _validate_key_results(payload.key_results)

    if owner_id is not None:
        in_use = await _weight_in_use(
            session,
            user,
            owner_employee_id=owner_id,
            cycle_id=payload.appraisal_cycle_id,
            due_date=payload.due_date,
        )
        try:
            engine.validate_weight_budget(existing=in_use, proposed=payload.weight)
        except engine.GoalError as exc:
            raise _unprocessable(str(exc)) from exc

    if payload.parent_goal_id is not None:
        await _check_parent(
            session,
            user,
            viewer,
            goal_id=None,
            owner_employee_id=owner_id,
            parent_goal_id=payload.parent_goal_id,
        )

    goal = Goal(
        tenant_id=user.tenant_id,
        owner_employee_id=owner_id,
        level=payload.level,
        title=payload.title,
        description=payload.description,
        weight=payload.weight,
        start_date=payload.start_date,
        due_date=payload.due_date,
        status=GoalStatus.ACTIVE,
        visibility=(
            GoalVisibility.COMPANY if payload.level is GoalLevel.COMPANY else payload.visibility
        ),
        parent_goal_id=payload.parent_goal_id,
        appraisal_cycle_id=payload.appraisal_cycle_id,
        created_by_user_id=user.id,
        progress_percent=Decimal(0),
    )
    session.add(goal)
    await session.flush()

    key_results = [
        KeyResult(
            tenant_id=user.tenant_id,
            goal_id=goal.id,
            title=kr.title,
            kind=kr.kind,
            unit=kr.unit,
            start_value=kr.start_value,
            target_value=kr.target_value,
            current_value=kr.start_value,
            weight=kr.weight,
            display_order=index,
        )
        for index, kr in enumerate(payload.key_results)
    ]
    session.add_all(key_results)
    await session.flush()
    goal.progress_percent = _progress(key_results)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="goal",
        entity_id=goal.id,
        actor=user,
        changes={
            "owner_employee_id": str(owner_id) if owner_id else None,
            "level": payload.level.value,
            "weight": str(payload.weight),
            "key_results": len(key_results),
            "parent_goal_id": str(payload.parent_goal_id) if payload.parent_goal_id else None,
        },
        summary=f"Created goal '{payload.title}'",
        request=request,
    )
    if owner_id is not None and owner_id != user.employee_id:
        recipients = await notices.user_ids_for_employees(session, user, [owner_id])
        await notices.send(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.goal.assigned",
            recipient_user_ids=recipients.values(),
            payload={"actor_name": user.full_name, "title": payload.title},
        )
    await session.commit()
    await session.refresh(goal)
    return (await _read_many(session, user, [goal], viewer))[0]


async def update_goal(
    session: AsyncSession,
    user: User,
    goal_id: uuid.UUID,
    payload: GoalUpdate,
    *,
    request: Request | None = None,
) -> GoalRead:
    goal = await _goal(session, user, goal_id)
    viewer = await _viewer(session, user)
    if not viewer.can_view(goal):
        raise permissions.forbidden("This goal is not shared with you.")
    if not viewer.can_edit(goal):
        raise permissions.forbidden(
            "Only the goal's owner, somebody above them in the reporting line, or HR can change it."
        )
    if goal.status is GoalStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This goal was cancelled. Create a new goal instead of reviving it.",
        )

    changes: dict[str, Any] = {}
    if payload.weight is not None and goal.owner_employee_id is not None:
        in_use = await _weight_in_use(
            session,
            user,
            owner_employee_id=goal.owner_employee_id,
            cycle_id=goal.appraisal_cycle_id,
            due_date=payload.due_date or goal.due_date,
            exclude=goal.id,
        )
        try:
            engine.validate_weight_budget(existing=in_use, proposed=payload.weight)
        except engine.GoalError as exc:
            raise _unprocessable(str(exc)) from exc
        changes["weight"] = {"from": str(goal.weight), "to": str(payload.weight)}
        goal.weight = payload.weight

    if payload.clear_parent and goal.parent_goal_id is not None:
        changes["parent_goal_id"] = {"from": str(goal.parent_goal_id), "to": None}
        goal.parent_goal_id = None
    elif payload.parent_goal_id is not None and payload.parent_goal_id != goal.parent_goal_id:
        await _check_parent(
            session,
            user,
            viewer,
            goal_id=goal.id,
            owner_employee_id=goal.owner_employee_id,
            parent_goal_id=payload.parent_goal_id,
        )
        changes["parent_goal_id"] = {
            "from": str(goal.parent_goal_id) if goal.parent_goal_id else None,
            "to": str(payload.parent_goal_id),
        }
        goal.parent_goal_id = payload.parent_goal_id

    for field in ("title", "description", "due_date"):
        value = getattr(payload, field)
        if value is not None and value != getattr(goal, field):
            changes[field] = {"from": str(getattr(goal, field)), "to": str(value)}
            setattr(goal, field, value)
    if goal.start_date and goal.start_date > goal.due_date:
        raise _unprocessable("The goal is due before it starts.")

    if payload.visibility is not None and goal.level is not GoalLevel.COMPANY:
        changes["visibility"] = {"from": goal.visibility.value, "to": payload.visibility.value}
        goal.visibility = payload.visibility

    if payload.status is not None and payload.status is not goal.status:
        changes["status"] = {"from": goal.status.value, "to": payload.status.value}
        goal.status = payload.status
        goal.completed_at = datetime.now(UTC) if payload.status is GoalStatus.COMPLETED else None

    if not changes:
        return (await _read_many(session, user, [goal], viewer))[0]

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="goal",
        entity_id=goal.id,
        actor=user,
        changes=changes,
        summary=f"Updated goal '{goal.title}'",
        request=request,
    )
    await session.commit()
    await session.refresh(goal)
    return (await _read_many(session, user, [goal], viewer))[0]


async def add_key_result(
    session: AsyncSession,
    user: User,
    goal_id: uuid.UUID,
    payload: KeyResultCreate,
    *,
    request: Request | None = None,
) -> GoalRead:
    goal = await _goal(session, user, goal_id)
    viewer = await _viewer(session, user)
    if not viewer.can_view(goal):
        raise permissions.forbidden("This goal is not shared with you.")
    if not viewer.can_edit(goal) or goal.status is GoalStatus.CANCELLED:
        raise permissions.forbidden("You cannot change this goal.")
    _validate_key_results([payload])

    existing = (await _key_results(session, user, [goal.id]))[goal.id]
    kr = KeyResult(
        tenant_id=user.tenant_id,
        goal_id=goal.id,
        title=payload.title,
        kind=payload.kind,
        unit=payload.unit,
        start_value=payload.start_value,
        target_value=payload.target_value,
        current_value=payload.start_value,
        weight=payload.weight,
        display_order=len(existing),
    )
    session.add(kr)
    await session.flush()
    goal.progress_percent = _progress([*existing, kr])

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="goal_key_result",
        entity_id=kr.id,
        actor=user,
        changes={"goal_id": str(goal.id), "title": payload.title},
        summary=f"Added key result '{payload.title}' to goal '{goal.title}'",
        request=request,
    )
    await session.commit()
    await session.refresh(goal)
    return (await _read_many(session, user, [goal], viewer))[0]


async def check_in(
    session: AsyncSession,
    user: User,
    goal_id: uuid.UUID,
    payload: GoalCheckInCreate,
    *,
    request: Request | None = None,
) -> GoalRead:
    """Record progress on one key result and recompute the goal, in one commit."""
    goal = await _goal(session, user, goal_id)
    viewer = await _viewer(session, user)
    if not viewer.can_view(goal):
        raise permissions.forbidden("This goal is not shared with you.")
    if not viewer.can_edit(goal):
        raise permissions.forbidden(
            "Progress is recorded by the goal's owner, somebody above them in the line, or HR."
        )
    if goal.status is not GoalStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This goal is {goal.status.value}; progress is recorded on active goals.",
        )

    key_results = (await _key_results(session, user, [goal.id]))[goal.id]
    target = next((kr for kr in key_results if kr.id == payload.key_result_id), None)
    if target is None:
        raise _not_found("That key result is not part of this goal.")

    previous = _d(target.current_value)
    target.current_value = payload.value
    progress = _progress(key_results)
    goal.progress_percent = progress

    row = GoalCheckIn(
        tenant_id=user.tenant_id,
        goal_id=goal.id,
        key_result_id=target.id,
        previous_value=previous,
        new_value=payload.value,
        progress_after=progress,
        confidence=payload.confidence,
        note=payload.note,
        recorded_by_user_id=user.id,
        recorded_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="goal_check_in",
        entity_id=row.id,
        actor=user,
        changes={
            "goal_id": str(goal.id),
            "key_result_id": str(target.id),
            "previous_value": str(previous),
            "new_value": str(payload.value),
            "progress_after": str(progress),
        },
        summary=f"Checked in on '{target.title}': {previous} to {payload.value}",
        request=request,
    )
    await session.commit()
    await session.refresh(goal)
    return (await _read_many(session, user, [goal], viewer))[0]


# ── The goal score ──────────────────────────────────────────────────────────


async def goal_score_for(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID, cycle: AppraisalCycle
) -> engine.GoalScore:
    """The goal component of one person's review.

    Counts goals linked to the cycle, plus unlinked goals due inside the cycle's
    review period. Authorisation is the caller's: this is read by the appraisal
    service once it has decided the caller may see the review.
    """
    goals = (
        await session.scalars(
            tenant_scoped(
                select(Goal).where(
                    Goal.owner_employee_id == employee_id,
                    or_(
                        Goal.appraisal_cycle_id == cycle.id,
                        (Goal.appraisal_cycle_id.is_(None))
                        & (Goal.due_date >= cycle.period_start)
                        & (Goal.due_date <= cycle.period_end),
                    ),
                ),
                Goal,
                user,
            ).order_by(Goal.due_date)
        )
    ).all()
    krs = await _key_results(session, user, [g.id for g in goals])
    return engine.goal_score(
        [
            engine.GoalFacts(
                goal_id=str(g.id),
                title=g.title,
                status=g.status.value,
                weight=_d(g.weight),
                key_results=_facts(krs.get(g.id, [])),
            )
            for g in goals
        ]
    )


async def goal_scores_for_many(
    session: AsyncSession,
    user: User,
    *,
    employee_ids: list[uuid.UUID],
    cycle: AppraisalCycle,
) -> dict[uuid.UUID, engine.GoalScore]:
    """The goal score for many people in two queries, for the calibration grid."""
    if not employee_ids:
        return {}
    goals = (
        await session.scalars(
            tenant_scoped(
                select(Goal).where(
                    Goal.owner_employee_id.in_(employee_ids),
                    or_(
                        Goal.appraisal_cycle_id == cycle.id,
                        (Goal.appraisal_cycle_id.is_(None))
                        & (Goal.due_date >= cycle.period_start)
                        & (Goal.due_date <= cycle.period_end),
                    ),
                ),
                Goal,
                user,
            )
        )
    ).all()
    krs = await _key_results(session, user, [g.id for g in goals])
    by_owner: dict[uuid.UUID, list[Goal]] = {}
    for goal in goals:
        if goal.owner_employee_id is not None:
            by_owner.setdefault(goal.owner_employee_id, []).append(goal)
    return {
        owner: engine.goal_score(
            [
                engine.GoalFacts(
                    goal_id=str(g.id),
                    title=g.title,
                    status=g.status.value,
                    weight=_d(g.weight),
                    key_results=_facts(krs.get(g.id, [])),
                )
                for g in owned
            ]
        )
        for owner, owned in by_owner.items()
    }


async def employee_goal_score(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID, cycle_id: uuid.UUID
) -> GoalScoreRead:
    await _employee(session, user, employee_id)
    await permissions.require_manager_or_hr(
        session,
        user,
        employee_id,
        detail="Goal scores are visible to the person, their reporting line and HR.",
        allow_self=True,
    )
    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == cycle_id), AppraisalCycle, user
        )
    )
    if cycle is None:
        raise _not_found("Appraisal cycle not found")
    result = await goal_score_for(session, user, employee_id=employee_id, cycle=cycle)
    return GoalScoreRead(
        employee_id=employee_id,
        cycle_id=cycle_id,
        score=result.score,
        measured_weight_percent=result.measured_weight_percent,
        contributing=result.contributing,
        excluded=result.excluded,
        notes=result.notes,
    )
