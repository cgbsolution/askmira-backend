"""W3 exits against a real database: who may start and see an exit, who signs
clearance off, the settlement prefill, holds gating letters, the statement
document, and encashment decided by the reporting line."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exit_benefits import (
    EncashmentPolicy,
    EncashmentRequest,
    EncashmentStatus,
    ExitReason,
    GratuityPayout,
)
from app.models.exit_workflow import (
    ClearanceItem,
    ClearanceOwnerRole,
    ClearanceStatus,
    ClearanceTemplate,
    ExitType,
    SettlementStatus,
)
from app.models.loans import InterestType, LoanAccount, LoanScheme, LoanStatus, LoanType
from app.models.user import UserRole
from app.services.exit_benefits import service as benefits_service
from app.services.exit_workflow import operations
from app.services.exit_workflow import service as exit_service
from app.services.exit_workflow import settlement as engine
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_ops_helpers import (
    person,
    storage_root,  # noqa: F401 - fixture
)

pytestmark = requires_db


async def _org(session, tenant, legal_entity, location):
    boss, boss_user = await person(
        session, tenant, legal_entity, location, code="MGR1", role=UserRole.EMPLOYEE
    )
    other, other_user = await person(
        session, tenant, legal_entity, location, code="MGR2", role=UserRole.EMPLOYEE
    )
    leaver, leaver_user = await person(
        session, tenant, legal_entity, location, code="EMP1", role=UserRole.EMPLOYEE, manager=boss
    )
    hr, hr_user = await person(
        session, tenant, legal_entity, location, code="HR1", role=UserRole.ADMIN
    )
    it_admin, it_user = await person(
        session, tenant, legal_entity, location, code="IT1", role=UserRole.EMPLOYEE
    )
    session.add_all(
        [
            ClearanceTemplate(
                tenant_id=tenant.id,
                department="IT",
                description="Laptop returned",
                owner_role=ClearanceOwnerRole.NAMED_USER.value,
                owner_user_id=it_user.id,
                display_order=1,
            ),
            ClearanceTemplate(
                tenant_id=tenant.id,
                department="Manager",
                description="Handover done",
                owner_role=ClearanceOwnerRole.REPORTING_MANAGER.value,
                display_order=2,
            ),
            ClearanceTemplate(
                tenant_id=tenant.id,
                department="Finance",
                description="Advances settled",
                display_order=3,
            ),
        ]
    )
    await session.commit()
    return {
        "boss": boss_user,
        "other": other_user,
        "leaver": leaver,
        "leaver_user": leaver_user,
        "hr": hr_user,
        "it": it_user,
    }


async def _exit(session, org, *, by=None):
    record = await exit_service.initiate_exit(
        session,
        by or org["hr"],
        employee_id=org["leaver"].id,
        exit_type=ExitType.RESIGNATION,
        resignation_date=date(2026, 6, 1),
        approved_last_working_day=date(2026, 8, 31),
        notice_period_days=90,
    )
    await session.commit()
    return record


async def test_a_manager_starts_an_exit_only_for_their_reporting_line(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    with pytest.raises(HTTPException) as exc:
        await _exit(session, org, by=org["other"])
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await exit_service.initiate_exit(
            session,
            org["leaver_user"],
            employee_id=org["leaver"].id,
            exit_type=ExitType.RESIGNATION,
            resignation_date=date(2026, 6, 1),
        )
    assert exc.value.status_code == 403

    record = await _exit(session, org, by=org["boss"])
    items = (
        await session.scalars(
            select(ClearanceItem).where(ClearanceItem.exit_record_id == record.id)
        )
    ).all()
    assert {item.owner_role for item in items} == {"named_user", "reporting_manager", "hr"}

    assert [row["id"] for row in await operations.list_exits(session, org["boss"])] == [record.id]
    assert await operations.list_exits(session, org["other"]) == []
    it_view = await operations.list_exits(session, org["it"])
    assert [row["id"] for row in it_view] == [record.id], "a named owner sees the exit"
    with pytest.raises(HTTPException) as exc:
        await operations.visible_exit(session, org["other"], record.id)
    assert exc.value.status_code == 404


async def test_clearance_is_signed_off_by_its_owner_or_hr_only(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    record = await _exit(session, org)
    items = {
        item.department: item
        for item in (
            await session.scalars(
                select(ClearanceItem).where(ClearanceItem.exit_record_id == record.id)
            )
        ).all()
    }

    it_view = {
        row["department"]: row
        for row in await operations.list_clearance(session, org["it"], record.id)
    }
    assert it_view["IT"]["can_update"] is True
    assert it_view["Manager"]["can_update"] is False
    assert it_view["IT"]["owner_name"] == "It1 Test"

    with pytest.raises(HTTPException) as exc:
        await exit_service.clear_item(
            session, org["boss"], item_id=items["IT"].id, outcome=ClearanceStatus.CLEARED
        )
    assert exc.value.status_code == 403

    await exit_service.clear_item(
        session,
        org["it"],
        item_id=items["IT"].id,
        outcome=ClearanceStatus.CLEARED_WITH_RECOVERY,
        recovery_amount=Decimal(15000),
    )
    await exit_service.clear_item(
        session, org["boss"], item_id=items["Manager"].id, outcome=ClearanceStatus.CLEARED
    )
    with pytest.raises(HTTPException) as exc:
        await exit_service.clear_item(
            session, org["boss"], item_id=items["Finance"].id, outcome=ClearanceStatus.CLEARED
        )
    assert exc.value.status_code == 403
    await exit_service.clear_item(
        session, org["hr"], item_id=items["Finance"].id, outcome=ClearanceStatus.CLEARED
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await exit_service.clear_item(
            session,
            org["leaver_user"],
            item_id=items["Finance"].id,
            outcome=ClearanceStatus.CLEARED,
        )
    assert exc.value.status_code == 403


async def _cleared(session, org):
    record = await _exit(session, org)
    for item in (
        await session.scalars(
            select(ClearanceItem).where(ClearanceItem.exit_record_id == record.id)
        )
    ).all():
        await exit_service.clear_item(
            session, org["hr"], item_id=item.id, outcome=ClearanceStatus.CLEARED
        )
    await session.commit()
    return record


async def test_the_settlement_prefill_reads_what_other_modules_hold(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    record = await _cleared(session, org)
    scheme = LoanScheme(
        tenant_id=tenant.id,
        name="Advance",
        loan_type=LoanType.SALARY_ADVANCE,
        interest_type=InterestType.INTEREST_FREE,
        max_tenure_months=6,
    )
    session.add(scheme)
    await session.flush()
    session.add_all(
        [
            LoanAccount(
                tenant_id=tenant.id,
                employee_id=org["leaver"].id,
                scheme_id=scheme.id,
                status=LoanStatus.ACTIVE,
                principal=Decimal(60000),
                interest_type=InterestType.INTEREST_FREE,
                tenure_months=6,
                emi_amount=Decimal(10000),
                outstanding_principal=Decimal(40000),
            ),
            GratuityPayout(
                tenant_id=tenant.id,
                employee_id=org["leaver"].id,
                exit_date=date(2026, 8, 31),
                exit_reason=ExitReason.RESIGNATION,
                service_days=3100,
                service_years=Decimal(8),
                monthly_wages=Decimal(40000),
                gross_amount=Decimal(184615),
                exempt_amount=Decimal(184615),
            ),
        ]
    )
    await session.commit()

    sources = await operations.settlement_sources(session, org["hr"], record.id)
    by_field = {s["field"]: s for s in sources["suggestions"]}
    assert by_field["loan_outstanding"]["amount"] == Decimal(40000)
    assert by_field["gratuity"]["amount"] == Decimal(184615)
    assert "8" in by_field["gratuity"]["basis"]
    assert sources["notice"]["shortfall_days"] == 0


async def test_a_hold_blocks_letters_and_nobody_approves_their_own_settlement(
    session: AsyncSession,
    tenant,
    legal_entity,
    location,
    storage_root,  # noqa: F811
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    record = await _cleared(session, org)
    settlement = await exit_service.compute_settlement(
        session,
        org["hr"],
        exit_id=record.id,
        inputs=engine.SettlementInput(
            final_month_salary=Decimal(45000),
            final_month_basis="18 of 30 days.",
            loan_outstanding=Decimal(60000),
            loan_basis="Advance outstanding.",
        ),
    )
    await session.commit()
    assert settlement.net_amount == Decimal("-15000.00")

    gate = await operations.letter_gate(session, org["hr"], org["leaver"].id)
    assert gate["allowed"] is False and gate["requires_settlement"] is True

    await exit_service.hold_settlement(
        session, org["hr"], settlement_id=settlement.id, reason="Laptop dispute"
    )
    await session.commit()
    gate = await operations.letter_gate(session, org["hr"], org["leaver"].id)
    assert gate["allowed"] is False and "Laptop dispute" in gate["reason"]
    with pytest.raises(HTTPException) as exc:
        await operations.issue_exit_letter(session, org["hr"], record.id, kind="relieving")
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        await operations.approve_settlement(session, org["hr"], settlement.id)
    assert exc.value.status_code == 409, "release the hold first"

    await operations.release_hold(session, org["hr"], settlement.id, note="Laptop returned")
    await session.commit()

    leaver_hr = org["leaver_user"]
    leaver_hr.role = UserRole.ADMIN
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await operations.approve_settlement(session, leaver_hr, settlement.id)
    assert exc.value.status_code == 403

    approved = await operations.approve_settlement(session, org["hr"], settlement.id)
    await session.commit()
    assert approved.status is SettlementStatus.DEMAND_RAISED
    # Settled, but still employed: the letter states the relieving date, which is
    # recorded when access is revoked on the last working day.
    gate = await operations.letter_gate(session, org["hr"], org["leaver"].id)
    assert gate["allowed"] is False and "once the employee has left" in gate["reason"]
    org["leaver"].date_of_exit = date(2026, 8, 31)
    await session.commit()
    gate = await operations.letter_gate(session, org["hr"], org["leaver"].id)
    assert gate["allowed"] is True

    letter = await operations.issue_statement(session, org["hr"], settlement.id)
    await session.commit()
    assert "Amount due from the employee to the company" in letter.merged_html
    assert "15,000.00" in letter.merged_html
    assert "Advance outstanding." in letter.merged_html
    data, _ = await operations.statement_pdf(session, org["hr"], settlement.id)
    assert data.startswith(b"%PDF")


async def test_the_letter_policy_is_configurable(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    await _exit(session, org)
    await operations.set_exit_policy(session, org["hr"], letters_require_settlement=False)
    org["leaver"].date_of_exit = date(2026, 8, 31)
    await session.commit()
    gate = await operations.letter_gate(session, org["hr"], org["leaver"].id)
    assert gate == {
        "allowed": True,
        "reason": "",
        "requires_settlement": False,
        "settlement_status": None,
        "hold_reason": None,
    }


async def test_encashment_is_decided_by_the_reporting_line_not_the_requester(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    session.add(
        EncashmentPolicy(
            tenant_id=tenant.id,
            leave_type_code="EL",
            encashable_in_service=True,
            formula_basis=["BASIC"],
            days_per_month_divisor=30,
        )
    )
    request = EncashmentRequest(
        tenant_id=tenant.id,
        employee_id=org["leaver"].id,
        leave_type_code="EL",
        financial_year="2026-27",
        days_requested=Decimal(5),
    )
    session.add(request)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await benefits_service.decide_encashment(session, org["other"], request.id, approve=True)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await benefits_service.decide_encashment(
            session, org["leaver_user"], request.id, approve=True
        )
    assert exc.value.status_code == 403

    boss_view = await benefits_service.list_encashments(
        session, org["boss"], employee_id=None, pending_only=True
    )
    assert [row.id for row in boss_view] == [request.id]
    assert (
        await benefits_service.list_encashments(
            session, org["other"], employee_id=None, pending_only=True
        )
        == []
    )

    decided = await benefits_service.decide_encashment(
        session, org["boss"], request.id, approve=True, note="OK"
    )
    assert decided.status is EncashmentStatus.APPROVED


async def test_the_scheduled_accrual_needs_approved_configuration(
    session: AsyncSession, tenant
) -> None:
    with pytest.raises(HTTPException) as exc:
        await benefits_service.accrue_gratuity_for_tenant(
            session, tenant.id, as_of=datetime.now(UTC).date()
        )
    assert exc.value.status_code == 409


async def test_claims_owed_to_a_leaver_are_settled_once_in_full_and_final(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    """An approved expense claim is suggested, must be paid in full, and is then
    marked so that no payroll run pays it again."""
    from app.models.expenses import (
        CityTierEnum,
        ClaimKindEnum,
        ExpenseClaim,
        ExpenseClaimStatusEnum,
        PolicyVerdictEnum,
    )

    org = await _org(session, tenant, legal_entity, location)
    record = await _cleared(session, org)
    claim = ExpenseClaim(
        tenant_id=tenant.id,
        employee_id=org["leaver"].id,
        category_key="local_travel",
        city_tier=CityTierEnum.A,
        kind=ClaimKindEnum.RECEIPTED,
        amount=Decimal("2400.00"),
        original_amount=Decimal("2400.00"),
        original_currency="INR",
        incurred_on=date(2026, 8, 20),
        has_receipt=True,
        status=ExpenseClaimStatusEnum.APPROVED,
        policy_verdict=PolicyVerdictEnum.WITHIN_POLICY,
        verdict_message="Within policy.",
        evaluated_at=datetime.now(UTC),
    )
    session.add(claim)
    await session.commit()

    sources = await operations.settlement_sources(session, org["hr"], record.id)
    suggestion = next(s for s in sources["suggestions"] if s["field"] == "pending_reimbursements")
    assert suggestion["amount"] == Decimal("2400.00")
    assert "local_travel" in suggestion["basis"]

    # A settlement that leaves the claim out cannot be approved.
    short = await exit_service.compute_settlement(
        session,
        org["hr"],
        exit_id=record.id,
        inputs=engine.SettlementInput(
            final_month_salary=Decimal(45000), final_month_basis="Full month."
        ),
    )
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await operations.approve_settlement(session, org["hr"], short.id)
    assert exc.value.status_code == 409

    settlement = await exit_service.compute_settlement(
        session,
        org["hr"],
        exit_id=record.id,
        inputs=engine.SettlementInput(
            final_month_salary=Decimal(45000),
            final_month_basis="Full month.",
            pending_reimbursements=Decimal("2400.00"),
            reimbursement_basis=suggestion["basis"],
        ),
    )
    await session.commit()
    await operations.approve_settlement(session, org["hr"], settlement.id)
    await session.commit()
    await session.refresh(claim)
    assert claim.settled_in_settlement_id == settlement.id
    assert claim.status is ExpenseClaimStatusEnum.REIMBURSED

    after = await operations.settlement_sources(session, org["hr"], record.id)
    assert not [s for s in after["suggestions"] if s["field"] == "pending_reimbursements"]

    # Recomputing gives the claim back to the open pool.
    await exit_service.compute_settlement(
        session,
        org["hr"],
        exit_id=record.id,
        inputs=engine.SettlementInput(
            final_month_salary=Decimal(45000),
            final_month_basis="Full month.",
            pending_reimbursements=Decimal("2400.00"),
            reimbursement_basis=suggestion["basis"],
        ),
    )
    await session.commit()
    await session.refresh(claim)
    assert claim.settled_in_settlement_id is None
    assert claim.status is ExpenseClaimStatusEnum.APPROVED


async def test_an_encashment_the_policy_reduces_to_nothing_is_refused_with_the_reason(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    """Not a 0-day request for a manager to approve: the employee is told why now."""
    from app.models.leave import LeaveType

    org = await _org(session, tenant, legal_entity, location)
    session.add(LeaveType(tenant_id=tenant.id, name="Earned Leave", code="EL"))
    session.add(
        EncashmentPolicy(
            tenant_id=tenant.id,
            leave_type_code="EL",
            encashable_in_service=True,
            formula_basis=["BASIC"],
            days_per_month_divisor=30,
            min_balance_after_encashment=Decimal(10),
        )
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await benefits_service.request_encashment(
            session,
            org["leaver_user"],
            employee_id=org["leaver"].id,
            leave_type_code="EL",
            days=Decimal(500),
            financial_year="2026-27",
        )
    assert exc.value.status_code == 422
    assert exc.value.detail.startswith("No EL can be encashed right now")
    assert not (await session.scalars(select(EncashmentRequest))).all()


async def test_leave_is_encashed_at_exit_only_once_the_last_working_day_arrives(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    """Until the last day the employee can still take leave, and more accrues."""
    from datetime import timedelta

    org = await _org(session, tenant, legal_entity, location)
    session.add(
        EncashmentPolicy(
            tenant_id=tenant.id,
            leave_type_code="EL",
            encashable_on_exit=True,
            formula_basis=["BASIC"],
            days_per_month_divisor=30,
        )
    )
    today = date.today()
    record = await exit_service.initiate_exit(
        session,
        org["hr"],
        employee_id=org["leaver"].id,
        exit_type=ExitType.RESIGNATION,
        resignation_date=today,
        approved_last_working_day=today + timedelta(days=40),
        notice_period_days=30,
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await benefits_service.compute_exit_encashments(session, org["hr"], exit_id=record.id)
    assert exc.value.status_code == 409
    assert "last working day" in exc.value.detail


async def test_a_settlement_is_approved_only_once_the_employee_has_left(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    """It can be prepared during the notice period, but not signed off before the last day."""
    from datetime import timedelta

    org = await _org(session, tenant, legal_entity, location)
    record = await _cleared(session, org)
    record.approved_last_working_day = date.today() + timedelta(days=20)
    await session.commit()
    settlement = await exit_service.compute_settlement(
        session,
        org["hr"],
        exit_id=record.id,
        inputs=engine.SettlementInput(
            final_month_salary=Decimal(45000), final_month_basis="Paid by the final run."
        ),
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await operations.approve_settlement(session, org["hr"], settlement.id)
    assert exc.value.status_code == 409
    assert "once the employee has left" in exc.value.detail
