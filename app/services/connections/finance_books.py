"""Post an approved payroll run's journal to the linked Finance product, once.

The same journal the Tally and Zoho Books exports carry (payroll/journal.py), sent
through the product link (use=act, kind "journal.post"). Finance books it as a
published manual journal under the run's key, so a retry never books the month
twice; a ledger Finance does not have yet is added there (earnings and employer
contributions as expenses, deductions and net pay as current liabilities) and
named in the answer. AskMiraa keeps the posting (PayrollJournalPosting, target
"books_finance") so the run shows where it went.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.accounting import PayrollJournalPosting
from app.models.audit import AuditAction
from app.models.payroll import PayrollRun
from app.models.user import User
from app.services import audit
from app.services.payroll import journal as journal_service
from app.services.products import data as products

TARGET = "books_finance"
LABEL = "Finance"


def _bad(detail: str, code: int = status.HTTP_422_UNPROCESSABLE_ENTITY) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


async def posting_for_run(
    session: AsyncSession, user: User, run_id: uuid.UUID
) -> PayrollJournalPosting | None:
    return await session.scalar(
        tenant_scoped(
            select(PayrollJournalPosting).where(
                PayrollJournalPosting.run_id == run_id, PayrollJournalPosting.target == TARGET
            ),
            PayrollJournalPosting,
            user,
        )
    )


def _read(row: PayrollJournalPosting) -> dict[str, Any]:
    return {
        "posted": True,
        "journal_id": row.external_id,
        "journal_number": row.external_number,
        "amount": str(row.amount),
        "posted_at": row.posted_at.isoformat(),
    }


async def run_posting_status(session: AsyncSession, user: User, run_id: uuid.UUID) -> dict:
    linked = await products.role_in(session, user, "finance") is not None
    posting = await posting_for_run(session, user, run_id)
    if posting is not None:
        return {"linked": linked, **_read(posting)}
    return {"linked": linked, "posted": False}


def journal_params(run_id: uuid.UUID, lines: list, meta: Any) -> dict[str, Any]:
    """The run's journal as Finance's journal.post takes it: money as text, a nature per ledger."""
    return {
        "key": f"payroll_run:{run_id}",
        "date": meta.voucher_date.isoformat(),
        "reference": meta.voucher_number,
        "narration": meta.narration,
        "lines": [
            {
                "ledger": line.ledger,
                "nature": "expense" if line.debit else "liability",
                "debit": f"{line.debit:.2f}",
                "credit": f"{line.credit:.2f}",
                "description": line.cost_centre,
            }
            for line in lines
        ],
    }


async def post_run(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    by_department: bool = False,
    request: Request | None = None,
) -> dict[str, Any]:
    if await products.role_in(session, user, "finance") is None:
        raise _bad(
            "Finance is not linked to this workspace for you. Connect it under Settings → "
            "Connectors.",
            status.HTTP_409_CONFLICT,
        )
    await session.execute(
        tenant_scoped(
            select(PayrollRun.id).where(PayrollRun.id == run_id).with_for_update(),
            PayrollRun,
            user,
        )
    )
    existing = await posting_for_run(session, user, run_id)
    if existing is not None:
        raise _bad(
            f"This run is already in Finance as journal "
            f"{existing.external_number or existing.external_id} (posted "
            f"{existing.posted_at:%d %b %Y}). Posting it again would book the month's salary "
            "twice.",
            status.HTTP_409_CONFLICT,
        )
    run, lines, meta = await journal_service.prepare_journal(
        session, user, run_id, by_department=by_department
    )
    try:
        answer = await products.act(
            session, user, "finance", "journal.post", journal_params(run.id, lines, meta)
        )
    except products.ProductUnavailableError as exc:
        raise _bad(
            f"Finance did not take the journal: {exc} Nothing was posted.",
            status.HTTP_502_BAD_GATEWAY,
        ) from exc
    journal = answer.get("journal") if isinstance(answer.get("journal"), dict) else {}
    total = sum((line.debit for line in lines), start=Decimal(0))
    posting = PayrollJournalPosting(
        tenant_id=user.tenant_id,
        run_id=run.id,
        target=TARGET,
        external_id=str(journal.get("id") or ""),
        external_number=str(journal.get("number") or "") or None,
        amount=total,
        posted_at=datetime.now(UTC),
        posted_by_user_id=user.id,
    )
    session.add(posting)
    await session.flush()
    created = [str(a) for a in answer.get("created_accounts") or []]
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={
            "posted_to": LABEL,
            "journal": posting.external_number,
            "lines": len(lines),
            "amount": str(total),
            "accounts_added_there": created,
        },
        summary=(
            f"Posted the payroll journal for {run.period_month:02d}/{run.period_year} to {LABEL}"
        ),
        request=request,
    )
    await session.commit()
    return {**_read(posting), "created_accounts": created}
