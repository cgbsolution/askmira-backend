"""Taxable income that reaches TDS from outside the salary structure.

Four other modules produce figures that are salary income for tax and that the
projection would otherwise never see:

- **Gratuity** above the exemption ceiling, on a payout at exit (slice 08).
- **Leave encashment**, the taxable part as the policy's treatment computed it
  (slice 08).
- **Arrears** received this year — taxable in the year of receipt, with
  section 89(1) relief claimed separately (slice 12).
- **A concessional-loan perquisite** and **reimbursement entitlement left
  unclaimed at year end** (slice 14).

Each figure is read from the record its own module stored, never recomputed
here, so the projection and the source cannot disagree. Nothing in this file is
a rate or a ceiling: the exemption was applied by the module that owns it, and
the perquisite's notional rate and threshold come from an approved rule set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.arrears import ArrearBatch, ArrearLine, ArrearStatus
from app.models.exit_benefits import EncashmentRequest, EncashmentStatus, GratuityPayout
from app.models.loans import (
    LoanAccount,
    LoanStatus,
    ReimbursementClaim,
    ReimbursementComponent,
)
from app.models.payroll import PayrollRuleSet
from app.models.user import User
from app.services.attendance.service import local_today
from app.services.loans import amortisation, flexi, reimbursements

ZERO = Decimal(0)

#: The rule type an SME-approved loan-perquisite configuration is stored under:
#: `{"notional_rate": …, "exempt_threshold": …}`.
LOAN_PERQUISITE_RULE_TYPE = "loan_perquisite"

#: Income lines a payroll run pays itself, as input lines carrying their own
#: taxable amount (`services/payroll/inputs.py`). The run's TDS counts them in
#: the month they are paid, so it must not count them here as well — once as
#: projected income and again as pay would tax the same rupee twice. The
#: employee's own projection keeps them: it looks at the year, paid or not.
PAID_THROUGH_PAYROLL = frozenset({"ARREARS", "LEAVE_ENCASHMENT_IN_SERVICE"})


@dataclass(frozen=True, slots=True)
class IncomeLine:
    code: str
    label: str
    amount: Decimal
    source: str
    basis: str

    def as_tuple(self) -> tuple[str, str, Decimal, str, str]:
        return (self.code, self.label, self.amount, self.source, self.basis)


@dataclass(slots=True)
class OtherIncome:
    taxable: list[IncomeLine]
    pending: list[IncomeLine]
    notes: list[str]


def _amount(value: object) -> Decimal:
    return Decimal(str(value or ZERO))


def _long_year(start: date) -> str:
    """"2026-2027", the form the reimbursement module stores."""
    return f"{start.year}-{start.year + 1}"


MONTHS_IN_YEAR = 12


def _months_between(earlier: date, later: date) -> int:
    """Whole calendar months from `earlier`'s month to `later`'s month."""
    return (later.year - earlier.year) * MONTHS_IN_YEAR + later.month - earlier.month


def _next_month(on: date) -> date:
    if on.month == MONTHS_IN_YEAR:
        return date(on.year + 1, 1, 1)
    return date(on.year, on.month + 1, 1)


async def other_taxable_income(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    financial_year: str,
    *,
    start: date,
    end: date,
    today: date | None = None,
) -> OtherIncome:
    """Everything outside the salary structure that is taxable this year."""
    moment = today or await local_today(session, user)
    result = OtherIncome(taxable=[], pending=[], notes=[])

    await _gratuity(session, user, employee_id, start, end, result)
    await _encashment(session, user, employee_id, financial_year, result)
    await _arrears(session, user, employee_id, start, end, result)
    await _loan_perquisite(session, user, employee_id, start, end, result)
    await _unclaimed_reimbursements(session, user, employee_id, start, end, moment, result)
    return result


async def _gratuity(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    start: date,
    end: date,
    result: OtherIncome,
) -> None:
    payouts = (
        await session.scalars(
            tenant_scoped(
                select(GratuityPayout).where(
                    GratuityPayout.employee_id == employee_id,
                    GratuityPayout.exit_date >= start,
                    GratuityPayout.exit_date <= end,
                ),
                GratuityPayout,
                user,
            )
        )
    ).all()
    for payout in payouts:
        taxable = _amount(payout.taxable_amount)
        if taxable <= ZERO:
            continue
        result.taxable.append(
            IncomeLine(
                code="GRATUITY_TAXABLE",
                label="Gratuity above the exemption",
                amount=taxable,
                source="exit_benefits",
                basis=(
                    f"Gratuity of {_amount(payout.gross_amount)} on exit "
                    f"{payout.exit_date.isoformat()}: {_amount(payout.exempt_amount)} exempt "
                    f"up to the configured ceiling, {taxable} taxable."
                ),
            )
        )


async def _encashment(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    financial_year: str,
    result: OtherIncome,
) -> None:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(EncashmentRequest).where(
                    EncashmentRequest.employee_id == employee_id,
                    EncashmentRequest.financial_year == financial_year,
                    EncashmentRequest.status.in_(
                        [EncashmentStatus.APPROVED, EncashmentStatus.PAID]
                    ),
                ),
                EncashmentRequest,
                user,
            )
        )
    ).all()
    for row in rows:
        taxable = _amount(row.taxable_amount)
        if taxable <= ZERO:
            continue
        kind = "at exit" if row.is_exit_encashment else "in service"
        result.taxable.append(
            IncomeLine(
                code=(
                    "LEAVE_ENCASHMENT_TAXABLE"
                    if row.is_exit_encashment
                    else "LEAVE_ENCASHMENT_IN_SERVICE"
                ),
                label=f"Leave encashment {kind}",
                amount=taxable,
                source="exit_benefits",
                basis=(
                    f"{_amount(row.days_approved)} day(s) of {row.leave_type_code} at "
                    f"{_amount(row.daily_rate)}: {_amount(row.gross_amount)} gross, "
                    f"{_amount(row.exempt_amount)} exempt under the policy's treatment, "
                    f"{taxable} taxable."
                ),
            )
        )


async def _arrears(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    start: date,
    end: date,
    result: OtherIncome,
) -> None:
    """Arrears are taxable in the year they are received.

    Received means approved for payment: an approved batch is paid in the next
    run, and the TDS for it has to be levelled from then, not discovered after.
    """
    batches = (
        await session.scalars(
            tenant_scoped(
                select(ArrearBatch).where(
                    ArrearBatch.status.in_([ArrearStatus.APPROVED, ArrearStatus.PAID]),
                    ArrearBatch.approved_at.is_not(None),
                ),
                ArrearBatch,
                user,
            )
        )
    ).all()
    for batch in batches:
        received = batch.approved_at.date() if batch.approved_at else None
        if received is None or not (start <= received <= end):
            continue
        lines = (
            await session.scalars(
                tenant_scoped(
                    select(ArrearLine).where(
                        ArrearLine.batch_id == batch.id,
                        ArrearLine.employee_id == employee_id,
                        ArrearLine.component_type == "earning",
                    ),
                    ArrearLine,
                    user,
                )
            )
        ).all()
        total = sum((_amount(line.difference) for line in lines), start=ZERO)
        if total <= ZERO:
            continue
        result.taxable.append(
            IncomeLine(
                code="ARREARS",
                label="Salary arrears received",
                amount=total,
                source="arrears",
                basis=(
                    f"Earnings arrears for {batch.from_period} to {batch.to_period}, approved "
                    f"{received.isoformat()}. Taxable in the year of receipt; relief under "
                    "section 89(1) is computed separately with Form 10E."
                ),
            )
        )


async def _loan_perquisite(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    start: date,
    end: date,
    result: OtherIncome,
) -> None:
    loans = (
        await session.scalars(
            tenant_scoped(
                select(LoanAccount).where(
                    LoanAccount.employee_id == employee_id,
                    LoanAccount.status.in_(
                        [LoanStatus.DISBURSED, LoanStatus.ACTIVE, LoanStatus.CLOSED]
                    ),
                    LoanAccount.disbursed_at.is_not(None),
                ),
                LoanAccount,
                user,
            )
        )
    ).all()
    loans = [loan for loan in loans if loan.disbursed_at and loan.disbursed_at.date() <= end]
    if not loans:
        return

    rule_set = await session.scalar(
        tenant_scoped(
            select(PayrollRuleSet)
            .where(
                PayrollRuleSet.rule_type == LOAN_PERQUISITE_RULE_TYPE,
                PayrollRuleSet.effective_from <= start,
                or_(PayrollRuleSet.effective_to.is_(None), PayrollRuleSet.effective_to >= start),
            )
            .order_by(PayrollRuleSet.effective_from.desc()),
            PayrollRuleSet,
            user,
        )
    )
    if rule_set is None or not rule_set.is_approved:
        result.notes.append(
            "A company loan is outstanding, but no approved loan-perquisite rule set "
            "(notional rate and exempt threshold) is in force for this year, so no "
            "perquisite value is included. The Compliance SME supplies it."
        )
        return

    for loan in loans:
        outstanding, interest = _balances_in_year(loan, start, end)
        if not outstanding:
            continue
        try:
            perquisite = amortisation.concessional_loan_perquisite(
                outstanding_by_month=outstanding,
                actual_interest_charged=interest,
                config=rule_set.rules,
            )
        except amortisation.AmortisationError as exc:
            result.notes.append(f"The loan perquisite could not be valued: {exc}")
            continue
        if perquisite.is_perquisite:
            result.taxable.append(
                IncomeLine(
                    code="LOAN_PERQUISITE",
                    label="Concessional loan perquisite",
                    amount=perquisite.perquisite_value,
                    source="loans",
                    basis=perquisite.reason,
                )
            )


def _balances_in_year(
    loan: LoanAccount, start: date, end: date
) -> tuple[list[Decimal], Decimal]:
    """The month-by-month balance inside the year, and interest charged in it.

    Read from the schedule stored on the account — the same ledger payroll
    recovers against. Instalment `n` falls `n` months after disbursement.
    """
    schedule = loan.schedule or {}
    instalments = schedule.get("instalments") or []
    principal = _amount(loan.principal)
    disbursed = loan.disbursed_at.date() if loan.disbursed_at else start

    balance_after: dict[int, Decimal] = {
        int(item["number"]): _amount(item.get("closing_balance")) for item in instalments
    }
    interest_by_number: dict[int, Decimal] = {
        int(item["number"]): _amount(item.get("interest")) for item in instalments
    }

    balances: list[Decimal] = []
    interest = ZERO
    cursor = date(max(start, disbursed).year, max(start, disbursed).month, 1)
    while cursor <= end:
        elapsed = _months_between(disbursed, cursor)
        opening = principal if elapsed <= 0 else balance_after.get(elapsed, ZERO)
        if opening > ZERO:
            balances.append(opening)
        interest += interest_by_number.get(elapsed + 1, ZERO) if elapsed >= 0 else ZERO
        cursor = _next_month(cursor)
    return balances, interest


async def _unclaimed_reimbursements(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    start: date,
    end: date,
    today: date,
    result: OtherIncome,
) -> None:
    components = (
        await session.scalars(
            tenant_scoped(
                select(ReimbursementComponent).where(ReimbursementComponent.active.is_(True)),
                ReimbursementComponent,
                user,
            )
        )
    ).all()
    if not components:
        return

    year = _long_year(start)
    for component in components:
        claims = (
            await session.scalars(
                tenant_scoped(
                    select(ReimbursementClaim).where(
                        ReimbursementClaim.employee_id == employee_id,
                        ReimbursementClaim.component_code == component.code,
                        ReimbursementClaim.financial_year == year,
                    ),
                    ReimbursementClaim,
                    user,
                )
            )
        ).all()
        position = reimbursements.position(
            component_code=component.code,
            financial_year=year,
            entitlement=await flexi.entitlement_for(session, user, employee_id, year, component),
            claims=[
                {
                    "amount": _amount(claim.amount),
                    "accepted_amount": _amount(claim.accepted_amount),
                    "status": claim.status.value,
                }
                for claim in claims
            ],
        )
        if position.unclaimed <= ZERO:
            continue
        line = IncomeLine(
            code="REIMBURSEMENT_UNCLAIMED",
            label=f"Unclaimed {component.name}",
            amount=position.unclaimed,
            source="reimbursements",
            basis=(
                f"{position.unclaimed} of the {position.entitlement} {component.name} "
                f"entitlement for {year} has no accepted bill"
                + (
                    ", and the year has ended, so it is taxable salary."
                    if today > end
                    else ". It becomes taxable salary if still unclaimed when the year ends."
                )
            ),
        )
        (result.taxable if today > end else result.pending).append(line)
