"""Expense Genie claims, paid with salary in AskMiraa payroll.

With Expense Genie linked, the payroll run asks it — as the person running
payroll, who must be an admin there — for the claims approved and not yet paid
(kind ``payable``). Each becomes a reimbursement line on the submitter's
payslip: paid at cost, never taxed, exactly as AskMiraa's own expense claims are
(inputs.ExpenseClaimProvider).

The run's approval writes a `LinkedClaimPayment` per claim, unique per
workspace, so a claim can never be paid by two runs. Then Expense Genie is told
(kind ``claims.paid``) and marks each one Paid with the payroll reference; if it
cannot be reached, the scheduler tells it later. Nothing a claim says is
trusted for the amount beyond what Expense Genie returned at compute time — and
the approval refuses a claim another run already paid.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.linked_claims import LinkedClaimPayment
from app.models.payroll import PayrollRun, PayrollRunStatus
from app.models.user import User, UserRole
from app.services import audit
from app.services.payroll.inputs import ExpenseGenieClaimProvider

__all__ = ["ExpenseGenieClaimProvider", "hand_back", "hand_back_pending"]

logger = logging.getLogger("askmira.payroll.linked_expenses")

PRODUCT = "expense"
BRAND = "Expense Genie"


async def hand_back(session: AsyncSession, user: User, run: PayrollRun) -> dict[str, Any]:
    """Tell Expense Genie which of its claims this approved run paid. Never raises."""
    from app.services.products import data as products

    rows = (
        await session.scalars(
            tenant_scoped(
                select(LinkedClaimPayment).where(
                    LinkedClaimPayment.run_id == run.id,
                    LinkedClaimPayment.product == PRODUCT,
                    LinkedClaimPayment.marked_at.is_(None),
                ),
                LinkedClaimPayment,
                user,
            )
        )
    ).all()
    if not rows:
        return {"marked": 0, "pending": 0}
    period = f"{run.period_year}-{run.period_month:02d}"
    paid_on = (run.approved_at or datetime.now(UTC)).date().isoformat()
    now = datetime.now(UTC)
    try:
        answer = await products.act(
            session,
            user,
            PRODUCT,
            "claims.paid",
            {
                "references": [row.reference for row in rows],
                "paid_on": paid_on,
                "note": f"Paid with salary in AskMiraa payroll for {period}",
            },
        )
    except products.ProductUnavailableError as exc:
        for row in rows:
            row.mark_error = str(exc)[:1000]
        await session.commit()
        return {"marked": 0, "pending": len(rows), "error": str(exc)}
    except Exception as exc:  # never let the hand-back undo an approved run
        logger.warning("Expense Genie hand-back failed for run %s", run.id, exc_info=True)
        for row in rows:
            row.mark_error = f"Could not reach {BRAND}: {exc}"[:1000]
        await session.commit()
        return {"marked": 0, "pending": len(rows), "error": str(exc)}

    done = set(answer.get("paid") or [])
    already = {
        str(item.get("reference"))
        for item in answer.get("skipped") or []
        if isinstance(item, dict) and item.get("reason") == "Already paid."
    }
    reasons = {
        str(item.get("reference")): str(item.get("reason"))
        for item in answer.get("skipped") or []
        if isinstance(item, dict)
    }
    marked = 0
    for row in rows:
        if row.reference in done or row.reference in already:
            row.marked_at, row.mark_error = now, None
            marked += 1
        else:
            row.mark_error = f"{BRAND} did not record it: {reasons.get(row.reference, 'no reason')}"
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={"expense_genie_marked_paid": marked, "references": [r.reference for r in rows]},
        summary=f"Told {BRAND} {marked} claim(s) were paid with salary for {period}",
    )
    await session.commit()
    return {"marked": marked, "pending": len(rows) - marked}


async def hand_back_pending(session: AsyncSession, actor: User | None) -> dict[str, Any]:
    """The scheduler: claims paid by approved runs that Expense Genie has not recorded yet.

    Told as the admin who approved the run, while they still are one: Expense
    Genie records a payment only from an admin there.
    """
    if actor is None:
        return {"runs": 0, "marked": 0}
    run_ids = (
        await session.scalars(
            select(func.distinct(LinkedClaimPayment.run_id)).where(
                tenant_filter(LinkedClaimPayment, actor.tenant_id),
                LinkedClaimPayment.marked_at.is_(None),
            )
        )
    ).all()
    marked = 0
    for run_id in run_ids:
        run = await session.scalar(
            select(PayrollRun).where(
                tenant_filter(PayrollRun, actor.tenant_id),
                PayrollRun.id == run_id,
                or_(
                    PayrollRun.status == PayrollRunStatus.APPROVED,
                    PayrollRun.status == PayrollRunStatus.PAID,
                ),
            )
        )
        if run is None or run.approved_by_user_id is None:
            continue
        approver = await session.get(User, run.approved_by_user_id)
        if (
            approver is None
            or not approver.is_active
            or approver.tenant_id != actor.tenant_id
            or approver.role not in (UserRole.ADMIN, UserRole.SUB_ADMIN, UserRole.SUPER_ADMIN)
        ):
            continue
        approver.has_reports = False  # type: ignore[attr-defined]
        result = await hand_back(session, approver, run)
        marked += int(result.get("marked") or 0)
    return {"runs": len(run_ids), "marked": marked}
