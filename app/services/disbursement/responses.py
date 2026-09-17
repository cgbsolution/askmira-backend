"""Reading a bank's response file.

Every bank returns credit outcomes in its own layout; what they share is the
beneficiary account number, a status and usually a reason and a reference. This
reads the common CSV shape — a header row naming `account_number` and `status`
(and optionally `reason`, `reference`) — into the rows `service.reconcile`
applies. Pure: bytes in, rows and problems out.

It has not been run against a real bank's response file; a bank whose file
differs needs its own reader here, not a change to reconciliation.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

__all__ = ["MAX_RESPONSE_BYTES", "ParsedResponse", "ResponseFileError", "parse_response_csv"]

#: A response file for a few thousand employees is tens of kilobytes.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_REQUIRED = ("account_number", "status")
_ALIASES = {
    "account": "account_number",
    "account_no": "account_number",
    "beneficiary_account": "account_number",
    "remarks": "reason",
    "failure_reason": "reason",
    "utr": "reference",
    "bank_reference": "reference",
}


class ResponseFileError(ValueError):
    """The file cannot be read at all."""


@dataclass(slots=True)
class ParsedResponse:
    rows: list[dict[str, str | None]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _header(name: str) -> str:
    key = name.strip().lower().replace(" ", "_")
    return _ALIASES.get(key, key)


def parse_response_csv(data: bytes) -> ParsedResponse:
    if not data:
        raise ResponseFileError("The response file is empty.")
    if len(data) > MAX_RESPONSE_BYTES:
        raise ResponseFileError("The response file is larger than a bank response should be.")
    if b"\x00" in data:
        raise ResponseFileError("This is not a text file. Upload the bank's CSV response.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResponseFileError("The response file is not UTF-8 text.") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ResponseFileError("The response file has no header row.") from exc
    columns = [_header(name) for name in header]
    missing = [name for name in _REQUIRED if name not in columns]
    if missing:
        raise ResponseFileError(
            "The header row must name account_number and status; it is missing "
            + ", ".join(missing)
            + "."
        )

    parsed = ParsedResponse()
    for line_number, values in enumerate(reader, start=2):
        if not any(value.strip() for value in values):
            continue
        row = dict(zip(columns, (value.strip() for value in values), strict=False))
        account = (row.get("account_number") or "").replace(" ", "")
        outcome = row.get("status") or ""
        if not account.isdigit():
            parsed.problems.append(f"Line {line_number}: the account number is not digits.")
            continue
        if not outcome:
            parsed.problems.append(f"Line {line_number}: no status.")
            continue
        parsed.rows.append(
            {
                "account_number": account,
                "status": outcome,
                "reason": row.get("reason") or None,
                "reference": row.get("reference") or None,
            }
        )
    if not parsed.rows and not parsed.problems:
        raise ResponseFileError("The response file has a header but no rows.")
    return parsed
