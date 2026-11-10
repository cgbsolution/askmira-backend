"""A joiner's IT tasks, sent to CGB Helpdesk when it is linked.

Onboarding gives IT its own tasks (a laptop from the register, accounts,
equipment assigned). With the helpdesk linked, that is where the IT team works,
so starting a joiner's onboarding raises one helpdesk ticket listing them, as
the HR person who started it, through the helpdesk's own rules (products.act,
use=act, kind ``create_ticket``). The ticket's number is written on each IT task
so HR and IT see the same thing.

The ticket is raised only once the onboarding is saved (after the commit, in a
session of its own): a joiner whose save fails — an email that already signs
in, a double click — leaves no ticket behind, and saving again raises one, not
two. A plan whose IT tasks already carry the ticket is never sent again.

Onboarding never waits on the helpdesk: if it cannot be reached or refuses, the
plan still starts, each IT task says so, and the audit log records why.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import Request
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import has_direct_reports, tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.onboarding import (
    OnboardingOwnerType,
    OnboardingPlan,
    OnboardingPlanStatus,
    OnboardingTask,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.products import data as products
from app.services.products.catalogue import PRODUCTS

__all__ = ["drain", "send_after_commit", "send_it_tasks"]

logger = logging.getLogger("askmira.onboarding.helpdesk")

PRODUCT = "helpdesk"
BRAND = PRODUCTS[PRODUCT].brand
#: The helpdesk categories an onboarding ticket fits, best first.
_CATEGORY_WORDS = ("onboard", "joiner", "access", "hardware", "laptop", "equipment", "asset")


#: Sub categories that mean setting someone up, best first.
_SUB_WORDS = ("onboard", "joiner", "new", "setup", "set up", "provision", "laptop")


def _pick_category(categories: dict[str, list[str]]) -> tuple[str, str] | None:
    """The category (and sub category) that fits a new joiner's IT setup best."""
    for word in _CATEGORY_WORDS:
        for name, subs in categories.items():
            named = next((s for s in subs if word in s.lower()), "")
            if word in name.lower() or named:
                setup = next((s for w in _SUB_WORDS for s in subs if w in s.lower()), named)
                return name, setup
    return next(((name, "") for name in categories), None)


def _description(employee: Employee, joining: date, tasks: list[OnboardingTask]) -> str:
    lines = [
        f"{employee.full_name} ({employee.employee_code}) joins on {joining:%d %b %Y}"
        + (f" as {employee.designation}" if employee.designation else "")
        + ".",
        "",
        "IT tasks from their onboarding checklist in AskMiraa:",
    ]
    for task in tasks:
        lines.append(f"- {task.title} (due {task.due_date:%d %b %Y})")
        if task.description:
            lines.append(f"  {task.description.strip()[:300]}")
    lines += ["", "Raised by AskMiraa when HR started the onboarding."]
    return "\n".join(lines)[:5000]


def _note(task: OnboardingTask, line: str) -> None:
    task.description = f"{task.description.rstrip()}\n\n{line}" if task.description else line


async def send_it_tasks(
    session: AsyncSession,
    user: User,
    plan: OnboardingPlan,
    employee: Employee,
    tasks: list[OnboardingTask],
    *,
    request: Request | None = None,
) -> dict[str, Any] | None:
    """Raise the joiner's IT tasks as one helpdesk ticket. None when there is nothing to send."""
    it = [t for t in tasks if t.owner_type is OnboardingOwnerType.IT]
    if not it:
        return None
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not (tenant.linked_products or {}).get(PRODUCT):
        return None
    if await products.role_in(session, user, PRODUCT) is None:
        return None

    try:
        options = await products.ask(session, user, PRODUCT, "options", {})
        categories = {
            str(c.get("name")): [str(s) for s in c.get("sub_categories") or []]
            for c in options.get("categories") or []
            if isinstance(c, dict) and c.get("name")
        }
        chosen = _pick_category(categories)
        if chosen is None:
            raise products.ProductUnavailableError(f"{BRAND} has no ticket categories set up.")
        category, sub = chosen
        params: dict[str, Any] = {
            "subject": (
                f"New joiner {employee.full_name} ({employee.employee_code}), "
                f"{plan.joining_date:%d %b}: IT setup"
            )[:200],
            "description": _description(employee, plan.joining_date, it),
            "category": category,
            "priority": "medium",
        }
        if sub:
            params["sub_category"] = sub
        answer = await products.act(session, user, PRODUCT, "create_ticket", params)
    except products.ProductUnavailableError as exc:
        reason = str(exc)
        for task in it:
            _note(task, f"Not sent to {BRAND} ({reason}). Raise it there by hand.")
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="onboarding_plan",
            entity_id=plan.id,
            summary=f"IT tasks for {employee.employee_code} could not be sent to {BRAND}: {reason}",
            request=request,
        )
        return {"sent": False, "reason": reason}

    ticket = answer.get("ticket") if isinstance(answer.get("ticket"), dict) else {}
    ref = str(ticket.get("ref") or "")[:12]
    for task in it:
        _note(task, f"In {BRAND} as ticket #{ref}." if ref else f"Sent to {BRAND}.")
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="onboarding_plan",
        entity_id=plan.id,
        summary=(
            f"{len(it)} IT task(s) for {employee.employee_code} sent to {BRAND}"
            + (f" as ticket #{ref}" if ref else "")
        ),
        changes={"helpdesk_ticket": ref or None, "tasks": [t.title for t in it]},
        request=request,
    )
    return {"sent": True, "ref": ref, "category": category}


# ── Once the onboarding is saved ────────────────────────────────────────────

_PENDING: set[asyncio.Task[None]] = set()
#: One at a time: a plan is checked for its ticket before another is raised.
_ONE_AT_A_TIME = asyncio.Lock()
#: Where the follow-up gets its own session (tests hand it theirs).
session_factory: Callable[[], Any] | None = None


def send_after_commit(
    session: AsyncSession, user: User, plan: OnboardingPlan, tasks: list[OnboardingTask]
) -> None:
    """Raise the joiner's IT ticket once this transaction commits; nothing if it never does."""
    if not any(t.owner_type is OnboardingOwnerType.IT for t in tasks):
        return
    ids = (user.tenant_id, user.id, plan.id)
    sync = session.sync_session
    fired = [False]

    def committed(_session: Any) -> None:
        if fired[0]:
            return
        fired[0] = True
        job = asyncio.get_running_loop().create_task(_send_later(*ids))
        _PENDING.add(job)
        job.add_done_callback(_PENDING.discard)

    event.listen(sync, "after_commit", committed)


async def drain() -> None:
    """Wait for the tickets being raised (tests; a shutdown)."""
    while _PENDING:
        await asyncio.gather(*list(_PENDING), return_exceptions=True)


async def _send_later(tenant_id: uuid.UUID, user_id: uuid.UUID, plan_id: uuid.UUID) -> None:
    from app.db.session import get_session_factory

    try:
        factory = session_factory or get_session_factory()
        async with _ONE_AT_A_TIME, factory() as session:
            user = await session.scalar(
                select(User).where(User.id == user_id, User.tenant_id == tenant_id)
            )
            if user is None or not user.is_active:
                return
            plan = await session.scalar(
                tenant_scoped(
                    select(OnboardingPlan).where(OnboardingPlan.id == plan_id), OnboardingPlan, user
                )
            )
            if plan is None or plan.status is not OnboardingPlanStatus.IN_PROGRESS:
                return  # rolled back after all, or cancelled meanwhile
            user.has_reports = await has_direct_reports(session, user)
            employee = await session.get(Employee, plan.employee_id)
            tasks = list(
                (
                    await session.scalars(
                        tenant_scoped(
                            select(OnboardingTask).where(OnboardingTask.plan_id == plan.id),
                            OnboardingTask,
                            user,
                        )
                    )
                ).all()
            )
            it = [t for t in tasks if t.owner_type is OnboardingOwnerType.IT]
            if employee is None or not it or any(BRAND in (t.description or "") for t in it):
                return  # sent (or tried) already
            await send_it_tasks(session, user, plan, employee, it)
            await session.commit()
    except Exception:  # the onboarding is saved; a ticket that failed is logged, not raised
        logger.exception("The IT ticket for onboarding plan %s could not be raised", plan_id)
