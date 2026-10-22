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

import calendar
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import tenant_scoped
from app.core.step_up import Confirm
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

#: A run's salary file that still stands: awaiting approval, approved, or sent.
#: While one does, a second file for the same run would pay everyone again.
_STANDING = frozenset(
    {
        DisbursementStatus.DRAFT,
        DisbursementStatus.PENDING_APPROVAL,
        DisbursementStatus.APPROVED,
        DisbursementStatus.RELEASED,
        DisbursementStatus.ACKNOWLEDGED,
    }
)
#: Sent to the bank. Nothing about the file can change after this.
_SENT = frozenset({DisbursementStatus.RELEASED, DisbursementStatus.ACKNOWLEDGED})


@dataclass(frozen=True, slots=True)
class HeldCredit:
    """A salary held out of a file, to be paid from a supplementary batch."""

    employee_id: uuid.UUID
    payslip_id: uuid.UUID | None
    amount: Decimal


class BatchBlockedError(Exception):
    """Validation stopped a batch being prepared. No file exists."""

    def __init__(self, batch: DisbursementBatch, result: validation.BatchValidation) -> None:
        self.batch = batch
        self.result = result
        super().__init__(
            f"{len(result.exceptions)} exception(s) prevent this disbursement being prepared."
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


async def _primary_account(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> tuple[EmployeeBankAccount | None, str]:
    """The employee's salary account as it stands now, and its number in the clear."""
    account = await session.scalar(
        tenant_scoped(
            select(EmployeeBankAccount)
            .where(
                EmployeeBankAccount.employee_id == employee_id,
                EmployeeBankAccount.is_primary.is_(True),
                EmployeeBankAccount.active.is_(True),
            )
            .order_by(EmployeeBankAccount.created_at.desc())
            .limit(1),
            EmployeeBankAccount,
            user,
        )
    )
    number = decrypt_secret(account.account_number, user.tenant_id) or "" if account else ""
    return account, number


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
    superseding: uuid.UUID | None = None,
    hold: Sequence[dict[str, Any]] = (),
    request: Request | None = None,
) -> DisbursementBatch:
    """Build the file for an approved run.

    Every exception blocks. A rejected statutory return is an annoyance; a
    salary credit to a wrong account is money gone and an employee unpaid, so
    there is no warning tier here.

    One standing file per run: a second would pay everyone again. `superseding`
    is the batch a regeneration replaces, the one standing file allowed.

    `hold` names salaries HR keeps out of this file, each with a reason
    (`{"employee_id" or "employee_code", "reason"}`): someone with no bank
    account yet is held and paid later, and everyone else is paid on time.
    """
    run = await session.scalar(
        tenant_scoped(select(PayrollRun).where(PayrollRun.id == payroll_run_id), PayrollRun, user)
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

    standing = select(DisbursementBatch).where(
        DisbursementBatch.payroll_run_id == run.id,
        DisbursementBatch.supplements_batch_id.is_(None),
        DisbursementBatch.status.in_(_STANDING),
    )
    if superseding is not None:
        standing = standing.where(DisbursementBatch.id != superseding)
    existing = await session.scalar(tenant_scoped(standing.limit(1), DisbursementBatch, user))
    if existing is not None:
        remedy = (
            "Credits the bank did not make are reissued from it as a supplementary batch."
            if existing.status in _SENT
            else "Regenerate that file instead; regenerating voids its approval."
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This run already has a salary file that is "
                f"{existing.status.value.replace('_', ' ')}. A second file would pay "
                f"everyone again. {remedy}"
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

    held = _held_entries(rows, hold)
    held_ids = {entry["employee_id"] for entry in held}
    rows = [(payslip, employee) for payslip, employee in rows if str(employee.id) not in held_ids]

    # A payslip with nothing to pay (a month of unpaid leave, recoveries that
    # took the whole wage) is left off and named, not sent as a zero credit or
    # left to block everyone else's salary.
    nothing_due = [employee for payslip, employee in rows if Decimal(str(payslip.net_pay)) == 0]
    rows = [(payslip, employee) for payslip, employee in rows if Decimal(str(payslip.net_pay)) != 0]
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No salary in this run is left to pay in a file: every payslip is held or "
                "has nothing to pay."
            ),
        )

    to_validate = []
    resolved: list[tuple[Payslip, Employee, EmployeeBankAccount | None, str]] = []

    for payslip, employee in rows:
        account, number = await _primary_account(session, user, employee.id)
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
        held=held,
        notes=_batch_notes(held, nothing_due),
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
        content = formats.serialise(instruction, profile.file_format, config=profile.format_config)
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


def _batch_notes(held: list[dict[str, Any]], nothing_due: list[Employee]) -> str | None:
    """Who is not in the file, and why, for anyone reading the batch later."""
    parts = []
    if held:
        people = "; ".join(
            f"{e['employee_code']} {e['employee_name']} ({e['reason']})" for e in held
        )
        parts.append(f"Held out of this file: {people}.")
    if nothing_due:
        people = ", ".join(f"{e.employee_code} {e.full_name}" for e in nothing_due)
        parts.append(f"Left off with nothing to pay: {people}.")
    return " ".join(parts) or None


def _held_entries(
    rows: Sequence[tuple[Payslip, Employee]], hold: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The salaries to hold, each matched to its payslip in the run, with its reason."""
    by_id = {str(employee.id): (payslip, employee) for payslip, employee in rows}
    by_code = {employee.employee_code: (payslip, employee) for payslip, employee in rows}
    held: list[dict[str, Any]] = []
    for item in hold:
        key = str(item.get("employee_id") or item.get("employee_code") or "").strip()
        pair = by_id.get(key) or by_code.get(key)
        if pair is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{key or 'An employee'} has no payslip in this run, so no salary to hold.",
            )
        payslip, employee = pair
        reason = str(item.get("reason") or "").strip()
        if len(reason) < 3:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Say why {employee.employee_code}'s salary is held.",
            )
        if any(entry["employee_id"] == str(employee.id) for entry in held):
            continue
        held.append(
            {
                "payslip_id": str(payslip.id),
                "employee_id": str(employee.id),
                "employee_code": employee.employee_code,
                "employee_name": employee.full_name,
                "amount": str(Decimal(str(payslip.net_pay))),
                "reason": reason,
                "paid_in_batch_id": None,
            }
        )
    return held


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
    if batch.status in _SENT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This batch has been sent to the bank. It cannot be rebuilt; credits the "
                "bank did not make are reissued from it as a supplementary batch."
            ),
        )

    had_approval = batch.approved_at is not None
    value_date = batch.value_date or await local_today(session, user)
    if batch.supplements_batch_id is not None:
        # A supplementary batch pays particular credits. Rebuilt from the run it
        # would pay the whole run again, so it is rebuilt from its own lines.
        if batch.status not in (DisbursementStatus.PENDING_APPROVAL, DisbursementStatus.APPROVED):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This supplementary batch has been superseded by a later one.",
            )
        own_lines = (
            await session.scalars(
                tenant_scoped(
                    select(DisbursementLine).where(DisbursementLine.batch_id == batch.id),
                    DisbursementLine,
                    user,
                )
            )
        ).all()
        rebuilt = await _reissue(
            session,
            user,
            original=await _batch(session, user, batch.supplements_batch_id),
            credits=list(own_lines),
            value_date=value_date,
            request=request,
        )
    else:
        rebuilt = await prepare_batch(
            session,
            user,
            payroll_run_id=batch.payroll_run_id,
            bank_profile_id=batch.bank_profile_id,
            value_date=value_date,
            superseding=batch.id,
            # The salaries held out of the file stay held in the rebuilt one.
            hold=[
                {"employee_id": entry["employee_id"], "reason": entry["reason"]}
                for entry in batch.held or []
            ],
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
    confirm: Confirm | None = None,
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
                "This batch was approved by the person who prepared it. It will not be released."
            ),
        )
    if confirm is not None:
        await confirm()

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
    already_reissued: list[str] = []
    now = datetime.now(UTC)

    for response in responses:
        account = str(response.get("account_number", "")).strip()
        line = by_account.get(account)
        if line is None:
            unmatched.append(account[-4:] if account else "(blank)")
            continue
        if line.status is LineStatus.REISSUED:
            # Already paid again from a supplementary batch. A response read a
            # second time must not reopen it, or it would be reissued twice.
            already_reissued.append(account[-4:])
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
    batch.acknowledged_at = batch.acknowledged_at or now
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
        "already_reissued": already_reissued,
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

    batch = await _reissue(
        session,
        user,
        original=original,
        credits=list(failed),
        value_date=value_date,
        request=request,
    )
    # The original line is marked reissued rather than left FAILED, so the
    # failure list does not keep reporting a credit that has been dealt with.
    for line in failed:
        line.status = LineStatus.REISSUED
    await session.flush()
    return batch


async def pay_held(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    value_date: date,
    request: Request | None = None,
) -> DisbursementBatch:
    """Pay the salaries held out of a file, from a supplementary batch.

    Only once the file has gone to the bank: before that, the salary simply
    goes in the file, by regenerating it without the hold.
    """
    original = await _batch(session, user, batch_id)
    waiting = [entry for entry in original.held or [] if not entry.get("paid_in_batch_id")]
    if not waiting:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No salary is held in this file, or every held salary has been paid.",
        )
    if original.status not in _SENT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This file has not gone to the bank yet. Regenerate it without the hold, so "
                "the salary goes with everyone else's."
            ),
        )
    batch = await _reissue(
        session,
        user,
        original=original,
        credits=[
            HeldCredit(
                employee_id=uuid.UUID(entry["employee_id"]),
                payslip_id=uuid.UUID(entry["payslip_id"]) if entry.get("payslip_id") else None,
                amount=Decimal(entry["amount"]),
            )
            for entry in waiting
        ],
        value_date=value_date,
        request=request,
        what="held salary(ies)",
    )
    # A new list, so the JSON column registers the change.
    original.held = [
        {**entry, "paid_in_batch_id": entry.get("paid_in_batch_id") or str(batch.id)}
        for entry in original.held
    ]
    await session.flush()
    return batch


async def _reissue(
    session: AsyncSession,
    user: User,
    *,
    original: DisbursementBatch,
    credits: Sequence[DisbursementLine | HeldCredit],
    value_date: date,
    request: Request | None,
    what: str = "failed credit(s)",
) -> DisbursementBatch:
    """A supplementary batch paying `credits` again, to each employee's account today.

    Today's account, not the one the credit failed on: "account closed" is the
    commonest failure, and its fix is the new account HR has since recorded.
    Checked as a full file is, and refused rather than built when it fails.
    """
    profile = await session.scalar(
        tenant_scoped(
            select(BankProfile).where(BankProfile.id == original.bank_profile_id),
            BankProfile,
            user,
        )
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bank profile not found")
    threshold = Decimal(str(profile.format_config.get("name_match_threshold", "0.5")))
    labels = await _employee_labels(session, user, {credit.employee_id for credit in credits})

    rows = []
    resolved: list[tuple[DisbursementLine, EmployeeBankAccount | None, str, str]] = []
    for credit in credits:
        account, number = await _primary_account(session, user, credit.employee_id)
        code, name = labels.get(credit.employee_id, (str(credit.employee_id), ""))
        resolved.append((credit, account, number, code))
        rows.append(
            {
                "subject": code,
                "employee_name": name,
                "account_holder_name": account.account_holder_name if account else "",
                "account_number": number,
                "ifsc": account.ifsc if account else "",
                "amount": Decimal(str(credit.amount)),
            }
        )
    result = validation.validate_lines(rows, name_match_threshold=threshold)
    if result.is_blocked:
        problems = " ".join(f"{e.subject}: {e.message}" for e in result.exceptions)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The {what} cannot be paid yet. {problems} Correct the account on the "
                "employee's profile, then try again."
            ),
        )

    spec = formats.FORMATS[profile.file_format]
    limit = int(profile.format_config.get("name_length", spec.name_length))
    run = await session.get(PayrollRun, original.payroll_run_id)
    source = (
        f"the {calendar.month_name[run.period_month]} {run.period_year} salary file"
        if run is not None
        else f"batch {original.id}"
    )
    batch = DisbursementBatch(
        tenant_id=user.tenant_id,
        payroll_run_id=original.payroll_run_id,
        bank_profile_id=original.bank_profile_id,
        supplements_batch_id=original.id,
        status=DisbursementStatus.PENDING_APPROVAL,
        value_date=value_date,
        employee_count=len(credits),
        total_amount=sum((Decimal(str(credit.amount)) for credit in credits), start=Decimal(0)),
        prepared_by_user_id=user.id,
        prepared_at=datetime.now(UTC),
        notes=f"Paying {len(credits)} {what} from {source}.",
    )
    session.add(batch)
    await session.flush()

    payment_lines: list[formats.PaymentLine] = []
    for credit, account, number, code in resolved:
        if account is None:  # refused above; kept for the type checker
            continue
        session.add(
            DisbursementLine(
                tenant_id=user.tenant_id,
                batch_id=batch.id,
                employee_id=credit.employee_id,
                payslip_id=credit.payslip_id,
                amount=credit.amount,
                # Snapshotted as a full file's lines are.
                account_number=encrypt_secret(number, user.tenant_id),
                ifsc=account.ifsc.upper(),
                beneficiary_name=formats.truncate_name(account.account_holder_name, limit),
            )
        )
        payment_lines.append(
            formats.PaymentLine(
                beneficiary_name=account.account_holder_name,
                account_number=number,
                ifsc=account.ifsc.upper(),
                amount=Decimal(str(credit.amount)),
                employee_code=code,
            )
        )

    instruction = formats.PaymentInstruction(
        debit_account=decrypt_secret(profile.account_number, user.tenant_id) or "",
        debit_ifsc=profile.ifsc,
        corporate_id=profile.corporate_id or "",
        value_date=value_date,
        batch_reference=f"SUPP{batch.id.hex[:8].upper()}",
        lines=payment_lines,
    )
    try:
        content = formats.serialise(instruction, profile.file_format, config=profile.format_config)
    except formats.FormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
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
            f"Prepared a supplementary disbursement for {len(credits)} {what} "
            f"from batch {original.id}"
        ),
        changes={"supplements": str(original.id), "employee_count": len(credits)},
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
        recipients = await support.active_user_ids(session, user.tenant_id, roles={UserRole.ADMIN})
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
