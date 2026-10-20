"""Payroll items for the unified approvals inbox.

Registered in `app.services.approvals.service.SOURCES`. Each mirrors the rule
on its module's decide endpoint — including the ones a role gate cannot see:
a payroll run is never shown to the person who computed it (maker-checker),
and nobody is shown a proof or a bill of their own.
"""

from __future__ import annotations

import calendar
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.loans import ClaimStatus, ReimbursementClaim, ReimbursementComponent
from app.models.org import LegalEntity
from app.models.payroll import PayrollRun, PayrollRunStatus
from app.models.tax import ProofStatus, TaxDeclaration, TaxProof
from app.models.user import User
from app.services.documents.indian_numbers import format_inr

__all__ = ["reimbursement_claim_items", "run_approval_items", "tax_proof_items"]


def _money(value: Any) -> str:
    return format_inr(value)


async def run_approval_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Computed runs waiting for sign-off, except the ones the caller computed."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(PayrollRun, LegalEntity.name)
                .outerjoin(LegalEntity, LegalEntity.id == PayrollRun.legal_entity_id)
                .where(
                    PayrollRun.status == PayrollRunStatus.PENDING_REVIEW,
                    or_(
                        PayrollRun.computed_by_user_id.is_(None),
                        PayrollRun.computed_by_user_id != user.id,
                    ),
                )
                .order_by(PayrollRun.period_year, PayrollRun.period_month),
                PayrollRun,
                user,
            )
        )
    ).all()
    items = []
    for run, entity_name in rows:
        open_flags = sum(1 for flag in run.variance_flags or [] if not flag.get("acknowledged"))
        detail = f"{run.employee_count} employees · net {_money(run.total_net)}"
        if open_flags:
            detail += f" · {open_flags} variance(s) to acknowledge"
        if run.review_flags:
            detail += f" · {len(run.review_flags)} statutory question(s) open"
        items.append(
            {
                "id": run.id,
                "title": (
                    f"Payroll for {calendar.month_name[run.period_month]} {run.period_year}"
                ),
                "subject_name": entity_name,
                "detail": detail,
                "submitted_at": run.processed_at or run.created_at,
                "due_on": run.payment_date,
            }
        )
    return items


async def tax_proof_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Investment proofs waiting for verification, never the caller's own."""
    stmt = (
        select(TaxProof, TaxDeclaration, Employee)
        .join(TaxDeclaration, TaxProof.declaration_id == TaxDeclaration.id)
        .join(Employee, TaxDeclaration.employee_id == Employee.id)
        .where(TaxProof.status == ProofStatus.PENDING)
        .order_by(TaxProof.created_at)
    )
    if user.employee_id is not None:
        stmt = stmt.where(TaxDeclaration.employee_id != user.employee_id)
    rows = (await session.execute(tenant_scoped(stmt, TaxProof, user))).all()
    return [
        {
            "id": proof.id,
            "title": f"{declaration.section} proof, {declaration.financial_year}",
            "subject_name": employee.full_name,
            "detail": (
                f"{_money(proof.proof_amount)} proved against "
                f"{_money(declaration.declared_amount)} declared"
            ),
            "submitted_at": proof.created_at,
        }
        for proof, declaration, employee in rows
    ]


async def reimbursement_claim_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Salary-reimbursement bills waiting for verification, never the caller's own."""
    stmt = (
        select(ReimbursementClaim, Employee)
        .join(Employee, ReimbursementClaim.employee_id == Employee.id)
        .where(ReimbursementClaim.status == ClaimStatus.SUBMITTED)
        .order_by(ReimbursementClaim.created_at)
    )
    if user.employee_id is not None:
        stmt = stmt.where(ReimbursementClaim.employee_id != user.employee_id)
    rows = (await session.execute(tenant_scoped(stmt, ReimbursementClaim, user))).all()
    if not rows:
        return []
    names = {
        code: name
        for code, name in (
            await session.execute(
                tenant_scoped(
                    select(ReimbursementComponent.code, ReimbursementComponent.name),
                    ReimbursementComponent,
                    user,
                )
            )
        ).all()
    }
    return [
        {
            "id": claim.id,
            "title": f"{names.get(claim.component_code, claim.component_code)} bill",
            "subject_name": employee.full_name,
            "detail": f"{_money(claim.amount)} · {claim.financial_year}",
            "submitted_at": claim.created_at,
        }
        for claim, employee in rows
    ]
