"""Lifecycle events, probation and confirmation (slice 24).

Thin: parse, gate, delegate to `app.services.lifecycle.service`, commit.

The preview endpoint is the important one. An approver signing a cross-state
transfer without being told that Professional Tax changes is approving something
they did not read — so the consequences are computed before approval, shown, and
snapshotted onto the event as what that person was told.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.lifecycle import LifecycleEventType, LifecycleStatus
from app.models.user import UserRole
from app.schemas.lifecycle import (
    BulkRaise,
    EventDecision,
    EventRaise,
    EventRead,
    ProbationPolicyIn,
    ProbationPolicyRead,
    ReasonRequest,
)
from app.services.lifecycle import service as lifecycle_service

router = APIRouter(tags=["lifecycle"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


# ═══════════════════════════════════════════════════════════════════════════
# Events
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/lifecycle/preview", dependencies=[Depends(require_roles(*HR_ROLES))])
async def preview(payload: EventRaise, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """What this event would do, what it drags with it, and which letter it issues."""
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
    rows = await lifecycle_service.list_events(session, user, employee_id=event.employee_id)
    return next(row for row in rows if row.id == event.id)


@router.get("/lifecycle/events/{event_id}", response_model=EventRead)
async def get_event(event_id: uuid.UUID, user: CurrentUser, session: DbSession) -> EventRead:
    """One event: for an admin, or for the approver its chain is waiting on."""
    return await lifecycle_service.event_for_decision(session, user, event_id)


@router.post("/lifecycle/events/{event_id}/decide")
async def decide(
    event_id: uuid.UUID,
    payload: EventDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Approve or reject the current step: its approver, their delegate, or an admin.
    Effecting is separate, and happens on its own date."""
    result = await lifecycle_service.approve_event(
        session,
        user,
        event_id=event_id,
        approve=payload.approve,
        note=payload.note,
        request=request,
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
    request: Request,
) -> dict[str, Any]:
    result = await lifecycle_service.cancel_event(
        session, user, event_id=event_id, reason=payload.reason, request=request
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
    """Apply the whole cascade — record, pay, letter — or none of it.

    Refuses before the effective date: applying early would change the record
    before the decision was due to take hold.
    """
    try:
        result = await lifecycle_service.effect_event(
            session, user, event_id=event_id, on=on, request=request
        )
    finally:
        # A failed cascade still records the failure on the event.
        await session.commit()
    return result


@router.post("/lifecycle/effect-due", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def effect_due(
    user: CurrentUser, session: DbSession, on: date | None = None
) -> dict[str, Any]:
    """The sweep, run by hand. Each event is applied in its own savepoint."""
    result = await lifecycle_service.effect_due_events(session, user, on=on)
    await session.commit()
    return result


@router.get(
    "/lifecycle/events",
    response_model=list[EventRead],
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def list_events(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    event_status: LifecycleStatus | None = None,
    event_type: LifecycleEventType | None = None,
) -> list[EventRead]:
    """Every event HR may see; a manager's reporting line only."""
    return await lifecycle_service.list_events(
        session, user, employee_id=employee_id, event_status=event_status, event_type=event_type
    )


# ═══════════════════════════════════════════════════════════════════════════
# History
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/lifecycle/employees/{employee_id}/history",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def history(employee_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Every event, effective date, approver and letter — failures included."""
    return await lifecycle_service.employee_history(session, user, employee_id=employee_id)


@router.get(
    "/lifecycle/employees/{employee_id}/state",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def state_on(
    employee_id: uuid.UUID, on: date, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """The employee record as it stood on a date, rebuilt from effected events."""
    return await lifecycle_service.state_on(session, user, employee_id=employee_id, on=on)


# ═══════════════════════════════════════════════════════════════════════════
# Bulk
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/lifecycle/bulk", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def bulk(
    payload: BulkRaise, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """One decision producing many events, with per-employee results."""
    result = await lifecycle_service.raise_bulk(
        session,
        user,
        name=payload.name,
        event_type=payload.event_type,
        effective_from=payload.effective_from,
        reason=payload.reason,
        rows=[row.model_dump() for row in payload.rows],
        approve_individually=payload.approve_individually,
        request=request,
    )
    await session.commit()
    return result


@router.get(
    "/lifecycle/batches",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def list_batches(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await lifecycle_service.list_batches(session, user)


# ═══════════════════════════════════════════════════════════════════════════
# Probation
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/lifecycle/probation/policy",
    response_model=ProbationPolicyRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def get_probation_policy(user: CurrentUser, session: DbSession) -> ProbationPolicyRead:
    return await lifecycle_service.get_probation_policy(session, user)


@router.put("/lifecycle/probation/policy", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def set_probation_policy(
    payload: ProbationPolicyIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Choose what silence means. There is no default and there will not be one."""
    result = await lifecycle_service.set_probation_policy(session, user, payload, request=request)
    await session.commit()
    return result


@router.post("/lifecycle/probation/remind", dependencies=[Depends(require_roles(*HR_ROLES))])
async def send_reminders(
    user: CurrentUser, session: DbSession, on: date | None = None
) -> dict[str, Any]:
    """The escalating reminder sweep: manager, manager again, skip level, HR."""
    result = await lifecycle_service.send_probation_reminders(session, user, on=on)
    await session.commit()
    return result


@router.get("/lifecycle/probation/drift", dependencies=[Depends(require_roles(*HR_ROLES))])
async def drift(user: CurrentUser, session: DbSession, on: date | None = None) -> dict[str, Any]:
    """Reviews nobody decided, and what this tenant's policy says happens."""
    return await lifecycle_service.resolve_probation_drift(session, user, on=on)
