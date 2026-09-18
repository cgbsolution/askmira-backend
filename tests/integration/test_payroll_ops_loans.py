"""W3 loans and reimbursements against a real database: employees act on their
own records only, nobody decides their own, and the recovery ledger the payroll
run writes respects the net-pay floor."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.loans import (
    ClaimStatus,
    InterestType,
    LoanRepayment,
    LoanScheme,
    LoanStatus,
    LoanType,
    ReimbursementComponent,
)
from app.models.notifications import Notification
from app.models.user import UserRole
from app.services.loans import service as loan_service
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_ops_helpers import (
    PNG_BYTES,
    person,
    storage_root,  # noqa: F401 - fixture
)

pytestmark = requires_db

FY = "2026-2027"


async def _people(session, tenant, legal_entity, location):
    borrower = await person(session, tenant, legal_entity, location, code="EMP1",
                            role=UserRole.EMPLOYEE)
    colleague = await person(session, tenant, legal_entity, location, code="EMP2",
                             role=UserRole.EMPLOYEE)
    admin = await person(session, tenant, legal_entity, location, code="ADM1",
                         role=UserRole.HR_ADMIN)
    return borrower, colleague, admin


async def _scheme(session, tenant, **overrides) -> LoanScheme:
    base = {
        "name": "Staff loan", "loan_type": LoanType.COMPANY_LOAN,
        "interest_type": InterestType.REDUCING, "interest_rate": Decimal(12),
        "max_tenure_months": 24, "max_amount": Decimal(300000),
        "minimum_net_pay": Decimal(15000),
    }
    base.update(overrides)
    scheme = LoanScheme(tenant_id=tenant.id, **base)
    session.add(scheme)
    await session.commit()
    return scheme


async def test_an_employee_applies_only_for_themselves(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    (borrower, borrower_user), (colleague, _), _ = await _people(
        session, tenant, legal_entity, location
    )
    scheme = await _scheme(session, tenant)
    loan = await loan_service.apply_for_loan(
        session, borrower_user, employee_id=borrower.id, scheme_id=scheme.id,
        principal=Decimal(120000), tenure_months=12, purpose="Medical",
    )
    await session.commit()
    assert loan.status is LoanStatus.APPLIED

    with pytest.raises(HTTPException) as exc:
        await loan_service.apply_for_loan(
            session, borrower_user, employee_id=colleague.id, scheme_id=scheme.id,
            principal=Decimal(1000), tenure_months=1, purpose=None,
        )
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await loan_service.apply_for_loan(
            session, borrower_user, employee_id=borrower.id, scheme_id=scheme.id,
            principal=Decimal(1000), tenure_months=1, purpose=None,
        )
    assert exc.value.status_code == 409, "one open application per scheme"


async def test_nobody_decides_their_own_loan_and_a_rejection_needs_a_reason(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    (borrower, borrower_user), _, (admin, admin_user) = await _people(
        session, tenant, legal_entity, location
    )
    scheme = await _scheme(session, tenant)
    own = await loan_service.apply_for_loan(
        session, admin_user, employee_id=admin.id, scheme_id=scheme.id,
        principal=Decimal(50000), tenure_months=6, purpose=None,
    )
    theirs = await loan_service.apply_for_loan(
        session, borrower_user, employee_id=borrower.id, scheme_id=scheme.id,
        principal=Decimal(50000), tenure_months=6, purpose=None,
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await loan_service.decide_loan(session, admin_user, own.id, approve=True, note=None)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await loan_service.decide_loan(session, admin_user, theirs.id, approve=False, note="")
    assert exc.value.status_code == 422

    rejected = await loan_service.decide_loan(
        session, admin_user, theirs.id, approve=False, note="Outside the policy this year."
    )
    await session.commit()
    assert rejected.status is LoanStatus.REJECTED
    assert rejected.decision_note == "Outside the policy this year."


async def test_the_recovery_ledger_respects_the_floor_and_is_idempotent(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    (borrower, borrower_user), (_, colleague_user), (_, admin_user) = await _people(
        session, tenant, legal_entity, location
    )
    scheme = await _scheme(session, tenant)
    loan = await loan_service.apply_for_loan(
        session, borrower_user, employee_id=borrower.id, scheme_id=scheme.id,
        principal=Decimal(120000), tenure_months=12, purpose=None,
    )
    await loan_service.decide_loan(session, admin_user, loan.id, approve=True, note=None)
    await loan_service.disburse_loan(session, admin_user, loan.id, disbursed_on=date(2026, 6, 1))
    await session.commit()

    skipped = await loan_service.record_recovery(
        session, admin_user, loan_id=loan.id, period="2026-07",
        net_pay_before_recovery=Decimal(18000),
    )
    await session.commit()
    assert skipped.is_skipped is True and skipped.reason
    assert Decimal(str(loan.carried_forward)) == Decimal(str(loan.emi_amount))
    assert loan.status is LoanStatus.DISBURSED

    again = await loan_service.record_recovery(
        session, admin_user, loan_id=loan.id, period="2026-07",
        net_pay_before_recovery=Decimal(90000),
    )
    assert again.id == skipped.id, "a re-run of the same month records nothing new"

    recovered = await loan_service.record_recovery(
        session, admin_user, loan_id=loan.id, period="2026-08",
        net_pay_before_recovery=Decimal(90000),
    )
    await session.commit()
    assert recovered.is_skipped is False
    assert Decimal(str(recovered.amount)) == Decimal(str(loan.emi_amount)) * 2
    assert Decimal(str(loan.carried_forward)) == Decimal(0)
    assert Decimal(str(loan.outstanding_principal)) < Decimal(120000)
    assert loan.status is LoanStatus.ACTIVE
    assert (
        Decimal(str(recovered.principal_component)) + Decimal(str(recovered.interest_component))
        == Decimal(str(recovered.amount))
    )

    detail, ledger = await loan_service.loan_detail(session, borrower_user, loan.id)
    assert [row.period for row in ledger] == ["2026-07", "2026-08"]
    with pytest.raises(HTTPException) as exc:
        await loan_service.loan_detail(session, colleague_user, loan.id)
    assert exc.value.status_code == 403
    rows = await session.scalar(select(func.count()).select_from(LoanRepayment))
    assert rows == 2


async def _component(session, tenant, **overrides) -> ReimbursementComponent:
    base = {"code": "FUEL", "name": "Fuel", "annual_entitlement": Decimal(24000),
            "reminder_months_before_year_end": 11}
    base.update(overrides)
    component = ReimbursementComponent(tenant_id=tenant.id, **base)
    session.add(component)
    await session.commit()
    return component


async def test_claims_are_your_own_need_a_bill_and_are_verified_by_someone_else(
    session: AsyncSession, tenant, legal_entity, location, storage_root  # noqa: F811
) -> None:
    (borrower, borrower_user), (colleague, colleague_user), (admin, admin_user) = await _people(
        session, tenant, legal_entity, location
    )
    await _component(session, tenant)

    with pytest.raises(HTTPException) as exc:
        await loan_service.submit_claim(
            session, borrower_user, employee_id=colleague.id, component_code="FUEL",
            financial_year=FY, amount=Decimal(1000), bill_date=None, bill_reference=None,
            bill=PNG_BYTES,
        )
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await loan_service.submit_claim(
            session, borrower_user, employee_id=borrower.id, component_code="FUEL",
            financial_year=FY, amount=Decimal(1000), bill_date=None, bill_reference=None,
            bill=None,
        )
    assert exc.value.status_code == 422

    claim = await loan_service.submit_claim(
        session, borrower_user, employee_id=borrower.id, component_code="FUEL",
        financial_year=FY, amount=Decimal(4000), bill_date=date(2026, 5, 2),
        bill_reference="INV-9", bill=PNG_BYTES,
    )
    own = await loan_service.submit_claim(
        session, admin_user, employee_id=admin.id, component_code="FUEL",
        financial_year=FY, amount=Decimal(100), bill_date=None, bill_reference=None,
        bill=PNG_BYTES,
    )
    await session.commit()

    mine = await loan_service.list_claims(session, borrower_user, employee_id=None)
    assert [row[0].id for row in mine] == [claim.id]
    with pytest.raises(HTTPException):
        await loan_service.list_claims(session, borrower_user, employee_id=colleague.id)

    with pytest.raises(HTTPException) as exc:
        await loan_service.verify_claim(
            session, admin_user, own.id, decision=ClaimStatus.VERIFIED,
            accepted_amount=Decimal(0), note=None,
        )
    assert exc.value.status_code == 403

    partial = await loan_service.verify_claim(
        session, admin_user, claim.id, decision=ClaimStatus.PARTIAL,
        accepted_amount=Decimal(2500), note="Only the May bill is legible.",
    )
    await session.commit()
    assert Decimal(str(partial.accepted_amount)) == Decimal(2500)

    data, mime, _ = await loan_service.claim_bill(session, borrower_user, claim.id)
    assert data == PNG_BYTES and mime == "image/png"
    with pytest.raises(HTTPException) as exc:
        await loan_service.claim_bill(session, colleague_user, claim.id)
    assert exc.value.status_code == 403

    positions = await loan_service.entitlement_positions(
        session, borrower_user, employee_id=borrower.id, financial_year=FY
    )
    assert positions[0]["accepted"] == "2500.00"
    assert positions[0]["unclaimed"] == "21500.00"


async def test_unclaimed_entitlement_reminders_go_once_a_month(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    await _people(session, tenant, legal_entity, location)
    await _component(session, tenant)
    today = date(2027, 2, 10)

    first = await loan_service.send_unclaimed_entitlement_reminders(
        session, tenant.id, today=today
    )
    second = await loan_service.send_unclaimed_entitlement_reminders(
        session, tenant.id, today=today
    )
    assert first == 3, "all three employees have the whole entitlement unclaimed"
    assert second == 0
    count = await session.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.event_key == loan_service.EVENT_UNCLAIMED
        )
    )
    assert count >= first
