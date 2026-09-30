"""Keeping login roles in step with the org chart.

Approvals for a person's reporting line (leave, attendance corrections,
expenses, performance) are gated on the `manager` login role. An HRMS that
asks HR to remember to change a login's role every time someone gets a report
leaves requests stuck with nobody able to decide them — so the role follows
the reporting line instead: whoever gains a direct report and still signs in
as a plain `employee` becomes a `manager`, audited like any role change.

It never demotes. Losing the last report is often temporary during a reorg,
and taking access away silently is worse than an extra role HR can remove.
"""

from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.user import User, UserRole
from app.services import audit

__all__ = ["ensure_manager_role", "promote_all_line_managers"]


async def ensure_manager_role(
    session: AsyncSession,
    actor: User,
    manager_employee_id: uuid.UUID | None,
    *,
    request: Request | None = None,
) -> bool:
    """Promote the manager's login from `employee` to `manager`. Returns whether it did.

    Call it in the same transaction that sets someone's `manager_id`. Does not
    commit.
    """
    if manager_employee_id is None:
        return False
    login = await session.scalar(
        tenant_scoped(
            select(User).where(
                User.employee_id == manager_employee_id, User.is_active.is_(True)
            ),
            User,
            actor,
        )
    )
    if login is None or login.role is not UserRole.EMPLOYEE:
        return False
    login.role = UserRole.MANAGER
    await audit.record(
        session,
        tenant_id=actor.tenant_id,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=login.id,
        summary=f"{login.email} became a manager: they now have direct reports",
        changes={"role": {"from": UserRole.EMPLOYEE.value, "to": UserRole.MANAGER.value}},
        request=request,
    )
    return True


async def promote_all_line_managers(session: AsyncSession, actor: User) -> int:
    """Bring every existing line manager's login up to date. Does not commit."""
    manager_ids = (
        await session.scalars(
            tenant_scoped(
                select(Employee.manager_id)
                .where(Employee.manager_id.is_not(None), Employee.deleted_at.is_(None))
                .distinct(),
                Employee,
                actor,
            )
        )
    ).all()
    promoted = 0
    for manager_id in manager_ids:
        if await ensure_manager_role(session, actor, manager_id):
            promoted += 1
    return promoted
