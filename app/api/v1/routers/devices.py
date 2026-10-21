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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.attendance import KioskPunchIn
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
from app.services.attendance import kiosk
from app.services.identity import adms
from app.services.identity import devices as device_service

router = APIRouter(tags=["devices"])
#: ADMS by serial number lives at the server root, outside the versioned
#: prefix: firmware that takes only a host and port always calls `/iclock/...`.
iclock_router = APIRouter(tags=["devices"])

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
    rows = await device_service.list_exceptions(session, user, include_resolved=include_resolved)
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
        accepts_serial_push=payload.accepts_serial_push,
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


# ── ADMS / iclock push (eSSL, ZKTeco) ───────────────────────────────────────
#
# Plain text in and out, as the devices expect. A device that cannot be tied to
# a registered device gets a 401 and nothing else.


def _plain(text: str, code: int = status.HTTP_200_OK) -> PlainTextResponse:
    return PlainTextResponse(text, status_code=code)


async def _adms_device(session, token: str | None, serial: str):
    if token is not None:
        return await adms.device_by_token(session, token, serial)
    return await adms.device_by_serial(session, serial)


async def _adms_handshake(session, token: str | None, serial: str) -> PlainTextResponse:
    try:
        device = await _adms_device(session, token, serial)
    except adms.AdmsError as exc:
        return _plain(str(exc), status.HTTP_401_UNAUTHORIZED)
    device_service.touch(device)
    await session.commit()
    return _plain(adms.handshake(device))


async def _adms_upload(
    session, token: str | None, serial: str, table: str, request: Request
) -> PlainTextResponse:
    try:
        device = await _adms_device(session, token, serial)
    except adms.AdmsError as exc:
        return _plain(str(exc), status.HTTP_401_UNAUTHORIZED)
    body = (await request.body()).decode("utf-8", errors="replace")
    return _plain(await adms.receive(session, device, table=table, body=body))


async def _adms_poll(session, token: str | None, serial: str) -> PlainTextResponse:
    try:
        device = await _adms_device(session, token, serial)
    except adms.AdmsError as exc:
        return _plain(str(exc), status.HTTP_401_UNAUTHORIZED)
    device_service.touch(device)
    await session.commit()
    # No commands are ever queued for a device.
    return _plain("OK")


@router.get("/adms/{token}/iclock/cdata", response_class=PlainTextResponse)
async def adms_handshake_token(
    token: str, session: DbSession, serial: str = Query(default="", alias="SN", max_length=128)
) -> PlainTextResponse:
    """ADMS handshake, the device identified by its push token in the path."""
    return await _adms_handshake(session, token, serial)


@router.post("/adms/{token}/iclock/cdata", response_class=PlainTextResponse)
async def adms_upload_token(
    token: str,
    request: Request,
    session: DbSession,
    serial: str = Query(default="", alias="SN", max_length=128),
    table: str = Query(default="", max_length=32),
) -> PlainTextResponse:
    """ADMS upload. Attendance logs are ingested; other tables are acknowledged."""
    return await _adms_upload(session, token, serial, table, request)


@router.get("/adms/{token}/iclock/getrequest", response_class=PlainTextResponse)
async def adms_poll_token(
    token: str, session: DbSession, serial: str = Query(default="", alias="SN", max_length=128)
) -> PlainTextResponse:
    return await _adms_poll(session, token, serial)


@router.post("/adms/{token}/iclock/devicecmd", response_class=PlainTextResponse)
async def adms_command_reply_token(
    token: str, session: DbSession, serial: str = Query(default="", alias="SN", max_length=128)
) -> PlainTextResponse:
    return await _adms_poll(session, token, serial)


@iclock_router.get("/iclock/cdata", response_class=PlainTextResponse)
async def adms_handshake_serial(
    session: DbSession, serial: str = Query(default="", alias="SN", max_length=128)
) -> PlainTextResponse:
    """ADMS handshake by serial number, for devices HR has allowed to push that way."""
    return await _adms_handshake(session, None, serial)


@iclock_router.post("/iclock/cdata", response_class=PlainTextResponse)
async def adms_upload_serial(
    request: Request,
    session: DbSession,
    serial: str = Query(default="", alias="SN", max_length=128),
    table: str = Query(default="", max_length=32),
) -> PlainTextResponse:
    return await _adms_upload(session, None, serial, table, request)


@iclock_router.get("/iclock/getrequest", response_class=PlainTextResponse)
async def adms_poll_serial(
    session: DbSession, serial: str = Query(default="", alias="SN", max_length=128)
) -> PlainTextResponse:
    return await _adms_poll(session, None, serial)


@iclock_router.post("/iclock/devicecmd", response_class=PlainTextResponse)
async def adms_command_reply_serial(
    session: DbSession, serial: str = Query(default="", alias="SN", max_length=128)
) -> PlainTextResponse:
    return await _adms_poll(session, None, serial)


# ── Shared kiosk ────────────────────────────────────────────────────────────
#
# No user session: the kiosk's device token in the path establishes the
# workspace, and each punch is the person's employee code and PIN.


@router.get("/kiosk/{token}")
async def kiosk_details(token: str, session: DbSession) -> dict:
    """The kiosk's name, place and photo rule, for its page."""
    return await kiosk.kiosk_info(session, token)


@router.post("/kiosk/{token}/punch")
async def kiosk_punch(token: str, payload: KioskPunchIn, session: DbSession) -> dict:
    """Punch in or out with an employee code and PIN, or a badge; the direction follows
    the last punch."""
    return await kiosk.kiosk_punch(
        session,
        token,
        employee_code=payload.employee_code,
        pin=payload.pin,
        badge=payload.badge,
        photo=payload.photo,
    )
