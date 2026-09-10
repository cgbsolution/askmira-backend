"""Income tax declaration and proof endpoints (slice 07)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.tax import (
    DeclarationStatus,
    DeclarationWindow,
    PreviousEmployerIncome,
    ProofStatus,
    TaxComputationSnapshot,
    TaxDeclaration,
    TaxProof,
    TaxRegime,
    WindowType,
)
from app.models.user import UserRole
from app.services.tax import service as tax_service
from app.services.tax.service import TaxWorkflowError

router = APIRouter(prefix="/tax", tags=["tax"])

PAYROLL_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)

# Money crosses the wire as a string. A JSON number is a float on the other
# side, and a tax figure that drifts in the browser is still a wrong figure.
Money = Decimal


def _workflow_error(exc: TaxWorkflowError) -> HTTPException:
    """Workflow errors carry a message the employee can act on.

    Swallowing them into a generic 400 makes the declaration screen unusable —
    "your landlord's PAN is required above ₹1,00,000" is the whole value.
    """
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


# ── Contracts ───────────────────────────────────────────────────────────────


class WindowUpsert(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    window_type: WindowType
    opens_at: date
    closes_at: date
    allow_late_submission: bool = False


class WindowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    financial_year: str
    window_type: WindowType
    opens_at: date
    closes_at: date
    allow_late_submission: bool


class RegimeElectionRequest(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    regime: TaxRegime
    employee_id: uuid.UUID | None = None


class RegimeElectionResponse(BaseModel):
    financial_year: str
    regime: TaxRegime
    switch_count: int
    full_year_recomputed: bool
    months_recomputed: int = 0


class DeclarationUpsert(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    section: str = Field(min_length=2, max_length=16)
    sub_category: str = Field(default="", max_length=64)
    declared_amount: Decimal = Field(ge=0)
    description: str | None = Field(default=None, max_length=255)
    employee_id: uuid.UUID | None = None


class DeclarationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    financial_year: str
    section: str
    sub_category: str
    description: str | None
    declared_amount: Decimal
    status: DeclarationStatus


class ProofUpload(BaseModel):
    declaration_id: uuid.UUID
    proof_amount: Decimal = Field(gt=0)
    document_storage_key: str | None = None
    document_name: str | None = None


class ProofDecision(BaseModel):
    decision: ProofStatus
    accepted_amount: Decimal = Field(default=Decimal(0), ge=0)
    remarks: str = Field(default="", max_length=2000)


class ProofRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    declaration_id: uuid.UUID
    proof_amount: Decimal
    accepted_amount: Decimal
    status: ProofStatus
    verifier_remarks: str | None
    verified_at: datetime | None
    document_name: str | None


class HouseRentUpsert(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    monthly_rent: list[dict[str, Any]] = Field(min_length=1, max_length=12)
    landlord_name: str | None = Field(default=None, max_length=255)
    landlord_pan: str | None = Field(default=None, max_length=10)
    property_address: str | None = None
    is_metro: bool = False
    employee_id: uuid.UUID | None = None


class PreviousEmployerUpsert(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")
    employer_name: str | None = Field(default=None, max_length=255)
    gross_salary: Decimal = Field(ge=0)
    tds_deducted: Decimal = Field(default=Decimal(0), ge=0)
    pf_deducted: Decimal = Field(default=Decimal(0), ge=0)
    professional_tax: Decimal = Field(default=Decimal(0), ge=0)
    exempt_allowances: Decimal = Field(default=Decimal(0), ge=0)
    document_storage_key: str | None = None


class SnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    financial_year: str
    regime: TaxRegime
    computed_for_month: int
    gross_annual_income: Decimal
    taxable_income: Decimal
    annual_tax_liability: Decimal
    monthly_tds: Decimal
    working: dict


def _resolve_employee(user, supplied: uuid.UUID | None) -> uuid.UUID:
    """Whose declaration this touches.

    An employee acts on their own; payroll may act on anyone's.
    """
    if supplied is None:
        if user.employee_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your login is not linked to an employee record",
            )
        return user.employee_id

    if supplied != user.employee_id and user.role not in PAYROLL_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage your own tax declarations",
        )
    return supplied


# ── Windows ─────────────────────────────────────────────────────────────────


@router.get("/windows", response_model=list[WindowRead])
async def list_windows(
    user: CurrentUser, session: DbSession, financial_year: str | None = None
) -> list[WindowRead]:
    stmt = tenant_scoped(
        select(DeclarationWindow).order_by(DeclarationWindow.opens_at), DeclarationWindow, user
    )
    if financial_year:
        stmt = stmt.where(DeclarationWindow.financial_year == financial_year)
    return [WindowRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


@router.put(
    "/windows",
    response_model=WindowRead,
    dependencies=[Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN))],
)
async def upsert_window(
    payload: WindowUpsert, user: CurrentUser, session: DbSession
) -> WindowRead:
    if payload.closes_at < payload.opens_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A window cannot close before it opens",
        )

    window = await tax_service.window_for(
        session, user, payload.financial_year, payload.window_type
    )
    if window is None:
        window = DeclarationWindow(
            tenant_id=user.tenant_id,
            financial_year=payload.financial_year,
            window_type=payload.window_type,
        )
        session.add(window)

    window.opens_at = payload.opens_at
    window.closes_at = payload.closes_at
    window.allow_late_submission = payload.allow_late_submission

    await session.commit()
    await session.refresh(window)
    return WindowRead.model_validate(window)


# ── Regime ──────────────────────────────────────────────────────────────────


@router.post("/regime", response_model=RegimeElectionResponse)
async def elect_regime(
    payload: RegimeElectionRequest, user: CurrentUser, session: DbSession, request: Request
) -> RegimeElectionResponse:
    """Elect or switch a regime.

    A switch after payroll has already run recomputes the **whole** financial
    year. Applying it forward only would leave months already run computed
    under the old regime, and the year would not reconcile at filing.
    """
    employee_id = _resolve_employee(user, payload.employee_id)

    try:
        election, needs_recompute = await tax_service.elect_regime(
            session,
            user,
            employee_id=employee_id,
            financial_year=payload.financial_year,
            regime=payload.regime,
            request=request,
        )
        await session.commit()

        recomputed = 0
        if needs_recompute:
            snapshots = await tax_service.recompute_financial_year(
                session, user, employee_id, payload.financial_year, request=request
            )
            recomputed = len(snapshots)
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc

    return RegimeElectionResponse(
        financial_year=election.financial_year,
        regime=election.regime,
        switch_count=election.switch_count,
        full_year_recomputed=needs_recompute,
        months_recomputed=recomputed,
    )


@router.get("/regime/compare")
async def compare_regimes(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Project both regimes on the employee's own numbers.

    Most employees cannot make this choice unaided, and every one who guesses
    wrong becomes a support ticket in March.
    """
    target = _resolve_employee(user, employee_id)
    try:
        return await tax_service.regime_comparison(session, user, target, financial_year)
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc


# ── Declarations ────────────────────────────────────────────────────────────


@router.get("/declarations", response_model=list[DeclarationRead])
async def list_declarations(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> list[DeclarationRead]:
    target = _resolve_employee(user, employee_id)
    stmt = tenant_scoped(
        select(TaxDeclaration)
        .where(
            TaxDeclaration.employee_id == target,
            TaxDeclaration.financial_year == financial_year,
        )
        .order_by(TaxDeclaration.section, TaxDeclaration.sub_category),
        TaxDeclaration,
        user,
    )
    return [DeclarationRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


@router.put("/declarations", response_model=DeclarationRead)
async def upsert_declaration(
    payload: DeclarationUpsert, user: CurrentUser, session: DbSession, request: Request
) -> DeclarationRead:
    target = _resolve_employee(user, payload.employee_id)
    try:
        declaration = await tax_service.upsert_declaration(
            session,
            user,
            employee_id=target,
            financial_year=payload.financial_year,
            section=payload.section,
            sub_category=payload.sub_category,
            declared_amount=payload.declared_amount,
            description=payload.description,
            request=request,
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc

    await session.commit()
    await session.refresh(declaration)
    return DeclarationRead.model_validate(declaration)


@router.post("/declarations/{declaration_id}/submit", response_model=DeclarationRead)
async def submit_declaration(
    declaration_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> DeclarationRead:
    declaration = await session.scalar(
        tenant_scoped(
            select(TaxDeclaration).where(TaxDeclaration.id == declaration_id),
            TaxDeclaration,
            user,
        )
    )
    if declaration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Declaration not found"
        )
    if declaration.status is DeclarationStatus.LOCKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This declaration is locked for the year and cannot be changed",
        )

    from datetime import UTC

    declaration.status = DeclarationStatus.SUBMITTED
    declaration.submitted_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(declaration)
    return DeclarationRead.model_validate(declaration)


@router.put("/house-rent")
async def upsert_house_rent(
    payload: HouseRentUpsert, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Record rent paid.

    Landlord PAN is enforced above a configured annual threshold, at submission
    rather than at assessment — an employee who finds out in March has lost the
    chance to obtain it.
    """
    target = _resolve_employee(user, payload.employee_id)
    try:
        record = await tax_service.submit_house_rent(
            session,
            user,
            employee_id=target,
            financial_year=payload.financial_year,
            monthly_rent=payload.monthly_rent,
            landlord_name=payload.landlord_name,
            landlord_pan=payload.landlord_pan,
            property_address=payload.property_address,
            is_metro=payload.is_metro,
            request=request,
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc

    await session.commit()
    await session.refresh(record)
    return {
        "id": str(record.id),
        "annual_rent": str(record.annual_rent),
        "is_metro": record.is_metro,
        "landlord_pan": record.landlord_pan,
    }


@router.put("/previous-employer")
async def upsert_previous_employer(
    payload: PreviousEmployerUpsert, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Income from a previous employer this financial year.

    Without it a mid-year joiner is taxed as though they earned nothing before
    joining, which under-deducts all year and lands them with a liability at
    filing.
    """
    employee_id = _resolve_employee(user, None)

    record = await session.scalar(
        tenant_scoped(
            select(PreviousEmployerIncome).where(
                PreviousEmployerIncome.employee_id == employee_id,
                PreviousEmployerIncome.financial_year == payload.financial_year,
            ),
            PreviousEmployerIncome,
            user,
        )
    )
    if record is None:
        record = PreviousEmployerIncome(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            financial_year=payload.financial_year,
        )
        session.add(record)

    for field_name, value in payload.model_dump(exclude={"financial_year"}).items():
        setattr(record, field_name, value)

    await session.commit()
    await session.refresh(record)
    return {"id": str(record.id), "gross_salary": str(record.gross_salary)}


# ── Proofs ──────────────────────────────────────────────────────────────────


@router.post("/proofs", response_model=ProofRead, status_code=status.HTTP_201_CREATED)
async def upload_proof(
    payload: ProofUpload, user: CurrentUser, session: DbSession
) -> ProofRead:
    declaration = await session.scalar(
        tenant_scoped(
            select(TaxDeclaration).where(TaxDeclaration.id == payload.declaration_id),
            TaxDeclaration,
            user,
        )
    )
    if declaration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Declaration not found"
        )

    try:
        await tax_service.assert_window_open(
            session, user, declaration.financial_year, WindowType.PROOF
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc

    proof = TaxProof(
        tenant_id=user.tenant_id,
        declaration_id=declaration.id,
        proof_amount=payload.proof_amount,
        document_storage_key=payload.document_storage_key,
        document_name=payload.document_name,
    )
    session.add(proof)
    await session.commit()
    await session.refresh(proof)
    return ProofRead.model_validate(proof)


@router.get(
    "/proofs/queue",
    response_model=list[ProofRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def verification_queue(
    user: CurrentUser, session: DbSession, financial_year: str | None = None
) -> list[ProofRead]:
    """Everything awaiting verification.

    The payroll team's working queue in January. Oldest first, so nothing sits
    at the bottom for a month.
    """
    stmt = tenant_scoped(
        select(TaxProof)
        .where(TaxProof.status == ProofStatus.PENDING)
        .order_by(TaxProof.created_at),
        TaxProof,
        user,
    )
    if financial_year:
        stmt = stmt.join(TaxDeclaration).where(
            TaxDeclaration.financial_year == financial_year
        )
    return [ProofRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


@router.post(
    "/proofs/{proof_id}/verify",
    response_model=ProofRead,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def verify_proof(
    proof_id: uuid.UUID,
    payload: ProofDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ProofRead:
    """Record a verification decision.

    Partial acceptance is a first-class outcome. A remark is required on
    anything but a clean full acceptance, and every decision writes an audit
    entry.
    """
    try:
        proof = await tax_service.verify_proof(
            session,
            user,
            proof_id,
            status_decision=payload.decision,
            accepted_amount=payload.accepted_amount,
            remarks=payload.remarks,
            request=request,
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc

    return ProofRead.model_validate(proof)


@router.post(
    "/proof-window/close",
    dependencies=[Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN))],
)
async def close_proof_window(
    user: CurrentUser, session: DbSession, financial_year: str, request: Request
) -> dict[str, int]:
    """Lock declarations for the year.

    Unproven amounts fall to what was accepted, which usually spikes March TDS.
    That is correct — the reminders sent while the window was open are what
    stop it being a surprise.
    """
    locked = await tax_service.close_proof_window(
        session, user, financial_year, request=request
    )
    return {"declarations_locked": locked}


# ── Projection ──────────────────────────────────────────────────────────────


@router.get("/projection")
async def projection(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """The live preview an employee sees while editing declarations.

    Uses declared amounts, not proved ones: before the proof window closes, the
    impact of what they intend to claim is the useful answer.
    """
    target = _resolve_employee(user, employee_id)
    try:
        computation = await tax_service.live_preview(
            session, user, target, financial_year
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc
    return computation.as_working()


@router.get("/snapshots", response_model=list[SnapshotRead])
async def list_snapshots(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> list[SnapshotRead]:
    """Stored projections, newest first.

    Kept so an employee asking "why did my TDS jump" has the previous
    projection to compare against.
    """
    target = _resolve_employee(user, employee_id)
    stmt = tenant_scoped(
        select(TaxComputationSnapshot)
        .where(
            TaxComputationSnapshot.employee_id == target,
            TaxComputationSnapshot.financial_year == financial_year,
        )
        .order_by(TaxComputationSnapshot.created_at.desc()),
        TaxComputationSnapshot,
        user,
    )
    return [SnapshotRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


@router.get("/form-12bb")
async def form_12bb_preview(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> dict[str, str]:
    """The claims table that goes into Form 12BB.

    Issuing the form itself goes through the letter engine, so it inherits
    hashing, versioning and e-signature.
    """
    from app.services.tax.form_12bb import build_claims_table

    target = _resolve_employee(user, employee_id)
    return {
        "claims_table": await build_claims_table(session, user, target, financial_year)
    }
