"""Biometric device endpoints (slice 04, part C).

Administration is HR-gated; registering, editing and rotating a token need an
admin. The push endpoint has no user session: the device's token
establishes the tenant, and only a hash of it is stored.

Pull ingestion (polling devices or an on-prem agent on a schedule) is not built
— it needs live vendor devices. `GET /devices/ingestion-status` says so on the
response.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.devices import (
    DeviceCreate,
    DeviceRead,
    DeviceUpdate,
    DeviceWithToken,
    ExceptionResolve,
    IngestionStatus,
    IngestResponse,
    PunchBatch,
    PunchExceptionRead,
)
from app.services.identity import devices as device_service

router = APIRouter(tags=["devices"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


def _with_token(device, token: str) -> DeviceWithToken:
    return DeviceWithToken(**DeviceRead.model_validate(device).model_dump(), push_token=token)


# ── Administration ──────────────────────────────────────────────────────────


@router.get(
    "/devices",
    response_model=list[DeviceRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_devices(user: CurrentUser, session: DbSession) -> list[DeviceRead]:
    rows = await device_service.list_devices(session, user)
    return [DeviceRead.model_validate(row) for row in rows]


@router.post(
    "/devices",
    response_model=DeviceWithToken,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def register_device(
    payload: DeviceCreate, user: CurrentUser, session: DbSession, request: Request
) -> DeviceWithToken:
    """Register a device and mint its push token — returned once, stored as a hash."""
    device, token = await device_service.create_device(
        session,
        user,
        device_id=payload.device_id,
        name=payload.name,
        vendor=payload.vendor,
        work_location_id=payload.work_location_id,
        assumed_timezone=payload.assumed_timezone,
        silence_threshold_minutes=payload.silence_threshold_minutes,
        request=request,
    )
    return _with_token(device, token)


@router.get(
    "/devices/ingestion-status",
    response_model=IngestionStatus,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def ingestion_status(user: CurrentUser, session: DbSession) -> IngestionStatus:
    return await device_service.ingestion_status(session, user)


@router.get(
    "/devices/silent",
    response_model=list[DeviceRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_silent_devices(user: CurrentUser, session: DbSession) -> list[DeviceRead]:
    """Devices past their silence threshold. No alert is sent; this is the report."""
    rows = await device_service.silent_devices(session, user.tenant_id)
    return [DeviceRead.model_validate(row) for row in rows]


@router.get(
    "/devices/exceptions",
    response_model=list[PunchExceptionRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_exceptions(
    user: CurrentUser, session: DbSession, include_resolved: bool = False
) -> list[PunchExceptionRead]:
    """Punches that could not be attributed. They did not fail the batch."""
    rows = await device_service.list_exceptions(
        session, user, include_resolved=include_resolved
    )
    return [PunchExceptionRead.model_validate(row) for row in rows]


@router.post(
    "/devices/exceptions/{exception_id}/resolve",
    response_model=PunchExceptionRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def resolve_exception(
    exception_id: uuid.UUID,
    payload: ExceptionResolve,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> PunchExceptionRead:
    """Map an orphaned punch to an employee and admit it to attendance."""
    record = await device_service.resolve_exception(
        session, user, exception_id, payload.employee_id, request=request
    )
    return PunchExceptionRead.model_validate(record)


@router.patch(
    "/devices/{device_pk}",
    response_model=DeviceRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def update_device(
    device_pk: uuid.UUID,
    payload: DeviceUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> DeviceRead:
    device = await device_service.update_device(
        session,
        user,
        device_pk,
        name=payload.name,
        work_location_id=payload.work_location_id,
        clear_work_location=payload.clear_work_location,
        assumed_timezone=payload.assumed_timezone,
        silence_threshold_minutes=payload.silence_threshold_minutes,
        enabled=payload.enabled,
        request=request,
    )
    return DeviceRead.model_validate(device)


@router.post(
    "/devices/{device_pk}/rotate-token",
    response_model=DeviceWithToken,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def rotate_token(
    device_pk: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> DeviceWithToken:
    """A new token, shown once. The previous token stops working immediately."""
    device, token = await device_service.rotate_token(session, user, device_pk, request=request)
    return _with_token(device, token)


# ── Ingestion ───────────────────────────────────────────────────────────────


@router.post("/devices/{token}/punches", response_model=IngestResponse)
async def push_punches(token: str, payload: PunchBatch, session: DbSession) -> IngestResponse:
    """Receive a batch from a device, authenticated by its push token.

    Never fails a batch over a bad row: an unreadable payload or an unknown
    employee code becomes a `DevicePunchException` and the rest is ingested.
    """
    try:
        device = await device_service.authenticate_device(session, token)
    except device_service.DeviceError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    result = await device_service.ingest_punches(session, device, payload.punches)
    return IngestResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        exceptions=result.exceptions,
        unmapped_employee_codes=result.unmapped_codes or [],
    )
