"""Ad-hoc payroll adjustments: incentives, one-off bonuses, recoveries, attachments.

Thin: parse, gate, delegate to `app.services.payroll.adjustments`, serialise.
Payroll (admin and sub-admin) keys them in; an admin who did not key one in
approves it (maker-checker, enforced in the service).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.payroll_inputs import AdjustmentCategory, AdjustmentStatus
from app.models.user import UserRole
from app.schemas.common import Page
from app.schemas.payroll_inputs import (
    AdjustmentCreate,
    AdjustmentDecision,
    AdjustmentImportResult,
    AdjustmentRead,
)
from app.services.payroll import adjustments as adjustment_service

PAYROLL_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]

router = APIRouter(
    prefix="/payroll/adjustments",
    tags=["payroll"],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)

_MAX_IMPORT_BYTES = 2 * 1024 * 1024


@router.get("", response_model=Page[AdjustmentRead])
async def list_adjustments(
    user: CurrentUser,
    session: DbSession,
    adjustment_status: AdjustmentStatus | None = Query(default=None, alias="status"),
    employee_id: uuid.UUID | None = None,
    category: AdjustmentCategory | None = None,
    period_year: int | None = Query(default=None, ge=2000, le=2100),
    period_month: int | None = Query(default=None, ge=1, le=12),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[AdjustmentRead]:
    rows, total = await adjustment_service.list_adjustments(
        session,
        user,
        adjustment_status=adjustment_status,
        employee_id=employee_id,
        category=category,
        period_year=period_year,
        period_month=period_month,
        page=page,
        page_size=page_size,
    )
    return Page[AdjustmentRead](items=rows, total=total, page=page, page_size=page_size)


@router.post("", response_model=AdjustmentRead, status_code=status.HTTP_201_CREATED)
async def create_adjustment(
    payload: AdjustmentCreate, user: CurrentUser, session: DbSession, request: Request
) -> AdjustmentRead:
    return await adjustment_service.create(session, user, payload, request=request)


@router.post("/import", response_model=AdjustmentImportResult)
async def import_adjustments(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: UploadFile = File(...),
) -> AdjustmentImportResult:
    data = await file.read(_MAX_IMPORT_BYTES + 1)
    if len(data) > _MAX_IMPORT_BYTES:
        return AdjustmentImportResult(created=0, errors=["The file is larger than 2 MB."])
    return await adjustment_service.import_csv(session, user, data, request=request)


@router.get("/{adjustment_id}", response_model=AdjustmentRead)
async def get_adjustment(
    adjustment_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> AdjustmentRead:
    return await adjustment_service.get(session, user, adjustment_id)


@router.post("/{adjustment_id}/approve", response_model=AdjustmentRead, dependencies=ADMIN_ONLY)
async def approve_adjustment(
    adjustment_id: uuid.UUID,
    payload: AdjustmentDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AdjustmentRead:
    return await adjustment_service.approve(
        session, user, adjustment_id, note=payload.note, request=request
    )


@router.post("/{adjustment_id}/reject", response_model=AdjustmentRead, dependencies=ADMIN_ONLY)
async def reject_adjustment(
    adjustment_id: uuid.UUID,
    payload: AdjustmentDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AdjustmentRead:
    return await adjustment_service.reject(
        session, user, adjustment_id, note=payload.note, request=request
    )


@router.post("/{adjustment_id}/cancel", response_model=AdjustmentRead)
async def cancel_adjustment(
    adjustment_id: uuid.UUID,
    payload: AdjustmentDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AdjustmentRead:
    return await adjustment_service.cancel(
        session, user, adjustment_id, note=payload.note, request=request
    )
