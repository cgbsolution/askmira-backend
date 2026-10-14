"""Contractors, their invoices with TDS under 194C / 194J, and the 26Q working file."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services import contractors

router = APIRouter(
    prefix="/contractors",
    tags=["contractors"],
    dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))],
)


class PaidIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paid_on: date
    reference: str | None = Field(default=None, max_length=64)


@router.get("")
async def list_contractors(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await contractors.list_contractors(session, user)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_contractor(
    payload: contractors.ContractorIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await contractors.create_contractor(session, user, payload, request=request)


@router.get("/invoices")
async def list_invoices(
    user: CurrentUser, session: DbSession, contractor_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    return await contractors.list_invoices(session, user, contractor_id=contractor_id)


@router.post("/{contractor_id}/invoices", status_code=status.HTTP_201_CREATED)
async def create_invoice(
    contractor_id: uuid.UUID,
    payload: contractors.InvoiceIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Enter an invoice; its TDS is worked out from the rule set in force that day."""
    return await contractors.create_invoice(
        session, user, contractor_id, payload, request=request
    )


@router.post("/invoices/{invoice_id}/approve")
async def approve_invoice(
    invoice_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await contractors.approve_invoice(session, user, invoice_id, request=request)


@router.post("/invoices/{invoice_id}/paid")
async def mark_paid(
    invoice_id: uuid.UUID,
    payload: PaidIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await contractors.mark_paid(
        session, user, invoice_id, paid_on=payload.paid_on, reference=payload.reference,
        request=request,
    )


@router.get("/form-26q")
async def form_26q(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    financial_year: str = Query(pattern=r"^\d{4}-\d{2}$"),
    quarter: int = Query(ge=1, le=4),
) -> Response:
    """The quarter's 26Q deductee lines, for your return-preparation utility."""
    content = await contractors.form_26q_csv(
        session, user, financial_year=financial_year, quarter=quarter, request=request
    )
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="26Q-{financial_year}-Q{quarter}.csv"',
            "X-Content-Type-Options": "nosniff",
        },
    )
