"""The read side of payroll: run summaries, the register, payslips with their trace.

Two audiences, two doors:

- HR reads any run and any payslip in the tenant (the router is HR-gated).
- An employee reads **their own published payslips** and their own salary
  history, through `my_*`. The employee id comes from the token
  (`user.employee_id`), never from the request, and a payslip that is not
  theirs — or not yet published — is a 404, exactly like one that does not
  exist.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.payroll import (
    EmployeeSalary,
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
)
from app.models.tenant import Tenant
from app.models.timesheets import Project
from app.models.user import User
from app.schemas.payroll_run import (
    InputNoticeRead,
    InputSourceRead,
    PayrollRunRead,
    PayslipImportSummaryRead,
    PayslipInputLineRead,
    PayslipLineRead,
    PayslipRead,
    PayslipSalaryRead,
    PayslipSummaryRead,
    RegisterRead,
    RegisterRowRead,
    RuleSetUsedRead,
    RunImportSummaryRead,
    SalaryRecordRead,
    VarianceFlagRead,
)
from app.services import audit
from app.services.documents.pdf import PdfRenderError
from app.services.employee_identifiers import PAN_PATTERN
from app.services.payroll import payslip_pdf
from app.services.payroll.run import (
    CLOSED_STATUSES,
    get_run,
    imported_from,
    is_imported,
    other_earnings_of,
)
from app.services.payroll.service import salary_record

_ZERO = Decimal(0)


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else _ZERO


# ── Imported runs ───────────────────────────────────────────────────────────


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _text_or_none(value: Any) -> str | None:
    """A register value as text: a list (combined rows) joined, blanks as None."""
    if value is None:
        return None
    if isinstance(value, list | tuple):
        parts = [str(item).strip() for item in value if item is not None and str(item).strip()]
        return ", ".join(dict.fromkeys(parts)) or None
    text = str(value).strip()
    return text or None


def _row_numbers(value: Any) -> list[int]:
    items = value if isinstance(value, list | tuple) else [value]
    rows: list[int] = []
    for item in items:
        try:
            rows.append(int(str(item)))
        except ValueError:
            continue
    return rows


def run_import_summary(run: PayrollRun, names: dict[uuid.UUID, str]) -> RunImportSummaryRead | None:
    """Which register files an imported run came from, when and by whom. None if computed."""
    if not is_imported(run):
        return None
    details = run.import_details or {}
    files = [
        _text_or_none(item.get("name") if isinstance(item, dict) else item)
        for item in details.get("files") or []
    ]
    try:
        imported_at = datetime.fromisoformat(str(details.get("imported_at")))
    except ValueError:
        imported_at = run.approved_at
    by = _uuid_or_none(details.get("imported_by_user_id"))
    return RunImportSummaryRead(
        system=imported_from(run),
        files=[name for name in files if name],
        imported_at=imported_at,
        imported_by_name=names.get(by) if by else None,
    )


def register_client_text(payslip: Payslip) -> str | None:
    """The account an imported payslip's register row named, as typed there.

    The register's Client column; from Feb 2025 the account moved to Project (and
    Domain took the old project values), so a row with a Domain value names its
    account in Project. An explicit `account` written by the importer wins.
    """
    details = payslip.import_details or {}
    if details.get("account"):
        return _text_or_none(details.get("account"))
    project = _text_or_none(details.get("project"))
    if "domain" in details and details.get("domain") is not None and project:
        return project
    return _text_or_none(details.get("client")) or project


def payslip_import_summary(
    payslip: Payslip, run: PayrollRun, *, with_cells: bool
) -> PayslipImportSummaryRead | None:
    """The register file, sheet and row(s) an imported payslip was read from. None if computed."""
    if not is_imported(run) and payslip.import_details is None:
        return None
    details = payslip.import_details or {}
    cells = details.get("cells") if with_cells else None
    return PayslipImportSummaryRead(
        system=str(details.get("system") or imported_from(run)),
        file=_text_or_none(details.get("file")),
        sheet=_text_or_none(details.get("sheet")),
        rows=_row_numbers(details.get("rows") or []),
        status=_text_or_none(details.get("status")),
        client=register_client_text(payslip),
        cells=(
            {str(key): "" if value is None else str(value) for key, value in cells.items()}
            if isinstance(cells, dict)
            else None
        ),
    )


async def _user_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(select(User.id, User.full_name).where(User.id.in_(wanted)), User, user)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _entity_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(LegalEntity.id, LegalEntity.name).where(LegalEntity.id.in_(ids)),
                LegalEntity,
                user,
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


# ── Runs ────────────────────────────────────────────────────────────────────


async def run_reads(
    session: AsyncSession, user: User, runs: list[PayrollRun]
) -> list[PayrollRunRead]:
    people: set[uuid.UUID | None] = set()
    for run in runs:
        people |= {
            run.computed_by_user_id,
            run.approved_by_user_id,
            run.published_by_user_id,
            run.paid_by_user_id,
            run.cancelled_by_user_id,
            _uuid_or_none((run.import_details or {}).get("imported_by_user_id")),
        }
        for flag in run.variance_flags or []:
            if flag.get("acknowledged_by_user_id"):
                people.add(uuid.UUID(flag["acknowledged_by_user_id"]))
    names = await _user_names(session, user, people)
    entities = await _entity_names(session, user, {run.legal_entity_id for run in runs})

    reads: list[PayrollRunRead] = []
    for run in runs:
        flags = []
        for flag in run.variance_flags or []:
            by = flag.get("acknowledged_by_user_id")
            flags.append(
                VarianceFlagRead(
                    employee_id=flag["employee_id"],
                    employee_code=flag.get("employee_code", ""),
                    employee_name=flag.get("employee_name"),
                    previous_net=(
                        str(flag["previous_net"]) if flag.get("previous_net") is not None else None
                    ),
                    current_net=str(flag.get("current_net", "")),
                    change_percent=(
                        str(flag["change_percent"])
                        if flag.get("change_percent") is not None
                        else None
                    ),
                    not_employed_days=str(flag.get("not_employed_days") or "0"),
                    deduction_ceiling=flag.get("deduction_ceiling"),
                    acknowledged=bool(flag.get("acknowledged")),
                    acknowledged_by_user_id=by,
                    acknowledged_by_name=names.get(uuid.UUID(by)) if by else None,
                    acknowledged_at=flag.get("acknowledged_at"),
                    note=flag.get("note"),
                )
            )
        notices = run.input_notices or []
        reads.append(
            PayrollRunRead(
                id=run.id,
                legal_entity_id=run.legal_entity_id,
                legal_entity_name=entities.get(run.legal_entity_id),
                period_year=run.period_year,
                period_month=run.period_month,
                effective_date=run.effective_date,
                payment_date=run.payment_date,
                run_type=run.run_type,
                status=run.status,
                employee_count=run.employee_count,
                total_gross=_dec(run.total_gross),
                total_deductions=_dec(run.total_deductions),
                total_net=_dec(run.total_net),
                total_employer_cost=_dec(run.total_employer_cost),
                variance_flags=flags,
                unacknowledged_variances=sum(1 for f in flags if not f.acknowledged),
                review_flags=list(run.review_flags or []),
                input_sources=[
                    InputSourceRead(**{k: v for k, v in n.items() if k != "kind"})
                    for n in notices
                    if n.get("kind") == "source"
                ],
                input_notices=[
                    InputNoticeRead(key=n["key"], label=n["label"], message=n["message"])
                    for n in notices
                    if n.get("kind") == "notice"
                ],
                rule_set_snapshot=dict(run.rule_set_snapshot or {}),
                error_log=run.error_log,
                processed_at=run.processed_at,
                computed_by_user_id=run.computed_by_user_id,
                computed_by_name=names.get(run.computed_by_user_id),  # type: ignore[arg-type]
                approved_by_user_id=run.approved_by_user_id,
                approved_by_name=names.get(run.approved_by_user_id),  # type: ignore[arg-type]
                approved_at=run.approved_at,
                published_at=run.published_at,
                published_by_name=names.get(run.published_by_user_id),  # type: ignore[arg-type]
                paid_at=run.paid_at,
                paid_by_name=names.get(run.paid_by_user_id),  # type: ignore[arg-type]
                cancelled_at=run.cancelled_at,
                cancelled_by_name=names.get(run.cancelled_by_user_id),  # type: ignore[arg-type]
                cancel_reason=run.cancel_reason,
                created_at=run.created_at,
                employee_scope=[uuid.UUID(e) for e in (run.employee_scope or [])],
                included_sources=run.included_sources,
                reason=run.reason,
                # Nobody approves an imported month: it was locked on import.
                caller_may_approve=run.computed_by_user_id != user.id and not is_imported(run),
                origin=run.origin,
                import_summary=run_import_summary(run, names),
            )
        )
    return reads


async def run_read(session: AsyncSession, user: User, run: PayrollRun) -> PayrollRunRead:
    return (await run_reads(session, user, [run]))[0]


# ── Register ────────────────────────────────────────────────────────────────


async def _payslips_with_people(
    session: AsyncSession, user: User, run_id: uuid.UUID
) -> list[tuple[Payslip, Employee]]:
    return list(
        (
            await session.execute(
                tenant_scoped(
                    select(Payslip, Employee)
                    .join(Employee, Payslip.employee_id == Employee.id)
                    .where(Payslip.run_id == run_id)
                    .order_by(Employee.employee_code),
                    Payslip,
                    user,
                )
            )
        ).all()
    )


def _register_row(payslip: Payslip, employee: Employee, variance_ids: set[str]) -> RegisterRowRead:
    statutory = (
        _dec(payslip.pf_employee)
        + _dec(payslip.esi_employee)
        + _dec(payslip.professional_tax)
        + _dec(payslip.lwf_employee)
        + _dec(payslip.vpf)
        + _dec(payslip.tds)
    )
    return RegisterRowRead(
        payslip_id=payslip.id,
        employee_id=employee.id,
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        working_days=_dec(payslip.working_days),
        paid_days=_dec(payslip.paid_days),
        lop_days=_dec(payslip.lop_days),
        not_employed_days=_dec(payslip.not_employed_days),
        gross_earnings=_dec(payslip.gross_earnings),
        other_earnings=other_earnings_of(payslip),
        pf_employee=_dec(payslip.pf_employee),
        esi_employee=_dec(payslip.esi_employee),
        professional_tax=_dec(payslip.professional_tax),
        lwf_employee=_dec(payslip.lwf_employee),
        vpf=_dec(payslip.vpf),
        tds=_dec(payslip.tds),
        other_deductions=_dec(payslip.total_deductions) - statutory,
        total_deductions=_dec(payslip.total_deductions),
        net_pay=_dec(payslip.net_pay),
        employer_cost=_dec(payslip.employer_cost),
        review_flags=list(payslip.review_flags or []),
        skipped_recoveries=sum(1 for line in payslip.input_lines or [] if line.get("skipped")),
        has_variance=str(employee.id) in variance_ids,
    )


_TOTAL_FIELDS = (
    "gross_earnings",
    "other_earnings",
    "pf_employee",
    "esi_employee",
    "professional_tax",
    "lwf_employee",
    "vpf",
    "tds",
    "other_deductions",
    "total_deductions",
    "net_pay",
    "employer_cost",
)


def payslip_project_id(
    payslip: Payslip, employee: Employee, *, imported: bool = False
) -> uuid.UUID | None:
    """The project a payslip was for: kept on it since projects began, else the person's.

    An imported payslip never falls back: the person's project today says nothing
    about which account they were on in a month paid by the previous payroll.
    """
    if imported or payslip.import_details is not None:
        return payslip.project_id
    return payslip.project_id or employee.project_id


async def project_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(
        tenant_scoped(select(Project.id, Project.name).where(Project.id.in_(wanted)), Project, user)
    )
    return dict(rows.all())


async def register(session: AsyncSession, user: User, run_id: uuid.UUID) -> RegisterRead:
    run = await get_run(session, user, run_id)
    imported = is_imported(run)
    variance_ids = {flag.get("employee_id") for flag in run.variance_flags or []}
    people = await _payslips_with_people(session, user, run.id)
    names = await project_names(
        session,
        user,
        {payslip_project_id(payslip, employee, imported=imported) for payslip, employee in people},
    )
    rows = []
    for payslip, employee in people:
        row = _register_row(payslip, employee, variance_ids)
        project_id = payslip_project_id(payslip, employee, imported=imported)
        row.project_id = project_id
        row.project_name = names.get(project_id) if project_id else None
        if imported and project_id is None:
            row.register_client = register_client_text(payslip)
        rows.append(row)
    totals = {
        name: sum((getattr(row, name) for row in rows), start=_ZERO) for name in _TOTAL_FIELDS
    }
    return RegisterRead(run=await run_read(session, user, run), rows=rows, totals=totals)


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # A cell starting with one of these is a formula to a spreadsheet. Names
    # come from people; neutralise rather than trust.
    if text[:1] in ("=", "+", "@", "\t", "\r") or (text[:1] == "-" and not _is_number(text)):
        return "'" + text
    return text


def _is_number(text: str) -> bool:
    try:
        Decimal(text)
    except ArithmeticError:
        return False
    except ValueError:
        return False
    return True


async def register_csv(
    session: AsyncSession, user: User, run_id: uuid.UUID, request: Request | None = None
) -> tuple[str, str]:
    """The register as CSV. Taking a copy of everybody's pay is audited."""
    data = await register(session, user, run_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    headings = [
        "Employee code",
        "Employee",
        "Working days",
        "Paid days",
        "LOP days",
        "Not employed days",
        "Gross earnings",
        "Other earnings",
        "PF (employee)",
        "ESI (employee)",
        "Professional tax",
        "LWF (employee)",
        "VPF",
        "TDS",
        "Other deductions",
        "Total deductions",
        "Net pay",
        "Employer cost",
    ]
    writer.writerow(headings)
    for row in data.rows:
        writer.writerow(
            [
                _csv_cell(v)
                for v in (
                    row.employee_code,
                    row.employee_name,
                    row.working_days,
                    row.paid_days,
                    row.lop_days,
                    row.not_employed_days,
                    row.gross_earnings,
                    row.other_earnings,
                    row.pf_employee,
                    row.esi_employee,
                    row.professional_tax,
                    row.lwf_employee,
                    row.vpf,
                    row.tds,
                    row.other_deductions,
                    row.total_deductions,
                    row.net_pay,
                    row.employer_cost,
                )
            ]
        )
    writer.writerow(
        ["", "Total", "", "", "", ""] + [str(data.totals[name]) for name in _TOTAL_FIELDS]
    )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="payroll_run",
        entity_id=data.run.id,
        actor=user,
        changes={"export": "register_csv", "rows": len(data.rows)},
        summary=(
            f"Downloaded the payroll register for {data.run.period_month}/{data.run.period_year}"
        ),
        request=request,
    )
    await session.commit()
    filename = f"payroll-register-{data.run.period_year}-{data.run.period_month:02d}.csv"
    return filename, buffer.getvalue()


# ── Payslips ────────────────────────────────────────────────────────────────


async def _payslip_read(
    session: AsyncSession, user: User, payslip: Payslip, run: PayrollRun, employee: Employee
) -> PayslipRead:
    rule_set_ids: set[uuid.UUID] = set()
    for line in payslip.line_items or []:
        if line.get("rule_set_id"):
            rule_set_ids.add(uuid.UUID(str(line["rule_set_id"])))
    working = payslip.tds_working
    if working and working.get("rule_set_id"):
        rule_set_ids.add(uuid.UUID(str(working["rule_set_id"])))

    rule_sets: list[PayrollRuleSet] = []
    if rule_set_ids:
        rule_sets = list(
            (
                await session.scalars(
                    tenant_scoped(
                        select(PayrollRuleSet).where(PayrollRuleSet.id.in_(rule_set_ids)),
                        PayrollRuleSet,
                        user,
                    )
                )
            ).all()
        )

    salary_read: PayslipSalaryRead | None = None
    if payslip.employee_salary_id is not None:
        salary = await session.scalar(
            tenant_scoped(
                select(EmployeeSalary).where(EmployeeSalary.id == payslip.employee_salary_id),
                EmployeeSalary,
                user,
            )
        )
        if salary is not None:
            salary_read = PayslipSalaryRead(
                id=salary.id,
                structure_name=salary.structure.name if salary.structure else None,
                ctc_annual=_dec(salary.ctc_annual),
                effective_from=salary.effective_from,
            )

    entity_names = await _entity_names(session, user, {run.legal_entity_id})

    if is_imported(run):
        system = imported_from(run)
        tds_note = f"TDS as deducted on {system} (imported). No income-tax working was imported."
    elif working:
        tds_note = (
            f"Projected under the {working.get('regime')} regime for "
            f"{working.get('financial_year')}, re-levelled over "
            f"{working.get('months_remaining')} remaining month(s)."
        )
    else:
        tds_note = (
            "TDS was not computed on this payslip: the run had no approved income tax "
            "rule set in force."
        )

    return PayslipRead(
        id=payslip.id,
        run_id=run.id,
        employee_id=employee.id,
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        designation=employee.designation,
        legal_entity_name=entity_names.get(run.legal_entity_id),
        period_year=run.period_year,
        period_month=run.period_month,
        run_type=run.run_type,
        run_status=run.status,
        effective_date=run.effective_date,
        payment_date=run.payment_date,
        is_published=payslip.is_published,
        published_at=payslip.published_at,
        is_supplementary=payslip.is_supplementary,
        working_days=_dec(payslip.working_days),
        paid_days=_dec(payslip.paid_days),
        lop_days=_dec(payslip.lop_days),
        not_employed_days=_dec(payslip.not_employed_days),
        gross_earnings=_dec(payslip.gross_earnings),
        other_earnings=other_earnings_of(payslip),
        total_deductions=_dec(payslip.total_deductions),
        net_pay=_dec(payslip.net_pay),
        employer_cost=_dec(payslip.employer_cost),
        pf_wage_base=_dec(payslip.pf_wage_base),
        esi_wage_base=_dec(payslip.esi_wage_base),
        pf_employee=_dec(payslip.pf_employee),
        pf_employer=_dec(payslip.pf_employer),
        esi_employee=_dec(payslip.esi_employee),
        esi_employer=_dec(payslip.esi_employer),
        professional_tax=_dec(payslip.professional_tax),
        lwf_employee=_dec(payslip.lwf_employee),
        lwf_employer=_dec(payslip.lwf_employer),
        vpf=_dec(payslip.vpf),
        tds=_dec(payslip.tds),
        review_flags=list(payslip.review_flags or []),
        lines=[
            PayslipLineRead(
                code=str(line.get("code")),
                name=str(line.get("name", line.get("code"))),
                type=str(line.get("type", "earning")),
                amount=_dec(line.get("amount")),
                basis=str(line.get("basis", "")),
                rule_set_id=line.get("rule_set_id"),
            )
            for line in payslip.line_items or []
        ],
        input_lines=[
            PayslipInputLineRead(
                provider=str(line.get("provider", "")),
                code=str(line.get("code", "")),
                name=str(line.get("name", "")),
                kind=str(line.get("kind", "")),
                amount=_dec(line.get("amount")),
                requested=_dec(line.get("requested", line.get("amount"))),
                taxable_amount=_dec(line.get("taxable_amount")),
                basis=str(line.get("basis", "")),
                source=dict(line.get("source") or {}),
                skipped=bool(line.get("skipped")),
                recovery=line.get("recovery"),
            )
            for line in payslip.input_lines or []
        ],
        tds_working=working,
        tds_note=tds_note,
        rule_sets=[
            RuleSetUsedRead(
                id=rs.id,
                rule_type=rs.rule_type,
                jurisdiction=rs.jurisdiction,
                version=rs.version,
                effective_from=rs.effective_from,
                effective_to=rs.effective_to,
                approved_at=rs.approved_at,
                source_reference=rs.source_reference,
            )
            for rs in sorted(rule_sets, key=lambda r: (r.rule_type, r.jurisdiction))
        ],
        salary=salary_read,
        origin=run.origin,
        # The register's own cells for this person's row(s): their own pay
        # figures, never an identifier, so HR and the employee both see them.
        import_summary=payslip_import_summary(payslip, run, with_cells=True),
    )


async def hr_payslip(
    session: AsyncSession, user: User, run_id: uuid.UUID, payslip_id: uuid.UUID
) -> PayslipRead:
    run = await get_run(session, user, run_id)
    row = (
        await session.execute(
            tenant_scoped(
                select(Payslip, Employee)
                .join(Employee, Payslip.employee_id == Employee.id)
                .where(Payslip.id == payslip_id, Payslip.run_id == run.id),
                Payslip,
                user,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payslip not found")
    return await _payslip_read(session, user, row[0], run, row[1])


# ── Self-service ────────────────────────────────────────────────────────────


def _visible_to_employee():
    return (
        Payslip.is_published.is_(True),
        PayrollRun.status.in_(CLOSED_STATUSES),
    )


async def my_payslips(session: AsyncSession, user: User) -> list[PayslipSummaryRead]:
    """The caller's own published payslips, newest first."""
    employee_id = user.employee_id
    if employee_id is None:
        return []
    rows = (
        await session.execute(
            tenant_scoped(
                select(Payslip, PayrollRun, Employee)
                .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                .join(Employee, Payslip.employee_id == Employee.id)
                .where(Payslip.employee_id == employee_id, *_visible_to_employee())
                .order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc()),
                Payslip,
                user,
            )
        )
    ).all()
    return [
        PayslipSummaryRead(
            id=payslip.id,
            run_id=run.id,
            employee_id=employee.id,
            employee_code=employee.employee_code,
            employee_name=employee.full_name,
            period_year=run.period_year,
            period_month=run.period_month,
            run_type=run.run_type,
            run_status=run.status,
            gross_earnings=_dec(payslip.gross_earnings),
            other_earnings=other_earnings_of(payslip),
            total_deductions=_dec(payslip.total_deductions),
            tds=_dec(payslip.tds),
            net_pay=_dec(payslip.net_pay),
            is_published=payslip.is_published,
            published_at=payslip.published_at,
            origin=run.origin,
            import_summary=payslip_import_summary(payslip, run, with_cells=False),
        )
        for payslip, run, employee in rows
    ]


async def my_payslip(session: AsyncSession, user: User, payslip_id: uuid.UUID) -> PayslipRead:
    """One of the caller's own published payslips. Anything else is a 404."""
    employee_id = user.employee_id
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payslip not found")
    if employee_id is None:
        raise not_found
    row = (
        await session.execute(
            tenant_scoped(
                select(Payslip, PayrollRun, Employee)
                .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                .join(Employee, Payslip.employee_id == Employee.id)
                .where(
                    Payslip.id == payslip_id,
                    Payslip.employee_id == employee_id,
                    *_visible_to_employee(),
                ),
                Payslip,
                user,
            )
        )
    ).first()
    if row is None:
        raise not_found
    payslip, run, employee = row
    return await _payslip_read(session, user, payslip, run, employee)


async def _render_pdf(
    session: AsyncSession, user: User, payslip: Payslip, run: PayrollRun, employee: Employee
) -> tuple[bytes, str]:
    entity = await session.scalar(
        tenant_scoped(
            select(LegalEntity).where(LegalEntity.id == run.legal_entity_id), LegalEntity, user
        )
    )
    company = entity.name if entity else None
    if not company:
        tenant = await session.get(Tenant, user.tenant_id)
        company = tenant.name if tenant else "Employer"
    try:
        document = payslip_pdf.build_payslip_document(
            company_name=company,
            company_address=entity.registered_address if entity else None,
            period_year=run.period_year,
            period_month=run.period_month,
            employee_name=employee.full_name,
            employee_code=employee.employee_code,
            designation=employee.designation,
            date_of_joining=employee.date_of_joining,
            pan=employee.pan,
            uan=employee.uan,
            payment_date=run.payment_date,
            working_days=payslip.working_days,
            paid_days=payslip.paid_days,
            lop_days=payslip.lop_days,
            not_employed_days=payslip.not_employed_days,
            line_items=list(payslip.line_items or []),
            input_lines=list(payslip.input_lines or []),
            tds=payslip.tds,
            gross_earnings=payslip.gross_earnings,
            other_earnings=other_earnings_of(payslip),
            total_deductions=payslip.total_deductions,
            net_pay=payslip.net_pay,
        )
        data = payslip_pdf.render_payslip_pdf(document)
    except payslip_pdf.PayslipMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PdfRenderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The payslip could not be rendered as a PDF just now: {exc}",
        ) from exc
    filename = f"payslip-{employee.employee_code}-{run.period_year}-{run.period_month:02d}.pdf"
    return data, filename


async def hr_payslip_pdf(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    payslip_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> tuple[bytes, str]:
    """Any payslip on a run, for HR. Taking a copy of somebody's pay is audited."""
    run = await get_run(session, user, run_id)
    row = (
        await session.execute(
            tenant_scoped(
                select(Payslip, Employee)
                .join(Employee, Payslip.employee_id == Employee.id)
                .where(Payslip.id == payslip_id, Payslip.run_id == run.id),
                Payslip,
                user,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payslip not found")
    payslip, employee = row
    data, filename = await _render_pdf(session, user, payslip, run, employee)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="payslip",
        entity_id=payslip.id,
        actor=user,
        changes={"export": "payslip_pdf", "employee_code": employee.employee_code},
        summary=(
            f"Downloaded the {run.period_month}/{run.period_year} payslip of "
            f"{employee.employee_code} as a PDF"
        ),
        request=request,
    )
    await session.commit()
    return data, filename


async def my_payslip_pdf(
    session: AsyncSession, user: User, payslip_id: uuid.UUID
) -> tuple[bytes, str]:
    """One of the caller's own published payslips as a PDF. Anything else is a 404."""
    employee_id = user.employee_id
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payslip not found")
    if employee_id is None:
        raise not_found
    row = (
        await session.execute(
            tenant_scoped(
                select(Payslip, PayrollRun, Employee)
                .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                .join(Employee, Payslip.employee_id == Employee.id)
                .where(
                    Payslip.id == payslip_id,
                    Payslip.employee_id == employee_id,
                    *_visible_to_employee(),
                ),
                Payslip,
                user,
            )
        )
    ).first()
    if row is None:
        raise not_found
    payslip, run, employee = row
    data, filename = await _render_pdf(session, user, payslip, run, employee)
    if await payslip_protection(session, user):
        data = protect_pdf(data, payslip_password(employee))
    return data, filename


PASSWORD_RULE = (
    "Your PAN in capitals; without a PAN, your date of birth as DDMMYYYY; without either, "
    "your employee code."
)


def payslip_password(employee: Employee) -> str:
    """The password a protected payslip opens with, by the rule above.

    A record can hold what the old HR system had in place of a PAN
    ("PANNOTAVBL", kept as typed for the TDS return): that is not a PAN the
    person could know, so the date of birth is used instead.
    """
    pan = (employee.pan or "").strip().upper()
    if PAN_PATTERN.match(pan):
        return pan
    if employee.date_of_birth:
        return employee.date_of_birth.strftime("%d%m%Y")
    return employee.employee_code


def protect_pdf(data: bytes, password: str) -> bytes:
    """The same PDF, encrypted (AES-256) so it opens only with the password."""
    import io

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(user_password=password, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


async def payslip_protection(session: AsyncSession, user: User) -> bool:
    tenant = await session.get(Tenant, user.tenant_id)
    return bool(((tenant.settings or {}) if tenant else {}).get("payslip_pdf_password"))


async def set_payslip_protection(
    session: AsyncSession, user: User, enabled: bool, *, request: Request | None = None
) -> dict[str, Any]:
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin changes how payslips are protected.")
    tenant = await session.get(Tenant, user.tenant_id)
    tenant.settings = {**(tenant.settings or {}), "payslip_pdf_password": enabled}
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="payslip_settings",
        entity_id=user.tenant_id,
        actor=user,
        changes={"payslip_pdf_password": enabled},
        summary=(
            "Payslip PDFs now open with a password"
            if enabled
            else "Payslip PDFs no longer need a password"
        ),
        request=request,
    )
    await session.commit()
    return {"enabled": enabled, "rule": PASSWORD_RULE}


async def my_salary_history(session: AsyncSession, user: User) -> list[SalaryRecordRead]:
    """The caller's own effective-dated salary series, newest first."""
    employee_id = user.employee_id
    if employee_id is None:
        return []
    rows = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeSalary)
                .where(EmployeeSalary.employee_id == employee_id)
                .order_by(EmployeeSalary.effective_from.desc()),
                EmployeeSalary,
                user,
            )
        )
    ).all()
    return [salary_record(row) for row in rows]


__all__ = [
    "PayrollRunStatus",
    "hr_payslip",
    "hr_payslip_pdf",
    "my_payslip",
    "my_payslip_pdf",
    "my_payslips",
    "my_salary_history",
    "register",
    "register_csv",
    "run_read",
    "run_reads",
]
