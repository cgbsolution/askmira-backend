"""Approval chain rule configuration, saved with an audit row.

A rule change decides whose signature every future offer needs, so it is as much
a record as any approval.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.offer import ApprovalChainRule, ApprovalScope
from app.models.user import User
from app.services import audit

__all__ = ["create_rule", "list_rules"]


async def create_rule(
    session: AsyncSession, user: User, *, values: dict[str, Any], request: Request | None = None
) -> ApprovalChainRule:
    taken = await session.scalar(
        tenant_scoped(
            select(ApprovalChainRule.id).where(
                ApprovalChainRule.scope == values["scope"],
                ApprovalChainRule.sequence == values["sequence"],
            ),
            ApprovalChainRule,
            user,
        )
    )
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Step {values['sequence']} already exists for "
                f"{values['scope'].value} approvals."
            ),
        )
    if values.get("ctc_max") is not None and values["ctc_max"] <= values.get("ctc_min", 0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The band's upper limit has to be above its lower limit.",
        )
    rule = ApprovalChainRule(tenant_id=user.tenant_id, **values)
    session.add(rule)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="approval_chain_rule",
        entity_id=rule.id,
        summary=(
            f"{rule.scope.value.capitalize()} approval step {rule.sequence} added: "
            f"{rule.approver_role} from {rule.ctc_min}"
            + (f" to {rule.ctc_max}" if rule.ctc_max is not None else " upwards")
        ),
        request=request,
    )
    return rule


async def list_rules(
    session: AsyncSession, user: User, *, scope: ApprovalScope | None
) -> list[ApprovalChainRule]:
    stmt = select(ApprovalChainRule).order_by(ApprovalChainRule.scope, ApprovalChainRule.sequence)
    if scope is not None:
        stmt = stmt.where(ApprovalChainRule.scope == scope)
    rows = await session.scalars(tenant_scoped(stmt, ApprovalChainRule, user))
    return list(rows.all())
