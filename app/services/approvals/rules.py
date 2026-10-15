"""Approval chains from the tenant's rules, delegation, and escalation.

**Resolving a chain.** The first active rule for the process whose conditions
all hold (by priority) gives the steps; with none, the built-in step — the
line manager — stands. Each step is resolved to a login at submission:

- ``line_manager`` — the employee's manager; ``skip_level`` — their manager's
  manager; ``user`` — a named person; ``hr`` — anybody in HR.
- A step nobody can act on (no manager, a manager with no login, a deactivated
  named user) is skipped, and said so — a request routed to an empty desk
  waits forever. A step that would land on the requester is skipped too.
- Two consecutive steps on the same person collapse into one.

**Delegation.** An approver away for a period hands their approvals to
somebody; the delegate may decide whatever the approver could, and the record
shows who actually did.

**Escalation.** A rule may say a step waiting longer than N hours goes up a
level: to the approver's own manager, or to HR. The sweep runs hourly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.approval_rules import ApprovalDelegation, ApprovalProcess, ApprovalRule
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.user import User
from app.services import audit

__all__ = [
    "STEP_KINDS",
    "Facts",
    "ResolvedStep",
    "acting_for",
    "attach",
    "cancel_delegation",
    "create_delegation",
    "create_rule",
    "decide",
    "delegate_options",
    "delete_rule",
    "escalate_due",
    "list_delegations",
    "list_rules",
    "matches",
    "may_decide",
    "next_recipients",
    "resolve_steps",
    "update_rule",
]

STEP_KINDS = ("line_manager", "skip_level", "user", "hr")

#: The step a process falls back to when no rule matches. A lifecycle change
#: has always been HR's to approve; everything else starts with the manager.
DEFAULT_STEPS: dict[ApprovalProcess, list[dict[str, Any]]] = {
    ApprovalProcess.LIFECYCLE: [{"kind": "hr"}],
}

#: What a rule's category condition holds, by process, for validation.
CATEGORY_PROCESSES = (
    ApprovalProcess.EXPENSE,
    ApprovalProcess.REGULARISATION,
    ApprovalProcess.LIFECYCLE,
)


@dataclass(frozen=True, slots=True)
class Facts:
    """What a rule's conditions are checked against."""

    process: ApprovalProcess
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    amount: Decimal | None = None
    days: Decimal | None = None
    leave_type_id: uuid.UUID | None = None
    category: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedStep:
    role: str
    user_id: uuid.UUID | None
    employee_id: uuid.UUID | None
    name: str
    escalate_after_hours: int | None = None


def _ids(values: list | None) -> set[str]:
    return {str(v) for v in values or []}


def matches(rule: ApprovalRule, facts: Facts) -> bool:
    """Every condition the rule sets holds for these facts. Pure."""
    if not rule.active or rule.process is not facts.process:
        return False
    if rule.min_amount is not None and (
        facts.amount is None or facts.amount < Decimal(str(rule.min_amount))
    ):
        return False
    if rule.max_amount is not None and (
        facts.amount is None or facts.amount > Decimal(str(rule.max_amount))
    ):
        return False
    if rule.min_days is not None and (
        facts.days is None or facts.days < Decimal(str(rule.min_days))
    ):
        return False
    if rule.leave_type_ids and str(facts.leave_type_id) not in _ids(rule.leave_type_ids):
        return False
    if rule.expense_categories and (facts.category or "") not in set(rule.expense_categories):
        return False
    if rule.department_ids and str(facts.department_id) not in _ids(rule.department_ids):
        return False
    return not (rule.location_ids and str(facts.location_id) not in _ids(rule.location_ids))


async def _login_of(
    session: AsyncSession, user: User, employee_id: uuid.UUID | None
) -> tuple[Employee | None, User | None]:
    if employee_id is None:
        return None, None
    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == employee_id, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )
    if employee is None:
        return None, None
    login = await session.scalar(
        tenant_scoped(
            select(User).where(User.employee_id == employee.id, User.is_active.is_(True)),
            User,
            user,
        )
    )
    return employee, login


async def _rule_for(session: AsyncSession, user: User, facts: Facts) -> ApprovalRule | None:
    rules = (
        await session.scalars(
            tenant_scoped(
                select(ApprovalRule)
                .where(ApprovalRule.process == facts.process, ApprovalRule.active.is_(True))
                .order_by(ApprovalRule.priority, ApprovalRule.created_at),
                ApprovalRule,
                user,
            )
        )
    ).all()
    return next((r for r in rules if matches(r, facts)), None)


async def resolve_steps(
    session: AsyncSession, user: User, employee: Employee, facts: Facts
) -> tuple[list[ResolvedStep], str | None]:
    """The approvers, in order, and which rule chose them (None for the built-in step)."""
    rule = await _rule_for(session, user, facts)
    specs = (
        rule.steps
        if rule is not None and rule.steps
        else DEFAULT_STEPS.get(facts.process, [{"kind": "line_manager"}])
    )
    escalate = rule.escalate_after_hours if rule is not None else None
    own_login = await session.scalar(
        tenant_scoped(
            select(User.id).where(User.employee_id == employee.id, User.is_active.is_(True)),
            User,
            user,
        )
    )

    resolved: list[ResolvedStep] = []
    for spec in specs:
        kind = spec.get("kind")
        step: ResolvedStep | None = None
        if kind == "line_manager":
            manager, login = await _login_of(session, user, employee.manager_id)
            if manager is not None and login is not None and manager.id != employee.id:
                step = ResolvedStep("manager", login.id, manager.id, manager.full_name, escalate)
        elif kind == "skip_level":
            manager, _ = await _login_of(session, user, employee.manager_id)
            above, login = await _login_of(
                session, user, manager.manager_id if manager is not None else None
            )
            if above is not None and login is not None and above.id != employee.id:
                step = ResolvedStep("skip_level", login.id, above.id, above.full_name, escalate)
        elif kind == "user" and spec.get("user_id"):
            named = await session.scalar(
                tenant_scoped(
                    select(User).where(
                        User.id == uuid.UUID(str(spec["user_id"])), User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
            if named is not None:
                step = ResolvedStep("user", named.id, named.employee_id, named.full_name, escalate)
        elif kind == "hr":
            step = ResolvedStep("hr", None, None, "HR team", escalate)
        if step is None or (step.user_id is not None and step.user_id == own_login):
            continue
        if resolved and step.user_id is not None and resolved[-1].user_id == step.user_id:
            continue
        resolved.append(step)
    return resolved, (rule.name if rule is not None else None)


def chain_from(steps: list[ResolvedStep], rule_name: str | None) -> list[dict[str, Any]]:
    """The JSON snapshot, in the shape the leave chain already uses."""
    return [
        {
            "step": index + 1,
            "role": step.role,
            "approver_user_id": str(step.user_id) if step.user_id else None,
            "approver_employee_id": str(step.employee_id) if step.employee_id else None,
            "approver_name": step.name,
            "status": "pending",
            "decided_by_user_id": None,
            "decided_by_name": None,
            "decided_at": None,
            "note": None,
            "escalate_after_hours": step.escalate_after_hours,
            "rule": rule_name,
        }
        for index, step in enumerate(steps)
    ]


async def attach(
    session: AsyncSession, user: User, employee: Employee, facts: Facts
) -> list[dict[str, Any]]:
    """The chain to snapshot on a request: the rule's steps, else the default,
    else HR — a request routed to nobody waits forever."""
    steps, rule_name = await resolve_steps(session, user, employee, facts)
    if not steps:
        steps = [ResolvedStep("hr", None, None, "HR team", None)]
    return chain_from(steps, rule_name)


async def may_decide(
    session: AsyncSession,
    user: User,
    chain: list[dict[str, Any]],
    *,
    hr_may: bool | None = None,
) -> bool:
    """Whether this person decides the chain's current step.

    The named approver or somebody they delegated to; an HR step is HR's; and
    HR may stand in at any step. `hr_may` narrows "HR" where a process has
    always been one role's (a lifecycle change is an admin's).
    """
    from app.services.leave import engine

    index = engine.current_step(chain)
    if index is None:
        return False
    is_hr = permissions.is_hr(user) if hr_may is None else hr_may
    if is_hr:
        return True
    approver = chain[index].get("approver_user_id")
    if approver is None:
        return False
    return approver in {str(a) for a in await acting_for(session, user)}


async def decide(
    session: AsyncSession,
    user: User,
    chain: list[dict[str, Any]],
    *,
    approve: bool,
    note: str | None,
    hr_may: bool | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Record this person's decision on the current step: (new chain, outcome).

    The outcome is "advanced" (a later step waits), "approved" or "rejected".
    """
    from app.services.leave import engine

    index = engine.current_step(chain)
    if index is None:
        raise HTTPException(status_code=409, detail="This request has already been decided.")
    if not await may_decide(session, user, chain, hr_may=hr_may):
        raise permissions.forbidden(
            f"This is waiting on {chain[index].get('approver_name') or 'another approver'}. "
            "Only they, somebody standing in for them, or HR can decide it."
        )
    return engine.apply_decision(
        chain,
        approve=approve,
        actor_user_id=user.id,
        actor_name=user.full_name,
        note=note,
        at=datetime.now(UTC),
    )


async def next_recipients(
    session: AsyncSession, user: User, chain: list[dict[str, Any]]
) -> list[uuid.UUID]:
    """Who to tell that the chain's current step is waiting: its approver, or HR."""
    from app.services.leave import engine

    index = engine.current_step(chain)
    if index is None:
        return []
    approver = chain[index].get("approver_user_id")
    if approver:
        return [uuid.UUID(approver)]
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.role.in_(permissions.HR_ROLES), User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )


def chain_first_named(chain: list[dict[str, Any]]) -> uuid.UUID | None:
    """The current step's named approver, or None for an HR step or a closed chain."""
    from app.services.leave import engine

    index = engine.current_step(chain)
    if index is None:
        return None
    approver = chain[index].get("approver_user_id")
    return uuid.UUID(approver) if approver else None


def waiting_on(chain: list[dict[str, Any]], acting: set[str]) -> bool:
    """Whether the chain's current step is with one of `acting` (user ids as text)."""
    from app.services.leave import engine

    index = engine.current_step(chain)
    return index is not None and chain[index].get("approver_user_id") in acting


# ── Delegation ──────────────────────────────────────────────────────────────


async def acting_for(
    session: AsyncSession, user: User, on: date | None = None
) -> set[uuid.UUID]:
    """The approvers whose decisions this person may take today, themselves included."""
    day = on or datetime.now(UTC).date()
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ApprovalDelegation.from_user_id).where(
                    ApprovalDelegation.to_user_id == user.id,
                    ApprovalDelegation.cancelled.is_(False),
                    ApprovalDelegation.starts_on <= day,
                    ApprovalDelegation.ends_on >= day,
                ),
                ApprovalDelegation,
                user,
            )
        )
    ).all()
    return {user.id, *rows}


def _delegation_read(row: ApprovalDelegation, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "from_user_id": str(row.from_user_id),
        "from_name": names.get(row.from_user_id, ""),
        "to_user_id": str(row.to_user_id),
        "to_name": names.get(row.to_user_id, ""),
        "starts_on": row.starts_on.isoformat(),
        "ends_on": row.ends_on.isoformat(),
        "reason": row.reason,
        "cancelled": row.cancelled,
    }


async def _names(session: AsyncSession, user: User, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids)), User, user)
        )
    ).all()
    return dict(rows)


async def list_delegations(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Your own delegations, both ways; HR sees everybody's."""
    stmt = select(ApprovalDelegation).order_by(ApprovalDelegation.starts_on.desc())
    if not permissions.is_hr(user):
        stmt = stmt.where(
            or_(
                ApprovalDelegation.from_user_id == user.id,
                ApprovalDelegation.to_user_id == user.id,
            )
        )
    rows = (await session.scalars(tenant_scoped(stmt, ApprovalDelegation, user))).all()
    people = {r.from_user_id for r in rows} | {r.to_user_id for r in rows}
    names = await _names(session, user, people)
    return [_delegation_read(r, names) for r in rows]


async def delegate_options(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Colleagues who can sign in, to choose a stand-in from. Names only."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(User.id, User.full_name, User.email)
                .where(User.is_active.is_(True), User.id != user.id)
                .order_by(User.full_name),
                User,
                user,
            )
        )
    ).all()
    return [{"user_id": str(uid), "name": name, "email": email} for uid, name, email in rows]


async def create_delegation(
    session: AsyncSession,
    user: User,
    *,
    to_user_id: uuid.UUID,
    starts_on: date,
    ends_on: date,
    reason: str | None,
    from_user_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Hand your approvals to somebody while away. HR may set one up for somebody else."""
    giver_id = from_user_id or user.id
    if giver_id != user.id and not permissions.is_hr(user):
        raise permissions.forbidden("You can only hand over your own approvals.")
    if to_user_id == giver_id:
        raise HTTPException(status_code=422, detail="Choose somebody else to approve for you.")
    if ends_on < starts_on:
        raise HTTPException(status_code=422, detail="The end date is before the start date.")
    if (ends_on - starts_on).days > 90:
        raise HTTPException(
            status_code=422,
            detail="A delegation runs for at most 90 days; set up another after that.",
        )
    delegate = await session.scalar(
        tenant_scoped(
            select(User).where(User.id == to_user_id, User.is_active.is_(True)), User, user
        )
    )
    if delegate is None:
        raise HTTPException(status_code=422, detail="That person cannot sign in to approve.")
    row = ApprovalDelegation(
        tenant_id=user.tenant_id, from_user_id=giver_id, to_user_id=delegate.id,
        starts_on=starts_on, ends_on=ends_on, reason=(reason or "").strip() or None,
        created_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.CREATE,
        entity_type="approval_delegation", entity_id=row.id,
        summary=f"Approvals handed to {delegate.full_name}, {starts_on} to {ends_on}",
        request=request,
    )
    names = await _names(session, user, {giver_id, delegate.id})
    return _delegation_read(row, names)


async def cancel_delegation(
    session: AsyncSession, user: User, delegation_id: uuid.UUID, *, request: Request | None = None
) -> None:
    row = await session.scalar(
        tenant_scoped(
            select(ApprovalDelegation).where(ApprovalDelegation.id == delegation_id),
            ApprovalDelegation,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Delegation not found")
    if row.from_user_id != user.id and not permissions.is_hr(user):
        raise permissions.forbidden("Only the person who handed over approvals ends it.")
    row.cancelled = True
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.UPDATE,
        entity_type="approval_delegation", entity_id=row.id, summary="Delegation ended",
        request=request,
    )


# ── Rules: administration ───────────────────────────────────────────────────


def _rule_read(rule: ApprovalRule) -> dict[str, Any]:
    return {
        "id": str(rule.id),
        "process": rule.process.value,
        "name": rule.name,
        "priority": rule.priority,
        "active": rule.active,
        "min_amount": str(rule.min_amount) if rule.min_amount is not None else None,
        "max_amount": str(rule.max_amount) if rule.max_amount is not None else None,
        "min_days": str(rule.min_days) if rule.min_days is not None else None,
        "leave_type_ids": list(rule.leave_type_ids or []),
        "expense_categories": list(rule.expense_categories or []),
        "department_ids": list(rule.department_ids or []),
        "location_ids": list(rule.location_ids or []),
        "steps": list(rule.steps or []),
        "escalate_after_hours": rule.escalate_after_hours,
        "description": rule.description,
    }


def _require_admin(user: User) -> None:
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin changes who approves what.")


def _validate_categories(process: ApprovalProcess, categories: list[Any] | None) -> None:
    if not categories:
        return
    if process not in CATEGORY_PROCESSES:
        raise HTTPException(
            status_code=422, detail=f"A {process.value} rule has no category condition."
        )
    allowed: set[str] | None = None
    if process is ApprovalProcess.REGULARISATION:
        from app.models.attendance import RegularisationKind

        allowed = {k.value for k in RegularisationKind}
    elif process is ApprovalProcess.LIFECYCLE:
        from app.models.lifecycle import LifecycleEventType

        allowed = {k.value for k in LifecycleEventType}
    if allowed is not None:
        unknown = sorted({str(c) for c in categories} - allowed)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Not a {process.value} type: {', '.join(unknown)}. "
                f"Choose from {', '.join(sorted(allowed))}.",
            )


def _validate_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not steps:
        raise HTTPException(status_code=422, detail="A rule needs at least one approver step.")
    if len(steps) > 5:
        raise HTTPException(status_code=422, detail="A chain of more than five steps is a queue.")
    cleaned = []
    for spec in steps:
        kind = spec.get("kind")
        if kind not in STEP_KINDS:
            raise HTTPException(status_code=422, detail=f"'{kind}' is not a kind of step.")
        if kind == "user" and not spec.get("user_id"):
            raise HTTPException(status_code=422, detail="A named-person step needs the person.")
        entry: dict[str, Any] = {"kind": kind}
        if kind == "user":
            entry["user_id"] = str(spec["user_id"])
        cleaned.append(entry)
    return cleaned


async def list_rules(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    permissions.require_hr(user, detail="Approval rules are HR's to read.")
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ApprovalRule).order_by(ApprovalRule.process, ApprovalRule.priority),
                ApprovalRule,
                user,
            )
        )
    ).all()
    return [_rule_read(r) for r in rows]


_FIELDS = (
    "name", "priority", "active", "min_amount", "max_amount", "min_days", "leave_type_ids",
    "expense_categories", "department_ids", "location_ids", "escalate_after_hours",
    "description",
)


async def create_rule(
    session: AsyncSession, user: User, values: dict[str, Any], *, request: Request | None = None
) -> dict[str, Any]:
    _require_admin(user)
    _validate_categories(ApprovalProcess(values["process"]), values.get("expense_categories"))
    rule = ApprovalRule(
        tenant_id=user.tenant_id,
        process=ApprovalProcess(values["process"]),
        steps=_validate_steps(values.get("steps") or []),
        **{k: _jsonable(values[k]) for k in _FIELDS if k in values and values[k] is not None},
    )
    session.add(rule)
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.CREATE,
        entity_type="approval_rule", entity_id=rule.id,
        summary=f"Approval rule '{rule.name}' for {rule.process.value} created",
        changes=_rule_read(rule), request=request,
    )
    return _rule_read(rule)


def _jsonable(value: Any) -> Any:
    if isinstance(value, list):
        return [str(v) for v in value]
    return value


async def update_rule(
    session: AsyncSession, user: User, rule_id: uuid.UUID, values: dict[str, Any], *,
    request: Request | None = None,
) -> dict[str, Any]:
    _require_admin(user)
    rule = await session.scalar(
        tenant_scoped(select(ApprovalRule).where(ApprovalRule.id == rule_id), ApprovalRule, user)
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    if "expense_categories" in values:
        _validate_categories(rule.process, values["expense_categories"])
    before = _rule_read(rule)
    for key in _FIELDS:
        if key in values:
            setattr(rule, key, _jsonable(values[key]))
    if "steps" in values and values["steps"] is not None:
        rule.steps = _validate_steps(values["steps"])
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.UPDATE,
        entity_type="approval_rule", entity_id=rule.id,
        summary=f"Approval rule '{rule.name}' changed; requests already submitted keep their chain",
        changes={"from": before, "to": _rule_read(rule)}, request=request,
    )
    return _rule_read(rule)


async def delete_rule(
    session: AsyncSession, user: User, rule_id: uuid.UUID, *, request: Request | None = None
) -> None:
    _require_admin(user)
    rule = await session.scalar(
        tenant_scoped(select(ApprovalRule).where(ApprovalRule.id == rule_id), ApprovalRule, user)
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    before = _rule_read(rule)
    await session.delete(rule)
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.DELETE,
        entity_type="approval_rule", entity_id=rule_id,
        summary=f"Approval rule '{before['name']}' deleted", changes=before, request=request,
    )


# ── Escalation ──────────────────────────────────────────────────────────────


def _waiting_since(chain: list[dict[str, Any]], index: int, submitted_at: datetime) -> datetime:
    previous = [
        datetime.fromisoformat(s["decided_at"])
        for s in chain[:index]
        if s.get("decided_at")
    ]
    return max([submitted_at, *previous])


async def _escalate_chain(
    session: AsyncSession, actor: User, chain: list[dict[str, Any]], submitted_at: datetime,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The chain with its current step moved up a level if it has waited too long."""
    from app.services.leave import engine

    index = engine.current_step(chain)
    if index is None:
        return chain, None
    step = dict(chain[index])
    hours = step.get("escalate_after_hours")
    if not hours or step.get("escalated") or step.get("role") == "hr":
        return chain, None
    if now - _waiting_since(chain, index, submitted_at) < timedelta(hours=int(hours)):
        return chain, None

    approver_employee = step.get("approver_employee_id")
    above, login = (None, None)
    if approver_employee:
        current, _ = await _login_of(session, actor, uuid.UUID(approver_employee))
        above, login = await _login_of(
            session, actor, current.manager_id if current is not None else None
        )
    step["escalated_from"] = step.get("approver_name")
    step["escalated"] = True
    if above is not None and login is not None:
        step.update(
            role="escalated", approver_user_id=str(login.id),
            approver_employee_id=str(above.id), approver_name=above.full_name,
        )
    else:
        step.update(role="hr", approver_user_id=None, approver_employee_id=None,
                    approver_name="HR team")
    new_chain = [dict(s) for s in chain]
    new_chain[index] = step
    return new_chain, step


async def escalate_due(session: AsyncSession, actor: User, *, now: datetime | None = None) -> dict:
    """Move every step that has waited past its rule's limit up a level. Scheduler-callable."""
    from app.models.attendance import AttendanceRegularisation, RegularisationStatus
    from app.models.expenses import ExpenseClaim, ExpenseClaimStatusEnum
    from app.models.leave import LeaveRequest, LeaveRequestStatus
    from app.models.lifecycle import LifecycleEvent, LifecycleStatus
    from app.models.timesheets import Timesheet, TimesheetStatus
    from app.services.notifications.dispatch import notify

    moment = now or datetime.now(UTC)
    moved = 0
    leaves = (
        await session.scalars(
            tenant_scoped(
                select(LeaveRequest).where(LeaveRequest.status == LeaveRequestStatus.PENDING),
                LeaveRequest,
                actor,
            )
        )
    ).all()
    claims = (
        await session.scalars(
            tenant_scoped(
                select(ExpenseClaim).where(
                    ExpenseClaim.status == ExpenseClaimStatusEnum.PENDING_MANAGER
                ),
                ExpenseClaim,
                actor,
            )
        )
    ).all()
    async def pending(model, status_column, status_value):
        return (
            await session.scalars(
                tenant_scoped(
                    select(model).where(status_column == status_value), model, actor
                )
            )
        ).all()

    corrections = await pending(
        AttendanceRegularisation, AttendanceRegularisation.status, RegularisationStatus.PENDING
    )
    sheets = await pending(Timesheet, Timesheet.status, TimesheetStatus.SUBMITTED)
    events = await pending(
        LifecycleEvent, LifecycleEvent.status, LifecycleStatus.PENDING_APPROVAL
    )
    for row, submitted, kind in [
        *((leave, leave.created_at, "leave request") for leave in leaves),
        *((claim, claim.submitted_at or claim.created_at, "expense claim") for claim in claims),
        *((reg, reg.created_at, "attendance correction") for reg in corrections),
        *((sheet, sheet.submitted_at or sheet.created_at, "timesheet") for sheet in sheets),
        *((event, event.created_at, "lifecycle event") for event in events),
    ]:
        chain = list(row.approval_chain or [])
        if not chain:
            continue
        new_chain, step = await _escalate_chain(session, actor, chain, submitted, moment)
        if step is None:
            continue
        row.approval_chain = new_chain
        moved += 1
        await audit.record(
            session, tenant_id=actor.tenant_id, actor=actor, actor_type="system",
            action=AuditAction.UPDATE, entity_type=row.__tablename__.rstrip("s"),
            entity_id=row.id,
            summary=(
                f"Escalated after {step['escalate_after_hours']} hours waiting on "
                f"{step['escalated_from']}; now with {step['approver_name']}"
            ),
        )
        if step.get("approver_user_id"):
            await notify(
                session, tenant_id=actor.tenant_id, event_key="approvals.escalated",
                recipient_user_ids=[uuid.UUID(step["approver_user_id"])],
                payload={"what": kind, "from": step["escalated_from"],
                         "hours": step["escalate_after_hours"]},
                commit=False,
            )
    await session.commit()
    return {"escalated": moved}


def _register_events() -> None:
    from app.services.notifications.dispatch import register_event

    register_event(
        "approvals.escalated",
        "A {what} is waiting on you",
        "A {what} waited more than {hours} hours on {from}, so it has come to you. "
        "Open Approvals to decide it.",
    )


_register_events()

