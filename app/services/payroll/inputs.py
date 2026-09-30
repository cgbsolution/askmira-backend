"""Everything that reaches a payslip from outside the salary structure.

═══════════════════════════════════════════════════════════════════════════════
PAID ONCE, AND TRACEABLE TO WHERE IT CAME FROM
═══════════════════════════════════════════════════════════════════════════════

Loan EMIs, salary reimbursements, approved expense claims, leave encashment,
statutory bonus, arrears, reward perquisites, referral bonuses and travel-advance
recoveries and balances are each owned by another module. Payroll does not
re-decide any of them: it reads their tables, and each provider turns what is
due into lines on a payslip.

Two phases, deliberately separate:

- **collect**, at compute. Read-only. A run can be recomputed any number of
  times before approval, so nothing is marked here — marking at compute would
  leave a loan instalment "recovered" by a run that was never approved.
- **consume**, at approval, in the approval's transaction. Each provider marks
  the source record with the run (`paid_in_run_id`, `payroll_reference`, a
  `LoanRepayment` row) and first checks nothing else got there first. If another
  run consumed the same record in between, approval refuses and asks for a
  recompute rather than paying it twice.

Every line carries a `source` naming the record it consumed, so a payslip line
answers "where did this come from" without anyone reading code.

Where a module's tables do not hold what a provider needs, the provider says
**not available** and adds nothing. It does not guess.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, ClassVar

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.arrears import ArrearBatch, ArrearLine, ArrearStatus
from app.models.exit_benefits import EncashmentRequest, EncashmentStatus
from app.models.expenses import (
    AdvanceSettlement,
    AdvanceStatusEnum,
    ExpenseClaim,
    ExpenseClaimStatusEnum,
    SettlementOutcomeEnum,
    TravelAdvance,
)
from app.models.loans import (
    ClaimStatus,
    InterestType,
    LoanAccount,
    LoanRepayment,
    LoanScheme,
    LoanStatus,
    ReimbursementClaim,
    ReimbursementComponent,
)
from app.models.payroll import BonusComputation, BonusStatus, PayrollRun
from app.models.recruiting import PayoutStatusEnum, Referral, ReferralPayout
from app.models.rewards import RewardEvent
from app.models.user import User
from app.services.loans.reimbursements import ReimbursementError, accepted_for_payment
from app.services.payroll.engine import InputKind, InputLine, LoanRecoveryInput

__all__ = [
    "PROVIDERS",
    "CollectedInputs",
    "InputConsumedElsewhereError",
    "ProviderReport",
    "RunScope",
    "advance_refund_line",
    "collect_inputs",
    "consume_inputs",
    "expense_claim_line",
]

ZERO = Decimal(0)


def _d(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


class InputConsumedElsewhereError(Exception):
    """A source record changed hands between compute and approval."""


@dataclass(frozen=True, slots=True)
class RunScope:
    """What a provider needs to know about the run it is feeding."""

    run: PayrollRun
    period_start: date
    period_end: date
    # "2026-12", the loan ledger's period key.
    period: str
    employee_ids: frozenset[uuid.UUID]
    fy_start: date
    fy_end: date
    # Input lines already on approved payslips earlier in this financial year,
    # per employee — what a cumulative source (reward perquisites) has already
    # handed over.
    prior_input_lines: dict[uuid.UUID, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderReport:
    key: str
    label: str
    # included | nothing_due | not_available | blocked
    status: str
    message: str
    item_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "item_count": self.item_count,
        }


@dataclass(slots=True)
class ProviderOutput:
    lines: dict[uuid.UUID, list[InputLine]] = field(default_factory=lambda: defaultdict(list))
    loans: dict[uuid.UUID, list[LoanRecoveryInput]] = field(
        default_factory=lambda: defaultdict(list)
    )
    notices: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CollectedInputs:
    lines: dict[uuid.UUID, list[InputLine]]
    loans: dict[uuid.UUID, list[LoanRecoveryInput]]
    reports: list[ProviderReport]
    # [{"key": provider key, "label": provider label, "message": ...}]
    notices: list[dict[str, str]]


class InputProvider:
    """One source of payslip lines. Subclasses implement `collect` and `consume`."""

    key: ClassVar[str]
    label: ClassVar[str]
    source_type: ClassVar[str]

    async def collect(
        self, session: AsyncSession, user: User, scope: RunScope
    ) -> ProviderOutput:  # pragma: no cover - interface
        raise NotImplementedError

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def report(self, output: ProviderOutput) -> ProviderReport:
        count = sum(len(v) for v in output.lines.values()) + sum(
            len(v) for v in output.loans.values()
        )
        return ProviderReport(
            key=self.key,
            label=self.label,
            status="included" if count else "nothing_due",
            message=(
                f"{count} item(s) included in this run."
                if count
                else "Nothing is due from this source for this period."
            ),
            item_count=count,
        )


def _reference(run: PayrollRun) -> str:
    return f"payroll_run:{run.id}"


def _conflict(what: str) -> InputConsumedElsewhereError:
    return InputConsumedElsewhereError(
        f"{what} was paid or changed by something else after this run was computed. "
        "Recompute the run so it pays what is actually still due."
    )


# ── Loan EMIs ───────────────────────────────────────────────────────────────


class LoanEmiProvider(InputProvider):
    """Instalments on disbursed loans, recovered against the scheme's net-pay floor.

    The floor decision is `app.services.loans.recovery.plan_recovery`, applied
    by the engine once net pay is known. A skipped instalment is still recorded
    at approval — as a skipped `LoanRepayment` with its reason — because "why
    was nothing deducted in July" is a question the borrower will ask.
    """

    key = "loan_emi"
    label = "Loan EMI recovery"
    source_type = "loan_account"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        output = ProviderOutput()
        if not scope.employee_ids:
            return output

        rows = (
            await session.execute(
                tenant_scoped(
                    select(LoanAccount, LoanScheme)
                    .join(LoanScheme, LoanAccount.scheme_id == LoanScheme.id)
                    .where(
                        LoanAccount.employee_id.in_(scope.employee_ids),
                        LoanAccount.status.in_([LoanStatus.DISBURSED, LoanStatus.ACTIVE]),
                        LoanAccount.emi_amount > 0,
                        LoanAccount.outstanding_principal > 0,
                    ),
                    LoanAccount,
                    user,
                )
            )
        ).all()
        if not rows:
            return output

        already = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(LoanRepayment.loan_account_id).where(
                            LoanRepayment.period == scope.period,
                            LoanRepayment.is_prepayment.is_(False),
                            LoanRepayment.loan_account_id.in_([loan.id for loan, _ in rows]),
                        ),
                        LoanRepayment,
                        user,
                    )
                )
            ).all()
        )

        for loan, scheme in rows:
            if loan.id in already:
                output.notices.append(
                    f"Loan {loan.id}: an instalment for {scope.period} is already on the "
                    "ledger, so this run does not recover another."
                )
                continue
            if loan.disbursed_at is not None and loan.disbursed_at.date() > scope.period_end:
                continue
            output.loans[loan.employee_id].append(
                LoanRecoveryInput(
                    provider=self.key,
                    code="LOAN_EMI",
                    name=f"{scheme.name} instalment",
                    emi=_d(loan.emi_amount),
                    outstanding_principal=_d(loan.outstanding_principal),
                    carried_forward=_d(loan.carried_forward),
                    minimum_net_pay=_d(scheme.minimum_net_pay),
                    allow_partial=bool(scheme.allow_partial_recovery),
                    source={
                        "type": self.source_type,
                        "id": str(loan.id),
                        "period": scope.period,
                    },
                )
            )
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        source = line.get("source") or {}
        recovery = line.get("recovery") or {}
        loan = await session.scalar(
            tenant_scoped(
                select(LoanAccount).where(LoanAccount.id == uuid.UUID(source["id"])),
                LoanAccount,
                user,
            )
        )
        if loan is None or loan.status not in (LoanStatus.DISBURSED, LoanStatus.ACTIVE):
            raise _conflict("A loan on this run")

        period = source["period"]
        existing = await session.scalar(
            tenant_scoped(
                select(LoanRepayment.id).where(
                    LoanRepayment.loan_account_id == loan.id,
                    LoanRepayment.period == period,
                    LoanRepayment.is_prepayment.is_(False),
                ),
                LoanRepayment,
                user,
            )
        )
        if existing is not None:
            raise _conflict(f"The {period} instalment on loan {loan.id}")

        amount = _d(recovery.get("amount"))
        outstanding = _d(loan.outstanding_principal)
        interest = ZERO
        if amount > ZERO and loan.interest_type is not InterestType.INTEREST_FREE:
            interest = await self._interest_due(session, user, loan, amount)
        principal = min(amount - interest, outstanding)
        closing = outstanding - principal

        session.add(
            LoanRepayment(
                tenant_id=user.tenant_id,
                loan_account_id=loan.id,
                payroll_run_id=run.id,
                period=period,
                amount=amount,
                principal_component=principal,
                interest_component=interest,
                closing_balance=closing,
                is_skipped=bool(recovery.get("is_skipped")),
                reason=str(recovery.get("reason") or line.get("basis") or ""),
            )
        )
        loan.outstanding_principal = closing
        loan.carried_forward = _d(recovery.get("carried_forward"))
        if loan.status is LoanStatus.DISBURSED and amount > ZERO:
            loan.status = LoanStatus.ACTIVE
        if closing == ZERO:
            loan.status = LoanStatus.CLOSED
            loan.closed_at = datetime.now(UTC)

    @staticmethod
    async def _interest_due(
        session: AsyncSession, user: User, loan: LoanAccount, amount: Decimal
    ) -> Decimal:
        """The interest inside a recovery, read off the loan's stored schedule.

        The next unpaid instalment is the count of recoveries recorded since the
        schedule was last rebuilt (a prepayment rebuilds it). A recovery that
        covers carried-forward instalments walks forward through them. Payroll
        does not re-derive interest from the rate: the schedule is the ledger
        the borrower was shown.
        """
        repayments = (
            await session.scalars(
                tenant_scoped(
                    select(LoanRepayment)
                    .where(LoanRepayment.loan_account_id == loan.id)
                    .order_by(LoanRepayment.created_at),
                    LoanRepayment,
                    user,
                )
            )
        ).all()
        paid_since_rebuild = 0
        for row in repayments:
            if row.is_prepayment:
                paid_since_rebuild = 0
            elif not row.is_skipped:
                paid_since_rebuild += 1

        instalments = (loan.schedule or {}).get("instalments") or []
        remaining = amount
        interest = ZERO
        for instalment in instalments[paid_since_rebuild:]:
            if remaining <= ZERO:
                break
            portion = min(remaining, _d(instalment.get("emi")))
            interest += min(portion, _d(instalment.get("interest")))
            remaining -= portion
        return min(interest, amount)


# ── Salary reimbursements ───────────────────────────────────────────────────


class ReimbursementProvider(InputProvider):
    """Verified reimbursement-component claims, paid exempt up to the entitlement.

    Salary reimbursements only. An expense claim is business expenditure, paid
    at cost by `ExpenseClaimProvider` and never against an entitlement here.
    """

    key = "reimbursements"
    label = "Salary reimbursements"
    source_type = "reimbursement_claim"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        output = ProviderOutput()
        if not scope.employee_ids:
            return output

        claims = (
            await session.scalars(
                tenant_scoped(
                    select(ReimbursementClaim)
                    .where(
                        ReimbursementClaim.employee_id.in_(scope.employee_ids),
                        ReimbursementClaim.status.in_([ClaimStatus.VERIFIED, ClaimStatus.PARTIAL]),
                        ReimbursementClaim.paid_in_run_id.is_(None),
                        ReimbursementClaim.accepted_amount > 0,
                    )
                    .order_by(ReimbursementClaim.created_at),
                    ReimbursementClaim,
                    user,
                )
            )
        ).all()
        if not claims:
            return output

        components = {
            row.code: row
            for row in (
                await session.scalars(
                    tenant_scoped(select(ReimbursementComponent), ReimbursementComponent, user)
                )
            ).all()
        }

        paid_rows = (
            await session.scalars(
                tenant_scoped(
                    select(ReimbursementClaim).where(
                        ReimbursementClaim.employee_id.in_({c.employee_id for c in claims}),
                        ReimbursementClaim.paid_in_run_id.is_not(None),
                    ),
                    ReimbursementClaim,
                    user,
                )
            )
        ).all()
        used: dict[tuple[uuid.UUID, str, str], Decimal] = defaultdict(lambda: ZERO)
        for row in paid_rows:
            used[(row.employee_id, row.component_code, row.financial_year)] += _d(
                row.accepted_amount
            )

        for claim in claims:
            component = components.get(claim.component_code)
            if component is None:
                output.notices.append(
                    f"Reimbursement claim {claim.id} is for component "
                    f"'{claim.component_code}', which does not exist; it is not paid."
                )
                continue
            key = (claim.employee_id, claim.component_code, claim.financial_year)
            remaining = max(ZERO, _d(component.annual_entitlement) - used[key])
            try:
                payable, why = accepted_for_payment(
                    claim_amount=_d(claim.amount),
                    accepted_amount=_d(claim.accepted_amount),
                    remaining_entitlement=remaining,
                    status=claim.status.value,
                )
            except ReimbursementError as exc:
                output.notices.append(f"Reimbursement claim {claim.id}: {exc}")
                continue
            if payable <= ZERO:
                output.notices.append(
                    f"Reimbursement claim {claim.id}: nothing is payable — {why}"
                )
                continue
            used[key] += payable
            output.lines[claim.employee_id].append(
                InputLine(
                    provider=self.key,
                    code=f"REIMB_{claim.component_code}"[:32],
                    name=component.name,
                    kind=InputKind.REIMBURSEMENT,
                    amount=payable,
                    basis=why,
                    source={"type": self.source_type, "id": str(claim.id)},
                )
            )
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        claim = await session.scalar(
            tenant_scoped(
                select(ReimbursementClaim).where(
                    ReimbursementClaim.id == uuid.UUID(line["source"]["id"])
                ),
                ReimbursementClaim,
                user,
            )
        )
        if claim is None or claim.paid_in_run_id not in (None, run.id):
            raise _conflict("A reimbursement claim on this run")
        claim.paid_in_run_id = run.id


# ── Leave encashment ────────────────────────────────────────────────────────


class EncashmentProvider(InputProvider):
    """Approved in-service encashment. Exit encashment is paid through Full & Final."""

    key = "leave_encashment"
    label = "Leave encashment"
    source_type = "encashment_request"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        output = ProviderOutput()
        if not scope.employee_ids:
            return output
        rows = (
            await session.scalars(
                tenant_scoped(
                    select(EncashmentRequest).where(
                        EncashmentRequest.employee_id.in_(scope.employee_ids),
                        EncashmentRequest.status == EncashmentStatus.APPROVED,
                        EncashmentRequest.is_exit_encashment.is_(False),
                        EncashmentRequest.paid_in_run_id.is_(None),
                        EncashmentRequest.gross_amount > 0,
                    ),
                    EncashmentRequest,
                    user,
                )
            )
        ).all()
        for row in rows:
            gross = _d(row.gross_amount)
            taxable = min(_d(row.taxable_amount), gross)
            output.lines[row.employee_id].append(
                InputLine(
                    provider=self.key,
                    code="LEAVE_ENCASHMENT",
                    name=f"Leave encashment ({row.leave_type_code})",
                    kind=InputKind.EARNING,
                    amount=gross,
                    taxable_amount=taxable,
                    basis=(
                        f"{row.days_approved} day(s) at {row.daily_rate} a day, "
                        f"{row.financial_year}; {taxable} taxable."
                    ),
                    source={"type": self.source_type, "id": str(row.id)},
                )
            )
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        row = await session.scalar(
            tenant_scoped(
                select(EncashmentRequest).where(
                    EncashmentRequest.id == uuid.UUID(line["source"]["id"])
                ),
                EncashmentRequest,
                user,
            )
        )
        if (
            row is None
            or row.paid_in_run_id not in (None, run.id)
            or row.status not in (EncashmentStatus.APPROVED, EncashmentStatus.PAID)
        ):
            raise _conflict("A leave encashment on this run")
        row.paid_in_run_id = run.id
        row.status = EncashmentStatus.PAID


# ── Statutory bonus ─────────────────────────────────────────────────────────


class StatutoryBonusProvider(InputProvider):
    """Approved statutory bonus that HR chose to pay inside this run.

    Bonus is usually its own payment. It rides a run only when someone
    scheduled it into one (Statutory → Bonus → pay in a payroll run), which sets
    `paid_in_run_id` while the row stays APPROVED; this run pays it and marks it
    PAID at approval. Nothing approved is swept into a run uninvited.
    """

    key = "statutory_bonus"
    label = "Statutory bonus"
    source_type = "bonus_computation"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        output = ProviderOutput()
        rows = (
            await session.scalars(
                tenant_scoped(
                    select(BonusComputation).where(
                        BonusComputation.paid_in_run_id == scope.run.id,
                        BonusComputation.status == BonusStatus.APPROVED,
                        BonusComputation.is_eligible.is_(True),
                        BonusComputation.amount > 0,
                    ),
                    BonusComputation,
                    user,
                )
            )
        ).all()
        for row in rows:
            if row.employee_id not in scope.employee_ids:
                output.notices.append(
                    f"Statutory bonus {row.id} for {row.financial_year} is scheduled into this "
                    "run, but its employee is not paid by it; it stays unpaid."
                )
                continue
            output.lines[row.employee_id].append(
                InputLine(
                    provider=self.key,
                    code="STATUTORY_BONUS",
                    name=f"Statutory bonus {row.financial_year}",
                    kind=InputKind.EARNING,
                    amount=_d(row.amount),
                    taxable_amount=_d(row.amount),
                    basis=(
                        f"{row.percentage}% of eligible wage {row.eligible_wage}, "
                        f"{row.days_worked} of {row.total_working_days} days worked."
                    ),
                    source={"type": self.source_type, "id": str(row.id)},
                )
            )
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        row = await session.scalar(
            tenant_scoped(
                select(BonusComputation).where(
                    BonusComputation.id == uuid.UUID(line["source"]["id"])
                ),
                BonusComputation,
                user,
            )
        )
        if row is None or row.paid_in_run_id != run.id or row.status is not BonusStatus.APPROVED:
            raise _conflict("A statutory bonus on this run")
        row.status = BonusStatus.PAID
        if run.payment_date is not None:
            row.paid_on = run.payment_date
        else:
            from app.services.attendance.service import local_today

            row.paid_on = await local_today(session, user)


# ── Arrears ─────────────────────────────────────────────────────────────────


class ArrearsProvider(InputProvider):
    """Approved arrear batches for this legal entity, paid in this run.

    A batch is paid whole or not at all: if anyone in it is not paid by this
    run, the batch waits and the run says why. Employee-side statutory arrears
    reduce the net; employer-side ones are employer cost only.
    """

    key = "arrears"
    label = "Arrears"
    source_type = "arrear_batch"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        output = ProviderOutput()
        batches = (
            await session.scalars(
                tenant_scoped(
                    select(ArrearBatch).where(
                        ArrearBatch.status == ArrearStatus.APPROVED,
                        ArrearBatch.paid_in_run_id.is_(None),
                        ArrearBatch.legal_entity_id == scope.run.legal_entity_id,
                    ),
                    ArrearBatch,
                    user,
                )
            )
        ).all()
        if not batches:
            return output

        lines = (
            await session.scalars(
                tenant_scoped(
                    select(ArrearLine).where(ArrearLine.batch_id.in_([b.id for b in batches])),
                    ArrearLine,
                    user,
                )
            )
        ).all()
        by_batch: dict[uuid.UUID, list[ArrearLine]] = defaultdict(list)
        for arrear_line in lines:
            by_batch[arrear_line.batch_id].append(arrear_line)

        for batch in batches:
            batch_lines = by_batch.get(batch.id, [])
            people = {line.employee_id for line in batch_lines}
            outside = people - scope.employee_ids
            if outside:
                output.notices.append(
                    f"Arrear batch {batch.from_period} to {batch.to_period} ({batch.id}) is "
                    f"not paid in this run: {len(outside)} of its employees are not paid by "
                    "it, and a batch is paid whole."
                )
                continue

            source = {
                "type": self.source_type,
                "id": str(batch.id),
                "periods": f"{batch.from_period} to {batch.to_period}",
            }
            for employee_id in sorted(people, key=str):
                mine = [line for line in batch_lines if line.employee_id == employee_id]
                earnings = sum(
                    (_d(x.difference) for x in mine if x.component_type == "earning"), start=ZERO
                )
                deductions = sum(
                    (_d(x.difference) for x in mine if x.component_type == "deduction"),
                    start=ZERO,
                )
                employer = sum(
                    (
                        _d(x.difference)
                        for x in mine
                        if x.component_type == "employer_contribution"
                    ),
                    start=ZERO,
                )
                basis = (
                    f"Arrears for {batch.from_period} to {batch.to_period}, "
                    f"{len({x.period for x in mine})} period(s)."
                )
                emitted = output.lines[employee_id]
                if earnings > ZERO:
                    emitted.append(
                        InputLine(self.key, "ARREARS", "Salary arrears", InputKind.EARNING,
                                  earnings, earnings, basis, source)
                    )
                elif earnings < ZERO:
                    emitted.append(
                        InputLine(self.key, "ARREARS_RECOVERY", "Salary arrears recovered",
                                  InputKind.DEDUCTION, -earnings, ZERO, basis, source)
                    )
                if deductions > ZERO:
                    emitted.append(
                        InputLine(self.key, "ARREARS_STATUTORY", "Statutory arrears (employee)",
                                  InputKind.DEDUCTION, deductions, ZERO, basis, source)
                    )
                elif deductions < ZERO:
                    emitted.append(
                        InputLine(self.key, "ARREARS_STATUTORY_REFUND",
                                  "Statutory arrears refunded (employee)",
                                  InputKind.REIMBURSEMENT, -deductions, ZERO, basis, source)
                    )
                if employer != ZERO:
                    emitted.append(
                        InputLine(self.key, "ARREARS_EMPLOYER", "Statutory arrears (employer)",
                                  InputKind.EMPLOYER_CONTRIBUTION, employer, ZERO, basis, source)
                    )
                if not emitted:
                    output.lines.pop(employee_id, None)
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        batch = await session.scalar(
            tenant_scoped(
                select(ArrearBatch).where(ArrearBatch.id == uuid.UUID(line["source"]["id"])),
                ArrearBatch,
                user,
            )
        )
        if batch is None:
            raise _conflict("An arrear batch on this run")
        if batch.paid_in_run_id == run.id:
            return  # already marked by another employee's line in this approval
        if batch.paid_in_run_id is not None or batch.status is not ArrearStatus.APPROVED:
            raise _conflict(f"Arrear batch {batch.id}")
        batch.paid_in_run_id = run.id
        batch.status = ArrearStatus.PAID


# ── Reward perquisites ──────────────────────────────────────────────────────


class RewardPerquisiteProvider(InputProvider):
    """The taxable value of rewards, as a non-cash perquisite that raises TDS.

    The rewards module values it cumulatively per financial year and refuses a
    figure from a rule no tax advisor has reviewed. Payroll takes the increase
    since the last approved run, and leaves an unreviewed figure out — visible
    as a blocked source, never silently applied to a payslip.
    """

    key = "reward_perquisites"
    label = "Reward perquisites"
    source_type = "reward_events"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        from app.services.rewards import service as rewards_service
        from app.services.rewards.tax import financial_year as rewards_financial_year

        output = ProviderOutput()
        if not scope.employee_ids:
            return output
        fy = rewards_financial_year(scope.period_end)
        events = (
            await session.scalars(
                tenant_scoped(
                    select(RewardEvent).where(
                        RewardEvent.employee_id.in_(scope.employee_ids),
                        RewardEvent.payroll_reference.is_(None),
                        RewardEvent.financial_year == fy,
                        RewardEvent.given_on <= scope.period_end,
                    ),
                    RewardEvent,
                    user,
                )
            )
        ).all()
        by_employee: dict[uuid.UUID, list[RewardEvent]] = defaultdict(list)
        for event in events:
            by_employee[event.employee_id].append(event)

        for employee_id, mine in by_employee.items():
            try:
                result = await rewards_service.perquisites(
                    session,
                    user,
                    employee_id=employee_id,
                    financial_year=fy,
                    as_of=scope.period_end,
                )
            except HTTPException as exc:
                output.notices.append(
                    f"Reward perquisites for employee {employee_id} are not included: "
                    f"{exc.detail}"
                )
                continue

            cumulative = sum(
                (_d(a.get("taxable_value")) for a in result.get("assessments", [])), start=ZERO
            )
            already = sum(
                (
                    _d(line.get("taxable_amount"))
                    for line in scope.prior_input_lines.get(employee_id, [])
                    if (line.get("source") or {}).get("type") == self.source_type
                ),
                start=ZERO,
            )
            increase = cumulative - already
            if increase <= ZERO:
                continue
            output.lines[employee_id].append(
                InputLine(
                    provider=self.key,
                    code="REWARD_PERQUISITE",
                    name="Reward perquisite (non-cash)",
                    kind=InputKind.PERQUISITE,
                    amount=increase,
                    taxable_amount=increase,
                    basis=(
                        f"Taxable reward value for {fy} is {cumulative}; {already} was taxed "
                        f"in earlier runs, so {increase} is added to taxable income. It is "
                        "not paid in cash."
                    ),
                    source={
                        "type": self.source_type,
                        "ids": sorted(str(event.id) for event in mine),
                    },
                )
            )
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        ids = [uuid.UUID(value) for value in line["source"].get("ids", [])]
        events = (
            await session.scalars(
                tenant_scoped(select(RewardEvent).where(RewardEvent.id.in_(ids)), RewardEvent, user)
            )
        ).all()
        reference = _reference(run)
        if len(events) != len(ids):
            raise _conflict("A reward on this run")
        for event in events:
            if event.payroll_reference not in (None, reference):
                raise _conflict(f"Reward {event.id}")
            event.payroll_reference = reference


# ── Referral bonuses ────────────────────────────────────────────────────────


class ReferralBonusProvider(InputProvider):
    """Referral milestones that have fallen due, paid to the referrer."""

    key = "referral_bonuses"
    label = "Referral bonuses"
    source_type = "referral_payout"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        output = ProviderOutput()
        if not scope.employee_ids:
            return output
        rows = (
            await session.execute(
                tenant_scoped(
                    select(ReferralPayout, Referral)
                    .join(Referral, ReferralPayout.referral_id == Referral.id)
                    .where(
                        ReferralPayout.status == PayoutStatusEnum.SCHEDULED,
                        ReferralPayout.payroll_reference.is_(None),
                        ReferralPayout.payable_on <= scope.period_end,
                        Referral.referrer_employee_id.in_(scope.employee_ids),
                    ),
                    ReferralPayout,
                    user,
                )
            )
        ).all()
        for payout, referral in rows:
            amount = _d(payout.amount)
            output.lines[referral.referrer_employee_id].append(
                InputLine(
                    provider=self.key,
                    code="REFERRAL_BONUS",
                    name=f"Referral bonus: {payout.milestone_label}",
                    kind=InputKind.EARNING,
                    amount=amount,
                    taxable_amount=amount,
                    basis=f"Milestone '{payout.milestone_label}' payable on {payout.payable_on}.",
                    source={"type": self.source_type, "id": str(payout.id)},
                )
            )
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        payout = await session.scalar(
            tenant_scoped(
                select(ReferralPayout).where(ReferralPayout.id == uuid.UUID(line["source"]["id"])),
                ReferralPayout,
                user,
            )
        )
        if (
            payout is None
            or payout.status is not PayoutStatusEnum.SCHEDULED
            or payout.payroll_reference is not None
        ):
            raise _conflict("A referral payout on this run")
        payout.status = PayoutStatusEnum.PAID
        payout.payroll_reference = _reference(run)
        payout.paid_at = datetime.now(UTC)


# ── Travel advance recoveries ───────────────────────────────────────────────


class AdvanceRecoveryProvider(InputProvider):
    """Unspent travel advances the settlement said to recover through payroll.

    Taken only when it fits inside net pay. One that does not is left unconsumed
    and attempted again next run, rather than part-recovered with nobody noting
    the part.
    """

    key = "advance_recoveries"
    label = "Travel advance recoveries"
    source_type = "advance_settlement"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        output = ProviderOutput()
        if not scope.employee_ids:
            return output
        rows = (
            await session.execute(
                tenant_scoped(
                    select(AdvanceSettlement, TravelAdvance)
                    .join(TravelAdvance, AdvanceSettlement.advance_id == TravelAdvance.id)
                    .where(
                        AdvanceSettlement.outcome
                        == SettlementOutcomeEnum.RECOVERY_DUE_FROM_EMPLOYEE,
                        AdvanceSettlement.recovery_amount > 0,
                        AdvanceSettlement.payroll_recovery_ref.is_(None),
                        AdvanceSettlement.settled_on <= scope.period_end,
                        TravelAdvance.employee_id.in_(scope.employee_ids),
                    ),
                    AdvanceSettlement,
                    user,
                )
            )
        ).all()
        for settlement, advance in rows:
            output.lines[advance.employee_id].append(
                InputLine(
                    provider=self.key,
                    code="ADVANCE_RECOVERY",
                    name="Travel advance recovery",
                    kind=InputKind.RECOVERY,
                    amount=_d(settlement.recovery_amount),
                    basis=(
                        f"Advance of {settlement.advance_amount} settled on "
                        f"{settlement.settled_on} against claims of {settlement.claims_total}."
                    ),
                    source={"type": self.source_type, "id": str(settlement.id)},
                )
            )
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        settlement = await session.scalar(
            tenant_scoped(
                select(AdvanceSettlement).where(
                    AdvanceSettlement.id == uuid.UUID(line["source"]["id"])
                ),
                AdvanceSettlement,
                user,
            )
        )
        if settlement is None or settlement.payroll_recovery_ref is not None:
            raise _conflict("A travel advance recovery on this run")
        settlement.payroll_recovery_ref = _reference(run)


# ── Expense reimbursements ──────────────────────────────────────────────────


def expense_claim_line(
    *,
    claim_id: uuid.UUID,
    category_key: str,
    amount: Decimal,
    incurred_on: date,
    original_amount: Decimal | None = None,
    original_currency: str = "INR",
) -> InputLine:
    """One approved expense claim as a payslip line: paid at cost, never taxed.

    Business expenditure reimbursed at what it cost is not income, so the line
    carries no taxable amount — unlike a salary reimbursement component, which
    is exempt only against a bill and up to an entitlement.
    """
    if not isinstance(amount, Decimal):
        raise TypeError("An expense reimbursement must be a Decimal, not a float")
    label = category_key.replace("_", " ").strip().capitalize() or "Expense"
    spent = (
        f" ({original_currency} {original_amount} converted)"
        if original_amount is not None and original_currency.upper() != "INR"
        else ""
    )
    return InputLine(
        provider=ExpenseClaimProvider.key,
        code="EXPENSE_REIMB",
        name=f"Expense reimbursement: {label}",
        kind=InputKind.REIMBURSEMENT,
        amount=amount,
        taxable_amount=ZERO,
        basis=(
            f"Approved {label.lower()} claim of {amount}{spent}, incurred "
            f"{incurred_on:%d %b %Y}. Business expenditure paid at cost; not taxable."
        ),
        source={"type": ExpenseClaimProvider.source_type, "id": str(claim_id)},
    )


def advance_refund_line(
    *,
    settlement_id: uuid.UUID,
    refund_amount: Decimal,
    advance_amount: Decimal,
    claims_total: Decimal,
    settled_on: date,
) -> InputLine:
    """A trip that cost more than its advance: the balance owed to the employee."""
    if not isinstance(refund_amount, Decimal):
        raise TypeError("An advance refund must be a Decimal, not a float")
    return InputLine(
        provider=AdvanceRefundProvider.key,
        code="ADVANCE_REFUND",
        name="Travel advance: balance due to you",
        kind=InputKind.REIMBURSEMENT,
        amount=refund_amount,
        taxable_amount=ZERO,
        basis=(
            f"Approved trip claims of {claims_total} exceeded the {advance_amount} advance; "
            f"settled on {settled_on:%d %b %Y}. Not taxable."
        ),
        source={"type": AdvanceRefundProvider.source_type, "id": str(settlement_id)},
    )


class ExpenseClaimProvider(InputProvider):
    """Approved expense claims not yet paid, reimbursed at cost in the next run.

    A claim on a trip whose travel advance is still open waits: the advance
    already put money in the employee's hands for that trip, and settling it
    decides what is still owed (see `AdvanceRefundProvider`). Paying the claim
    here as well would pay the trip twice.
    """

    key = "expense_claims"
    label = "Expense reimbursements"
    source_type = "expense_claim"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        output = ProviderOutput()
        if not scope.employee_ids:
            return output
        claims = (
            await session.scalars(
                tenant_scoped(
                    select(ExpenseClaim)
                    .where(
                        ExpenseClaim.employee_id.in_(scope.employee_ids),
                        ExpenseClaim.status == ExpenseClaimStatusEnum.APPROVED,
                        ExpenseClaim.reimbursed_in_run_id.is_(None),
                        ExpenseClaim.advance_settlement_id.is_(None),
                        ExpenseClaim.amount > 0,
                    )
                    .order_by(ExpenseClaim.incurred_on, ExpenseClaim.created_at),
                    ExpenseClaim,
                    user,
                )
            )
        ).all()
        if not claims:
            return output

        trip_ids = {claim.travel_request_id for claim in claims if claim.travel_request_id}
        open_trips: set[uuid.UUID] = set()
        if trip_ids:
            open_trips = set(
                (
                    await session.scalars(
                        tenant_scoped(
                            select(TravelAdvance.travel_request_id).where(
                                TravelAdvance.travel_request_id.in_(trip_ids),
                                TravelAdvance.status == AdvanceStatusEnum.ISSUED,
                            ),
                            TravelAdvance,
                            user,
                        )
                    )
                ).all()
            )

        waiting = 0
        for claim in claims:
            if claim.travel_request_id in open_trips:
                waiting += 1
                continue
            output.lines[claim.employee_id].append(
                expense_claim_line(
                    claim_id=claim.id,
                    category_key=claim.category_key,
                    amount=_d(claim.amount),
                    incurred_on=claim.incurred_on,
                    original_amount=_d(claim.original_amount),
                    original_currency=claim.original_currency or "INR",
                )
            )
        if waiting:
            output.notices.append(
                f"{waiting} approved claim(s) are on trips whose travel advance is not settled "
                "yet. They are paid through the advance settlement, not directly, so the trip "
                "is not paid twice."
            )
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        claim = await session.scalar(
            tenant_scoped(
                select(ExpenseClaim).where(ExpenseClaim.id == uuid.UUID(line["source"]["id"])),
                ExpenseClaim,
                user,
            )
        )
        if (
            claim is None
            or claim.status is not ExpenseClaimStatusEnum.APPROVED
            or claim.reimbursed_in_run_id not in (None, run.id)
            or claim.advance_settlement_id is not None
        ):
            raise _conflict("An expense claim on this run")
        claim.status = ExpenseClaimStatusEnum.REIMBURSED
        claim.reimbursed_in_run_id = run.id
        claim.reimbursed_at = datetime.now(UTC)


class AdvanceRefundProvider(InputProvider):
    """A settled trip that cost more than its advance: the balance, paid in the run.

    Consuming it also marks the trip's claims reimbursed: the advance paid the
    first part of them and this run pays the rest.
    """

    key = "advance_refunds"
    label = "Travel advance balances due"
    source_type = "advance_refund"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        output = ProviderOutput()
        if not scope.employee_ids:
            return output
        rows = (
            await session.execute(
                tenant_scoped(
                    select(AdvanceSettlement, TravelAdvance)
                    .join(TravelAdvance, AdvanceSettlement.advance_id == TravelAdvance.id)
                    .where(
                        AdvanceSettlement.outcome == SettlementOutcomeEnum.REFUND_DUE_TO_EMPLOYEE,
                        AdvanceSettlement.refund_amount > 0,
                        AdvanceSettlement.payroll_refund_ref.is_(None),
                        AdvanceSettlement.settled_on <= scope.period_end,
                        TravelAdvance.employee_id.in_(scope.employee_ids),
                    ),
                    AdvanceSettlement,
                    user,
                )
            )
        ).all()
        for settlement, advance in rows:
            output.lines[advance.employee_id].append(
                advance_refund_line(
                    settlement_id=settlement.id,
                    refund_amount=_d(settlement.refund_amount),
                    advance_amount=_d(settlement.advance_amount),
                    claims_total=_d(settlement.claims_total),
                    settled_on=settlement.settled_on,
                )
            )
        return output

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        settlement = await session.scalar(
            tenant_scoped(
                select(AdvanceSettlement).where(
                    AdvanceSettlement.id == uuid.UUID(line["source"]["id"])
                ),
                AdvanceSettlement,
                user,
            )
        )
        reference = _reference(run)
        if settlement is None or settlement.payroll_refund_ref not in (None, reference):
            raise _conflict("A travel advance balance on this run")
        settlement.payroll_refund_ref = reference
        now = datetime.now(UTC)
        claims = (
            await session.scalars(
                tenant_scoped(
                    select(ExpenseClaim).where(
                        ExpenseClaim.advance_settlement_id == settlement.id,
                        ExpenseClaim.status == ExpenseClaimStatusEnum.APPROVED,
                    ),
                    ExpenseClaim,
                    user,
                )
            )
        ).all()
        for claim in claims:
            claim.status = ExpenseClaimStatusEnum.REIMBURSED
            claim.reimbursed_in_run_id = run.id
            claim.reimbursed_at = now


# ── Not yet available ───────────────────────────────────────────────────────


class ConcessionalLoanPerquisiteProvider(InputProvider):
    """An interest-free or concessional loan is a taxable perquisite.

    Slice 14 can compute it (`amortisation.concessional_loan_perquisite`), but
    only from a notional interest rate and an exemption threshold, and no table
    stores either. Rather than assume them, this adds nothing and says so.
    """

    key = "loan_perquisites"
    label = "Concessional loan perquisites"
    source_type = "loan_perquisite"

    async def collect(self, session: AsyncSession, user: User, scope: RunScope) -> ProviderOutput:
        return ProviderOutput()

    async def consume(
        self, session: AsyncSession, user: User, run: PayrollRun, line: dict[str, Any]
    ) -> None:
        raise _conflict("A concessional loan perquisite")

    def report(self, output: ProviderOutput) -> ProviderReport:
        return ProviderReport(
            key=self.key,
            label=self.label,
            status="not_available",
            message=(
                "Not yet available. Valuing an interest-free or concessional loan needs a "
                "notional interest rate and an exemption threshold, and nothing in the "
                "system stores them yet. Nothing is added to taxable income for these loans."
            ),
        )


PROVIDERS: tuple[InputProvider, ...] = (
    StatutoryBonusProvider(),
    ArrearsProvider(),
    EncashmentProvider(),
    ReferralBonusProvider(),
    ReimbursementProvider(),
    ExpenseClaimProvider(),
    AdvanceRefundProvider(),
    RewardPerquisiteProvider(),
    AdvanceRecoveryProvider(),
    LoanEmiProvider(),
    ConcessionalLoanPerquisiteProvider(),
)

_BY_SOURCE = {provider.source_type: provider for provider in PROVIDERS}


async def collect_inputs(session: AsyncSession, user: User, scope: RunScope) -> CollectedInputs:
    """Ask every provider what is due for this run. Reads only."""
    lines: dict[uuid.UUID, list[InputLine]] = defaultdict(list)
    loans: dict[uuid.UUID, list[LoanRecoveryInput]] = defaultdict(list)
    reports: list[ProviderReport] = []
    notices: list[dict[str, str]] = []

    for provider in PROVIDERS:
        output = await provider.collect(session, user, scope)
        for employee_id, items in output.lines.items():
            lines[employee_id].extend(items)
        for employee_id, items in output.loans.items():
            loans[employee_id].extend(items)
        report = provider.report(output)
        if output.notices and report.status == "nothing_due":
            report.status = "blocked"
            report.message = output.notices[0]
        reports.append(report)
        notices.extend(
            {"key": provider.key, "label": provider.label, "message": notice}
            for notice in output.notices
        )

    return CollectedInputs(lines=dict(lines), loans=dict(loans), reports=reports, notices=notices)


async def consume_inputs(
    session: AsyncSession, user: User, run: PayrollRun, input_lines: list[dict[str, Any]]
) -> int:
    """Mark every consumed source with this run. Called inside the approval.

    A skipped recovery is not consumed — it is still owed — except a loan
    instalment, whose skip is itself a ledger row.

    Raises `InputConsumedElsewhereError` when a source is no longer due.
    """
    consumed = 0
    for line in input_lines:
        source = line.get("source") or {}
        provider = _BY_SOURCE.get(source.get("type", ""))
        if provider is None:
            raise InputConsumedElsewhereError(
                f"The payslip line '{line.get('name')}' names a source this system does not "
                "recognise. Recompute the run."
            )
        if line.get("skipped") and provider.source_type != LoanEmiProvider.source_type:
            continue
        await provider.consume(session, user, run, line)
        consumed += 1
    return consumed
