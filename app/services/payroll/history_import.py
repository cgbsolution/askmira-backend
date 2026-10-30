"""Payroll history from factoHR: months paid there, loaded as they were paid.

`plan` reads the parsed registers (`history_parse`) against the workspace and
builds everything in memory — one locked, published REGULAR run per month, one
payslip per person per month, and the person's salary revisions dated from the
registers — then checks it: every payslip must add up the way the PDF checks it,
every register row must belong to an AskMira employee, no month may already have
a run. `apply` writes it in one transaction with its audit rows; `undo_imported_run`
removes one imported month again.

What is stored is what factoHR paid, never recomputed: no rule set, attendance or
input module is consulted. Each figure keeps the register cell it came from (file,
sheet, row, column, raw text), and where the register's own totals do not add up
the difference is a line of its own (GROSS_DIFF, DEDUCTION_DIFF, NET_DIFF), so a
payslip's lines always reproduce its gross, deductions and net to the paisa.

Employees are read by plain columns only: their encrypted identifiers are never
loaded, and nothing from the registers' PAN, Aadhaar, UAN, ESIC or bank columns
is stored anywhere.
"""

from __future__ import annotations

import asyncio
import calendar
import io
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Any

from fastapi import HTTPException, Request, status
from openpyxl import Workbook
from openpyxl.styles import Font
from pydantic import ValidationError
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
from app.models.payroll import (
    RUN_ORIGIN_IMPORTED,
    EmployeeSalary,
    PayrollRun,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
    SalaryComponent,
    SalaryStructure,
)
from app.models.timesheets import Project
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate, SalaryComponentAmountInput
from app.services import audit
from app.services.payroll import payslip_pdf
from app.services.payroll.history_parse import (
    DEDUCTION_KEYS,
    EARNING_PARTS,
    Issue,
    RegisterFile,
    RegisterMonth,
    RegisterRow,
    parse_registers,
)
from app.services.payroll.salary_master import revision_locks, revision_snapshot
from app.services.payroll.service import (
    ResolvedSalary,
    _new_salary,
    manual_summary,
    resolve_amounts,
)

__all__ = [
    "ImportPlan",
    "apply",
    "check_files",
    "issues_xlsx",
    "plan",
    "preview",
    "undo_imported_run",
]

SYSTEM = "factoHR"
PROVIDER = "factohr_register"
STRUCTURE_NAME = "Standard CTC"

_ZERO = Decimal(0)
_PAISE = Decimal("0.01")
#: A register total that differs from its parts by more than this is honoured
#: as a line of its own; less is rounding of unrounded cells.
_CELL_TOLERANCE = Decimal("0.02")
#: What rounding each cell to the paisa may leave between the lines and the net.
_PAISA_LIMIT = Decimal("0.05")
#: A live salary "matches" a register rate when every figure is within this, a month.
_MATCH_LIMIT = Decimal("2.00")
_EN_DASH = chr(0x2013)

_EARNING_LINES = (
    ("BASIC", "BASIC SALARY", "basic"),
    ("HRA", "HOUSE RENT ALLOWANCE", "hra"),
    ("SPECIAL", "SPECIAL ALLOWANCE", "special"),
    ("BONUS", "BONUS", "bonus"),
)
_DEDUCTION_LINES = (
    ("PF_EMPLOYEE", "Provident fund (employee)", "pf_employee"),
    ("ESI_EMPLOYEE", "ESI (employee)", "esi_employee"),
    ("INSURANCE", "Medical insurance", "medical_insurance"),
    ("PT", "Professional tax", "professional_tax"),
    ("OTHER_DEDUCTION", "Other deduction", "other_deduction"),
)
_EMPLOYER_LINES = (
    (
        "PF_EMPLOYER",
        "Provident fund (employer, incl. admin & EDLI as in factoHR)",
        "pf_employer",
    ),
    ("ESI_EMPLOYER", "ESI (employer)", "esi_employer"),
    ("GRATUITY_PROVISION", "Gratuity", "gratuity"),
)
_INPUT_NAMES = {
    "NET_DIFF": "Net salary in the register differs from gross - deductions",
    "ADJUSTMENT": "Adjustment (bank transfer - net salary, as in the register)",
    "ROUNDED_OFF": "Rounded off (as in the register's ROUNDED column)",
    "PAISA_ROUNDING": "Rounding to the paisa",
}
#: Figures that make a row a payment. A row with all of them empty or 0 is a
#: "zero row": listed by the register (on hold, absent) but paid nothing.
_PAID_KEYS = (
    *(f"earned_{k}" for k in (*EARNING_PARTS, "gross")),
    *DEDUCTION_KEYS,
    "total_deduction",
    "net_salary",
    "adjustment",
    "bank_transfer",
    "bank_transfer_rounded",
    "pf_employer",
    "esi_employer",
    "gratuity",
    "ctc_earned",
)
_REGISTER_KEYS = (
    "ctc_monthly",
    "ctc_earned",
    "earned_gross",
    "total_deduction",
    "net_salary",
    "adjustment",
    "bank_transfer",
    "bank_transfer_rounded",
    "workable_days",
    "days_worked",
    "lop_days",
)
_DIFF_CODES = (
    "GROSS_DIFF",
    "DEDUCTION_DIFF",
    "NET_DIFF",
    "ADJUSTMENT",
    "ROUNDED_OFF",
    "PAISA_ROUNDING",
)
_DAYS_LIMIT = Decimal("999.99")


# ── Small helpers ───────────────────────────────────────────────────────────


def _q(value: Decimal | None) -> Decimal:
    """A cell to the paisa, half up. An empty cell is 0."""
    return (value if value is not None else _ZERO).quantize(_PAISE, rounding=ROUND_HALF_UP)


def _s(amount: Decimal) -> str:
    return str(amount.quantize(_PAISE, rounding=ROUND_HALF_UP))


def _label(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


def _month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _norm_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace(".", " ")).strip().casefold()


def _norm_account(value: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").casefold())


def _iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _sum(rows: list[RegisterRow], key: str) -> Decimal:
    return sum((row.money(key) or _ZERO for row in rows), _ZERO)


def _present(rows: list[RegisterRow], key: str) -> bool:
    return any(row.money(key) is not None for row in rows)


#: Client text in the registers that names an AskMira project differently.
_ACCOUNT_ALIASES = {"cgbinternal": "internal"}


# ── The plan ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Person:
    id: uuid.UUID
    code: str
    name: str
    first_last: str
    status: EmploymentStatus
    joined: date | None
    exited: date | None
    legal_entity_id: uuid.UUID | None
    project_id: uuid.UUID | None


@dataclass(slots=True)
class PayslipPlan:
    id: uuid.UUID
    person: Person
    year: int
    month: int
    rows: list[RegisterRow]
    kind: str
    working_days: Decimal
    paid_days: Decimal
    lop_days: Decimal
    not_employed_days: Decimal
    line_items: list[dict[str, Any]]
    input_lines: list[dict[str, Any]]
    gross_earnings: Decimal
    total_deductions: Decimal
    net_pay: Decimal
    employer_cost: Decimal
    pf_employee: Decimal
    esi_employee: Decimal
    professional_tax: Decimal
    tds: Decimal
    pf_employer: Decimal
    esi_employer: Decimal
    pf_wage_base: Decimal
    esi_wage_base: Decimal
    project_id: uuid.UUID | None
    import_details: dict[str, Any]
    zero_row: bool
    employee_salary_id: uuid.UUID | None = None
    codes: set[str] = field(default_factory=set)


@dataclass(slots=True)
class MonthPlan:
    year: int
    month: int
    files: list[RegisterFile]
    run_id: uuid.UUID
    payslips: list[PayslipPlan] = field(default_factory=list)
    rows_read: int = 0
    combined_rows: int = 0
    zero_rows: int = 0
    register_totals: dict[str, str] = field(default_factory=dict)
    #: Sums of the main sheet's raw cells, for the totals row comparison.
    sheet_sums: dict[str, Decimal] = field(default_factory=dict)
    refused: bool = False

    @property
    def key(self) -> str:
        return _month_key(self.year, self.month)

    @property
    def label(self) -> str:
        return _label(self.year, self.month)

    def totals(self) -> dict[str, Decimal]:
        return {
            "gross": sum((p.gross_earnings for p in self.payslips), _ZERO),
            "deductions": sum((p.total_deductions for p in self.payslips), _ZERO),
            "net": sum((p.net_pay for p in self.payslips), _ZERO),
            "employer_cost": sum((p.employer_cost for p in self.payslips), _ZERO),
        }


Rate = tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]
#: (revision id, from, to): one revision's span in a person's salary history.
_Span = tuple[uuid.UUID, date, date | None]


@dataclass(slots=True)
class Segment:
    """A run of months on one rate, which becomes one salary revision."""

    rate: Rate
    start: date
    months: list[str]
    refs: list[str]
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    end: date | None = None
    salary_id: uuid.UUID | None = None
    #: The CTC of every month it covers, as typed (they may differ by paise).
    ctcs: list[Decimal] = field(default_factory=list)
    #: "new" — inserted from the registers; "live" — the existing revision.
    record: str = "new"


@dataclass(slots=True)
class LiveRevision:
    """The person's existing revision, as plain data (survives a rollback)."""

    id: uuid.UUID
    effective_from: date
    effective_to: date | None
    ctc_monthly: Decimal
    parts: dict[str, Decimal]
    reason: str | None


@dataclass(slots=True)
class PersonSalary:
    person: Person
    action: str
    segments: list[Segment] = field(default_factory=list)
    live: LiveRevision | None = None
    live_new_from: date | None = None
    delete_live: bool = False
    notes: list[str] = field(default_factory=list)
    #: (revision id, from, to) in date order: what payslips link to.
    timeline: list[_Span] = field(default_factory=list)
    #: New revisions, built and resolved, not added to the session yet.
    new_revisions: list[tuple[EmployeeSalary, Segment, ResolvedSalary, Decimal | None]] = field(
        default_factory=list
    )
    live_before: dict[str, Any] | None = None
    live_after: dict[str, Any] | None = None
    #: The kept revision's reason once its date comes from the registers.
    live_new_reason: str | None = None


@dataclass(slots=True)
class ImportPlan:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    legal_entity_id: uuid.UUID | None
    legal_entity_name: str
    structure_id: uuid.UUID | None
    months: list[MonthPlan]
    files: list[RegisterFile]
    salaries: list[PersonSalary]
    issues: list[Issue]
    people: dict[str, Any]
    planned_at: datetime
    applied: bool = False
    dry_run: bool = False
    stats: dict[str, Any] = field(default_factory=dict)
    run_ids: list[uuid.UUID] = field(default_factory=list)
    #: ORM objects to write, kept off the report.
    _live_objects: dict[uuid.UUID, EmployeeSalary] = field(default_factory=dict)

    @property
    def blocking(self) -> int:
        return sum(1 for issue in self.issues if issue.level == "error")


# ── Reading the workspace ───────────────────────────────────────────────────


async def _people(session: AsyncSession, user: User) -> list[Person]:
    rows = await session.execute(
        tenant_scoped(
            select(
                Employee.id,
                Employee.employee_code,
                Employee.first_name,
                Employee.last_name,
                Employee.display_name,
                Employee.status,
                Employee.date_of_joining,
                Employee.date_of_exit,
                Employee.legal_entity_id,
                Employee.project_id,
            ),
            Employee,
            user,
        )
    )
    people = []
    for r in rows.all():
        first_last = " ".join(filter(None, [r.first_name, r.last_name]))
        people.append(
            Person(
                id=r.id,
                code=r.employee_code,
                name=r.display_name or first_last,
                first_last=first_last,
                status=r.status,
                joined=r.date_of_joining,
                exited=r.date_of_exit,
                legal_entity_id=r.legal_entity_id,
                project_id=r.project_id,
            )
        )
    return people


def _code_key(code: str) -> str:
    return re.sub(r"\s+", "", code).casefold()


def _match_people(
    months: list[RegisterMonth], people: list[Person], issues: list[Issue]
) -> tuple[dict[str, Person], dict[str, Any]]:
    """Register code → AskMira person: by code, else by name and joining date."""
    by_code = {_code_key(p.code): p for p in people}
    by_name: dict[str, list[Person]] = defaultdict(list)
    for p in people:
        for name in {_norm_name(p.name), _norm_name(p.first_last)}:
            if name:
                by_name[name].append(p)

    seen: dict[str, list[RegisterRow]] = defaultdict(list)
    for month in months:
        for row in month.rows:
            seen[row.code].append(row)

    mapping: dict[str, Person] = {}
    mapped_by_name: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for code, rows in sorted(seen.items()):
        person = by_code.get(_code_key(code))
        if person is not None:
            mapping[code] = person
            continue
        candidates: dict[uuid.UUID, Person] = {}
        for row in rows:
            joined = _iso_date(row.doj)
            for p in by_name.get(_norm_name(row.name), []):
                if joined is not None and p.joined == joined:
                    candidates[p.id] = p
        months_seen = sorted({f"{r.file}" for r in rows})
        if len(candidates) == 1:
            person = next(iter(candidates.values()))
            mapping[code] = person
            mapped_by_name.append({"register_code": code, "code": person.code, "name": person.name})
            issues.append(
                Issue(
                    "note",
                    "mapped_by_name",
                    f"Register code {code} is AskMira {person.code} ({person.name}): same name "
                    "and joining date.",
                    code=code,
                )
            )
            continue
        name = next((r.name for r in rows if r.name), None)
        unmatched.append({"code": code, "name": name, "files": months_seen})
        issues.append(
            Issue(
                "error",
                "unmatched_person",
                f"{code} ({name or 'no name'}) is not an AskMira employee"
                + (" (more than one has that name and joining date)" if candidates else "")
                + f"; {len(rows)} register row(s). Add the person or correct the code.",
                code=code,
            )
        )
    summary = {
        "matched": len(mapping) - len(mapped_by_name),
        "mapped_by_name": mapped_by_name,
        "unmatched": unmatched,
    }
    return mapping, summary


async def _projects(session: AsyncSession, user: User) -> dict[str, uuid.UUID | None]:
    rows = await session.execute(
        tenant_scoped(select(Project.id, Project.name, Project.code), Project, user)
    )
    index: dict[str, uuid.UUID | None] = {}
    for project_id, name, code in rows.all():
        for key in {_norm_account(name), _norm_account(code)}:
            if not key:
                continue
            # Two projects with the same normalised name: neither is guessed.
            index[key] = None if key in index and index[key] != project_id else project_id
    return index


# ── Payslips ────────────────────────────────────────────────────────────────


def _row_source(rows: list[RegisterRow], key: str) -> dict[str, Any] | list[dict[str, Any]]:
    if len(rows) == 1:
        row = rows[0]
        letter, header = row.columns.get(key, ("", ""))
        return {"column": letter, "header": header, "raw": row.raw.get(key)}
    return [
        {
            "file": row.file,
            "sheet": row.sheet,
            "row": row.row,
            "column": row.columns[key][0],
            "header": row.columns[key][1],
            "raw": row.raw.get(key),
        }
        for row in rows
        if key in row.columns
    ]


def _basis(label: str, rows: list[RegisterRow]) -> str:
    where = (
        f"row {rows[0].row}"
        if len(rows) == 1
        else " + ".join(f"{row.sheet} row {row.row}" for row in rows)
    )
    return f"As paid in factoHR — {label} register, {rows[0].file}, {where}"


def _register_raw(row: RegisterRow) -> dict[str, str | None]:
    return {key: row.raw.get(key) for key in _REGISTER_KEYS}


def _row_details(row: RegisterRow) -> dict[str, Any]:
    return {
        "file": row.file,
        "sheet": row.sheet,
        "row": row.row,
        "kind": row.kind,
        "hidden": row.hidden,
        "status": row.status,
        "client": row.client,
        "project": row.project,
        "domain": row.domain,
        "dol": row.dol,
        "remarks": row.remarks,
        "payment_status": row.payment_status,
        "pt_status": row.pt_status,
        "gratuity_status": row.gratuity_status,
        "register": _register_raw(row),
        "cells": dict(row.cells),
    }


def _days(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


def _build_payslip(
    person: Person,
    rows: list[RegisterRow],
    year: int,
    month: int,
    *,
    projects: dict[str, uuid.UUID | None],
    unmatched_accounts: Counter,
    issues: list[Issue],
) -> PayslipPlan:
    label = _label(year, month)
    key = _month_key(year, month)
    basis = _basis(label, rows)
    first = rows[0]
    where = {"month": key, "file": first.file, "sheet": first.sheet, "row": first.row}

    def line(code: str, name: str, kind: str, amount: Decimal, source: Any) -> dict[str, Any]:
        return {
            "code": code,
            "name": name,
            "type": kind,
            "amount": _s(amount),
            "basis": basis,
            "rule_set_id": None,
            "source": source,
        }

    lines: list[dict[str, Any]] = []
    codes: set[str] = set()

    # Earnings: the earned block, part by part.
    parts_total = _ZERO
    for code, name, part in _EARNING_LINES:
        amount = _q(_sum(rows, f"earned_{part}"))
        parts_total += amount
        if amount != _ZERO:
            lines.append(line(code, name, "earning", amount, _row_source(rows, f"earned_{part}")))
    if _present(rows, "earned_gross"):
        cell_diff = _sum(rows, "earned_gross") - sum(
            (_sum(rows, f"earned_{p}") for p in EARNING_PARTS), _ZERO
        )
        amount = _q(_sum(rows, "earned_gross")) - parts_total
        if abs(cell_diff) > _CELL_TOLERANCE and amount != _ZERO:
            source = _row_source(rows, "earned_gross")
            lines.append(
                line(
                    "GROSS_DIFF",
                    "Difference in the register's gross",
                    "earning",
                    amount,
                    {"total": source, "rule": "earned Gross Salary - its four earned parts"},
                )
            )
            codes.add("GROSS_DIFF")
    gross_earnings = sum((Decimal(item["amount"]) for item in lines), _ZERO)

    # Deductions: each cell; TDS is never a line, only the payslip's tds.
    tds = _q(_sum(rows, "tds"))
    deduction_cells = tds
    deduction_lines: list[dict[str, Any]] = []
    for code, name, k in _DEDUCTION_LINES:
        amount = _q(_sum(rows, k))
        deduction_cells += amount
        if amount != _ZERO:
            deduction_lines.append(line(code, name, "deduction", amount, _row_source(rows, k)))
    if _present(rows, "total_deduction"):
        cell_diff = _sum(rows, "total_deduction") - sum(
            (_sum(rows, k) for k in DEDUCTION_KEYS), _ZERO
        )
        amount = _q(_sum(rows, "total_deduction")) - deduction_cells
        if abs(cell_diff) > _CELL_TOLERANCE and amount != _ZERO:
            deduction_lines.append(
                line(
                    "DEDUCTION_DIFF",
                    "Difference in the register's total deduction",
                    "deduction",
                    amount,
                    {
                        "total": _row_source(rows, "total_deduction"),
                        "rule": "Total Deduction - its deduction cells (TDS included)",
                    },
                )
            )
            codes.add("DEDUCTION_DIFF")
    lines.extend(deduction_lines)
    total_deductions = sum((Decimal(item["amount"]) for item in deduction_lines), _ZERO) + tds

    employer_total = _ZERO
    for code, name, k in _EMPLOYER_LINES:
        amount = _q(_sum(rows, k))
        if amount != _ZERO:
            employer_total += amount
            lines.append(
                line(code, name, "employer_contribution", amount, _row_source(rows, k))
            )

    # What reached the bank, and the steps from net salary to it.
    net_pay = _ZERO
    adjustment = _ZERO
    rounded_off = _ZERO
    net_cells = _ZERO
    raw_net_gap = _ZERO
    for row in rows:
        net = _q(row.net_salary)
        net_cells += net
        raw_net_gap += (row.net_salary or _ZERO) - (
            (row.earned.get("gross") or _ZERO) - (row.total_deduction or _ZERO)
        )
        bank = row.bank_transfer
        rounded = row.bank_transfer_rounded if row.has_rounded else None
        before_round = _q(bank) if bank is not None else net
        if bank is not None:
            adjustment += _q(bank) - net
        if rounded is not None:
            rounded_off += _q(rounded) - before_round
            net_pay += _q(rounded)
        else:
            net_pay += before_round

    inputs: list[tuple[str, Decimal]] = []
    if abs(raw_net_gap) > _CELL_TOLERANCE:
        net_diff = net_cells - (gross_earnings - total_deductions)
        if net_diff != _ZERO:
            inputs.append(("NET_DIFF", net_diff))
    if adjustment != _ZERO:
        inputs.append(("ADJUSTMENT", adjustment))
    if rounded_off != _ZERO:
        inputs.append(("ROUNDED_OFF", rounded_off))
    residual = net_pay - (gross_earnings + sum((a for _, a in inputs), _ZERO) - total_deductions)
    if residual != _ZERO:
        inputs.append(("PAISA_ROUNDING", residual))
        if abs(residual) > _PAISA_LIMIT:
            issues.append(
                Issue(
                    "error",
                    "row_does_not_add_up",
                    f"{person.code}, {label}: gross {gross_earnings} - deductions "
                    f"{total_deductions} with the register's net, adjustment and rounding comes to "
                    f"{net_pay - residual}, but {net_pay} was paid ({residual} apart). "
                    "Check the row in the register.",
                    code=person.code,
                    **where,
                )
            )

    source_ref = {
        "type": PROVIDER,
        "file": first.file,
        "sheet": first.sheet,
        "rows": [row.row for row in rows],
    }
    input_lines = []
    for code, amount in inputs:
        codes.add(code)
        taxable = amount if code == "ADJUSTMENT" and amount > _ZERO else _ZERO
        input_lines.append(
            {
                "provider": PROVIDER,
                "code": code,
                "name": _INPUT_NAMES[code],
                "kind": "earning",
                "amount": _s(amount),
                "requested": _s(amount),
                "taxable_amount": _s(taxable),
                "basis": basis,
                "source": source_ref,
                "skipped": False,
                "recovery": None,
                "counts_as_wages": False,
            }
        )

    # Days, as given; never derived.
    working = max(
        (
            row.workable_days
            if row.workable_days is not None
            else (row.days_worked or _ZERO) if row.kind == "fnf" else _ZERO
        )
        for row in rows
    )
    paid = _sum_days(rows, "days_worked")
    lop = _sum_days(rows, "lop_days")
    working, paid, lop = _days(working), _days(paid), _days(lop)
    not_employed = max(_ZERO, working - paid - lop)
    for name, value in (("working", working), ("paid", paid), ("loss of pay", lop)):
        if abs(value) > _DAYS_LIMIT:
            issues.append(
                Issue(
                    "error",
                    "days_out_of_range",
                    f"{person.code}, {label}: {name} days {value} cannot be stored.",
                    code=person.code,
                    **where,
                )
            )

    pf_base = sum(
        (
            (row.pf_wage if "pf_wage" in row.columns else row.earned.get("basic")) or _ZERO
            for row in rows
        ),
        _ZERO,
    )
    esi_base = _ZERO
    for row in rows:
        if "esi_wage" in row.columns:
            esi_base += row.esi_wage or _ZERO
        elif (row.esi_employee or _ZERO) > _ZERO:
            esi_base += row.earned.get("gross") or _ZERO

    account_row = next((row for row in rows if row.account), None)
    account = account_row.account if account_row else None
    project_id: uuid.UUID | None = None
    if account:
        normalised = _norm_account(account)
        normalised = _ACCOUNT_ALIASES.get(normalised, normalised)
        project_id = projects.get(normalised)
        if project_id is None:
            unmatched_accounts[account] += 1

    zero_row = all(not row.money(k) for row in rows for k in _PAID_KEYS)
    details: dict[str, Any] = {
        "system": SYSTEM,
        "file": first.file,
        "sheet": first.sheet,
        "rows": [row.row for row in rows],
        "kind": "combined" if len(rows) > 1 else first.kind,
        "status": next((r.status for r in rows if r.status), None),
        "client": next((r.client for r in rows if r.client), None),
        "project": next((r.project for r in rows if r.project), None),
        "domain": next((r.domain for r in rows if r.domain), None),
        "dol": next((r.dol for r in rows if r.dol), None),
        "remarks": next((r.remarks for r in rows if r.remarks), None),
        "payment_status": next((r.payment_status for r in rows if r.payment_status), None),
        "pt_status": next((r.pt_status for r in rows if r.pt_status), None),
        "gratuity_status": next((r.gratuity_status for r in rows if r.gratuity_status), None),
        "hidden": any(row.hidden for row in rows),
        "zero_row": zero_row,
    }
    register_codes = sorted({row.code for row in rows})
    if any(_code_key(c) != _code_key(person.code) for c in register_codes):
        details["register_code"] = register_codes[0] if len(register_codes) == 1 else register_codes
    if len(rows) == 1:
        details["register"] = _register_raw(first)
        details["cells"] = dict(first.cells)
    else:
        details["sheets"] = [row.sheet for row in rows]
        details["register"] = {
            k: (str(_sum(rows, k)) if any(k in row.raw for row in rows) else None)
            for k in _REGISTER_KEYS
        }
        details["cells"] = {}
        details["row_details"] = [_row_details(row) for row in rows]

    return PayslipPlan(
        id=uuid.uuid4(),
        person=person,
        year=year,
        month=month,
        rows=rows,
        kind=details["kind"],
        working_days=working,
        paid_days=paid,
        lop_days=lop,
        not_employed_days=not_employed,
        line_items=lines,
        input_lines=input_lines,
        gross_earnings=gross_earnings,
        total_deductions=total_deductions,
        net_pay=net_pay,
        employer_cost=gross_earnings + employer_total,
        pf_employee=_q(_sum(rows, "pf_employee")),
        esi_employee=_q(_sum(rows, "esi_employee")),
        professional_tax=_q(_sum(rows, "professional_tax")),
        tds=tds,
        pf_employer=_q(_sum(rows, "pf_employer")),
        esi_employer=_q(_sum(rows, "esi_employer")),
        pf_wage_base=_q(pf_base),
        esi_wage_base=_q(esi_base),
        project_id=project_id,
        import_details=details,
        zero_row=zero_row,
        codes=codes,
    )


def _sum_days(rows: list[RegisterRow], key: str) -> Decimal:
    return sum((getattr(row, key) or _ZERO for row in rows), _ZERO)


def _other_earnings(input_lines: list[dict[str, Any]]) -> Decimal:
    return sum(
        (
            Decimal(line["amount"])
            for line in input_lines
            if line.get("kind") in ("earning", "reimbursement") and not line.get("skipped")
        ),
        _ZERO,
    ).quantize(_PAISE)


def _pdf_check(slip: PayslipPlan, company: str, issues: list[Issue]) -> None:
    """The arithmetic the payslip PDF refuses to render without."""
    try:
        payslip_pdf.build_payslip_document(
            company_name=company,
            company_address=None,
            period_year=slip.year,
            period_month=slip.month,
            employee_name=slip.person.name,
            employee_code=slip.person.code,
            designation=None,
            date_of_joining=None,
            pan=None,
            uan=None,
            payment_date=None,
            working_days=slip.working_days,
            paid_days=slip.paid_days,
            lop_days=slip.lop_days,
            not_employed_days=slip.not_employed_days,
            line_items=slip.line_items,
            input_lines=slip.input_lines,
            tds=slip.tds,
            gross_earnings=slip.gross_earnings,
            other_earnings=_other_earnings(slip.input_lines),
            total_deductions=slip.total_deductions,
            net_pay=slip.net_pay,
        )
    except payslip_pdf.PayslipMismatchError as exc:
        issues.append(
            Issue(
                "error",
                "pdf_check",
                f"{slip.person.code}, {_label(slip.year, slip.month)}: {exc}",
                month=_month_key(slip.year, slip.month),
                file=slip.rows[0].file,
                sheet=slip.rows[0].sheet,
                row=slip.rows[0].row,
                code=slip.person.code,
            )
        )


# ── Salary revisions from the registers ─────────────────────────────────────


def _rate(row: RegisterRow) -> Rate | None:
    """The full-month rate a row was paid on: (CTC, basic, HRA, special, bonus, gross)."""
    if row.actual is not None:
        parts = row.actual
    elif "actual_gross" not in row.columns:
        # A register with one earnings block (Sep-2023) shows what was earned;
        # it is the rate only for a full month without loss of pay.
        if (
            row.lop_days == _ZERO
            and row.days_worked is not None
            and row.workable_days is not None
            and row.days_worked == row.workable_days
        ):
            parts = row.earned
        else:
            return None
    else:
        return None
    rate: Rate = (
        _q(row.ctc_monthly),
        _q(parts.get("basic")),
        _q(parts.get("hra")),
        _q(parts.get("special")),
        _q(parts.get("bonus")),
        _q(parts.get("gross")),
    )
    # No salary parts (a row kept for a person paid nothing, with only the CTC
    # beside it) is no rate: a revision of a CTC alone would be invented.
    if all(value == _ZERO for value in rate[1:]):
        return None
    return rate


def _live(salary: EmployeeSalary) -> LiveRevision:
    annual = {c.component_code: Decimal(str(c.annual_amount)) for c in salary.components}
    return LiveRevision(
        id=salary.id,
        effective_from=salary.effective_from,
        effective_to=salary.effective_to,
        ctc_monthly=Decimal(str(salary.ctc_annual)) / 12,
        parts={code: annual.get(code, _ZERO) / 12 for code in ("BASIC", "HRA", "SPECIAL", "BONUS")},
        reason=salary.revision_reason,
    )


def _matches(live: LiveRevision, rate: Rate) -> bool:
    figures = (
        (live.ctc_monthly, rate[0]),
        (live.parts["BASIC"], rate[1]),
        (live.parts["HRA"], rate[2]),
        (live.parts["SPECIAL"], rate[3]),
        (live.parts["BONUS"], rate[4]),
    )
    return all(abs(a - b) <= _MATCH_LIMIT for a, b in figures)


def _rate_dict(rate: Rate) -> dict[str, str]:
    return {
        "ctc_monthly": _s(rate[0]),
        "basic": _s(rate[1]),
        "hra": _s(rate[2]),
        "special": _s(rate[3]),
        "bonus": _s(rate[4]),
        "gross": _s(rate[5]),
    }


@dataclass(slots=True)
class _SalaryContext:
    structure: SalaryStructure | None
    components: dict[str, SalaryComponent]
    first_month: tuple[int, int]
    last_month: tuple[int, int]
    month_set: set[tuple[int, int]]


def _observations(
    person: Person,
    rows_by_month: dict[tuple[int, int], list[RegisterRow]],
    issues: list[Issue],
) -> list[tuple[tuple[int, int], date, Rate, RegisterRow, bool]]:
    """(month, start, rate, row, mid-month) in time order, one per rate seen.

    Two rates in one month (a change mid-month) are taken in sheet order, except
    that the rate continuing the previous month comes first: the registers list
    the new rate's row above the old one as often as not, and a salary that goes
    back to the old rate the next day is not what happened.
    """
    found: list[tuple[tuple[int, int], date, Rate, RegisterRow, bool]] = []
    previous: Rate | None = None
    for (year, month), month_rows in sorted(rows_by_month.items()):
        rated = [(row, _rate(row)) for row in month_rows]
        distinct = {rate for _, rate in rated if rate is not None}
        if previous is not None and len(distinct) > 1 and previous in distinct:
            rated.sort(key=lambda pair: 0 if pair[1] == previous else 1)
        in_month: list[tuple[int, Rate, RegisterRow]] = []
        offset = 0
        for row, rate in rated:
            if rate is not None and rate[0] <= _ZERO:
                issues.append(
                    Issue(
                        "warning",
                        "rate_without_ctc",
                        f"{person.code}, {_label(year, month)}: the row has a salary but no CTC; "
                        "it is not used for the salary revisions.",
                        month=_month_key(year, month),
                        file=row.file,
                        sheet=row.sheet,
                        row=row.row,
                        code=person.code,
                    )
                )
                rate = None
            if rate is not None:
                if in_month and in_month[-1][1] == rate:
                    pass
                elif in_month and in_month[-1][0] == offset:
                    # The earlier row paid no days: the later rate is the month's.
                    in_month[-1] = (offset, rate, row)
                else:
                    in_month.append((offset, rate, row))
            offset += int((row.days_worked or _ZERO).to_integral_value(rounding=ROUND_FLOOR))
        last_day = calendar.monthrange(year, month)[1]
        for i, (day_offset, rate, row) in enumerate(in_month):
            if i == 0:
                found.append(((year, month), date(year, month, 1), rate, row, False))
            else:
                day = min(day_offset + 1, last_day)
                found.append(((year, month), date(year, month, day), rate, row, True))
        if in_month:
            previous = in_month[-1][1]
    return found


#: The 2023 registers' CTC columns are worked out from the month's figures
#: (Sep-23 "Sep CTC" = CTC Earned / days x 30), so the same pay shows a CTC a few
#: paise apart from one month to the next. That is not a revision.
_CTC_NOISE = Decimal("1.00")


def _same_pay(a: Rate, b: Rate) -> bool:
    """The same salary: every part and the gross equal, the CTC within a rupee."""
    return a[1:] == b[1:] and abs(a[0] - b[0]) <= _CTC_NOISE


def _settle_ctc(segment: Segment) -> None:
    """One CTC for a run of months whose CTC differed only by paise: the one most
    months show, the latest of those on a tie. The others are named in the
    reason; each payslip keeps its own month's CTC as typed."""
    if len(set(segment.ctcs)) < 2:
        return
    counts: dict[Decimal, int] = {}
    last_seen: dict[Decimal, int] = {}
    for i, ctc in enumerate(segment.ctcs):
        counts[ctc] = counts.get(ctc, 0) + 1
        last_seen[ctc] = i
    best = max(counts, key=lambda c: (counts[c], last_seen[c]))
    segment.rate = (best, *segment.rate[1:])
    segment.notes.append(
        f"CTC as typed varied by under {_CTC_NOISE} a month between these registers "
        f"({min(counts)} to {max(counts)}); {best} kept"
    )


def _segments(
    person: Person,
    observations: list[tuple[tuple[int, int], date, Rate, RegisterRow, bool]],
    first_seen: tuple[int, int],
    ctx: _SalaryContext,
) -> list[Segment]:
    segments: list[Segment] = []
    for (year, month), start, rate, row, mid in observations:
        key = _month_key(year, month)
        ref = f"{row.file} {row.sheet} row {row.row}"
        if segments and _same_pay(segments[-1].rate, rate):
            if key not in segments[-1].months:
                segments[-1].months.append(key)
            segments[-1].ctcs.append(rate[0])
            segments[-1].refs.append(ref)
            if row.file not in segments[-1].files:
                segments[-1].files.append(row.file)
            continue
        segment = Segment(
            rate=rate, start=start, months=[key], refs=[ref], files=[row.file], ctcs=[rate[0]]
        )
        if mid:
            segment.notes.append(
                f"from day {start.day} of {_label(year, month)} (a second rate in that month's "
                "register)"
            )
        elif segments and (year, month) != ctx.first_month:
            previous = _previous_month(year, month)
            if previous not in ctx.month_set:
                segment.notes.append(
                    f"may have started a month earlier (no register for {_label(*previous)})"
                )
        segments.append(segment)
    if not segments:
        return segments
    for segment in segments:
        _settle_ctc(segment)

    # The first revision: from joining when that falls in the registers' span,
    # else from the first register the person is in.
    first = segments[0]
    span_start = date(*ctx.first_month, 1)
    cutoff = span_start - timedelta(days=6)  # the 26th: a client cycle's first day
    seen_start = date(*first_seen, 1)
    seen_end = _month_end(*first_seen)
    first.notes = [n for n in first.notes if not n.startswith("may have started")]
    if person.joined is not None and person.joined >= cutoff:
        start = max(person.joined, span_start)
        if start > seen_end:
            first.notes.append(
                f"AskMira's joining date {person.joined.isoformat()} is after the first register "
                f"with this person ({_label(*first_seen)}); dated from that register"
            )
            start = seen_start
    elif first_seen == ctx.first_month:
        start = span_start
        first.notes.append(
            f"In force at least from {_label(*ctx.first_month)} (the first register available)"
        )
    else:
        start = seen_start
        first.notes.append(
            f"In force at least from {_label(*first_seen)} (the first register with this person)"
        )
    first.start = start
    return segments


def _reason(segment: Segment) -> str:
    first = segment.months[0]
    last = segment.months[-1]

    def label(key: str) -> str:
        year, month = key.split("-")
        return _label(int(year), int(month))

    text_ = f"factoHR payroll register {label(first)}"
    if last != first:
        text_ += f" {_EN_DASH} {label(last)}"
    if segment.notes:
        text_ += ". " + "; ".join(segment.notes)
    return text_[:255]


_WEF_NOTE = "Start date is the sheet's WEF"


def _redated_reason(old: str | None, note: str) -> str:
    """The kept revision's reason, with its start date's source replaced."""
    base = (old or "").strip()
    if _WEF_NOTE in base:
        base = base[: base.index(_WEF_NOTE)].rstrip()
    return f"{base} {note}".strip()[:255]


def _active(person: Person, last_month: tuple[int, int]) -> bool:
    if person.status is not EmploymentStatus.EXITED:
        return True
    return person.exited is not None and person.exited > _month_end(*last_month)


async def _resolve_segment(
    session: AsyncSession,
    user: User,
    person: Person,
    segment: Segment,
    ctx: _SalaryContext,
    *,
    strict: bool,
    issues: list[Issue],
) -> tuple[EmployeeSalary, ResolvedSalary] | None:
    assert ctx.structure is not None
    ctc, basic, hra, special, bonus, gross = segment.rate
    where = {"code": person.code, "month": segment.months[0]}
    try:
        payload = EmployeeSalaryCreate(
            entry="manual",
            ctc_monthly=ctc,
            gross_amount=gross if gross > _ZERO else None,
            gross_basis="monthly",
            components=[
                SalaryComponentAmountInput(component_code=code, amount=amount, basis="monthly")
                for code, amount in (
                    ("BASIC", basic),
                    ("HRA", hra),
                    ("SPECIAL", special),
                    ("BONUS", bonus),
                )
                if amount != _ZERO
            ],
            structure_id=ctx.structure.id,
            effective_from=segment.start,
            revision_reason=_reason(segment),
        )
    except ValidationError as exc:
        issues.append(
            Issue(
                "error",
                "salary_rule",
                f"{person.code}: the register rate from {segment.start.isoformat()} cannot be "
                f"recorded: {exc.errors()[0].get('msg')}",
                **where,
            )
        )
        return None
    try:
        resolved = await resolve_amounts(
            session, user, ctx.structure, payload, strict=strict, components=ctx.components
        )
    except HTTPException as exc:
        issues.append(
            Issue(
                "error",
                "salary_rule",
                f"{person.code}: the register rate from {segment.start.isoformat()} cannot be "
                f"recorded as {person.code}'s current salary: {exc.detail}",
                **where,
            )
        )
        return None
    for message in resolved.warnings:
        issues.append(
            Issue(
                "note",
                "salary_rule",
                f"{person.code}, revision from {segment.start.isoformat()}: {message} "
                "(kept as the register had it)",
                **where,
            )
        )
    salary = _new_salary(user, person.id, ctx.structure, payload, resolved)
    salary.effective_to = segment.end
    return salary, resolved


async def _plan_salaries(
    session: AsyncSession,
    user: User,
    rows_by_person: dict[uuid.UUID, dict[tuple[int, int], list[RegisterRow]]],
    persons: dict[uuid.UUID, Person],
    ctx: _SalaryContext,
    issues: list[Issue],
    live_objects: dict[uuid.UUID, EmployeeSalary],
) -> list[PersonSalary]:
    existing: dict[uuid.UUID, list[EmployeeSalary]] = defaultdict(list)
    if rows_by_person:
        for salary in (
            await session.scalars(
                tenant_scoped(
                    select(EmployeeSalary)
                    .where(EmployeeSalary.employee_id.in_(list(rows_by_person)))
                    .order_by(EmployeeSalary.effective_from),
                    EmployeeSalary,
                    user,
                )
            )
        ).all():
            existing[salary.employee_id].append(salary)
    locks = await revision_locks(
        session, user, [s.id for series in existing.values() for s in series]
    )

    plans: list[PersonSalary] = []
    for person_id, by_month in sorted(rows_by_person.items(), key=lambda kv: persons[kv[0]].code):
        person = persons[person_id]
        series = existing.get(person_id, [])
        result = PersonSalary(person=person, action="keep")
        plans.append(result)

        def as_timeline(revisions: list[EmployeeSalary]) -> list[_Span]:
            return [(s.id, s.effective_from, s.effective_to) for s in revisions]

        if len(series) > 1 or any(s.id in locks for s in series):
            result.action = "left_as_is"
            result.timeline = as_timeline(series)
            why = (
                "payroll already approved on it"
                if any(s.id in locks for s in series)
                else f"{len(series)} revisions already"
            )
            issues.append(
                Issue(
                    "warning",
                    "salary_left_as_is",
                    f"{person.code}: salary revisions left as they are ({why}); the register "
                    "rates are not recorded. Payslips link to the revision in force.",
                    code=person.code,
                )
            )
            continue

        live_obj = series[0] if series else None
        if live_obj is not None:
            result.live = _live(live_obj)
            live_objects[live_obj.id] = live_obj

        observations = _observations(person, by_month, issues)
        first_seen = min(by_month)
        segments = _segments(person, observations, first_seen, ctx)
        if not segments:
            result.action = "keep" if live_obj is not None else "none"
            result.timeline = as_timeline(series)
            issues.append(
                Issue(
                    "note",
                    "no_rate",
                    f"{person.code}: no full-month rate in the registers"
                    + ("; the current salary is kept as it is." if live_obj else "."),
                    code=person.code,
                )
            )
            continue

        last = segments[-1]
        live = result.live
        in_last_month = ctx.last_month in by_month
        last_seen_end = _month_end(*max(by_month))
        if live is None:
            result.action = "no_live"
        elif _matches(live, last.rate):
            result.action = "match_last"
        elif live.effective_from > last_seen_end and (
            person.exited is None or person.exited >= live.effective_from
        ):
            # It starts after the last register with this person, while they
            # were still employed (a move to another company, a later
            # increment): nothing in the registers contradicts it, so it stays
            # as their next revision. One dated after they left is replaced.
            result.action = "later"
        elif any(_matches(live, s.rate) for s in segments[:-1]):
            result.action = "replace"
            result.notes.append("the current salary matches an earlier register rate")
        elif _active(person, ctx.last_month) and in_last_month:
            result.action = "increment"
        else:
            result.action = "replace"

        if result.action == "match_last":
            assert live is not None
            last.record = "live"
            last.salary_id = live.id
            first_paid = last.months[0]
            first_label = _label(int(first_paid[:4]), int(first_paid[5:7]))
            if len(segments) == 1 and live.effective_from < last.start:
                last.start = live.effective_from
                last.notes = [n for n in last.notes if not n.startswith("In force at least")]
                result.live_new_reason = _redated_reason(
                    live.reason,
                    "Start date as in the Employee Detail; paid at this rate in every factoHR "
                    f"payroll register from {first_label}.",
                )
            else:
                result.live_new_reason = _redated_reason(
                    live.reason,
                    "Start date from the factoHR payroll registers: first paid at this rate in "
                    f"{first_label}.",
                )
            result.live_new_from = last.start
        elif result.action == "increment":
            assert live is not None
            after_last = date(*_next_month(*ctx.last_month), 1)
            result.live_new_from = max(live.effective_from, after_last)
            result.live_new_reason = _redated_reason(
                live.reason,
                f"Not in the factoHR payroll registers (last: {_label(*ctx.last_month)}); dated "
                "after them until the real date is set.",
            )
            issues.append(
                Issue(
                    "warning",
                    "increment_date_unknown",
                    f"{person.code}: the current salary (CTC {_s(live.ctc_monthly)} a month) "
                    f"differs from the last register rate (CTC {_s(last.rate[0])}, "
                    f"{_label(*ctx.last_month)}); it is kept as a later increment from "
                    f"{result.live_new_from.isoformat()}. That date is not in the registers: set "
                    "the real date in the Salary master.",
                    code=person.code,
                )
            )
        elif result.action == "later":
            assert live is not None
            issues.append(
                Issue(
                    "note",
                    "salary_kept_later",
                    f"{person.code}: the current salary from {live.effective_from.isoformat()} "
                    f"(CTC {_s(live.ctc_monthly)} a month) starts after the last register with "
                    f"this person ({_label(*max(by_month))}); it is kept as their next revision, "
                    "after the register rates.",
                    code=person.code,
                )
            )
        elif result.action == "replace":
            assert live is not None
            result.delete_live = True
            issues.append(
                Issue(
                    "warning",
                    "salary_replaced",
                    f"{person.code}: the current salary from {live.effective_from.isoformat()} "
                    f"(CTC {_s(live.ctc_monthly)} a month) is replaced by the register rates "
                    f"(last: CTC {_s(last.rate[0])} from {last.start.isoformat()})"
                    + (f"; {result.notes[-1]}" if result.notes else "")
                    + ".",
                    code=person.code,
                )
            )
        else:
            issues.append(
                Issue(
                    "note",
                    "no_live_salary",
                    f"{person.code}: no salary in AskMira; {len(segments)} revision(s) recorded "
                    "from the registers.",
                    code=person.code,
                )
            )

        # Dates: each revision runs until the next one starts.
        starts = [s.start for s in segments]
        if result.action == "increment":
            assert result.live_new_from is not None
            starts.append(result.live_new_from)
        elif result.action == "later":
            assert live is not None
            starts.append(live.effective_from)
        if any(b <= a for a, b in pairwise(starts)):
            result.action = "left_as_is"
            result.segments = []
            result.delete_live = False
            result.live_new_from = None
            result.timeline = as_timeline(series)
            issues.append(
                Issue(
                    "warning",
                    "salary_order",
                    f"{person.code}: the register rates give revision dates out of order "
                    f"({', '.join(d.isoformat() for d in starts)}); salary left as it is.",
                    code=person.code,
                )
            )
            continue
        for i, segment in enumerate(segments):
            nxt = starts[i + 1] if i + 1 < len(starts) else None
            segment.end = nxt - timedelta(days=1) if nxt else None
        result.segments = segments

        # Build the new revisions; the open one of somebody still employed is
        # checked as strictly as any salary that future payroll will use.
        previous_ctc: Decimal | None = None
        timeline: list[_Span] = []
        for segment in segments:
            if segment.record == "live":
                assert live is not None
                timeline.append((live.id, segment.start, None))
                continue
            strict = segment.end is None and _active(person, ctx.last_month)
            built = await _resolve_segment(
                session, user, person, segment, ctx, strict=strict, issues=issues
            )
            if built is None:
                continue
            salary, resolved = built
            segment.salary_id = salary.id
            result.new_revisions.append((salary, segment, resolved, previous_ctc))
            previous_ctc = resolved.ctc_annual
            timeline.append((salary.id, segment.start, segment.end))
        if result.action == "increment":
            assert live is not None and result.live_new_from is not None
            timeline.append((live.id, result.live_new_from, None))
        elif result.action == "later":
            assert live is not None
            timeline.append((live.id, live.effective_from, live.effective_to))
        result.timeline = timeline

        if live_obj is not None and result.live_new_from is not None:
            reason_changes = (
                result.live_new_reason is not None
                and result.live_new_reason != live_obj.revision_reason
            )
            if (
                result.live_new_from != live_obj.effective_from
                or live_obj.effective_to is not None
                or reason_changes
            ):
                name = live_obj.structure.name if live_obj.structure else None
                result.live_before = revision_snapshot(live_obj, name)
                result.live_after = {
                    **result.live_before,
                    "effective_from": result.live_new_from.isoformat(),
                }
                if reason_changes:
                    result.live_after["reason"] = result.live_new_reason
                else:
                    result.live_new_reason = None
            else:
                result.live_new_from = None  # already right
                result.live_new_reason = None
        if result.delete_live and live_obj is not None:
            name = live_obj.structure.name if live_obj.structure else None
            result.live_before = revision_snapshot(live_obj, name)
    return plans


def _in_force(timeline: list[_Span], day: date) -> uuid.UUID | None:
    found = [
        (start, salary_id)
        for salary_id, start, end in timeline
        if start <= day and (end is None or end >= day)
    ]
    return max(found)[1] if found else None


# ── Plan ────────────────────────────────────────────────────────────────────


async def plan(
    session: AsyncSession,
    user: User,
    months: list[RegisterMonth],
    *,
    legal_entity_id: uuid.UUID | None = None,
) -> ImportPlan:
    """Everything the import would write, checked, without writing anything."""
    issues: list[Issue] = []
    files: list[RegisterFile] = []
    dated = [m for m in months if m.year]
    for month in months:
        files.extend(month.files)
        issues.extend(month.issues)
        for f in month.files:
            issues.extend(f.issues)
            for note in f.notes:
                month_key = _month_key(f.year, f.month) if f.year and f.month else None
                issues.append(
                    Issue("note", "register_layout", note, month=month_key, file=f.name)
                )

    # The company the months were paid by.
    entities = (
        await session.execute(
            tenant_scoped(
                select(LegalEntity.id, LegalEntity.name, LegalEntity.code), LegalEntity, user
            )
        )
    ).all()
    entity = None
    if legal_entity_id is not None:
        entity = next((e for e in entities if e.id == legal_entity_id), None)
        if entity is None:
            issues.append(Issue("error", "company", "That company is not in this workspace."))
    elif len(entities) == 1:
        entity = entities[0]
    else:
        issues.append(
            Issue(
                "error",
                "company",
                f"This workspace has {len(entities)} companies; choose the one these months were "
                "paid by.",
            )
        )

    structure = await session.scalar(
        tenant_scoped(
            select(SalaryStructure)
            .where(SalaryStructure.name == STRUCTURE_NAME)
            .order_by(SalaryStructure.effective_from),
            SalaryStructure,
            user,
        )
    )
    if structure is None:
        issues.append(
            Issue(
                "error",
                "structure_missing",
                f"No '{STRUCTURE_NAME}' salary structure: set up the standard wage types first.",
            )
        )
    components = {
        c.code: c
        for c in (
            await session.scalars(tenant_scoped(select(SalaryComponent), SalaryComponent, user))
        ).all()
    }
    missing = [c for c in ("BASIC", "HRA", "SPECIAL", "BONUS") if c not in components]
    if missing:
        issues.append(
            Issue(
                "error",
                "component_missing",
                f"Wage type(s) {', '.join(missing)} missing: set up the standard wage types first.",
            )
        )

    people = await _people(session, user)
    mapping, people_summary = _match_people(dated, people, issues)
    projects = await _projects(session, user)

    # Months that already have a regular run for the company are refused.
    taken: set[tuple[int, int]] = set()
    if entity is not None and dated:
        rows = await session.execute(
            tenant_scoped(
                select(PayrollRun.period_year, PayrollRun.period_month, PayrollRun.origin).where(
                    PayrollRun.legal_entity_id == entity.id,
                    PayrollRun.run_type == PayrollRunType.REGULAR,
                    PayrollRun.status != PayrollRunStatus.CANCELLED,
                    PayrollRun.period_year.in_({m.year for m in dated}),
                ),
                PayrollRun,
                user,
            )
        )
        wanted = {(m.year, m.month) for m in dated}
        for year, month, origin in rows.all():
            if (year, month) not in wanted:
                continue
            taken.add((year, month))
            issues.append(
                Issue(
                    "error",
                    "month_already_run",
                    f"{_label(year, month)} already has a payroll run for this company "
                    + (
                        "(imported earlier); it is not imported again. Undo that import first "
                        "if it must be replaced."
                        if origin == RUN_ORIGIN_IMPORTED
                        else "(computed in AskMira); a month AskMira paid is never imported."
                    ),
                    month=_month_key(year, month),
                )
            )

    # Payslips, one per person per month.
    month_plans: list[MonthPlan] = []
    rows_by_person: dict[uuid.UUID, dict[tuple[int, int], list[RegisterRow]]] = defaultdict(dict)
    persons: dict[uuid.UUID, Person] = {}
    unmatched_accounts: Counter = Counter()
    for month in dated:
        mp = MonthPlan(year=month.year, month=month.month, files=month.files, run_id=uuid.uuid4())
        mp.refused = (month.year, month.month) in taken or any(
            i.level == "error" for i in month.issues
        ) or any(i.level == "error" for f in month.files for i in f.issues)
        month_plans.append(mp)
        if len(month.files) != 1:
            continue
        main = month.files[0].main
        if main is not None:
            mp.register_totals = dict(main.register_totals)
            for key in main.register_totals:
                mp.sheet_sums[key] = sum((r.money(key) or _ZERO for r in main.rows), _ZERO)
        grouped: dict[uuid.UUID, list[RegisterRow]] = {}
        for row in month.rows:
            mp.rows_read += 1
            person = mapping.get(row.code)
            if person is None:
                continue
            grouped.setdefault(person.id, []).append(row)
            persons[person.id] = person
        for person_id, rows in grouped.items():
            person = persons[person_id]
            rows_by_person[person_id][(month.year, month.month)] = rows
            slip = _build_payslip(
                person,
                rows,
                month.year,
                month.month,
                projects=projects,
                unmatched_accounts=unmatched_accounts,
                issues=issues,
            )
            mp.payslips.append(slip)
            if len(rows) > 1:
                mp.combined_rows += len(rows)
                issues.append(
                    Issue(
                        "note",
                        "combined_rows",
                        f"{person.code}, {mp.label}: {len(rows)} rows ("
                        + ", ".join(f"{r.sheet} row {r.row}" for r in rows)
                        + ") make one payslip, their cells added up.",
                        month=mp.key,
                        code=person.code,
                    )
                )
            if slip.zero_row:
                mp.zero_rows += 1
            if entity is not None and person.legal_entity_id not in (None, entity.id):
                issues.append(
                    Issue(
                        "warning",
                        "other_company",
                        f"{person.code} belongs to another company in AskMira but is in the "
                        f"{mp.label} register.",
                        month=mp.key,
                        code=person.code,
                    )
                )
            start, end = date(month.year, month.month, 1), _month_end(month.year, month.month)
            if person.exited is not None and person.exited < start:
                issues.append(
                    Issue(
                        "note",
                        "outside_employment",
                        f"{person.code}: paid in {mp.label}, after the AskMira exit date "
                        f"{person.exited.isoformat()} (a 26th-25th client cycle, or a late "
                        "settlement).",
                        month=mp.key,
                        code=person.code,
                    )
                )
            elif person.joined is not None and person.joined > end:
                issues.append(
                    Issue(
                        "note",
                        "outside_employment",
                        f"{person.code}: paid in {mp.label}, before the AskMira joining date "
                        f"{person.joined.isoformat()} (a rehire, or a joining date to check).",
                        month=mp.key,
                        code=person.code,
                    )
                )
    for account, count in sorted(unmatched_accounts.items()):
        issues.append(
            Issue(
                "note",
                "project_unmatched",
                f"Client/project '{account}' ({count} payslips) is no AskMira project; those "
                "payslips have no project (the register's text is kept on them).",
            )
        )

    # Salary revisions, then which revision each payslip was paid on.
    salaries: list[PersonSalary] = []
    live_objects: dict[uuid.UUID, EmployeeSalary] = {}
    if dated:
        ctx = _SalaryContext(
            structure=structure,
            components=components,
            first_month=(dated[0].year, dated[0].month),
            last_month=(dated[-1].year, dated[-1].month),
            month_set={(m.year, m.month) for m in dated},
        )
        if structure is not None and not missing:
            salaries = await _plan_salaries(
                session, user, rows_by_person, persons, ctx, issues, live_objects
            )
    timelines = {s.person.id: s.timeline for s in salaries}
    for mp in month_plans:
        start, end = date(mp.year, mp.month, 1), _month_end(mp.year, mp.month)
        for slip in mp.payslips:
            day = end
            exited = slip.person.exited
            if exited is not None and start <= exited <= end:
                day = exited
            slip.employee_salary_id = _in_force(timelines.get(slip.person.id, []), day)

    company = entity.name if entity is not None else "Employer"
    for mp in month_plans:
        for slip in mp.payslips:
            _pdf_check(slip, company, issues)

    return ImportPlan(
        tenant_id=user.tenant_id,
        user_id=user.id,
        legal_entity_id=entity.id if entity is not None else None,
        legal_entity_name=company,
        structure_id=structure.id if structure is not None else None,
        months=month_plans,
        files=files,
        salaries=salaries,
        issues=issues,
        people=people_summary,
        planned_at=datetime.now(UTC),
        _live_objects=live_objects,
    )


#: What one upload may hold: a register is a few hundred KB, one has 3 MB of styling.
MAX_FILES = 60
MAX_FILE_BYTES = 25 * 1024 * 1024


async def check_files(
    session: AsyncSession,
    user: User,
    files: list[tuple[str, bytes]],
    *,
    legal_entity_id: uuid.UUID | None = None,
) -> ImportPlan:
    """Read uploaded registers and plan their import (nothing is written)."""
    if not files:
        raise HTTPException(status_code=422, detail="Choose the register files to check.")
    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_FILES} files at a time; {len(files)} given."
        )
    for name, data in files:
        if not name.lower().endswith(".xlsx"):
            raise HTTPException(
                status_code=422, detail=f"{name}: only Excel workbooks (.xlsx) can be read."
            )
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=422,
                detail=f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.",
            )
    # Reading workbooks is CPU work: off the event loop.
    months = await asyncio.to_thread(parse_registers, files)
    return await plan(session, user, months, legal_entity_id=legal_entity_id)


# ── Apply ───────────────────────────────────────────────────────────────────


def _payslip_values(
    slip: PayslipPlan, tenant_id: uuid.UUID, run_id: uuid.UUID, at: datetime
) -> dict[str, Any]:
    return {
        "id": slip.id,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "employee_id": slip.person.id,
        "employee_salary_id": slip.employee_salary_id,
        "project_id": slip.project_id,
        "working_days": slip.working_days,
        "paid_days": slip.paid_days,
        "lop_days": slip.lop_days,
        "not_employed_days": slip.not_employed_days,
        "gross_earnings": slip.gross_earnings,
        "total_deductions": slip.total_deductions,
        "net_pay": slip.net_pay,
        "employer_cost": slip.employer_cost,
        "pf_wage_base": slip.pf_wage_base,
        "esi_wage_base": slip.esi_wage_base,
        "pf_employee": slip.pf_employee,
        "pf_employer": slip.pf_employer,
        "esi_employee": slip.esi_employee,
        "esi_employer": slip.esi_employer,
        "professional_tax": slip.professional_tax,
        "tds": slip.tds,
        "lwf_employee": _ZERO,
        "lwf_employer": _ZERO,
        "vpf": _ZERO,
        "review_flags": [],
        "line_items": slip.line_items,
        "input_lines": slip.input_lines,
        "tds_working": None,
        "import_details": slip.import_details,
        "is_supplementary": False,
        "is_published": True,
        "published_at": at,
    }


def _file_summaries(files: list[RegisterFile]) -> list[dict[str, Any]]:
    return [f.summary() for f in files]


async def apply(
    session: AsyncSession,
    user: User,
    import_plan: ImportPlan,
    request: Request | None = None,
    *,
    dry_run: bool = False,
) -> ImportPlan:
    """Write the plan in one transaction, with its audit rows; or nothing at all.

    A plan with any error writes nothing. `dry_run` writes and flushes it all,
    so the database's own checks run, then rolls back.
    """
    p = import_plan
    p.dry_run = dry_run
    if p.blocking:
        return p
    tenant_id, user_id = p.tenant_id, p.user_id
    at = datetime.now(UTC)

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"payroll-history-import:{tenant_id}"},
    )
    # A month imported by somebody else since the plan was made.
    wanted = {(m.year, m.month) for m in p.months}
    existing = (
        await session.execute(
            tenant_scoped(
                select(PayrollRun.period_year, PayrollRun.period_month).where(
                    PayrollRun.legal_entity_id == p.legal_entity_id,
                    PayrollRun.run_type == PayrollRunType.REGULAR,
                    PayrollRun.status != PayrollRunStatus.CANCELLED,
                    PayrollRun.period_year.in_({year for year, _ in wanted}),
                ),
                PayrollRun,
                user,
            )
        )
    ).all()
    if any((year, month) in wanted for year, month in existing):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Some of these months were given a payroll run since the check. Check again.",
        )

    # 1. Salary revisions: deletions, new dates, new revisions — before any run
    #    can lock them.
    redated = deleted = created = 0
    for person_salary in p.salaries:
        person = person_salary.person
        live = person_salary.live
        live_obj = p._live_objects.get(live.id) if live else None
        if person_salary.delete_live and live_obj is not None:
            await audit.record(
                session,
                tenant_id=tenant_id,
                action=AuditAction.DELETE,
                entity_type="employee_salary",
                entity_id=live_obj.id,
                actor=user,
                changes={
                    "employee_id": str(person.id),
                    "deleted": person_salary.live_before,
                    "replaced_by": "factoHR payroll registers",
                    "reason": (
                        "The registers are the payroll record up to the last month paid there; "
                        "this revision was to be corrected from payroll history."
                    ),
                },
                summary=(
                    f"Salary revision from {live_obj.effective_from.isoformat()} replaced for "
                    f"{person.code} by the factoHR payroll registers"
                ),
                request=request,
                flush=False,
            )
            await session.delete(live_obj)
            deleted += 1
        elif person_salary.live_new_from is not None and live_obj is not None:
            old_from = live_obj.effective_from
            live_obj.effective_from = person_salary.live_new_from
            live_obj.effective_to = None
            if person_salary.live_new_reason is not None:
                live_obj.revision_reason = person_salary.live_new_reason
            await audit.record(
                session,
                tenant_id=tenant_id,
                action=AuditAction.UPDATE,
                entity_type="employee_salary",
                entity_id=live_obj.id,
                actor=user,
                changes={
                    "employee_id": str(person.id),
                    "before": person_salary.live_before,
                    "after": person_salary.live_after,
                    "reason": "dated from the factoHR payroll registers",
                },
                summary=(
                    f"Salary revision for {person.code} dated from the factoHR payroll registers: "
                    f"{old_from.isoformat()} to {person_salary.live_new_from.isoformat()}"
                ),
                request=request,
                flush=False,
            )
            redated += 1
        for salary, segment, resolved, previous_ctc in person_salary.new_revisions:
            session.add(salary)
            await audit.record(
                session,
                tenant_id=tenant_id,
                action=AuditAction.CREATE,
                entity_type="employee_salary",
                entity_id=salary.id,
                actor=user,
                changes={
                    "employee_id": str(person.id),
                    "old_ctc": _s(previous_ctc) if previous_ctc is not None else None,
                    "new_ctc": _s(resolved.ctc_annual),
                    "structure": STRUCTURE_NAME,
                    "effective_from": segment.start.isoformat(),
                    "effective_to": segment.end.isoformat() if segment.end else None,
                    "reason": salary.revision_reason,
                    "typed_amounts": manual_summary(resolved),
                    "source": {
                        "system": SYSTEM,
                        "files": list(segment.files),
                        "months": list(segment.months),
                    },
                },
                summary=(
                    f"Salary from {segment.start.isoformat()} recorded for {person.code} from the "
                    "factoHR payroll registers"
                ),
                request=request,
                flush=False,
            )
            created += 1
    await session.flush()

    # 2. One locked, published run per month, and its payslips.
    run_ids: list[uuid.UUID] = []
    for mp in p.months:
        totals = mp.totals()
        file_rows = _file_summaries(mp.files)
        register_totals = mp.register_totals
        run = PayrollRun(
            id=mp.run_id,
            tenant_id=tenant_id,
            legal_entity_id=p.legal_entity_id,
            period_year=mp.year,
            period_month=mp.month,
            effective_date=_month_end(mp.year, mp.month),
            payment_date=None,
            run_type=PayrollRunType.REGULAR,
            status=PayrollRunStatus.PAID,
            employee_count=len(mp.payslips),
            total_gross=totals["gross"],
            total_deductions=totals["deductions"],
            total_net=totals["net"],
            total_employer_cost=totals["employer_cost"],
            variance_flags=[],
            review_flags=[],
            rule_set_snapshot={},
            processed_at=None,
            computed_by_user_id=None,
            approved_by_user_id=user_id,
            approved_at=at,
            published_by_user_id=user_id,
            published_at=at,
            paid_by_user_id=user_id,
            paid_at=at,
            input_notices=[],
            employee_scope=[],
            included_sources=None,
            reason="Imported from factoHR: " + ", ".join(f.name for f in mp.files),
            origin=RUN_ORIGIN_IMPORTED,
            import_details={
                "system": SYSTEM,
                "imported_at": at.isoformat(),
                "imported_by_user_id": str(user_id),
                "files": file_rows,
                "register_totals": register_totals,
                "computed_totals": {
                    **{k: _s(v) for k, v in totals.items()},
                    "sheet_sums": {k: str(v) for k, v in mp.sheet_sums.items()},
                },
            },
        )
        session.add(run)
        run_ids.append(run.id)
        changes = {
            "origin": RUN_ORIGIN_IMPORTED,
            "system": SYSTEM,
            "period": f"{mp.month}/{mp.year}",
            "files": [{"name": f.name, "sha256": f.sha256} for f in mp.files],
            "payslips": len(mp.payslips),
            "totals": {k: _s(v) for k, v in totals.items()},
            "register_totals": register_totals,
        }
        await audit.record(
            session,
            tenant_id=tenant_id,
            action=AuditAction.CREATE,
            entity_type="payroll_run",
            entity_id=run.id,
            actor=user,
            changes=changes,
            summary=f"Imported the {mp.label} payroll from factoHR ({len(mp.payslips)} payslips)",
            request=request,
            flush=False,
        )
        await audit.record(
            session,
            tenant_id=tenant_id,
            action=AuditAction.APPROVE,
            entity_type="payroll_run",
            entity_id=run.id,
            actor=user,
            changes={"origin": RUN_ORIGIN_IMPORTED, "status": PayrollRunStatus.PAID.value},
            summary="Locked on import: figures as paid on factoHR",
            request=request,
            flush=False,
        )
    await session.flush()
    values = [
        _payslip_values(slip, tenant_id, mp.run_id, at) for mp in p.months for slip in mp.payslips
    ]
    if values:
        # `render_nulls`: an empty project or salary link is sent as NULL rather
        # than left out, so every row has the same columns and goes in one batch.
        await session.execute(insert(Payslip).execution_options(render_nulls=True), values)

    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CREATE,
        entity_type="payroll_history_import",
        entity_id=None,
        actor=user,
        changes={
            "system": SYSTEM,
            "legal_entity_id": str(p.legal_entity_id),
            "months": [mp.key for mp in p.months],
            "runs": [str(r) for r in run_ids],
            "files": [{"name": f.name, "sha256": f.sha256, "size": f.size} for f in p.files],
            "payslips": len(values),
            "revisions_new": created,
            "revisions_redated": redated,
            "revisions_deleted": deleted,
            "increments_date_unknown": [
                s.person.code for s in p.salaries if s.action == "increment"
            ],
        },
        summary=(
            f"Imported {len(run_ids)} months of factoHR payroll ({len(values)} payslips) and "
            f"{created} salary revisions"
        ),
        request=request,
        flush=True,
    )
    p.run_ids = run_ids
    p.stats = {
        "revisions_new": created,
        "revisions_redated": redated,
        "revisions_deleted": deleted,
        "payslips": len(values),
        "runs": len(run_ids),
    }
    if dry_run:
        await session.rollback()
        p.applied = False
    else:
        await session.commit()
        p.applied = True
    return p


# ── Undo ────────────────────────────────────────────────────────────────────


async def undo_imported_run(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    request: Request | None = None,
) -> None:
    """Remove one imported month and its payslips. Salary revisions stay."""
    run = await session.scalar(
        tenant_scoped(select(PayrollRun).where(PayrollRun.id == run_id), PayrollRun, user)
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payroll run not found")
    if run.origin != RUN_ORIGIN_IMPORTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Only a month imported from factoHR can be undone this way. A run AskMira "
                "computed follows its own lifecycle."
            ),
        )
    count = int(
        await session.scalar(
            tenant_scoped(
                select(func.count(Payslip.id)).where(Payslip.run_id == run.id), Payslip, user
            )
        )
        or 0
    )
    snapshot = {
        "origin": run.origin,
        "period": f"{run.period_month}/{run.period_year}",
        "legal_entity_id": str(run.legal_entity_id),
        "status": run.status.value,
        "payslips_removed": count,
        "total_gross": str(run.total_gross),
        "total_net": str(run.total_net),
        "files": [f.get("name") for f in (run.import_details or {}).get("files", [])],
    }
    label = _label(run.period_year, run.period_month)
    deleted_id = run.id
    # Bulk statements: both rows were reached through a tenant-scoped read.
    await session.execute(delete(Payslip).where(Payslip.run_id == deleted_id))
    await session.execute(delete(PayrollRun).where(PayrollRun.id == deleted_id))
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="payroll_run",
        entity_id=deleted_id,
        actor=user,
        changes=snapshot,
        summary=f"Undid the {label} payroll imported from factoHR ({count} payslips removed)",
        request=request,
    )
    await session.commit()


# ── Report ──────────────────────────────────────────────────────────────────


def _issues_of(p: ImportPlan, level: str, month: str | None = None) -> list[dict[str, Any]]:
    return [
        i.as_dict()
        for i in p.issues
        if i.level == level and (month is None or i.month == month)
    ]


def preview(p: ImportPlan) -> dict[str, Any]:
    """The plan as the report shows it: months, salary changes, people, issues."""
    months: list[dict[str, Any]] = []
    overall = Counter()
    for mp in p.months:
        totals = mp.totals()
        counts = Counter(code for slip in mp.payslips for code in slip.codes)
        overall.update(counts)
        differences = {}
        for key, raw in mp.register_totals.items():
            register = Decimal(raw)
            computed = mp.sheet_sums.get(key, _ZERO)
            differences[key] = {
                "in_register": _s(register),
                "computed": _s(computed),
                "difference": _s(register - computed),
            }
        months.append(
            {
                "year": mp.year,
                "month": mp.month,
                "label": mp.label,
                "files": [f.name for f in mp.files],
                "sheets": [s.summary() for f in mp.files for s in f.sheets],
                "rows_read": mp.rows_read,
                "payslips": len(mp.payslips),
                "people": len({slip.person.id for slip in mp.payslips}),
                "combined_rows": mp.combined_rows,
                "zero_rows": mp.zero_rows,
                "refused": mp.refused,
                "totals": {k: _s(v) for k, v in totals.items()},
                "register_totals": dict(mp.register_totals),
                "differences": differences,
                "line_counts": {code: counts.get(code, 0) for code in _DIFF_CODES},
                "notes": [i["message"] for i in _issues_of(p, "note", mp.key)],
                "warnings": [i["message"] for i in _issues_of(p, "warning", mp.key)],
                "errors": [i["message"] for i in _issues_of(p, "error", mp.key)],
            }
        )

    persons = []
    for s in p.salaries:
        segments = [
            {
                "effective_from": seg.start.isoformat(),
                "effective_to": seg.end.isoformat() if seg.end else None,
                "months": list(seg.months),
                "record": seg.record,
                "notes": list(seg.notes),
                **_rate_dict(seg.rate),
            }
            for seg in s.segments
        ]
        persons.append(
            {
                "code": s.person.code,
                "name": s.person.name,
                "action": s.action,
                "live": (
                    {
                        "effective_from": s.live.effective_from.isoformat(),
                        "ctc_monthly": _s(s.live.ctc_monthly),
                        "new_effective_from": s.live_new_from.isoformat()
                        if s.live_new_from
                        else None,
                        "deleted": s.delete_live,
                    }
                    if s.live
                    else None
                ),
                "segments": segments,
                "notes": list(s.notes),
            }
        )
    salary = {
        "revisions_new": sum(len(s.new_revisions) for s in p.salaries),
        "revisions_redated": sum(1 for s in p.salaries if s.live_new_from is not None),
        "revisions_deleted": sum(1 for s in p.salaries if s.delete_live),
        "increments_date_unknown": [s.person.code for s in p.salaries if s.action == "increment"],
        "replaced": [s.person.code for s in p.salaries if s.action == "replace"],
        "no_live_salary": [s.person.code for s in p.salaries if s.action == "no_live"],
        "left_as_is": [s.person.code for s in p.salaries if s.action == "left_as_is"],
        "kept_later": [s.person.code for s in p.salaries if s.action == "later"],
        "actions": dict(Counter(s.action for s in p.salaries)),
        "persons": persons,
    }

    # Payslips of the last register month's financial year that carry register
    # lines: AskMira's own TDS will read them as this year's pay.
    current_fy: list[dict[str, Any]] = []
    if p.months:
        last = p.months[-1]
        fy_start = (last.year if last.month >= 4 else last.year - 1, 4)
        for mp in p.months:
            if (mp.year, mp.month) < fy_start:
                continue
            for slip in mp.payslips:
                if slip.input_lines:
                    current_fy.append(
                        {
                            "month": mp.key,
                            "code": slip.person.code,
                            "lines": [
                                {
                                    "code": line["code"],
                                    "amount": line["amount"],
                                    "taxable_amount": line["taxable_amount"],
                                }
                                for line in slip.input_lines
                            ],
                        }
                    )

    categories: dict[str, dict[str, int]] = {}
    for issue in p.issues:
        categories.setdefault(issue.level, {})
        categories[issue.level][issue.category] = categories[issue.level].get(issue.category, 0) + 1

    return {
        "system": SYSTEM,
        "applied": p.applied,
        "dry_run": p.dry_run,
        "legal_entity_id": str(p.legal_entity_id) if p.legal_entity_id else None,
        "legal_entity_name": p.legal_entity_name,
        "files": [f.summary() for f in p.files],
        "months": months,
        "salary": salary,
        "people": p.people,
        "payslips": sum(len(mp.payslips) for mp in p.months),
        "line_counts": {code: overall.get(code, 0) for code in _DIFF_CODES},
        "current_year_input_lines": current_fy,
        "blocking_errors": p.blocking,
        "issue_counts": categories,
        "issues": [i.as_dict() for i in p.issues],
        "run_ids": [str(r) for r in p.run_ids],
        "stats": dict(p.stats),
    }


def issues_xlsx(p: ImportPlan) -> bytes:
    """The report as a workbook to work through: errors first, then the rest."""
    book = Workbook()
    bold = Font(bold=True)

    def sheet(title: str, headers: list[str], rows: list[list[Any]]):
        ws = book.create_sheet(title)
        ws.append(headers)
        for cell in ws[1]:
            cell.font = bold
        for row in rows:
            ws.append(row)
        ws.freeze_panes = "A2"
        return ws

    readme = book.active
    readme.title = "Read me"
    report = preview(p)
    lines = [
        ["factoHR payroll history: import check"],
        [f"Company: {p.legal_entity_name}"],
        [f"Checked: {p.planned_at.isoformat(timespec='seconds')}"],
        [f"Files: {len(p.files)}; months: {len(p.months)}; payslips: {report['payslips']}"],
        [f"Errors (nothing is imported while any remains): {p.blocking}"],
        ["Errors: what must be fixed. Warnings: imported, but look at them."],
        ["Notes: for the record."],
        ["Salary changes: each person's revisions as they will be after the import."],
        ["Months: the computed totals against the register's own totals row."],
    ]
    for line in lines:
        readme.append(line)
    readme["A1"].font = Font(bold=True, size=13)

    columns = ["Category", "Month", "File", "Sheet", "Row", "Code", "Message"]
    for level, title in (("error", "Errors"), ("warning", "Warnings"), ("note", "Notes")):
        sheet(
            title,
            columns,
            [
                [i.category, i.month, i.file, i.sheet, i.row, i.code, i.message]
                for i in p.issues
                if i.level == level
            ],
        )

    salary_rows: list[list[Any]] = []
    for s in p.salaries:
        if s.live is not None:
            live_action = (
                "deleted (replaced by the registers)"
                if s.delete_live
                else f"dated from {s.live_new_from.isoformat()}"
                if s.live_new_from
                else "kept as it is"
            )
            salary_rows.append(
                [
                    s.person.code,
                    s.person.name,
                    s.action,
                    "current salary",
                    s.live.effective_from.isoformat(),
                    None,
                    _s(s.live.ctc_monthly),
                    None,
                    None,
                    None,
                    None,
                    None,
                    live_action,
                ]
            )
        for seg in s.segments:
            salary_rows.append(
                [
                    s.person.code,
                    s.person.name,
                    s.action,
                    "current salary (dated)" if seg.record == "live" else "new revision",
                    seg.start.isoformat(),
                    seg.end.isoformat() if seg.end else None,
                    *(_s(v) for v in seg.rate),
                    f"{seg.months[0]} to {seg.months[-1]}; " + "; ".join(seg.notes),
                ]
            )
    sheet(
        "Salary changes",
        [
            "Code",
            "Name",
            "Action",
            "Revision",
            "From",
            "To",
            "CTC a month",
            "Basic",
            "HRA",
            "Special",
            "Bonus",
            "Gross",
            "Notes",
        ],
        salary_rows,
    )

    month_rows: list[list[Any]] = []
    for m in report["months"]:
        diffs = m["differences"]
        month_rows.append(
            [
                m["label"],
                ", ".join(m["files"]),
                m["rows_read"],
                m["payslips"],
                m["combined_rows"],
                m["zero_rows"],
                m["totals"]["gross"],
                m["totals"]["deductions"],
                m["totals"]["net"],
                m["totals"]["employer_cost"],
                diffs.get("earned_gross", {}).get("in_register"),
                diffs.get("net_salary", {}).get("in_register"),
                (diffs.get("bank_transfer_rounded") or diffs.get("bank_transfer") or {}).get(
                    "in_register"
                ),
                (diffs.get("bank_transfer_rounded") or diffs.get("bank_transfer") or {}).get(
                    "difference"
                ),
                "refused" if m["refused"] else "",
            ]
        )
    sheet(
        "Months",
        [
            "Month",
            "Files",
            "Rows read",
            "Payslips",
            "Rows combined",
            "Zero rows",
            "Gross",
            "Deductions",
            "Net paid",
            "Employer cost",
            "Register gross (totals row)",
            "Register net (totals row)",
            "Register bank (totals row)",
            "Totals row - rows read (bank)",
            "Refused",
        ],
        month_rows,
    )
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()

