"""Read models for the factoHR payroll history import (report and preview).

Money is a string of the exact amount ("12345.67"), as the payroll reads give
it: the report is checked against Excel figures to the paisa, so nothing here
is ever a binary float.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HistoryIssueRead(BaseModel):
    """One finding. Errors block the import; warnings and notes do not."""

    level: str
    category: str
    message: str
    month: str | None = None
    file: str | None = None
    sheet: str | None = None
    row: int | None = None
    code: str | None = None


class HistorySheetRead(BaseModel):
    name: str
    role: str
    header_row: int
    rows_read: int
    first_row: int | None = None
    last_row: int | None = None
    totals_row: int | None = None


class HistoryOtherSheetRead(BaseModel):
    name: str
    rows: int


class HistoryFileRead(BaseModel):
    name: str
    sha256: str
    size: int
    sheets: list[HistorySheetRead] = Field(default_factory=list)
    other_sheets: list[HistoryOtherSheetRead] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class HistoryDifferenceRead(BaseModel):
    """The register's own totals row against the sum of the rows read."""

    in_register: str
    computed: str
    difference: str


class HistoryMonthRead(BaseModel):
    year: int
    month: int
    label: str
    files: list[str]
    sheets: list[HistorySheetRead]
    rows_read: int
    payslips: int
    people: int
    combined_rows: int
    zero_rows: int
    refused: bool
    #: Computed from the payslips: gross, deductions, net, employer_cost.
    totals: dict[str, str]
    register_totals: dict[str, str]
    differences: dict[str, HistoryDifferenceRead]
    #: Payslips carrying each register-difference line (GROSS_DIFF, NET_DIFF, …).
    line_counts: dict[str, int]
    notes: list[str]
    warnings: list[str]
    errors: list[str]


class HistorySegmentRead(BaseModel):
    """One salary revision as the import leaves it."""

    effective_from: str
    effective_to: str | None = None
    months: list[str]
    #: "new" (from the registers) or "live" (the existing revision, re-dated).
    record: str
    notes: list[str]
    ctc_monthly: str
    basic: str
    hra: str
    special: str
    bonus: str
    gross: str


class HistoryLiveSalaryRead(BaseModel):
    effective_from: str
    ctc_monthly: str
    new_effective_from: str | None = None
    deleted: bool


class HistoryPersonSalaryRead(BaseModel):
    code: str
    name: str
    #: match_last | increment | later | replace | no_live | keep | none | left_as_is
    action: str
    live: HistoryLiveSalaryRead | None = None
    segments: list[HistorySegmentRead]
    notes: list[str]


class HistorySalaryRead(BaseModel):
    revisions_new: int
    revisions_redated: int
    revisions_deleted: int
    increments_date_unknown: list[str]
    replaced: list[str]
    no_live_salary: list[str]
    left_as_is: list[str]
    kept_later: list[str]
    actions: dict[str, int]
    persons: list[HistoryPersonSalaryRead]


class HistoryMappedPersonRead(BaseModel):
    register_code: str
    code: str
    name: str


class HistoryUnmatchedPersonRead(BaseModel):
    code: str
    name: str | None = None
    files: list[str]


class HistoryPeopleRead(BaseModel):
    matched: int
    mapped_by_name: list[HistoryMappedPersonRead]
    unmatched: list[HistoryUnmatchedPersonRead]


class HistoryInputLineRead(BaseModel):
    code: str
    amount: str
    taxable_amount: str


class HistoryCurrentYearPayslipRead(BaseModel):
    """A payslip of the last register month's financial year with register lines:
    AskMira's own TDS reads it as this year's pay."""

    month: str
    code: str
    lines: list[HistoryInputLineRead]


class HistoryImportRead(BaseModel):
    system: str
    applied: bool
    dry_run: bool
    legal_entity_id: str | None = None
    legal_entity_name: str
    files: list[HistoryFileRead]
    months: list[HistoryMonthRead]
    salary: HistorySalaryRead
    people: HistoryPeopleRead
    payslips: int
    line_counts: dict[str, int]
    current_year_input_lines: list[HistoryCurrentYearPayslipRead]
    blocking_errors: int
    #: level → category → count
    issue_counts: dict[str, dict[str, int]]
    issues: list[HistoryIssueRead]
    run_ids: list[str]
    stats: dict[str, Any]
