"""Salaries in bulk from the Employee Salary Master sheet: a template, a check, then all at once.

The sheet is laid out the way HR already keeps it (the "EmployeeSalaryMaster"
template of the HRMS the user moved from): Employee Code, WEF, Pay Basis,
Replace all future salary revisions, CTC TOTAL, MONTHLY GROSS, then one column
per wage type. A file exported from that system uploads without editing.

How a row is read:

* Pay Basis (Yearly, Monthly, Weekly, Daily or Hourly) applies to every amount
  on the row.
* A row with amounts is taken exactly as typed, the way the Salary master's
  editor saves: each wage type given is paid at that amount, one left empty is
  not paid, and CTC TOTAL is kept as given. The CTC also holds employer PF, ESI
  and admin charges, which payroll adds on top, so the wage types need not add
  up to it; the difference is shown on the row.
* A row with only CTC TOTAL is split by the salary structure.
* The statutory MANUAL columns (TDS, PROF. TAX, MPF, ESIC, LWF) are deducted
  instead of what payroll would work out, for that person; empty, payroll works
  them out as usual.
* MONTHLY GROSS is a check: a different gross is noted on the row.
* "Replace all future salary revisions" set to Yes removes the person's
  revisions from WEF on (only ones payroll has not used) and puts this one in
  their place. Otherwise WEF must be after the latest revision.

An upload is checked first and nothing is saved. Saving takes every ready row
in one transaction, and only when no row has an error.
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request, status
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.payroll import (
    ComponentType,
    EmployeeSalary,
    SalaryComponent,
    SalaryStructure,
    WageGroup,
    wage_group_of,
)
from app.models.user import User
from app.schemas.payroll import (
    EmployeeSalaryCreate,
    SalaryComponentAmountInput,
    SalaryImportRead,
    SalaryImportRowRead,
)
from app.services import audit
from app.services.employee_import.fields import read_date, text
from app.services.payroll.salary_master import revision_locks, revision_snapshot
from app.services.payroll.service import (
    assign_salary,
    get_structure,
    resolve_amounts,
    to_period_amounts,
)
from app.services.storage import StorageError, get_storage

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000

# ── The columns ─────────────────────────────────────────────────────────────

CODE = "Employee Code"
WEF = "WEF"
BASIS = "Pay Basis"
REPLACE = "Replace all future salary revisions"
CTC = "CTC TOTAL"
GROSS = "MONTHLY GROSS"
STRUCTURE = "Salary Structure"
REASON = "Reason"
NAME = "Employee Name"

#: The Employee Salary Master layout, column for column. Wage-type headers are
#: matched to the workspace's own wage types by name (see `_OLD_NAMES`).
LAYOUT: tuple[str, ...] = (
    CODE, WEF, BASIS, REPLACE, CTC, GROSS,
    "GRATUITY AMOUNT MANUAL", "PROF TAX CTC",
    "BASIC SALARY", "HOUSE RENT ALLOWANCE", "CONVEYANCE ALLOWANCE", "SPECIAL ALLOWANCE",
    "EDUCATION ALLOWANCE", "BONUS", "LEAVE ENCASHMENT", "MEDICAL REIMBURSEMENT",
    "FUEL REIMBURSEMENT", "BONUS PROVISION", "GRATUITY PROVISION",
    "TDS MANUAL", "PROF. TAX MANUAL", "MPF CONTRIBUTION MANUAL",
    "ESIC EMPLOYEE CONTRIBUTION MANUAL", "LWF MANUAL",
    "INSURANCE", "Uniform Allowance", "Telephone Allowance",
)  # fmt: skip


def _key(header: Any) -> str:
    """A header compared loosely: case, punctuation and spacing do not matter."""
    return re.sub(r"[^a-z0-9]+", " ", str(header or "").lower()).strip()


#: Other ways the fixed columns are written.
_FIXED: dict[str, str] = {
    "employee code": CODE, "emp code": CODE, "empcode": CODE, "code": CODE,
    "employee no": CODE, "emp no": CODE,
    "wef": WEF, "w e f": WEF, "with effect from": WEF, "effective from": WEF,
    "effective date": WEF,
    "pay basis": BASIS, "basis": BASIS,
    "replace all future salary revisions": REPLACE, "replace future revisions": REPLACE,
    "replace future salary revisions": REPLACE,
    "ctc total": CTC, "ctc": CTC, "total ctc": CTC,
    "monthly gross": GROSS, "gross": GROSS, "gross salary": GROSS,
    "salary structure": STRUCTURE, "structure": STRUCTURE,
    "reason": REASON, "remarks": REASON,
    "employee name": NAME, "name": NAME,
}  # fmt: skip

#: The old system's wage-type names, mapped to AskMira's standard codes.
_OLD_NAMES: dict[str, str] = {
    "basic salary": "BASIC", "basic": "BASIC",
    "house rent allowance": "HRA", "hra": "HRA",
    "conveyance allowance": "CONVEYANCE", "special allowance": "SPECIAL",
    "education allowance": "EDUCATION", "bonus": "BONUS",
    "leave encashment": "LEAVE_ENCASH", "medical reimbursement": "MEDICAL",
    "fuel reimbursement": "FUEL", "bonus provision": "BONUS_PROVISION",
    "gratuity amount manual": "GRATUITY", "gratuity": "GRATUITY",
    "gratuity provision": "GRATUITY_PROVISION", "insurance": "INSURANCE",
    "uniform allowance": "UNIFORM", "telephone allowance": "TELEPHONE",
    "tds manual": "TDS_MANUAL", "prof tax manual": "PT_MANUAL",
    "professional tax manual": "PT_MANUAL", "mpf contribution manual": "PF_MANUAL",
    "pf contribution manual": "PF_MANUAL", "esic employee contribution manual": "ESIC_MANUAL",
    "lwf manual": "LWF_MANUAL", "prof tax ctc": "PT_CTC",
}  # fmt: skip

#: Template columns a workspace set up before they were added lacks.
_TEMPLATE_ONLY = frozenset(
    key for key, code in _OLD_NAMES.items()
    if code in {"TDS_MANUAL", "PT_MANUAL", "PF_MANUAL", "ESIC_MANUAL", "LWF_MANUAL", "PT_CTC",
                "GRATUITY_PROVISION"}
)  # fmt: skip

_HEADER_FILL = PatternFill("solid", fgColor="14595C")  # Deep Teal
_REQUIRED_FILL = PatternFill("solid", fgColor="EFC259")  # Marigold
_INFO_FILL = PatternFill("solid", fgColor="E4E0D8")  # Cream, darker: not read
_GROUP_ORDER = {group: index for index, group in enumerate(WageGroup)}


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def _components(session: AsyncSession, user: User) -> list[SalaryComponent]:
    rows = (
        await session.scalars(tenant_scoped(select(SalaryComponent), SalaryComponent, user))
    ).all()
    return sorted(
        rows,
        key=lambda c: (
            _GROUP_ORDER[wage_group_of(c.component_type, c.wage_group)],
            c.display_order,
            c.code,
        ),
    )


def _wage_code(header: Any, components: list[SalaryComponent]) -> str | None:
    """The workspace wage type a header names: by its name, its code, or the old name."""
    key = _key(header)
    by_name = {_key(c.name): c.code for c in components}
    codes = {c.code for c in components}
    if key in by_name:
        return by_name[key]
    as_code = key.upper().replace(" ", "_")
    if as_code in codes:
        return as_code
    legacy = re.search(r"\[([A-Z][A-Z0-9_]*)\]", str(header or ""))
    if legacy and legacy.group(1) in codes:
        return legacy.group(1)
    old = _OLD_NAMES.get(key)
    return old if old in codes else None


# ── The template ────────────────────────────────────────────────────────────


async def template(session: AsyncSession, user: User) -> bytes:
    """The Employee Salary Master sheet, with this workspace's people listed."""
    components = await _components(session, user)
    active = [c for c in components if c.active]
    structures = (
        await session.scalars(
            tenant_scoped(
                select(SalaryStructure)
                .where(SalaryStructure.active.is_(True))
                .order_by(SalaryStructure.name),
                SalaryStructure,
                user,
            )
        )
    ).all()
    people = (
        await session.scalars(
            tenant_scoped(
                select(Employee)
                .where(Employee.deleted_at.is_(None), Employee.status != EmploymentStatus.EXITED)
                .order_by(Employee.employee_code),
                Employee,
                user,
            )
        )
    ).all()
    current: dict[uuid.UUID, EmployeeSalary] = {}
    if people:
        for salary in (
            await session.scalars(
                tenant_scoped(
                    select(EmployeeSalary).where(
                        EmployeeSalary.employee_id.in_([p.id for p in people]),
                        EmployeeSalary.effective_to.is_(None),
                    ),
                    EmployeeSalary,
                    user,
                )
            )
        ).all():
            known = current.get(salary.employee_id)
            if known is None or salary.effective_from > known.effective_from:
                current[salary.employee_id] = salary

    covered = {_wage_code(h, active) for h in LAYOUT} - {None}
    extras = [c.name.upper() for c in active if c.code not in covered]
    headers = [*LAYOUT, *extras, STRUCTURE, REASON, NAME]
    required = {CODE, WEF, BASIS, CTC}
    not_read = {_key(NAME)}

    book = Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    sheet.append(headers)
    for index, header in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=index)
        if header in required:
            cell.fill, cell.font = _REQUIRED_FILL, Font(bold=True, color="12161D")
        elif _key(header) in not_read:
            cell.fill, cell.font = _INFO_FILL, Font(bold=True, color="55524B")
        elif _OLD_NAMES.get(_key(header), "").endswith("_MANUAL"):
            cell.fill, cell.font = _HEADER_FILL, Font(bold=True, color="FFFFFF")
            cell.comment = Comment(
                "Deducted instead of what payroll works out, for this person. "
                "Leave empty to let payroll work it out.",
                "AskMira",
            )
        else:
            cell.fill, cell.font = _HEADER_FILL, Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = max(12, min(len(header) + 3, 28))
    sheet.row_dimensions[1].height = 42
    sheet.freeze_panes = "C2"

    for person in people:
        salary = current.get(person.id)
        row = [None] * len(headers)
        row[0] = person.employee_code
        if salary and salary.structure and salary.structure.active:
            row[headers.index(STRUCTURE)] = salary.structure.name
        row[headers.index(NAME)] = person.full_name
        sheet.append(row)

    last = max(len(people) + 1, 2) + 500
    for row in range(2, last + 1):
        sheet.cell(row=row, column=headers.index(WEF) + 1).number_format = "DD-MMM-YYYY"

    def choices(column: str, values: list[str]) -> None:
        listed = ",".join(v.replace(",", " ") for v in values)
        if not values or len(listed) >= 250:
            return
        validation = DataValidation(
            type="list", formula1=f'"{listed}"', allow_blank=True, showErrorMessage=False
        )
        letter = get_column_letter(headers.index(column) + 1)
        validation.add(f"{letter}2:{letter}{last}")
        sheet.add_data_validation(validation)

    choices(BASIS, ["Yearly", "Monthly", "Weekly", "Daily", "Hourly"])
    choices(REPLACE, ["Yes", "No"])
    choices(STRUCTURE, [s.name for s in structures])

    sample = book.create_sheet("Sample")
    sample.append(headers)
    example = {
        CODE: "EMP001", WEF: date(2026, 4, 1), BASIS: "Monthly", REPLACE: "No",
        CTC: Decimal("51666.67"), "BASIC SALARY": 25833, "HOUSE RENT ALLOWANCE": 10334,
        "GRATUITY AMOUNT MANUAL": 1242, "INSURANCE": 239, REASON: "Annual increment",
    }  # fmt: skip
    sample.append([example.get(h) for h in headers])
    sample.cell(row=2, column=headers.index(WEF) + 1).number_format = "DD-MMM-YYYY"
    for index, header in enumerate(headers, start=1):
        sample.cell(row=1, column=index).font = Font(bold=True)
        sample.column_dimensions[get_column_letter(index)].width = max(12, min(len(header) + 3, 28))

    notes = book.create_sheet("Instructions")
    lines = [
        "Employee Salary Master: how to fill Sheet1",
        "",
        "One row is one salary revision. Rows without CTC TOTAL or any amount are skipped, so",
        "the listed people can stay: fill only the rows you want to change.",
        "",
        f"{CODE} (required): as in AskMira.",
        f"{WEF} (required): the date the salary applies from, e.g. 01-Apr-2026.",
        f"{BASIS} (required): Yearly, Monthly, Weekly, Daily or Hourly. Every amount on the",
        "    row is read in it (a week is 1/52 of a year; a month has 26 working days of",
        "    8 hours).",
        f"{REPLACE}: Yes removes the person's revisions from {WEF} on and puts this one in their",
        f"    place (only ones payroll has not used). No or empty: {WEF} must be after the latest",
        "    revision.",
        f"{CTC}: the CTC, kept as given.",
        "Wage-type columns: the amounts, in the row's Pay Basis. A row with amounts is saved",
        "    exactly as typed; a wage type left empty is not paid. A row with only CTC TOTAL",
        "    is split by the salary structure.",
        "TDS / PROF. TAX / MPF / ESIC / LWF MANUAL: deducted instead of what payroll works out;",
        "    leave empty to let payroll work it out.",
        f"{GROSS}: a check. A different gross is shown on the row.",
        f"{STRUCTURE}: empty keeps the person's present structure (or the workspace's only one).",
        f"{REASON}: why the salary changes. {NAME} is for reference and is not read.",
        "",
        "Upload on Payroll > Salary master > Bulk upload. Every row is checked and shown first;",
        "nothing is saved until you confirm, and nothing at all while any row has an error.",
        "",
        "Structures in this workspace: " + (", ".join(s.name for s in structures) or "none yet"),
        "See the Sample sheet for a filled row.",
    ]
    for line in lines:
        notes.append([line])
    notes["A1"].font = Font(bold=True, size=13)
    notes.column_dimensions["A"].width = 100

    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


# ── Reading an upload ───────────────────────────────────────────────────────


def _rows(data: bytes, filename: str) -> list[tuple[int, list[Any]]]:
    """Every row of the sheet to read, numbered as Excel does.

    The first sheet with an Employee Code header, passing over a Sample or an
    Instructions sheet.
    """
    if len(data) > MAX_BYTES:
        raise _unprocessable("The file is larger than 5 MB.")
    if filename.lower().endswith(".csv"):
        try:
            decoded = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = data.decode("latin-1")
        return list(enumerate(csv.reader(io.StringIO(decoded)), start=1))
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many kinds for a bad file
        raise _unprocessable("This is not an Excel (.xlsx) or CSV file AskMira can read.") from exc
    for sheet in book.worksheets:
        if _key(sheet.title) in {"sample", "instructions"}:
            continue
        rows = [
            (number, list(values))
            for number, values in enumerate(sheet.iter_rows(values_only=True), start=1)
        ]
        if any(_FIXED.get(_key(v)) == CODE for _, values in rows[:15] for v in values):
            return rows
    raise _unprocessable(f"No sheet has an '{CODE}' column. Start from the template.")


def _money(raw: Any, what: str) -> Decimal | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{what}: '{raw}' is not an amount")
    cleaned = str(raw).strip().replace(",", "").replace("₹", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"{what}: '{raw}' is not an amount") from exc
    if amount < 0:
        raise ValueError(f"{what} cannot be negative")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError(f"{what}: {cleaned} has more than two decimals")
    return amount


_BASES = {
    "annual": {"yearly", "annual", "annually", "year", "per annum", "pa"},
    "monthly": {"monthly", "month", "pm", "per month"},
    "weekly": {"weekly", "week", "per week"},
    "daily": {"daily", "day", "per day"},
    "hourly": {"hourly", "hour", "per hour"},
}


def _basis(raw: Any) -> str | None:
    value = _key(raw)
    if not value:
        return None
    for basis, words in _BASES.items():
        if value in words:
            return basis
    raise ValueError(f"{BASIS} '{raw}': use Yearly, Monthly, Weekly, Daily or Hourly")


def _yes(raw: Any) -> bool:
    return _key(raw) in {"yes", "y", "true", "1"}


def _rupees(amount: Decimal) -> str:
    whole, _, paise = f"{amount:.2f}".partition(".")
    grouped = whole if len(whole) <= 3 else f"{_indian(whole[:-3])},{whole[-3:]}"
    return f"₹{grouped}" + ("" if paise == "00" else f".{paise}")


def _indian(digits: str) -> str:
    parts = []
    while len(digits) > 2:
        parts.insert(0, digits[-2:])
        digits = digits[:-2]
    if digits:
        parts.insert(0, digits)
    return ",".join(parts)


@dataclass
class _Plan:
    employee: Employee
    payload: EmployeeSalaryCreate
    replace: bool


@dataclass
class _Outcome:
    rows: list[SalaryImportRowRead] = field(default_factory=list)
    plans: list[_Plan] = field(default_factory=list)


async def _check(session: AsyncSession, user: User, data: bytes, filename: str) -> _Outcome:
    table = _rows(data, filename)
    header_at = next(
        (
            index
            for index, (_, values) in enumerate(table[:15])
            if any(_FIXED.get(_key(v)) == CODE for v in values)
        ),
        None,
    )
    if header_at is None:
        raise _unprocessable(f"No '{CODE}' column found. Start from the template.")
    header_values = table[header_at][1]
    body = table[header_at + 1 :]
    if len(body) > MAX_ROWS:
        raise _unprocessable(f"A sheet can hold up to {MAX_ROWS} rows.")

    components = await _components(session, user)
    by_code = {c.code: c for c in components}
    fixed: dict[str, int] = {}
    wage_columns: list[tuple[int, str, str]] = []  # (index, header, code)
    unknown_columns: list[tuple[int, str]] = []
    for index, header in enumerate(header_values):
        key = _key(header)
        if not key:
            continue
        if key in _FIXED and _FIXED[key] not in fixed:
            fixed[_FIXED[key]] = index
        elif (code := _wage_code(header, components)) is not None:
            wage_columns.append((index, str(header), code))
        else:
            unknown_columns.append((index, str(header)))

    def cell(values: list[Any], name: str) -> Any:
        index = fixed.get(name)
        return values[index] if index is not None and index < len(values) else None

    def at(values: list[Any], index: int) -> Any:
        return values[index] if index < len(values) else None

    codes = {code for _, values in body if (code := text(cell(values, CODE))) is not None}
    employees: dict[str, Employee] = {}
    if codes:
        employees = {
            e.employee_code.upper(): e
            for e in (
                await session.scalars(
                    tenant_scoped(
                        select(Employee).where(
                            Employee.deleted_at.is_(None),
                            func.upper(Employee.employee_code).in_([c.upper() for c in codes]),
                        ),
                        Employee,
                        user,
                    )
                )
            ).all()
        }
    structures = {
        _key(s.name): s
        for s in (
            await session.scalars(
                tenant_scoped(
                    select(SalaryStructure).where(SalaryStructure.active.is_(True)),
                    SalaryStructure,
                    user,
                )
            )
        ).all()
    }

    # Each person's revisions as dates, so rows can be checked in order, with
    # the ones payroll has used marked: those can never be replaced.
    series: dict[uuid.UUID, list[EmployeeSalary]] = {}
    if employees:
        for salary in (
            await session.scalars(
                tenant_scoped(
                    select(EmployeeSalary)
                    .where(EmployeeSalary.employee_id.in_([e.id for e in employees.values()]))
                    .order_by(EmployeeSalary.effective_from),
                    EmployeeSalary,
                    user,
                )
            )
        ).all():
            series.setdefault(salary.employee_id, []).append(salary)
    locks = await revision_locks(session, user, [s.id for ss in series.values() for s in ss])
    dates: dict[uuid.UUID, list[date]] = {
        k: [s.effective_from for s in v] for k, v in series.items()
    }
    locked: dict[uuid.UUID, set[date]] = {
        k: {s.effective_from for s in v if s.id in locks} for k, v in series.items()
    }

    outcome = _Outcome()
    for number, values in body:
        if all(text(v) is None for v in values):
            continue
        code = text(cell(values, CODE))
        employee = employees.get(code.upper()) if code else None
        row = SalaryImportRowRead(
            row=number,
            employee_code=code,
            employee_name=employee.full_name if employee else text(cell(values, NAME)),
            effective_from=None,
            structure_name=None,
            ctc_annual=None,
            gross_monthly=None,
            status="error",
        )
        outcome.rows.append(row)
        notes: list[str] = []
        try:
            ctc = _money(cell(values, CTC), CTC)
            given: dict[str, tuple[str, Decimal]] = {}
            for index, header, wage_code in wage_columns:
                amount = _money(at(values, index), header)
                if amount is None:
                    continue
                if wage_code in given and given[wage_code][1] != amount:
                    raise ValueError(
                        f"{given[wage_code][0]} and {header} are both {by_code[wage_code].name} "
                        "but hold different amounts"
                    )
                given[wage_code] = (header, amount)
            for index, header in unknown_columns:
                if text(at(values, index)) is not None:
                    if _key(header) in _TEMPLATE_ONLY:
                        raise ValueError(
                            f"'{header}' is not set up in this workspace yet. Press "
                            "'Update wage types' on the Salary master, then upload again."
                        )
                    raise ValueError(
                        f"'{header}' is not a wage type of this workspace. Add it under "
                        "Payroll setup → Salary structures, or clear the column."
                    )
            if ctc is None and not given:
                row.status, row.message = "skipped", "No CTC or amount on this row: left as it is."
                continue
            if code is None:
                raise ValueError(f"{CODE} is empty")
            if employee is None:
                raise ValueError(f"No employee with code {code}")
            basis = _basis(cell(values, BASIS))
            if basis is None:
                raise ValueError(f"{BASIS} is empty: Yearly, Monthly, Weekly, Daily or Hourly")
            on = read_date(cell(values, WEF))
            if on is None:
                raise ValueError(f"{WEF} is empty")
            row.effective_from = on

            # ── Where the revision goes ─────────────────────────────────────
            replace = _yes(cell(values, REPLACE))
            person_dates = dates.setdefault(employee.id, [])
            if replace:
                later = sorted(d for d in person_dates if d >= on)
                stuck = [d for d in later if d in locked.get(employee.id, set())]
                if stuck:
                    raise ValueError(
                        f"The revision from {stuck[0]:%d-%b-%Y} cannot be replaced: payroll "
                        "has been approved on it"
                    )
                if later:
                    notes.append(f"Replaces {len(later)} revision(s) from {later[0]:%d-%b-%Y} on")
            elif person_dates and on <= max(person_dates):
                raise ValueError(
                    f"{WEF} must be after the latest revision ({max(person_dates):%d-%b-%Y}). "
                    f"Put Yes in '{REPLACE}' to replace revisions from that date on."
                )

            # ── Which structure ─────────────────────────────────────────────
            named = text(cell(values, STRUCTURE))
            if named:
                structure = structures.get(_key(named))
                if structure is None:
                    raise ValueError(f"No active salary structure called '{named}'")
            else:
                history = series.get(employee.id, [])
                present = (
                    await get_structure(session, user, history[-1].structure_id)
                    if history
                    else None
                )
                if present is not None and present.active:
                    structure = present
                elif len(structures) == 1:
                    structure = next(iter(structures.values()))
                else:
                    raise ValueError(f"{STRUCTURE} is empty: name the salary structure to use")
            row.structure_name = structure.name

            # ── The amounts ─────────────────────────────────────────────────
            for wage_code in given:
                component = by_code[wage_code]
                if not component.active:
                    raise ValueError(f"{component.name} is switched off in payroll setup")
            reason = (text(cell(values, REASON)) or "Bulk upload")[:255]
            if given:
                # Amounts given: saved exactly as typed, like the Salary master editor.
                if ctc is not None:
                    ctc_fields: dict[str, Any] = {"ctc_amount": ctc, "ctc_basis": basis}
                else:
                    in_ctc = sum(
                        (
                            to_period_amounts(amount, basis)[0]
                            for c, (_, amount) in given.items()
                            if by_code[c].part_of_ctc
                        ),
                        Decimal(0),
                    )
                    if in_ctc <= 0:
                        raise ValueError(f"{CTC} is empty")
                    ctc_fields = {"ctc_annual": in_ctc}
                    notes.append(f"{CTC} empty: set to the wage types' total, {_rupees(in_ctc)}")
                payload = EmployeeSalaryCreate(
                    entry="manual",
                    structure_id=structure.id,
                    effective_from=on,
                    revision_reason=reason,
                    components=[
                        SalaryComponentAmountInput(component_code=c, amount=amount, basis=basis)
                        for c, (_, amount) in given.items()
                    ],
                    **ctc_fields,
                )
            else:
                # Only a CTC: the structure splits it.
                assert ctc is not None
                payload = EmployeeSalaryCreate(
                    structure_id=structure.id,
                    effective_from=on,
                    revision_reason=reason,
                    ctc_amount=ctc,
                    ctc_basis=basis,
                )
            resolved = await resolve_amounts(session, user, structure, payload)
        except ValidationError as exc:
            row.message = exc.errors()[0].get("msg", "Invalid row").removeprefix("Value error, ")
            continue
        except HTTPException as exc:
            row.message = str(exc.detail)
            continue
        except ValueError as exc:
            row.message = str(exc)
            continue

        earnings = [
            c for c in resolved.breakdown.components if c.component_type is ComponentType.EARNING
        ]
        gross_monthly = sum((c.monthly_amount for c in earnings), Decimal(0))
        gross_given = None
        with contextlib.suppress(ValueError):
            gross_given = _money(cell(values, GROSS), GROSS)
        if gross_given is not None:
            gross_in_basis = (
                gross_monthly
                if basis == "monthly"
                else sum((c.annual_amount for c in earnings), Decimal(0))
            )
            if gross_given != gross_in_basis:
                notes.append(
                    f"{GROSS} in the sheet is {_rupees(gross_given)}; it works out to "
                    f"{_rupees(gross_in_basis)}"
                )
        if payload.entry == "manual":
            in_ctc = sum(
                (c.annual_amount for c in resolved.breakdown.components if c.part_of_ctc),
                Decimal(0),
            )
            gap = resolved.ctc_annual - in_ctc
            if gap > 0:
                notes.append(
                    f"{CTC} {_rupees(resolved.ctc_annual)} a year; the wage types in it add up "
                    f"to {_rupees(in_ctc)}. {_rupees(gap)} is outside them (employer PF, ESI "
                    "and admin charges, which payroll adds)"
                )
            elif gap < 0:
                notes.append(
                    f"The wage types in CTC add up to {_rupees(in_ctc)} a year, "
                    f"{_rupees(-gap)} more than {CTC} {_rupees(resolved.ctc_annual)}"
                )
        row.status = "ready"
        row.ctc_annual = resolved.ctc_annual
        row.gross_monthly = gross_monthly
        row.typed = len(given)
        row.notes = notes
        if replace:
            dates[employee.id] = [d for d in dates[employee.id] if d < on] + [on]
        else:
            dates[employee.id].append(on)
        outcome.plans.append(_Plan(employee=employee, payload=payload, replace=replace))
    return outcome


async def _replace_from(
    session: AsyncSession,
    user: User,
    employee: Employee,
    on: date,
    request: Request | None,
) -> list[str]:
    """Remove a person's revisions from `on` (none payroll has used), audited.

    The revision before `on` is reopened, so the upload's revision follows it.
    Returns the storage keys of files the removed revisions carried.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeSalary)
                .where(EmployeeSalary.employee_id == employee.id)
                .order_by(EmployeeSalary.effective_from),
                EmployeeSalary,
                user,
            )
        )
    ).all()
    removing = [s for s in rows if s.effective_from >= on]
    if not removing:
        return []
    locks = await revision_locks(session, user, [s.id for s in removing])
    if locks:
        raise _unprocessable(
            f"A revision of {employee.employee_code} was paid by payroll meanwhile; "
            "nothing was saved."
        )
    keys: list[str] = []
    for salary in removing:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.DELETE,
            entity_type="employee_salary",
            entity_id=salary.id,
            actor=user,
            changes={
                "employee_id": str(employee.id),
                "deleted": revision_snapshot(
                    salary, salary.structure.name if salary.structure else None
                ),
                "replaced_by_upload": True,
            },
            summary=(
                f"Salary revision from {salary.effective_from:%d %b %Y} replaced by a bulk "
                f"upload for {employee.employee_code}"
            ),
            request=request,
        )
        if salary.attachment_key:
            keys.append(salary.attachment_key)
        await session.delete(salary)
    await session.flush()
    before = [s for s in rows if s.effective_from < on]
    if before:
        before[-1].effective_to = None
        await session.flush()
    return keys


async def upload(
    session: AsyncSession,
    user: User,
    data: bytes,
    filename: str,
    *,
    apply: bool,
    request: Request | None = None,
) -> SalaryImportRead:
    """Check a filled sheet; with `apply`, save every ready row in one transaction."""
    outcome = await _check(session, user, data, filename)
    errors = sum(1 for r in outcome.rows if r.status == "error")
    result = SalaryImportRead(
        filename=filename,
        rows=outcome.rows,
        ready=len(outcome.plans),
        errors=errors,
        skipped=sum(1 for r in outcome.rows if r.status == "skipped"),
        noted=sum(1 for r in outcome.rows if r.status == "ready" and r.notes),
    )
    if not apply:
        return result
    if errors:
        raise _unprocessable(f"Fix the {errors} row(s) marked as errors first; nothing was saved.")
    if not outcome.plans:
        raise _unprocessable("No row has a CTC or an amount, so there is nothing to save.")

    stale: list[str] = []
    for plan in outcome.plans:
        if plan.replace:
            stale += await _replace_from(
                session, user, plan.employee, plan.payload.effective_from, request
            )
        await assign_salary(
            session, user, plan.employee.id, plan.payload, request=request, commit=False
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="salary_import",
        actor=user,
        changes={
            "filename": filename,
            "revisions": len(outcome.plans),
            "employees": sorted({p.employee.employee_code for p in outcome.plans}),
        },
        summary=f"Bulk salary upload: {len(outcome.plans)} revision(s) from {filename}",
        request=request,
    )
    await session.commit()
    for key in stale:
        # The rows are gone; a file left behind is only storage, never data.
        with contextlib.suppress(StorageError):
            await get_storage().delete(key)
    result.applied = True
    return result
