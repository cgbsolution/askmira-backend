"""Reading an uploaded spreadsheet into header-matched rows.

An HR system's export is rarely a clean table. factoHR's Employee Detail
report opens with a title, puts group headings over its columns on two rows
(PF / ESIC / EPS Detail > PF Detail > PF No), and repeats headers such as
Address, State and Joining Date under several groups. So the header row is
found by looking for the employee-code column, and every column carries the
nearest group heading to its left as well as its own name.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.services.employee_import.fields import FIELDS, GROUP_SEP, norm, qualified

#: Big enough for a few thousand people with every column; an export larger
#: than this is not an employee master.
MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 5000
#: How far down the header row may be.
HEADER_SCAN = 25

_CODE_ALIASES = frozenset(FIELDS[0].aliases)


class SheetError(ValueError):
    """The file cannot be read as an employee list, said so HR can fix it."""


@dataclass
class Column:
    index: int
    name: str
    group: str | None
    #: Another column has the same name, so the group is what tells them apart.
    repeated: bool = False

    @property
    def header(self) -> str:
        """As HR sees it: "Local Address > City" where the name repeats, else "City"."""
        return f"{self.group}{GROUP_SEP}{self.name}" if self.group and self.repeated else self.name

    @property
    def key(self) -> str:
        return qualified(self.group, self.name) if self.group else norm(self.name)


@dataclass
class Table:
    filename: str
    #: Which kind of file this looked like, for the preview.
    source: str
    header_row: int
    columns: list[Column]
    #: (sheet row number, cells) for each row under the header that has anything in it.
    rows: list[tuple[int, list[Any]]]
    #: field key -> column index, and the columns no field claimed.
    mapping: dict[str, int] = field(default_factory=dict)
    unused: list[Column] = field(default_factory=list)


def _cells_from_xlsx(data: bytes) -> list[list[Any]]:
    """The cells of the first sheet that has an employee-code column."""
    from openpyxl import load_workbook

    try:
        book = load_workbook(io.BytesIO(data), data_only=True)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        raise SheetError("The file could not be opened as an Excel workbook (.xlsx).") from exc
    for sheet in book.worksheets:
        grid = [list(row) for row in sheet.iter_rows(values_only=True)]
        if _find_header(grid) is not None:
            return grid
    raise SheetError(
        f"No sheet has an 'Emp Code' or 'Employee Code' column in its first {HEADER_SCAN} rows."
    )


def _cells_from_csv(data: bytes) -> list[list[Any]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            decoded = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes any byte string
        raise SheetError("The file's text encoding could not be read. Save it as UTF-8 CSV.")
    try:
        dialect = csv.Sniffer().sniff(decoded[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [list(row) for row in csv.reader(io.StringIO(decoded), dialect)]


def _find_header(grid: list[list[Any]]) -> int | None:
    for index, row in enumerate(grid[:HEADER_SCAN]):
        if any(norm(cell) in _CODE_ALIASES for cell in row if cell is not None):
            return index
    return None


def _groups(grid: list[list[Any]], header: int, width: int) -> list[str | None]:
    """Each column's group heading: the nearest one at or to its left on the
    two rows just above the header, the closer row winning.

    Nearest-to-the-left rather than the merged range, because exports merge
    unevenly: factoHR's "Permanent Address" heading spans Address to Country
    but not the State, City and Pincode that follow it.
    """
    labels: list[str | None] = [None] * width
    for row_index in range(header - 1, max(header - 3, -1), -1):
        row = grid[row_index]
        starts = {
            index: str(value).strip()
            for index, value in enumerate(row[:width])
            if value is not None and str(value).strip()
        }
        # A lone cell at the start of the row is the report's title, not a group.
        if not starts or list(starts) == [0]:
            continue
        current: str | None = None
        for index in range(width):
            current = starts.get(index, current)
            if labels[index] is None:
                labels[index] = current
    return labels


def _source(title_rows: list[list[Any]], names: list[str]) -> str:
    text = " ".join(str(cell) for row in title_rows for cell in row if cell is not None).lower()
    keys = {norm(name) for name in names}
    if "employee detail report" in text or {"code series", "payroll group"} <= keys:
        return "factoHR Employee Detail report"
    if {"emp code", "first name", "local pincode"} <= keys:
        return "AskMira template"
    return "Spreadsheet"


def read_table(data: bytes, filename: str) -> Table:
    if not data:
        raise SheetError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise SheetError("The file is over 10 MB. Split it, or remove columns you do not need.")
    lowered = filename.lower()
    if lowered.endswith(".xls"):
        raise SheetError("Old .xls files cannot be read. Open it in Excel and save as .xlsx.")
    if lowered.endswith(".csv"):
        grid = _cells_from_csv(data)
    elif data[:2] == b"PK":
        grid = _cells_from_xlsx(data)
    else:
        raise SheetError("Upload an Excel workbook (.xlsx) or a CSV file.")

    header = _find_header(grid)
    if header is None:
        raise SheetError(
            f"No 'Emp Code' or 'Employee Code' column in the first {HEADER_SCAN} rows."
        )
    head = grid[header]
    width = max((i + 1 for i, cell in enumerate(head) if cell not in (None, "")), default=0)
    groups = _groups(grid, header, width)
    columns = [
        Column(index=i, name=str(head[i]).strip(), group=groups[i])
        for i in range(width)
        if head[i] is not None and str(head[i]).strip()
    ]
    seen = Counter(norm(column.name) for column in columns)
    for column in columns:
        column.repeated = seen[norm(column.name)] > 1

    rows: list[tuple[int, list[Any]]] = []
    for offset, row in enumerate(grid[header + 1 :], start=header + 2):
        if any(cell not in (None, "") and str(cell).strip() for cell in row):
            rows.append((offset, list(row) + [None] * max(0, width - len(row))))
    if len(rows) > MAX_ROWS:
        raise SheetError(f"The file has {len(rows)} rows; import at most {MAX_ROWS} at a time.")

    table = Table(
        filename=filename,
        source=_source(grid[:header], [c.name for c in columns]),
        header_row=header + 1,
        columns=columns,
        rows=rows,
    )
    _match(table)
    return table


def _match(table: Table) -> None:
    """Give each field the first column its aliases name. A grouped alias must
    match the group too; a plain one prefers an ungrouped column, then any."""
    claimed: set[int] = set()
    for field_ in FIELDS:
        for alias in field_.aliases:
            column = _column_for(table.columns, alias, claimed)
            if column is not None:
                table.mapping[field_.key] = column.index
                claimed.add(column.index)
                break
    table.unused = [c for c in table.columns if c.index not in claimed]


def _column_for(columns: list[Column], alias: str, claimed: set[int]) -> Column | None:
    free = [c for c in columns if c.index not in claimed]
    if GROUP_SEP in alias:
        return next((c for c in free if c.key == alias), None)
    plain = [c for c in free if norm(c.name) == alias]
    return next((c for c in plain if c.group is None), plain[0] if plain else None)


def claim_custom(table: Table, labels: dict[str, str]) -> dict[str, int]:
    """Columns named after a custom field (its label or key): field key -> column."""
    found: dict[str, int] = {}
    for key, label in labels.items():
        for column in list(table.unused):
            if norm(column.name) in {norm(label), norm(key)}:
                found[key] = column.index
                table.unused.remove(column)
                break
    return found


#: The column the rows-left-out sheet puts first.
WHY_COLUMN = "Why not added"


def write_left_out(table: Table, left_out: list[tuple[list[Any], str]]) -> bytes:
    """The rows that were not added, each with its own cells and why, laid out
    as the file was — group headings over the header when it had them — so
    the sheet can be corrected and imported again."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    book = Workbook()
    sheet = book.active
    sheet.title = "Not added"
    if any(column.group for column in table.columns):
        sheet.append([None] + [column.group for column in table.columns])
    sheet.append([WHY_COLUMN] + [column.name for column in table.columns])
    header = sheet.max_row
    for cell in sheet[header]:
        cell.font = Font(bold=True)
    for cells, why in left_out:
        sheet.append([why] + [cells[column.index] for column in table.columns])
        for cell in sheet[sheet.max_row][1:]:
            # Their own text, never a formula to run.
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"
        sheet.cell(row=sheet.max_row, column=1).alignment = Alignment(wrap_text=True,
                                                                     vertical="top")
    sheet.column_dimensions["A"].width = 60
    sheet.freeze_panes = sheet.cell(row=header + 1, column=3)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
