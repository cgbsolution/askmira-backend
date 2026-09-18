"""GSTR-2B CSV in, GST register CSV out. Pure: no session, no web layer.

A GSTR-2B download is supplied by a person — nothing here talks to the GST
portal. Reading it follows the same rules as every upload: the bytes decide
(size, text, UTF-8, header), a bad row is reported with its number rather than
failing the file, and nothing is inferred that the file does not say.

Writing the register neutralises spreadsheet formulas. A vendor name beginning
with `=` is data from outside the company, and a CA opening the export in Excel
must not execute it.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "MAX_BYTES",
    "MAX_ROWS",
    "GstCsvError",
    "GstCsvTooLargeError",
    "decode_csv",
    "neutralise",
    "parse_gstr2b_csv",
    "register_to_csv",
]

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 20_000
REQUIRED = ("vendor_gstin", "invoice_number", "invoice_date", "taxable_value")
TAX_COLUMNS = ("cgst", "sgst", "igst", "cess")
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y")
_MONEY = re.compile(r"^-?\d+(\.\d{1,2})?$")


class GstCsvError(ValueError):
    """The file as a whole cannot be read."""


class GstCsvTooLargeError(GstCsvError):
    """The file is over the size cap."""


def decode_csv(raw: bytes, *, max_bytes: int = MAX_BYTES) -> str:
    if len(raw) > max_bytes:
        raise GstCsvTooLargeError(
            f"The file is larger than {max_bytes // (1024 * 1024)} MB. Split it by month."
        )
    if not raw.strip():
        raise GstCsvError("The file is empty.")
    if b"\x00" in raw:
        raise GstCsvError("This is not a text CSV file. Export the 2B sheet as CSV (UTF-8).")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GstCsvError("The file is not UTF-8 text. Export it as CSV (UTF-8).") from exc


def _date(text: str) -> date:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"'{text}' is not a date. Use YYYY-MM-DD or DD-MM-YYYY.")


def _money(text: str, column: str) -> Decimal:
    cleaned = text.replace(",", "").strip() or "0"
    if not _MONEY.match(cleaned):
        raise ValueError(f"{column} '{text}' is not an amount.")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - the regex already refused it
        raise ValueError(f"{column} '{text}' is not an amount.") from exc
    if value < 0:
        raise ValueError(f"{column} cannot be negative.")
    return value


def parse_gstr2b_csv(
    text: str, *, max_rows: int = MAX_ROWS
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(lines, errors)`. Lines are shaped for `service.import_gstr2b`."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise GstCsvError("The file has no header row.") from exc
    columns = [h.strip().lower() for h in header]
    missing = [c for c in REQUIRED if c not in columns]
    if missing:
        raise GstCsvError(
            f"The header is missing {', '.join(missing)}. Expected: vendor_gstin,vendor_name,"
            "invoice_number,invoice_date,taxable_value,cgst,sgst,igst,cess."
        )
    index = {name: columns.index(name) for name in columns}

    lines: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for number, record in enumerate(reader, start=1):
        if number > max_rows:
            raise GstCsvError(f"The file has more than {max_rows} rows. Split it by month.")
        if not any(cell.strip() for cell in record):
            continue

        def cell(name: str, record: list[str] = record) -> str:
            position = index.get(name)
            if position is None or position >= len(record):
                return ""
            return record[position].strip()

        try:
            gstin = cell("vendor_gstin").upper().replace(" ", "")
            if not gstin:
                raise ValueError("The vendor GSTIN is blank.")
            if len(gstin) > 15:
                raise ValueError(f"'{gstin}' is longer than a GSTIN.")
            invoice_number = cell("invoice_number")
            if not invoice_number:
                raise ValueError("The invoice number is blank.")
            if len(invoice_number) > 120:
                raise ValueError("The invoice number is too long.")
            line: dict[str, Any] = {
                "vendor_gstin": gstin,
                "vendor_name": (cell("vendor_name") or None),
                "invoice_number": invoice_number,
                "invoice_date": _date(cell("invoice_date")),
                "taxable_value": _money(cell("taxable_value"), "taxable_value"),
            }
            for column in TAX_COLUMNS:
                line[column] = _money(cell(column), column)
            if line["vendor_name"] is not None:
                line["vendor_name"] = line["vendor_name"][:240]
        except ValueError as exc:
            errors.append({"row": number, "error": str(exc)})
            continue
        lines.append(line)
    return lines, errors


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralise(value: Any) -> str:
    """A cell a spreadsheet will show, not run."""
    text = "" if value is None else str(value)
    # A negative number is data, not a formula; everything else is quoted.
    if text.startswith(_FORMULA_PREFIXES) and not re.fullmatch(r"-\d+(\.\d+)?", text):
        return "'" + text
    return text


REGISTER_COLUMNS = (
    "section",
    "vendor_gstin",
    "invoice_number",
    "invoice_date",
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "cess",
    "hsn_sac",
    "place_of_supply",
    "eligibility",
    "eligibility_reason",
    "reconciliation_status",
)


def register_to_csv(register: dict[str, Any]) -> str:
    """The register export as CSV: eligible lines, then lines awaiting review."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(REGISTER_COLUMNS)
    for section in ("eligible", "awaiting_review"):
        for line in register.get(section, []):
            writer.writerow(
                [neutralise(section)]
                + [neutralise(line.get(column)) for column in REGISTER_COLUMNS[1:]]
            )
    return buffer.getvalue()
