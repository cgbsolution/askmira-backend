"""GST input tax credit capture and GSTR-2B reconciliation (slice 34).

`GET /gst/dashboard` is the endpoint the whole slice is for: input credit
captured, confirmed claimable and at risk from unfiled vendors, in rupees. In a
client conversation it converts the expense module from a convenience into a
line item with a return — and a figure that turns out to be optimistic destroys
that conversation permanently, so nothing reads as confirmed until a GSTR-2B has
been reconciled.

`GET /gst/posture` says what is enforced and, first in the list, that **the
blocked-category rules have not been reviewed by a tax advisor**.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.gst import GstEligibilityEnum
from app.models.user import UserRole
from app.services.gst import eligibility as elig_engine
from app.services.gst import gstin as gstin_engine
from app.services.gst import service as gst

router = APIRouter(tags=["gst"])

FINANCE_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class EntityGstinIn(BaseModel):
    legal_entity_id: uuid.UUID
    gstin: str = Field(min_length=15, max_length=15)


class BlockedCategoryIn(BaseModel):
    category_key: str = Field(min_length=1, max_length=64)
    #: Required. A line marked ineligible with no explanation is one finance
    #: cannot challenge.
    reason: str = Field(min_length=1, max_length=4000)
    reference: str = Field(default="Section 17(5), CGST Act", max_length=120)
    #: Where the section allows the credit in circumstances an expense category
    #: cannot express, a line routes to a person instead of being decided.
    has_exceptions: bool = False
    exception_note: str | None = Field(default=None, max_length=4000)
    reviewed: bool = False
    reviewed_by: str | None = Field(default=None, max_length=160)
    reviewed_on: date | None = None


class CaptureIn(BaseModel):
    vendor_gstin: str = Field(default="", max_length=20)
    invoice_number: str = Field(min_length=1, max_length=120)
    invoice_date: date
    category_key: str = Field(min_length=1, max_length=64)
    taxable_value: Decimal = Field(ge=0)
    legal_entity_id: uuid.UUID | None = None
    expense_claim_id: uuid.UUID | None = None
    employee_id: uuid.UUID | None = None
    cgst: Decimal = Field(default=Decimal("0"), ge=0)
    sgst: Decimal = Field(default=Decimal("0"), ge=0)
    igst: Decimal = Field(default=Decimal("0"), ge=0)
    cess: Decimal = Field(default=Decimal("0"), ge=0)
    supplier_state_code: str | None = Field(default=None, max_length=2)
    place_of_supply_state_code: str | None = Field(default=None, max_length=2)
    hsn_sac: str | None = Field(default=None, max_length=16)


class ReviewIn(BaseModel):
    #: `uncertain` is refused — a review has to reach a decision.
    decision: GstEligibilityEnum
    rationale: str = Field(min_length=1, max_length=4000)
    decided_on: date


class Gstr2bLineIn(BaseModel):
    vendor_gstin: str = Field(min_length=1, max_length=20)
    vendor_name: str | None = Field(default=None, max_length=240)
    invoice_number: str = Field(min_length=1, max_length=120)
    invoice_date: date
    taxable_value: Decimal = Field(ge=0)
    cgst: Decimal = Field(default=Decimal("0"), ge=0)
    sgst: Decimal = Field(default=Decimal("0"), ge=0)
    igst: Decimal = Field(default=Decimal("0"), ge=0)
    cess: Decimal = Field(default=Decimal("0"), ge=0)


class Gstr2bImportIn(BaseModel):
    period_label: str = Field(min_length=1, max_length=16)
    imported_on: date
    lines: list[Gstr2bLineIn] = Field(default_factory=list)
    legal_entity_id: uuid.UUID | None = None
    source_note: str | None = Field(default=None, max_length=2000)


class ReconcileIn(BaseModel):
    period_label: str = Field(min_length=1, max_length=16)
    period_start: date
    period_end: date
    legal_entity_id: uuid.UUID | None = None


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
    """The shipped reading of Section 17(5), every rule unreviewed.

    The exceptions are written out so an advisor can disagree with the reading
    rather than only with the outcome.
    """
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
async def validate_gstin(
    user: CurrentUser,
    gstin: str,
) -> dict[str, Any]:
    """Check a GSTIN's structure and checksum.

    Never blocks anything. A number that fails here means the input credit is
    doubtful, not that an expense is refused.
    """
    return gstin_engine.validate_gstin(gstin).as_dict()


# ── Configuration ───────────────────────────────────────────────────────────


@router.put("/gst/entity-registrations")
async def register_entity_gstin(
    body: EntityGstinIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Record one of the company's own GST registrations.

    This one *is* refused when it does not validate: a wrong entity GSTIN
    misclassifies the place of supply on every invoice that follows.
    """
    row = await gst.register_entity_gstin(
        session,
        user,
        legal_entity_id=body.legal_entity_id,
        gstin=body.gstin,
        request=request,
    )
    return {
        "registration_id": str(row.id),
        "gstin": row.gstin,
        "state_code": row.state_code,
        "state_name": row.state_name,
    }


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
    """Capture one invoice line and decide what it is worth as input credit.

    Never refuses. A bad GSTIN, a blocked category and an undecidable place of
    supply all produce a stored row with a verdict and a reason — the expense
    behind it is real either way.
    """
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
        employee_id=body.employee_id,
        cgst=body.cgst,
        sgst=body.sgst,
        igst=body.igst,
        cess=body.cess,
        supplier_state_code=body.supplier_state_code,
        place_of_supply_state_code=body.place_of_supply_state_code,
        hsn_sac=body.hsn_sac,
        request=request,
    )


@router.get("/gst/review-queue")
async def review_queue(
    session: DbSession,
    user: CurrentUser,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Undecided lines, largest amount at stake first.

    Nothing here is decided either way. Guessing eligible claims credit the
    company may not be entitled to; guessing ineligible leaks money.
    """
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
    """Record a person's decision on an uncertain line.

    Only an uncertain line can be reviewed: overriding one the rules already
    settled would depart from Section 17(5) with nothing recording it.
    """
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
    """The same vendor invoice claimed more than once, across the tenant.

    Across the tenant, not per employee: two people submitting the same bill is
    the case that matters and a per-employee check cannot see it.
    """
    return await gst.duplicate_report(session, user)


# ── GSTR-2B ─────────────────────────────────────────────────────────────────


@router.post("/gst/gstr2b-imports", status_code=status.HTTP_201_CREATED)
async def import_gstr2b(
    body: Gstr2bImportIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Import a GSTR-2B download, stored as received.

    Nothing connects to the portal; the file is supplied by a person.
    """
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


@router.post("/gst/reconciliations", status_code=status.HTTP_201_CREATED)
async def reconcile_period(
    body: ReconcileIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Match the register against GSTR-2B and report the rupee gap.

    Matched, missing-in-2B and missing-in-register are reported separately. A
    net figure would let the second and third cancel, and both are real.
    """
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
    """Captured, claimable and at-risk credit in rupees.

    Nothing reads as confirmed until a GSTR-2B has been reconciled, and the
    at-risk figure counts only eligible lines.
    """
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
    """The register in the shape a CA prepares GSTR-3B from.

    Uncertain lines are listed separately rather than dropped: a CA who is not
    told about them cannot ask.
    """
    return await gst.register_export(
        session, user, period_start=period_start, period_end=period_end
    )
