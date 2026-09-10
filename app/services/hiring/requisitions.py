"""Requisition approval, budget checks and vacancy accounting.

Three decisions worth stating.

**The budget breach is a headline, not a field.** Approvers approve — that is
what the button says and what the day is full of. A requisition that exceeds
approved headcount or its CTC band puts a written flag at the top of the
approval payload and records, on the approver's own row, that it was shown to
them. Salary creep happens one individually defensible approval at a time.

**The chain comes from the decision.** A junior backfill and a senior new
position do not need the same signatures. Resolved from grade and CTC band, and
snapshotted onto the requisition so a later change to the rules cannot rewrite
who was asked.

**Vacancies are accounted, not inferred.** `filled_count` moves when an offer
takes a seat and moves back when one is revoked, declined, lapsed or dropped.
Counting joined employees instead would show a requisition as open for the
three months between a release and a joining, and as filled forever after a
revocation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.hiring import Requisition, RequisitionStatus
from app.models.offer import (
    ApprovalChainRule,
    ApprovalDecision,
    ApprovalScope,
    RequisitionApproval,
)
from app.models.user import User
from app.services import audit
from app.services.hiring import approval_chain
from app.services.positions import service as position_service

__all__ = [
    "approve_requisition",
    "budget_flags",
    "load_rules",
    "release_vacancy",
    "reserve_vacancy",
    "submit_requisition",
]


def _money(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


async def load_rules(
    session: AsyncSession, user: User, scope: ApprovalScope
) -> list[approval_chain.RuleSpec]:
    """The active chain rules for one scope, as engine specs."""
    rows = await session.scalars(
        tenant_scoped(
            select(ApprovalChainRule).where(
                ApprovalChainRule.scope == scope,
                ApprovalChainRule.is_active.is_(True),
            ),
            ApprovalChainRule,
            user,
        )
    )
    return [
        approval_chain.RuleSpec(
            rule_id=str(row.id),
            sequence=row.sequence,
            approver_role=row.approver_role,
            ctc_min=Decimal(str(row.ctc_min)),
            ctc_max=None if row.ctc_max is None else Decimal(str(row.ctc_max)),
            grades=tuple(row.grades or ()),
            only_when_over_band=row.only_when_over_band,
            label=row.label or "",
        )
        for row in rows
    ]


def budget_flags(requisition: Requisition, *, live_headcount: int | None = None) -> list[dict]:
    """What the approver has to be told, in words, before they sign.

    Returned as a list rather than a boolean because an approver needs to know
    *which* budget this breaks. Empty means nothing to flag; a caller cannot
    mistake that for a falsy zero.
    """
    flags: list[dict] = []

    approved = requisition.approved_headcount
    if approved is not None:
        # Position management (slice 23) is configured. `live_headcount` is what
        # is already on the books for this position; the requisition adds to it.
        existing = live_headcount or 0
        requested = existing + requisition.openings
        if requested > approved:
            flags.append(
                {
                    "kind": "headcount",
                    "severity": "blocking_attention",
                    "message": (
                        f"This requisition takes the position to {requested} people "
                        f"against an approved headcount of {approved}. Approving it "
                        f"adds {requested - approved} unbudgeted "
                        f"{'seat' if requested - approved == 1 else 'seats'}."
                    ),
                }
            )
    else:
        flags.append(
            {
                "kind": "headcount_unknown",
                "severity": "informational",
                "message": (
                    "No approved headcount is configured for this position, so this "
                    "requisition has not been checked against one. It is being "
                    "approved on the CTC band alone."
                ),
            }
        )

    band_min = _money(requisition.budget_ctc_min)
    band_max = _money(requisition.budget_ctc_max)
    if band_max is None:
        flags.append(
            {
                "kind": "no_ctc_band",
                "severity": "informational",
                "message": (
                    "This requisition has no budgeted CTC ceiling, so no offer against "
                    "it can ever be flagged as over band."
                ),
            }
        )
    elif band_min is not None and band_min > band_max:
        flags.append(
            {
                "kind": "inverted_band",
                "severity": "blocking_attention",
                "message": (
                    f"The budgeted band runs from {band_min} down to {band_max}. No "
                    "offer can sit inside it."
                ),
            }
        )

    return flags


async def submit_requisition(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID,
    live_headcount: int | None = None,
    request: Request | None = None,
) -> dict:
    """Send a requisition for approval, resolving and recording its chain."""
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")

    if requisition.status not in (RequisitionStatus.DRAFT, RequisitionStatus.REJECTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This requisition is {requisition.status.value}, not a draft. Only a "
                "draft or a rejected requisition can be sent for approval."
            ),
        )
    if not requisition.justification:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A requisition needs a justification. An approver deciding on a "
                "headcount request with no stated reason is signing a number."
            ),
        )

    rules = await load_rules(session, user, ApprovalScope.REQUISITION)
    try:
        approval_chain.validate_rules(rules)
        # A requisition is approved on its ceiling, since that is the most it
        # can cost. Approving on the floor would route the expensive half of
        # every band to whoever signs the cheap end.
        ceiling = _money(requisition.budget_ctc_max) or Decimal(0)
        chain = approval_chain.resolve_chain(
            rules, ctc_annual=ceiling, grade=requisition.grade, over_band=False
        )
    except approval_chain.ApprovalChainError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    flags = budget_flags(requisition, live_headcount=live_headcount)
    # Position management (slice 23) has its own view of whether this seat is
    # approved, frozen, already filled, or budgeted below what is being asked
    # for. Asked here rather than duplicated, so the two modules cannot disagree.
    flags.extend(
        await position_service.off_budget_flags(
            session,
            user,
            position_id=requisition.position_id,
            department_id=requisition.department_id,
            requested_ctc_max=_money(requisition.budget_ctc_max),
        )
    )

    existing = await session.scalars(
        tenant_scoped(
            select(RequisitionApproval).where(
                RequisitionApproval.requisition_id == requisition.id
            ),
            RequisitionApproval,
            user,
        )
    )
    for row in existing.all():
        await session.delete(row)
    await session.flush()

    for step in chain:
        session.add(
            RequisitionApproval(
                tenant_id=user.tenant_id,
                requisition_id=requisition.id,
                sequence=step.sequence,
                approver_role=step.approver_role,
                label=step.label,
                reason=step.reason,
                # Snapshotted on every step, so "did they see the headcount
                # breach" is answerable about each approver individually.
                flags_shown=flags,
            )
        )

    requisition.status = RequisitionStatus.PENDING_APPROVAL
    requisition.raised_by_user_id = user.id
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=(
            f"Requisition {requisition.code} sent for approval to "
            f"{len(chain)} approver(s)"
            + (f" with {len(flags)} budget flag(s)" if flags else "")
        ),
        changes={"chain": [s.approver_role for s in chain], "flags": flags},
        request=request,
    )
    return {
        "requisition_id": str(requisition.id),
        "status": requisition.status.value,
        "chain": [
            {"sequence": s.sequence, "role": s.approver_role, "reason": s.reason}
            for s in chain
        ],
        "flags": flags,
    }


async def approve_requisition(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> dict:
    """Record one approver's decision.

    The first rejection ends it. Collecting the rest of the signatures on a
    requisition somebody has already refused wastes everyone's time and makes
    the refusal look negotiable.
    """
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")
    if requisition.status is not RequisitionStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This requisition is {requisition.status.value}, not awaiting approval.",
        )

    steps = (
        await session.scalars(
            tenant_scoped(
                select(RequisitionApproval)
                .where(RequisitionApproval.requisition_id == requisition.id)
                .order_by(RequisitionApproval.sequence),
                RequisitionApproval,
                user,
            )
        )
    ).all()
    pending = next((s for s in steps if s.decision is ApprovalDecision.PENDING), None)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Every approval step on this requisition has already been decided.",
        )

    pending.decision = (
        ApprovalDecision.APPROVED if approve else ApprovalDecision.REJECTED
    )
    pending.decided_by_user_id = user.id
    pending.decided_at = datetime.now(UTC)
    pending.note = note

    if not approve:
        requisition.status = RequisitionStatus.REJECTED
        remaining = 0
    else:
        remaining = sum(
            1 for s in steps if s.decision is ApprovalDecision.PENDING
        )
        if remaining == 0:
            requisition.status = RequisitionStatus.OPEN
            requisition.approved_by_user_id = user.id
            requisition.approved_at = datetime.now(UTC)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=(
            f"Requisition {requisition.code} "
            f"{'approved' if approve else 'rejected'} at step {pending.sequence} "
            f"({pending.approver_role})"
        ),
        changes={"note": note, "flags_shown": pending.flags_shown},
        request=request,
    )
    return {
        "requisition_id": str(requisition.id),
        "status": requisition.status.value,
        "step": pending.sequence,
        "remaining_steps": remaining,
    }


async def reserve_vacancy(
    session: AsyncSession, user: User, *, requisition: Requisition
) -> None:
    """Take a seat for an offer being released.

    Refuses when the requisition is full. Two offers released against one
    opening is a real thing that happens under pressure, and it is much cheaper
    to catch here than after both are accepted.
    """
    if requisition.status not in (RequisitionStatus.OPEN, RequisitionStatus.FULFILLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Requisition {requisition.code} is {requisition.status.value}. An offer "
                "cannot be released against a requisition that is not open."
            ),
        )
    if requisition.filled_count >= requisition.openings:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Requisition {requisition.code} has {requisition.openings} opening(s), "
                f"all of them already held by a live offer or a joined employee. Raise "
                "the opening count or wait for one to be released."
            ),
        )
    requisition.filled_count += 1
    if requisition.filled_count >= requisition.openings:
        requisition.status = RequisitionStatus.FULFILLED
    await session.flush()


async def release_vacancy(
    session: AsyncSession, user: User, *, requisition: Requisition, reason: str
) -> None:
    """Put a seat back after a revocation, decline, lapse or drop.

    The requisition reopens. Leaving it FULFILLED would show the seat as filled
    while it is empty, which is how a team waits a quarter for a role nobody
    knows is vacant.
    """
    if requisition.filled_count > 0:
        requisition.filled_count -= 1
    if (
        requisition.filled_count < requisition.openings
        and requisition.status is RequisitionStatus.FULFILLED
    ):
        requisition.status = RequisitionStatus.OPEN
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=(
            f"Vacancy reopened on requisition {requisition.code}: {reason}. "
            f"{requisition.open_vacancies} of {requisition.openings} now open."
        ),
    )
