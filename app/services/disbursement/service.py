"""The disbursement batch lifecycle, and the maker-checker control.

═══════════════════════════════════════════════════════════════════════════════
THE CONTROL, AND WHY IT IS HERE RATHER THAN IN THE UI
═══════════════════════════════════════════════════════════════════════════════

Segregation of duties is enforced in this module because a control implemented
only in a screen is not a control. The API is reachable without the screen, the
screen changes, and a mobile client or an integration will not reimplement it.

Two rules, both refusals:

1. **The preparer cannot approve.** Checked against `prepared_by_user_id`, not
   against a role — a user with both roles still cannot approve their own batch.
2. **An approval names the file it approved.** The file hash is stored with the
   approval. Regenerate afterwards and the hashes differ, so release refuses
   and the batch drops back to PENDING_APPROVAL. Without this the control is
   theatre: approve a clean file, regenerate a different one, release it.

Release is the irreversible step — after it, money moves — so it is the one
that re-checks everything.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.disbursement import (
    BankProfile,
    DisbursementBatch,
    DisbursementDownload,
    DisbursementLine,
    DisbursementStatus,
    EmployeeBankAccount,
    LineStatus,
)
from app.models.employee import Employee
from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip
from app.models.user import User, UserRole
from app.services import audit
from app.services.attendance.service import local_today
from app.services.disbursement import formats, responses, validation
from app.services.filings import support
from app.services.notifications.dispatch import notify

__all__ = [
    "BatchBlockedError",
    "approve_batch",
    "download_file",
    "prepare_batch",
    "release_batch",
]

# How long a prepared file stays downloadable before it has to be regenerated.
# A file listing the whole workforce's bank details should not sit behind a
# link that works next month.
DOWNLOAD_WINDOW = 24  # hours


class BatchBlockedError(Exception):
    """Validation stopped a batch being prepared. No file exists."""

    def __init__(self, batch: DisbursementBatch, result: validation.BatchValidation) -> None:
        self.batch = batch
        self.result = result
        super().__init__(
            f"{len(result.exceptions)} exception(s) prevent this disbursement being "
            "prepared."
        )


async def _batch(session: AsyncSession, user: User, batch_id: uuid.UUID) -> DisbursementBatch:
    batch = await session.scalar(
        tenant_scoped(
            select(DisbursementBatch).where(DisbursementBatch.id == batch_id),
            DisbursementBatch,
            user,
        )
    )
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Disbursement batch not found"
        )
    return batch


# ═══════════════════════════════════════════════════════════════════════════
# Prepare
# ═══════════════════════════════════════════════════════════════════════════


async def prepare_batch(
    session: AsyncSession,
    user: User,
    *,
    payroll_run_id: uuid.UUID,
    bank_profile_id: uuid.UUID,
    value_date: date,
    name_match_threshold: Decimal | None = None,
    request: Request | None = None,
) -> DisbursementBatch:
    """Build the file for an approved run.

    Every exception blocks. A rejected statutory return is an annoyance; a
    salary credit to a wrong account is money gone and an employee unpaid, so
    there is no warning tier here.
    """
    run = await session.scalar(
        tenant_scoped(
            select(PayrollRun).where(PayrollRun.id == payroll_run_id), PayrollRun, user
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payroll run not found")
    if run.status not in (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This run is '{run.status.value}'. Money is only ever disbursed against "
                "an approved run."
            ),
        )

    profile = await session.scalar(
        tenant_scoped(
            select(BankProfile).where(BankProfile.id == bank_profile_id), BankProfile, user
        )
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bank profile not found")

    rows = (
        await session.execute(
            tenant_scoped(
                select(Payslip, Employee)
                .join(Employee, Payslip.employee_id == Employee.id)
                .where(Payslip.run_id == run.id)
                .order_by(Employee.employee_code),
                Payslip,
                user,
            )
        )
    ).all()

    threshold = name_match_threshold or Decimal(
        str(profile.format_config.get("name_match_threshold", "0.5"))
    )

    to_validate = []
    resolved: list[tuple[Payslip, Employee, EmployeeBankAccount | None, str]] = []

    for payslip, employee in rows:
        account = await session.scalar(
            tenant_scoped(
                select(EmployeeBankAccount)
                .where(
                    EmployeeBankAccount.employee_id == employee.id,
                    EmployeeBankAccount.is_primary.is_(True),
                    EmployeeBankAccount.active.is_(True),
                )
                .order_by(EmployeeBankAccount.created_at.desc()),
                EmployeeBankAccount,
                user,
            )
        )
        number = (
            decrypt_secret(account.account_number, user.tenant_id) or ""
            if account
            else ""
        )
        resolved.append((payslip, employee, account, number))
        to_validate.append(
            {
                "subject": employee.employee_code,
                "employee_name": employee.full_name,
                "account_holder_name": account.account_holder_name if account else "",
                "account_number": number,
                "ifsc": account.ifsc if account else "",
                "amount": Decimal(str(payslip.net_pay)),
            }
        )

    result = validation.validate_lines(to_validate, name_match_threshold=threshold)

    batch = DisbursementBatch(
        tenant_id=user.tenant_id,
        payroll_run_id=run.id,
        bank_profile_id=profile.id,
        value_date=value_date,
        prepared_by_user_id=user.id,
        prepared_at=datetime.now(UTC),
        validation_exceptions=result.as_list(),
    )

    if result.is_blocked:
        batch.status = DisbursementStatus.FAILED
        session.add(batch)
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.CREATE,
            entity_type="disbursement_batch",
            entity_id=batch.id,
            summary=f"Disbursement blocked: {result.summary()}",
            changes={"exceptions": len(result.exceptions)},
            request=request,
        )
        raise BatchBlockedError(batch, result)

    # ── The file ────────────────────────────────────────────────────────────
    spec = formats.FORMATS[profile.file_format]
    limit = int(profile.format_config.get("name_length", spec.name_length))

    payment_lines = [
        formats.PaymentLine(
            beneficiary_name=account.account_holder_name,
            account_number=number,
            ifsc=account.ifsc.upper(),
            amount=Decimal(str(payslip.net_pay)),
            employee_code=employee.employee_code,
        )
        for payslip, employee, account, number in resolved
        if account is not None
    ]

    instruction = formats.PaymentInstruction(
        debit_account=decrypt_secret(profile.account_number, user.tenant_id) or "",
        debit_ifsc=profile.ifsc,
        corporate_id=profile.corporate_id or "",
        value_date=value_date,
        batch_reference=f"SAL{run.period_year}{run.period_month:02d}",
        lines=payment_lines,
    )

    try:
        content = formats.serialise(
            instruction, profile.file_format, config=profile.format_config
        )
    except formats.FormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    batch.status = DisbursementStatus.PENDING_APPROVAL
    batch.employee_count = len(payment_lines)
    batch.total_amount = instruction.total_amount
    batch.file_name = (
        f"{profile.file_format.value.upper()}_{run.period_year}"
        f"{run.period_month:02d}_{run.id.hex[:8]}.txt"
    )
    batch.file_hash = formats.file_hash(content)
    # Encrypted at rest: this file is every employee's bank account and salary.
    batch.encrypted_file = encrypt_secret(content, user.tenant_id)
    session.add(batch)
    await session.flush()

    for payslip, employee, account, number in resolved:
        if account is None:
            continue
        session.add(
            DisbursementLine(
                tenant_id=user.tenant_id,
                batch_id=batch.id,
                employee_id=employee.id,
                payslip_id=payslip.id,
                amount=Decimal(str(payslip.net_pay)),
                # Snapshotted: an account changed next month must not alter
                # what this batch says was paid.
                account_number=encrypt_secret(number, user.tenant_id),
                ifsc=account.ifsc.upper(),
                beneficiary_name=formats.truncate_name(account.account_holder_name, limit),
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="disbursement_batch",
        entity_id=batch.id,
        summary=(
            f"Prepared disbursement of {batch.total_amount} to {batch.employee_count} "
            f"employees for {run.period_month:02d}/{run.period_year}"
        ),
        changes={
            "total_amount": str(batch.total_amount),
            "employee_count": batch.employee_count,
            "file_hash": batch.file_hash,
        },
        request=request,
    )
    return batch


# ═══════════════════════════════════════════════════════════════════════════
# Approve
# ═══════════════════════════════════════════════════════════════════════════


async def approve_batch(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    file_hash: str,
    request: Request | None = None,
) -> DisbursementBatch:
    """Sign off a prepared batch.

    `file_hash` is the hash of the file the approver actually looked at. It is
    required rather than read from the batch on purpose: an approver confirming
    a hash they were shown is confirming a specific file, and if the batch was
    regenerated between their review and their click the hashes differ and this
    refuses.
    """
    batch = await _batch(session, user, batch_id)

    if batch.status is not DisbursementStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A batch in status '{batch.status.value}' is not awaiting approval.",
        )

    # ── Maker-checker ───────────────────────────────────────────────────────
    if batch.prepared_by_user_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You prepared this batch, so you cannot approve it. Segregation of "
                "duties on a salary payment is the whole point of the second signature "
                "— a different person has to look at it."
            ),
        )

    if file_hash != batch.file_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The file has changed since it was shown to you. Review the current "
                "file before approving — approving a hash you have not seen is the "
                "same as not approving at all."
            ),
        )

    batch.status = DisbursementStatus.APPROVED
    batch.approved_by_user_id = user.id
    batch.approved_at = datetime.now(UTC)
    batch.approved_file_hash = file_hash
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="disbursement_batch",
        entity_id=batch.id,
        summary=(
            f"Approved disbursement of {batch.total_amount} to {batch.employee_count} "
            f"employees (prepared by another user)"
        ),
        changes={"approved_file_hash": file_hash},
        request=request,
    )
    return batch


async def regenerate_batch(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    request: Request | None = None,
) -> DisbursementBatch:
    """Rebuild the file for a batch, voiding any approval.

    Voiding is not a side effect to be tidied away later — it is the reason
    this function exists as its own operation rather than a silent rewrite.
    """
    batch = await _batch(session, user, batch_id)
    if batch.status is DisbursementStatus.RELEASED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This batch has been released to the bank. It cannot be rebuilt.",
        )

    had_approval = batch.approved_at is not None
    rebuilt = await prepare_batch(
        session,
        user,
        payroll_run_id=batch.payroll_run_id,
        bank_profile_id=batch.bank_profile_id,
        value_date=batch.value_date or await local_today(session, user),
        request=request,
    )

    # The old batch is superseded rather than edited, so the trail keeps what
    # was approved before.
    batch.status = DisbursementStatus.FAILED
    batch.notes = f"Superseded by batch {rebuilt.id}."
    await session.flush()

    if had_approval:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="disbursement_batch",
            entity_id=batch.id,
            summary=(
                "Approval voided: the file was regenerated after being approved, so the "
                "earlier signature no longer applies to any file."
            ),
            changes={"superseded_by": str(rebuilt.id)},
            request=request,
        )
    return rebuilt


# ═══════════════════════════════════════════════════════════════════════════
# Release
# ═══════════════════════════════════════════════════════════════════════════


async def release_batch(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    request: Request | None = None,
) -> DisbursementBatch:
    """Mark a batch sent to the bank.

    The irreversible step, so it re-checks the approval rather than trusting
    the status column.
    """
    batch = await _batch(session, user, batch_id)

    if batch.status is not DisbursementStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A batch in status '{batch.status.value}' cannot be released. Money "
                "only moves against an approved batch."
            ),
        )
    if not batch.approval_is_current:
        # Belt and braces: `regenerate_batch` supersedes rather than rewriting,
        # so this should be unreachable. It is checked anyway because the cost
        # of being wrong is a payment file nobody approved.
        batch.status = DisbursementStatus.PENDING_APPROVAL
        batch.approved_by_user_id = None
        batch.approved_at = None
        batch.approved_file_hash = None
        await session.flush()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The file has changed since it was approved, so the approval no longer "
                "applies to it. The batch is back awaiting approval."
            ),
        )
    if batch.approved_by_user_id == batch.prepared_by_user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This batch was approved by the person who prepared it. It will not be "
                "released."
            ),
        )

    batch.status = DisbursementStatus.RELEASED
    batch.released_at = datetime.now(UTC)
    batch.released_by_user_id = user.id
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="disbursement_batch",
        entity_id=batch.id,
        summary=(
            f"Released disbursement of {batch.total_amount} to {batch.employee_count} "
            "employees to the bank"
        ),
        changes={"file_hash": batch.file_hash},
        request=request,
    )
    return batch


# ═══════════════════════════════════════════════════════════════════════════
# Download
# ═══════════════════════════════════════════════════════════════════════════


async def download_file(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    request: Request | None = None,
) -> tuple[str, DisbursementBatch]:
    """Hand out the file, recording who took it.

    Every download is recorded, not sampled. The file is a list of every
    employee's bank account; if a copy turns up somewhere it should not, the
    question is who took it and when, and that has to be answerable.

    The window is deliberate too — a file this sensitive should not stay
    downloadable indefinitely. Past it, regenerate.
    """
    batch = await _batch(session, user, batch_id)

    if batch.encrypted_file is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No file exists for this batch — validation blocked it.",
        )
    if batch.status is DisbursementStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This batch has not been approved. The file is released to the approver "
                "after sign-off, not before."
            ),
        )

    prepared = batch.prepared_at
    if prepared is not None:
        age_hours = (datetime.now(UTC) - prepared).total_seconds() / 3600
        if age_hours > DOWNLOAD_WINDOW:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=(
                    f"This file was prepared {int(age_hours)} hours ago and is no longer "
                    f"downloadable — the window is {DOWNLOAD_WINDOW} hours. Regenerate "
                    "it, which also means it is re-approved."
                ),
            )

    content = decrypt_secret(batch.encrypted_file, user.tenant_id) or ""

    session.add(
        DisbursementDownload(
            tenant_id=user.tenant_id,
            batch_id=batch.id,
            downloaded_by_user_id=user.id,
            downloaded_at=datetime.now(UTC),
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
            file_hash=batch.file_hash,
        )
    )
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.EXPORT,
        entity_type="disbursement_batch",
        entity_id=batch.id,
        summary=(
            f"Downloaded the disbursement file for {batch.employee_count} employees "
            f"({batch.total_amount})"
        ),
        changes={"file_hash": batch.file_hash},
        request=request,
    )
    return content, batch


# ═══════════════════════════════════════════════════════════════════════════
# Reconciliation
# ═══════════════════════════════════════════════════════════════════════════


async def reconcile(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    responses: list[dict],
    request: Request | None = None,
) -> dict:
    """Apply the bank's response file to a released batch.

    Each response is `{"account_number", "status", "reason", "reference"}`.
    Matching is on account number because that is what every bank returns —
    employee codes do not survive the round trip.

    A failed credit that nobody notices until the employee calls is a serious
    incident, so the caller is handed the failures to notify on.
    """
    batch = await _batch(session, user, batch_id)
    if batch.status not in (DisbursementStatus.RELEASED, DisbursementStatus.ACKNOWLEDGED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A batch in status '{batch.status.value}' has not been sent to a bank, "
                "so there is nothing to reconcile."
            ),
        )

    lines = (
        await session.scalars(
            tenant_scoped(
                select(DisbursementLine).where(DisbursementLine.batch_id == batch.id),
                DisbursementLine,
                user,
            )
        )
    ).all()
    by_account = {
        (decrypt_secret(line.account_number, user.tenant_id) or ""): line for line in lines
    }

    credited = 0
    failed: list[dict] = []
    unmatched: list[str] = []
    now = datetime.now(UTC)

    for response in responses:
        account = str(response.get("account_number", "")).strip()
        line = by_account.get(account)
        if line is None:
            unmatched.append(account[-4:] if account else "(blank)")
            continue

        outcome = str(response.get("status", "")).strip().lower()
        line.reconciled_at = now
        line.bank_reference = response.get("reference")

        if outcome in ("success", "credited", "processed", "s"):
            line.status = LineStatus.CREDITED
            credited += 1
        else:
            line.status = LineStatus.FAILED
            line.failure_reason = response.get("reason") or "The bank did not credit this."
            failed.append(
                {
                    "line_id": str(line.id),
                    "employee_id": str(line.employee_id),
                    "amount": str(line.amount),
                    "reason": line.failure_reason,
                }
            )

    batch.status = DisbursementStatus.ACKNOWLEDGED
    await session.flush()

    # A failed salary credit nobody notices until the employee calls is a
    # serious incident, so HR Ops hear about it now. Raised inside this
    # transaction: the reconciliation and the alert land together.
    notified = 0
    if failed:
        notified = await _notify_failed_credits(session, user, batch, failed)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="disbursement_batch",
        entity_id=batch.id,
        summary=(
            f"Reconciled the bank response: {credited} credited, {len(failed)} failed"
            f"{f', {len(unmatched)} unmatched' if unmatched else ''}"
        ),
        changes={"credited": credited, "failed": len(failed), "unmatched": unmatched},
        request=request,
    )
    return {
        "credited": credited,
        "failed": failed,
        "unmatched": unmatched,
        "notified": notified,
        "parse_errors": [],
    }


async def supplementary_batch(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    value_date: date,
    request: Request | None = None,
) -> DisbursementBatch:
    """Reissue the failed credits from a batch, and only those.

    A handful of bounced credits must not mean regenerating and re-approving
    the whole run — that is how a correction turns into a second payment to
    everybody.
    """
    original = await _batch(session, user, batch_id)
    failed = (
        await session.scalars(
            tenant_scoped(
                select(DisbursementLine).where(
                    DisbursementLine.batch_id == original.id,
                    DisbursementLine.status == LineStatus.FAILED,
                ),
                DisbursementLine,
                user,
            )
        )
    ).all()

    if not failed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This batch has no failed credits to reissue.",
        )

    batch = DisbursementBatch(
        tenant_id=user.tenant_id,
        payroll_run_id=original.payroll_run_id,
        bank_profile_id=original.bank_profile_id,
        supplements_batch_id=original.id,
        status=DisbursementStatus.PENDING_APPROVAL,
        value_date=value_date,
        employee_count=len(failed),
        total_amount=sum((Decimal(str(line.amount)) for line in failed), start=Decimal(0)),
        prepared_by_user_id=user.id,
        prepared_at=datetime.now(UTC),
        notes=f"Reissuing {len(failed)} failed credit(s) from batch {original.id}.",
    )
    session.add(batch)
    await session.flush()

    profile = await session.scalar(
        tenant_scoped(
            select(BankProfile).where(BankProfile.id == original.bank_profile_id),
            BankProfile,
            user,
        )
    )
    payment_lines: list[formats.PaymentLine] = []

    for line in failed:
        number = decrypt_secret(line.account_number, user.tenant_id) or ""
        session.add(
            DisbursementLine(
                tenant_id=user.tenant_id,
                batch_id=batch.id,
                employee_id=line.employee_id,
                payslip_id=line.payslip_id,
                amount=line.amount,
                account_number=line.account_number,
                ifsc=line.ifsc,
                beneficiary_name=line.beneficiary_name,
            )
        )
        payment_lines.append(
            formats.PaymentLine(
                beneficiary_name=line.beneficiary_name,
                account_number=number,
                ifsc=line.ifsc,
                amount=Decimal(str(line.amount)),
                employee_code="",
            )
        )
        # The original line is marked reissued rather than left FAILED, so the
        # failure list does not keep reporting a credit that has been dealt with.
        line.status = LineStatus.REISSUED

    if profile is not None and payment_lines:
        instruction = formats.PaymentInstruction(
            debit_account=decrypt_secret(profile.account_number, user.tenant_id) or "",
            debit_ifsc=profile.ifsc,
            corporate_id=profile.corporate_id or "",
            value_date=value_date,
            batch_reference=f"SUPP{batch.id.hex[:8].upper()}",
            lines=payment_lines,
        )
        content = formats.serialise(
            instruction, profile.file_format, config=profile.format_config
        )
        batch.file_hash = formats.file_hash(content)
        batch.encrypted_file = encrypt_secret(content, user.tenant_id)
        batch.file_name = f"SUPP_{profile.file_format.value.upper()}_{batch.id.hex[:8]}.txt"

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="disbursement_batch",
        entity_id=batch.id,
        summary=(
            f"Prepared a supplementary disbursement for {len(failed)} failed credit(s) "
            f"from batch {original.id}"
        ),
        changes={"supplements": str(original.id), "employee_count": len(failed)},
        request=request,
    )
    return batch


# ═══════════════════════════════════════════════════════════════════════════
# Failed-credit alerts, response import, and read models
# ═══════════════════════════════════════════════════════════════════════════

EVENT_CREDIT_FAILED = "payroll.disbursement.credit_failed"

NOTICE_TEMPLATES = {
    EVENT_CREDIT_FAILED: (
        "{failed_count} salary credit(s) failed",
        "The bank did not credit {failed_count} salary payment(s) totalling {failed_total} "
        "in the disbursement for {period}. Reasons: {reasons}. Review them and issue a "
        "supplementary batch in AskMira.",
    ),
}

#: Who hears about a failed credit. HR Ops own the fix; HR admins own the money.
ALERT_ROLES = {UserRole.SUB_ADMIN, UserRole.ADMIN}


async def _notify_failed_credits(
    session: AsyncSession, user: User, batch: DisbursementBatch, failed: list[dict]
) -> int:
    """Tell HR Ops about failed credits. Returns how many people were told."""
    await support.ensure_event_templates(session, user.tenant_id, NOTICE_TEMPLATES)
    recipients = await support.active_user_ids(session, user.tenant_id, roles=ALERT_ROLES)
    if not recipients:
        recipients = await support.active_user_ids(
            session, user.tenant_id, roles={UserRole.ADMIN}
        )
    if not recipients:
        return 0
    run = await session.scalar(
        tenant_scoped(
            select(PayrollRun).where(PayrollRun.id == batch.payroll_run_id), PayrollRun, user
        )
    )
    reasons = sorted({item["reason"] for item in failed})
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key=EVENT_CREDIT_FAILED,
        recipient_user_ids=recipients,
        payload={
            "batch_id": str(batch.id),
            "failed_count": len(failed),
            "failed_total": str(sum((Decimal(item["amount"]) for item in failed), Decimal(0))),
            "period": f"{run.period_month:02d}/{run.period_year}" if run else "this run",
            "reasons": "; ".join(reasons),
        },
        commit=False,
    )
    return len(recipients)


async def import_bank_response(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    data: bytes,
    request: Request | None = None,
) -> dict:
    """Read the bank's CSV response and reconcile the batch with it."""
    try:
        parsed = responses.parse_response_csv(data)
    except responses.ResponseFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if not parsed.rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No row of the response file could be read: " + " ".join(parsed.problems),
        )
    outcome = await reconcile(
        session, user, batch_id=batch_id, responses=parsed.rows, request=request
    )
    outcome["parse_errors"] = parsed.problems
    return outcome


async def _user_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids)), User, user)
        )
    ).all()
    return {row_id: name for row_id, name in rows}


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


def _names_for(
    batch: DisbursementBatch, names: dict[uuid.UUID, str]
) -> tuple[DisbursementBatch, str | None, str | None]:
    return (
        batch,
        names.get(batch.prepared_by_user_id) if batch.prepared_by_user_id else None,
        names.get(batch.approved_by_user_id) if batch.approved_by_user_id else None,
    )


async def list_batches(
    session: AsyncSession, user: User, *, payroll_run_id: uuid.UUID | None = None
) -> list[tuple[DisbursementBatch, str | None, str | None]]:
    """Batches newest first, with who prepared and approved each by name."""
    stmt = select(DisbursementBatch).order_by(DisbursementBatch.created_at.desc())
    if payroll_run_id is not None:
        stmt = stmt.where(DisbursementBatch.payroll_run_id == payroll_run_id)
    batches = (await session.scalars(tenant_scoped(stmt, DisbursementBatch, user))).all()
    names = await _user_names(
        session,
        user,
        {
            uid
            for batch in batches
            for uid in (batch.prepared_by_user_id, batch.approved_by_user_id)
            if uid
        },
    )
    return [_names_for(batch, names) for batch in batches]


async def get_batch(
    session: AsyncSession, user: User, batch_id: uuid.UUID
) -> tuple[DisbursementBatch, str | None, str | None]:
    batch = await _batch(session, user, batch_id)
    names = await _user_names(
        session,
        user,
        {uid for uid in (batch.prepared_by_user_id, batch.approved_by_user_id) if uid},
    )
    return _names_for(batch, names)


async def batch_lines(
    session: AsyncSession, user: User, batch_id: uuid.UUID
) -> list[tuple[DisbursementLine, tuple[str, str] | None]]:
    await _batch(session, user, batch_id)
    lines = (
        await session.scalars(
            tenant_scoped(
                select(DisbursementLine).where(DisbursementLine.batch_id == batch_id),
                DisbursementLine,
                user,
            )
        )
    ).all()
    labels = await _employee_labels(session, user, {line.employee_id for line in lines})
    ordered = sorted(lines, key=lambda line: labels.get(line.employee_id, ("", ""))[0])
    return [(line, labels.get(line.employee_id)) for line in ordered]


async def bank_accounts(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID | None = None
) -> list[tuple[EmployeeBankAccount, tuple[str, str] | None]]:
    stmt = select(EmployeeBankAccount).order_by(EmployeeBankAccount.created_at.desc())
    if employee_id is not None:
        stmt = stmt.where(EmployeeBankAccount.employee_id == employee_id)
    rows = (await session.scalars(tenant_scoped(stmt, EmployeeBankAccount, user))).all()
    labels = await _employee_labels(session, user, {row.employee_id for row in rows})
    return [(row, labels.get(row.employee_id)) for row in rows]
