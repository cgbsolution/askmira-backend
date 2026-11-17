"""Consultant payout runs: approved consultant weeks paid outside payroll, with GST and TDS."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.routers.disbursement import BatchRead, _named
from app.core.deps import CurrentUser, DbSession, require_roles
from app.core.step_up import confirmation
from app.models.contractors import TdsSection
from app.models.user import UserRole
from app.services.consultants import attention, payouts, statement
from app.services.disbursement import consultant as consultant_disbursement
from app.services.disbursement import service as disbursement_service

router = APIRouter(
    prefix="/consultant-payouts",
    tags=["consultant-payouts"],
    dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))],
)


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The company paying; leave out for every company in the workspace.
    legal_entity_id: uuid.UUID | None = None
    cut_off: date
    pay_date: date


class ExcludeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class WeekIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timesheet_id: uuid.UUID


@router.get("")
async def list_runs(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await payouts.list_runs(session, user)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(
    payload: RunIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Draft a run of every approved consultant week ending by the cut-off."""
    return await payouts.create_run(
        session,
        user,
        legal_entity_id=payload.legal_entity_id,
        cut_off=payload.cut_off,
        pay_date=payload.pay_date,
        request=request,
    )


class PaidIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paid_on: date
    reference: str = Field(min_length=1, max_length=200)


class BankFileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_profile_id: uuid.UUID
    value_date: date


class EndSalariesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    effective_to: date


class OpeningIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    fy_start: date
    section: TdsSection
    fees_paid: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    taxed_base: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    tds_deducted: Decimal = Field(ge=0, max_digits=14, decimal_places=2)


# Fixed paths come before `/{run_id}` so they are not read as a run id.


@router.get("/attention")
async def attention_list(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Consultants to sort out before payouts start, payroll adjustments still pending,
    and how many older weeks are waiting to be added to a run."""
    return {
        "consultants": await attention.attention_list(session, user),
        "pending_adjustments": await attention.pending_adjustments(session, user),
        "includable_weeks": len(await payouts.includable_weeks(session, user)),
    }


@router.get("/includable-weeks")
async def includable_weeks(
    user: CurrentUser,
    session: DbSession,
    legal_entity_id: uuid.UUID | None = None,
    cut_off: date | None = None,
) -> list[dict[str, Any]]:
    """Approved, unpaid weeks from before payouts were released, to add to a draft run."""
    return await payouts.includable_weeks(
        session, user, legal_entity_id=legal_entity_id, cut_off=cut_off
    )


@router.post("/attention/end-salaries")
async def end_salaries(
    payload: EndSalariesIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, int]:
    """End these consultants' current salaries; they are paid through payouts instead."""
    ended = await attention.end_salaries(
        session, user, payload.employee_ids, effective_to=payload.effective_to, request=request
    )
    return {"salaries_ended": ended}


@router.put("/openings")
async def set_opening(
    payload: OpeningIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Fees already paid to a consultant earlier in the financial year, for the TDS threshold."""
    return await attention.set_opening(session, user, **payload.model_dump(), request=request)


@router.get("/lines/{line_id}/pdf", response_model=None)
async def line_pdf(line_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    """One consultant's payout statement as a PDF."""
    filename, data = await statement.payout_statement_pdf(session, user, line_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await payouts.get_run(session, user, run_id)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    """Delete a draft; its weeks are free for a later run."""
    await payouts.delete_run(session, user, run_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{run_id}/recalculate")
async def recalculate(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await payouts.recalculate(session, user, run_id, request=request)


@router.post("/{run_id}/lines/{line_id}/exclude")
async def exclude_line(
    run_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: ExcludeIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Leave a consultant out of this run; their weeks are free for a later one."""
    return await payouts.exclude_line(
        session, user, run_id, line_id, reason=payload.reason, request=request
    )


@router.post("/{run_id}/weeks")
async def include_week(
    run_id: uuid.UUID, payload: WeekIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Add an older approved, unpaid week the run did not take on its own."""
    return await payouts.include_week(session, user, run_id, payload.timesheet_id, request=request)


@router.post("/{run_id}/approve")
async def approve_run(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Maker-checker sign-off, with fresh proof of identity. Locks the run."""
    return await payouts.approve_run(
        session,
        user,
        run_id,
        confirm=confirmation(session, user, "payroll.run_approve"),
        request=request,
    )


@router.post("/{run_id}/paid")
async def mark_paid(
    run_id: uuid.UUID, payload: PaidIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Record that an approved run was paid, with the bank reference."""
    return await payouts.mark_paid(
        session, user, run_id, paid_on=payload.paid_on, reference=payload.reference, request=request
    )


@router.post("/{run_id}/bank-file", response_model=BatchRead, status_code=status.HTTP_201_CREATED)
async def bank_file(
    run_id: uuid.UUID,
    payload: BankFileIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> BatchRead:
    """Build the bank file paying an approved run; approved and released like a salary file."""
    try:
        batch = await consultant_disbursement.prepare_consultant_batch(
            session,
            user,
            payout_run_id=run_id,
            bank_profile_id=payload.bank_profile_id,
            value_date=payload.value_date,
            request=request,
        )
    except disbursement_service.BatchBlockedError as exc:
        # The blocked batch is kept, with its exceptions, so HR can see what to fix.
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": str(exc),
                "batch_id": str(exc.batch.id),
                "exceptions": exc.result.as_list(),
            },
        ) from exc
    await session.commit()
    await session.refresh(batch)
    return await _named(session, user, batch)
