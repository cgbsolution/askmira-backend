"""Slice 11 gates that need a real database.

The maker-checker control is the reason this file exists. The validators and
serialisers are proved in `tests/disbursement/`; what only a database settles is
that the preparer genuinely cannot approve, that regenerating after approval
voids it, that downloads are recorded, and that a handful of bounced credits can
be reissued without paying everybody twice.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.audit import AuditAction, AuditLog
from app.models.disbursement import (
    BankFileFormat,
    BankProfile,
    DisbursementDownload,
    DisbursementLine,
    DisbursementStatus,
    EmployeeBankAccount,
    LineStatus,
)
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.disbursement import service as disbursement_service
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _profile(
    session: AsyncSession,
    tenant: Tenant,
    legal_entity: LegalEntity,
    *,
    file_format: BankFileFormat = BankFileFormat.HDFC,
    config: dict | None = None,
) -> BankProfile:
    profile = BankProfile(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        bank_name="HDFC Bank",
        account_number=encrypt_secret("00110123456789", tenant.id),
        ifsc="HDFC0000123",
        corporate_id="ACMECORP01",
        file_format=file_format,
        format_config=config or {},
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


async def _account(
    session: AsyncSession,
    tenant: Tenant,
    employee: Employee,
    *,
    number: str = "50100123456789",
    ifsc: str = "HDFC0001234",
    holder: str | None = None,
) -> EmployeeBankAccount:
    account = EmployeeBankAccount(
        tenant_id=tenant.id,
        employee_id=employee.id,
        account_number=encrypt_secret(number, tenant.id),
        ifsc=ifsc,
        account_holder_name=holder or employee.full_name,
        is_primary=True,
    )
    session.add(account)
    await session.commit()
    return account


async def _run_with_pay(
    session: AsyncSession,
    tenant: Tenant,
    legal_entity: LegalEntity,
    employee: Employee,
    *,
    net_pay: str = "48200.00",
) -> PayrollRun:
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=6,
        effective_date=date(2026, 6, 30),
        status=PayrollRunStatus.APPROVED,
    )
    session.add(run)
    await session.flush()
    session.add(
        Payslip(
            tenant_id=tenant.id,
            run_id=run.id,
            employee_id=employee.id,
            working_days=Decimal(30),
            paid_days=Decimal(30),
            gross_earnings=Decimal("50000.00"),
            net_pay=Decimal(net_pay),
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


async def _second_user(session: AsyncSession, tenant: Tenant) -> User:
    """A different person, for the second signature."""
    from app.core.security import hash_password

    user = User(
        tenant_id=tenant.id,
        email="checker@acme.test",
        password_hash=hash_password("Str0ng!Passw0rd"),
        full_name="Priya Checker",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _ready(session, tenant, legal_entity, employee, hr_user, **kwargs):
    profile = await _profile(session, tenant, legal_entity, **kwargs)
    await _account(session, tenant, employee)
    run = await _run_with_pay(session, tenant, legal_entity, employee)
    batch = await disbursement_service.prepare_batch(
        session, hr_user, payroll_run_id=run.id, bank_profile_id=profile.id,
        value_date=date(2026, 6, 30),
    )
    await session.commit()
    return profile, run, batch


# ═══════════════════════════════════════════════════════════════════════════
# Maker-checker
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_preparer_cannot_approve_their_own_batch(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate, enforced in the service layer.

    Not a role check — this user has every role. It is a check against who
    prepared this specific batch.
    """
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)

    with pytest.raises(HTTPException) as exc:
        await disbursement_service.approve_batch(
            session, hr_user, batch_id=batch.id, file_hash=batch.file_hash
        )
    assert exc.value.status_code == 403
    assert "you cannot approve it" in exc.value.detail


async def test_a_different_user_can_approve(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    checker = await _second_user(session, tenant)

    approved = await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await session.commit()

    assert approved.status is DisbursementStatus.APPROVED
    assert approved.approved_by_user_id == checker.id
    assert approved.approved_by_user_id != approved.prepared_by_user_id
    assert approved.approved_file_hash == batch.file_hash


async def test_approving_a_hash_the_approver_did_not_see_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """Approving a hash you have not seen is the same as not approving."""
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    checker = await _second_user(session, tenant)

    with pytest.raises(HTTPException) as exc:
        await disbursement_service.approve_batch(
            session, checker, batch_id=batch.id, file_hash="0" * 64
        )
    assert exc.value.status_code == 409
    assert "changed since it was shown to you" in exc.value.detail


async def test_regenerating_after_approval_voids_the_approval(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate.

    Without this the control is theatre: approve a clean file, regenerate a
    different one, release it. The superseded batch keeps its own record of
    what was approved, and the rebuilt one starts again from scratch.
    """
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    checker = await _second_user(session, tenant)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await session.commit()
    assert batch.status is DisbursementStatus.APPROVED

    rebuilt = await disbursement_service.regenerate_batch(
        session, hr_user, batch_id=batch.id
    )
    await session.commit()

    assert rebuilt.id != batch.id
    assert rebuilt.status is DisbursementStatus.PENDING_APPROVAL
    assert rebuilt.approved_by_user_id is None
    assert batch.status is DisbursementStatus.FAILED
    assert str(rebuilt.id) in (batch.notes or "")

    entries = (
        await session.scalars(
            select(AuditLog).where(AuditLog.entity_id == batch.id)
        )
    ).all()
    assert any("Approval voided" in (e.summary or "") for e in entries)


async def test_a_rebuilt_batch_cannot_be_released_without_a_fresh_approval(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    checker = await _second_user(session, tenant)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    rebuilt = await disbursement_service.regenerate_batch(
        session, hr_user, batch_id=batch.id
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await disbursement_service.release_batch(session, checker, batch_id=rebuilt.id)
    assert "cannot be released" in exc.value.detail


async def test_an_unapproved_batch_cannot_be_released(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    with pytest.raises(HTTPException) as exc:
        await disbursement_service.release_batch(session, hr_user, batch_id=batch.id)
    assert exc.value.status_code == 409
    assert "Money only moves against an approved batch" in exc.value.detail


async def test_an_approved_batch_releases(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    checker = await _second_user(session, tenant)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    released = await disbursement_service.release_batch(
        session, checker, batch_id=batch.id
    )
    await session.commit()

    assert released.status is DisbursementStatus.RELEASED
    assert released.released_at is not None


# ═══════════════════════════════════════════════════════════════════════════
# Preparation and validation
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_batch_cannot_be_prepared_from_an_unapproved_run(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    profile = await _profile(session, tenant, legal_entity)
    await _account(session, tenant, employee)
    run = await _run_with_pay(session, tenant, legal_entity, employee)
    run.status = PayrollRunStatus.PENDING_REVIEW
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await disbursement_service.prepare_batch(
            session, hr_user, payroll_run_id=run.id, bank_profile_id=profile.id,
            value_date=date(2026, 6, 30),
        )
    assert "only ever disbursed against an approved run" in exc.value.detail


async def test_a_missing_bank_account_blocks_the_batch(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    profile = await _profile(session, tenant, legal_entity)
    run = await _run_with_pay(session, tenant, legal_entity, employee)

    with pytest.raises(disbursement_service.BatchBlockedError) as exc:
        await disbursement_service.prepare_batch(
            session, hr_user, payroll_run_id=run.id, bank_profile_id=profile.id,
            value_date=date(2026, 6, 30),
        )
    await session.commit()

    assert exc.value.batch.status is DisbursementStatus.FAILED
    assert exc.value.batch.encrypted_file is None
    codes = {e["code"] for e in exc.value.result.as_list()}
    assert "ACCOUNT_MISSING" in codes


async def test_a_duplicate_account_across_two_employees_blocks(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """Occasionally legitimate; far more often it pays one person twice."""
    profile = await _profile(session, tenant, legal_entity)
    second = Employee(
        tenant_id=tenant.id, employee_code="ACM002", first_name="Rahul", last_name="Iyer",
        work_email="rahul@acme.test", legal_entity_id=legal_entity.id,
    )
    session.add(second)
    await session.flush()

    await _account(session, tenant, employee)
    await _account(session, tenant, second, holder="Rahul Iyer")

    run = await _run_with_pay(session, tenant, legal_entity, employee)
    session.add(
        Payslip(
            tenant_id=tenant.id, run_id=run.id, employee_id=second.id,
            working_days=Decimal(30), paid_days=Decimal(30),
            gross_earnings=Decimal("30000.00"), net_pay=Decimal("29000.00"),
        )
    )
    await session.commit()

    with pytest.raises(disbursement_service.BatchBlockedError) as exc:
        await disbursement_service.prepare_batch(
            session, hr_user, payroll_run_id=run.id, bank_profile_id=profile.id,
            value_date=date(2026, 6, 30),
        )
    await session.commit()
    assert "ACCOUNT_DUPLICATE" in {e["code"] for e in exc.value.result.as_list()}


async def test_the_prepared_file_is_encrypted_at_rest(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """A database dump must not be a list of every employee's bank account."""
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)

    assert batch.encrypted_file is not None
    assert "50100123456789" not in batch.encrypted_file
    assert batch.encrypted_file.startswith("enc:")


async def test_the_line_snapshots_the_account_it_actually_sent(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """An account change next month must not alter what this batch says was
    paid, and a bounced credit is investigated against what was sent."""
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    lines = (
        await session.scalars(
            select(DisbursementLine).where(DisbursementLine.batch_id == batch.id)
        )
    ).all()

    assert len(lines) == 1
    assert lines[0].ifsc == "HDFC0001234"
    assert "50100123456789" not in lines[0].account_number, "encrypted on the line too"


# ═══════════════════════════════════════════════════════════════════════════
# Downloads
# ═══════════════════════════════════════════════════════════════════════════


async def test_every_download_writes_a_record_and_an_audit_entry(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate.

    If a copy of this file turns up where it should not, the question is who
    took it and when.
    """
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    checker = await _second_user(session, tenant)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await session.commit()

    content, _ = await disbursement_service.download_file(
        session, checker, batch_id=batch.id
    )
    await session.commit()

    assert "50100123456789" in content, "the decrypted file"

    downloads = (
        await session.scalars(
            select(DisbursementDownload).where(DisbursementDownload.batch_id == batch.id)
        )
    ).all()
    assert len(downloads) == 1
    assert downloads[0].downloaded_by_user_id == checker.id
    assert downloads[0].file_hash == batch.file_hash

    entries = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == batch.id, AuditLog.action == AuditAction.EXPORT
            )
        )
    ).all()
    assert len(entries) == 1


async def test_an_unapproved_file_cannot_be_downloaded(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """It is released to the approver after sign-off, not before."""
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    with pytest.raises(HTTPException) as exc:
        await disbursement_service.download_file(session, hr_user, batch_id=batch.id)
    assert exc.value.status_code == 403


async def test_the_download_window_expires(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """A file this sensitive should not stay downloadable indefinitely."""
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    checker = await _second_user(session, tenant)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    batch.prepared_at = datetime.now(UTC) - timedelta(hours=48)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await disbursement_service.download_file(session, checker, batch_id=batch.id)
    assert exc.value.status_code == 410
    assert "no longer downloadable" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Reconciliation
# ═══════════════════════════════════════════════════════════════════════════


async def _released(session, tenant, legal_entity, employee, hr_user):
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    checker = await _second_user(session, tenant)
    await disbursement_service.approve_batch(
        session, checker, batch_id=batch.id, file_hash=batch.file_hash
    )
    await disbursement_service.release_batch(session, checker, batch_id=batch.id)
    await session.commit()
    return batch, checker


async def test_reconciliation_marks_credited_and_failed_lines(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    batch, _ = await _released(session, tenant, legal_entity, employee, hr_user)

    outcome = await disbursement_service.reconcile(
        session, hr_user, batch_id=batch.id,
        responses=[
            {
                "account_number": "50100123456789",
                "status": "failed",
                "reason": "Account closed",
                "reference": "REF001",
            }
        ],
    )
    await session.commit()

    assert outcome["credited"] == 0
    assert len(outcome["failed"]) == 1
    assert outcome["failed"][0]["reason"] == "Account closed"

    line = await session.scalar(
        select(DisbursementLine).where(DisbursementLine.batch_id == batch.id)
    )
    assert line.status is LineStatus.FAILED
    assert line.bank_reference == "REF001"
    assert batch.status is DisbursementStatus.ACKNOWLEDGED


async def test_a_successful_response_marks_the_line_credited(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    batch, _ = await _released(session, tenant, legal_entity, employee, hr_user)
    outcome = await disbursement_service.reconcile(
        session, hr_user, batch_id=batch.id,
        responses=[{"account_number": "50100123456789", "status": "SUCCESS"}],
    )
    await session.commit()
    assert outcome["credited"] == 1
    assert outcome["failed"] == []


async def test_a_response_for_an_unknown_account_is_reported_not_swallowed(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    batch, _ = await _released(session, tenant, legal_entity, employee, hr_user)
    outcome = await disbursement_service.reconcile(
        session, hr_user, batch_id=batch.id,
        responses=[{"account_number": "99999999999999", "status": "SUCCESS"}],
    )
    await session.commit()
    assert outcome["unmatched"] == ["9999"]


async def test_an_unreleased_batch_cannot_be_reconciled(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)
    with pytest.raises(HTTPException) as exc:
        await disbursement_service.reconcile(
            session, hr_user, batch_id=batch.id,
            responses=[{"account_number": "50100123456789", "status": "SUCCESS"}],
        )
    assert "not been sent to a bank" in exc.value.detail


async def test_failed_credits_reissue_as_a_supplementary_batch(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate.

    A handful of bounced credits must not mean regenerating and re-approving
    the whole run — that is how a correction becomes a second payment to
    everybody.
    """
    batch, _ = await _released(session, tenant, legal_entity, employee, hr_user)
    await disbursement_service.reconcile(
        session, hr_user, batch_id=batch.id,
        responses=[
            {"account_number": "50100123456789", "status": "failed", "reason": "Closed"}
        ],
    )
    await session.commit()

    supplementary = await disbursement_service.supplementary_batch(
        session, hr_user, batch_id=batch.id, value_date=date(2026, 7, 2)
    )
    await session.commit()

    assert supplementary.supplements_batch_id == batch.id
    assert supplementary.employee_count == 1
    assert supplementary.status is DisbursementStatus.PENDING_APPROVAL
    assert supplementary.file_hash is not None

    # The original line no longer reports as failed, so the worklist does not
    # keep showing a credit that has been dealt with.
    original_line = await session.scalar(
        select(DisbursementLine).where(DisbursementLine.batch_id == batch.id)
    )
    assert original_line.status is LineStatus.REISSUED


async def test_a_supplementary_batch_still_needs_a_second_signature(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """It is a payment file like any other."""
    batch, _ = await _released(session, tenant, legal_entity, employee, hr_user)
    await disbursement_service.reconcile(
        session, hr_user, batch_id=batch.id,
        responses=[{"account_number": "50100123456789", "status": "failed"}],
    )
    supplementary = await disbursement_service.supplementary_batch(
        session, hr_user, batch_id=batch.id, value_date=date(2026, 7, 2)
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await disbursement_service.approve_batch(
            session, hr_user, batch_id=supplementary.id,
            file_hash=supplementary.file_hash,
        )
    assert exc.value.status_code == 403


async def test_a_batch_with_no_failures_has_nothing_to_reissue(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    batch, _ = await _released(session, tenant, legal_entity, employee, hr_user)
    await disbursement_service.reconcile(
        session, hr_user, batch_id=batch.id,
        responses=[{"account_number": "50100123456789", "status": "SUCCESS"}],
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await disbursement_service.supplementary_batch(
            session, hr_user, batch_id=batch.id, value_date=date(2026, 7, 2)
        )
    assert "no failed credits to reissue" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


async def test_another_tenant_cannot_see_or_approve_a_batch(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User,
    legal_entity: LegalEntity, employee: Employee,
) -> None:
    _, _, batch = await _ready(session, tenant, legal_entity, employee, hr_user)

    with pytest.raises(HTTPException) as exc:
        await disbursement_service.approve_batch(
            session, other_hr_user, batch_id=batch.id, file_hash=batch.file_hash
        )
    assert exc.value.status_code == 404
