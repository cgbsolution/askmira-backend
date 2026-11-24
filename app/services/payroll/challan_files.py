"""Challan upload files from a locked payroll run, for the people HR picks.

After a run is approved and locked, HR downloads the month's PF and ESI upload
files from the run page:

* **ESIC MC template** — the portal's MC_Template.xls (Excel 97-2003): IP
  number, name, days paid, wages, and for an IP with no days a reason code and
  last working day.
* **PF ECR upload CSV** — the "CHALLAN UPLOAD" sheet: the ECR's eleven fields
  under a header row.
* **PF ECR text** — the EPFO's ``#~#`` file, ready to upload as is.

The figures are the month's, for the run's company: the regular run and any
locked top-up run of the month, merged per person exactly as the Filings page
merges them (`filings.service.month_pay`, `ecr_wage_rows`, `esic_month_lines`),
so this file and the filed return can never disagree.

Filters narrow who is in the file: department, location, project, designation,
grade, employment type, status, joiners or leavers of the month, and named
people. Someone owed a contribution whose UAN or IP number is missing or
malformed is left out — a blank or bad number makes the portal reject the whole
upload — and named in the preview so HR can fix the record first.
"""

from __future__ import annotations

import calendar
import re
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus, EmploymentType
from app.models.org import Department, LegalEntity, Location
from app.models.payroll import PayrollRun
from app.models.timesheets import Project
from app.models.user import User
from app.schemas.challan_files import (
    ChallanFilters,
    ChallanIssue,
    ChallanKind,
    ChallanOptions,
    ChallanPerson,
    ChallanPreview,
    ChallanTotal,
    FilterChoice,
)
from app.services import audit
from app.services.filings import serialisers, validation
from app.services.filings import service as filing_service
from app.services.filings.records import EcrLine, EcrReturn, EsicLine, EsicReturn
from app.services.payroll.run import CLOSED_STATUSES, get_run, period_label

_ZERO = Decimal(0)

KIND_LABEL: dict[str, str] = {
    "esic_mc": "ESIC MC template (.xls)",
    "pf_ecr_csv": "PF ECR upload (.csv)",
    "pf_ecr_txt": "PF ECR text file (.txt)",
}

STATUS_LABEL: dict[EmploymentStatus, str] = {
    EmploymentStatus.ACTIVE: "Active",
    EmploymentStatus.ON_NOTICE: "On notice",
    EmploymentStatus.ON_LEAVE: "On leave",
    EmploymentStatus.EXITED: "Exited",
    EmploymentStatus.PRE_JOINING: "Pre-joining",
}
TYPE_LABEL: dict[EmploymentType, str] = {
    EmploymentType.FULL_TIME: "Full time",
    EmploymentType.PART_TIME: "Part time",
    EmploymentType.CONTRACT: "Contract",
    EmploymentType.INTERN: "Intern",
    EmploymentType.CONSULTANT: "Consultant",
}

#: The MC template's IP Name column: "Only alphabets and space".
_MC_NAME = re.compile(r"^[A-Za-z ]+$")
#: ESIC reason codes that need a last working day (the template's rule 5).
_LEFT_SERVICE = 2
_WITHOUT_REASON = 0
#: ECR validation codes that make the portal reject the whole file: such a line
#: is left out rather than written.
_ECR_REJECTS = frozenset({"UAN_MISSING", "UAN_MALFORMED", "UAN_DUPLICATE", "NAME_MISSING"})
_ESIC_REJECTS = frozenset({"IP_MISSING", "IP_MALFORMED"})


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else _ZERO


def _whole(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1), rounding=ROUND_HALF_UP)


def _month_bounds(run: PayrollRun) -> tuple[date, date]:
    last = calendar.monthrange(run.period_year, run.period_month)[1]
    return date(run.period_year, run.period_month, 1), date(run.period_year, run.period_month, last)


# ── The month ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Month:
    run: PayrollRun
    entity: LegalEntity | None
    rows: list[tuple[Any, Employee]]
    runs_included: int

    @property
    def label(self) -> str:
        return period_label(self.run.period_year, self.run.period_month)


async def _locked_month(session: AsyncSession, user: User, run_id: uuid.UUID) -> _Month:
    run = await get_run(session, user, run_id)
    label = period_label(run.period_year, run.period_month)
    if run.status not in CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The {label} run is not locked yet. PF and ESI challan files are made from "
                "approved figures only: check the run, then Approve & lock it."
            ),
        )
    entity = await session.scalar(
        tenant_scoped(
            select(LegalEntity).where(LegalEntity.id == run.legal_entity_id), LegalEntity, user
        )
    )
    rows = await filing_service.month_pay(session, user, run)
    runs_included = await session.scalar(
        tenant_scoped(
            select(func.count(PayrollRun.id)).where(
                PayrollRun.legal_entity_id == run.legal_entity_id,
                PayrollRun.period_year == run.period_year,
                PayrollRun.period_month == run.period_month,
                PayrollRun.status.in_(CLOSED_STATUSES),
            ),
            PayrollRun,
            user,
        )
    )
    return _Month(run=run, entity=entity, rows=rows, runs_included=int(runs_included or 0))


def _matches(employee: Employee, filters: ChallanFilters, start: date, end: date) -> bool:
    if filters.department_ids and employee.department_id not in filters.department_ids:
        return False
    if filters.location_ids and employee.location_id not in filters.location_ids:
        return False
    if filters.project_ids and employee.project_id not in filters.project_ids:
        return False
    if filters.designations and (employee.designation or "") not in filters.designations:
        return False
    if filters.grades and (employee.grade or "") not in filters.grades:
        return False
    if filters.employment_types and employee.employment_type not in filters.employment_types:
        return False
    if filters.statuses and employee.status not in filters.statuses:
        return False
    joined = employee.date_of_joining is not None and start <= employee.date_of_joining <= end
    left = employee.date_of_exit is not None and start <= employee.date_of_exit <= end
    if filters.movement == "joined" and not joined:
        return False
    if filters.movement == "left" and not left:
        return False
    if filters.movement == "continuing" and (joined or left):
        return False
    if filters.employee_ids and employee.id not in filters.employee_ids:
        return False
    return employee.id not in filters.exclude_employee_ids


def _filtered(month: _Month, filters: ChallanFilters) -> list[tuple[Any, Employee]]:
    start, end = _month_bounds(month.run)
    return [
        (pay, employee) for pay, employee in month.rows if _matches(employee, filters, start, end)
    ]


# ── Filter choices ──────────────────────────────────────────────────────────


async def _names(
    session: AsyncSession, user: User, model: Any, ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(
        tenant_scoped(select(model.id, model.name).where(model.id.in_(wanted)), model, user)
    )
    return dict(rows.all())


def _choices(counts: Counter[Any], label: Any) -> list[FilterChoice]:
    """Each value the run's people hold, most common first, then by name."""
    out = [
        FilterChoice(value=str(value), label=label(value), count=count)
        for value, count in counts.items()
        if value not in (None, "")
    ]
    return sorted(out, key=lambda choice: (-choice.count, choice.label.lower()))


async def options(session: AsyncSession, user: User, run_id: uuid.UUID) -> ChallanOptions:
    month = await _locked_month(session, user, run_id)
    employees = [employee for _, employee in month.rows]
    start, end = _month_bounds(month.run)

    departments = await _names(session, user, Department, (e.department_id for e in employees))
    locations = await _names(session, user, Location, (e.location_id for e in employees))
    projects = await _names(session, user, Project, (e.project_id for e in employees))

    return ChallanOptions(
        period_label=month.label,
        legal_entity_name=month.entity.name if month.entity else None,
        runs_included=month.runs_included,
        people=len(employees),
        departments=_choices(
            Counter(e.department_id for e in employees if e.department_id in departments),
            lambda value: departments[value],
        ),
        locations=_choices(
            Counter(e.location_id for e in employees if e.location_id in locations),
            lambda value: locations[value],
        ),
        projects=_choices(
            Counter(e.project_id for e in employees if e.project_id in projects),
            lambda value: projects[value],
        ),
        designations=_choices(Counter(e.designation for e in employees), str),
        grades=_choices(Counter(e.grade for e in employees), str),
        employment_types=_choices(
            Counter(e.employment_type for e in employees),
            lambda value: TYPE_LABEL.get(value, str(value)),
        ),
        statuses=_choices(
            Counter(e.status for e in employees),
            lambda value: STATUS_LABEL.get(value, str(value)),
        ),
        joined=sum(1 for e in employees if e.date_of_joining and start <= e.date_of_joining <= end),
        left=sum(1 for e in employees if e.date_of_exit and start <= e.date_of_exit <= end),
        employees=[
            ChallanPerson(id=e.id, employee_code=e.employee_code, name=e.full_name)
            for e in employees
        ],
    )


# ── Building a file ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Built:
    rows: int
    totals: list[ChallanTotal]
    not_covered: int
    wage_mismatches: int = 0
    left_out: list[ChallanIssue] = field(default_factory=list)
    warnings: list[ChallanIssue] = field(default_factory=list)
    content: bytes = b""


def _issue(employee: Employee, message: str) -> ChallanIssue:
    return ChallanIssue(
        employee_code=employee.employee_code, name=employee.full_name, message=message
    )


def _by_subject(
    exceptions: Iterable[validation.FilingException],
) -> dict[str, list[validation.FilingException]]:
    found: dict[str, list[validation.FilingException]] = {}
    for exception in exceptions:
        found.setdefault(exception.subject, []).append(exception)
    return found


def _wage_mismatch(line: EsicLine, esi_rules: Any) -> str | None:
    """Why the wages on this line do not explain the ESI deducted, if they do not.

    The portal works out both contributions from the wages in the file (the
    template's rule 7), so wages that disagree with what was deducted make a
    challan that disagrees with the payslips. Months imported from another
    HRMS without its "ESI Gross" column carry gross pay as the ESI wage; this
    is where that shows. Nothing is changed: the figure to file is HR's call.
    """
    if esi_rules is None or line.employee_contribution <= 0 or line.wages_paid <= 0:
        return None
    try:
        rate = Decimal(str(esi_rules.rules.get("employee_rate")))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    expected = (line.wages_paid * rate / Decimal(100)).quantize(Decimal(1), rounding=ROUND_CEILING)
    if abs(expected - line.employee_contribution) <= 1:
        return None
    implied = _whole(line.employee_contribution * Decimal(100) / rate)
    return (
        f"ESI deducted is {line.employee_contribution:,.0f}, which is {rate}% of about "
        f"{implied:,.0f}, but the ESI wages on the payslip are {_whole(line.wages_paid):,.0f}. "
        "The portal works out contributions from Total Monthly Wages, so correct that column "
        "before uploading."
    )


async def _build_esic(
    session: AsyncSession, user: User, month: _Month, rows: list[tuple[Any, Employee]]
) -> _Built:
    esi_rules = await filing_service.esi_rules_for(session, user, month.run)
    lines, who, to_validate, noted = await filing_service.esic_month_lines(
        session, user, month.run, rows, esi_rules
    )
    in_scope = {employee.employee_code for _, employee in rows}
    checked = validation.validate_esic_lines(to_validate)
    problems = _by_subject(
        [*checked.exceptions, *(note for note in noted if note.subject in in_scope)]
    )

    built = _Built(rows=0, totals=[], not_covered=0)
    kept: list[EsicLine] = []
    seen: set[str] = set()
    for line, (employee, _) in zip(lines, who, strict=True):
        own = problems.get(employee.employee_code, [])
        rejects = [p for p in own if p.code in _ESIC_REJECTS]
        if rejects:
            built.left_out.append(_issue(employee, rejects[0].message))
            continue
        if line.ip_number in seen:
            built.left_out.append(
                _issue(
                    employee,
                    f"IP number {line.ip_number} is also on someone else in this file. "
                    "One of the two records is wrong.",
                )
            )
            continue
        seen.add(line.ip_number)
        for problem in own:
            built.warnings.append(_issue(employee, problem.message))
        if not _MC_NAME.match(line.ip_name):
            built.warnings.append(
                _issue(
                    employee,
                    f"'{line.ip_name}' has characters other than letters and spaces; the MC "
                    "template's IP Name takes only those. Kept as recorded.",
                )
            )
        mismatch = _wage_mismatch(line, esi_rules)
        if mismatch:
            built.wage_mismatches += 1
            built.warnings.append(_issue(employee, mismatch))
        kept.append(line)

    # An IP on the run with no wages at all this month stays on the employer's
    # list until the file says why: 0 days, a reason code, and a last working
    # day when they have left (the template's rule 5).
    covered = {employee.id for employee, _ in who}
    start, end = _month_bounds(month.run)
    for pay, employee in rows:
        if employee.id in covered or not employee.esi_number:
            continue
        if _dec(pay.gross_earnings) or _dec(pay.paid_days):
            continue
        if not employee.esi_number.strip().isdigit() or employee.esi_number in seen:
            continue
        left = employee.date_of_exit is not None and employee.date_of_exit <= end
        kept.append(
            EsicLine(
                ip_number=employee.esi_number.strip(),
                ip_name=employee.full_name,
                days_worked=_ZERO,
                wages_paid=_ZERO,
                employee_contribution=_ZERO,
                employer_contribution=_ZERO,
                reason_code=_LEFT_SERVICE if left else _WITHOUT_REASON,
                last_working_day=employee.date_of_exit if left else None,
            )
        )
        seen.add(employee.esi_number)
        built.warnings.append(
            _issue(
                employee,
                "No wages this month: written with 0 days and reason code "
                + (
                    f"2 (Left Service), last working day {employee.date_of_exit:%d/%m/%Y}."
                    if left
                    else "0 (Without Reason). Change it on the sheet if another reason applies."
                ),
            )
        )

    esic = EsicReturn(
        employer_code=(month.entity.esi_registration_no if month.entity else None) or "",
        contribution_period="",
        wage_month=f"{month.run.period_month:02d}{month.run.period_year}",
        lines=kept,
    )
    employee_share = sum((line.employee_contribution for line in kept), _ZERO)
    employer_share = sum((line.employer_contribution for line in kept), _ZERO)
    built.rows = len(kept)
    built.not_covered = len(rows) - len(kept) - len(built.left_out)
    built.totals = [
        ChallanTotal(
            label="Total monthly wages",
            amount=sum((_whole(line.wages_paid) for line in kept), _ZERO),
        ),
        ChallanTotal(label="IP contribution (employee)", amount=employee_share),
        ChallanTotal(label="Employer contribution", amount=employer_share),
        ChallanTotal(label="Total ESI", amount=employee_share + employer_share),
    ]
    built.content = serialisers.serialise_esic_mc_xls(esic)
    return built


async def _build_ecr(
    session: AsyncSession,
    user: User,
    month: _Month,
    rows: list[tuple[Any, Employee]],
    kind: ChallanKind,
) -> _Built:
    pf_rules = await filing_service.pf_rules_for(session, user, month.run)
    errors: list[tuple[Employee, str]] = []
    computed = await filing_service.ecr_wage_rows(
        session, user, month.run, rows, pf_rules, errors=errors
    )
    order = {employee.id: index for index, (_, employee) in enumerate(rows)}
    candidates: list[tuple[int, Employee, EcrLine, Decimal | None]] = [
        (
            order[employee.id],
            employee,
            EcrLine(
                uan=(employee.uan or "").strip(),
                member_name=employee.full_name,
                gross_wages=wages.gross_wages,
                epf_wages=wages.epf_wages,
                eps_wages=wages.eps_wages,
                edli_wages=wages.edli_wages,
                employee_share=wages.employee_share,
                employer_epf_share=wages.employer_epf_share,
                eps_contribution=wages.eps_contribution,
                ncp_days=wages.ncp_days,
            ),
            _dec(pay.paid_days),
        )
        for employee, pay, wages in computed
    ]
    # A member on the run with no wages at all (a whole month without pay) goes
    # on the ECR with zeros and the month's days as NCP days, as the ECR sheet
    # HR keeps carries them.
    on_file = {employee.id for employee, _, _ in computed}
    failed = {employee.id for employee, _ in errors}
    for pay, employee in rows:
        if employee.id in on_file or employee.id in failed or not (employee.uan or "").strip():
            continue
        if _dec(pay.gross_earnings) or _dec(pay.pf_employee) or _dec(pay.pf_employer):
            continue
        ncp = _dec(pay.lop_days).quantize(Decimal(1), rounding=ROUND_CEILING)
        candidates.append(
            (
                order[employee.id],
                employee,
                EcrLine(
                    uan=(employee.uan or "").strip(),
                    member_name=employee.full_name,
                    gross_wages=_ZERO,
                    epf_wages=_ZERO,
                    eps_wages=_ZERO,
                    edli_wages=_ZERO,
                    employee_share=_ZERO,
                    employer_epf_share=_ZERO,
                    eps_contribution=_ZERO,
                    ncp_days=ncp,
                ),
                None,
            )
        )
    candidates.sort(key=lambda item: item[0])

    checked = validation.validate_ecr_lines(
        [
            {
                "subject": employee.employee_code,
                "uan": line.uan,
                "member_name": line.member_name,
                "gross_wages": line.gross_wages,
                "epf_wages": line.epf_wages,
                "eps_wages": line.eps_wages,
                "edli_wages": line.edli_wages,
                "employee_share": line.employee_share,
                "employer_epf_share": line.employer_epf_share,
                "eps_contribution": line.eps_contribution,
                "ncp_days": line.ncp_days,
                **({"paid_days": paid} if paid is not None else {}),
            }
            for _, employee, line, paid in candidates
        ]
    )
    # Validation names the employee; a duplicate UAN is reported on the second
    # holder only, so it is matched per line in order.
    pending = _by_subject(checked.exceptions)

    built = _Built(rows=0, totals=[], not_covered=0)
    for employee, message in errors:
        built.left_out.append(_issue(employee, message))
    kept: list[EcrLine] = []
    for _, employee, line, paid in candidates:
        own = pending.pop(employee.employee_code, [])
        rejects = [p for p in own if p.code in _ECR_REJECTS]
        if rejects:
            built.left_out.append(_issue(employee, rejects[0].message))
            continue
        for problem in own:
            built.warnings.append(_issue(employee, problem.message))
        if paid is None:
            built.warnings.append(
                _issue(
                    employee,
                    f"No wages this month: written with zero wages and {line.ncp_days} NCP days.",
                )
            )
        kept.append(line)

    ecr = EcrReturn(
        establishment_code=(month.entity.pf_registration_no if month.entity else None) or "",
        wage_month=f"{month.run.period_month:02d}{month.run.period_year}",
        lines=kept,
    )

    def total(attr: str) -> Decimal:
        return sum((_whole(getattr(line, attr)) for line in kept), _ZERO)

    built.rows = len(kept)
    built.not_covered = len(rows) - len(kept) - len(built.left_out)
    member, pension, employer = (
        total("employee_share"),
        total("eps_contribution"),
        total("employer_epf_share"),
    )
    built.totals = [
        ChallanTotal(label="Gross wages", amount=total("gross_wages")),
        ChallanTotal(label="EPF wages", amount=total("epf_wages")),
        ChallanTotal(label="EPS wages", amount=total("eps_wages")),
        ChallanTotal(label="EDLI wages", amount=total("edli_wages")),
        ChallanTotal(label="EPF contribution (employee)", amount=member),
        ChallanTotal(label="EPS contribution (pension)", amount=pension),
        ChallanTotal(label="EPF-EPS difference (employer)", amount=employer),
        ChallanTotal(label="Total contribution", amount=member + pension + employer),
    ]
    text = (
        serialisers.serialise_ecr_upload_csv(ecr)
        if kind == "pf_ecr_csv"
        else serialisers.serialise_ecr(ecr)
    )
    built.content = text.encode("utf-8")
    return built


def _file_name(month: _Month, kind: ChallanKind) -> str:
    code = f"{month.entity.code}_" if month.entity is not None and month.entity.code else ""
    run = month.run
    if kind == "esic_mc":
        return f"MC_Template_{code}{calendar.month_abbr[run.period_month]}-{run.period_year}.xls"
    if kind == "pf_ecr_csv":
        return f"ECR_Upload_{code}{run.period_month:02d}{run.period_year}.csv"
    return f"ECR_{code}{run.period_month:02d}{run.period_year}.txt"


MEDIA_TYPE: dict[str, str] = {
    "esic_mc": "application/vnd.ms-excel",
    "pf_ecr_csv": "text/csv; charset=utf-8",
    "pf_ecr_txt": "text/plain; charset=utf-8",
}


async def _build(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    kind: ChallanKind,
    filters: ChallanFilters,
) -> tuple[_Month, list[tuple[Any, Employee]], _Built]:
    month = await _locked_month(session, user, run_id)
    rows = _filtered(month, filters)
    if kind == "esic_mc":
        built = await _build_esic(session, user, month, rows)
    else:
        built = await _build_ecr(session, user, month, rows, kind)
    return month, rows, built


async def preview(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    kind: ChallanKind,
    filters: ChallanFilters,
) -> ChallanPreview:
    month, rows, built = await _build(session, user, run_id, kind, filters)
    return ChallanPreview(
        kind=kind,
        file_name=_file_name(month, kind),
        period_label=month.label,
        matched=len(rows),
        rows=built.rows,
        totals=built.totals,
        not_covered=built.not_covered,
        wage_mismatches=built.wage_mismatches,
        left_out=built.left_out,
        warnings=built.warnings,
    )


def _filters_used(filters: ChallanFilters) -> dict[str, Any]:
    """The filters that narrowed the file, for the audit trail."""
    used = filters.model_dump(mode="json", exclude_defaults=True)
    for key in ("employee_ids", "exclude_employee_ids"):
        if key in used:
            used[key] = len(used[key])
    return used


async def download(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    kind: ChallanKind,
    filters: ChallanFilters,
    request: Request | None = None,
) -> tuple[str, bytes, str]:
    month, _, built = await _build(session, user, run_id, kind, filters)
    if built.rows == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Nobody the filters chose has "
                f"{'ESI' if kind == 'esic_mc' else 'PF'} to file for {month.label}"
                + (
                    f" ({len(built.left_out)} left out: missing or wrong "
                    f"{'IP number' if kind == 'esic_mc' else 'UAN'})"
                    if built.left_out
                    else ""
                )
                + ". Change the filters, or fix the records and try again."
            ),
        )
    file_name = _file_name(month, kind)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="payroll_run",
        entity_id=month.run.id,
        actor=user,
        changes={
            "export": kind,
            "rows": built.rows,
            "left_out": len(built.left_out),
            "filters": _filters_used(filters),
        },
        summary=(f"Downloaded the {KIND_LABEL[kind]} for {month.label} ({built.rows} row(s))"),
        request=request,
    )
    await session.commit()
    return file_name, built.content, MEDIA_TYPE[kind]
