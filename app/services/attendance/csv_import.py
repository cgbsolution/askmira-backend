"""Reading a punch CSV into rows. Pure: no session, no web layer.

A device export or a spreadsheet from a site office. Three rules:

1. **The bytes decide, not the filename.** A file is accepted if it is small
   enough, is text (no NUL bytes), decodes as UTF-8 and has the required header.
   The client's filename and content type are not consulted.
2. **A bad row is reported, never fatal.** One unreadable timestamp must not
   stop a site's attendance loading — the row comes back with its number and
   the reason, and the rest is imported.
3. **A timestamp with no offset is read in the stated timezone**, and the
   assumption is recorded on the punch, exactly as device ingestion does.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.attendance import PunchDirection

__all__ = [
    "MAX_BYTES",
    "MAX_ROWS",
    "CsvImportError",
    "CsvTooLargeError",
    "ParsedPunchRow",
    "RowError",
    "decode_csv",
    "parse_punch_csv",
    "parse_timestamp",
]

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
REQUIRED_COLUMNS = ("employee_code", "punched_at", "direction")

_NAIVE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)
_DIRECTIONS = {
    "in": PunchDirection.IN,
    "i": PunchDirection.IN,
    "check-in": PunchDirection.IN,
    "out": PunchDirection.OUT,
    "o": PunchDirection.OUT,
    "check-out": PunchDirection.OUT,
}
#: How far ahead of the server clock a timestamp may be before it is refused as
#: a typo. A device a few minutes fast is normal; a punch next week is not.
_FUTURE_TOLERANCE = timedelta(hours=12)


class CsvImportError(ValueError):
    """The file as a whole cannot be read."""


class CsvTooLargeError(CsvImportError):
    """The file is over the size cap."""


@dataclass(frozen=True, slots=True)
class ParsedPunchRow:
    row: int
    employee_code: str
    punched_at: datetime
    direction: PunchDirection
    device_id: str | None
    timezone_assumed: bool


@dataclass(frozen=True, slots=True)
class RowError:
    row: int
    employee_code: str | None
    error: str

    def as_dict(self) -> dict[str, object]:
        return {"row": self.row, "employee_code": self.employee_code, "error": self.error}


def decode_csv(raw: bytes, *, max_bytes: int = MAX_BYTES) -> str:
    """Bytes to text, refusing anything that is not a small UTF-8 text file."""
    if len(raw) > max_bytes:
        raise CsvTooLargeError(
            f"The file is larger than {max_bytes // (1024 * 1024)} MB. Split it by month "
            "or by site and import the parts."
        )
    if not raw.strip():
        raise CsvImportError("The file is empty.")
    if b"\x00" in raw:
        raise CsvImportError(
            "This is not a text CSV file. Export the sheet as CSV (UTF-8) and try again."
        )
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError(
            "The file is not UTF-8 text. Export the sheet as CSV (UTF-8) and try again."
        ) from exc


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CsvImportError(
            f"'{timezone_name}' is not a known timezone, e.g. Asia/Kolkata."
        ) from exc


def parse_timestamp(value: str, zone: ZoneInfo) -> tuple[datetime, bool]:
    """Return `(aware UTC datetime, timezone_was_assumed)`.

    Raises:
        ValueError: when the value is not a timestamp this reader understands.
    """
    text = value.strip()
    if not text:
        raise ValueError("The timestamp is blank.")
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _NAIVE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ValueError(
            f"'{text}' is not a timestamp. Use ISO 8601, e.g. 2026-04-06T09:02:00+05:30."
        )
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC), False
    return parsed.replace(tzinfo=zone).astimezone(UTC), True


def parse_punch_csv(
    text: str,
    *,
    timezone_name: str,
    now: datetime,
    max_rows: int = MAX_ROWS,
) -> tuple[list[ParsedPunchRow], list[RowError]]:
    """Read every row, returning the good ones and a reason for each bad one.

    Row numbers are 1-based data rows, so row 1 is the first line after the
    header — the number a person sees when they open the file.
    """
    zone = _zone(timezone_name)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CsvImportError("The file has no header row.") from exc

    columns = [h.strip().lower() for h in header]
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise CsvImportError(
            f"The header is missing {', '.join(missing)}. Expected: "
            "employee_code,punched_at,direction[,device_id]."
        )
    index = {name: columns.index(name) for name in columns}

    rows: list[ParsedPunchRow] = []
    errors: list[RowError] = []

    for number, record in enumerate(reader, start=1):
        if number > max_rows:
            raise CsvImportError(
                f"The file has more than {max_rows} rows. Split it and import the parts."
            )
        if not any(cell.strip() for cell in record):
            continue

        def cell(name: str, record: list[str] = record) -> str:
            position = index.get(name)
            if position is None or position >= len(record):
                return ""
            return record[position].strip()

        code = cell("employee_code")
        if not code:
            errors.append(RowError(number, None, "The employee code is blank."))
            continue
        if len(code) > 32:
            errors.append(RowError(number, code[:32], "The employee code is too long."))
            continue

        direction = _DIRECTIONS.get(cell("direction").lower())
        if direction is None:
            errors.append(
                RowError(number, code, f"Direction '{cell('direction')}' is not in or out.")
            )
            continue

        try:
            punched_at, assumed = parse_timestamp(cell("punched_at"), zone)
        except ValueError as exc:
            errors.append(RowError(number, code, str(exc)))
            continue
        if punched_at > now + _FUTURE_TOLERANCE:
            errors.append(
                RowError(number, code, f"{cell('punched_at')} is in the future.")
            )
            continue

        device_id = cell("device_id") or None
        if device_id is not None and len(device_id) > 128:
            errors.append(RowError(number, code, "The device id is too long."))
            continue

        rows.append(
            ParsedPunchRow(
                row=number,
                employee_code=code,
                punched_at=punched_at,
                direction=direction,
                device_id=device_id,
                timezone_assumed=assumed,
            )
        )

    return rows, errors
