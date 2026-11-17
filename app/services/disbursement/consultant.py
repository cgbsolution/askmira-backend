"""The bank file for an approved consultant payout run.

Built exactly as a salary file is (same bank formats, same account checks, same
maker-checker on approve and release), paying each consultant's net payable
from the run. Only lines still in the run with something to pay go in the
file; a line HR left out, or one with nothing to pay, does not.

A file not yet sent is regenerated as a salary file is (voiding its
approval). Reissuing failed credits, paying held credits and reading the
bank's response are salary-file operations not yet built for consultants;
they refuse a consultant file rather than guess.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hr_scope, permissions
from app.core.deps import tenant_scoped
from app.models.consultant_payouts import (
    ConsultantPayoutLine,
    ConsultantPayoutRun,
    PayoutRunStatus,
)
from app.models.disbursement import BankProfile, DisbursementBatch
from app.models.employee import Employee
from app.models.user import User
from app.services.disbursement import formats
from app.services.disbursement.service import (
    _SENT,
    _STANDING,
    BatchPayee,
    _build_batch,
)
from app.services.payroll.run import _require_scope_covers

__all__ = ["prepare_consultant_batch"]

_HR_ONLY = "Consultant payouts are run by HR."


async def prepare_consultant_batch(
    session: AsyncSession,
    user: User,
    *,
    payout_run_id: uuid.UUID,
    bank_profile_id: uuid.UUID,
    value_date: date,
    superseding: uuid.UUID | None = None,
    request: Request | None = None,
) -> DisbursementBatch:
    """Build the bank file paying an approved consultant payout run.

    A run already marked paid gets no file: it was paid by other means.

    One standing file per run, as for a payroll run: a second would pay every
    consultant again. `superseding` is the batch a regeneration replaces, the
    one standing file allowed. Every validation exception blocks, as it does for
    salaries; the blocked batch is kept as FAILED with its exceptions.
    """
    permissions.require_hr(user, detail=_HR_ONLY)
    run = await session.scalar(
        tenant_scoped(
            select(ConsultantPayoutRun).where(ConsultantPayoutRun.id == payout_run_id),
            ConsultantPayoutRun,
            user,
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payout run not found")
    if run.status is PayoutRunStatus.PAID:
        # Marked paid by HR, by some other means: nothing ties that payment to a
        # file here, so a file now would pay every consultant a second time.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payout run is marked paid. A bank file now would pay everyone again.",
        )
    if run.status is not PayoutRunStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This payout run is '{run.status.value}'. Money is only ever disbursed "
                "against an approved run."
            ),
        )

    standing = select(DisbursementBatch).where(
        DisbursementBatch.consultant_payout_run_id == run.id,
        DisbursementBatch.supplements_batch_id.is_(None),
        DisbursementBatch.status.in_(_STANDING),
    )
    if superseding is not None:
        standing = standing.where(DisbursementBatch.id != superseding)
    existing = await session.scalar(tenant_scoped(standing.limit(1), DisbursementBatch, user))
    if existing is not None:
        remedy = (
            ""
            if existing.status in _SENT
            else " Regenerate that file instead; regenerating voids its approval."
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This payout run already has a bank file that is "
                f"{existing.status.value.replace('_', ' ')}. A second file would pay "
                f"everyone again.{remedy}"
            ),
        )

    profile = await session.scalar(
        tenant_scoped(
            select(BankProfile).where(BankProfile.id == bank_profile_id), BankProfile, user
        )
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bank profile not found")

    # The file pays everybody on the run, so, as for approving the run, the
    # preparer's department or location scope must cover them all.
    if hr_scope.current_scope() is not None:
        on_run = await session.scalars(
            tenant_scoped(
                select(ConsultantPayoutLine.employee_id).where(
                    ConsultantPayoutLine.run_id == run.id
                ),
                ConsultantPayoutLine,
                user,
            ).execution_options(hr_scope_bypass=True)
        )
        _require_scope_covers(set(on_run.all()), doing="prepare the bank file for")

    rows = (
        await session.execute(
            tenant_scoped(
                select(ConsultantPayoutLine, Employee)
                .join(Employee, ConsultantPayoutLine.employee_id == Employee.id)
                .where(
                    ConsultantPayoutLine.run_id == run.id,
                    ConsultantPayoutLine.excluded.is_(False),
                    ConsultantPayoutLine.net_payable > 0,
                )
                .order_by(Employee.employee_code),
                ConsultantPayoutLine,
                user,
            ).execution_options(hr_scope_bypass=True)
        )
    ).all()
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Nobody in this payout run is left to pay in a file.",
        )

    return await _build_batch(
        session,
        user,
        profile=profile,
        value_date=value_date,
        payees=[
            BatchPayee(employee=employee, amount=line.net_payable, payout_line_id=line.id)
            for line, employee in rows
        ],
        batch_reference=f"CON{run.pay_date:%Y%m%d}",
        file_stem=f"CON{run.pay_date:%Y%m%d}_{run.id.hex[:8]}",
        link={"consultant_payout_run_id": run.id},
        held=[],
        notes=None,
        period=f"the consultant payout run paying {run.pay_date:%d %b %Y}",
        # Labelled as fees at the bank, never as salary.
        purpose=formats.PaymentPurpose.PROFESSIONAL_FEES,
        request=request,
    )
