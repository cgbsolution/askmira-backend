"""Income tax declaration and proof endpoints (slice 07).

Thin: parse, gate, delegate to `app.services.tax.service`, serialise. Whose
record a request touches is decided through `app.core.permissions` — the
caller's own unless payroll says otherwise, and never taken from a body on
trust.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.audit import AuditAction
from app.models.tax import ProofStatus, WindowType
from app.models.user import UserRole
from app.schemas.payroll_ops import IssuedDocumentRead
from app.schemas.tax import (
    DeclarationRead,
    DeclarationUpsert,
    Form12BBIssue,
    HouseRentRead,
    HouseRentUpsert,
    PreviousEmployerRead,
    PreviousEmployerUpsert,
    ProofDecision,
    ProofQueueItem,
    ProofRead,
    RegimeElectionRequest,
    RegimeElectionResponse,
    RegimeRead,
    ReminderRunRead,
    SnapshotRead,
    WindowRead,
    WindowUpsert,
)
from app.services import audit
from app.services.attendance.service import local_today
from app.services.filings import support
from app.services.tax import form16, form16_part_a, reminders
from app.services.tax import service as tax_service
from app.services.tax.service import TaxWorkflowError

router = APIRouter(prefix="/tax", tags=["tax"])

PAYROLL_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


def _workflow_error(exc: TaxWorkflowError) -> HTTPException:
    """Workflow errors carry a message the employee can act on.

    Swallowing them into a generic 400 makes the declaration screen unusable —
    "your landlord's PAN is required above ₹1,00,000" is the whole value.
    """
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def _resolve_employee(user, supplied: uuid.UUID | None) -> uuid.UUID:
    """Whose declaration record this touches: the caller's own, unless HR."""
    return permissions.resolve_employee(
        user, supplied, detail="You can only manage your own tax declarations"
    )


async def _read_upload(file: UploadFile) -> bytes:
    """At most one byte past the limit, so an oversized file is refused for its
    size without being read into memory in full."""
    return await file.read(support.MAX_UPLOAD_BYTES + 1)


# ── Windows ─────────────────────────────────────────────────────────────────


@router.get("/windows", response_model=list[WindowRead])
async def list_windows(
    user: CurrentUser, session: DbSession, financial_year: str | None = None
) -> list[WindowRead]:
    today = await local_today(session, user)
    rows = await tax_service.list_windows(session, user, financial_year)
    return [WindowRead.from_model(row, today) for row in rows]


@router.put(
    "/windows",
    response_model=WindowRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def upsert_window(
    payload: WindowUpsert, user: CurrentUser, session: DbSession, request: Request
) -> WindowRead:
    try:
        window = await tax_service.upsert_window(
            session,
            user,
            financial_year=payload.financial_year,
            window_type=payload.window_type,
            opens_at=payload.opens_at,
            closes_at=payload.closes_at,
            allow_late_submission=payload.allow_late_submission,
            reminder_days_before=payload.reminder_days_before,
            request=request,
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc
    await session.commit()
    await session.refresh(window)
    return WindowRead.from_model(window, await local_today(session, user))


@router.post(
    "/reminders/run",
    response_model=ReminderRunRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def run_reminders(user: CurrentUser, session: DbSession) -> ReminderRunRead:
    """Send the window reminders due today.

    What a daily scheduler calls. There is no scheduler in this build, so HR runs
    it; it is idempotent, so running it twice in a day sends nothing twice.
    """
    outcome = await reminders.send_window_reminders(
        session, user.tenant_id, today=await local_today(session, user)
    )
    return ReminderRunRead(
        windows_checked=outcome.windows_checked,
        notifications_raised=outcome.notifications_raised,
        events=outcome.events,
    )


# ── Regime ──────────────────────────────────────────────────────────────────


@router.get("/regime", response_model=RegimeRead | None)
async def get_regime(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> RegimeRead | None:
    target = _resolve_employee(user, employee_id)
    election = await tax_service.get_election(session, user, target, financial_year)
    return RegimeRead.model_validate(election) if election else None


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
    rows = await tax_service.list_declarations(session, user, target, financial_year)
    return [DeclarationRead.from_model(row) for row in rows]


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
    await session.refresh(declaration, ["proofs"])
    return DeclarationRead.from_model(declaration)


@router.post("/declarations/{declaration_id}/submit", response_model=DeclarationRead)
async def submit_declaration(
    declaration_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> DeclarationRead:
    try:
        declaration = await tax_service.submit_declaration(
            session, user, declaration_id, request=request
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc
    await session.commit()
    await session.refresh(declaration, ["proofs"])
    return DeclarationRead.from_model(declaration)


# ── House rent and previous employer ────────────────────────────────────────


@router.get("/house-rent", response_model=HouseRentRead | None)
async def get_house_rent(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> HouseRentRead | None:
    target = _resolve_employee(user, employee_id)
    record = await tax_service.get_house_rent(session, user, target, financial_year)
    return HouseRentRead.from_model(record) if record else None


@router.put("/house-rent", response_model=HouseRentRead)
async def upsert_house_rent(
    payload: HouseRentUpsert, user: CurrentUser, session: DbSession, request: Request
) -> HouseRentRead:
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
            monthly_rent=[row.model_dump(mode="json") for row in payload.monthly_rent],
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
    return HouseRentRead.from_model(record)


@router.get("/previous-employer", response_model=PreviousEmployerRead | None)
async def get_previous_employer(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> PreviousEmployerRead | None:
    target = _resolve_employee(user, employee_id)
    record = await tax_service.get_previous_employer(session, user, target, financial_year)
    return PreviousEmployerRead.from_model(record) if record else None


@router.put("/previous-employer", response_model=PreviousEmployerRead)
async def upsert_previous_employer(
    payload: PreviousEmployerUpsert, user: CurrentUser, session: DbSession, request: Request
) -> PreviousEmployerRead:
    """Income from a previous employer this financial year.

    Without it a mid-year joiner is taxed as though they earned nothing before
    joining, which under-deducts all year and lands them with a liability at
    filing.
    """
    employee_id = _resolve_employee(user, None)
    record = await tax_service.upsert_previous_employer(
        session,
        user,
        employee_id=employee_id,
        financial_year=payload.financial_year,
        values=payload.model_dump(exclude={"financial_year"}),
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return PreviousEmployerRead.from_model(record)


@router.post("/previous-employer/document", response_model=PreviousEmployerRead)
async def attach_previous_employer_document(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    financial_year: str = Form(pattern=r"^\d{4}-\d{2}$"),
    file: UploadFile = File(...),
) -> PreviousEmployerRead:
    """Attach the previous employer's Form 16 Part B (PDF, PNG or JPEG)."""
    employee_id = _resolve_employee(user, None)
    try:
        record = await tax_service.attach_previous_employer_document(
            session,
            user,
            employee_id=employee_id,
            financial_year=financial_year,
            data=await _read_upload(file),
            request=request,
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc
    await session.commit()
    await session.refresh(record)
    return PreviousEmployerRead.from_model(record)


@router.get("/previous-employer/document")
async def previous_employer_document(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> Response:
    target = _resolve_employee(user, employee_id)
    data, mime, name = await tax_service.previous_employer_document(
        session, user, employee_id=target, financial_year=financial_year, request=request
    )
    await session.commit()
    return Response(content=data, media_type=mime, headers=support.attachment_headers(name))


# ── Proofs ──────────────────────────────────────────────────────────────────


@router.post("/proofs", response_model=ProofRead, status_code=status.HTTP_201_CREATED)
async def upload_proof(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    declaration_id: uuid.UUID = Form(...),
    proof_amount: Decimal = Form(gt=0, max_digits=14, decimal_places=2),
    file: UploadFile = File(...),
) -> ProofRead:
    """Attach a proof document to one of your submitted declarations.

    PDF, PNG or JPEG up to the upload limit. The type is read from the file's
    contents, never its name, and the file is stored encrypted under a key the
    server generates. The declaration must be yours unless you are payroll.
    """
    try:
        proof = await tax_service.submit_proof(
            session,
            user,
            declaration_id=declaration_id,
            proof_amount=proof_amount,
            data=await _read_upload(file),
            document_name=file.filename,
            request=request,
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc
    await session.commit()
    await session.refresh(proof)
    return ProofRead.from_model(proof)


@router.get(
    "/proofs/queue",
    response_model=list[ProofQueueItem],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def verification_queue(
    user: CurrentUser,
    session: DbSession,
    financial_year: str | None = None,
    proof_status: Literal["pending", "all"] = Query(default="pending", alias="status"),
) -> list[ProofQueueItem]:
    """Proofs awaiting verification, oldest first — the payroll team's January queue."""
    rows = await tax_service.proof_queue(
        session, user, financial_year=financial_year, pending_only=proof_status == "pending"
    )
    return [ProofQueueItem(**row) for row in rows]


@router.get("/proofs/{proof_id}/document")
async def proof_document(
    proof_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    """The proof as uploaded. Its owner and payroll only."""
    data, mime, name = await tax_service.proof_document(
        session, user, proof_id, request=request
    )
    await session.commit()
    return Response(content=data, media_type=mime, headers=support.attachment_headers(name))


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
    anything but a clean full acceptance, every decision writes an audit entry,
    and a verifier cannot decide a proof against their own declaration.
    """
    if payload.decision is ProofStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A verification decision cannot be 'pending'",
        )
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

    return ProofRead.from_model(proof)


@router.post(
    "/proof-window/close",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
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
    impact of what they intend to claim is the useful answer. Includes taxable
    income from gratuity, encashment, arrears, loan perquisites and unclaimed
    reimbursements.
    """
    target = _resolve_employee(user, employee_id)
    try:
        computation = await tax_service.live_preview(session, user, target, financial_year)
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
    rows = await tax_service.list_snapshots(session, user, target, financial_year)
    return [SnapshotRead.model_validate(row) for row in rows]


# ── Form 12BB ───────────────────────────────────────────────────────────────


@router.get("/form-12bb")
async def form_12bb_preview(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> dict[str, str]:
    """The claims as an escaped HTML table, for a sandboxed on-screen preview."""
    from app.services.tax.form_12bb import build_claims_table

    target = _resolve_employee(user, employee_id)
    return {
        "claims_table": await build_claims_table(session, user, target, financial_year)
    }


@router.post(
    "/form-12bb/issue",
    response_model=IssuedDocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def issue_form_12bb(
    payload: Form12BBIssue, user: CurrentUser, session: DbSession, request: Request
) -> IssuedDocumentRead:
    """Issue Form 12BB through the letter engine, for the employee to e-sign."""
    target = _resolve_employee(user, payload.employee_id)
    try:
        letter = await tax_service.issue_form_12bb(
            session,
            user,
            employee_id=target,
            financial_year=payload.financial_year,
            parent_name=payload.parent_name,
            place=payload.place,
            request=request,
        )
    except TaxWorkflowError as exc:
        raise _workflow_error(exc) from exc
    await session.commit()
    await session.refresh(letter)
    return IssuedDocumentRead.from_model(letter)


@router.get("/form-12bb/documents", response_model=list[IssuedDocumentRead])
async def list_form_12bb(
    user: CurrentUser,
    session: DbSession,
    financial_year: str,
    employee_id: uuid.UUID | None = None,
) -> list[IssuedDocumentRead]:
    target = _resolve_employee(user, employee_id)
    letters = await tax_service.list_form_12bb(session, user, target, financial_year)
    return [IssuedDocumentRead.from_model(letter) for letter in letters]


@router.get("/form-12bb/{letter_id}/pdf")
async def form_12bb_pdf(letter_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    data, name = await tax_service.form_12bb_pdf(session, user, letter_id)
    return Response(
        content=data, media_type="application/pdf", headers=support.attachment_headers(name)
    )


# ── Form 16 Part B ──────────────────────────────────────────────────────────


@router.get("/form-16")
async def form_16_years(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Each financial year with approved payroll, and whether its Part B is ready."""
    target = _resolve_employee(user, employee_id)
    return await form16.list_form16_years(session, user, target)


@router.get("/form-16/pdf", response_model=None)
async def form_16_pdf(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    financial_year: str = Query(pattern=r"^\d{4}-\d{2}$"),
    employee_id: uuid.UUID | None = None,
) -> Response:
    """Form 16 Part B for one financial year, computed from the year's approved payroll."""
    target = _resolve_employee(user, employee_id)
    try:
        statement = await form16.form16_statement(session, user, target, financial_year)
        data = form16.render_form16_pdf(statement)
    except form16.Form16UnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    data, with_part_a = await form16_part_a.merged_pdf(
        await form16_part_a.part_a_for(session, user, target, financial_year), data
    )
    if target != user.employee_id:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.EXPORT,
            entity_type="form_16",
            entity_id=target,
            actor=user,
            changes={"financial_year": financial_year},
            summary=f"Downloaded Form 16 Part B for {financial_year} of another employee",
            request=request,
        )
        await session.commit()
    parts = "Form16" if with_part_a else "Form16-PartB"
    name = f"{parts}-{statement['employee']['code']}-{financial_year}.pdf"
    return Response(
        content=data, media_type="application/pdf", headers=support.attachment_headers(name)
    )


__all__ = ["WindowType", "router"]



# ── Form 16 Part A (from TRACES) ────────────────────────────────────────────


@router.post("/form-16/part-a", dependencies=[Depends(require_roles(*PAYROLL_ROLES))])
async def upload_form16_part_a(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    financial_year: Annotated[str, Form(pattern=r"^\d{4}-\d{2}$")],
    file: Annotated[UploadFile, File()],
    employee_id: Annotated[uuid.UUID | None, Form()] = None,
) -> dict[str, Any]:
    """One Part A PDF for one person, or the TRACES ZIP matched on the PAN in each name."""
    data = await file.read(form16_part_a.MAX_ZIP_BYTES + 1)
    if employee_id is not None:
        return await form16_part_a.upload_one(
            session, user, employee_id=employee_id, financial_year=financial_year,
            data=data, filename=file.filename, request=request,
        )
    return await form16_part_a.upload_zip(
        session, user, financial_year=financial_year, data=data, request=request
    )
