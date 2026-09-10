"""Biometric device ingestion.

Indian device estates are mixed — a client will have eSSL at one site, Matrix
at another, and something nobody can identify at the third. So ingestion is
built around three assumptions that are always true in practice:

1. **Devices re-send.** Network flaps, buffered offline records, an operator
   pressing sync twice. Ingestion deduplicates on
   `(device_id, employee_code, timestamp)` rather than trusting the device to
   send each punch once.

2. **Devices send local wall-clock time with no offset.** Almost universally.
   The device's configured timezone is applied, and the assumption is recorded
   on the punch's `raw_payload` — so when a punch looks an hour wrong, the
   evidence of what was assumed is right there.

3. **An unknown employee code is normal.** A new joiner enrolled on the device
   before HR created their record, or a stale enrolment. The punch goes to
   `DevicePunchException` for HR to map, and ingestion continues. Rejecting the
   batch would take out an entire site's attendance for one unmapped person.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendancePunch, PunchDirection, PunchSource
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.identity import (
    DeviceIntegration,
    DevicePunchException,
    DeviceStatus,
    DeviceVendor,
)
from app.services import audit

__all__ = [
    "DeviceError",
    "IngestResult",
    "NormalisedPunch",
    "authenticate_device",
    "create_device",
    "ingest_punches",
    "normalise",
    "silent_devices",
]


class DeviceError(RuntimeError):
    """The device token is unknown, or a payload could not be normalised."""


@dataclass(frozen=True, slots=True)
class NormalisedPunch:
    employee_code: str
    punched_at: datetime
    direction: PunchDirection
    raw: dict[str, Any]


@dataclass(slots=True)
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    exceptions: int = 0
    unmapped_codes: list[str] | None = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_device(
    session: AsyncSession,
    user,
    *,
    device_id: str,
    name: str,
    vendor: DeviceVendor,
    work_location_id: uuid.UUID | None = None,
    assumed_timezone: str = "Asia/Kolkata",
    request=None,
) -> tuple[DeviceIntegration, str]:
    """Register a device. Returns `(device, plaintext push token)`.

    The token is shown once; only its hash is stored.
    """
    plaintext = f"dev_{secrets.token_urlsafe(32)}"
    device = DeviceIntegration(
        tenant_id=user.tenant_id,
        device_id=device_id,
        name=name,
        vendor=vendor,
        work_location_id=work_location_id,
        push_token_hash=_hash_token(plaintext),
        push_token_hint=f"{plaintext[:8]}…{plaintext[-4:]}",
        assumed_timezone=assumed_timezone,
    )
    session.add(device)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="device_integration",
        entity_id=device.id,
        actor=user,
        changes={"device_id": device_id, "vendor": vendor.value},
        summary=f"Registered attendance device {name} ({device_id})",
        request=request,
    )
    await session.commit()
    await session.refresh(device)
    return device, plaintext


async def authenticate_device(session: AsyncSession, token: str) -> DeviceIntegration:
    """Resolve a push token to its device.

    Not tenant-scoped: the token establishes the tenant, and it is looked up by
    a unique hash rather than by anything enumerable.
    """
    device = await session.scalar(
        select(DeviceIntegration).where(
            DeviceIntegration.push_token_hash == _hash_token(token),
            DeviceIntegration.status != DeviceStatus.DISABLED,
        )
    )
    if device is None:
        raise DeviceError("Unknown or disabled device token")
    return device


# ── Vendor normalisation ────────────────────────────────────────────────────


def _parse_timestamp(value: Any, timezone_name: str) -> datetime:
    """Turn a device timestamp into an aware UTC datetime.

    A value with no offset is interpreted in the device's configured timezone —
    that is assumption 2 in the module docstring, and it is recorded on the
    punch rather than left implicit.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, int | float):
        # Epoch seconds are unambiguous, so no assumption is needed.
        return datetime.fromtimestamp(float(value), tz=UTC)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise DeviceError(f"Could not read the timestamp '{value}'") from None

    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)

    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Kolkata")
    return parsed.replace(tzinfo=zone).astimezone(UTC)


# Per-vendor field names. Kept as data rather than as a class per vendor,
# because the only thing that actually differs is what the keys are called.
_VENDOR_FIELDS: dict[DeviceVendor, dict[str, tuple[str, ...]]] = {
    DeviceVendor.ESSL: {
        "code": ("UserID", "userid", "user_id", "EmployeeCode"),
        "time": ("LogTime", "logtime", "punch_time", "DateTime"),
        "direction": ("Status", "status", "InOut", "punch_state"),
    },
    DeviceVendor.MATRIX: {
        "code": ("UserId", "EmployeeCode", "user_id"),
        "time": ("EventDateTime", "event_time", "DateTime"),
        "direction": ("EventType", "Direction", "InOutMode"),
    },
    DeviceVendor.ZKTECO: {
        "code": ("pin", "PIN", "user_id", "userid"),
        "time": ("timestamp", "time", "checktime", "punch_time"),
        "direction": ("state", "status", "checktype", "punch"),
    },
    DeviceVendor.GENERIC: {
        "code": ("employee_code", "employeeCode", "code", "user_id"),
        "time": ("timestamp", "punched_at", "time", "datetime"),
        "direction": ("direction", "type", "state"),
    },
}

# Values devices use for "out". Anything else is treated as "in", because a
# missing OUT surfaces as an unpaired punch needing regularisation (slice 02)
# rather than as silently lost time.
_OUT_VALUES = {"1", "out", "OUT", "Out", "check-out", "checkout", "exit", "o"}


def _pick(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    # Case-insensitive second pass — device firmware is inconsistent about it.
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def normalise(
    payload: dict[str, Any], *, vendor: DeviceVendor, timezone_name: str
) -> NormalisedPunch:
    """Map one vendor payload onto our punch shape.

    Raises:
        DeviceError: when the payload has no employee code or no timestamp.
            Those are the only two fields without which a punch means nothing.
    """
    fields = _VENDOR_FIELDS.get(vendor, _VENDOR_FIELDS[DeviceVendor.GENERIC])

    code = _pick(payload, fields["code"])
    if code in (None, ""):
        raise DeviceError("The payload carries no employee code")

    raw_time = _pick(payload, fields["time"])
    if raw_time in (None, ""):
        raise DeviceError("The payload carries no timestamp")

    punched_at = _parse_timestamp(raw_time, timezone_name)
    direction_value = str(_pick(payload, fields["direction"]) or "").strip()
    direction = (
        PunchDirection.OUT if direction_value in _OUT_VALUES else PunchDirection.IN
    )

    return NormalisedPunch(
        employee_code=str(code).strip(),
        punched_at=punched_at,
        direction=direction,
        raw={
            **payload,
            # The audit trail for assumption 2. When a punch looks an hour
            # wrong, this is the evidence of what was assumed and why.
            "_askmira": {
                "vendor": vendor.value,
                "assumed_timezone": timezone_name,
                "timezone_was_assumed": not _has_offset(raw_time),
                "normalised_utc": punched_at.isoformat(),
            },
        },
    )


def _has_offset(value: Any) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is not None
    if isinstance(value, int | float):
        return True
    text = str(value)
    return text.endswith("Z") or "+" in text[10:] or text[10:].count("-") > 0


# ── Ingestion ───────────────────────────────────────────────────────────────


async def ingest_punches(
    session: AsyncSession,
    device: DeviceIntegration,
    payloads: list[dict[str, Any]],
) -> IngestResult:
    """Normalise, deduplicate and store a batch.

    Never raises on a bad row. Every failure mode — unreadable payload, unknown
    employee code — becomes a `DevicePunchException` so the rest of the batch
    lands. One unmapped person must not take out a site's attendance.
    """
    result = IngestResult(unmapped_codes=[])

    codes: set[str] = set()
    normalised: list[NormalisedPunch] = []

    for payload in payloads:
        try:
            punch = normalise(
                payload, vendor=device.vendor, timezone_name=device.assumed_timezone
            )
        except DeviceError as exc:
            session.add(
                DevicePunchException(
                    tenant_id=device.tenant_id,
                    device_id=device.device_id,
                    employee_code=None,
                    punched_at=datetime.now(UTC),
                    reason=str(exc),
                    raw_payload=payload,
                )
            )
            result.exceptions += 1
            continue

        normalised.append(punch)
        codes.add(punch.employee_code)

    employees = {}
    if codes:
        rows = (
            await session.scalars(
                select(Employee).where(
                    Employee.tenant_id == device.tenant_id,
                    Employee.employee_code.in_(codes),
                    Employee.deleted_at.is_(None),
                )
            )
        ).all()
        employees = {row.employee_code: row.id for row in rows}

    # Existing punches in the batch's time range, for deduplication. One query
    # rather than one per punch — a device sync can carry thousands of rows.
    existing_keys: set[tuple[uuid.UUID, datetime]] = set()
    if normalised:
        earliest = min(p.punched_at for p in normalised)
        latest = max(p.punched_at for p in normalised)
        rows = (
            await session.scalars(
                select(AttendancePunch).where(
                    AttendancePunch.tenant_id == device.tenant_id,
                    AttendancePunch.device_id == device.device_id,
                    AttendancePunch.punched_at >= earliest,
                    AttendancePunch.punched_at <= latest,
                )
            )
        ).all()
        existing_keys = {(row.employee_id, row.punched_at) for row in rows}

    seen_in_batch: set[tuple[str, datetime]] = set()

    for punch in normalised:
        employee_id = employees.get(punch.employee_code)

        if employee_id is None:
            session.add(
                DevicePunchException(
                    tenant_id=device.tenant_id,
                    device_id=device.device_id,
                    employee_code=punch.employee_code,
                    punched_at=punch.punched_at,
                    reason=(
                        f"No employee with code {punch.employee_code}. The punch is kept "
                        "here for HR to map; the rest of the batch was ingested."
                    ),
                    raw_payload=punch.raw,
                )
            )
            result.exceptions += 1
            if punch.employee_code not in result.unmapped_codes:
                result.unmapped_codes.append(punch.employee_code)
            continue

        batch_key = (punch.employee_code, punch.punched_at)
        if batch_key in seen_in_batch or (employee_id, punch.punched_at) in existing_keys:
            result.duplicates += 1
            continue
        seen_in_batch.add(batch_key)

        session.add(
            AttendancePunch(
                tenant_id=device.tenant_id,
                employee_id=employee_id,
                punched_at=punch.punched_at,
                direction=punch.direction,
                source=PunchSource.BIOMETRIC,
                device_id=device.device_id,
                raw_payload=punch.raw,
            )
        )
        result.accepted += 1

    device.last_seen_at = datetime.now(UTC)
    if device.status is DeviceStatus.SILENT:
        device.status = DeviceStatus.ACTIVE

    await session.commit()
    return result


async def silent_devices(session: AsyncSession, tenant_id: uuid.UUID) -> list[DeviceIntegration]:
    """Devices past their silence threshold.

    Silent device failure is how a month of attendance goes missing: nobody
    notices until payroll, by which point the data is unrecoverable.
    """
    devices = (
        await session.scalars(
            select(DeviceIntegration).where(
                DeviceIntegration.tenant_id == tenant_id,
                DeviceIntegration.status != DeviceStatus.DISABLED,
            )
        )
    ).all()

    now = datetime.now(UTC)
    silent = []
    for device in devices:
        threshold = now - timedelta(minutes=device.silence_threshold_minutes)
        if device.last_seen_at is None or device.last_seen_at < threshold:
            if device.status is not DeviceStatus.SILENT:
                device.status = DeviceStatus.SILENT
            silent.append(device)

    if silent:
        await session.commit()
    return silent
