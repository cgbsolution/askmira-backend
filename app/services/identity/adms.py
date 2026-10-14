"""The ADMS ("iclock") push protocol spoken by eSSL and ZKTeco devices.

Most biometric terminals sold in India push attendance this way when pointed at
a server: they handshake with `GET /iclock/cdata`, upload logs with
`POST /iclock/cdata?table=ATTLOG`, and poll `GET /iclock/getrequest` for
commands. This module reads those requests and hands the punches to the same
ingestion every other device uses — deduplication, the device's timezone, and
unknown codes parked as exceptions all apply unchanged.

**How a device is recognised.** The protocol carries only the serial number
(`SN`), which is printed on the device's label. So there are two ways in:

- `/adms/{token}/iclock/...` — the device's push token in the path, for
  firmware whose server setting accepts a path. Preferred.
- `/iclock/...` — the serial number alone, for firmware that takes only a host
  and port. Off for every device until HR switches it on for that device
  (`accepts_serial_push`), and refused if two workspaces have the same serial
  switched on.

Either way the serial number in the request must match the registered device.
Nothing is ever sent back to a device beyond the protocol's acknowledgements:
no commands, no user lists.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import DeviceIntegration, DeviceStatus
from app.services.identity import devices as device_service

__all__ = [
    "AdmsError",
    "device_by_serial",
    "device_by_token",
    "handshake",
    "parse_attlog",
    "receive",
]

#: ATTLOG status codes that mean leaving: check-out, break-out, overtime-out.
_OUT_STATUSES = {"1", "2", "5"}
#: How often the device should send, in seconds, and how long to wait after an error.
_DELAY_SECONDS = 30
_ERROR_DELAY_SECONDS = 60


class AdmsError(RuntimeError):
    """The request cannot be tied to a registered, enabled device."""


async def device_by_token(session: AsyncSession, token: str, serial: str) -> DeviceIntegration:
    try:
        device = await device_service.authenticate_device(session, token)
    except device_service.DeviceError as exc:
        raise AdmsError(str(exc)) from exc
    if serial and serial != device.device_id:
        raise AdmsError("The serial number does not match the device this token belongs to.")
    return device


async def device_by_serial(session: AsyncSession, serial: str) -> DeviceIntegration:
    """The one device, across workspaces, that accepts pushes by this serial.

    Not tenant-scoped, as with a push token: the request establishes the
    tenant. Ambiguity is refused rather than guessed.
    """
    if not serial:
        raise AdmsError("No serial number in the request.")
    rows = (
        await session.scalars(
            select(DeviceIntegration).where(
                DeviceIntegration.device_id == serial,
                DeviceIntegration.accepts_serial_push.is_(True),
                DeviceIntegration.status != DeviceStatus.DISABLED,
            )
        )
    ).all()
    if len(rows) != 1:
        raise AdmsError("This serial number is not registered for direct push.")
    return rows[0]


def handshake(device: DeviceIntegration) -> str:
    """The options a device asks for on `GET /iclock/cdata`.

    Only attendance logs are requested (`TransFlag`); operation logs, photos
    and the user table stay on the device.
    """
    lines = [
        f"GET OPTION FROM: {device.device_id}",
        "ATTLOGStamp=None",
        "OPERLOGStamp=9999",
        "ATTPHOTOStamp=None",
        f"ErrorDelay={_ERROR_DELAY_SECONDS}",
        f"Delay={_DELAY_SECONDS}",
        "TransTimes=00:00;14:05",
        "TransInterval=1",
        "TransFlag=TransData AttLog",
        "Realtime=1",
        "Encrypt=None",
    ]
    return "\n".join(lines) + "\n"


def parse_attlog(body: str) -> list[dict[str, Any]]:
    """ATTLOG lines into payloads the ZKTeco field map reads.

    Each line is `PIN<TAB>YYYY-MM-DD HH:MM:SS<TAB>STATUS<TAB>VERIFY<TAB>...`.
    A line that cannot be split is kept whole, so ingestion files it as an
    exception rather than it vanishing.
    """
    payloads: list[dict[str, Any]] = []
    for raw in body.splitlines():
        line = raw.strip("\r\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            payloads.append({"raw_line": line})
            continue
        status = parts[2].strip() if len(parts) > 2 else "0"
        payloads.append(
            {
                "pin": parts[0].strip(),
                "timestamp": parts[1].strip(),
                "state": "out" if status in _OUT_STATUSES else "in",
                "adms_status": status,
                "verify": parts[3].strip() if len(parts) > 3 else None,
                "workcode": parts[4].strip() if len(parts) > 4 else None,
            }
        )
    return payloads


async def receive(
    session: AsyncSession, device: DeviceIntegration, *, table: str, body: str
) -> str:
    """Handle `POST /iclock/cdata`: attendance logs are ingested, the rest acknowledged."""
    if table.upper() != "ATTLOG":
        device_service.touch(device)
        await session.commit()
        return "OK"
    payloads = parse_attlog(body)
    result = await device_service.ingest_punches(session, device, payloads)
    return f"OK: {result.accepted + result.duplicates + result.exceptions}"
