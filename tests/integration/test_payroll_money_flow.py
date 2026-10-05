"""The money flows, end to end against a real database.

Approved money reaching the person it is owed to, exactly once:

- an expense claim goes to the line manager, then finance, is paid in the next
  payroll run and is marked reimbursed with that run when the run is approved;
- a trip whose claims exceed its advance has the balance paid in payroll, and
  the claims covered by the advance are not paid a second time;
- a joiner and a leaver are paid only for the days they were employed;
- a full and final settlement is recorded as paid, with its bank reference;
- Form 16 Part B is produced from a completed year's approved payroll.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.exit_workflow import ExitType, SettlementStatus
from app.models.expenses import (
    AdvanceSettlement,
    CityTierEnum,
    ExpenseClaim,
    ExpenseClaimStatusEnum,
    TravelRequestStatusEnum,
)
from app.models.notifications import Notification
from app.models.payroll import (
    CalculationType,
    ComponentType,
    PayrollRunStatus,
    Payslip,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.user import UserRole
from app.schemas.payroll import EmployeeSalaryCreate
from app.schemas.payroll_run import RuleSetCreate
from app.services.attendance.service import local_today
from app.services.exit_workflow import operations
from app.services.exit_workflow import service as exit_service
from app.services.exit_workflow import settlement as settlement_engine
from app.services.expenses import inbox as expense_inbox
from app.services.expenses import reads
from app.services.expenses import service as expenses
from app.services.payroll import inputs as payroll_inputs
from app.services.payroll import payslips as payslip_service
from app.services.payroll import rule_sets as rule_set_service
from app.services.payroll import run as run_service
from app.services.payroll import service as salary_service
from app.services.tax import form16
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_ops_helpers import person
from tests.integration.test_payroll_run_flow import PF_RULES, TDS_RULES, _lock

pytestmark = requires_db

SEPTEMBER = (2026, 9)


async def _structure(session: AsyncSession, tenant, effective: date) -> SalaryStructure:
    """BASIC 40% of CTC (the PF base) and a balancing SPECIAL allowance."""
    basic = SalaryComponent(
        tenant_id=tenant.id, code="BASIC", name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF, base_component_code="CTC",
        value=Decimal(40), counts_toward_pf=True, display_order=1,
    )
    special = SalaryComponent(
        tenant_id=tenant.id, code="SPECIAL", name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE, display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()
    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=effective,
        applicable_grades=[], applicable_locations=[],
    )
    for index, item in enumerate((basic, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id, component_id=item.id, component=item, display_order=index
            )
        )
    session.add(structure)
    await session.commit()
    await session.refresh(structure)
    return structure


async def _org(session: AsyncSession, tenant, entity, location, *, rules_from=date(2026, 4, 1)):
    maker_emp, maker = await person(session, tenant, entity, location, code="MAKER",
                                    role=UserRole.ADMIN)
    checker_emp, checker = await person(session, tenant, entity, location, code="CHECKER",
                                        role=UserRole.SUB_ADMIN)
    boss_emp, boss = await person(session, tenant, entity, location, code="BOSS",
                                  role=UserRole.EMPLOYEE)
    staff_emp, staff = await person(session, tenant, entity, location, code="STAFF",
                                    role=UserRole.EMPLOYEE, manager=boss_emp)
    loner_emp, loner = await person(session, tenant, entity, location, code="LONER",
                                    role=UserRole.EMPLOYEE)
    structure = await _structure(session, tenant, rules_from)
    for employee in (maker_emp, checker_emp, boss_emp, staff_emp, loner_emp):
        await salary_service.assign_salary(
            session, maker, employee.id,
            EmployeeSalaryCreate(structure_id=structure.id, ctc_annual=Decimal("1200000"),
                                 effective_from=rules_from),
        )
    for rule_type, rules in (("pf", PF_RULES), ("tds", TDS_RULES)):
        created = await rule_set_service.create_rule_set(
            session, maker,
            RuleSetCreate(rule_type=rule_type, jurisdiction="IN", effective_from=rules_from,
                          rules=rules, source_reference="Test fixture, not a statutory source"),
        )
        await rule_set_service.approve_rule_set(session, checker, created.id, attest=True)
    return {
        "tenant": tenant, "entity": entity, "location": location, "structure": structure,
        "maker": maker, "checker": checker, "boss": boss, "boss_emp": boss_emp,
        "staff": staff, "staff_emp": staff_emp, "loner": loner, "loner_emp": loner_emp,
    }


async def _claim(session, org, who: str, amount: str, day: int, *, trip=None) -> uuid.UUID:
    out = await expenses.capture_claim(
        session, org[who], employee_id=org[f"{who}_emp"].id, category_key="local_travel",
        city_tier=CityTierEnum.A, amount=Decimal(amount), incurred_on=date(2026, 9, day),
        travel_request_id=trip,
    )
    return uuid.UUID(out["claim_id"])


async def _events(session, user) -> list[str]:
    return list(
        (
            await session.scalars(
                select(Notification.event_key).where(Notification.recipient_user_id == user.id)
            )
        ).all()
    )


async def _slip(session, run_id, employee_id) -> Payslip:
    return await session.scalar(
        select(Payslip).where(Payslip.run_id == run_id, Payslip.employee_id == employee_id)
    )


async def _run(session, org, year: int, month: int):
    session.add(_lock(org["tenant"], org["maker"], year, month))
    await session.commit()
    run = await run_service.create_run(
        session, org["maker"], legal_entity_id=org["entity"].id,
        period_year=year, period_month=month,
    )
    return run


# ═══════════════════════════════════════════════════════════════════════════
# Claim → line manager → finance → payroll run → reimbursed
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_claim_is_approved_twice_paid_once_and_joiners_and_leavers_are_prorated(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    maker, checker, boss, staff = org["maker"], org["checker"], org["boss"], org["staff"]

    # A joiner on the 15th and a leaver whose agreed last day is the 10th.
    joiner, _ = await person(session, tenant, legal_entity, location, code="JOINER",
                             role=UserRole.EMPLOYEE)
    joiner.date_of_joining = date(2026, 9, 15)
    leaver, _ = await person(session, tenant, legal_entity, location, code="LEAVER",
                             role=UserRole.EMPLOYEE)
    future, _ = await person(session, tenant, legal_entity, location, code="FUTURE",
                             role=UserRole.EMPLOYEE)
    future.date_of_joining = date(2026, 10, 5)
    await session.commit()
    for employee, effective in ((joiner, date(2026, 9, 15)), (leaver, date(2026, 4, 1))):
        await salary_service.assign_salary(
            session, maker, employee.id,
            EmployeeSalaryCreate(structure_id=org["structure"].id,
                                 ctc_annual=Decimal("1200000"), effective_from=effective),
        )
    leaver_exit = await exit_service.initiate_exit(
        session, maker, employee_id=leaver.id, exit_type=ExitType.RESIGNATION,
        resignation_date=date(2026, 6, 1), approved_last_working_day=date(2026, 9, 10),
        notice_period_days=90,
    )
    await session.commit()
    leaver_exit_id = leaver_exit.id

    # ── The claim: submitted, it goes to the line manager ───────────────────
    claim_id = await _claim(session, org, "staff", "1250.50", 12)
    claim = await expenses.submit_claim(session, staff, claim_id=claim_id)
    assert claim.status is ExpenseClaimStatusEnum.PENDING_MANAGER
    assert "expenses.claim.awaiting_manager" in await _events(session, boss)

    # Finance cannot jump the queue; nobody decides their own; an outsider cannot.
    with pytest.raises(HTTPException) as exc:
        await expenses.decide_claim(session, maker, claim_id=claim_id, approve=True)
    assert exc.value.status_code == 400 and "line manager" in exc.value.detail
    for intruder in (staff, org["loner"]):
        with pytest.raises(HTTPException) as exc:
            await expenses.decide_claim_as_manager(
                session, intruder, claim_id=claim_id, approve=True
            )
        assert exc.value.status_code == 403

    # The manager sees it in their queue and their inbox.
    team = await reads.team_queue(session, boss)
    assert [row.id for row in team.claims] == [claim_id]
    assert [item["id"] for item in await expense_inbox.manager_claim_items(session, boss)] == [
        claim_id
    ]
    assert await expense_inbox.manager_claim_items(session, org["loner"]) == []

    claim = await expenses.decide_claim_as_manager(
        session, boss, claim_id=claim_id, approve=True, note="Client visit"
    )
    assert claim.status is ExpenseClaimStatusEnum.SUBMITTED
    assert claim.manager_decided_by_user_id == boss.id
    assert "expenses.claim.manager_decided" in await _events(session, staff)
    queue = await reads.review_queue(session, maker, today=date(2026, 9, 30))
    assert claim_id in {row.id for row in queue.claims}

    claim = await expenses.decide_claim(session, maker, claim_id=claim_id, approve=True)
    assert claim.status is ExpenseClaimStatusEnum.APPROVED
    assert "expenses.claim.decided" in await _events(session, staff)
    read = await reads.get_claim(session, staff, claim_id)
    assert read.payment_note == "Approved. It is paid in your next payroll."
    assert read.manager_decided_by_name == boss.full_name

    # Someone with no line manager goes straight to finance.
    lone_id = await _claim(session, org, "loner", "300", 3)
    lone = await expenses.submit_claim(session, org["loner"], claim_id=lone_id)
    assert lone.status is ExpenseClaimStatusEnum.SUBMITTED

    # A trip takes the same two stages.
    trip = await expenses.create_travel_request(
        session, staff, employee_id=org["staff_emp"].id, purpose="Client workshop",
        from_date=date(2026, 10, 12), to_date=date(2026, 10, 14),
    )
    assert trip.status is TravelRequestStatusEnum.PENDING_MANAGER
    trip = await expenses.decide_travel_request_as_manager(
        session, boss, travel_request_id=trip.id, approve=True
    )
    assert trip.status is TravelRequestStatusEnum.SUBMITTED
    trip = await expenses.decide_travel_request(
        session, maker, travel_request_id=trip.id, approve=True
    )
    assert trip.status is TravelRequestStatusEnum.APPROVED
    assert [step["stage"] for step in trip.approvals] == ["manager", "finance"]

    # ── The run pays it, and consumes it only at approval ───────────────────
    run = await _run(session, org, *SEPTEMBER)
    computed = await run_service.compute_run(session, maker, run.id)
    assert computed.status is PayrollRunStatus.PENDING_REVIEW, computed.error_log

    staff_slip = await _slip(session, run.id, org["staff_emp"].id)
    lines = {line["code"]: line for line in staff_slip.input_lines}
    assert lines["EXPENSE_REIMB"]["amount"] == "1250.50"
    assert lines["EXPENSE_REIMB"]["taxable_amount"] == "0.00"
    assert lines["EXPENSE_REIMB"]["source"] == {"type": "expense_claim", "id": str(claim_id)}
    await session.refresh(claim)
    assert claim.status is ExpenseClaimStatusEnum.APPROVED, "nothing is consumed at compute"

    # Joiner and leaver: paid for the days they were employed.
    joiner_slip = await _slip(session, run.id, joiner.id)
    assert Decimal(str(joiner_slip.not_employed_days)) == Decimal(14)
    assert Decimal(str(joiner_slip.paid_days)) == Decimal(16)
    assert Decimal(str(joiner_slip.gross_earnings)) == Decimal("53333.33")
    leaver_slip = await _slip(session, run.id, leaver.id)
    assert Decimal(str(leaver_slip.not_employed_days)) == Decimal(20)
    assert Decimal(str(leaver_slip.paid_days)) == Decimal(10)
    assert await _slip(session, run.id, future.id) is None
    notices = [n["message"] for n in computed.input_notices if n.get("key") == "employment"]
    assert any("JOINER joined" in n for n in notices)
    assert any("LEAVER's last working day" in n for n in notices)
    assert any("FUTURE is not paid" in n for n in notices)

    approved = await run_service.approve_run(session, checker, run.id)
    assert approved.status is PayrollRunStatus.APPROVED
    claim = await session.scalar(select(ExpenseClaim).where(ExpenseClaim.id == claim_id))
    await session.refresh(claim)
    assert claim.status is ExpenseClaimStatusEnum.REIMBURSED
    assert claim.reimbursed_in_run_id == run.id and claim.reimbursed_at is not None
    read = await reads.get_claim(session, staff, claim_id)
    assert read.reimbursed_in_period == "September 2026"
    assert read.payment_note.startswith("Included in the approved September 2026 payroll")

    # The leaver's settlement is told the run already paid their final days.
    sources = await operations.settlement_sources(session, maker, leaver_exit_id)
    assert any("already paid 10 day(s)" in note for note in sources["notes"])

    # A later run finds nothing left to pay.
    scope = payroll_inputs.RunScope(
        run=approved, period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
        period="2026-10", employee_ids=frozenset({org["staff_emp"].id}),
        fy_start=date(2026, 4, 1), fy_end=date(2027, 3, 31),
    )
    later = await payroll_inputs.ExpenseClaimProvider().collect(session, maker, scope)
    assert not later.lines

    await run_service.mark_paid(session, checker, run.id)
    read = await reads.get_claim(session, staff, claim_id)
    assert read.payment_note == "Paid in September 2026 payroll."

    # The payslip as a PDF: the employee's only once published; HR's audited.
    with pytest.raises(HTTPException) as exc:
        await payslip_service.my_payslip_pdf(session, staff, staff_slip.id)
    assert exc.value.status_code == 404
    await run_service.publish_run(session, checker, run.id)
    data, filename = await payslip_service.my_payslip_pdf(session, staff, staff_slip.id)
    assert data[:5] == b"%PDF-" and filename == "payslip-STAFF-2026-09.pdf"
    with pytest.raises(HTTPException) as exc:
        await payslip_service.my_payslip_pdf(session, org["loner"], staff_slip.id)
    assert exc.value.status_code == 404
    data, _ = await payslip_service.hr_payslip_pdf(session, maker, run.id, joiner_slip.id)
    assert data[:5] == b"%PDF-"
    exported = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "payslip", AuditLog.entity_id == joiner_slip.id
        )
    )
    assert exported is not None


# ═══════════════════════════════════════════════════════════════════════════
# A trip that cost more than its advance
# ═══════════════════════════════════════════════════════════════════════════


async def _through_both_stages(session, org, claim_id) -> None:
    await expenses.submit_claim(session, org["staff"], claim_id=claim_id)
    await expenses.decide_claim_as_manager(session, org["boss"], claim_id=claim_id, approve=True)
    await expenses.decide_claim(session, org["maker"], claim_id=claim_id, approve=True)


async def test_the_balance_over_an_advance_is_paid_in_payroll_and_the_trip_only_once(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    org = await _org(session, tenant, legal_entity, location)
    maker, checker = org["maker"], org["checker"]

    trip = await expenses.create_travel_request(
        session, org["staff"], employee_id=org["staff_emp"].id, purpose="Site visit",
        from_date=date(2026, 9, 5), to_date=date(2026, 9, 6), advance_requested=Decimal(10000),
    )
    await expenses.decide_travel_request_as_manager(
        session, org["boss"], travel_request_id=trip.id, approve=True
    )
    await expenses.decide_travel_request(session, maker, travel_request_id=trip.id, approve=True)
    advance = await expenses.issue_advance(
        session, maker, travel_request_id=trip.id, amount=Decimal(10000),
        issued_on=date(2026, 9, 1),
    )
    first = await _claim(session, org, "staff", "6000", 5, trip=trip.id)
    second = await _claim(session, org, "staff", "5800", 6, trip=trip.id)
    for claim_id in (first, second):
        await _through_both_stages(session, org, claim_id)

    # While the advance is open, the claims wait: the advance already paid for the trip.
    run = await _run(session, org, *SEPTEMBER)
    computed = await run_service.compute_run(session, maker, run.id)
    slip = await _slip(session, run.id, org["staff_emp"].id)
    assert not [line for line in slip.input_lines if line["code"] == "EXPENSE_REIMB"]
    assert any(
        n.get("key") == "expense_claims" and "advance is not settled" in n["message"]
        for n in computed.input_notices
    )
    read = await reads.get_claim(session, org["staff"], first)
    assert "travel advance" in read.payment_note

    result = await expenses.settle_advance(
        session, maker, advance_id=advance.id, settled_on=date(2026, 9, 20)
    )
    assert result["outcome"] == "refund_due_to_employee"
    assert result["payroll_refund"]["amount"] == "1800.00"

    # Recomputed, the run pays the balance — not the claims, which the advance covered.
    await run_service.compute_run(session, maker, run.id)
    slip = await _slip(session, run.id, org["staff_emp"].id)
    codes = [line["code"] for line in slip.input_lines]
    assert codes.count("ADVANCE_REFUND") == 1 and "EXPENSE_REIMB" not in codes
    refund = next(line for line in slip.input_lines if line["code"] == "ADVANCE_REFUND")
    assert refund["amount"] == "1800.00"

    await run_service.approve_run(session, checker, run.id)
    settlement = await session.scalar(
        select(AdvanceSettlement).where(AdvanceSettlement.advance_id == advance.id)
    )
    await session.refresh(settlement)
    assert settlement.payroll_refund_ref == f"payroll_run:{run.id}"
    for claim_id in (first, second):
        claim = await session.scalar(select(ExpenseClaim).where(ExpenseClaim.id == claim_id))
        await session.refresh(claim)
        assert claim.status is ExpenseClaimStatusEnum.REIMBURSED
        assert claim.advance_settlement_id == settlement.id
        assert claim.reimbursed_in_run_id == run.id
    read = await reads.get_claim(session, org["staff"], first)
    assert "balance after your travel advance" in read.payment_note


# ═══════════════════════════════════════════════════════════════════════════
# Full and final: marked paid, with the bank's reference
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_approved_settlement_is_recorded_as_paid_once(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    hr_emp, hr = await person(session, tenant, legal_entity, location, code="HRA",
                              role=UserRole.ADMIN)
    leaver, leaver_user = await person(session, tenant, legal_entity, location, code="LEFT",
                                       role=UserRole.EMPLOYEE)
    record = await exit_service.initiate_exit(
        session, hr, employee_id=leaver.id, exit_type=ExitType.RESIGNATION,
        resignation_date=date(2026, 6, 1), approved_last_working_day=date(2026, 8, 31),
        notice_period_days=90,
    )
    await session.commit()
    settlement = await exit_service.compute_settlement(
        session, hr, exit_id=record.id,
        inputs=settlement_engine.SettlementInput(
            final_month_salary=Decimal(45000), final_month_basis="31 of 31 days."
        ),
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await operations.mark_settlement_paid(
            session, hr, settlement.id, paid_on=date(2026, 9, 1), payment_reference="UTR123456"
        )
    assert exc.value.status_code == 409, "approve before paying"

    await operations.approve_settlement(session, hr, settlement.id)
    await session.commit()
    assert "exits.settlement.approved" in await _events(session, leaver_user)

    today = await local_today(session, hr)
    for paid_on, reference, detail in (
        (today, "x", "bank's reference"),
        (date(2099, 1, 1), "UTR123456", "future"),
        (date(2026, 1, 1), "UTR123456", "approved on"),
    ):
        with pytest.raises(HTTPException) as exc:
            await operations.mark_settlement_paid(
                session, hr, settlement.id, paid_on=paid_on, payment_reference=reference
            )
        assert exc.value.status_code == 422 and detail in exc.value.detail

    paid = await operations.mark_settlement_paid(
        session, hr, settlement.id, paid_on=today, payment_reference="  UTR  2026093012345 "
    )
    await session.commit()
    assert paid.status is SettlementStatus.PAID
    assert paid.paid_on == today and paid.payment_reference == "UTR 2026093012345"
    assert paid.paid_by_user_id == hr.id and paid.paid_at is not None
    assert "exits.settlement.paid" in await _events(session, leaver_user)
    [row] = await operations.settlement_reads(session, hr, [paid])
    assert row["payment_reference"] == "UTR 2026093012345"
    assert row["paid_by_name"] == hr.full_name
    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "settlement",
            AuditLog.entity_id == settlement.id,
            AuditLog.summary.like("Recorded payment%"),
        )
    )
    assert audit is not None and audit.changes["payment_reference"] == "UTR 2026093012345"

    with pytest.raises(HTTPException) as exc:
        await operations.mark_settlement_paid(
            session, hr, settlement.id, paid_on=today, payment_reference="UTR999999"
        )
    assert exc.value.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════
# Form 16 Part B
# ═══════════════════════════════════════════════════════════════════════════


async def test_form_16_part_b_comes_from_a_completed_years_approved_payroll(
    session: AsyncSession, tenant, legal_entity, location
) -> None:
    org = await _org(session, tenant, legal_entity, location, rules_from=date(2025, 4, 1))
    legal_entity.tan = "BLRA12345B"
    await session.commit()

    run = await _run(session, org, 2026, 3)
    await run_service.compute_run(session, org["maker"], run.id)
    await run_service.approve_run(session, org["checker"], run.id)
    slip = await _slip(session, run.id, org["staff_emp"].id)

    statement = await form16.form16_statement(
        session, org["staff"], org["staff_emp"].id, "2025-26"
    )
    lines = {line["key"]: Decimal(line["amount"]) for line in statement["lines"]}
    assert statement["assessment_year"] == "2026-27"
    assert statement["employer"]["tan"] == "BLRA12345B"
    assert lines["1a"] == Decimal("100000.00"), "one approved month of taxable pay"
    assert Decimal(statement["tds"]["deducted_by_this_employer"]) == Decimal(str(slip.tds))
    assert form16.render_form16_pdf(statement)[:5] == b"%PDF-"

    years = await form16.list_form16_years(session, org["staff"], org["staff_emp"].id)
    assert [(y["financial_year"], y["available"]) for y in years] == [("2025-26", True)]

    # A year still running is not certified.
    with pytest.raises(form16.Form16UnavailableError, match="after the financial year ends"):
        await form16.form16_statement(session, org["staff"], org["staff_emp"].id, "2026-27")
    employee = await session.get(Employee, org["staff_emp"].id)
    assert employee is not None
