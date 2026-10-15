"""Flexi-benefit declarations: a person's pot, split across reimbursement components.

The rules:

- HR sets the pot for a person and year (the flexible part of their CTC).
- The person declares an amount per active component. Each is at most the
  component's own annual limit; together they are at most the pot. What is
  left of the pot undeclared stays as taxable salary — the screen says so.
- HR approves or sends it back. An approved split is the person's entitlement
  for the year (`entitlement_for`); without one, the component's default applies.
- The person cannot change an approved declaration themselves; HR can reopen it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.loans import FlexiDeclaration, ReimbursementComponent
from app.models.user import User
from app.services import audit

__all__ = [
    "approved_entitlements",
    "decide",
    "declaration_for",
    "declare",
    "entitlement_for",
    "set_pot",
]

ZERO = Decimal(0)


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _read(row: FlexiDeclaration | None, components: list[ReimbursementComponent]) -> dict:
    allocations = dict((row.allocations or {}) if row else {})
    declared = sum((_d(v) for v in allocations.values()), ZERO)
    pot = _d(row.pot) if row else ZERO
    return {
        "id": str(row.id) if row else None,
        "employee_id": str(row.employee_id) if row else None,
        "financial_year": row.financial_year if row else None,
        "pot": str(pot),
        "declared": str(declared),
        "undeclared": str(max(ZERO, pot - declared)),
        "status": row.status if row else "none",
        "decision_note": row.decision_note if row else None,
        "components": [
            {
                "code": c.code,
                "name": c.name,
                "limit": str(_d(c.annual_entitlement)),
                "amount": allocations.get(c.code, "0"),
            }
            for c in components
        ],
        "note": (
            "What you leave undeclared is paid as taxable salary. Each component is "
            "reimbursed against bills, up to the amount you declare for it."
        ),
    }


async def _components(session: AsyncSession, user: User) -> list[ReimbursementComponent]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(ReimbursementComponent)
                    .where(ReimbursementComponent.active.is_(True))
                    .order_by(ReimbursementComponent.code),
                    ReimbursementComponent,
                    user,
                )
            )
        ).all()
    )


async def _row(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> FlexiDeclaration | None:
    return await session.scalar(
        tenant_scoped(
            select(FlexiDeclaration).where(
                FlexiDeclaration.employee_id == employee_id,
                FlexiDeclaration.financial_year == financial_year,
            ),
            FlexiDeclaration,
            user,
        )
    )


async def declaration_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> dict:
    permissions.require_self_or_hr(
        user, employee_id, detail="A flexi declaration is yours or HR's."
    )
    return _read(
        await _row(session, user, employee_id, financial_year), await _components(session, user)
    )


async def set_pot(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    financial_year: str,
    pot: Decimal,
    *,
    request: Request | None = None,
) -> dict:
    """HR: the flexible part of this person's CTC for the year."""
    permissions.require_hr(user, detail="HR sets the flexi pot.")
    if pot < 0:
        raise HTTPException(status_code=422, detail="A pot cannot be negative.")
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    row = await _row(session, user, employee_id, financial_year)
    if row is None:
        row = FlexiDeclaration(
            tenant_id=user.tenant_id, employee_id=employee_id,
            financial_year=financial_year, pot=pot, allocations={}, status="draft",
        )
        session.add(row)
    else:
        declared = sum((_d(v) for v in (row.allocations or {}).values()), ZERO)
        if declared > pot:
            raise HTTPException(
                status_code=409,
                detail=f"{declared} is already declared, more than a pot of {pot}. Reopen the "
                "declaration and have it lowered first.",
            )
        row.pot = pot
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, action=AuditAction.UPDATE,
        entity_type="flexi_declaration", entity_id=row.id, actor=user,
        changes={"pot": str(pot), "financial_year": financial_year},
        summary=f"Set the {financial_year} flexi pot of {employee.employee_code} to {pot}",
        request=request,
    )
    await session.commit()
    return _read(row, await _components(session, user))


async def declare(
    session: AsyncSession,
    user: User,
    financial_year: str,
    allocations: dict[str, Decimal],
    *,
    submit: bool,
    request: Request | None = None,
) -> dict:
    """The person: how much of their pot goes to each component."""
    me = permissions.own_employee_id(user)
    row = await _row(session, user, me, financial_year)
    if row is None:
        raise HTTPException(
            status_code=409, detail="HR has not set a flexi pot for you for this year."
        )
    if row.status == "approved":
        raise HTTPException(
            status_code=409, detail="This declaration is approved. Ask HR to reopen it."
        )
    components = {c.code: c for c in await _components(session, user)}
    clean: dict[str, str] = {}
    for code, amount in allocations.items():
        if amount < 0:
            raise HTTPException(status_code=422, detail="An amount cannot be negative.")
        component = components.get(code)
        if component is None:
            raise HTTPException(status_code=422, detail=f"'{code}' is not a flexi component.")
        if amount > _d(component.annual_entitlement):
            raise HTTPException(
                status_code=422,
                detail=f"{component.name} is capped at {component.annual_entitlement} a year.",
            )
        if amount > 0:
            clean[code] = str(amount.quantize(Decimal("0.01")))
    total = sum((_d(v) for v in clean.values()), ZERO)
    if total > _d(row.pot):
        raise HTTPException(
            status_code=422, detail=f"{total} is more than your flexi pot of {row.pot}."
        )
    row.allocations = clean
    row.status = "submitted" if submit else "draft"
    row.decision_note = None
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, action=AuditAction.UPDATE,
        entity_type="flexi_declaration", entity_id=row.id, actor=user,
        changes={"allocations": clean, "status": row.status},
        summary=f"{'Submitted' if submit else 'Saved'} the {financial_year} flexi declaration",
        request=request,
    )
    await session.commit()
    return _read(row, list(components.values()))


async def decide(
    session: AsyncSession,
    user: User,
    declaration_id: uuid.UUID,
    *,
    approve: bool | None,
    note: str | None,
    request: Request | None = None,
) -> dict:
    """HR approves, sends back, or (approve=None) reopens an approved declaration."""
    permissions.require_hr(user, detail="HR decides flexi declarations.")
    row = await session.scalar(
        tenant_scoped(
            select(FlexiDeclaration).where(FlexiDeclaration.id == declaration_id),
            FlexiDeclaration,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Declaration not found")
    if row.employee_id == user.employee_id:
        raise permissions.forbidden("Somebody else decides your own declaration.")
    if approve is None:
        if row.status != "approved":
            raise HTTPException(status_code=409, detail="Only an approved declaration reopens.")
        row.status = "draft"
    else:
        if row.status != "submitted":
            raise HTTPException(status_code=409, detail="This declaration is not submitted.")
        if not approve and not (note or "").strip():
            raise HTTPException(status_code=422, detail="Say what needs changing.")
        row.status = "approved" if approve else "rejected"
    row.decided_by_user_id = user.id
    row.decided_at = datetime.now(UTC)
    row.decision_note = (note or "").strip() or None
    await audit.record(
        session, tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.UPDATE,
        entity_type="flexi_declaration", entity_id=row.id, actor=user,
        changes={"status": row.status, "note": row.decision_note},
        summary=f"Flexi declaration for {row.financial_year} {row.status}",
        request=request,
    )
    await session.commit()
    return _read(row, await _components(session, user))


async def approved_entitlements(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> dict[str, Decimal] | None:
    """The person's approved split for the year, or None to use the component defaults."""
    row = await _row(session, user, employee_id, financial_year)
    if row is None or row.status != "approved":
        return None
    return {code: _d(v) for code, v in (row.allocations or {}).items()}


async def entitlement_for(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    financial_year: str,
    component: ReimbursementComponent,
) -> Decimal:
    """What this person may claim under a component this year."""
    approved = await approved_entitlements(session, user, employee_id, financial_year)
    if approved is None:
        return _d(component.annual_entitlement)
    return approved.get(component.code, ZERO)
