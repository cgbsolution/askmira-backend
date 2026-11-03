"""Payslips for chosen people over the last 1, 3, 6 or 12 months: downloaded or emailed.

HR-gated (admin, sub-admin). Thin: parse, gate, delegate to
`app.services.payroll.payslip_batches`, serialise.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.payslip_batch import (
    PayslipBatchCreate,
    PayslipBatchRead,
    PayslipEmailCreate,
    PayslipPeopleRead,
)
from app.services.payroll import payslip_batches as service

router = APIRouter(
    prefix="/payroll/payslip-batches",
    tags=["payslips"],
    dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))],
)


@router.get("/people", response_model=PayslipPeopleRead)
async def payslip_people(
    user: CurrentUser,
    session: DbSession,
    months: int = Query(default=1),
    to: date | None = Query(default=None, description="Last month covered; default the latest"),
) -> PayslipPeopleRead:
    """Everybody, with how many payslips each has in the period."""
    return await service.people(session, user, months=months, to=to)


@router.post("/export", response_model=PayslipBatchRead, status_code=status.HTTP_202_ACCEPTED)
async def download_payslips(
    payload: PayslipBatchCreate, user: CurrentUser, session: DbSession, request: Request
) -> PayslipBatchRead:
    """Prepare the payslips of the people picked as one file to download."""
    return await service.create_batch(
        session, user, payload, action=service.DOWNLOAD, request=request
    )


@router.post("/email", response_model=PayslipBatchRead, status_code=status.HTTP_202_ACCEPTED)
async def email_payslips(
    payload: PayslipEmailCreate, user: CurrentUser, session: DbSession, request: Request
) -> PayslipBatchRead:
    """Email each person picked their released payslips for the period."""
    return await service.create_batch(session, user, payload, action=service.EMAIL, request=request)


@router.get("", response_model=list[PayslipBatchRead])
async def recent_batches(user: CurrentUser, session: DbSession) -> list[PayslipBatchRead]:
    """The latest requests, newest first."""
    return await service.list_batches(session, user)


@router.get("/{batch_id}", response_model=PayslipBatchRead)
async def payslip_batch(
    batch_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> PayslipBatchRead:
    """One request, with each person's outcome."""
    return await service.get_batch(session, user, batch_id)


@router.get("/{batch_id}/file", response_model=None)
async def payslip_batch_file(
    batch_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    """The prepared ZIP (or PDF). Taking a copy of people's pay is audited."""
    data, filename, media = await service.batch_file(session, user, batch_id, request=request)
    return Response(
        content=data,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
