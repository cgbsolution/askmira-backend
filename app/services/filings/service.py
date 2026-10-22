"""Return generation: query, validate, build the canonical structure, serialise.

The order matters and is the same for every return type:

1. Query the stored payslips. Never recompute — the return has to agree with
   what the employee was actually paid.
2. Validate. **Refuse on any blocking exception**, recording the fixable list
   against a BLOCKED filing record so it survives a page refresh.
3. Build the canonical structure from `records.py`.
4. Hand it to the serialiser.

Steps 1-3 are here; step 4 is one call into `serialisers.py`. That split is what
makes a format change a one-file patch during a filing window.
"""

from __future__ import annotations

import calendar
import dataclasses
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.arrears import ArrearBatch, ArrearLine, ArrearStatus
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.filings import (
    FilingRecord,
    FilingStatus,
    ReturnType,
    TdsChallan,
    TdsChallanMapping,
)
from app.models.org import LegalEntity, Location
from app.models.payroll import (
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
)
from app.models.tax import TaxComputationSnapshot
from app.models.user import User
from app.services import audit
from app.services.arrears.engine import ESI_WAGES, PF_WAGES
from app.services.filings import serialisers, support, validation
from app.services.filings.records import (
    ArrearEcrLine,
    ArrearEcrReturn,
    EcrLine,
    EcrReturn,
    EsicLine,
    EsicReturn,
    PtChallan,
    PtLine,
    Tds24QReturn,
    TdsChallanLine,
    TdsDeducteeLine,
    TdsSalaryDetail,
)
from app.services.filings.wages import WageError, compute_ecr_wages
from app.services.payroll import statutory_service

__all__ = [
    "FilingBlockedError",
    "GeneratedFile",
    "generate_arrear_ecr",
    "generate_ecr",
    "generate_esic",
    "generate_pt_challans",
    "generate_tds_24q",
    "record_acknowledgement",
]


class FilingBlockedError(Exception):
    """Validation found defects. No file was produced.

    Carries the filing record so the caller can hand the exception list back to
    the user without another query.
    """

    def __init__(self, record: FilingRecord, result: validation.ValidationResult) -> None:
        self.record = record
        self.result = result
        super().__init__(
            f"{len(result.blocking)} blocking exception(s) prevent this return being generated."
        )


class GeneratedFile:
    """A produced return: its bytes, its record, and any non-blocking warnings."""

    def __init__(
        self, content: str, record: FilingRecord, result: validation.ValidationResult
    ) -> None:
        self.content = content
        self.record = record
        self.result = result


# ── Shared plumbing ─────────────────────────────────────────────────────────


async def _entity(session: AsyncSession, user: User, entity_id: uuid.UUID) -> LegalEntity:
    entity = await session.scalar(
        tenant_scoped(select(LegalEntity).where(LegalEntity.id == entity_id), LegalEntity, user)
    )
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Legal entity not found")
    return entity


async def _approved_run(
    session: AsyncSession, user: User, entity_id: uuid.UUID, year: int, month: int
) -> PayrollRun:
    """The run a return is built from.

    A return is only ever generated from an approved run. Filing figures from a
    draft means filing numbers nobody has signed off, and a statutory return is
    not a preview.
    """
    run = await session.scalar(
        tenant_scoped(
            select(PayrollRun).where(
                PayrollRun.legal_entity_id == entity_id,
                PayrollRun.period_year == year,
                PayrollRun.period_month == month,
                PayrollRun.run_type == PayrollRunType.REGULAR,
                # A month can hold a run cancelled in error beside the real one.
                PayrollRun.status != PayrollRunStatus.CANCELLED,
            ),
            PayrollRun,
            user,
        )
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No payroll run exists for {month:02d}/{year}.",
        )
    if run.status not in (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The run for {month:02d}/{year} is '{run.status.value}'. A statutory "
                "return is generated from an approved run — filing unapproved figures "
                "is filing numbers nobody has signed off."
            ),
        )
    return run


@dataclass(slots=True)
class _MonthPay:
    """One employee's month across every approved run that paid it.

    A monthly return has one line per person. Where an off-cycle run topped the
    month up — overtime keyed in late, a bonus — its amounts are added to the
    regular payslip's, and the days come from the payslip that paid the days.
    """

    id: uuid.UUID
    gross_earnings: Decimal
    #: Structure gross plus pay for work (overtime, shift allowance): the PT base.
    month_gross: Decimal
    pf_wage_base: Decimal
    esi_wage_base: Decimal
    pf_employee: Decimal
    pf_employer: Decimal
    esi_employee: Decimal
    esi_employer: Decimal
    professional_tax: Decimal
    tds: Decimal
    lop_days: Decimal
    paid_days: Decimal
    #: Voluntary PF: remitted with the employee's share on the ECR.
    vpf: Decimal = Decimal(0)


def _as_decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _wage_lines(payslip: Payslip) -> Decimal:
    return sum(
        (
            Decimal(str(line.get("amount", "0")))
            for line in (payslip.input_lines or [])
            if line.get("counts_as_wages") and not line.get("skipped")
        ),
        start=Decimal(0),
    )


async def _payslips(
    session: AsyncSession, user: User, run: PayrollRun
) -> list[tuple[_MonthPay, Employee]]:
    """The month's pay per employee: the regular run plus any approved top-ups."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(Payslip, Employee)
                .join(Employee, Payslip.employee_id == Employee.id)
                .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                .where(
                    PayrollRun.legal_entity_id == run.legal_entity_id,
                    PayrollRun.period_year == run.period_year,
                    PayrollRun.period_month == run.period_month,
                    PayrollRun.status.in_([PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]),
                )
                .order_by(Employee.employee_code, Payslip.is_supplementary),
                Payslip,
                user,
            )
        )
    ).all()
    merged: dict[uuid.UUID, tuple[_MonthPay, Employee]] = {}
    for payslip, employee in rows:
        current = merged.get(employee.id)
        if current is None:
            merged[employee.id] = (
                _MonthPay(
                    id=payslip.id,
                    gross_earnings=_as_decimal(payslip.gross_earnings),
                    month_gross=_as_decimal(payslip.gross_earnings) + _wage_lines(payslip),
                    pf_wage_base=_as_decimal(payslip.pf_wage_base),
                    esi_wage_base=_as_decimal(payslip.esi_wage_base),
                    pf_employee=_as_decimal(payslip.pf_employee),
                    pf_employer=_as_decimal(payslip.pf_employer),
                    esi_employee=_as_decimal(payslip.esi_employee),
                    esi_employer=_as_decimal(payslip.esi_employer),
                    professional_tax=_as_decimal(payslip.professional_tax),
                    tds=_as_decimal(payslip.tds),
                    lop_days=_as_decimal(payslip.lop_days),
                    paid_days=_as_decimal(payslip.paid_days),
                    vpf=_as_decimal(payslip.vpf or 0),
                ),
                employee,
            )
            continue
        pay = current[0]
        pay.gross_earnings += _as_decimal(payslip.gross_earnings)
        pay.month_gross += _as_decimal(payslip.gross_earnings) + _wage_lines(payslip)
        pay.pf_wage_base += _as_decimal(payslip.pf_wage_base)
        pay.esi_wage_base += _as_decimal(payslip.esi_wage_base)
        pay.pf_employee += _as_decimal(payslip.pf_employee)
        pay.pf_employer += _as_decimal(payslip.pf_employer)
        pay.esi_employee += _as_decimal(payslip.esi_employee)
        pay.esi_employer += _as_decimal(payslip.esi_employer)
        pay.professional_tax += _as_decimal(payslip.professional_tax)
        pay.tds += _as_decimal(payslip.tds)
        pay.vpf += _as_decimal(payslip.vpf or 0)
        if not payslip.is_supplementary:
            pay.lop_days = _as_decimal(payslip.lop_days)
            pay.paid_days = _as_decimal(payslip.paid_days)
    return list(merged.values())


async def _rule_set(
    session: AsyncSession, user: User, rule_type: str, jurisdiction: str, on_date: date
) -> PayrollRuleSet | None:
    return await session.scalar(
        tenant_scoped(
            select(PayrollRuleSet)
            .where(
                PayrollRuleSet.rule_type == rule_type,
                PayrollRuleSet.jurisdiction == jurisdiction,
                PayrollRuleSet.effective_from <= on_date,
                or_(
                    PayrollRuleSet.effective_to.is_(None),
                    PayrollRuleSet.effective_to >= on_date,
                ),
            )
            .order_by(PayrollRuleSet.effective_from.desc()),
            PayrollRuleSet,
            user,
        )
    )


async def _record(
    session: AsyncSession,
    user: User,
    *,
    return_type: ReturnType,
    entity_id: uuid.UUID,
    period: str,
    jurisdiction: str | None,
    result: validation.ValidationResult,
    content: str | None,
    file_name: str | None,
    record_count: int,
    total_amount: Decimal,
    request: Request | None,
) -> FilingRecord:
    """Write the filing record, blocked or generated.

    A blocked attempt is recorded too. "We tried to file and could not, here is
    why" is exactly what a missed-deadline conversation needs, and losing it on
    a page refresh helps nobody.
    """
    blocked = result.is_blocked
    record = FilingRecord(
        tenant_id=user.tenant_id,
        return_type=return_type,
        legal_entity_id=entity_id,
        period=period,
        jurisdiction=jurisdiction,
        status=FilingStatus.BLOCKED if blocked else FilingStatus.GENERATED,
        file_name=None if blocked else file_name,
        content_hash=None if blocked else serialisers.content_hash(content or ""),
        record_count=0 if blocked else record_count,
        total_amount=Decimal(0) if blocked else total_amount,
        generated_at=None if blocked else datetime.now(UTC),
        generated_by_user_id=user.id,
        validation_exceptions=result.as_list(),
    )
    if not blocked and content is not None:
        # Kept, encrypted, so the file that was filed can be downloaded again
        # exactly — a regenerated file can differ once payslips move.
        record.file_storage_key = await support.store_generated(
            user.tenant_id, "filings", content.encode("utf-8"), ".txt"
        )
    session.add(record)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="filing_record",
        entity_id=record.id,
        summary=(
            f"{'Blocked' if blocked else 'Generated'} {return_type.value} for {period}"
            f"{f' ({jurisdiction})' if jurisdiction else ''}: {result.summary()}"
        ),
        changes={
            "return_type": return_type.value,
            "period": period,
            "status": record.status.value,
            "record_count": record.record_count,
            "blocking": len(result.blocking),
        },
        request=request,
    )
    return record


# ═══════════════════════════════════════════════════════════════════════════
# PF ECR
# ═══════════════════════════════════════════════════════════════════════════


async def generate_ecr(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    year: int,
    month: int,
    request: Request | None = None,
) -> GeneratedFile:
    entity = await _entity(session, user, legal_entity_id)
    run = await _approved_run(session, user, legal_entity_id, year, month)
    rows = await _payslips(session, user, run)

    pf_rules = await _rule_set(session, user, "pf", "IN", run.effective_date)
    if pf_rules is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No PF rule set is in force for this period, so EPF, EPS and EDLI "
                "ceilings are unknown. None of them is assumed."
            ),
        )

    period = f"{year}-{month:02d}"
    to_validate: list[dict[str, Any]] = []
    computed: list[tuple[Employee, Any]] = []

    for payslip, employee in rows:
        if not Decimal(str(payslip.pf_employee)) and not Decimal(str(payslip.pf_employer)):
            # No PF this month — a contractor, or someone excluded. They do not
            # belong on the ECR at all.
            continue

        election = await statutory_service.active_pf_election(
            session, user, employee.id, run.effective_date
        )
        try:
            wages = compute_ecr_wages(
                rules=pf_rules.rules,
                gross_earnings=Decimal(str(payslip.gross_earnings)),
                pf_wage_base=Decimal(str(payslip.pf_wage_base)),
                pf_employee=Decimal(str(payslip.pf_employee)),
                pf_employer=Decimal(str(payslip.pf_employer)),
                lop_days=Decimal(str(payslip.lop_days)),
                eps_member=_eps_member(employee),
                pf_on_actual_wage=bool(election and election.on_actual_wage),
                vpf=payslip.vpf,
            )
        except WageError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{employee.employee_code}: {exc}",
            ) from exc

        computed.append((employee, wages))
        to_validate.append(
            {
                "subject": employee.employee_code,
                "uan": employee.uan,
                "member_name": employee.full_name,
                "gross_wages": wages.gross_wages,
                "epf_wages": wages.epf_wages,
                "eps_wages": wages.eps_wages,
                "edli_wages": wages.edli_wages,
                "employee_share": wages.employee_share,
                "employer_epf_share": wages.employer_epf_share,
                "eps_contribution": wages.eps_contribution,
                "ncp_days": wages.ncp_days,
                "paid_days": Decimal(str(payslip.paid_days)),
            }
        )

    result = validation.validate_ecr_lines(to_validate)
    if result.is_blocked:
        record = await _record(
            session,
            user,
            return_type=ReturnType.PF_ECR,
            entity_id=legal_entity_id,
            period=period,
            jurisdiction=None,
            result=result,
            content=None,
            file_name=None,
            record_count=0,
            total_amount=Decimal(0),
            request=request,
        )
        raise FilingBlockedError(record, result)

    ecr = EcrReturn(
        establishment_code=entity.pf_registration_no or "",
        wage_month=f"{month:02d}{year}",
        lines=[
            EcrLine(
                uan=employee.uan or "",
                member_name=employee.full_name,
                gross_wages=wages.gross_wages,
                epf_wages=wages.epf_wages,
                eps_wages=wages.eps_wages,
                edli_wages=wages.edli_wages,
                employee_share=wages.employee_share,
                employer_epf_share=wages.employer_epf_share,
                eps_contribution=wages.eps_contribution,
                ncp_days=wages.ncp_days,
            )
            for employee, wages in computed
        ],
    )

    content = serialisers.serialise_ecr(ecr)
    record = await _record(
        session,
        user,
        return_type=ReturnType.PF_ECR,
        entity_id=legal_entity_id,
        period=period,
        jurisdiction=None,
        result=result,
        content=content,
        file_name=f"ECR_{entity.code}_{month:02d}{year}.txt",
        record_count=len(ecr.lines),
        total_amount=ecr.total_contributions,
        request=request,
    )
    return GeneratedFile(content, record, result)


def _eps_member(employee: Employee) -> bool:
    """Whether this employee is a pension-scheme member.

    Membership is decided at enrolment — an employee who first joined the fund
    above the wage ceiling is commonly excluded — and it is not something that
    can be inferred from a payslip. It is held on the employee record, and the
    default is membership because that is the common case.
    """
    value = (employee.custom_fields or {}).get("eps_member")
    return True if value is None else bool(value)


def _rupee(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1), rounding=ROUND_HALF_UP)


async def generate_arrear_ecr(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    year: int,
    month: int,
    request: Request | None = None,
) -> GeneratedFile:
    """The PF arrear ECR for the arrears paid with a month's payroll.

    The EPFO takes PF arrears on a separate arrear ECR: per member the arrear
    EPF, EPS and EDLI wages and the three shares. Each recomputed month is
    worked on its own rule set and the member's PF election then, and the
    months add up per member. The figures are the arrear batch's, which is what
    the arrear payslip deducted.
    """
    entity = await _entity(session, user, legal_entity_id)
    await _approved_run(session, user, legal_entity_id, year, month)
    period = f"{year}-{month:02d}"
    arrears = await _arrears_paid_in(
        session, user, legal_entity_id, year, month, ("PF_EMPLOYEE", "PF_EMPLOYER", "VPF", PF_WAGES)
    )
    if not arrears:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No PF arrears were paid with the payroll for {calendar.month_name[month]} "
                f"{year}, so there is no arrear ECR to file."
            ),
        )

    rule_sets: dict[uuid.UUID, PayrollRuleSet | None] = {}
    elections: dict[tuple[uuid.UUID, str], bool] = {}
    by_employee: dict[uuid.UUID, list[ArrearLine]] = {}
    for arrear in arrears:
        by_employee.setdefault(arrear.employee_id, []).append(arrear)

    exceptions: list[validation.FilingException] = []
    to_validate: list[dict[str, Any]] = []
    computed: list[ArrearEcrLine] = []
    for employee_id, mine in by_employee.items():
        employee = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
        )
        if employee is None:
            continue
        eps_member = _eps_member(employee)
        totals = dict.fromkeys(("epf", "eps", "edli", "ee", "er", "eps_share"), Decimal(0))
        wage_periods = {a.period for a in mine if a.component_code == PF_WAGES}
        unrecorded = sorted(
            {a.period for a in mine if a.component_code != PF_WAGES and a.difference} - wage_periods
        )
        for arrear in mine:
            difference = Decimal(str(arrear.difference))
            if arrear.component_code in ("PF_EMPLOYEE", "VPF"):
                totals["ee"] += difference
                continue
            if arrear.component_code == "PF_EMPLOYER":
                totals["er"] += difference
                continue
            year_, month_ = (int(part) for part in arrear.period.split("-"))
            period_end = date(year_, month_, calendar.monthrange(year_, month_)[1])
            if arrear.rule_set_id not in rule_sets:
                rule_sets[arrear.rule_set_id] = (
                    await session.get(PayrollRuleSet, arrear.rule_set_id)
                    if arrear.rule_set_id
                    else await _rule_set(session, user, "pf", "IN", period_end)
                )
            rules = rule_sets[arrear.rule_set_id]
            if rules is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"No PF rule set covers {arrear.period}, the month this arrear revises.",
                )
            key = (employee_id, arrear.period)
            if key not in elections:
                election = await statutory_service.active_pf_election(
                    session, user, employee_id, period_end
                )
                elections[key] = bool(election and election.on_actual_wage)
            missing = [
                name
                for name in ("wage_ceiling", "eps_wage_ceiling", "edli_wage_ceiling", "eps_rate")
                if name not in rules.rules
            ]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"The PF rule set for {arrear.period} has no {', '.join(missing)}. EPF, "
                        "EPS and EDLI arrear wages each take their own ceiling; none is assumed."
                    ),
                )
            ceiling = Decimal(str(rules.rules["wage_ceiling"]))
            eps_ceiling = Decimal(str(rules.rules["eps_wage_ceiling"]))
            edli_ceiling = Decimal(str(rules.rules["edli_wage_ceiling"]))
            eps_rate = Decimal(str(rules.rules["eps_rate"]))
            for sign, wage in (
                (Decimal(-1), Decimal(str(arrear.paid_amount))),
                (Decimal(1), Decimal(str(arrear.should_have_been_amount))),
            ):
                eps_wage = min(wage, eps_ceiling) if eps_member else Decimal(0)
                totals["epf"] += sign * (wage if elections[key] else min(wage, ceiling))
                totals["eps"] += sign * eps_wage
                totals["edli"] += sign * min(wage, edli_ceiling)
                # Whole rupees per month, as each month's own return carried it.
                totals["eps_share"] += sign * _rupee(eps_wage * eps_rate / Decimal(100))
        if unrecorded:
            exceptions.append(
                validation.FilingException(
                    validation.Severity.BLOCKING,
                    "ARREAR_WAGES_UNRECORDED",
                    employee.employee_code,
                    "epf_wages",
                    "These arrears were computed before AskMira recorded the PF wages they "
                    f"revise ({', '.join(unrecorded)}), so their arrear wages are not known "
                    "here. Prepare this member's arrear ECR line from the arrear batch.",
                )
            )
            continue
        line = ArrearEcrLine(
            uan=employee.uan or "",
            member_name=employee.full_name,
            epf_wages=totals["epf"],
            eps_wages=totals["eps"],
            edli_wages=totals["edli"],
            employee_share=totals["ee"],
            employer_epf_share=totals["er"] - totals["eps_share"],
            eps_contribution=totals["eps_share"],
        )
        figures = (
            line.epf_wages,
            line.eps_wages,
            line.edli_wages,
            line.employee_share,
            line.employer_epf_share,
            line.eps_contribution,
        )
        if not any(figures):
            continue
        if any(figure < 0 for figure in figures):
            exceptions.append(
                validation.FilingException(
                    validation.Severity.WARNING,
                    "ARREAR_REDUCTION",
                    employee.employee_code,
                    "employee_share",
                    "These arrears reduce PF. The arrear ECR carries no negative figures, so "
                    "this member is left off it; the excess is settled with the EPFO.",
                )
            )
            continue
        computed.append(line)
        to_validate.append(
            {
                "subject": employee.employee_code,
                "uan": line.uan,
                "member_name": line.member_name,
                "epf_wages": line.epf_wages,
                "eps_wages": line.eps_wages,
                "edli_wages": line.edli_wages,
                "employee_share": line.employee_share,
                "employer_epf_share": line.employer_epf_share,
                "eps_contribution": line.eps_contribution,
            }
        )

    checked = validation.validate_ecr_lines(to_validate)
    found = [*checked.exceptions, *exceptions]
    if not computed:
        found.append(
            validation.FilingException(
                validation.Severity.BLOCKING,
                "NOTHING_TO_FILE",
                entity.code,
                "lines",
                "The arrears paid this month change no PF wage or contribution the arrear "
                "ECR carries.",
            )
        )
    result = validation.ValidationResult(found)
    if result.is_blocked:
        record = await _record(
            session,
            user,
            return_type=ReturnType.PF_ECR_ARREAR,
            entity_id=legal_entity_id,
            period=period,
            jurisdiction=None,
            result=result,
            content=None,
            file_name=None,
            record_count=0,
            total_amount=Decimal(0),
            request=request,
        )
        raise FilingBlockedError(record, result)

    ecr = ArrearEcrReturn(
        establishment_code=entity.pf_registration_no or "",
        wage_month=f"{month:02d}{year}",
        lines=computed,
    )
    content = serialisers.serialise_arrear_ecr(ecr)
    record = await _record(
        session,
        user,
        return_type=ReturnType.PF_ECR_ARREAR,
        entity_id=legal_entity_id,
        period=period,
        jurisdiction=None,
        result=result,
        content=content,
        file_name=f"ARREAR_ECR_{entity.code}_{month:02d}{year}.txt",
        record_count=len(ecr.lines),
        total_amount=ecr.total_contributions,
        request=request,
    )
    return GeneratedFile(content, record, result)


# ═══════════════════════════════════════════════════════════════════════════
# ESIC
# ═══════════════════════════════════════════════════════════════════════════


async def generate_esic(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    year: int,
    month: int,
    request: Request | None = None,
) -> GeneratedFile:
    entity = await _entity(session, user, legal_entity_id)
    run = await _approved_run(session, user, legal_entity_id, year, month)
    rows = await _payslips(session, user, run)
    esi_rules = await _rule_set(session, user, "esi", "IN", run.effective_date)

    period = f"{year}-{month:02d}"
    to_validate: list[dict[str, Any]] = []
    lines: list[EsicLine] = []
    line_of: dict[uuid.UUID, int] = {}

    for payslip, employee in rows:
        employee_share = Decimal(str(payslip.esi_employee))
        employer_share = Decimal(str(payslip.esi_employer))
        if not employee_share and not employer_share:
            # Above the ceiling from the start of the period, or not covered.
            continue

        # Someone above the ceiling who is still contributing is on this return
        # because the contribution period has not ended (slice 09). Recording
        # why turns "an auditor asks about a high earner" into a one-line answer.
        continuing = None
        if esi_rules is not None:
            threshold = Decimal(str(esi_rules.rules.get("wage_threshold", 0)))
            wage_base = Decimal(str(payslip.esi_wage_base))
            if threshold and wage_base > threshold:
                _, coverage = await statutory_service.esi_coverage_for(
                    session,
                    user,
                    employee_id=employee.id,
                    on_date=date(year, month, 1),
                    esi_rules=esi_rules,
                )
                continuing = (
                    f"Wages of {wage_base} are above the ceiling of {threshold}; "
                    f"contributions continue to the end of the "
                    f"{coverage.period_label if coverage else 'current'} period."
                )

        line = EsicLine(
            ip_number=employee.esi_number or "",
            ip_name=employee.full_name,
            days_worked=Decimal(str(payslip.paid_days)),
            wages_paid=Decimal(str(payslip.esi_wage_base)),
            employee_contribution=employee_share,
            employer_contribution=employer_share,
            continuing_reason=continuing,
        )
        line_of[employee.id] = len(lines)
        lines.append(line)
        to_validate.append(
            {
                "subject": employee.employee_code,
                "ip_number": line.ip_number,
                "days_worked": line.days_worked,
                "wages_paid": line.wages_paid,
                "employee_contribution": line.employee_contribution,
                "employer_contribution": line.employer_contribution,
            }
        )

    noted = await _esi_arrears_into(
        session, user, legal_entity_id, year, month, lines, line_of, to_validate
    )
    checked = validation.validate_esic_lines(to_validate)
    result = validation.ValidationResult([*checked.exceptions, *noted])
    if result.is_blocked:
        record = await _record(
            session,
            user,
            return_type=ReturnType.ESIC,
            entity_id=legal_entity_id,
            period=period,
            jurisdiction=None,
            result=result,
            content=None,
            file_name=None,
            record_count=0,
            total_amount=Decimal(0),
            request=request,
        )
        raise FilingBlockedError(record, result)

    esic = EsicReturn(
        employer_code=entity.esi_registration_no or "",
        contribution_period=_esi_period_label(month, esi_rules),
        wage_month=f"{month:02d}{year}",
        lines=lines,
    )
    content = serialisers.serialise_esic(esic)
    record = await _record(
        session,
        user,
        return_type=ReturnType.ESIC,
        entity_id=legal_entity_id,
        period=period,
        jurisdiction=None,
        result=result,
        content=content,
        file_name=f"ESIC_{entity.code}_{month:02d}{year}.csv",
        record_count=len(lines),
        total_amount=esic.total_contributions,
        request=request,
    )
    return GeneratedFile(content, record, result)


async def _arrears_paid_in(
    session: AsyncSession,
    user: User,
    legal_entity_id: uuid.UUID,
    year: int,
    month: int,
    codes: tuple[str, ...],
) -> list[ArrearLine]:
    """Arrear lines with these codes, from batches paid with the month's payroll."""
    runs = tenant_scoped(
        select(PayrollRun.id).where(
            PayrollRun.legal_entity_id == legal_entity_id,
            PayrollRun.period_year == year,
            PayrollRun.period_month == month,
            PayrollRun.status.in_([PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]),
        ),
        PayrollRun,
        user,
    )
    rows = await session.scalars(
        tenant_scoped(
            select(ArrearLine)
            .join(ArrearBatch, ArrearLine.batch_id == ArrearBatch.id)
            .where(
                ArrearBatch.status == ArrearStatus.PAID,
                ArrearBatch.legal_entity_id == legal_entity_id,
                ArrearBatch.paid_in_run_id.in_(runs),
                ArrearLine.component_code.in_(codes),
            )
            .order_by(ArrearLine.employee_id, ArrearLine.period),
            ArrearLine,
            user,
        )
    )
    return list(rows.all())


async def _esi_arrears_into(
    session: AsyncSession,
    user: User,
    legal_entity_id: uuid.UUID,
    year: int,
    month: int,
    lines: list[EsicLine],
    line_of: dict[uuid.UUID, int],
    to_validate: list[dict[str, Any]],
) -> list[validation.FilingException]:
    """Add the ESI arrears paid with this month to its return, in place.

    ESIC takes arrears of wages with the month they are paid in, so an IP's
    arrear ESI wages and contributions join that month's line. What cannot go
    on the file is said rather than dropped.
    """
    arrears = await _arrears_paid_in(
        session, user, legal_entity_id, year, month, (ESI_WAGES, "ESI_EMPLOYEE", "ESI_EMPLOYER")
    )
    per_employee: dict[uuid.UUID, dict[str, Decimal]] = {}
    for arrear in arrears:
        bucket = per_employee.setdefault(arrear.employee_id, {})
        code = arrear.component_code
        bucket[code] = bucket.get(code, Decimal(0)) + Decimal(str(arrear.difference))

    noted: list[validation.FilingException] = []
    for employee_id, bucket in per_employee.items():
        wages = bucket.get(ESI_WAGES, Decimal(0))
        employee_share = bucket.get("ESI_EMPLOYEE", Decimal(0))
        employer_share = bucket.get("ESI_EMPLOYER", Decimal(0))
        if not (wages or employee_share or employer_share):
            continue
        employee = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
        )
        subject = employee.employee_code if employee else str(employee_id)
        owed = employee_share + employer_share
        if wages < 0 or owed < 0:
            noted.append(
                validation.FilingException(
                    validation.Severity.WARNING,
                    "ARREAR_REDUCTION",
                    subject,
                    "wages_paid",
                    f"Arrears paid this month reduce ESI wages by {-wages} and contributions "
                    f"by {-owed}. ESIC takes no negative wages: the month is filed on its own "
                    "wages and the excess is settled with ESIC.",
                )
            )
            continue
        position = line_of.get(employee_id)
        if position is None:
            noted.append(
                validation.FilingException(
                    validation.Severity.WARNING,
                    "ARREAR_NOT_ON_FILE",
                    subject,
                    "wages_paid",
                    f"ESI arrears of {owed} (on arrear wages of {wages}) were paid this month, "
                    "but this IP has no ESI wages this month and the monthly file cannot "
                    "carry wages without days. Deposit them on the ESIC portal separately.",
                )
            )
            continue
        line = lines[position]
        lines[position] = dataclasses.replace(
            line,
            wages_paid=line.wages_paid + wages,
            employee_contribution=line.employee_contribution + employee_share,
            employer_contribution=line.employer_contribution + employer_share,
        )
        row = to_validate[position]
        row["wages_paid"] = lines[position].wages_paid
        row["employee_contribution"] = lines[position].employee_contribution
        row["employer_contribution"] = lines[position].employer_contribution
        noted.append(
            validation.FilingException(
                validation.Severity.WARNING,
                "ARREARS_INCLUDED",
                subject,
                "wages_paid",
                f"Includes arrear wages of {wages} paid this month (ESI {employee_share} "
                f"employee, {employer_share} employer): ESIC takes arrears with the month "
                "they are paid in.",
            )
        )
    return noted


def _esi_period_label(month: int, esi_rules: PayrollRuleSet | None) -> str:
    if esi_rules is None:
        return ""
    from app.services.payroll.statutory import StatutoryError, esi_contribution_period

    try:
        return esi_contribution_period(month, esi_rules.rules).label
    except StatutoryError:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# Professional Tax
# ═══════════════════════════════════════════════════════════════════════════


async def generate_pt_challans(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    year: int,
    month: int,
    request: Request | None = None,
) -> list[GeneratedFile]:
    """One challan set per state.

    Professional Tax is filed with each state separately, so a run covering
    Karnataka and Maharashtra produces two challans and two filing records —
    not one file with a state column.
    """
    entity = await _entity(session, user, legal_entity_id)
    run = await _approved_run(session, user, legal_entity_id, year, month)
    rows = await _payslips(session, user, run)

    period = f"{year}-{month:02d}"
    by_state: dict[str, list[tuple[_MonthPay, Employee]]] = {}

    for payslip, employee in rows:
        if not Decimal(str(payslip.professional_tax)):
            continue
        state = await _state_of(session, user, employee.location_id)
        if state is None:
            # Slice 09 flags this on the run itself and refuses approval, so a
            # run that got this far has no unresolved states. Being defensive
            # here rather than silently filing them under a guessed state.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{employee.employee_code} has Professional Tax deducted but no "
                    "resolvable work state. Fix the record and recompute the run."
                ),
            )
        by_state.setdefault(state, []).append((payslip, employee))

    produced: list[GeneratedFile] = []
    for state, state_rows in sorted(by_state.items()):
        pt_rules = await _rule_set(session, user, "professional_tax", state, run.effective_date)
        registration = (entity.pt_registrations or {}).get(state)

        lines = [
            PtLine(
                employee_code=employee.employee_code,
                employee_name=employee.full_name,
                gross_wages=payslip.month_gross,
                tax_deducted=Decimal(str(payslip.professional_tax)),
            )
            for payslip, employee in state_rows
        ]
        to_validate = [
            {"subject": line.employee_code, "tax_deducted": line.tax_deducted} for line in lines
        ]
        result = validation.validate_pt_challan(
            state_code=state, registration_number=registration, rows=to_validate
        )

        if result.is_blocked:
            record = await _record(
                session,
                user,
                return_type=ReturnType.PROFESSIONAL_TAX,
                entity_id=legal_entity_id,
                period=period,
                jurisdiction=state,
                result=result,
                content=None,
                file_name=None,
                record_count=0,
                total_amount=Decimal(0),
                request=request,
            )
            raise FilingBlockedError(record, result)

        challan = PtChallan(
            state_code=state,
            registration_number=registration,
            period=period,
            lines=lines,
            slab_counts=_slab_counts(pt_rules, lines),
        )
        content = serialisers.serialise_pt_challan(challan)
        record = await _record(
            session,
            user,
            return_type=ReturnType.PROFESSIONAL_TAX,
            entity_id=legal_entity_id,
            period=period,
            jurisdiction=state,
            result=result,
            content=content,
            file_name=f"PT_{state}_{entity.code}_{month:02d}{year}.csv",
            record_count=len(lines),
            total_amount=challan.total_tax,
            request=request,
        )
        produced.append(GeneratedFile(content, record, result))

    return produced


def _slab_counts(pt_rules: PayrollRuleSet | None, lines: list[PtLine]) -> dict[str, int]:
    """How many employees fall in each of the state's own slabs.

    Several states want a count per slab rather than a per-employee list. The
    slabs come from the rule set, so the labels are the state's wording and no
    boundary is written here.
    """
    if pt_rules is None:
        return {}
    slabs = pt_rules.rules.get("slabs")
    if not isinstance(slabs, list):
        return {}

    counts: dict[str, int] = {}
    for line in lines:
        for slab in slabs:
            upper = slab.get("up_to")
            if upper is None or line.gross_wages <= Decimal(str(upper)):
                label = slab.get("label") or (f"up to {upper}" if upper is not None else "above")
                counts[label] = counts.get(label, 0) + 1
                break
    return counts


async def _state_of(session: AsyncSession, user: User, location_id: uuid.UUID | None) -> str | None:
    if location_id is None:
        return None
    return await session.scalar(
        tenant_scoped(select(Location.state_code).where(Location.id == location_id), Location, user)
    )


# ═══════════════════════════════════════════════════════════════════════════
# TDS 24Q
# ═══════════════════════════════════════════════════════════════════════════

# April-June is Q1, and so on. The financial year starts in April, so the
# quarter a month belongs to is not the calendar quarter.
_QUARTER_MONTHS = {1: (4, 5, 6), 2: (7, 8, 9), 3: (10, 11, 12), 4: (1, 2, 3)}


def quarter_months(quarter: int) -> tuple[int, ...]:
    if quarter not in _QUARTER_MONTHS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Q{quarter} is not a quarter. Use 1 to 4.",
        )
    return _QUARTER_MONTHS[quarter]


async def generate_tds_24q(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    financial_year: str,
    quarter: int,
    request: Request | None = None,
) -> GeneratedFile:
    """The quarterly TDS return.

    Generation refuses when any deduction in the quarter is not mapped to a
    challan. That is not a strictness preference: a 24Q with an unmapped
    deduction cannot be filed at all, and finding out from the portal costs a
    day that a filing deadline does not have.
    """
    entity = await _entity(session, user, legal_entity_id)
    months = quarter_months(quarter)
    year_start, year_end = statutory_service.financial_year_bounds(financial_year)

    # ── The quarter's payslips ──────────────────────────────────────────────
    payslip_rows = (
        await session.execute(
            tenant_scoped(
                select(Payslip, Employee, PayrollRun)
                .join(Employee, Payslip.employee_id == Employee.id)
                .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                .where(
                    PayrollRun.legal_entity_id == legal_entity_id,
                    PayrollRun.period_month.in_(months),
                    PayrollRun.effective_date >= year_start,
                    PayrollRun.effective_date <= year_end,
                    PayrollRun.status.in_([PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]),
                )
                .order_by(Employee.employee_code, PayrollRun.period_month),
                Payslip,
                user,
            )
        )
    ).all()

    # ── Challans and their mappings ─────────────────────────────────────────
    challans = (
        await session.scalars(
            tenant_scoped(
                select(TdsChallan)
                .where(
                    TdsChallan.legal_entity_id == legal_entity_id,
                    TdsChallan.financial_year == financial_year,
                    TdsChallan.quarter == quarter,
                    TdsChallan.form == "24Q",
                )
                .order_by(TdsChallan.deposit_date, TdsChallan.challan_serial),
                TdsChallan,
                user,
            )
        )
    ).all()

    mapping_rows = (
        await session.scalars(
            tenant_scoped(
                select(TdsChallanMapping).where(
                    TdsChallanMapping.challan_id.in_([c.id for c in challans])
                    if challans
                    else TdsChallanMapping.challan_id.is_(None)
                ),
                TdsChallanMapping,
                user,
            )
        )
    ).all()
    mapped_by_payslip = {m.payslip_id: m for m in mapping_rows}

    # ── What is not mapped ──────────────────────────────────────────────────
    unmapped: list[dict[str, Any]] = []
    deductee_rows: list[dict[str, Any]] = []
    deductees: list[TdsDeducteeLine] = []
    challan_position = {challan.id: index for index, challan in enumerate(challans, start=1)}

    serial = 0
    for payslip, employee, run in payslip_rows:
        tds = Decimal(str(payslip.tds))
        if not tds:
            continue

        mapping = mapped_by_payslip.get(payslip.id)
        if mapping is None:
            unmapped.append(
                {
                    "subject": employee.employee_code,
                    "amount": str(tds),
                    "period": f"{run.period_month:02d}/{run.period_year}",
                }
            )
            continue

        serial += 1
        deposited = Decimal(str(mapping.tds_amount))
        gross = Decimal(str(payslip.gross_earnings))
        deductees.append(
            TdsDeducteeLine(
                challan_serial_in_file=challan_position[mapping.challan_id],
                serial=serial,
                employee_code=employee.employee_code,
                pan=(employee.pan or "").upper(),
                name=employee.full_name,
                amount_paid=gross,
                tds_deducted=tds,
                tds_deposited=deposited,
                deduction_date=mapping.deduction_date,
                rate=(
                    (tds / gross * Decimal(100)).quantize(Decimal("0.01")) if gross else Decimal(0)
                ),
            )
        )
        deductee_rows.append(
            {
                "subject": employee.employee_code,
                "pan": employee.pan,
                "tds_deducted": tds,
                "tds_deposited": deposited,
            }
        )

    # ── Annexure II, Q4 only ────────────────────────────────────────────────
    salary_details: list[TdsSalaryDetail] = []
    if quarter == 4:
        salary_details = await _annexure_ii(session, user, legal_entity_id, financial_year)

    result = validation.validate_tds_return(
        tan=entity.tan,
        quarter=quarter,
        deductees=deductee_rows,
        unmapped_deductions=unmapped,
        salary_details=[{"pan": d.pan} for d in salary_details],
    )

    period = f"{financial_year}-Q{quarter}"
    if result.is_blocked:
        record = await _record(
            session,
            user,
            return_type=ReturnType.TDS_24Q,
            entity_id=legal_entity_id,
            period=period,
            jurisdiction=None,
            result=result,
            content=None,
            file_name=None,
            record_count=0,
            total_amount=Decimal(0),
            request=request,
        )
        raise FilingBlockedError(record, result)

    return_ = Tds24QReturn(
        tan=entity.tan or "",
        pan=entity.pan or "",
        financial_year=financial_year,
        quarter=quarter,
        deductor_name=entity.name,
        challans=[
            TdsChallanLine(
                serial=challan_position[challan.id],
                bsr_code=challan.bsr_code,
                deposit_date=challan.deposit_date,
                challan_serial=challan.challan_serial,
                tds_amount=Decimal(str(challan.tds_amount)),
                surcharge=Decimal(str(challan.surcharge)),
                education_cess=Decimal(str(challan.education_cess)),
                interest=Decimal(str(challan.interest)),
                late_fee=Decimal(str(challan.late_fee)),
                total_amount=Decimal(str(challan.total_amount)),
                is_book_entry=challan.is_book_entry,
            )
            for challan in challans
        ],
        deductees=deductees,
        salary_details=salary_details,
    )

    content = serialisers.serialise_tds_24q(return_)
    record = await _record(
        session,
        user,
        return_type=ReturnType.TDS_24Q,
        entity_id=legal_entity_id,
        period=period,
        jurisdiction=None,
        result=result,
        content=content,
        file_name=f"24Q_{entity.tan or entity.code}_{financial_year}_Q{quarter}.txt",
        record_count=len(deductees),
        total_amount=return_.total_tds,
        request=request,
    )
    return GeneratedFile(content, record, result)


async def _annexure_ii(
    session: AsyncSession, user: User, legal_entity_id: uuid.UUID, financial_year: str
) -> list[TdsSalaryDetail]:
    """The annual salary detail, from the year's tax snapshots.

    This is where slice 07's declarations and exemptions reach a statutory
    return. The latest snapshot for the year is used — it is the one computed
    against the proofs actually submitted.
    """
    rows = (
        await session.execute(
            tenant_scoped(
                select(TaxComputationSnapshot, Employee)
                .join(Employee, TaxComputationSnapshot.employee_id == Employee.id)
                .where(
                    TaxComputationSnapshot.financial_year == financial_year,
                    Employee.legal_entity_id == legal_entity_id,
                )
                .order_by(
                    Employee.employee_code,
                    TaxComputationSnapshot.created_at.desc(),
                ),
                TaxComputationSnapshot,
                user,
            )
        )
    ).all()

    latest: dict[uuid.UUID, tuple[TaxComputationSnapshot, Employee]] = {}
    for snapshot, employee in rows:
        latest.setdefault(employee.id, (snapshot, employee))

    details: list[TdsSalaryDetail] = []
    for snapshot, employee in latest.values():
        working = snapshot.working or {}
        details.append(
            TdsSalaryDetail(
                employee_code=employee.employee_code,
                pan=(employee.pan or "").upper(),
                name=employee.full_name,
                regime=snapshot.regime.value,
                gross_salary=Decimal(str(snapshot.gross_annual_income)),
                exempt_allowances=Decimal(str(snapshot.total_exemptions)),
                standard_deduction=Decimal(str(working.get("standard_deduction", 0))),
                income_from_salary=Decimal(str(snapshot.gross_annual_income))
                - Decimal(str(snapshot.total_exemptions)),
                other_income=Decimal(str(working.get("other_income", 0))),
                gross_total_income=Decimal(str(snapshot.gross_annual_income)),
                chapter_via_deductions=Decimal(str(snapshot.total_deductions)),
                taxable_income=Decimal(str(snapshot.taxable_income)),
                tax_payable=Decimal(str(snapshot.annual_tax_liability)),
                relief=Decimal(str(working.get("relief", 0))),
                total_tds_deducted=Decimal(str(snapshot.tds_already_deducted)),
            )
        )
    return details


# ═══════════════════════════════════════════════════════════════════════════
# The acknowledgement
# ═══════════════════════════════════════════════════════════════════════════


async def record_acknowledgement(
    session: AsyncSession,
    user: User,
    *,
    record_id: uuid.UUID,
    acknowledgement_number: str,
    filed_at: datetime | None = None,
    request: Request | None = None,
) -> FilingRecord:
    """Close the loop after a successful portal upload.

    Nothing in this system can compute an acknowledgement number, and it is the
    first thing an inspection asks for. It is typed in.
    """
    record = await session.scalar(
        tenant_scoped(select(FilingRecord).where(FilingRecord.id == record_id), FilingRecord, user)
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing record not found")
    if record.status is FilingStatus.BLOCKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This return was never generated — validation blocked it. There is "
                "nothing that could have been filed."
            ),
        )
    if record.acknowledgement_number:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This return is already acknowledged as {record.acknowledgement_number}. "
                "A correction is a fresh filing, not an edit of this record."
            ),
        )
    if record.status is FilingStatus.REJECTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The portal rejected this file"
                + (f" ({record.rejection_reason})" if record.rejection_reason else "")
                + ". Generate the return again and record the acknowledgement on the new file."
            ),
        )

    record.acknowledgement_number = acknowledgement_number
    record.filed_at = filed_at or datetime.now(UTC)
    record.filed_by_user_id = user.id
    record.status = FilingStatus.FILED
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="filing_record",
        entity_id=record.id,
        summary=(
            f"Recorded acknowledgement {acknowledgement_number} for "
            f"{record.return_type.value} {record.period}"
        ),
        changes={
            "acknowledgement_number": acknowledgement_number,
            "filed_at": record.filed_at.isoformat(),
        },
        request=request,
    )
    return record


async def record_rejection(
    session: AsyncSession,
    user: User,
    *,
    record_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> FilingRecord:
    """Record that the portal rejected a generated file, and why.

    Kept rather than deleted: the next attempt needs to know what was wrong. Only
    a generated file can be rejected: one the validation blocked was never
    produced, and one already acknowledged was accepted by the portal.
    """
    record = await session.scalar(
        tenant_scoped(select(FilingRecord).where(FilingRecord.id == record_id), FilingRecord, user)
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing record not found")
    if record.status is not FilingStatus.GENERATED:
        detail = {
            FilingStatus.BLOCKED: (
                "This return was never generated — validation blocked it — so the portal "
                "cannot have rejected it."
            ),
            FilingStatus.FILED: (
                f"This return is acknowledged as {record.acknowledgement_number}: the portal "
                "accepted it. A correction is a fresh filing."
            ),
            FilingStatus.REJECTED: (
                f"This file's rejection is already recorded: {record.rejection_reason}"
            ),
        }.get(record.status, f"A {record.status.value} return cannot be marked rejected.")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    record.status = FilingStatus.REJECTED
    record.rejection_reason = reason.strip()
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="filing_record",
        entity_id=record.id,
        summary=f"Portal rejected {record.return_type.value} {record.period}",
        changes={"rejection_reason": record.rejection_reason},
        request=request,
    )
    return record


# ═══════════════════════════════════════════════════════════════════════════
# Reading and downloading a filing record
# ═══════════════════════════════════════════════════════════════════════════


async def get_record(session: AsyncSession, user: User, record_id: uuid.UUID) -> FilingRecord:
    record = await session.scalar(
        tenant_scoped(select(FilingRecord).where(FilingRecord.id == record_id), FilingRecord, user)
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing record not found")
    return record


async def download_record(
    session: AsyncSession,
    user: User,
    record_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> tuple[bytes, str]:
    """The stored return file, exactly as generated. Every download is audited.

    The caller commits: the audit row and the download are one transaction.
    """
    record = await get_record(session, user, record_id)
    if record.status is FilingStatus.BLOCKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This return was blocked by validation, so no file exists.",
        )
    stored = await support.load_file(user.tenant_id, record.file_storage_key)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No stored file exists for this {record.return_type.value} return "
                f"({record.period}); it was generated before files were kept. Generate it "
                f"again and check it against the recorded hash {record.content_hash}."
            ),
        )
    data, _ = stored
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.EXPORT,
        entity_type="filing_record",
        entity_id=record.id,
        summary=f"Downloaded the {record.return_type.value} file for {record.period}",
        changes={"content_hash": record.content_hash},
        request=request,
    )
    return data, record.file_name or f"{record.return_type.value}_{record.period}.txt"
