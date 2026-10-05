"""Payroll runs and statutory rule sets (FRD 5.2).

HR-gated at the router. Thin: parse, gate, delegate to
`app.services.payroll.run` / `payslips` / `rule_sets`, serialise.

Employees read their own payslips through `payslips.py`, which is not
HR-gated and is declared in `app/core/access_policies/payroll_run.py`.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.payroll import PayrollRunStatus
from app.models.user import UserRole
from app.schemas.common import Page
from app.schemas.payroll_run import (
    PayrollRunCreate,
    PayrollRunRead,
    PayslipRead,
    RegisterRead,
    RuleSetApprove,
    RuleSetCreate,
    RuleSetRead,
    RunMarkPaid,
    VarianceAcknowledge,
)
from app.services.payroll import payslips as payslip_service
from app.services.payroll import rule_sets as rule_set_service
from app.services.payroll import run as run_service

PAYROLL_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)

router = APIRouter(
    prefix="/payroll",
    tags=["payroll"],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)

ADMIN_ONLY = [Depends(require_roles(*ADMIN_ROLES))]


# ── Rule sets ───────────────────────────────────────────────────────────────


@router.get("/rule-sets", response_model=list[RuleSetRead])
async def list_rule_sets(
    user: CurrentUser,
    session: DbSession,
    rule_type: str | None = Query(default=None, max_length=32),
    jurisdiction: str | None = Query(default=None, max_length=8),
    as_of: date | None = Query(default=None, description="Return the versions active on this date"),
) -> list[RuleSetRead]:
    """Statutory rule sets, every version.

    Passing `as_of` returns the versions that were active on that date — the
    same resolution a historical payroll run performs (FS-PAY-02).
    """
    return await rule_set_service.list_rule_sets(
        session, user, rule_type=rule_type, jurisdiction=jurisdiction, as_of=as_of
    )


@router.post(
    "/rule-sets",
    response_model=RuleSetRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
)
async def create_rule_set(
    payload: RuleSetCreate, user: CurrentUser, session: DbSession, request: Request
) -> RuleSetRead:
    """Enter a new effective-dated version. It is unapproved and unused until signed off."""
    return await rule_set_service.create_rule_set(session, user, payload, request)


@router.post(
    "/rule-sets/{rule_set_id}/approve",
    response_model=RuleSetRead,
    dependencies=ADMIN_ONLY,
)
async def approve_rule_set(
    rule_set_id: uuid.UUID,
    payload: RuleSetApprove,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> RuleSetRead:
    """The Compliance SME's sign-off. Not by whoever entered the version."""
    return await rule_set_service.approve_rule_set(
        session, user, rule_set_id, attest=payload.attest, request=request
    )


# ── Runs ────────────────────────────────────────────────────────────────────


@router.get("/runs", response_model=Page[PayrollRunRead])
async def list_runs(
    user: CurrentUser,
    session: DbSession,
    year: int | None = Query(default=None, ge=2000, le=2100),
    run_status: PayrollRunStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[PayrollRunRead]:
    rows, total = await run_service.list_runs(
        session, user, year=year, run_status=run_status, page=page, page_size=page_size
    )
    return Page[PayrollRunRead](
        items=await payslip_service.run_reads(session, user, rows),
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/runs", response_model=PayrollRunRead, status_code=status.HTTP_201_CREATED)
async def create_run(
    payload: PayrollRunCreate, user: CurrentUser, session: DbSession, request: Request
) -> PayrollRunRead:
    run = await run_service.create_run(
        session,
        user,
        legal_entity_id=payload.legal_entity_id,
        period_year=payload.period_year,
        period_month=payload.period_month,
        run_type=payload.run_type,
        payment_date=payload.payment_date,
        request=request,
    )
    return await payslip_service.run_read(session, user, run)


@router.get("/runs/{run_id}", response_model=PayrollRunRead)
async def get_run(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> PayrollRunRead:
    run = await run_service.get_run(session, user, run_id)
    return await payslip_service.run_read(session, user, run)


@router.post("/runs/{run_id}/compute", response_model=PayrollRunRead)
async def compute_run(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> PayrollRunRead:
    """Compute every payslip. Refuses an unlocked attendance period."""
    run = await run_service.compute_run(session, user, run_id, request=request)
    return await payslip_service.run_read(session, user, run)


@router.post(
    "/runs/{run_id}/variances/{employee_id}/acknowledge", response_model=PayrollRunRead
)
async def acknowledge_variance(
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    payload: VarianceAcknowledge,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> PayrollRunRead:
    run = await run_service.acknowledge_variance(
        session, user, run_id, employee_id, note=payload.note, request=request
    )
    return await payslip_service.run_read(session, user, run)


@router.post("/runs/{run_id}/approve", response_model=PayrollRunRead)
async def approve_run(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> PayrollRunRead:
    """Maker-checker sign-off. Marks every consumed input with this run."""
    run = await run_service.approve_run(session, user, run_id, request=request)
    return await payslip_service.run_read(session, user, run)


@router.post("/runs/{run_id}/publish", response_model=PayrollRunRead)
async def publish_run(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> PayrollRunRead:
    """Release payslips to employees and notify them."""
    run = await run_service.publish_run(session, user, run_id, request=request)
    return await payslip_service.run_read(session, user, run)


@router.post("/runs/{run_id}/mark-paid", response_model=PayrollRunRead)
async def mark_paid(
    run_id: uuid.UUID,
    payload: RunMarkPaid,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> PayrollRunRead:
    run = await run_service.mark_paid(
        session, user, run_id, payment_date=payload.payment_date, request=request
    )
    return await payslip_service.run_read(session, user, run)


@router.get("/runs/{run_id}/register", response_model=RegisterRead)
async def run_register(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> RegisterRead:
    return await payslip_service.register(session, user, run_id)


@router.get("/runs/{run_id}/register.csv", response_model=None)
async def run_register_csv(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    filename, content = await payslip_service.register_csv(session, user, run_id, request)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runs/{run_id}/payslips/{payslip_id}", response_model=PayslipRead)
async def run_payslip(
    run_id: uuid.UUID, payslip_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> PayslipRead:
    """One employee's payslip with the full computation trace."""
    return await payslip_service.hr_payslip(session, user, run_id, payslip_id)


@router.get("/runs/{run_id}/payslips/{payslip_id}/pdf", response_model=None)
async def run_payslip_pdf(
    run_id: uuid.UUID,
    payslip_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> Response:
    """The payslip as a PDF. HR's copy of somebody's pay is audited."""
    data, filename = await payslip_service.hr_payslip_pdf(
        session, user, run_id, payslip_id, request=request
    )
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
