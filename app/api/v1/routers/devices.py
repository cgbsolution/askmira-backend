"""Biometric device endpoints (slice 04)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.identity import (
    DeviceIntegration,
    DevicePunchException,
    DeviceStatus,
    DeviceVendor,
)
from app.models.user import UserRole
from app.services.identity import devices as device_service

router = APIRouter(tags=["devices"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)


class DeviceCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    vendor: DeviceVendor = DeviceVendor.GENERIC
    work_location_id: uuid.UUID | None = None
    assumed_timezone: str = Field(
        default="Asia/Kolkata",
        description=(
            "Devices usually send local wall-clock time with no offset. This is the "
            "timezone that assumption uses, and it is recorded on every punch."
        ),
    )
    silence_threshold_minutes: int = Field(default=180, ge=15, le=10_080)


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: str
    name: str
    vendor: DeviceVendor
    status: DeviceStatus
    assumed_timezone: str
    last_seen_at: datetime | None
    silence_threshold_minutes: int
    push_token_hint: str | None


class DeviceCreated(DeviceRead):
    """The plaintext push token, shown exactly once."""

    push_token: str


class PunchBatch(BaseModel):
    """Whatever shape the device sends. Normalisation happens server-side."""

    punches: list[dict[str, Any]] = Field(min_length=1, max_length=10_000)


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    exceptions: int
    unmapped_employee_codes: list[str]


class PunchExceptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: str
    employee_code: str | None
    punched_at: datetime
    reason: str
    resolved_at: datetime | None


# ── Administration ──────────────────────────────────────────────────────────


@router.get(
    "/devices",
    response_model=list[DeviceRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_devices(user: CurrentUser, session: DbSession) -> list[DeviceRead]:
    stmt = tenant_scoped(
        select(DeviceIntegration).order_by(DeviceIntegration.name), DeviceIntegration, user
    )
    return [DeviceRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


@router.post(
    "/devices",
    response_model=DeviceCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN))],
)
async def register_device(
    payload: DeviceCreate, user: CurrentUser, session: DbSession, request: Request
) -> DeviceCreated:
    """Register a device and mint its push token.

    The token is returned once and never again — only its hash is stored.
    """
    existing = await session.scalar(
        tenant_scoped(
            select(DeviceIntegration.id).where(
                DeviceIntegration.device_id == payload.device_id
            ),
            DeviceIntegration,
            user,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A device with id {payload.device_id} is already registered",
        )

    device, token = await device_service.create_device(
        session,
        user,
        device_id=payload.device_id,
        name=payload.name,
        vendor=payload.vendor,
        work_location_id=payload.work_location_id,
        assumed_timezone=payload.assumed_timezone,
        request=request,
    )
    device.silence_threshold_minutes = payload.silence_threshold_minutes
    await session.commit()
    await session.refresh(device)

    return DeviceCreated.model_validate(device).model_copy(update={"push_token": token})


@router.get(
    "/devices/silent",
    response_model=list[DeviceRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_silent_devices(user: CurrentUser, session: DbSession) -> list[DeviceRead]:
    """Devices past their silence threshold.

    Worth checking before a payroll run: silent device failure is how a month
    of attendance goes missing, and nobody notices until payroll.
    """
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
    """Punches that could not be attributed to an employee.

    These did not fail the batch — they are held here for HR to map.
    """
    stmt = tenant_scoped(
        select(DevicePunchException).order_by(DevicePunchException.punched_at.desc()),
        DevicePunchException,
        user,
    )
    if not include_resolved:
        stmt = stmt.where(DevicePunchException.resolved_at.is_(None))
    return [
        PunchExceptionRead.model_validate(row) for row in (await session.scalars(stmt)).all()
    ]


# ── Ingestion ───────────────────────────────────────────────────────────────


@router.post("/devices/{token}/punches", response_model=IngestResponse)
async def push_punches(
    token: str, payload: PunchBatch, session: DbSession
) -> IngestResponse:
    """Receive a batch from a device.

    Authenticated by the push token, which establishes the tenant — the device
    has no user session.

    Never fails a batch over a bad row. An unreadable payload or an unknown
    employee code becomes a `DevicePunchException` and the rest is ingested:
    one unmapped person must not take out a whole site's attendance.
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


@router.post(
    "/devices/exceptions/{exception_id}/resolve",
    response_model=PunchExceptionRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def resolve_exception(
    exception_id: uuid.UUID,
    employee_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> PunchExceptionRead:
    """Map an orphaned punch to an employee and admit it to attendance."""
    from app.models.attendance import PunchDirection, PunchSource
    from app.models.audit import AuditAction
    from app.models.employee import Employee
    from app.services import audit
    from app.services.attendance import service as attendance_service

    record = await session.scalar(
        tenant_scoped(
            select(DevicePunchException).where(DevicePunchException.id == exception_id),
            DevicePunchException,
            user,
        )
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exception not found")
    if record.resolved_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This exception is already resolved"
        )

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    direction_raw = str(record.raw_payload.get("direction", "")).lower()
    await attendance_service.record_punch(
        session,
        user,
        employee_id=employee.id,
        punched_at=record.punched_at,
        direction=PunchDirection.OUT if direction_raw == "out" else PunchDirection.IN,
        source=PunchSource.BIOMETRIC,
        device_id=record.device_id,
        raw_payload={**record.raw_payload, "_resolved_from_exception": str(record.id)},
        commit=False,
    )

    from datetime import UTC
    from datetime import datetime as dt

    record.resolved_at = dt.now(UTC)
    record.resolved_by_user_id = user.id
    record.resolved_employee_id = employee.id

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="device_punch_exception",
        entity_id=record.id,
        actor=user,
        changes={
            "employee_code": record.employee_code,
            "mapped_to": employee.employee_code,
        },
        summary=(
            f"Mapped orphaned punch from device code {record.employee_code} to "
            f"{employee.employee_code}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return PunchExceptionRead.model_validate(record)
