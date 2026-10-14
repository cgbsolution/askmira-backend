"""Payslip self-service: an employee's own payslips and salary history.

Not HR-gated — every employee reads their own. Each route acts only on the
caller's own employee record, taken from the token in the service; none takes a
person id. Declared in `app/core/access_policies/payroll_run.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.payroll_run import PayslipRead, PayslipSummaryRead, SalaryRecordRead
from app.services.payroll import payslips as payslip_service
from app.services.payroll import total_rewards

router = APIRouter(prefix="/payroll", tags=["payslips"])


@router.get("/my-payslips", response_model=list[PayslipSummaryRead])
async def my_payslips(user: CurrentUser, session: DbSession) -> list[PayslipSummaryRead]:
    """Your published payslips, newest first."""
    return await payslip_service.my_payslips(session, user)


@router.get("/my-payslips/{payslip_id}", response_model=PayslipRead)
async def my_payslip(
    payslip_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> PayslipRead:
    """One of your published payslips, with its computation trace."""
    return await payslip_service.my_payslip(session, user, payslip_id)


@router.get("/my-payslips/{payslip_id}/pdf", response_model=None)
async def my_payslip_pdf(payslip_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    """One of your published payslips as a PDF to keep."""
    data, filename = await payslip_service.my_payslip_pdf(session, user, payslip_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/my-salary", response_model=list[SalaryRecordRead])
async def my_salary(user: CurrentUser, session: DbSession) -> list[SalaryRecordRead]:
    """Your salary history: every revision, newest first."""
    return await payslip_service.my_salary_history(session, user)



@router.get("/my-total-rewards")
async def my_total_rewards(user: CurrentUser, session: DbSession) -> dict:
    """Everything the company spends on you this year: CTC, contributions, recognition."""
    permissions.own_employee_id(user)
    return await total_rewards.statement(session, user, user.employee_id)


@router.get(
    "/employees/{employee_id}/total-rewards",
    dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))],
)
async def employee_total_rewards(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict:
    """One person's total-rewards statement, for HR."""
    return await total_rewards.statement(session, user, employee_id)
