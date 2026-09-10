"""Labour Welfare Fund, statutory bonus, PF elections and registers (slice 09)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmployeeWorkLocation
from app.models.payroll import (
    BonusComputation,
    BonusStatus,
    EmployeePfElection,
    EsiCoveragePeriod,
)
from app.models.user import UserRole
from app.services import audit
from app.services.payroll import registers as register_formats
from app.services.payroll import statutory_service

router = APIRouter(tags=["statutory"])

PAYROLL_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class PfElectionUpsert(BaseModel):
    """An employee's PF choices.

    The two are unrelated and are validated as such: electing PF on actual
    wages does not imply VPF, and VPF does not imply the ceiling election.
    """

    employee_id: uuid.UUID
    effective_from: date
    pf_on_actual_wage: bool = False
    vpf_percentage: Decimal | None = Field(default=None, ge=0, le=100)
    vpf_fixed_amount: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def one_vpf_form(self) -> PfElectionUpsert:
        if self.vpf_percentage is not None and self.vpf_fixed_amount is not None:
            raise ValueError(
                "A VPF election is either a percentage of wages or a fixed amount, "
                "not both."
            )
        return self


class PfElectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    effective_from: date
    effective_to: date | None
    pf_on_actual_wage: bool
    vpf_percentage: Decimal | None
    vpf_fixed_amount: Decimal | None


class WorkLocationCreate(BaseModel):
    employee_id: uuid.UUID
    location_id: uuid.UUID
    effective_from: date
    reason: str | None = Field(default=None, max_length=255)


class WorkLocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    location_id: uuid.UUID
    effective_from: date
    effective_to: date | None
    reason: str | None


class BonusComputeRequest(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{4}$", examples=["2025-2026"])
    legal_entity_id: uuid.UUID
    jurisdiction: str = Field(default="IN", max_length=8)
    # Above the statutory minimum, an employer may declare a higher rate. It is
    # a decision, so it is passed in rather than inferred from profits.
    percentage: Decimal | None = Field(default=None, ge=0, le=100)


class BonusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    financial_year: str
    is_eligible: bool
    ineligibility_reason: str | None
    eligible_wage: Decimal
    days_worked: Decimal
    total_working_days: Decimal
    percentage: Decimal
    amount: Decimal
    status: BonusStatus
    paid_on: date | None
    paid_in_run_id: uuid.UUID | None
    working: dict[str, Any]


class BonusDisburseRequest(BaseModel):
    """Paying an approved bonus.

    `run_id` is optional and usually left out — bonus is normally its own
    payment ahead of a festival rather than a line inside a monthly run.
    """

    financial_year: str = Field(pattern=r"^\d{4}-\d{4}$")
    paid_on: date
    run_id: uuid.UUID | None = None


class EsiCoverageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    period_label: str
    period_start_month: int
    period_end_month: int
    was_contributing_at_start: bool
    opening_wage: Decimal
    ceiling_crossed_on: date | None


class RegisterFormatRead(BaseModel):
    code: str
    title: str
    prescribed_by: str
    columns: list[str]


# ── PF elections ────────────────────────────────────────────────────────────


@router.get(
    "/statutory/pf-elections",
    response_model=list[PfElectionRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_pf_elections(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
) -> list[PfElectionRead]:
    stmt = select(EmployeePfElection).order_by(EmployeePfElection.effective_from.desc())
    if employee_id is not None:
        stmt = stmt.where(EmployeePfElection.employee_id == employee_id)
    rows = (
        await session.scalars(tenant_scoped(stmt, EmployeePfElection, user))
    ).all()
    return [PfElectionRead.model_validate(row) for row in rows]


@router.post(
    "/statutory/pf-elections",
    response_model=PfElectionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def create_pf_election(
    payload: PfElectionUpsert,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> PfElectionRead:
    """Record an election, closing the previous one.

    A new election never edits the old row: September's payslip was computed
    against what was true in September, and a change made in October must not
    move it.
    """
    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == payload.employee_id), Employee, user
        )
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    current = await session.scalar(
        tenant_scoped(
            select(EmployeePfElection)
            .where(
                EmployeePfElection.employee_id == payload.employee_id,
                EmployeePfElection.effective_to.is_(None),
            )
            .order_by(EmployeePfElection.effective_from.desc()),
            EmployeePfElection,
            user,
        )
    )
    if current is not None:
        if current.effective_from >= payload.effective_from:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"An election already runs from {current.effective_from}. A new one "
                    "has to start after it."
                ),
            )
        current.effective_to = payload.effective_from - timedelta(days=1)

    row = EmployeePfElection(
        tenant_id=user.tenant_id,
        employee_id=payload.employee_id,
        effective_from=payload.effective_from,
        pf_on_actual_wage=payload.pf_on_actual_wage,
        vpf_percentage=payload.vpf_percentage,
        vpf_fixed_amount=payload.vpf_fixed_amount,
        declared_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="employee_pf_election",
        entity_id=row.id,
        summary=(
            f"Recorded PF election for {employee.employee_code} from "
            f"{payload.effective_from}"
        ),
        changes={
            "pf_on_actual_wage": payload.pf_on_actual_wage,
            "vpf_percentage": str(payload.vpf_percentage) if payload.vpf_percentage else None,
            "vpf_fixed_amount": (
                str(payload.vpf_fixed_amount) if payload.vpf_fixed_amount else None
            ),
        },
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return PfElectionRead.model_validate(row)


# ── Work locations, for multi-state levies ──────────────────────────────────


@router.get(
    "/statutory/work-locations",
    response_model=list[WorkLocationRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_work_locations(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
) -> list[WorkLocationRead]:
    stmt = select(EmployeeWorkLocation).order_by(EmployeeWorkLocation.effective_from)
    if employee_id is not None:
        stmt = stmt.where(EmployeeWorkLocation.employee_id == employee_id)
    rows = (await session.scalars(tenant_scoped(stmt, EmployeeWorkLocation, user))).all()
    return [WorkLocationRead.model_validate(row) for row in rows]


@router.post(
    "/statutory/work-locations",
    response_model=WorkLocationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def create_work_location(
    payload: WorkLocationCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> WorkLocationRead:
    """Post an employee to a location from a date.

    Recording this is what lets payroll notice a mid-period state change
    instead of applying the destination state to the whole month.
    """
    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == payload.employee_id), Employee, user
        )
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    current = await session.scalar(
        tenant_scoped(
            select(EmployeeWorkLocation)
            .where(
                EmployeeWorkLocation.employee_id == payload.employee_id,
                EmployeeWorkLocation.effective_to.is_(None),
            )
            .order_by(EmployeeWorkLocation.effective_from.desc()),
            EmployeeWorkLocation,
            user,
        )
    )
    if current is not None:
        if current.effective_from >= payload.effective_from:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"This employee already has a posting from {current.effective_from}. "
                    "A new one has to start after it."
                ),
            )
        current.effective_to = payload.effective_from - timedelta(days=1)

    row = EmployeeWorkLocation(
        tenant_id=user.tenant_id,
        employee_id=payload.employee_id,
        location_id=payload.location_id,
        effective_from=payload.effective_from,
        reason=payload.reason,
    )
    session.add(row)
    # The directory answer follows the posting, so a search for "who is in
    # Pune" agrees with what payroll used.
    employee.location_id = payload.location_id
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="employee_work_location",
        entity_id=row.id,
        summary=(
            f"Posted {employee.employee_code} to a new work location from "
            f"{payload.effective_from}"
        ),
        changes={"location_id": str(payload.location_id), "reason": payload.reason},
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return WorkLocationRead.model_validate(row)


# ── Statutory bonus ─────────────────────────────────────────────────────────


@router.post(
    "/statutory/bonus/compute",
    response_model=list[BonusRead],
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def compute_bonus(
    payload: BonusComputeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> list[BonusRead]:
    rows = await statutory_service.compute_bonus_for_year(
        session,
        user,
        financial_year=payload.financial_year,
        legal_entity_id=payload.legal_entity_id,
        jurisdiction=payload.jurisdiction,
        percentage=payload.percentage,
        request=request,
    )
    await session.commit()
    return [BonusRead.model_validate(row) for row in rows]


@router.get(
    "/statutory/bonus",
    response_model=list[BonusRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_bonus(
    user: CurrentUser,
    session: DbSession,
    financial_year: str = Query(pattern=r"^\d{4}-\d{4}$"),
    eligible_only: bool = False,
) -> list[BonusRead]:
    stmt = select(BonusComputation).where(
        BonusComputation.financial_year == financial_year
    )
    if eligible_only:
        stmt = stmt.where(BonusComputation.is_eligible.is_(True))
    rows = (await session.scalars(tenant_scoped(stmt, BonusComputation, user))).all()
    return [BonusRead.model_validate(row) for row in rows]


@router.post(
    "/statutory/bonus/approve",
    response_model=list[BonusRead],
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def approve_bonus(
    payload: BonusComputeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> list[BonusRead]:
    """Sign off a year's computation, separately from paying it."""
    rows = await statutory_service.approve_bonus(
        session, user, financial_year=payload.financial_year, request=request
    )
    await session.commit()
    return [BonusRead.model_validate(row) for row in rows]


@router.post(
    "/statutory/bonus/disburse",
    response_model=list[BonusRead],
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def disburse_bonus(
    payload: BonusDisburseRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> list[BonusRead]:
    rows = await statutory_service.disburse_bonus(
        session,
        user,
        financial_year=payload.financial_year,
        paid_on=payload.paid_on,
        run_id=payload.run_id,
        request=request,
    )
    await session.commit()
    return [BonusRead.model_validate(row) for row in rows]


# ── ESI coverage ────────────────────────────────────────────────────────────


@router.get(
    "/statutory/esi-coverage",
    response_model=list[EsiCoverageRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_esi_coverage(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
) -> list[EsiCoverageRead]:
    """What was true when each contribution period opened.

    Worth surfacing: an employee asking why ESI is still being deducted after
    a raise gets a straight answer from this, and so does an auditor.
    """
    stmt = select(EsiCoveragePeriod).order_by(EsiCoveragePeriod.period_label.desc())
    if employee_id is not None:
        stmt = stmt.where(EsiCoveragePeriod.employee_id == employee_id)
    rows = (await session.scalars(tenant_scoped(stmt, EsiCoveragePeriod, user))).all()
    return [EsiCoverageRead.model_validate(row) for row in rows]


# ── Registers ───────────────────────────────────────────────────────────────


@router.get(
    "/statutory/registers",
    response_model=list[RegisterFormatRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_register_formats() -> list[RegisterFormatRead]:
    return [
        RegisterFormatRead(
            code=spec.code,
            title=spec.title,
            prescribed_by=spec.prescribed_by,
            columns=[column.heading for column in spec.columns],
        )
        for spec in register_formats.REGISTERS.values()
    ]


@router.get(
    "/statutory/registers/{code}",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def get_register(
    code: str,
    user: CurrentUser,
    session: DbSession,
    run_id: uuid.UUID | None = None,
    financial_year: str | None = None,
    export_format: str = Query(default="json", pattern="^(json|csv)$"),
) -> Any:
    rendered = await statutory_service.build_register(
        session, user, code=code, run_id=run_id, financial_year=financial_year
    )
    if export_format == "csv":
        return Response(
            content=register_formats.to_csv(rendered),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{rendered.spec.code.lower()}.csv"'
            },
        )
    return rendered.as_dict()
