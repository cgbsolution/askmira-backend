"""Salary disbursement: bank profiles, batches, maker-checker (slice 11)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.crypto import encrypt_secret
from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.core.step_up import confirmation
from app.models.audit import AuditAction
from app.models.disbursement import (
    BankFileFormat,
    BankProfile,
    DisbursementBatch,
    DisbursementStatus,
    EmployeeBankAccount,
    LineStatus,
    VerificationMethod,
)
from app.models.employee import Employee
from app.models.user import UserRole
from app.services import audit
from app.services.disbursement import service as disbursement_service
from app.services.disbursement import validation
from app.services.disbursement.responses import MAX_RESPONSE_BYTES

router = APIRouter(tags=["disbursement"])

# Preparing a batch is an HR Ops task. Approving and releasing move money, so
# they are held tighter — and the service enforces that they are different
# people regardless of role.
PREPARER_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
APPROVER_ROLES = (UserRole.ADMIN,)


# ── Contracts ───────────────────────────────────────────────────────────────


class BankProfileCreate(BaseModel):
    legal_entity_id: uuid.UUID
    bank_name: str = Field(min_length=1, max_length=128)
    account_number: str = Field(min_length=9, max_length=18, pattern=r"^\d+$")
    ifsc: str = Field(min_length=11, max_length=11)
    corporate_id: str | None = Field(default=None, max_length=64)
    file_format: BankFileFormat
    format_config: dict[str, Any] = Field(default_factory=dict)


class BankProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID
    bank_name: str
    ifsc: str
    corporate_id: str | None
    file_format: BankFileFormat
    format_config: dict[str, Any]
    active: bool
    # The account number is deliberately absent. It is encrypted at rest and
    # there is no screen that needs it back — a profile list that returns
    # company bank accounts is a needless place for them to leak.


class BankAccountCreate(BaseModel):
    employee_id: uuid.UUID
    account_number: str = Field(min_length=9, max_length=18, pattern=r"^\d+$")
    ifsc: str = Field(min_length=11, max_length=11)
    account_holder_name: str = Field(min_length=1, max_length=255)
    bank_name: str | None = Field(default=None, max_length=128)
    verification_method: VerificationMethod = VerificationMethod.NONE


class BankAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    ifsc: str
    account_holder_name: str
    bank_name: str | None
    is_primary: bool
    verified_at: datetime | None
    verification_method: VerificationMethod
    employee_code: str | None = None
    employee_name: str | None = None
    # No account number, for the same reason as above.


class BatchPrepare(BaseModel):
    payroll_run_id: uuid.UUID
    bank_profile_id: uuid.UUID
    value_date: date


class BatchApprove(BaseModel):
    """The hash the approver saw.

    Required rather than read from the batch: an approver confirming a hash
    they were shown is confirming a specific file. If it was regenerated
    between their review and their click, the hashes differ and approval
    refuses.
    """

    file_hash: str = Field(min_length=64, max_length=64)


class BatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    payroll_run_id: uuid.UUID
    bank_profile_id: uuid.UUID
    supplements_batch_id: uuid.UUID | None
    status: DisbursementStatus
    total_amount: Decimal
    employee_count: int
    value_date: date | None
    file_name: str | None
    file_hash: str | None
    prepared_by_user_id: uuid.UUID | None
    prepared_by_name: str | None = None
    prepared_at: datetime | None
    approved_by_user_id: uuid.UUID | None
    approved_by_name: str | None = None
    approved_at: datetime | None
    released_at: datetime | None
    validation_exceptions: list[dict[str, Any]]
    notes: str | None


class LineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    amount: Decimal
    ifsc: str
    beneficiary_name: str
    status: LineStatus
    failure_reason: str | None
    bank_reference: str | None
    employee_code: str | None = None
    employee_name: str | None = None


class BankResponse(BaseModel):
    account_number: str
    status: str
    reason: str | None = None
    reference: str | None = None


class ReconcileRequest(BaseModel):
    responses: list[BankResponse] = Field(min_length=1)


class SupplementaryRequest(BaseModel):
    value_date: date


def _batch_read(
    batch: DisbursementBatch, prepared_by: str | None = None, approved_by: str | None = None
) -> BatchRead:
    read = BatchRead.model_validate(batch)
    read.prepared_by_name = prepared_by
    read.approved_by_name = approved_by
    return read


async def _named(session, user, batch: DisbursementBatch) -> BatchRead:
    _, prepared_by, approved_by = await disbursement_service.get_batch(session, user, batch.id)
    return _batch_read(batch, prepared_by, approved_by)


# ── Bank profiles ───────────────────────────────────────────────────────────


@router.get(
    "/disbursement/bank-profiles",
    response_model=list[BankProfileRead],
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def list_profiles(user: CurrentUser, session: DbSession) -> list[BankProfileRead]:
    rows = (
        await session.scalars(
            tenant_scoped(select(BankProfile).order_by(BankProfile.bank_name), BankProfile, user)
        )
    ).all()
    return [BankProfileRead.model_validate(row) for row in rows]


@router.post(
    "/disbursement/bank-profiles",
    response_model=BankProfileRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*APPROVER_ROLES))],
)
async def create_profile(
    payload: BankProfileCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> BankProfileRead:
    problem = validation.validate_ifsc(payload.ifsc)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)

    profile = BankProfile(
        tenant_id=user.tenant_id,
        legal_entity_id=payload.legal_entity_id,
        bank_name=payload.bank_name,
        account_number=encrypt_secret(payload.account_number, user.tenant_id),
        ifsc=payload.ifsc.upper(),
        corporate_id=payload.corporate_id,
        file_format=payload.file_format,
        format_config=payload.format_config,
    )
    session.add(profile)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="bank_profile",
        entity_id=profile.id,
        summary=f"Added the {payload.bank_name} disbursement profile",
        changes={"bank_name": payload.bank_name, "file_format": payload.file_format.value},
        request=request,
    )
    await session.commit()
    await session.refresh(profile)
    return BankProfileRead.model_validate(profile)


# ── Employee bank accounts ──────────────────────────────────────────────────


@router.get(
    "/disbursement/bank-accounts",
    response_model=list[BankAccountRead],
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def list_accounts(
    user: CurrentUser, session: DbSession, employee_id: uuid.UUID | None = None
) -> list[BankAccountRead]:
    rows = await disbursement_service.bank_accounts(session, user, employee_id=employee_id)
    out = []
    for account, label in rows:
        read = BankAccountRead.model_validate(account)
        if label:
            read.employee_code, read.employee_name = label
        out.append(read)
    return out


@router.post(
    "/disbursement/bank-accounts",
    response_model=BankAccountRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def create_account(
    payload: BankAccountCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> BankAccountRead:
    """Record where an employee's salary goes.

    A new primary account supersedes the old one rather than editing it: an
    account change must not retroactively alter what a past batch says was
    paid, and a bounced credit is investigated against the details actually
    sent.
    """
    problem = validation.validate_ifsc(payload.ifsc)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == payload.employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    score = validation.name_similarity(employee.full_name, payload.account_holder_name)

    existing = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeBankAccount).where(
                    EmployeeBankAccount.employee_id == payload.employee_id,
                    EmployeeBankAccount.is_primary.is_(True),
                ),
                EmployeeBankAccount,
                user,
            )
        )
    ).all()
    for row in existing:
        row.is_primary = False
        row.active = False

    account = EmployeeBankAccount(
        tenant_id=user.tenant_id,
        employee_id=payload.employee_id,
        account_number=encrypt_secret(payload.account_number, user.tenant_id),
        ifsc=payload.ifsc.upper(),
        account_holder_name=payload.account_holder_name,
        bank_name=payload.bank_name,
        is_primary=True,
        verification_method=payload.verification_method,
        verified_at=(
            datetime.now(tz=None).astimezone()
            if payload.verification_method is not VerificationMethod.NONE
            else None
        ),
    )
    session.add(account)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="employee_bank_account",
        entity_id=account.id,
        summary=(f"Recorded a bank account for {employee.employee_code} (name similarity {score})"),
        changes={
            "ifsc": account.ifsc,
            "verification_method": payload.verification_method.value,
            "name_similarity": str(score),
            "superseded": len(existing),
        },
        request=request,
    )
    await session.commit()
    await session.refresh(account)
    read = BankAccountRead.model_validate(account)
    read.employee_code, read.employee_name = employee.employee_code, employee.full_name
    return read


# ── Batches ─────────────────────────────────────────────────────────────────


@router.post(
    "/disbursement/batches",
    response_model=BatchRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def prepare(
    payload: BatchPrepare,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> BatchRead:
    try:
        batch = await disbursement_service.prepare_batch(
            session,
            user,
            payroll_run_id=payload.payroll_run_id,
            bank_profile_id=payload.bank_profile_id,
            value_date=payload.value_date,
            request=request,
        )
    except disbursement_service.BatchBlockedError as exc:
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


@router.get(
    "/disbursement/batches",
    response_model=list[BatchRead],
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def list_batches(
    user: CurrentUser,
    session: DbSession,
    payroll_run_id: uuid.UUID | None = None,
) -> list[BatchRead]:
    rows = await disbursement_service.list_batches(session, user, payroll_run_id=payroll_run_id)
    return [_batch_read(batch, prepared, approved) for batch, prepared, approved in rows]


@router.get(
    "/disbursement/batches/{batch_id}",
    response_model=BatchRead,
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def get_batch(batch_id: uuid.UUID, user: CurrentUser, session: DbSession) -> BatchRead:
    batch, prepared, approved = await disbursement_service.get_batch(session, user, batch_id)
    return _batch_read(batch, prepared, approved)


@router.get(
    "/disbursement/batches/{batch_id}/lines",
    response_model=list[LineRead],
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def list_lines(batch_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[LineRead]:
    rows = await disbursement_service.batch_lines(session, user, batch_id)
    out = []
    for line, label in rows:
        read = LineRead.model_validate(line)
        if label:
            read.employee_code, read.employee_name = label
        out.append(read)
    return out


@router.post(
    "/disbursement/batches/{batch_id}/approve",
    response_model=BatchRead,
    dependencies=[Depends(require_roles(*APPROVER_ROLES))],
)
async def approve(
    batch_id: uuid.UUID,
    payload: BatchApprove,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> BatchRead:
    """Second signature. The service refuses if it is the first one again."""
    batch = await disbursement_service.approve_batch(
        session, user, batch_id=batch_id, file_hash=payload.file_hash, request=request
    )
    await session.commit()
    await session.refresh(batch)
    return await _named(session, user, batch)


@router.post(
    "/disbursement/batches/{batch_id}/regenerate",
    response_model=BatchRead,
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def regenerate(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> BatchRead:
    """Rebuild a batch. Any approval on the old one no longer applies."""
    try:
        batch = await disbursement_service.regenerate_batch(
            session, user, batch_id=batch_id, request=request
        )
    except disbursement_service.BatchBlockedError as exc:
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "exceptions": exc.result.as_list()},
        ) from exc
    await session.commit()
    await session.refresh(batch)
    return await _named(session, user, batch)


@router.post(
    "/disbursement/batches/{batch_id}/release",
    response_model=BatchRead,
    dependencies=[Depends(require_roles(*APPROVER_ROLES))],
)
async def release(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> BatchRead:
    batch = await disbursement_service.release_batch(
        session,
        user,
        batch_id=batch_id,
        confirm=confirmation(session, user, "disbursement.release"),
        request=request,
    )
    await session.commit()
    await session.refresh(batch)
    return await _named(session, user, batch)


@router.get(
    "/disbursement/batches/{batch_id}/file",
    dependencies=[Depends(require_roles(*APPROVER_ROLES))],
)
async def download(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> Response:
    """Hand out the payment file.

    Restricted to the approver roles, every download recorded, and the window
    is short. The file is every employee's bank account and salary; it is never
    emailed and it does not sit behind a link that still works next month.
    """
    content, batch = await disbursement_service.download_file(
        session, user, batch_id=batch_id, request=request
    )
    await session.commit()
    return Response(
        content=content,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{batch.file_name}"',
            "Cache-Control": "no-store",
        },
    )


# ── Reconciliation ──────────────────────────────────────────────────────────


@router.post(
    "/disbursement/batches/{batch_id}/reconcile",
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def reconcile(
    batch_id: uuid.UUID,
    payload: ReconcileRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Apply the bank's response file.

    The failures come back in the response so HR Ops can act on them now — a
    failed salary credit that nobody notices until the employee calls is a
    serious incident.
    """
    outcome = await disbursement_service.reconcile(
        session,
        user,
        batch_id=batch_id,
        responses=[item.model_dump() for item in payload.responses],
        request=request,
    )
    await session.commit()
    return outcome


@router.post(
    "/disbursement/batches/{batch_id}/reconcile/import",
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def reconcile_import(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Import the bank's CSV response (account_number, status, reason, reference).

    Failed credits come back in the response, and HR Ops are notified in the
    same transaction.
    """
    outcome = await disbursement_service.import_bank_response(
        session,
        user,
        batch_id=batch_id,
        data=await file.read(MAX_RESPONSE_BYTES + 1),
        request=request,
    )
    await session.commit()
    return outcome


@router.post(
    "/disbursement/batches/{batch_id}/supplementary",
    response_model=BatchRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PREPARER_ROLES))],
)
async def supplementary(
    batch_id: uuid.UUID,
    payload: SupplementaryRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> BatchRead:
    """Reissue only the failed credits, not the whole run."""
    batch = await disbursement_service.supplementary_batch(
        session, user, batch_id=batch_id, value_date=payload.value_date, request=request
    )
    await session.commit()
    await session.refresh(batch)
    return await _named(session, user, batch)
