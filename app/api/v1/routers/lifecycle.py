"""Lifecycle events, probation and confirmation (slice 24).

The preview endpoint is the important one. An approver signing a cross-state
transfer without being told that Professional Tax changes is approving something
they did not read — so the consequences are computed before approval, shown, and
snapshotted onto the event as what that person was told.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.lifecycle import (
    LifecycleBatch,
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    ProbationPolicy,
    SilencePolicy,
)
from app.models.user import UserRole
from app.services.lifecycle import service as lifecycle_service

router = APIRouter(tags=["lifecycle"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)
VIEW_ROLES = (*HR_ROLES, UserRole.MANAGER)


# ── Contracts ───────────────────────────────────────────────────────────────


class EventRaise(BaseModel):
    employee_id: uuid.UUID
    event_type: LifecycleEventType
    effective_from: date
    #: Only the fields this event type is allowed to change. Anything else is
    #: refused — an event that can change any field is a field edit with a nicer
    #: name, and the point of these is that they are not field edits.
    new_values: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)
    new_ctc_annual: Decimal | None = Field(default=None, gt=0)


class EventDecision(BaseModel):
    approve: bool = True
    note: str = Field(default="", max_length=2000)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    event_type: LifecycleEventType
    effective_from: date
    status: LifecycleStatus
    previous_values: dict
    new_values: dict
    reason: str
    new_ctc_annual: Decimal | None
    consequences_shown: list
    effected_at: Any | None
    failure_reason: str | None


class BulkRow(BaseModel):
    employee_id: uuid.UUID
    new_values: dict[str, Any] = Field(default_factory=dict)
    new_ctc_annual: Decimal | None = Field(default=None, gt=0)
    reason: str | None = None


class BulkRaise(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    event_type: LifecycleEventType
    effective_from: date
    reason: str = Field(min_length=1, max_length=2000)
    rows: list[BulkRow] = Field(min_length=1, max_length=2000)
    #: Approving four hundred promotions individually is theatre; approving a
    #: mass termination in one click is not. Per tenant, per batch.
    approve_individually: bool = True


class ProbationPolicyIn(BaseModel):
    """No default on `silence_policy`.

    Auto-confirming and requiring a decision lead to opposite outcomes for a real
    person, so the tenant chooses. A missing value is a 422, not a fallback.
    """

    silence_policy: SilencePolicy
    grace_days: int = Field(default=3, ge=0, le=90)
    confirmation_raises_salary: bool = False
    hr_escalation_user_id: uuid.UUID | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Events
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/lifecycle/preview", dependencies=[Depends(require_roles(*HR_ROLES))])
async def preview(
    payload: EventRaise, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """What this event would do, and what it drags with it.

    The approval screen. Cross-state transfers change Professional Tax, the
    holiday calendar and the geofence; a pay change effective in the past brings
    arrears. None of that follows from reading the field diff.
    """
    return await lifecycle_service.preview_event(
        session,
        user,
        employee_id=payload.employee_id,
        event_type=payload.event_type,
        effective_from=payload.effective_from,
        new_values=payload.new_values,
        reason=payload.reason,
        new_ctc_annual=payload.new_ctc_annual,
    )


@router.post(
    "/lifecycle/events",
    response_model=EventRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def raise_event(
    payload: EventRaise, user: CurrentUser, session: DbSession, request: Request
) -> EventRead:
    event = await lifecycle_service.raise_event(
        session,
        user,
        employee_id=payload.employee_id,
        event_type=payload.event_type,
        effective_from=payload.effective_from,
        new_values=payload.new_values,
        reason=payload.reason,
        new_ctc_annual=payload.new_ctc_annual,
        request=request,
    )
    await session.commit()
    await session.refresh(event)
    return EventRead.model_validate(event)


@router.post(
    "/lifecycle/events/{event_id}/decide",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def decide(
    event_id: uuid.UUID,
    payload: EventDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Approve or reject. Effecting is separate, and happens on its own date."""
    result = await lifecycle_service.approve_event(
        session, user, event_id=event_id, approve=payload.approve,
        note=payload.note, request=request,
    )
    await session.commit()
    return result


@router.post(
    "/lifecycle/events/{event_id}/cancel",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def cancel(
    event_id: uuid.UUID,
    payload: ReasonRequest,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    result = await lifecycle_service.cancel_event(
        session, user, event_id=event_id, reason=payload.reason
    )
    await session.commit()
    return result


@router.post(
    "/lifecycle/events/{event_id}/effect",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def effect(
    event_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    on: date | None = None,
) -> dict[str, Any]:
    """Apply the whole cascade, or none of it.

    Refuses before the effective date: applying early would change the record
    before the decision was due to take hold.
    """
    result = await lifecycle_service.effect_event(
        session, user, event_id=event_id, on=on, request=request
    )
    await session.commit()
    return result


@router.post("/lifecycle/effect-due", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def effect_due(
    user: CurrentUser, session: DbSession, on: date | None = None
) -> dict[str, Any]:
    """The scheduled sweep.

    A promotion approved in March effective April lands on 1 April without
    anybody remembering. Each event is applied in its own savepoint, so one
    failure does not take the rest of the batch with it.
    """
    result = await lifecycle_service.effect_due_events(session, user, on=on)
    await session.commit()
    return result


@router.get(
    "/lifecycle/events",
    response_model=list[EventRead],
    dependencies=[Depends(require_roles(*VIEW_ROLES))],
)
async def list_events(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    event_status: LifecycleStatus | None = None,
) -> list[EventRead]:
    stmt = select(LifecycleEvent).order_by(LifecycleEvent.effective_from.desc())
    if employee_id is not None:
        stmt = stmt.where(LifecycleEvent.employee_id == employee_id)
    if event_status is not None:
        stmt = stmt.where(LifecycleEvent.status == event_status)
    rows = await session.scalars(tenant_scoped(stmt, LifecycleEvent, user))
    return [EventRead.model_validate(r) for r in rows.all()]


# ═══════════════════════════════════════════════════════════════════════════
# History
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/lifecycle/employees/{employee_id}/history",
    dependencies=[Depends(require_roles(*VIEW_ROLES))],
)
async def history(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Every event, effective date, approver and letter.

    What an audit or a dispute asks for. Cancelled and failed events are
    included — "we tried and it did not go through" is part of the history.
    """
    return await lifecycle_service.employee_history(
        session, user, employee_id=employee_id
    )


@router.get(
    "/lifecycle/employees/{employee_id}/state",
    dependencies=[Depends(require_roles(*VIEW_ROLES))],
)
async def state_on(
    employee_id: uuid.UUID, on: date, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """The employee record as it stood on a date.

    Reconstructed by undoing effected events backwards from today, so there is no
    separate history table to fall out of step with the record.
    """
    return await lifecycle_service.state_on(
        session, user, employee_id=employee_id, on=on
    )


# ═══════════════════════════════════════════════════════════════════════════
# Bulk
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/lifecycle/bulk", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def bulk(
    payload: BulkRaise, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """One decision producing many events, with per-employee results.

    A four-hundred-person increment cycle where one employee fails validation
    must not fail the other 399, and the one that failed has to be named — a
    count of successes leaves somebody reconciling two lists by hand.
    """
    result = await lifecycle_service.raise_bulk(
        session,
        user,
        name=payload.name,
        event_type=payload.event_type,
        effective_from=payload.effective_from,
        reason=payload.reason,
        rows=[row.model_dump() for row in payload.rows],
        approve_individually=payload.approve_individually,
    )
    await session.commit()
    return result


@router.get("/lifecycle/batches", dependencies=[Depends(require_roles(*VIEW_ROLES))])
async def list_batches(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    rows = await session.scalars(
        tenant_scoped(
            select(LifecycleBatch).order_by(LifecycleBatch.created_at.desc()),
            LifecycleBatch,
            user,
        )
    )
    return [
        {
            "batch_id": str(b.id),
            "name": b.name,
            "event_type": b.event_type.value,
            "effective_from": b.effective_from.isoformat(),
            "events": len(b.events),
            "approve_individually": b.approve_individually,
        }
        for b in rows.all()
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Probation
# ═══════════════════════════════════════════════════════════════════════════


@router.put(
    "/lifecycle/probation/policy", dependencies=[Depends(require_roles(*ADMIN_ROLES))]
)
async def set_probation_policy(
    payload: ProbationPolicyIn, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Choose what silence means.

    There is no default and there will not be one. Auto-confirming and requiring
    a decision lead to opposite outcomes for a real person, and a product that
    picked one would be making an employment decision on a client's behalf.
    """
    existing = await session.scalar(
        tenant_scoped(select(ProbationPolicy), ProbationPolicy, user)
    )
    policy = existing or ProbationPolicy(
        tenant_id=user.tenant_id, silence_policy=payload.silence_policy
    )
    policy.silence_policy = payload.silence_policy
    policy.grace_days = payload.grace_days
    policy.confirmation_raises_salary = payload.confirmation_raises_salary
    policy.hr_escalation_user_id = payload.hr_escalation_user_id
    policy.configured_by_user_id = user.id
    session.add(policy)
    await session.commit()
    return {
        "silence_policy": policy.silence_policy.value,
        "grace_days": policy.grace_days,
        "note": (
            "Silence now means "
            + {
                SilencePolicy.AUTO_CONFIRM: "the employee is confirmed.",
                SilencePolicy.REQUIRE_DECISION: (
                    "nothing happens until somebody decides."
                ),
                SilencePolicy.AUTO_EXTEND_ONCE: (
                    "probation extends once, then escalates."
                ),
            }[policy.silence_policy]
        ),
    }


@router.post(
    "/lifecycle/probation/remind", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def send_reminders(
    user: CurrentUser, session: DbSession, on: date | None = None
) -> dict[str, Any]:
    """The escalating reminder sweep.

    Manager, manager again, skip level, HR. Each step is a wider circle of people
    who could act, and the last is somebody whose job it is to notice.
    """
    result = await lifecycle_service.send_probation_reminders(session, user, on=on)
    await session.commit()
    return result


@router.get(
    "/lifecycle/probation/drift", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def drift(
    user: CurrentUser, session: DbSession, on: date | None = None
) -> dict[str, Any]:
    """Reviews nobody decided, and what this tenant's policy says happens."""
    return await lifecycle_service.resolve_probation_drift(session, user, on=on)
