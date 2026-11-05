"""Writing a table as an Excel or CSV file people can trust.

- Codes, phone numbers and IDs are text, so Excel keeps every digit and the
  leading zeros.
- Dates are dates and times are times (day first, 24-hour), so they sort and
  sum; durations are hours and minutes that add up across rows.
- Nothing a person typed can run as a formula: in Excel a value is always a
  value, and in CSV a cell starting = + - @ gets a leading apostrophe.
- A link column is a clickable link in Excel and the plain address in CSV.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

__all__ = ["Column", "Link", "csv_bytes", "xlsx_bytes"]

Kind = Literal["text", "number", "date", "time", "minutes", "link"]

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_MAROON = "7D0712"


@dataclass(frozen=True)
class Column:
    label: str
    kind: Kind = "text"
    width: int | None = None


@dataclass(frozen=True)
class Link:
    """A clickable link: `text` shown in Excel, `url` in CSV."""

    url: str
    text: str = "Open map"


def _clock(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"


def _csv_text(value: Any, kind: Kind) -> str:
    if value is None:
        return ""
    if isinstance(value, Link):
        return value.url
    if kind == "minutes" and isinstance(value, int):
        return _clock(value)
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def csv_bytes(columns: Sequence[Column], rows: Iterable[Sequence[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([_csv_text(c.label, "text") for c in columns])
    for row in rows:
        writer.writerow([_csv_text(v, c.kind) for c, v in zip(columns, row, strict=True)])
    # The byte-order mark makes Excel read the file as UTF-8.
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def xlsx_bytes(
    columns: Sequence[Column],
    rows: Iterable[Sequence[Any]],
    *,
    sheet_title: str = "Sheet1",
    row_count: int | None = None,
    freeze: str = "C2",
) -> bytes:
    """An Excel workbook with one sheet: a maroon header row, then the rows."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook(write_only=True)
    sheet = book.create_sheet(sheet_title[:31])
    for index, column in enumerate(columns, start=1):
        width = column.width or min(max(len(column.label) + 2, 12), 48)
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = freeze
    if columns and row_count is not None:
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{row_count + 1}"

    fill = PatternFill("solid", fgColor=_MAROON)
    font = Font(bold=True, color="FFFFFF")
    wrap = Alignment(vertical="center", wrap_text=True)
    header = []
    for column in columns:
        cell = WriteOnlyCell(sheet, value=column.label)
        cell.fill, cell.font, cell.alignment = fill, font, wrap
        header.append(cell)
    sheet.append(header)

    for row in rows:
        out: list[Any] = []
        for column, value in zip(columns, row, strict=True):
            if value is None:
                out.append(None)
                continue
            if isinstance(value, Link):
                cell = WriteOnlyCell(sheet, value=value.text)
                cell.hyperlink = value.url
                cell.style = "Hyperlink"
            elif column.kind == "minutes" and isinstance(value, int):
                cell = WriteOnlyCell(sheet, value=timedelta(minutes=value))
                cell.number_format = "[h]:mm"
            elif isinstance(value, datetime):
                cell = WriteOnlyCell(sheet, value=value.replace(tzinfo=None))
                cell.number_format = "DD-MM-YYYY HH:MM"
            elif isinstance(value, date):
                cell = WriteOnlyCell(sheet, value=value)
                cell.number_format = "DD-MM-YYYY"
            elif isinstance(value, time):
                cell = WriteOnlyCell(sheet, value=value)
                cell.number_format = "HH:MM"
            elif column.kind == "text" or isinstance(value, str):
                cell = WriteOnlyCell(sheet, value=str(value))
                # Never a formula: a value starting "=" is shown, not run.
                cell.data_type = "s"
                if column.kind == "text":
                    cell.number_format = "@"
            else:
                out.append(value)
                continue
            out.append(cell)
        sheet.append(out)

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
