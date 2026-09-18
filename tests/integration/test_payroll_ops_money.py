"""W3 money gates against a real database: failed-credit alerts, stored
filings, arrear maker-checker and second approval, section 89(1) relief and
Form 10E, and the arrears the payroll run reads."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.arrears import ArrearBatch, ArrearLine, ArrearStatus
from app.models.audit import AuditAction, AuditLog
from app.models.disbursement import (
    BankFileFormat,
    BankProfile,
    DisbursementStatus,
    EmployeeBankAccount,
)
from app.models.filings import FilingRecord, FilingStatus, ReturnType
from app.models.notifications import Notification
from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip
from app.models.user import UserRole
from app.services.arrears import relief_service
from app.services.arrears import service as arrears_service
from app.services.disbursement import service as disbursement_service
from app.services.filings import service as filing_service
from app.services.filings import support
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_ops_helpers import (
    TDS_RULES,
    person,
    plain_user,
    storage_root,  # noqa: F401 - fixture
    tds_rule_set,
)

pytestmark = requires_db


# ═══════════════════════════════════════════════════════════════════════════
# Disbursement: a failed credit reaches HR Ops
# ═══════════════════════════════════════════════════════════════════════════


async def _released_batch(session, tenant, legal_entity, employee, hr_user):
    profile = BankProfile(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id, bank_name="HDFC Bank",
        account_number=encrypt_secret("00110123456789", tenant.id), ifsc="HDFC0000123",
        corporate_id="ACME", file_format=BankFileFormat.HDFC, format_config={},
    )
    session.add(profile)
    session.add(
        EmployeeBankAccount(
            tenant_id=tenant.id, employee_id=employee.id,
            account_number=encrypt_secret("50100123456789", tenant.id), ifsc="HDFC0001234",
            account_holder_name=employee.full_name, is_primary=True,
        )
    )
    run = PayrollRun(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id, period_year=2026,
        period_month=6, effective_date=date(2026, 6, 30), status=PayrollRunStatus.APPROVED,
    )
    session.add(run)
    await session.flush()
    session.add(
        Payslip(
            tenant_id=tenant.id, run_id=run.id, employee_id=employee.id,
            working_days=Decimal(30), paid_days=Decimal(30),
            gross_earnings=Decimal("50000.00"), net_pay=Decimal("48200.00"),
        )
    )
    await session.commit()
    batch = await disbursement_service.prepare_batch(
        session, hr_user, payroll_run_id=run.id, bank_profile_id=profile.id,
        value_date=date(2026, 6, 30),
    )
    await session.commit()
    checker = await plain_user(session, tenant, email="checker@acme.test", role=UserRole.HR_ADMIN)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await disbursement_service.release_batch(session, checker, batch_id=batch.id)
    await session.commit()
    return batch


async def test_a_failed_credit_notifies_hr_ops_in_the_same_transaction(
    session: AsyncSession, tenant, legal_entity, employee, hr_user
) -> None:
    ops = await plain_user(session, tenant, email="ops@acme.test", role=UserRole.HR_OPS)
    batch = await _released_batch(session, tenant, legal_entity, employee, hr_user)

    outcome = await disbursement_service.import_bank_response(
        session, hr_user, batch_id=batch.id,
        data=b"account_number,status,reason\n50100123456789,FAILED,Account closed\n9,x\n",
    )
    await session.commit()

    assert outcome["credited"] == 0
    assert outcome["failed"][0]["reason"] == "Account closed"
    assert outcome["notified"] >= 2, "HR Ops and the HR admin"
    assert outcome["unmatched"] == ["9"]
    sent = (
        await session.scalars(
            select(Notification.recipient_user_id).where(
                Notification.event_key == disbursement_service.EVENT_CREDIT_FAILED
            )
        )
    ).all()
    assert ops.id in sent


async def test_a_clean_reconciliation_notifies_nobody(
    session: AsyncSession, tenant, legal_entity, employee, hr_user
) -> None:
    await plain_user(session, tenant, email="ops@acme.test", role=UserRole.HR_OPS)
    batch = await _released_batch(session, tenant, legal_entity, employee, hr_user)
    outcome = await disbursement_service.reconcile(
        session, hr_user, batch_id=batch.id,
        responses=[{"account_number": "50100123456789", "status": "SUCCESS"}],
    )
    await session.commit()
    assert outcome["notified"] == 0
    count = await session.scalar(select(func.count()).select_from(Notification))
    assert count == 0


async def test_an_unreadable_response_file_changes_nothing(
    session: AsyncSession, tenant, legal_entity, employee, hr_user
) -> None:
    batch = await _released_batch(session, tenant, legal_entity, employee, hr_user)
    with pytest.raises(HTTPException) as exc:
        await disbursement_service.import_bank_response(
            session, hr_user, batch_id=batch.id, data=b"foo,bar\n1,2\n"
        )
    assert exc.value.status_code == 422
    fresh, _, _ = await disbursement_service.get_batch(session, hr_user, batch.id)
    assert fresh.status is DisbursementStatus.RELEASED


# ═══════════════════════════════════════════════════════════════════════════
# Filings: the stored file is the one downloaded, and the download is audited
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_filed_return_is_downloaded_exactly_and_audited(
    session: AsyncSession, tenant, legal_entity, hr_user, other_hr_user,
    storage_root,  # noqa: F811
) -> None:
    content = b"#~#ECR#~#UAN|NAME\n100000000001|ANITA SHARMA\n"
    record = FilingRecord(
        tenant_id=tenant.id, return_type=ReturnType.PF_ECR, legal_entity_id=legal_entity.id,
        period="2026-06", status=FilingStatus.GENERATED, file_name="ECR_202606.txt",
        file_storage_key=await support.store_generated(tenant.id, "filings", content, ".txt"),
    )
    blocked = FilingRecord(
        tenant_id=tenant.id, return_type=ReturnType.ESIC, legal_entity_id=legal_entity.id,
        period="2026-06", status=FilingStatus.BLOCKED,
    )
    session.add_all([record, blocked])
    await session.commit()

    data, name = await filing_service.download_record(session, hr_user, record.id)
    await session.commit()
    assert data == content and name == "ECR_202606.txt"
    exports = await session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == AuditAction.EXPORT, AuditLog.entity_id == record.id
        )
    )
    assert exports == 1

    with pytest.raises(HTTPException) as exc:
        await filing_service.download_record(session, hr_user, blocked.id)
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        await filing_service.download_record(session, other_hr_user, record.id)
    assert exc.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Arrears: maker-checker, own arrears, second approval
# ═══════════════════════════════════════════════════════════════════════════


async def _batch(session, tenant, legal_entity, *, computed_by, employee_id, **extra):
    batch = ArrearBatch(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id, from_period="2025-11",
        to_period="2026-05", status=ArrearStatus.PENDING_APPROVAL, employee_count=1,
        total_earnings_arrear=Decimal("60000"), total_net_payable=Decimal("57840"),
        computed_at=datetime.now(UTC), computed_by_user_id=computed_by.id,
        affected_filings=[], **extra,
    )
    session.add(batch)
    await session.flush()
    for period, amount in (("2025-11", "10000"), ("2026-01", "20000"), ("2026-05", "30000")):
        session.add(
            ArrearLine(
                tenant_id=tenant.id, batch_id=batch.id, employee_id=employee_id,
                period=period, component_code="BASIC", component_name="Basic",
                component_type="earning", paid_amount=Decimal(0),
                should_have_been_amount=Decimal(amount), difference=Decimal(amount),
            )
        )
    session.add(
        ArrearLine(
            tenant_id=tenant.id, batch_id=batch.id, employee_id=employee_id,
            period="2026-05", component_code="PF_EMPLOYEE", component_name="PF",
            component_type="deduction", paid_amount=Decimal(0),
            should_have_been_amount=Decimal("2160"), difference=Decimal("2160"),
        )
    )
    await session.commit()
    return batch


async def test_the_person_who_computed_arrears_cannot_approve_them(
    session: AsyncSession, tenant, legal_entity, employee, hr_user
) -> None:
    batch = await _batch(session, tenant, legal_entity, computed_by=hr_user,
                         employee_id=employee.id)
    with pytest.raises(HTTPException) as exc:
        await arrears_service.approve_batch(session, hr_user, batch_id=batch.id)
    assert exc.value.status_code == 403

    checker = await plain_user(session, tenant, email="c@acme.test", role=UserRole.HR_ADMIN)
    approved = await arrears_service.approve_batch(session, checker, batch_id=batch.id)
    assert approved.status is ArrearStatus.APPROVED


async def test_nobody_approves_arrears_that_pay_themselves(
    session: AsyncSession, tenant, legal_entity, location, hr_user
) -> None:
    approver_employee, approver = await person(
        session, tenant, legal_entity, location, code="ADM1", role=UserRole.HR_ADMIN
    )
    batch = await _batch(session, tenant, legal_entity, computed_by=hr_user,
                         employee_id=approver_employee.id)
    with pytest.raises(HTTPException) as exc:
        await arrears_service.approve_batch(session, approver, batch_id=batch.id)
    assert exc.value.status_code == 403


async def test_above_the_threshold_two_different_approvers_are_needed(
    session: AsyncSession, tenant, legal_entity, employee, hr_user
) -> None:
    batch = await _batch(session, tenant, legal_entity, computed_by=hr_user,
                         employee_id=employee.id, requires_additional_approval=True)
    first = await plain_user(session, tenant, email="a1@acme.test", role=UserRole.HR_ADMIN)
    second = await plain_user(session, tenant, email="a2@acme.test", role=UserRole.TENANT_ADMIN)

    after_first = await arrears_service.approve_batch(session, first, batch_id=batch.id)
    await session.commit()
    assert after_first.status is ArrearStatus.PENDING_APPROVAL
    assert after_first.approved_by_user_id == first.id

    with pytest.raises(HTTPException) as exc:
        await arrears_service.approve_batch(session, first, batch_id=batch.id)
    assert exc.value.status_code == 409

    final = await arrears_service.approve_batch(session, second, batch_id=batch.id)
    await session.commit()
    assert final.status is ArrearStatus.APPROVED
    assert final.additional_approved_by_user_id == second.id


async def test_the_payroll_run_reads_approved_arrears_per_component_and_marks_them_paid(
    session: AsyncSession, tenant, legal_entity, employee, hr_user
) -> None:
    batch = await _batch(session, tenant, legal_entity, computed_by=hr_user,
                         employee_id=employee.id)
    checker = await plain_user(session, tenant, email="c@acme.test", role=UserRole.HR_ADMIN)
    await arrears_service.approve_batch(session, checker, batch_id=batch.id)
    await session.commit()

    payable = await arrears_service.payable_arrears(session, hr_user)
    assert len(payable) == 1
    row = payable[0]
    assert {c["component_code"]: c["amount"] for c in row["components"]} == {
        "BASIC": Decimal("60000"), "PF_EMPLOYEE": Decimal("2160"),
    }
    assert row["net_payable"] == Decimal("57840")

    run = PayrollRun(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id, period_year=2026,
        period_month=9, effective_date=date(2026, 9, 30), status=PayrollRunStatus.DRAFT,
    )
    session.add(run)
    await session.commit()
    await arrears_service.mark_arrears_paid(
        session, hr_user, batch_id=batch.id, payroll_run_id=run.id
    )
    await session.commit()
    assert await arrears_service.payable_arrears(session, hr_user) == []


# ═══════════════════════════════════════════════════════════════════════════
# Section 89(1) relief and Form 10E
# ═══════════════════════════════════════════════════════════════════════════


async def test_relief_prices_each_year_by_its_own_rule_set_and_issues_form_10e(
    session: AsyncSession, tenant, legal_entity, location, hr_user,
    storage_root,  # noqa: F811
) -> None:
    earlier_rules = {
        **TDS_RULES,
        "regimes": {
            **TDS_RULES["regimes"],
            "new": {
                "slabs": [{"up_to": 300000, "rate": 0}, {"up_to": None, "rate": 5}],
                "standard_deduction": 0,
                "deductions_allowed": [],
            },
        },
    }
    await tds_rule_set(session, tenant, effective=date(2025, 4, 1), rules=earlier_rules)
    later = await tds_rule_set(session, tenant, effective=date(2026, 4, 1))
    # The earlier rule set ends where the later one starts.
    from app.models.payroll import PayrollRuleSet

    earlier = await session.scalar(
        select(PayrollRuleSet).where(PayrollRuleSet.effective_from == date(2025, 4, 1))
    )
    earlier.effective_to = date(2026, 3, 31)
    await session.commit()
    assert later.effective_from == date(2026, 4, 1)

    leaver, _ = await person(
        session, tenant, legal_entity, location, code="ARR1", role=UserRole.EMPLOYEE
    )
    batch = await _batch(session, tenant, legal_entity, computed_by=hr_user,
                         employee_id=leaver.id)
    batch.status = ArrearStatus.APPROVED
    batch.approved_at = datetime(2026, 9, 1, tzinfo=UTC)
    await session.commit()

    years = await relief_service.relief_years(session, hr_user, batch.id, leaver.id)
    assert years["receipt_financial_year"] == "2026-27"
    assert years["years"] == [{"financial_year": "2025-26", "arrear_portion": Decimal(30000)}]

    with pytest.raises(HTTPException) as exc:
        await relief_service.compute_employee_relief(
            session, hr_user, batch_id=batch.id, employee_id=leaver.id, regime="new",
            receipt_year_taxable_income=Decimal("1500000"), prior_years=[],
        )
    assert "2025-26" in exc.value.detail

    record = await relief_service.compute_employee_relief(
        session, hr_user, batch_id=batch.id, employee_id=leaver.id, regime="new",
        receipt_year_taxable_income=Decimal("1500000"),
        prior_years=[{"financial_year": "2025-26", "original_taxable_income": "400000"}],
    )
    await session.commit()
    # 2026-27 taxes the 30,000 at 20% plus 4% cess = 6,240; 2025-26 at 5% plus
    # 4% cess = 1,560. Relief is the difference.
    assert record.tax_without_relief == Decimal("6240.00")
    assert record.tax_with_spreading == Decimal("1560.00")
    assert record.relief_amount == Decimal("4680.00")
    assert record.is_beneficial is True

    letter = await relief_service.issue_form_10e(session, hr_user, record.id)
    await session.commit()
    assert "4,680.00" in letter.merged_html
    data, name = await relief_service.form_10e_pdf(session, hr_user, record.id)
    assert data.startswith(b"%PDF") and name.endswith(".pdf")
