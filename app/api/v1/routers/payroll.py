"""Payroll and statutory compliance endpoints (FRD 5.2).

The computation engine is V1 build order step 4 and is deliberately not stubbed
with placeholder maths — a payroll number that looks right but is not is worse
than no number. What exists here reads the versioned rule sets and run history,
which is enough for the frontend to build the payroll screens against.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.payroll import PayrollRuleSet, PayrollRun, PayrollRunStatus, PayrollRunType
from app.models.user import UserRole
from app.schemas.common import Page

router = APIRouter(
    prefix="/payroll",
    tags=["payroll"],
    # Payroll data is field-level restricted; the coarse gate is here and the
    # fine-grained one is in the service layer.
    dependencies=[
        Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS))
    ],
)


class PayrollRuleSetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rule_type: str
    jurisdiction: str
    version: int
    effective_from: date
    effective_to: date | None
    rules: dict
    source_reference: str | None
    is_approved: bool


class PayrollRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID
    period_year: int
    period_month: int
    run_type: PayrollRunType
    status: PayrollRunStatus
    employee_count: int
    total_gross: float
    total_deductions: float
    total_net: float
    variance_flags: list


@router.get("/rule-sets", response_model=list[PayrollRuleSetRead])
async def list_rule_sets(
    user: CurrentUser,
    session: DbSession,
    rule_type: str | None = Query(default=None, max_length=32),
    jurisdiction: str | None = Query(default=None, max_length=8),
    as_of: date | None = Query(default=None, description="Return the versions active on this date"),
) -> list[PayrollRuleSetRead]:
    """Statutory rule sets.

    Passing `as_of` returns the versions that were active on that date — the
    same resolution a historical payroll run performs (FS-PAY-02).
    """
    stmt = tenant_scoped(select(PayrollRuleSet), PayrollRuleSet, user)
    if rule_type:
        stmt = stmt.where(PayrollRuleSet.rule_type == rule_type)
    if jurisdiction:
        stmt = stmt.where(PayrollRuleSet.jurisdiction == jurisdiction)
    if as_of:
        stmt = stmt.where(
            PayrollRuleSet.effective_from <= as_of,
            (PayrollRuleSet.effective_to.is_(None)) | (PayrollRuleSet.effective_to >= as_of),
        )

    rows = (
        await session.scalars(
            stmt.order_by(
                PayrollRuleSet.rule_type,
                PayrollRuleSet.jurisdiction,
                PayrollRuleSet.effective_from.desc(),
            )
        )
    ).all()
    return [PayrollRuleSetRead.model_validate(row) for row in rows]


@router.get("/runs", response_model=Page[PayrollRunRead])
async def list_runs(
    user: CurrentUser,
    session: DbSession,
    year: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[PayrollRunRead]:
    stmt = tenant_scoped(select(PayrollRun), PayrollRun, user)
    if year:
        stmt = stmt.where(PayrollRun.period_year == year)

    rows = (
        await session.scalars(
            stmt.order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return Page[PayrollRunRead](
        items=[PayrollRunRead.model_validate(row) for row in rows],
        total=len(rows),
        page=page,
        page_size=page_size,
    )


# TODO(V1 step 4) — the computation engine, in this order:
#   POST /runs                      create a draft for a period
#   POST /runs/{id}/compute         resolve rule versions, compute, flag variances
#   POST /runs/{id}/approve         HR Ops sign-off; blocked while variances are unacknowledged
#   POST /runs/{id}/publish         release payslips to employees
#   GET  /runs/{id}/payslips        per-employee computation trace
#
# Two rules the engine must hold to, both release-blocking:
#   - resolve rule versions by the run's own `effective_date`, never by today
#   - refuse to compute against a rule set with no Compliance SME approval
