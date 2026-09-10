"""Statutory return generation and the filing record (slice 10)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.filings import (
    FilingRecord,
    FilingStatus,
    ReturnType,
    TdsChallan,
    TdsChallanMapping,
)
from app.models.payroll import Payslip
from app.models.user import UserRole
from app.services import audit
from app.services.filings import service as filing_service

router = APIRouter(tags=["filings"])

FILING_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class MonthlyReturnRequest(BaseModel):
    legal_entity_id: uuid.UUID
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


class QuarterlyReturnRequest(BaseModel):
    legal_entity_id: uuid.UUID
    financial_year: str = Field(pattern=r"^\d{4}-\d{4}$", examples=["2026-2027"])
    quarter: int = Field(ge=1, le=4)


class FilingRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    return_type: ReturnType
    legal_entity_id: uuid.UUID
    period: str
    jurisdiction: str | None
    status: FilingStatus
    file_name: str | None
    content_hash: str | None
    record_count: int
    total_amount: Decimal
    generated_at: datetime | None
    acknowledgement_number: str | None
    filed_at: datetime | None
    rejection_reason: str | None
    validation_exceptions: list[dict[str, Any]]


class GenerationResult(BaseModel):
    """What a generation attempt produced.

    `content` is returned inline rather than behind a download URL: these files
    are a few kilobytes, and an HR user in a filing window wants the text now.
    """

    record: FilingRecordRead
    content: str | None
    warnings: list[dict[str, Any]]


class AcknowledgementCreate(BaseModel):
    acknowledgement_number: str = Field(min_length=1, max_length=64)
    filed_at: datetime | None = None


class RejectionCreate(BaseModel):
    reason: str = Field(min_length=1)


class ChallanCreate(BaseModel):
    legal_entity_id: uuid.UUID
    bsr_code: str = Field(pattern=r"^\d{7}$", description="Seven-digit collecting branch code")
    deposit_date: date
    challan_serial: str = Field(min_length=1, max_length=9)
    financial_year: str = Field(pattern=r"^\d{4}-\d{4}$")
    quarter: int = Field(ge=1, le=4)
    tds_amount: Decimal = Field(ge=0)
    surcharge: Decimal = Field(default=Decimal(0), ge=0)
    education_cess: Decimal = Field(default=Decimal(0), ge=0)
    interest: Decimal = Field(default=Decimal(0), ge=0)
    late_fee: Decimal = Field(default=Decimal(0), ge=0)
    total_amount: Decimal = Field(ge=0)
    is_book_entry: bool = False

    @model_validator(mode="after")
    def components_sum_to_total(self) -> ChallanCreate:
        parts = (
            self.tds_amount
            + self.surcharge
            + self.education_cess
            + self.interest
            + self.late_fee
        )
        if parts != self.total_amount:
            raise ValueError(
                f"The components sum to {parts} but the total is {self.total_amount}. A "
                "challan whose parts do not reconcile is a transcription error, and the "
                "portal will find it later than this will."
            )
        return self


class ChallanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID
    bsr_code: str
    deposit_date: date
    challan_serial: str
    financial_year: str
    quarter: int
    tds_amount: Decimal
    total_amount: Decimal
    is_book_entry: bool


class MappingCreate(BaseModel):
    challan_id: uuid.UUID
    payslip_id: uuid.UUID
    tds_amount: Decimal = Field(gt=0)
    deduction_date: date


class MappingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    challan_id: uuid.UUID
    employee_id: uuid.UUID
    payslip_id: uuid.UUID
    tds_amount: Decimal
    deduction_date: date


# ── Helpers ─────────────────────────────────────────────────────────────────


def _blocked(exc: filing_service.FilingBlockedError) -> HTTPException:
    """A blocked generation is a 422 carrying the fixable list.

    The list is the point. A bare "validation failed" would send the user back
    to the portal-rejection workflow this whole slice exists to avoid.
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "message": str(exc),
            "filing_record_id": str(exc.record.id),
            "exceptions": exc.result.as_list(),
        },
    )


def _result(generated: filing_service.GeneratedFile) -> GenerationResult:
    return GenerationResult(
        record=FilingRecordRead.model_validate(generated.record),
        content=generated.content,
        warnings=[e.as_dict() for e in generated.result.warnings],
    )


# ── Generation ──────────────────────────────────────────────────────────────


@router.post(
    "/filings/pf-ecr",
    response_model=GenerationResult,
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def generate_pf_ecr(
    payload: MonthlyReturnRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> GenerationResult:
    try:
        generated = await filing_service.generate_ecr(
            session,
            user,
            legal_entity_id=payload.legal_entity_id,
            year=payload.year,
            month=payload.month,
            request=request,
        )
    except filing_service.FilingBlockedError as exc:
        await session.commit()
        raise _blocked(exc) from exc
    await session.commit()
    return _result(generated)


@router.post(
    "/filings/esic",
    response_model=GenerationResult,
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def generate_esic(
    payload: MonthlyReturnRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> GenerationResult:
    try:
        generated = await filing_service.generate_esic(
            session,
            user,
            legal_entity_id=payload.legal_entity_id,
            year=payload.year,
            month=payload.month,
            request=request,
        )
    except filing_service.FilingBlockedError as exc:
        await session.commit()
        raise _blocked(exc) from exc
    await session.commit()
    return _result(generated)


@router.post(
    "/filings/professional-tax",
    response_model=list[GenerationResult],
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def generate_pt(
    payload: MonthlyReturnRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> list[GenerationResult]:
    """One challan per state, because that is how Professional Tax is filed."""
    try:
        produced = await filing_service.generate_pt_challans(
            session,
            user,
            legal_entity_id=payload.legal_entity_id,
            year=payload.year,
            month=payload.month,
            request=request,
        )
    except filing_service.FilingBlockedError as exc:
        await session.commit()
        raise _blocked(exc) from exc
    await session.commit()
    return [_result(item) for item in produced]


@router.post(
    "/filings/tds-24q",
    response_model=GenerationResult,
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def generate_24q(
    payload: QuarterlyReturnRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> GenerationResult:
    try:
        generated = await filing_service.generate_tds_24q(
            session,
            user,
            legal_entity_id=payload.legal_entity_id,
            financial_year=payload.financial_year,
            quarter=payload.quarter,
            request=request,
        )
    except filing_service.FilingBlockedError as exc:
        await session.commit()
        raise _blocked(exc) from exc
    await session.commit()
    return _result(generated)


# ── Filing records ──────────────────────────────────────────────────────────


@router.get(
    "/filings",
    response_model=list[FilingRecordRead],
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def list_filings(
    user: CurrentUser,
    session: DbSession,
    return_type: ReturnType | None = None,
    period: str | None = None,
    filing_status: FilingStatus | None = Query(default=None, alias="status"),
) -> list[FilingRecordRead]:
    stmt = select(FilingRecord).order_by(FilingRecord.created_at.desc())
    if return_type is not None:
        stmt = stmt.where(FilingRecord.return_type == return_type)
    if period is not None:
        stmt = stmt.where(FilingRecord.period == period)
    if filing_status is not None:
        stmt = stmt.where(FilingRecord.status == filing_status)

    rows = (await session.scalars(tenant_scoped(stmt, FilingRecord, user))).all()
    return [FilingRecordRead.model_validate(row) for row in rows]


@router.post(
    "/filings/{record_id}/acknowledgement",
    response_model=FilingRecordRead,
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def acknowledge(
    record_id: uuid.UUID,
    payload: AcknowledgementCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> FilingRecordRead:
    """Record the portal's acknowledgement after a successful upload."""
    record = await filing_service.record_acknowledgement(
        session,
        user,
        record_id=record_id,
        acknowledgement_number=payload.acknowledgement_number,
        filed_at=payload.filed_at,
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return FilingRecordRead.model_validate(record)


@router.post(
    "/filings/{record_id}/rejection",
    response_model=FilingRecordRead,
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def reject(
    record_id: uuid.UUID,
    payload: RejectionCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> FilingRecordRead:
    """Record a portal rejection and why.

    Kept rather than deleted: the next attempt needs to know what was wrong,
    and "we filed on the 14th and it bounced" is part of the trail.
    """
    record = await session.scalar(
        tenant_scoped(
            select(FilingRecord).where(FilingRecord.id == record_id), FilingRecord, user
        )
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing record not found")

    record.status = FilingStatus.REJECTED
    record.rejection_reason = payload.reason
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="filing_record",
        entity_id=record.id,
        summary=f"Portal rejected {record.return_type.value} {record.period}",
        changes={"rejection_reason": payload.reason},
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return FilingRecordRead.model_validate(record)


@router.get(
    "/filings/{record_id}/download",
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def download(record_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    """Regenerate is not the same as re-download.

    A file is not stored on disk in this build, so this returns the metadata a
    caller needs to regenerate deterministically — including the content hash,
    so a regenerated file can be checked against what was filed.
    """
    record = await session.scalar(
        tenant_scoped(
            select(FilingRecord).where(FilingRecord.id == record_id), FilingRecord, user
        )
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing record not found")
    if record.status is FilingStatus.BLOCKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This return was blocked by validation, so no file exists.",
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "File retention is not wired to object storage in this build. Regenerate "
            f"the {record.return_type.value} return for {record.period} and check it "
            f"against the recorded hash {record.content_hash}."
        ),
    )


# ── TDS challans ────────────────────────────────────────────────────────────


@router.get(
    "/filings/tds-challans",
    response_model=list[ChallanRead],
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def list_challans(
    user: CurrentUser,
    session: DbSession,
    financial_year: str | None = None,
    quarter: int | None = Query(default=None, ge=1, le=4),
) -> list[ChallanRead]:
    stmt = select(TdsChallan).order_by(TdsChallan.deposit_date.desc())
    if financial_year is not None:
        stmt = stmt.where(TdsChallan.financial_year == financial_year)
    if quarter is not None:
        stmt = stmt.where(TdsChallan.quarter == quarter)
    rows = (await session.scalars(tenant_scoped(stmt, TdsChallan, user))).all()
    return [ChallanRead.model_validate(row) for row in rows]


@router.post(
    "/filings/tds-challans",
    response_model=ChallanRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_challan(
    payload: ChallanCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ChallanRead:
    """Record a TDS deposit.

    This is transcribed from a bank challan. Nothing in payroll knows the BSR
    code or the challan serial, and a 24Q cannot be filed without them.
    """
    challan = TdsChallan(
        tenant_id=user.tenant_id,
        legal_entity_id=payload.legal_entity_id,
        bsr_code=payload.bsr_code,
        deposit_date=payload.deposit_date,
        challan_serial=payload.challan_serial,
        financial_year=payload.financial_year,
        quarter=payload.quarter,
        tds_amount=payload.tds_amount,
        surcharge=payload.surcharge,
        education_cess=payload.education_cess,
        interest=payload.interest,
        late_fee=payload.late_fee,
        total_amount=payload.total_amount,
        is_book_entry=payload.is_book_entry,
    )
    session.add(challan)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="tds_challan",
        entity_id=challan.id,
        summary=(
            f"Recorded TDS challan {payload.challan_serial} of {payload.total_amount} "
            f"deposited {payload.deposit_date}"
        ),
        changes={
            "bsr_code": payload.bsr_code,
            "challan_serial": payload.challan_serial,
            "total_amount": str(payload.total_amount),
        },
        request=request,
    )
    await session.commit()
    await session.refresh(challan)
    return ChallanRead.model_validate(challan)


@router.post(
    "/filings/tds-challan-mappings",
    response_model=MappingRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def map_deduction(
    payload: MappingCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> MappingRead:
    """Tie one payslip's TDS to the challan it was deposited under."""
    challan = await session.scalar(
        tenant_scoped(
            select(TdsChallan).where(TdsChallan.id == payload.challan_id), TdsChallan, user
        )
    )
    if challan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Challan not found")

    payslip = await session.scalar(
        tenant_scoped(
            select(Payslip).where(Payslip.id == payload.payslip_id), Payslip, user
        )
    )
    if payslip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payslip not found")

    deducted = Decimal(str(payslip.tds))
    if payload.tds_amount != deducted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"This payslip shows {deducted} of TDS but {payload.tds_amount} is being "
                "mapped. Every rupee deducted has to appear against a challan, and the "
                "portal reconciles the two."
            ),
        )

    mapping = TdsChallanMapping(
        tenant_id=user.tenant_id,
        challan_id=payload.challan_id,
        employee_id=payslip.employee_id,
        payslip_id=payload.payslip_id,
        tds_amount=payload.tds_amount,
        deduction_date=payload.deduction_date,
    )
    session.add(mapping)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="tds_challan_mapping",
        entity_id=mapping.id,
        summary=(
            f"Mapped {payload.tds_amount} of TDS to challan {challan.challan_serial}"
        ),
        changes={"challan_id": str(payload.challan_id), "amount": str(payload.tds_amount)},
        request=request,
    )
    await session.commit()
    await session.refresh(mapping)
    return MappingRead.model_validate(mapping)


@router.get(
    "/filings/unmapped-tds",
    dependencies=[Depends(require_roles(*FILING_ROLES))],
)
async def unmapped_tds(
    user: CurrentUser,
    session: DbSession,
    financial_year: str = Query(pattern=r"^\d{4}-\d{4}$"),
    quarter: int = Query(ge=1, le=4),
) -> list[dict[str, Any]]:
    """Deductions with no challan behind them.

    Worth its own endpoint: this is the list that has to be empty before a 24Q
    can be generated, and it is better found now than during a filing window.
    """
    from app.models.payroll import PayrollRun, PayrollRunStatus
    from app.services.payroll import statutory_service

    months = filing_service.quarter_months(quarter)
    year_start, year_end = statutory_service.financial_year_bounds(financial_year)

    rows = (
        await session.execute(
            tenant_scoped(
                select(Payslip, Employee, PayrollRun)
                .join(Employee, Payslip.employee_id == Employee.id)
                .join(PayrollRun, Payslip.run_id == PayrollRun.id)
                .where(
                    PayrollRun.period_month.in_(months),
                    PayrollRun.effective_date >= year_start,
                    PayrollRun.effective_date <= year_end,
                    PayrollRun.status.in_(
                        [PayrollRunStatus.APPROVED, PayrollRunStatus.PAID]
                    ),
                    Payslip.tds > 0,
                ),
                Payslip,
                user,
            )
        )
    ).all()

    mapped = set(
        (
            await session.scalars(
                tenant_scoped(select(TdsChallanMapping.payslip_id), TdsChallanMapping, user)
            )
        ).all()
    )

    return [
        {
            "payslip_id": str(payslip.id),
            "employee_code": employee.employee_code,
            "employee_name": employee.full_name,
            "period": f"{run.period_month:02d}/{run.period_year}",
            "tds_amount": str(payslip.tds),
        }
        for payslip, employee, run in rows
        if payslip.id not in mapped
    ]
