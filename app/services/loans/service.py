"""Loan and reimbursement persistence (slice 14).

The arithmetic is in `amortisation.py`, `recovery.py` and `reimbursements.py`.
This module holds the workflow and who may do what:

- an employee applies for their own loan and claims against their own
  entitlement; payroll can act for anyone;
- nobody approves, rejects or disburses their own loan, or verifies their own
  claim;
- a bill is a stored, encrypted document its owner and payroll can open;
- the recovery ledger is the source of truth the payroll run reads and writes
  through `record_recovery` — skipped instalments are rows too.

A reimbursement component is salary, not an expense claim: see the model
module. The two stay separate here as well.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.loans import (
    ClaimStatus,
    LoanAccount,
    LoanRepayment,
    LoanScheme,
    LoanStatus,
    ReimbursementClaim,
    ReimbursementComponent,
)
from app.models.user import User
from app.services import audit
from app.services.filings import support
from app.services.loans import amortisation, recovery, reimbursements
from app.services.notifications.dispatch import notify

ZERO = Decimal(0)
#: Months of service are counted in 30-day blocks for scheme eligibility.
DAYS_PER_SERVICE_MONTH = 30


def _refuse(detail: str, code: int = status.HTTP_422_UNPROCESSABLE_ENTITY) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


async def _employee_labels(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    if not ids:
        return {}
    rows = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.id.in_(ids)), Employee, user)
        )
    ).all()
    return {row.id: (row.employee_code, row.full_name) for row in rows}


# ═══════════════════════════════════════════════════════════════════════════
# Schemes
# ═══════════════════════════════════════════════════════════════════════════


async def list_schemes(session: AsyncSession, user: User) -> list[LoanScheme]:
    return list(
        (
            await session.scalars(
                tenant_scoped(select(LoanScheme).order_by(LoanScheme.name), LoanScheme, user)
            )
        ).all()
    )


async def create_scheme(
    session: AsyncSession, user: User, values: dict[str, Any], *, request: Request | None = None
) -> LoanScheme:
    existing = await session.scalar(
        tenant_scoped(
            select(LoanScheme.id).where(LoanScheme.name == values["name"]), LoanScheme, user
        )
    )
    if existing is not None:
        raise _refuse(
            f"A scheme called '{values['name']}' already exists.", status.HTTP_409_CONFLICT
        )
    scheme = LoanScheme(tenant_id=user.tenant_id, **values)
    session.add(scheme)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.RULE_CHANGE,
        entity_type="loan_scheme",
        entity_id=scheme.id,
        summary=f"Created the loan scheme '{scheme.name}'",
        changes={
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in values.items()
            if not isinstance(value, list)
        },
        request=request,
    )
    return scheme


# ═══════════════════════════════════════════════════════════════════════════
# Loans
# ═══════════════════════════════════════════════════════════════════════════


async def _loan(session: AsyncSession, user: User, loan_id: uuid.UUID) -> LoanAccount:
    loan = await session.scalar(
        tenant_scoped(select(LoanAccount).where(LoanAccount.id == loan_id), LoanAccount, user)
    )
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found")
    return loan


async def apply_for_loan(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    scheme_id: uuid.UUID,
    principal: Decimal,
    tenure_months: int,
    purpose: str | None,
    request: Request | None = None,
) -> LoanAccount:
    """Apply for a loan. The terms are snapshotted from the scheme now.

    A scheme's rate changing next year must not retroactively alter what an
    existing borrower owes. `employee_id` has already been resolved by the
    caller: the applicant's own, or anyone's for payroll.
    """
    permissions.require_self_or_hr(
        user, employee_id, detail="You can only apply for a loan for yourself"
    )
    scheme = await session.scalar(
        tenant_scoped(select(LoanScheme).where(LoanScheme.id == scheme_id), LoanScheme, user)
    )
    if scheme is None or not scheme.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan scheme not found")

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if scheme.max_amount and principal > Decimal(str(scheme.max_amount)):
        raise _refuse(f"{principal} exceeds the scheme's maximum of {scheme.max_amount}.")
    if tenure_months > scheme.max_tenure_months:
        raise _refuse(
            f"A tenure of {tenure_months} months exceeds the scheme's maximum of "
            f"{scheme.max_tenure_months}."
        )
    if scheme.eligibility_min_service_months:
        if employee.date_of_joining is None:
            raise _refuse(
                "This scheme needs a minimum period of service, and there is no joining "
                "date on the employee record."
            )
        served = (date.today() - employee.date_of_joining).days // DAYS_PER_SERVICE_MONTH
        if served < scheme.eligibility_min_service_months:
            raise _refuse(
                f"{employee.employee_code} has {served} month(s) of service against the "
                f"{scheme.eligibility_min_service_months} this scheme requires."
            )
    open_loan = await session.scalar(
        tenant_scoped(
            select(LoanAccount.id).where(
                LoanAccount.employee_id == employee_id,
                LoanAccount.scheme_id == scheme.id,
                LoanAccount.status.in_([LoanStatus.APPLIED, LoanStatus.APPROVED]),
            ),
            LoanAccount,
            user,
        )
    )
    if open_loan is not None:
        raise _refuse(
            "There is already an application under this scheme awaiting a decision or "
            "disbursal.",
            status.HTTP_409_CONFLICT,
        )

    try:
        schedule = amortisation.build_schedule(
            principal=principal,
            annual_rate=Decimal(str(scheme.interest_rate)),
            tenure_months=tenure_months,
            interest_type=scheme.interest_type.value,
            moratorium_months=scheme.moratorium_months,
            accrue_interest_during_moratorium=scheme.accrue_interest_during_moratorium,
        )
    except amortisation.AmortisationError as exc:
        raise _refuse(str(exc)) from exc

    loan = LoanAccount(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        scheme_id=scheme.id,
        status=LoanStatus.APPLIED if scheme.requires_approval else LoanStatus.APPROVED,
        principal=principal,
        interest_rate=scheme.interest_rate,
        interest_type=scheme.interest_type,
        tenure_months=tenure_months,
        emi_amount=schedule.emi,
        outstanding_principal=principal,
        purpose=purpose,
        applied_at=datetime.now(UTC),
        schedule=schedule.as_dict(),
    )
    session.add(loan)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="loan_account",
        entity_id=loan.id,
        summary=(
            f"{employee.employee_code} applied for {principal} over {tenure_months} months "
            f"at {scheme.interest_rate}% ({scheme.interest_type.value})"
        ),
        changes={
            "principal": str(principal),
            "emi": str(schedule.emi),
            "total_interest": str(schedule.total_interest),
            "on_behalf": user.employee_id != employee_id,
        },
        request=request,
    )
    return loan


async def list_loans(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None = None,
    loan_status: LoanStatus | None = None,
) -> list[tuple[LoanAccount, tuple[str, str] | None, str | None]]:
    stmt = select(LoanAccount).order_by(LoanAccount.created_at.desc())
    if employee_id is not None:
        stmt = stmt.where(LoanAccount.employee_id == employee_id)
    if loan_status is not None:
        stmt = stmt.where(LoanAccount.status == loan_status)
    loans = (await session.scalars(tenant_scoped(stmt, LoanAccount, user))).all()
    labels = await _employee_labels(session, user, {loan.employee_id for loan in loans})
    schemes = {scheme.id: scheme.name for scheme in await list_schemes(session, user)}
    return [(loan, labels.get(loan.employee_id), schemes.get(loan.scheme_id)) for loan in loans]


async def loan_detail(
    session: AsyncSession, user: User, loan_id: uuid.UUID
) -> tuple[LoanAccount, list[LoanRepayment]]:
    """A loan, its schedule and its ledger: the borrower's own, or anyone's for payroll."""
    loan = await _loan(session, user, loan_id)
    permissions.require_self_or_hr(user, loan.employee_id, detail="You can only see your own loans")
    repayments = (
        await session.scalars(
            tenant_scoped(
                select(LoanRepayment)
                .where(LoanRepayment.loan_account_id == loan.id)
                .order_by(LoanRepayment.period, LoanRepayment.created_at),
                LoanRepayment,
                user,
            )
        )
    ).all()
    return loan, list(repayments)


def _not_own(user: User, loan: LoanAccount, action: str) -> None:
    if user.employee_id is not None and loan.employee_id == user.employee_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This is your own loan. Somebody else has to {action} it.",
        )


async def decide_loan(
    session: AsyncSession,
    user: User,
    loan_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
) -> LoanAccount:
    """Approve or reject an application. A rejection needs a reason."""
    loan = await _loan(session, user, loan_id)
    _not_own(user, loan, "decide")
    if loan.status is not LoanStatus.APPLIED:
        raise _refuse(
            f"A loan in status '{loan.status.value}' is not awaiting a decision.",
            status.HTTP_409_CONFLICT,
        )
    if not approve and not (note or "").strip():
        raise _refuse("A rejection needs a reason. The applicant is told it.")

    now = datetime.now(UTC)
    loan.status = LoanStatus.APPROVED if approve else LoanStatus.REJECTED
    loan.approved_by_user_id = user.id
    loan.approved_at = now
    loan.decision_note = (note or "").strip() or None
    if not approve:
        loan.closed_at = now
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="loan_account",
        entity_id=loan.id,
        summary=f"{'Approved' if approve else 'Rejected'} a loan of {loan.principal}",
        changes={"principal": str(loan.principal), "note": loan.decision_note},
        request=request,
    )
    return loan


async def disburse_loan(
    session: AsyncSession,
    user: User,
    loan_id: uuid.UUID,
    *,
    disbursed_on: date | None,
    request: Request | None = None,
) -> LoanAccount:
    """Mark an approved loan paid out. Recovery starts from the next payroll run."""
    loan = await _loan(session, user, loan_id)
    _not_own(user, loan, "disburse")
    if loan.status is not LoanStatus.APPROVED:
        raise _refuse(
            f"A loan in status '{loan.status.value}' cannot be disbursed; it has to be "
            "approved first.",
            status.HTTP_409_CONFLICT,
        )
    when = disbursed_on or date.today()
    loan.status = LoanStatus.DISBURSED
    loan.disbursed_at = datetime.combine(when, datetime.min.time(), tzinfo=UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="loan_account",
        entity_id=loan.id,
        summary=f"Disbursed a loan of {loan.principal} on {when.isoformat()}",
        changes={"disbursed_on": when.isoformat()},
        request=request,
    )
    return loan


async def prepay_loan(
    session: AsyncSession,
    user: User,
    loan_id: uuid.UUID,
    *,
    amount: Decimal,
    mode: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Part-prepay and reschedule, reducing the EMI or the tenure as asked."""
    loan = await _loan(session, user, loan_id)
    if loan.status not in (LoanStatus.DISBURSED, LoanStatus.ACTIVE):
        raise _refuse(
            f"A loan in status '{loan.status.value}' cannot be prepaid.", status.HTTP_409_CONFLICT
        )
    period = date.today().strftime("%Y-%m")
    recovered = (
        await session.scalars(
            tenant_scoped(
                select(LoanRepayment).where(
                    LoanRepayment.loan_account_id == loan.id,
                    LoanRepayment.is_skipped.is_(False),
                    LoanRepayment.is_prepayment.is_(False),
                ),
                LoanRepayment,
                user,
            )
        )
    ).all()
    already = await session.scalar(
        tenant_scoped(
            select(LoanRepayment.id).where(
                LoanRepayment.loan_account_id == loan.id, LoanRepayment.period == period
            ),
            LoanRepayment,
            user,
        )
    )
    if already is not None:
        raise _refuse(
            f"A recovery or prepayment is already recorded for {period}. Record the "
            "prepayment next month, or with that month's payroll.",
            status.HTTP_409_CONFLICT,
        )
    remaining = max(1, loan.tenure_months - len(recovered))

    try:
        schedule = amortisation.recompute_after_prepayment(
            outstanding_principal=Decimal(str(loan.outstanding_principal)),
            annual_rate=Decimal(str(loan.interest_rate)),
            remaining_months=remaining,
            prepayment=amount,
            mode=mode,
            current_emi=Decimal(str(loan.emi_amount)),
            interest_type=loan.interest_type.value,
        )
    except amortisation.AmortisationError as exc:
        raise _refuse(str(exc)) from exc

    loan.outstanding_principal = schedule.principal
    loan.emi_amount = schedule.emi
    loan.tenure_months = len(recovered) + schedule.tenure_months
    loan.schedule = schedule.as_dict()
    if schedule.principal <= ZERO:
        loan.status = LoanStatus.CLOSED
        loan.closed_at = datetime.now(UTC)
    session.add(
        LoanRepayment(
            tenant_id=user.tenant_id,
            loan_account_id=loan.id,
            period=period,
            amount=amount,
            principal_component=amount,
            interest_component=ZERO,
            closing_balance=schedule.principal,
            is_prepayment=True,
            reason=schedule.notes[0] if schedule.notes else "Part-prepayment.",
        )
    )
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="loan_account",
        entity_id=loan.id,
        summary=f"Part-prepayment of {amount} ({mode})",
        changes={
            "amount": str(amount),
            "mode": mode,
            "new_emi": str(schedule.emi),
            "new_tenure_months": schedule.tenure_months,
        },
        request=request,
    )
    return schedule.as_dict()


async def record_recovery(
    session: AsyncSession,
    user: User,
    *,
    loan_id: uuid.UUID,
    period: str,
    net_pay_before_recovery: Decimal,
    payroll_run_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> LoanRepayment:
    """Plan and record this month's recovery for one loan. For the payroll run.

    Respects the scheme's net-pay floor through `recovery.plan_recovery`: an
    instalment that does not fit is skipped and carried forward, and the skip
    is a ledger row with its reason. Idempotent per period — a re-run of the
    same month returns the row already recorded. The caller commits.
    """
    loan = await _loan(session, user, loan_id)
    existing = await session.scalar(
        tenant_scoped(
            select(LoanRepayment).where(
                LoanRepayment.loan_account_id == loan.id, LoanRepayment.period == period
            ),
            LoanRepayment,
            user,
        )
    )
    if existing is not None:
        return existing
    if loan.status not in (LoanStatus.DISBURSED, LoanStatus.ACTIVE):
        raise _refuse(
            f"A loan in status '{loan.status.value}' is not being recovered.",
            status.HTTP_409_CONFLICT,
        )
    scheme = await session.scalar(
        tenant_scoped(select(LoanScheme).where(LoanScheme.id == loan.scheme_id), LoanScheme, user)
    )
    outstanding = Decimal(str(loan.outstanding_principal))
    try:
        decision = recovery.plan_recovery(
            emi=Decimal(str(loan.emi_amount)),
            net_pay_before_recovery=net_pay_before_recovery,
            minimum_net_pay=Decimal(str(scheme.minimum_net_pay)) if scheme else ZERO,
            outstanding_principal=outstanding,
            already_carried_forward=Decimal(str(loan.carried_forward)),
            allow_partial=bool(scheme and scheme.allow_partial_recovery),
        )
    except recovery.RecoveryError as exc:
        raise _refuse(str(exc)) from exc

    # Split what was recovered into interest and principal from the stored
    # schedule's next instalment, so the ledger keeps reconciling.
    recovered_rows = (
        await session.scalars(
            tenant_scoped(
                select(LoanRepayment.id).where(
                    LoanRepayment.loan_account_id == loan.id,
                    LoanRepayment.is_skipped.is_(False),
                    LoanRepayment.is_prepayment.is_(False),
                ),
                LoanRepayment,
                user,
            )
        )
    ).all()
    instalments = (loan.schedule or {}).get("instalments") or []
    next_instalment = instalments[len(recovered_rows)] if len(recovered_rows) < len(
        instalments
    ) else None
    interest = ZERO
    if decision.amount > ZERO and next_instalment is not None:
        interest = min(decision.amount, Decimal(str(next_instalment["interest"])))
    principal_part = min(outstanding, decision.amount - interest)
    closing = outstanding - principal_part

    row = LoanRepayment(
        tenant_id=user.tenant_id,
        loan_account_id=loan.id,
        payroll_run_id=payroll_run_id,
        period=period,
        amount=decision.amount,
        principal_component=principal_part,
        interest_component=interest,
        closing_balance=closing,
        is_skipped=decision.is_skipped,
        reason=decision.reason,
    )
    session.add(row)
    loan.carried_forward = decision.carried_forward
    loan.outstanding_principal = closing
    if loan.status is LoanStatus.DISBURSED and decision.amount > ZERO:
        loan.status = LoanStatus.ACTIVE
    if closing <= ZERO:
        loan.status = LoanStatus.CLOSED
        loan.closed_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="loan_account",
        entity_id=loan.id,
        summary=(
            f"Recovery for {period}: {'skipped' if decision.is_skipped else decision.amount}"
        ),
        changes=decision.as_dict(),
        request=request,
    )
    return row


async def active_recoveries(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[LoanAccount]:
    """Loans the payroll run recovers for this employee this month."""
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(LoanAccount).where(
                        LoanAccount.employee_id == employee_id,
                        LoanAccount.status.in_([LoanStatus.DISBURSED, LoanStatus.ACTIVE]),
                    ),
                    LoanAccount,
                    user,
                )
            )
        ).all()
    )


# ═══════════════════════════════════════════════════════════════════════════
# Reimbursement components and claims
# ═══════════════════════════════════════════════════════════════════════════


async def list_components(
    session: AsyncSession, user: User, *, active_only: bool = False
) -> list[ReimbursementComponent]:
    stmt = select(ReimbursementComponent).order_by(ReimbursementComponent.code)
    if active_only:
        stmt = stmt.where(ReimbursementComponent.active.is_(True))
    return list((await session.scalars(tenant_scoped(stmt, ReimbursementComponent, user))).all())


async def create_component(
    session: AsyncSession, user: User, values: dict[str, Any], *, request: Request | None = None
) -> ReimbursementComponent:
    existing = await session.scalar(
        tenant_scoped(
            select(ReimbursementComponent.id).where(
                ReimbursementComponent.code == values["code"]
            ),
            ReimbursementComponent,
            user,
        )
    )
    if existing is not None:
        raise _refuse(
            f"A component with code '{values['code']}' already exists.", status.HTTP_409_CONFLICT
        )
    component = ReimbursementComponent(tenant_id=user.tenant_id, **values)
    session.add(component)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.RULE_CHANGE,
        entity_type="reimbursement_component",
        entity_id=component.id,
        summary=f"Created the reimbursement component {component.code}",
        changes={
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in values.items()
        },
        request=request,
    )
    return component


async def submit_claim(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    component_code: str,
    financial_year: str,
    amount: Decimal,
    bill_date: date | None,
    bill_reference: str | None,
    bill: bytes | None,
    request: Request | None = None,
) -> ReimbursementClaim:
    """Claim against an entitlement: your own, or anyone's for payroll."""
    permissions.require_self_or_hr(
        user, employee_id, detail="You can only claim reimbursements for yourself"
    )
    component = await session.scalar(
        tenant_scoped(
            select(ReimbursementComponent).where(
                ReimbursementComponent.code == component_code,
                ReimbursementComponent.active.is_(True),
            ),
            ReimbursementComponent,
            user,
        )
    )
    if component is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{component_code}' is not a reimbursement component.",
        )
    if component.bill_required and not bill:
        raise _refuse(f"{component.name} is exempt only against a bill, and none was attached.")
    if bill_date is not None and bill_date > date.today():
        raise _refuse("A bill cannot be dated in the future.")

    stored = support.store_upload(user.tenant_id, "reimbursement-bills", bill) if bill else None
    claim = ReimbursementClaim(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        component_code=component.code,
        financial_year=financial_year,
        amount=amount,
        bill_storage_key=stored.key if stored else None,
        bill_date=bill_date,
        bill_reference=bill_reference,
    )
    session.add(claim)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="reimbursement_claim",
        entity_id=claim.id,
        summary=f"Claimed {amount} against {component.code} for {financial_year}",
        changes={"amount": str(amount), "bill_attached": stored is not None},
        request=request,
    )
    return claim


async def list_claims(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None,
    financial_year: str | None = None,
    claim_status: ClaimStatus | None = None,
) -> list[tuple[ReimbursementClaim, tuple[str, str] | None, str | None]]:
    """Claims: everybody's for payroll, otherwise only the caller's own."""
    stmt = select(ReimbursementClaim).order_by(ReimbursementClaim.created_at.desc())
    if employee_id is not None or not permissions.is_hr(user):
        target = permissions.resolve_employee(
            user, employee_id, detail="You can only see your own claims"
        )
        stmt = stmt.where(ReimbursementClaim.employee_id == target)
    if financial_year:
        stmt = stmt.where(ReimbursementClaim.financial_year == financial_year)
    if claim_status is not None:
        stmt = stmt.where(ReimbursementClaim.status == claim_status)
    claims = (await session.scalars(tenant_scoped(stmt, ReimbursementClaim, user))).all()
    labels = await _employee_labels(session, user, {claim.employee_id for claim in claims})
    names = {c.code: c.name for c in await list_components(session, user)}
    return [
        (claim, labels.get(claim.employee_id), names.get(claim.component_code))
        for claim in claims
    ]


async def _claim(session: AsyncSession, user: User, claim_id: uuid.UUID) -> ReimbursementClaim:
    claim = await session.scalar(
        tenant_scoped(
            select(ReimbursementClaim).where(ReimbursementClaim.id == claim_id),
            ReimbursementClaim,
            user,
        )
    )
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    return claim


async def verify_claim(
    session: AsyncSession,
    user: User,
    claim_id: uuid.UUID,
    *,
    decision: ClaimStatus,
    accepted_amount: Decimal,
    note: str | None,
    request: Request | None = None,
) -> ReimbursementClaim:
    """Verify a bill, in full or in part. Never your own."""
    claim = await _claim(session, user, claim_id)
    if user.employee_id is not None and claim.employee_id == user.employee_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This is your own claim. Somebody else in payroll has to verify it.",
        )
    if decision is ClaimStatus.SUBMITTED:
        raise _refuse("A verification decision cannot be 'submitted'.")
    if decision in (ClaimStatus.REJECTED, ClaimStatus.PARTIAL) and not (note or "").strip():
        raise _refuse(
            "A rejection or a partial acceptance needs a note. Without one the employee "
            "cannot tell what to fix."
        )
    claimed = Decimal(str(claim.amount))
    if accepted_amount > claimed:
        raise _refuse(f"{accepted_amount} cannot be accepted against a claim of {claimed}.")
    if decision is ClaimStatus.VERIFIED:
        accepted_amount = claimed
    if decision is ClaimStatus.PARTIAL and not (ZERO < accepted_amount < claimed):
        raise _refuse(
            "A partial acceptance is more than zero and less than the claim. Use "
            "'verified' or 'rejected' otherwise."
        )

    claim.status = decision
    claim.accepted_amount = accepted_amount if decision is not ClaimStatus.REJECTED else ZERO
    claim.verification_note = (note or "").strip() or None
    claim.verified_by_user_id = user.id
    claim.verified_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.REJECT if decision is ClaimStatus.REJECTED else AuditAction.APPROVE,
        entity_type="reimbursement_claim",
        entity_id=claim.id,
        summary=(
            f"{claim.component_code} claim of {claim.amount} marked {decision.value}, "
            f"{claim.accepted_amount} accepted"
        ),
        changes={
            "status": decision.value,
            "accepted_amount": str(claim.accepted_amount),
            "note": claim.verification_note,
        },
        request=request,
    )
    return claim


async def claim_bill(
    session: AsyncSession, user: User, claim_id: uuid.UUID, *, request: Request | None = None
) -> tuple[bytes, str, str]:
    claim = await _claim(session, user, claim_id)
    permissions.require_self_or_hr(
        user, claim.employee_id, detail="You can only open your own bills"
    )
    stored = support.load_file(user.tenant_id, claim.bill_storage_key)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No bill is attached")
    data, mime = stored
    if user.employee_id != claim.employee_id:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.SENSITIVE_READ,
            entity_type="reimbursement_claim",
            entity_id=claim.id,
            summary="Opened an employee's reimbursement bill",
            request=request,
        )
    extension = claim.bill_storage_key[claim.bill_storage_key.rfind(".") :]
    return data, mime, f"{claim.component_code}_bill_{claim.id.hex[:8]}{extension}"


def _year_end(financial_year: str) -> date:
    return date(int(financial_year.split("-")[1]), 3, 31)


async def entitlement_positions(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Where the employee stands against each component, at any point in the year."""
    permissions.require_self_or_hr(
        user, employee_id, detail="You can only see your own entitlements"
    )
    moment = today or date.today()
    out: list[dict[str, Any]] = []
    for component in await list_components(session, user, active_only=True):
        claims = (
            await session.scalars(
                tenant_scoped(
                    select(ReimbursementClaim).where(
                        ReimbursementClaim.employee_id == employee_id,
                        ReimbursementClaim.component_code == component.code,
                        ReimbursementClaim.financial_year == financial_year,
                    ),
                    ReimbursementClaim,
                    user,
                )
            )
        ).all()
        position = reimbursements.position(
            component_code=component.code,
            financial_year=financial_year,
            entitlement=Decimal(str(component.annual_entitlement)),
            claims=[
                {
                    "amount": Decimal(str(c.amount)),
                    "accepted_amount": Decimal(str(c.accepted_amount)),
                    "status": c.status.value,
                }
                for c in claims
            ],
        )
        warn, message = reimbursements.should_warn(
            today=moment,
            financial_year_end=_year_end(financial_year),
            unclaimed=position.unclaimed,
            reminder_months_before_year_end=component.reminder_months_before_year_end,
        )
        out.append(
            {
                **position.as_dict(),
                "component_name": component.name,
                "should_warn": warn,
                "warning": message,
            }
        )
    return out


EVENT_UNCLAIMED = "payroll.reimbursement.unclaimed"
NOTICE_TEMPLATES = {
    EVENT_UNCLAIMED: (
        "{unclaimed} of your {component} entitlement is unclaimed",
        "{warning} Submit bills in AskMira before {year_end}.",
    ),
}


async def send_unclaimed_entitlement_reminders(
    session: AsyncSession, tenant_id: uuid.UUID, *, today: date
) -> int:
    """Warn employees about entitlement that will become taxable. Commits.

    For the scheduler: run monthly per tenant. It warns only inside each
    component's configured reminder window, and at most once a month per
    employee and component (keyed on the notification payload).
    """
    from app.models.notifications import Notification
    from app.models.user import User as UserModel

    await support.ensure_event_templates(session, tenant_id, NOTICE_TEMPLATES)
    actor = support.system_actor(tenant_id)
    start_year = today.year if today.month >= 4 else today.year - 1
    financial_year = f"{start_year}-{start_year + 1}"
    month_key = today.strftime("%Y-%m")

    rows = (
        await session.execute(
            select(UserModel.id, UserModel.employee_id).where(
                UserModel.tenant_id == tenant_id,
                UserModel.is_active.is_(True),
                UserModel.employee_id.is_not(None),
            )
        )
    ).all()
    payloads = (
        await session.scalars(
            select(Notification.payload).where(
                Notification.tenant_id == tenant_id,
                Notification.event_key == EVENT_UNCLAIMED,
            )
        )
    ).all()
    already = {
        (p.get("employee_id"), p.get("component_code"), p.get("month"))
        for p in payloads
        if isinstance(p, dict)
    }

    raised = 0
    for user_id, employee_id in rows:
        positions = await entitlement_positions(
            session, actor, employee_id=employee_id, financial_year=financial_year, today=today
        )
        for position in positions:
            key = (str(employee_id), position["component_code"], month_key)
            if not position["should_warn"] or key in already:
                continue
            await notify(
                session,
                tenant_id=tenant_id,
                event_key=EVENT_UNCLAIMED,
                recipient_user_ids=[user_id],
                payload={
                    "employee_id": str(employee_id),
                    "component_code": position["component_code"],
                    "component": position["component_name"],
                    "unclaimed": position["unclaimed"],
                    "warning": position["warning"],
                    "year_end": _year_end(financial_year).isoformat(),
                    "month": month_key,
                },
                commit=False,
            )
            raised += 1

    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CREATE,
        entity_type="reimbursement_reminder",
        actor_type="system",
        changes={"as_of": today.isoformat(), "notifications": raised},
        summary=f"Unclaimed-entitlement reminders for {month_key}: {raised}",
    )
    await session.commit()
    return raised
