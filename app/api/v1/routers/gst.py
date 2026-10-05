"""GST input tax credit capture and GSTR-2B reconciliation (slice 34).

`GET /gst/dashboard` is what the slice is for: input credit captured, confirmed
claimable and at risk from unfiled vendors, in rupees. Nothing reads as
confirmed until a GSTR-2B has been reconciled.

Not built, and said on `GET /gst/posture`: the blocked-category rules have not
been reviewed by a tax advisor; there is no GST portal status check; there is no
OCR extraction — invoice details are typed in.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.gst import GstEligibilityEnum, Gstr2bMatchEnum
from app.models.user import UserRole
from app.schemas.gst import (
    BlockedCategoryIn,
    BlockedCategoryRead,
    CaptureIn,
    EntityGstinIn,
    EntityRegistrationRead,
    GstCreditRead,
    Gstr2bCsvResult,
    Gstr2bImportIn,
    Gstr2bImportRead,
    ReconcileIn,
    ReconciliationRead,
    ReviewIn,
)
from app.services.gst import csv_io
from app.services.gst import eligibility as elig_engine
from app.services.gst import gstin as gstin_engine
from app.services.gst import service as gst

router = APIRouter(tags=["gst"])

FINANCE_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


# ── Posture and reference data ──────────────────────────────────────────────


@router.get("/gst/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    """What is enforced, and what is not built."""
    return gst.gst_posture()


@router.get("/gst/blocked-categories/defaults")
async def default_blocked_categories(
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """The shipped reading of Section 17(5), every rule unreviewed."""
    return {
        "rules": [
            {
                "category_key": r.category_key,
                "reason": r.reason,
                "reference": r.reference,
                "has_exceptions": r.has_exceptions,
                "exception_note": r.exception_note,
                "reviewed": r.reviewed,
            }
            for r in elig_engine.DEFAULT_BLOCKED_CATEGORIES
        ],
        "warning": (
            "Every rule here is unreviewed. A category with exceptions routes "
            "its lines to a finance review rather than being decided, which is "
            "the safe behaviour until an advisor has been through them."
        ),
    }


@router.get("/gst/validate-gstin")
async def validate_gstin(user: CurrentUser, gstin: str) -> dict[str, Any]:
    """Check a GSTIN's structure and checksum. Never blocks anything."""
    return gstin_engine.validate_gstin(gstin).as_dict()


# ── Configuration ───────────────────────────────────────────────────────────


@router.get("/gst/entity-registrations", response_model=list[EntityRegistrationRead])
async def list_entity_registrations(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> list[EntityRegistrationRead]:
    rows = await gst.list_entity_registrations(session, user)
    return [EntityRegistrationRead.model_validate(r) for r in rows]


@router.put("/gst/entity-registrations")
async def register_entity_gstin(
    body: EntityGstinIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Record one of the company's own registrations. Refused if it does not validate."""
    row = await gst.register_entity_gstin(
        session, user, legal_entity_id=body.legal_entity_id, gstin=body.gstin, request=request
    )
    return {
        "registration_id": str(row.id),
        "gstin": row.gstin,
        "state_code": row.state_code,
        "state_name": row.state_name,
    }


@router.get("/gst/blocked-categories", response_model=list[BlockedCategoryRead])
async def list_blocked_categories(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> list[BlockedCategoryRead]:
    rows = await gst.list_blocked_categories(session, user)
    return [BlockedCategoryRead.model_validate(r) for r in rows]


@router.put("/gst/blocked-categories")
async def set_blocked_category(
    body: BlockedCategoryIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await gst.set_blocked_category(
        session,
        user,
        category_key=body.category_key,
        reason=body.reason,
        reference=body.reference,
        has_exceptions=body.has_exceptions,
        exception_note=body.exception_note,
        reviewed=body.reviewed,
        reviewed_by=body.reviewed_by,
        reviewed_on=body.reviewed_on,
        request=request,
    )
    return {
        "rule_id": str(row.id),
        "category_key": row.category_key,
        "has_exceptions": row.has_exceptions,
        "reviewed": row.reviewed,
    }


# ── Capture and review ──────────────────────────────────────────────────────


@router.post("/gst/credits", status_code=status.HTTP_201_CREATED)
async def capture_credit(
    body: CaptureIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Capture one invoice and decide what it is worth as input credit. Never refuses."""
    employee_id = body.employee_id
    if not permissions.is_hr(user):
        # Employees capture a GST invoice against their own expense claim; the
        # company-level register is finance's.
        employee_id = permissions.resolve_employee(
            user, body.employee_id, detail="You can only capture invoices for yourself."
        )
        if body.expense_claim_id is None:
            raise permissions.forbidden(
                "Outside finance, a GST invoice is captured against one of your "
                "own expense claims."
            )
        await gst.require_own_expense_claim(session, user, body.expense_claim_id)
    return await gst.capture_credit(
        session,
        user,
        vendor_gstin=body.vendor_gstin,
        invoice_number=body.invoice_number,
        invoice_date=body.invoice_date,
        category_key=body.category_key,
        taxable_value=body.taxable_value,
        legal_entity_id=body.legal_entity_id,
        expense_claim_id=body.expense_claim_id,
        employee_id=employee_id,
        cgst=body.cgst,
        sgst=body.sgst,
        igst=body.igst,
        cess=body.cess,
        supplier_state_code=body.supplier_state_code,
        place_of_supply_state_code=body.place_of_supply_state_code,
        hsn_sac=body.hsn_sac,
        request=request,
    )


@router.get("/gst/credits", response_model=list[GstCreditRead])
async def list_credits(
    session: DbSession,
    user: CurrentUser,
    eligibility: GstEligibilityEnum | None = None,
    reconciliation_status: Gstr2bMatchEnum | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> list[GstCreditRead]:
    rows = await gst.list_credits(
        session,
        user,
        eligibility=eligibility,
        reconciliation_status=reconciliation_status,
        period_start=period_start,
        period_end=period_end,
    )
    return [GstCreditRead.model_validate(r) for r in rows]


@router.get("/gst/claims/{claim_id}/credits", response_model=list[GstCreditRead])
async def claim_credits(
    claim_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> list[GstCreditRead]:
    """GST invoices on one expense claim: the claimant's own, or any for finance."""
    rows = await gst.claim_credits(session, user, claim_id)
    return [GstCreditRead.model_validate(r) for r in rows]


@router.get("/gst/review-queue")
async def review_queue(
    session: DbSession,
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Undecided lines, largest amount at stake first."""
    return await gst.review_queue(session, user)


@router.post("/gst/credits/{credit_id}/review")
async def review_credit(
    credit_id: uuid.UUID,
    body: ReviewIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Record a person's decision, with a rationale, on an uncertain line."""
    return await gst.review_credit(
        session,
        user,
        credit_id=credit_id,
        decision=body.decision,
        rationale=body.rationale,
        decided_on=body.decided_on,
        request=request,
    )


@router.get("/gst/duplicates")
async def duplicates(
    session: DbSession,
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """The same vendor invoice claimed more than once, across the tenant."""
    return await gst.duplicate_report(session, user)


# ── GSTR-2B ─────────────────────────────────────────────────────────────────


@router.get("/gst/gstr2b-imports", response_model=list[Gstr2bImportRead])
async def list_gstr2b_imports(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> list[Gstr2bImportRead]:
    rows = await gst.list_gstr2b_imports(session, user)
    return [Gstr2bImportRead.model_validate(r) for r in rows]


@router.post("/gst/gstr2b-imports", status_code=status.HTTP_201_CREATED)
async def import_gstr2b(
    body: Gstr2bImportIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Import a GSTR-2B download, stored as received. Nothing connects to the portal."""
    return await gst.import_gstr2b(
        session,
        user,
        period_label=body.period_label,
        lines=[line.model_dump() for line in body.lines],
        imported_on=body.imported_on,
        legal_entity_id=body.legal_entity_id,
        source_note=body.source_note,
        request=request,
    )


@router.post(
    "/gst/gstr2b-imports/csv",
    response_model=Gstr2bCsvResult,
    dependencies=[Depends(require_roles(*FINANCE_ROLES))],
)
async def import_gstr2b_csv(
    session: DbSession,
    user: CurrentUser,
    request: Request,
    file: UploadFile = File(...),
    period_label: str = Form(..., min_length=1, max_length=16),
    imported_on: date = Form(...),
    source_note: str | None = Form(default=None, max_length=2000),
    legal_entity_id: uuid.UUID | None = Form(default=None),
) -> Gstr2bCsvResult:
    """Upload a GSTR-2B CSV.

    A file with any unreadable row is not imported: a period imports once, and a
    2B with rows silently dropped would report those vendors as unfiled. The rows
    come back with their numbers so the file can be fixed and uploaded again.
    """
    raw = await file.read(csv_io.MAX_BYTES + 1)
    try:
        text = csv_io.decode_csv(raw)
        lines, errors = csv_io.parse_gstr2b_csv(text)
    except csv_io.GstCsvTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except csv_io.GstCsvError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    rows_read = len(lines) + len(errors)
    if errors or not lines:
        return Gstr2bCsvResult(
            imported=None,
            rows_read=rows_read,
            errors=errors or [{"row": 0, "error": "The file has a header but no lines."}],
        )
    imported = await gst.import_gstr2b(
        session,
        user,
        period_label=period_label,
        lines=lines,
        imported_on=imported_on,
        legal_entity_id=legal_entity_id,
        source_note=source_note,
        request=request,
    )
    return Gstr2bCsvResult(imported=imported, rows_read=rows_read, errors=[])


@router.get("/gst/reconciliations", response_model=list[ReconciliationRead])
async def list_reconciliations(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> list[ReconciliationRead]:
    rows = await gst.list_reconciliations(session, user)
    return [ReconciliationRead.model_validate(r) for r in rows]


@router.post("/gst/reconciliations", status_code=status.HTTP_201_CREATED)
async def reconcile_period(
    body: ReconcileIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Match the register against GSTR-2B and report the rupee gap."""
    return await gst.reconcile_period(
        session,
        user,
        period_label=body.period_label,
        period_start=body.period_start,
        period_end=body.period_end,
        legal_entity_id=body.legal_entity_id,
        request=request,
    )


# ── Reporting ───────────────────────────────────────────────────────────────


@router.get("/gst/dashboard")
async def dashboard(
    session: DbSession,
    user: CurrentUser,
    period_start: date | None = None,
    period_end: date | None = None,
    period_label: str = "",
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Captured, claimable and at-risk credit in rupees."""
    return await gst.credit_dashboard(
        session,
        user,
        period_start=period_start,
        period_end=period_end,
        period_label=period_label,
    )


@router.get("/gst/register")
async def register_export(
    session: DbSession,
    user: CurrentUser,
    period_start: date,
    period_end: date,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """The register in the shape a CA prepares GSTR-3B from."""
    return await gst.register_export(
        session, user, period_start=period_start, period_end=period_end
    )


@router.get("/gst/register.csv", response_model=None)
async def register_csv(
    session: DbSession,
    user: CurrentUser,
    request: Request,
    period_start: date,
    period_end: date,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> Response:
    """The register as a CSV download. Formulas in cells are neutralised."""
    content = await gst.register_csv(
        session, user, period_start=period_start, period_end=period_end, request=request
    )
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="gst-register-{period_start}-{period_end}.csv"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
