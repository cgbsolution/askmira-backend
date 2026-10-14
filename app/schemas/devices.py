"""Biometric device contracts (slice 04, part C)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.identity import DeviceStatus, DeviceVendor


def _valid_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"'{value}' is not a known timezone, e.g. Asia/Kolkata.") from exc
    return value


class DeviceCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    vendor: DeviceVendor = DeviceVendor.GENERIC
    work_location_id: uuid.UUID | None = None
    assumed_timezone: str = Field(
        default="Asia/Kolkata",
        max_length=64,
        description=(
            "Devices usually send local wall-clock time with no offset. This is the "
            "timezone that assumption uses, and it is recorded on every punch."
        ),
    )
    silence_threshold_minutes: int = Field(default=180, ge=15, le=10_080)

    _zone = field_validator("assumed_timezone")(_valid_zone)


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    work_location_id: uuid.UUID | None = None
    clear_work_location: bool = False
    assumed_timezone: str | None = Field(default=None, max_length=64)
    silence_threshold_minutes: int | None = Field(default=None, ge=15, le=10_080)
    #: `silent` is set by the silence check, never by a person.
    enabled: bool | None = None
    #: Let the device push by serial number alone (ADMS/iclock firmware that
    #: takes only a host and port).
    accepts_serial_push: bool | None = None

    @field_validator("assumed_timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        return _valid_zone(value) if value is not None else None


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: str
    name: str
    vendor: DeviceVendor
    status: DeviceStatus
    work_location_id: uuid.UUID | None = None
    assumed_timezone: str
    last_seen_at: datetime | None
    silence_threshold_minutes: int
    push_token_hint: str | None
    accepts_serial_push: bool = False


class DeviceWithToken(DeviceRead):
    """The plaintext push token, shown exactly once. Only its hash is stored."""

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
    resolved_employee_id: uuid.UUID | None = None
    created_at: datetime | None = None


class ExceptionResolve(BaseModel):
    employee_id: uuid.UUID


class DeviceIngestionRow(BaseModel):
    device: DeviceRead
    is_silent: bool
    minutes_since_seen: int | None
    punches_last_24h: int
    punches_last_7d: int
    open_exceptions: int


class IngestionStatus(BaseModel):
    generated_at: datetime
    device_count: int
    silent_count: int
    open_exceptions: int
    punches_last_24h: int
    devices: list[DeviceIngestionRow]
    #: Stated on the response so the screen cannot imply otherwise.
    pull_ingestion_built: bool = False
    silence_alerts_sent: bool = False
    notes: list[str]
