"""Employee import contracts: the check before anything is saved, and the result.

The check and the import read the same file the same way, so what the check
says will happen is what the import does. Nothing about the file is kept on
the server between the two.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.models.employee import EmploymentStatus

RowOutcome = Literal["ready", "skip", "error"]


class ImportColumn(BaseModel):
    #: As in the file; a repeated name carries its group ("Local Address > City").
    header: str
    #: The AskMira field it fills.
    field: str
    section: str


class ImportNewLocation(BaseModel):
    name: str
    state_code: str


class ImportRow(BaseModel):
    #: The row number in the file, as Excel shows it.
    line: int
    employee_code: str | None
    name: str | None
    status: EmploymentStatus | None
    date_of_joining: date | None
    date_of_exit: date | None
    designation: str | None
    department: str | None
    location: str | None
    manager: str | None
    outcome: RowOutcome
    #: Why the row is not imported. Empty unless `outcome` is "error".
    errors: list[str]
    #: Values left out of an imported row, each with its reason.
    warnings: list[str]
    #: Set once imported.
    employee_id: uuid.UUID | None = None


class ImportPreview(BaseModel):
    filename: str
    #: "factoHR Employee Detail report", "AskMira template" or "Spreadsheet".
    source: str
    header_row: int
    total_rows: int
    #: Rows that will be added: people working here and people who have left.
    ready_current: int
    ready_leavers: int
    #: Employee codes already in AskMira: left as they are.
    skipped: int
    errors: int
    with_warnings: int
    #: Created by the import (an admin's import only; otherwise left blank and listed).
    new_departments: list[str]
    new_locations: list[ImportNewLocation]
    columns_used: list[ImportColumn]
    columns_ignored: list[str]
    #: Whether this person may create departments and locations.
    can_create_org: bool
    rows: list[ImportRow]
    #: People already here whose full Aadhaar number the file supplies (only
    #: the last four were kept before) — filled in by the import, nothing else
    #: about them changes.
    aadhaar_fills: int = 0
    #: Already here, but the file's Aadhaar ends differently from the one on
    #: record: left alone. "CODE — Name".
    aadhaar_mismatches: list[str] = []


class ImportResult(BaseModel):
    filename: str
    created: int
    #: Of `created`, people recorded as having left.
    created_leavers: int
    skipped: int
    failed: int
    departments_created: list[str]
    locations_created: list[str]
    rows: list[ImportRow]
    #: People already here whose full Aadhaar number was filled in.
    aadhaar_filled: int = 0
    aadhaar_mismatches: list[str] = []
