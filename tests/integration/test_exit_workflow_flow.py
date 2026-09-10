"""Slice 13 gates that need a real database.

The clearance gating, the hold, and — the one that matters most — that exit
retains every historical record while revoking access.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceDay, AttendanceStatus
from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import (
    ClearanceItem,
    ClearanceStatus,
    ClearanceTemplate,
    ExitRecord,
    ExitStatus,
    ExitType,
    Settlement,
    SettlementLine,
    SettlementStatus,
)
from app.models.org import LegalEntity
from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.exit_workflow import service as exit_service
from app.services.exit_workflow import settlement as settlement_engine
from tests.integration.conftest import requires_db

pytestmark = requires_db

RESIGNED = date(2026, 6, 1)
LAST_DAY = date(2026, 8, 31)


async def _templates(session: AsyncSession, tenant: Tenant) -> None:
    session.add_all(
        [
            ClearanceTemplate(
                tenant_id=tenant.id, department="IT",
                description="Laptop and access cards returned",
                is_mandatory=True, display_order=1,
            ),
            ClearanceTemplate(
                tenant_id=tenant.id, department="Finance",
                description="Advances and reimbursements settled",
                is_mandatory=True, display_order=2,
            ),
            ClearanceTemplate(
                tenant_id=tenant.id, department="Library",
                description="Books returned", is_mandatory=False, display_order=3,
            ),
        ]
    )
    await session.commit()


async def _exit(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee, **overrides
) -> ExitRecord:
    base = {
        "employee_id": employee.id,
        "exit_type": ExitType.RESIGNATION,
        "resignation_date": RESIGNED,
        "approved_last_working_day": LAST_DAY,
        "notice_period_days": 90,
    }
    base.update(overrides)
    record = await exit_service.initiate_exit(session, hr_user, **base)
    await session.commit()
    await session.refresh(record)
    return record


async def _clear_all(
    session: AsyncSession, hr_user: User, record: ExitRecord, **per_item
) -> None:
    items = (
        await session.scalars(
            select(ClearanceItem).where(ClearanceItem.exit_record_id == record.id)
        )
    ).all()
    for item in items:
        outcome = per_item.get(item.department, ClearanceStatus.CLEARED)
        amount = per_item.get(f"{item.department}_amount", Decimal(0))
        await exit_service.clear_item(
            session, hr_user, item_id=item.id, outcome=outcome, recovery_amount=amount
        )
    await session.commit()


def _inputs(**overrides) -> settlement_engine.SettlementInput:
    base = {
        "final_month_salary": Decimal(45_000),
        "final_month_basis": "18 of 30 days worked in August.",
        "leave_encashment": Decimal(20_000),
        "leave_encashment_basis": "10 days of earned leave at 2,000 per day.",
        "gratuity": Decimal(150_000),
        "gratuity_basis": "6 completed years at 15 days per year.",
    }
    base.update(overrides)
    return settlement_engine.SettlementInput(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Clearance gating
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_checklist_is_instantiated_from_the_tenant_template(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A company with no library does not get a library clearance."""
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee)

    items = (
        await session.scalars(
            select(ClearanceItem).where(ClearanceItem.exit_record_id == record.id)
        )
    ).all()
    assert len(items) == 3
    assert {item.department for item in items} == {"IT", "Finance", "Library"}
    assert sum(1 for item in items if item.is_mandatory) == 2


async def test_settlement_is_blocked_while_mandatory_clearance_is_pending(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate, enforced in the service layer.

    The recoveries come from clearance items, so settling first means settling
    without knowing what is owed.
    """
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee)

    with pytest.raises(HTTPException) as exc:
        await exit_service.compute_settlement(
            session, hr_user, exit_id=record.id, inputs=_inputs()
        )
    assert exc.value.status_code == 409
    assert "mandatory clearance item(s) are still pending" in exc.value.detail
    assert "IT" in exc.value.detail


async def test_a_non_mandatory_item_does_not_block(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)

    items = (
        await session.scalars(
            select(ClearanceItem).where(
                ClearanceItem.exit_record_id == record.id,
                ClearanceItem.is_mandatory.is_(True),
            )
        )
    ).all()
    for item in items:
        await exit_service.clear_item(
            session, hr_user, item_id=item.id, outcome=ClearanceStatus.CLEARED
        )
    await session.commit()

    settlement = await exit_service.compute_settlement(
        session, hr_user, exit_id=record.id, inputs=_inputs()
    )
    await session.commit()
    assert settlement.net_amount > 0, "the library book did not block it"


async def test_a_waiver_without_a_reason_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Waiving silently is how an unreturned asset becomes nobody's problem."""
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee)
    item = await session.scalar(
        select(ClearanceItem).where(ClearanceItem.exit_record_id == record.id)
    )

    with pytest.raises(HTTPException) as exc:
        await exit_service.clear_item(
            session, hr_user, item_id=item.id, outcome=ClearanceStatus.WAIVED
        )
    assert "requires a reason" in exc.value.detail


async def test_a_recovery_clearance_needs_an_amount(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee)
    item = await session.scalar(
        select(ClearanceItem).where(ClearanceItem.exit_record_id == record.id)
    )

    with pytest.raises(HTTPException) as exc:
        await exit_service.clear_item(
            session, hr_user, item_id=item.id,
            outcome=ClearanceStatus.CLEARED_WITH_RECOVERY,
        )
    assert "needs an amount" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Recovery flowing from clearance
# ═══════════════════════════════════════════════════════════════════════════


async def test_asset_recovery_flows_from_clearance_into_the_settlement(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Read from the cleared items rather than typed in twice, precisely so the
    two cannot disagree.
    """
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)
    await _clear_all(
        session, hr_user, record,
        IT=ClearanceStatus.CLEARED_WITH_RECOVERY,
        IT_amount=Decimal(35_000),
    )

    settlement = await exit_service.compute_settlement(
        session, hr_user, exit_id=record.id, inputs=_inputs()
    )
    await session.commit()

    asset_line = await session.scalar(
        select(SettlementLine).where(
            SettlementLine.settlement_id == settlement.id,
            SettlementLine.code == "ASSETS",
        )
    )
    assert asset_line is not None
    assert Decimal(str(asset_line.amount)) == Decimal(35_000)
    assert "IT" in asset_line.basis
    assert "Laptop" in asset_line.basis


# ═══════════════════════════════════════════════════════════════════════════
# The negative settlement
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_negative_settlement_is_stored_as_a_demand(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate, tested explicitly.

    The product must be able to say "the employee owes the company".
    """
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)
    await _clear_all(
        session, hr_user, record,
        IT=ClearanceStatus.CLEARED_WITH_RECOVERY, IT_amount=Decimal(60_000),
    )

    settlement = await exit_service.compute_settlement(
        session, hr_user, exit_id=record.id,
        inputs=_inputs(
            final_month_salary=Decimal(10_000),
            leave_encashment=Decimal(0),
            gratuity=Decimal(0),
            loan_outstanding=Decimal(40_000),
            loan_basis="Outstanding personal loan.",
        ),
    )
    await session.commit()

    assert Decimal(str(settlement.net_amount)) == Decimal(-90_000)
    assert settlement.is_demand is True


async def test_notice_shortfall_must_be_supplied_or_waived(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The wage basis for notice recovery is a contractual term, so it is not
    assumed — which means a shortfall with no figure is a refusal, not a zero.
    """
    await _templates(session, tenant)
    # Leaving on 1 July against a 90-day notice from 1 June is 60 days short.
    # The default fixture serves the notice in full, which is the other test.
    record = await _exit(
        session, tenant, hr_user, employee, approved_last_working_day=date(2026, 7, 1)
    )
    await _clear_all(session, hr_user, record)

    assert record.notice_shortfall_days == 60

    with pytest.raises(HTTPException) as exc:
        await exit_service.compute_settlement(
            session, hr_user, exit_id=record.id, inputs=_inputs()
        )
    assert "wage basis for notice recovery" in exc.value.detail

    # Waived, and it computes.
    record.notice_waived = True
    record.notice_waiver_reason = "Released early at the company's request."
    await session.commit()

    settlement = await exit_service.compute_settlement(
        session, hr_user, exit_id=record.id, inputs=_inputs()
    )
    await session.commit()
    assert settlement.net_amount > 0


# ═══════════════════════════════════════════════════════════════════════════
# The statement
# ═══════════════════════════════════════════════════════════════════════════


async def test_every_settlement_line_states_its_basis(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate."""
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)
    await _clear_all(session, hr_user, record)

    settlement = await exit_service.compute_settlement(
        session, hr_user, exit_id=record.id,
        inputs=_inputs(
            final_tds=Decimal(-15_000),
            tax_basis="Full-year tax recomputed at exit on actual figures.",
        ),
    )
    await session.commit()

    lines = (
        await session.scalars(
            select(SettlementLine).where(SettlementLine.settlement_id == settlement.id)
        )
    ).all()
    assert lines
    for line in lines:
        assert line.basis and line.basis.strip(), f"{line.code} has no basis"

    tax_line = next(line for line in lines if line.line_type == "tax")
    assert "refund" in tax_line.label.lower()


async def test_recomputing_replaces_the_lines_rather_than_appending(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A statement with each line twice is worse than a wrong one."""
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)
    await _clear_all(session, hr_user, record)

    first = await exit_service.compute_settlement(
        session, hr_user, exit_id=record.id, inputs=_inputs()
    )
    await session.commit()
    count = await session.scalar(
        select(func.count()).select_from(SettlementLine).where(
            SettlementLine.settlement_id == first.id
        )
    )

    await exit_service.compute_settlement(
        session, hr_user, exit_id=record.id, inputs=_inputs()
    )
    await session.commit()
    again = await session.scalar(
        select(func.count()).select_from(SettlementLine).where(
            SettlementLine.settlement_id == first.id
        )
    )
    assert again == count

    settlements = (
        await session.scalars(
            select(Settlement).where(Settlement.exit_record_id == record.id)
        )
    ).all()
    assert len(settlements) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Holds
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_hold_is_computed_but_not_paid_with_the_reason_recorded(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    "Why has my settlement not been paid" is the question that follows.
    """
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)
    await _clear_all(session, hr_user, record)
    settlement = await exit_service.compute_settlement(
        session, hr_user, exit_id=record.id, inputs=_inputs()
    )
    await session.commit()

    held = await exit_service.hold_settlement(
        session, hr_user, settlement_id=settlement.id,
        reason="Pending resolution of a client escalation raised on 12 August.",
    )
    await session.commit()

    assert held.status is SettlementStatus.ON_HOLD
    assert held.hold_reason
    assert held.paid_at is None
    assert Decimal(str(held.net_amount)) > 0, "computed, just not paid"
    assert held.held_by_user_id == hr_user.id


async def test_a_hold_without_a_reason_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)
    await _clear_all(session, hr_user, record)
    settlement = await exit_service.compute_settlement(
        session, hr_user, exit_id=record.id, inputs=_inputs()
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await exit_service.hold_settlement(
            session, hr_user, settlement_id=settlement.id, reason="   "
        )
    assert "requires a reason" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Retention — the gate that matters most
# ═══════════════════════════════════════════════════════════════════════════


async def test_exit_deactivates_the_login_and_retains_every_record(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    """The checklist gate.

    Statutory retention runs to years after employment ends. A filed return has
    to remain explainable and a payslip the ex-employee still holds must still
    reconcile to something.
    """
    from app.core.security import hash_password

    login = User(
        tenant_id=tenant.id, email="leaver@acme.test",
        password_hash=hash_password("Str0ng!Passw0rd"), full_name="Anita Sharma",
        role=UserRole.EMPLOYEE, employee_id=employee.id, is_active=True,
    )
    session.add(login)

    run = PayrollRun(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id, period_year=2026,
        period_month=6, effective_date=date(2026, 6, 30), status=PayrollRunStatus.PAID,
    )
    session.add(run)
    await session.flush()
    session.add(
        Payslip(
            tenant_id=tenant.id, run_id=run.id, employee_id=employee.id,
            working_days=Decimal(30), paid_days=Decimal(30),
            gross_earnings=Decimal("75000.00"), net_pay=Decimal("68000.00"),
        )
    )
    session.add(
        AttendanceDay(
            tenant_id=tenant.id, employee_id=employee.id,
            attendance_date=date(2026, 6, 15), status=AttendanceStatus.PRESENT,
            payable_fraction=Decimal(1), computed_at=datetime.now(UTC),
        )
    )
    await session.commit()

    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)

    await exit_service.revoke_access(session, hr_user, exit_id=record.id)
    await session.commit()

    await session.refresh(login)
    await session.refresh(employee)

    assert login.is_active is False, "the login is deactivated"
    assert employee.status is EmploymentStatus.EXITED
    assert record.access_revoked_at is not None

    # And nothing was deleted.
    assert await session.get(Employee, employee.id) is not None
    payslips = await session.scalar(
        select(func.count()).select_from(Payslip).where(Payslip.employee_id == employee.id)
    )
    attendance = await session.scalar(
        select(func.count())
        .select_from(AttendanceDay)
        .where(AttendanceDay.employee_id == employee.id)
    )
    audits = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == tenant.id)
    )
    assert payslips == 1
    assert attendance == 1
    assert audits > 0


async def test_the_revocation_audit_says_nothing_was_deleted(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The record of what was *not* done is part of the retention evidence."""
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)
    await exit_service.revoke_access(session, hr_user, exit_id=record.id)
    await session.commit()

    entry = await session.scalar(
        select(AuditLog)
        .where(AuditLog.entity_id == record.id)
        .order_by(AuditLog.created_at.desc())
    )
    assert entry is not None
    assert "retained" in (entry.summary or "")
    assert entry.changes.get("records_deleted") == 0


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_second_open_exit_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _templates(session, tenant)
    await _exit(session, tenant, hr_user, employee)

    with pytest.raises(HTTPException) as exc:
        await exit_service.initiate_exit(
            session, hr_user, employee_id=employee.id, exit_type=ExitType.RESIGNATION,
            resignation_date=date(2026, 7, 1),
        )
    assert exc.value.status_code == 409
    assert "already has an open exit" in exc.value.detail


async def test_the_notice_shortfall_is_computed_at_initiation(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """So the employee sees it during their notice period rather than
    discovering it on their final statement."""
    await _templates(session, tenant)
    record = await _exit(
        session, tenant, hr_user, employee,
        approved_last_working_day=date(2026, 7, 1),
        notice_period_days=90,
    )
    assert record.notice_shortfall_days == 60
    assert record.status is ExitStatus.INITIATED


async def test_full_notice_served_leaves_no_shortfall(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _templates(session, tenant)
    record = await _exit(
        session, tenant, hr_user, employee,
        approved_last_working_day=date(2026, 9, 1), notice_period_days=90,
    )
    assert record.notice_shortfall_days == 0
    assert record.status is ExitStatus.NOTICE_PERIOD


async def test_exits_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User,
    employee: Employee,
) -> None:
    await _templates(session, tenant)
    record = await _exit(session, tenant, hr_user, employee, notice_period_days=0)

    with pytest.raises(HTTPException) as exc:
        await exit_service.revoke_access(session, other_hr_user, exit_id=record.id)
    assert exc.value.status_code == 404
