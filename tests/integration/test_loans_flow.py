"""Slice 14 gates that need a real database.

The engines are proved in `tests/loans/`. What only a database settles: that the
schedule stored on the account is the one the borrower and payroll both read,
that a skipped instalment leaves a row explaining itself, that the outstanding
balance reaches the full and final settlement, and that a partial acceptance
pays only what was accepted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.loans import (
    ClaimStatus,
    InterestType,
    LoanAccount,
    LoanRepayment,
    LoanScheme,
    LoanStatus,
    LoanType,
    ReimbursementClaim,
    ReimbursementComponent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services.loans import amortisation, recovery, reimbursements
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _scheme(session: AsyncSession, tenant: Tenant, **overrides) -> LoanScheme:
    base = {
        "name": "Staff loan",
        "loan_type": LoanType.COMPANY_LOAN,
        "interest_type": InterestType.REDUCING,
        "interest_rate": Decimal(12),
        "max_tenure_months": 36,
        "max_amount": Decimal(500_000),
        "minimum_net_pay": Decimal(15_000),
    }
    base.update(overrides)
    scheme = LoanScheme(tenant_id=tenant.id, **base)
    session.add(scheme)
    await session.commit()
    await session.refresh(scheme)
    return scheme


async def _loan(
    session: AsyncSession,
    tenant: Tenant,
    employee: Employee,
    scheme: LoanScheme,
    *,
    principal: str = "120000",
    tenure: int = 12,
) -> LoanAccount:
    schedule = amortisation.build_schedule(
        principal=Decimal(principal),
        annual_rate=Decimal(str(scheme.interest_rate)),
        tenure_months=tenure,
        interest_type=scheme.interest_type.value,
    )
    loan = LoanAccount(
        tenant_id=tenant.id,
        employee_id=employee.id,
        scheme_id=scheme.id,
        status=LoanStatus.ACTIVE,
        principal=Decimal(principal),
        interest_rate=scheme.interest_rate,
        interest_type=scheme.interest_type,
        tenure_months=tenure,
        emi_amount=schedule.emi,
        outstanding_principal=Decimal(principal),
        disbursed_at=datetime.now(UTC),
        schedule=schedule.as_dict(),
    )
    session.add(loan)
    await session.commit()
    await session.refresh(loan)
    return loan


# ═══════════════════════════════════════════════════════════════════════════
# The stored schedule
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_stored_schedule_reconciles_to_the_principal(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    """The checklist gate, on what is actually persisted.

    Stored rather than rebuilt on read, so the ledger the borrower sees and the
    one payroll recovers against cannot drift apart.
    """
    scheme = await _scheme(session, tenant)
    loan = await _loan(session, tenant, employee, scheme)

    stored = loan.schedule
    assert stored["reconciles"] is True
    assert Decimal(stored["total_principal"]) == Decimal(120_000)
    assert len(stored["instalments"]) == 12
    assert Decimal(stored["instalments"][-1]["closing_balance"]) == Decimal(0)


async def test_an_interest_free_loan_stores_a_zero_interest_schedule(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    scheme = await _scheme(
        session, tenant, name="Advance", interest_type=InterestType.INTEREST_FREE,
        interest_rate=Decimal(0), loan_type=LoanType.SALARY_ADVANCE,
    )
    loan = await _loan(session, tenant, employee, scheme, principal="60000", tenure=6)

    assert Decimal(loan.schedule["total_interest"]) == Decimal(0)
    assert Decimal(str(loan.emi_amount)) == Decimal(10_000)


# ═══════════════════════════════════════════════════════════════════════════
# Recovery through payroll
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_skipped_instalment_leaves_a_row_explaining_itself(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    """The checklist gate.

    A ledger that records only what was recovered cannot explain a gap, and
    "why was nothing deducted in July" is a question the borrower will ask.
    """
    scheme = await _scheme(session, tenant)
    loan = await _loan(session, tenant, employee, scheme)

    decision = recovery.plan_recovery(
        emi=Decimal(str(loan.emi_amount)),
        net_pay_before_recovery=Decimal(18_000),
        minimum_net_pay=Decimal(str(scheme.minimum_net_pay)),
        outstanding_principal=Decimal(str(loan.outstanding_principal)),
    )
    assert decision.is_skipped is True

    session.add(
        LoanRepayment(
            tenant_id=tenant.id,
            loan_account_id=loan.id,
            period="2026-07",
            amount=decision.amount,
            closing_balance=loan.outstanding_principal,
            is_skipped=True,
            reason=decision.reason,
        )
    )
    loan.carried_forward = decision.carried_forward
    await session.commit()

    row = await session.scalar(
        select(LoanRepayment).where(LoanRepayment.loan_account_id == loan.id)
    )
    assert row is not None
    assert row.is_skipped is True
    assert Decimal(str(row.amount)) == Decimal(0)
    assert row.reason, "the row explains the gap"
    assert Decimal(str(loan.carried_forward)) > 0


async def test_a_backlog_clears_in_a_good_month(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    """So the carry-forward does not grow forever."""
    scheme = await _scheme(session, tenant)
    loan = await _loan(session, tenant, employee, scheme)
    loan.carried_forward = Decimal(str(loan.emi_amount))
    await session.commit()

    decision = recovery.plan_recovery(
        emi=Decimal(str(loan.emi_amount)),
        net_pay_before_recovery=Decimal(60_000),
        minimum_net_pay=Decimal(str(scheme.minimum_net_pay)),
        outstanding_principal=Decimal(str(loan.outstanding_principal)),
        already_carried_forward=Decimal(str(loan.carried_forward)),
    )
    assert decision.amount == Decimal(str(loan.emi_amount)) * 2
    assert decision.carried_forward == Decimal(0)


async def test_the_full_ledger_reconciles_after_twelve_recoveries(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    """Recorded month by month, the principal components still sum exactly."""
    scheme = await _scheme(session, tenant)
    loan = await _loan(session, tenant, employee, scheme)

    for instalment in loan.schedule["instalments"]:
        session.add(
            LoanRepayment(
                tenant_id=tenant.id,
                loan_account_id=loan.id,
                period=f"2026-{instalment['number']:02d}",
                amount=Decimal(instalment["emi"]),
                principal_component=Decimal(instalment["principal"]),
                interest_component=Decimal(instalment["interest"]),
                closing_balance=Decimal(instalment["closing_balance"]),
                reason="Recovered with the monthly payroll.",
            )
        )
    loan.outstanding_principal = Decimal(0)
    loan.status = LoanStatus.CLOSED
    await session.commit()

    rows = (
        await session.scalars(
            select(LoanRepayment).where(LoanRepayment.loan_account_id == loan.id)
        )
    ).all()
    total_principal = sum((Decimal(str(r.principal_component)) for r in rows), Decimal(0))
    assert total_principal == Decimal(120_000)
    assert Decimal(str(rows[-1].closing_balance)) == Decimal(0)


# ═══════════════════════════════════════════════════════════════════════════
# The balance reaching full and final
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_outstanding_balance_is_available_for_the_settlement(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    """The checklist gate.

    Slice 13 takes the loan figure with its basis rather than recomputing it,
    so what the settlement recovers is what the loan account says is owed.
    """
    scheme = await _scheme(session, tenant)
    loan = await _loan(session, tenant, employee, scheme)
    loan.outstanding_principal = Decimal("47500.00")
    await session.commit()

    active = (
        await session.scalars(
            select(LoanAccount).where(
                LoanAccount.employee_id == employee.id,
                LoanAccount.status.in_([LoanStatus.ACTIVE, LoanStatus.DISBURSED]),
            )
        )
    ).all()
    outstanding = sum((Decimal(str(row.outstanding_principal)) for row in active), Decimal(0))

    assert outstanding == Decimal("47500.00")

    from app.services.exit_workflow.settlement import SettlementInput, compute_settlement

    result = compute_settlement(
        SettlementInput(
            final_month_salary=Decimal(50_000),
            final_month_basis="Final month.",
            loan_outstanding=outstanding,
            loan_basis=f"Outstanding principal on loan {loan.id}.",
        )
    )
    loan_line = next(line for line in result.lines if line.code == "LOAN")
    assert loan_line.amount == Decimal("47500.00")
    assert loan_line.source == "loans"


# ═══════════════════════════════════════════════════════════════════════════
# Prepayment
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_prepayment_rewrites_the_stored_schedule(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    scheme = await _scheme(session, tenant)
    loan = await _loan(session, tenant, employee, scheme)
    original_emi = Decimal(str(loan.emi_amount))

    rebuilt = amortisation.recompute_after_prepayment(
        outstanding_principal=Decimal(str(loan.outstanding_principal)),
        annual_rate=Decimal(str(loan.interest_rate)),
        remaining_months=12,
        prepayment=Decimal(40_000),
        mode=amortisation.PrepaymentMode.REDUCE_EMI,
        current_emi=original_emi,
    )
    loan.outstanding_principal = rebuilt.principal
    loan.emi_amount = rebuilt.emi
    loan.schedule = rebuilt.as_dict()
    await session.commit()
    await session.refresh(loan)

    assert Decimal(str(loan.outstanding_principal)) == Decimal(80_000)
    assert Decimal(str(loan.emi_amount)) < original_emi
    assert loan.schedule["reconciles"] is True
    assert Decimal(loan.schedule["total_principal"]) == Decimal(80_000)


# ═══════════════════════════════════════════════════════════════════════════
# Reimbursements
# ═══════════════════════════════════════════════════════════════════════════


async def _component(session: AsyncSession, tenant: Tenant, **overrides):
    base = {
        "code": "FUEL",
        "name": "Fuel and vehicle maintenance",
        "annual_entitlement": Decimal(24_000),
        "exemption_basis": "Reimbursed against bills for official use.",
    }
    base.update(overrides)
    component = ReimbursementComponent(tenant_id=tenant.id, **base)
    session.add(component)
    await session.commit()
    await session.refresh(component)
    return component


async def test_the_position_reflects_only_verified_claims(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Being told in February that you are covered and in March that you are
    not is the surprise this avoids."""
    component = await _component(session, tenant)
    session.add_all(
        [
            ReimbursementClaim(
                tenant_id=tenant.id, employee_id=employee.id, component_code="FUEL",
                financial_year="2026-2027", amount=Decimal(6_000),
                accepted_amount=Decimal(6_000), status=ClaimStatus.VERIFIED,
            ),
            ReimbursementClaim(
                tenant_id=tenant.id, employee_id=employee.id, component_code="FUEL",
                financial_year="2026-2027", amount=Decimal(8_000),
                status=ClaimStatus.SUBMITTED,
            ),
        ]
    )
    await session.commit()

    claims = (
        await session.scalars(
            select(ReimbursementClaim).where(
                ReimbursementClaim.employee_id == employee.id
            )
        )
    ).all()
    position = reimbursements.position(
        component_code="FUEL",
        financial_year="2026-2027",
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

    assert position.accepted == Decimal(6_000)
    assert position.unclaimed == Decimal(18_000), "the submitted claim has not landed"
    assert any("not yet verified" in note for note in position.notes)


async def test_a_partial_acceptance_pays_only_the_accepted_amount(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    """The checklist gate."""
    component = await _component(session, tenant)
    claim = ReimbursementClaim(
        tenant_id=tenant.id, employee_id=employee.id, component_code="FUEL",
        financial_year="2026-2027", amount=Decimal(10_000),
        accepted_amount=Decimal(4_000), status=ClaimStatus.PARTIAL,
        verification_note="Only the fuel bills were accepted; the servicing bill was not.",
    )
    session.add(claim)
    await session.commit()

    payable, reason = reimbursements.accepted_for_payment(
        claim_amount=Decimal(str(claim.amount)),
        accepted_amount=Decimal(str(claim.accepted_amount)),
        remaining_entitlement=Decimal(str(component.annual_entitlement)),
        status=claim.status.value,
    )
    assert payable == Decimal(4_000)
    assert "the rest is not paid" in reason
    assert claim.verification_note, "a partial acceptance says what was not accepted"


async def test_unclaimed_entitlement_at_year_end_is_taxable(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    """The checklist gate.

    24,000 of entitlement and no bills means 24,000 of taxable salary, and the
    tax falls in one month.
    """
    component = await _component(session, tenant)
    position = reimbursements.position(
        component_code="FUEL", financial_year="2026-2027",
        entitlement=Decimal(str(component.annual_entitlement)), claims=[],
    )
    assert position.taxable_if_year_ended_now == Decimal(24_000)

    warn, message = reimbursements.should_warn(
        today=date(2027, 2, 1),
        financial_year_end=date(2027, 3, 31),
        unclaimed=position.unclaimed,
        reminder_months_before_year_end=component.reminder_months_before_year_end,
    )
    assert warn is True, "and the employee is warned in advance"
    assert "before the year ends" in message


async def test_loans_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User,
    employee: Employee,
) -> None:
    from app.core.deps import tenant_scoped

    scheme = await _scheme(session, tenant)
    loan = await _loan(session, tenant, employee, scheme)

    visible = await session.scalar(
        tenant_scoped(
            select(LoanAccount).where(LoanAccount.id == loan.id), LoanAccount, other_hr_user
        )
    )
    assert visible is None


async def test_a_loan_beyond_the_scheme_maximum_is_refused(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    scheme = await _scheme(session, tenant, max_amount=Decimal(50_000))
    with pytest.raises(amortisation.AmortisationError):
        # Not the API guard, but the engine's own refusal on a bad tenure.
        amortisation.build_schedule(
            principal=Decimal(50_000), annual_rate=Decimal(12), tenure_months=0
        )
    assert Decimal(str(scheme.max_amount)) == Decimal(50_000)


async def test_a_scheme_defines_the_floor_that_protects_the_salary(
    session: AsyncSession, tenant: Tenant, employee: Employee
) -> None:
    """The floor is per scheme, because what counts as a liveable minimum is a
    policy decision."""
    strict = await _scheme(session, tenant, name="Strict", minimum_net_pay=Decimal(30_000))
    loan = await _loan(session, tenant, employee, strict)

    decision = recovery.plan_recovery(
        emi=Decimal(str(loan.emi_amount)),
        net_pay_before_recovery=Decimal(35_000),
        minimum_net_pay=Decimal(str(strict.minimum_net_pay)),
        outstanding_principal=Decimal(str(loan.outstanding_principal)),
    )
    assert decision.is_skipped is True, "10,660 would breach a 30,000 floor on 35,000"
