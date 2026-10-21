"""The DB-facing half of GST input credit capture.

The checksum, the eligibility rules, the 2B matching and the dashboard
arithmetic live in the pure modules beside this. An invariant checks the
isolation.

═══════════════════════════════════════════════════════════════════════════════
CAPTURE NEVER REFUSES
═══════════════════════════════════════════════════════════════════════════════

`capture_credit` records the line whatever the GSTIN says and whatever the
category is. A bad GSTIN makes the *credit* doubtful; the expense behind it is
real and reimbursable, and a capture step that rejects a receipt because the
fifteenth character does not check out teaches employees that the scanner is
broken.

═══════════════════════════════════════════════════════════════════════════════
AND UNCERTAIN NEVER RESOLVES ITSELF
═══════════════════════════════════════════════════════════════════════════════

An `UNCERTAIN` line stays uncertain until `review_credit` records a person's
decision with their reasoning. There is no code path that promotes it, and an
invariant fails the build if one appears.

**What is and is not wired**, stated once here:

- **No GST portal integration.** `gstin_portal_status` stays `NOT_CHECKED`, so a
  structurally valid GSTIN belonging to a cancelled registration passes.
- **No receipt extraction.** The invoice fields arrive from the caller; nothing
  reads them off an image.
- **Nothing files anything.** The register is exportable; GSTR-3B preparation is
  the client's CA's.
- **The blocked-category rules have not been reviewed by a tax advisor.** Every
  shipped rule carries `reviewed=False`.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.expenses import ExpenseClaim
from app.models.gst import (
    GstBlockedCategory,
    GstCreditReview,
    GstEligibilityEnum,
    GstEntityRegistration,
    GstinPortalStatusEnum,
    GstInputCredit,
    Gstr2bImport,
    Gstr2bLine,
    Gstr2bMatchEnum,
    GstReconciliation,
    GstTaxSplitEnum,
)
from app.models.user import User
from app.services import audit
from app.services.gst import csv_io
from app.services.gst import eligibility as elig_engine
from app.services.gst import gstin as gstin_engine
from app.services.gst import register as register_engine

__all__ = [
    "capture_credit",
    "credit_dashboard",
    "duplicate_report",
    "gst_posture",
    "import_gstr2b",
    "reconcile_period",
    "register_entity_gstin",
    "register_export",
    "review_credit",
    "review_queue",
    "set_blocked_category",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _d(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════


async def register_entity_gstin(
    session: AsyncSession,
    user: User,
    *,
    legal_entity_id: uuid.UUID,
    gstin: str,
    request: Request | None = None,
) -> GstEntityRegistration:
    """Record one of the company's own registrations.

    Refused if it does not validate — this one *is* worth blocking, because it
    is configuration entered by an admin rather than a number read off somebody
    else's receipt, and a wrong entity GSTIN misclassifies every invoice that
    follows.
    """
    verdict = gstin_engine.validate_gstin(gstin)
    if not verdict.is_valid:
        raise _bad_request(
            f"{verdict.message} This is the company's own registration rather "
            "than a vendor's, so it is refused: a wrong entity GSTIN "
            "misclassifies the place of supply on every invoice that follows."
        )

    stmt = tenant_scoped(
        select(GstEntityRegistration)
        .where(GstEntityRegistration.legal_entity_id == legal_entity_id)
        .where(GstEntityRegistration.state_code == verdict.state_code),
        GstEntityRegistration,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = GstEntityRegistration(
            tenant_id=user.tenant_id,
            legal_entity_id=legal_entity_id,
            gstin=verdict.gstin,
            state_code=verdict.state_code or "",
            state_name=verdict.state_name,
        )
        session.add(row)
    else:
        row.gstin = verdict.gstin
        row.state_name = verdict.state_name
    row.is_active = True

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="gst_entity_registration",
        entity_id=row.id,
        actor=user,
        summary=f"GST registration {verdict.gstin} recorded for {verdict.state_name}.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def set_blocked_category(
    session: AsyncSession,
    user: User,
    *,
    category_key: str,
    reason: str,
    reference: str = "Section 17(5), CGST Act",
    has_exceptions: bool = False,
    exception_note: str | None = None,
    reviewed: bool = False,
    reviewed_by: str | None = None,
    reviewed_on: date | None = None,
    request: Request | None = None,
) -> GstBlockedCategory:
    try:
        elig_engine.BlockedCategoryRule(
            category_key=category_key,
            reason=reason,
            reference=reference,
            has_exceptions=has_exceptions,
            exception_note=exception_note or "",
            reviewed=reviewed,
            reviewed_by=reviewed_by or "",
        )
    except elig_engine.EligibilityError as exc:
        raise _bad_request(str(exc)) from exc

    stmt = tenant_scoped(
        select(GstBlockedCategory).where(GstBlockedCategory.category_key == category_key),
        GstBlockedCategory,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = GstBlockedCategory(tenant_id=user.tenant_id, category_key=category_key, reason=reason)
        session.add(row)
    row.reason = reason
    row.reference = reference
    row.has_exceptions = has_exceptions
    row.exception_note = exception_note
    row.reviewed = reviewed
    row.reviewed_by = reviewed_by
    row.reviewed_on = reviewed_on

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="gst_blocked_category",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Blocked-credit rule for {category_key!r} set"
            + (f", reviewed by {reviewed_by}." if reviewed else ", NOT reviewed.")
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _blocked_rules(
    session: AsyncSession, user: User
) -> tuple[elig_engine.BlockedCategoryRule, ...]:
    """Configured rules, with the unreviewed defaults filling the gaps.

    A tenant that has configured nothing still gets Section 17(5) applied. That
    is the safe direction: the alternative is treating every blocked category as
    eligible until somebody remembers to configure it.
    """
    stmt = tenant_scoped(select(GstBlockedCategory), GstBlockedCategory, user)
    rows = list((await session.execute(stmt)).scalars())
    configured = tuple(
        elig_engine.BlockedCategoryRule(
            category_key=row.category_key,
            reason=row.reason,
            reference=row.reference,
            has_exceptions=row.has_exceptions,
            exception_note=row.exception_note or "",
            reviewed=row.reviewed,
            reviewed_by=row.reviewed_by or "",
        )
        for row in rows
    )
    covered = {r.category_key for r in configured}
    return configured + tuple(
        r for r in elig_engine.DEFAULT_BLOCKED_CATEGORIES if r.category_key not in covered
    )


# ══════════════════════════════════════════════════════════════════════════════
# Capture
# ══════════════════════════════════════════════════════════════════════════════


async def capture_credit(
    session: AsyncSession,
    user: User,
    *,
    vendor_gstin: str,
    invoice_number: str,
    invoice_date: date,
    category_key: str,
    taxable_value: Decimal,
    legal_entity_id: uuid.UUID | None = None,
    expense_claim_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    cgst: Decimal = Decimal("0"),
    sgst: Decimal = Decimal("0"),
    igst: Decimal = Decimal("0"),
    cess: Decimal = Decimal("0"),
    supplier_state_code: str | None = None,
    place_of_supply_state_code: str | None = None,
    hsn_sac: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record one invoice line and decide what it is worth as input credit.

    Never refuses. A malformed GSTIN, a blocked category and an undecidable
    place of supply all produce a stored row with a verdict and a reason - the
    expense behind it is real either way.
    """
    verdict = gstin_engine.validate_gstin(vendor_gstin)

    # Which of the company's own registrations is the recipient. Where an
    # entity is given, its registrations; where none is, the tenant's - and
    # only if there is exactly one, because a company registered in three
    # states has three answers and guessing produces the hotel-bill error at
    # scale. A single-registration company is the common case and must not have
    # every line routed to review.
    reg_stmt = tenant_scoped(
        select(GstEntityRegistration).where(GstEntityRegistration.is_active.is_(True)),
        GstEntityRegistration,
        user,
    )
    if legal_entity_id is not None:
        reg_stmt = reg_stmt.where(GstEntityRegistration.legal_entity_id == legal_entity_id)
    registrations = list((await session.execute(reg_stmt)).scalars())

    recipient_state = None
    pos = place_of_supply_state_code or ""
    match = next((r for r in registrations if r.state_code == pos), None)
    if match is not None:
        # Registered in the state of supply: this is an intra-state supply we
        # can claim.
        recipient_state = match.state_code
    elif len(registrations) == 1:
        # One registration and the supply is elsewhere. That is precisely the
        # other-state case, and naming it is the whole point.
        recipient_state = registrations[0].state_code
    elif not registrations:
        # No registration on file at all. Fall back to the place of supply so an
        # otherwise-clean invoice is not dragged into the review queue by a
        # configuration gap - but say so in the posture, because a company that
        # has not recorded its own GSTIN cannot have the other-state case
        # detected for it.
        recipient_state = place_of_supply_state_code

    supplier_state = supplier_state_code or (verdict.state_code or "")
    facts = elig_engine.InvoiceFacts(
        category_key=category_key,
        vendor_gstin=verdict.gstin,
        invoice_number=invoice_number,
        taxable_value=_d(taxable_value),
        cgst=_d(cgst),
        sgst=_d(sgst),
        igst=_d(igst),
        cess=_d(cess),
        supplier_state_code=supplier_state or "",
        place_of_supply_state_code=place_of_supply_state_code or "",
        recipient_state_code=recipient_state or "",
        hsn_sac=hsn_sac or "",
        gstin_is_valid=verdict.is_valid,
    )
    assessment = elig_engine.assess(facts, blocked=await _blocked_rules(session, user))

    normalised = register_engine.normalise_invoice_number(invoice_number)
    row = GstInputCredit(
        tenant_id=user.tenant_id,
        legal_entity_id=legal_entity_id,
        expense_claim_id=expense_claim_id,
        employee_id=employee_id,
        vendor_gstin=verdict.gstin,
        gstin_is_valid=verdict.is_valid,
        gstin_problem=verdict.problem.value,
        gstin_portal_status=GstinPortalStatusEnum(verdict.portal_status.value),
        invoice_number=invoice_number,
        normalised_invoice_number=normalised,
        invoice_date=invoice_date,
        category_key=category_key,
        taxable_value=_d(taxable_value),
        cgst=_d(cgst),
        sgst=_d(sgst),
        igst=_d(igst),
        cess=_d(cess),
        hsn_sac=hsn_sac,
        supplier_state_code=supplier_state or None,
        place_of_supply_state_code=place_of_supply_state_code,
        recipient_state_code=recipient_state,
        tax_split=(GstTaxSplitEnum(assessment.tax_split.value) if assessment.tax_split else None),
        eligibility=GstEligibilityEnum(assessment.eligibility.value),
        eligibility_reason=assessment.reason,
        eligibility_reference=assessment.reference,
        claimable_amount=assessment.claimable_amount,
        amount_at_stake=assessment.amount_at_stake,
        reconciliation_status=Gstr2bMatchEnum.NOT_RECONCILED,
    )
    session.add(row)
    await session.flush()

    existing = list(
        (
            await session.execute(
                tenant_scoped(
                    select(GstInputCredit)
                    .where(GstInputCredit.vendor_gstin == verdict.gstin)
                    .where(GstInputCredit.normalised_invoice_number == normalised)
                    .where(GstInputCredit.id != row.id),
                    GstInputCredit,
                    user,
                )
            )
        ).scalars()
    )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="gst_input_credit",
        entity_id=row.id,
        actor=user,
        summary=(f"Input credit captured for {invoice_number}: {assessment.eligibility.value}."),
        request=request,
    )
    await session.commit()
    await session.refresh(row)

    return {
        "credit_id": str(row.id),
        "gstin": verdict.as_dict(),
        "assessment": assessment.as_dict(),
        "duplicate_of": [str(e.id) for e in existing],
        "duplicate_across_employees": bool(
            {e.employee_id for e in existing if e.employee_id} - {employee_id}
        ),
        # Said explicitly so no caller has to infer it.
        "expense_recorded": True,
        "note": (
            "The expense stands whatever the credit verdict says. Only the "
            "input tax credit is in question here."
        ),
    }


async def review_credit(
    session: AsyncSession,
    user: User,
    *,
    credit_id: uuid.UUID,
    decision: GstEligibilityEnum,
    rationale: str,
    decided_on: date,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record a person's decision on an uncertain line.

    Only an uncertain line can be reviewed. Re-deciding a line the rules already
    settled would let a person quietly override Section 17(5) with no record
    that a rule was being departed from.
    """
    if not rationale.strip():
        raise _bad_request(
            "A review needs a rationale. The basis is the whole point of the "
            "record - in an assessment the question is who decided and why, and "
            "a status column answers neither."
        )
    if decision is GstEligibilityEnum.UNCERTAIN:
        raise _bad_request(
            "A review has to reach a decision. Leaving it uncertain is what the queue already says."
        )

    row = (
        await session.execute(
            tenant_scoped(
                select(GstInputCredit).where(GstInputCredit.id == credit_id),
                GstInputCredit,
                user,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such input credit line in this tenant.")
    if row.eligibility is not GstEligibilityEnum.UNCERTAIN:
        raise _bad_request(
            f"This line is already {row.eligibility.value} on the rules. "
            "Overriding a settled line here would depart from Section 17(5) "
            "with nothing recording that a departure was made."
        )

    session.add(
        GstCreditReview(
            tenant_id=user.tenant_id,
            credit_id=row.id,
            decision=decision,
            rationale=rationale,
            decided_by_user_id=user.id,
            decided_on=decided_on,
        )
    )
    row.eligibility = decision
    row.eligibility_reason = (
        f"Reviewed by a person on {decided_on.isoformat()}: {rationale} "
        f"(the rules routed this to review: {row.eligibility_reason})"
    )
    row.eligibility_reference = "Finance review"
    row.claimable_amount = (
        _d(row.amount_at_stake) if decision is GstEligibilityEnum.ELIGIBLE else Decimal("0.00")
    )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE
        if decision is GstEligibilityEnum.ELIGIBLE
        else AuditAction.REJECT,
        entity_type="gst_input_credit",
        entity_id=row.id,
        actor=user,
        summary=f"Uncertain credit line decided {decision.value}: {rationale}",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return {
        "credit_id": str(row.id),
        "eligibility": row.eligibility.value,
        "claimable_amount": str(row.claimable_amount),
    }


async def review_queue(session: AsyncSession, user: User) -> dict[str, Any]:
    """Undecided lines, largest amount at stake first.

    Prioritised by money rather than by date, because the queue is worked
    through by a person with limited time and the expensive lines are the ones
    that matter.
    """
    rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(GstInputCredit)
                    .where(GstInputCredit.eligibility == GstEligibilityEnum.UNCERTAIN)
                    .order_by(GstInputCredit.amount_at_stake.desc()),
                    GstInputCredit,
                    user,
                )
            )
        ).scalars()
    )
    return {
        "count": len(rows),
        "total_at_stake": str(sum((_d(r.amount_at_stake) for r in rows), Decimal("0"))),
        "lines": [
            {
                "credit_id": str(r.id),
                "vendor_gstin": r.vendor_gstin,
                "invoice_number": r.invoice_number,
                "invoice_date": r.invoice_date.isoformat(),
                "category_key": r.category_key,
                "amount_at_stake": str(r.amount_at_stake),
                "reason": r.eligibility_reason,
            }
            for r in rows
        ],
        "note": (
            "Nothing here is decided either way. Guessing eligible claims "
            "credit the company may not be entitled to; guessing ineligible "
            "leaks money."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# GSTR-2B
# ══════════════════════════════════════════════════════════════════════════════


async def import_gstr2b(
    session: AsyncSession,
    user: User,
    *,
    period_label: str,
    lines: list[dict[str, Any]],
    imported_on: date,
    legal_entity_id: uuid.UUID | None = None,
    source_note: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Import a GSTR-2B download, stored as received.

    Nothing here connects to the portal; the file is supplied by the caller.
    """
    existing = (
        await session.execute(
            tenant_scoped(
                select(Gstr2bImport)
                .where(Gstr2bImport.period_label == period_label)
                .where(Gstr2bImport.legal_entity_id == legal_entity_id),
                Gstr2bImport,
                user,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _bad_request(
            f"GSTR-2B for {period_label} has already been imported. Importing "
            "it twice would double every line and turn a clean reconciliation "
            "into a register full of duplicates."
        )

    row = Gstr2bImport(
        tenant_id=user.tenant_id,
        legal_entity_id=legal_entity_id,
        period_label=period_label,
        imported_on=imported_on,
        imported_by_user_id=user.id,
        source_note=source_note,
    )
    session.add(row)
    await session.flush()

    total = Decimal("0")
    for spec in lines:
        tax = (
            _d(spec.get("cgst"))
            + _d(spec.get("sgst"))
            + _d(spec.get("igst"))
            + _d(spec.get("cess"))
        )
        total += tax
        session.add(
            Gstr2bLine(
                tenant_id=user.tenant_id,
                import_id=row.id,
                vendor_gstin=str(spec["vendor_gstin"]).upper(),
                vendor_name=spec.get("vendor_name"),
                invoice_number=str(spec["invoice_number"]),
                normalised_invoice_number=register_engine.normalise_invoice_number(
                    str(spec["invoice_number"])
                ),
                invoice_date=spec["invoice_date"],
                taxable_value=_d(spec.get("taxable_value")),
                cgst=_d(spec.get("cgst")),
                sgst=_d(spec.get("sgst")),
                igst=_d(spec.get("igst")),
                cess=_d(spec.get("cess")),
            )
        )
    row.line_count = len(lines)
    row.total_tax = total

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="gstr2b_import",
        entity_id=row.id,
        actor=user,
        summary=f"GSTR-2B {period_label} imported: {len(lines)} line(s).",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return {
        "import_id": str(row.id),
        "period_label": period_label,
        "line_count": row.line_count,
        "total_tax": str(row.total_tax),
    }


async def reconcile_period(
    session: AsyncSession,
    user: User,
    *,
    period_label: str,
    period_start: date,
    period_end: date,
    legal_entity_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Match the register against the imported 2B and report the rupee gap."""
    captured_rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(GstInputCredit)
                    .where(GstInputCredit.invoice_date >= period_start)
                    .where(GstInputCredit.invoice_date <= period_end),
                    GstInputCredit,
                    user,
                )
            )
        ).scalars()
    )
    import_row = (
        await session.execute(
            tenant_scoped(
                select(Gstr2bImport)
                .where(Gstr2bImport.period_label == period_label)
                .where(Gstr2bImport.legal_entity_id == legal_entity_id),
                Gstr2bImport,
                user,
            )
        )
    ).scalar_one_or_none()
    if import_row is None:
        raise _bad_request(
            f"No GSTR-2B has been imported for {period_label}. Reconciling "
            "against nothing would report every captured line as unfiled, which "
            "is a frightening number that means nothing."
        )

    portal_rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(Gstr2bLine).where(Gstr2bLine.import_id == import_row.id),
                    Gstr2bLine,
                    user,
                )
            )
        ).scalars()
    )

    report = register_engine.reconcile_2b(
        captured=[
            register_engine.CapturedInvoice(
                credit_id=str(r.id),
                vendor_gstin=r.vendor_gstin,
                invoice_number=r.invoice_number,
                invoice_date=r.invoice_date,
                taxable_value=_d(r.taxable_value),
                total_tax=_d(r.cgst) + _d(r.sgst) + _d(r.igst) + _d(r.cess),
                eligibility=r.eligibility.value,
                employee_id=str(r.employee_id) if r.employee_id else "",
            )
            for r in captured_rows
        ],
        portal=[
            register_engine.Gstr2bLine(
                vendor_gstin=r.vendor_gstin,
                invoice_number=r.invoice_number,
                invoice_date=r.invoice_date,
                taxable_value=_d(r.taxable_value),
                total_tax=_d(r.cgst) + _d(r.sgst) + _d(r.igst) + _d(r.cess),
            )
            for r in portal_rows
        ],
        period_label=period_label,
    )

    by_id = {str(r.id): r for r in captured_rows}
    for line in report["lines"]:
        row = by_id.get(line["credit_id"])
        if row is None:
            continue
        row.reconciliation_status = Gstr2bMatchEnum(
            line["state"] if line["state"] != "missing_in_register" else "matched"
        )
        if line["state"] == "matched":
            row.gstr2b_matched_at = period_end

    existing = (
        await session.execute(
            tenant_scoped(
                select(GstReconciliation)
                .where(GstReconciliation.period_label == period_label)
                .where(GstReconciliation.legal_entity_id == legal_entity_id),
                GstReconciliation,
                user,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _bad_request(
            f"{period_label} has already been reconciled. Re-running it would "
            "overwrite the record of what was found and who chased it."
        )

    session.add(
        GstReconciliation(
            tenant_id=user.tenant_id,
            legal_entity_id=legal_entity_id,
            period_label=period_label,
            captured_lines=report["captured_lines"],
            portal_lines=report["portal_lines"],
            matched_tax=Decimal(report["matched_tax"]),
            missing_in_2b_tax=Decimal(report["missing_in_2b_tax"]),
            missing_in_register_tax=Decimal(report["missing_in_register_tax"]),
            at_risk_tax=Decimal(report["at_risk_from_unfiled_vendors"]),
            counts=report["counts"],
            lines=report["lines"],
            run_on=period_end,
            run_by_user_id=user.id,
        )
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="gst_reconciliation",
        entity_id=user.tenant_id,
        actor=user,
        summary=(
            f"GST reconciliation {period_label}: "
            f"Rs {report['at_risk_from_unfiled_vendors']} at risk from unfiled "
            "vendors."
        ),
        request=request,
    )
    await session.commit()
    return report


# ══════════════════════════════════════════════════════════════════════════════
# Reporting
# ══════════════════════════════════════════════════════════════════════════════


async def _captured(
    session: AsyncSession,
    user: User,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> list[register_engine.CapturedInvoice]:
    stmt = tenant_scoped(select(GstInputCredit), GstInputCredit, user)
    if period_start is not None:
        stmt = stmt.where(GstInputCredit.invoice_date >= period_start)
    if period_end is not None:
        stmt = stmt.where(GstInputCredit.invoice_date <= period_end)
    return [
        register_engine.CapturedInvoice(
            credit_id=str(r.id),
            vendor_gstin=r.vendor_gstin,
            invoice_number=r.invoice_number,
            invoice_date=r.invoice_date,
            taxable_value=_d(r.taxable_value),
            total_tax=_d(r.cgst) + _d(r.sgst) + _d(r.igst) + _d(r.cess),
            eligibility=r.eligibility.value,
            employee_id=str(r.employee_id) if r.employee_id else "",
        )
        for r in (await session.execute(stmt)).scalars()
    ]


async def duplicate_report(session: AsyncSession, user: User) -> dict[str, Any]:
    """The same vendor invoice claimed more than once, across the tenant.

    Across the tenant, not per employee: two people submitting the same
    restaurant bill is the case that matters, and a per-employee check cannot
    see it.
    """
    found = register_engine.duplicates(await _captured(session, user))
    return {
        "duplicate_groups": len(found),
        "across_employees": sum(1 for d in found if d["across_employees"]),
        "duplicates": found,
        "note": (
            "Whether a duplicate is an error or fraud is not something this "
            "decides. It names both claims and both claimants."
        ),
    }


async def credit_dashboard(
    session: AsyncSession,
    user: User,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
    period_label: str = "",
) -> dict[str, Any]:
    """Captured, claimable and at-risk credit in rupees.

    Nothing is reported as confirmed until a GSTR-2B has been reconciled. A
    figure that turns out to be optimistic destroys the conversation this
    dashboard exists to have.
    """
    captured = await _captured(session, user, period_start=period_start, period_end=period_end)
    recon_row = None
    if period_label:
        recon_row = (
            await session.execute(
                tenant_scoped(
                    select(GstReconciliation).where(GstReconciliation.period_label == period_label),
                    GstReconciliation,
                    user,
                )
            )
        ).scalar_one_or_none()

    reconciliation = (
        {
            "matched_tax": str(recon_row.matched_tax),
            "at_risk_from_unfiled_vendors": str(recon_row.at_risk_tax),
        }
        if recon_row is not None
        else None
    )
    return register_engine.value_dashboard(
        captured=captured,
        reconciliation=reconciliation,
        period_label=period_label,
    )


async def register_export(
    session: AsyncSession,
    user: User,
    *,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """The register in the shape a CA prepares GSTR-3B from.

    Eligible lines only, with the uncertain ones listed separately rather than
    dropped: a CA who is not told about them cannot ask, and a line quietly
    omitted is the leak this module exists to close.
    """
    rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(GstInputCredit)
                    .where(GstInputCredit.invoice_date >= period_start)
                    .where(GstInputCredit.invoice_date <= period_end)
                    .order_by(GstInputCredit.invoice_date),
                    GstInputCredit,
                    user,
                )
            )
        ).scalars()
    )

    def line(r: GstInputCredit) -> dict[str, Any]:
        return {
            "vendor_gstin": r.vendor_gstin,
            "invoice_number": r.invoice_number,
            "invoice_date": r.invoice_date.isoformat(),
            "taxable_value": str(r.taxable_value),
            "cgst": str(r.cgst),
            "sgst": str(r.sgst),
            "igst": str(r.igst),
            "cess": str(r.cess),
            "hsn_sac": r.hsn_sac,
            "place_of_supply": r.place_of_supply_state_code,
            "eligibility": r.eligibility.value,
            "eligibility_reason": r.eligibility_reason,
            "reconciliation_status": r.reconciliation_status.value,
        }

    eligible = [r for r in rows if r.eligibility is GstEligibilityEnum.ELIGIBLE]
    uncertain = [r for r in rows if r.eligibility is GstEligibilityEnum.UNCERTAIN]

    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "eligible": [line(r) for r in eligible],
        "eligible_total_tax": str(
            sum(
                (_d(r.cgst) + _d(r.sgst) + _d(r.igst) for r in eligible),
                Decimal("0"),
            )
        ),
        "awaiting_review": [line(r) for r in uncertain],
        "note": (
            f"{len(uncertain)} line(s) are awaiting a finance decision and are "
            "listed separately rather than dropped. A CA who is not told about "
            "them cannot ask."
        ),
    }


def gst_posture() -> dict[str, Any]:
    """What this slice enforces, and what it does not."""
    return {
        "enforced": [
            "GSTIN structure and the modulus-36 checksum, which catches a "
            "single transposed character that a format check passes.",
            "A failed GSTIN flags the credit and never blocks the expense.",
            "Section 17(5) blocked categories, with exception-bearing "
            "categories routed to a person rather than decided.",
            "Place-of-supply logic, including intra-state supply in a state the "
            "entity is not registered in - the hotel-bill case.",
            "Uncertain lines stay uncertain until a person records a decision and a rationale.",
            "GSTR-2B matching on normalised invoice numbers, reporting matched, "
            "missing-in-2B and missing-in-register separately.",
            "Duplicate detection across the whole tenant, not per employee.",
        ],
        "not_built": [
            "**The blocked-category rules have not been reviewed by a tax "
            "advisor.** Every shipped rule carries reviewed=false; the reading "
            "and the exceptions are a starting point for that conversation.",
            "Where a tenant has recorded no GST registration of its own, the "
            "place of supply is taken as the recipient state - which means the "
            "other-state case cannot be detected for them until they record it.",
            "No GST portal integration. A GSTIN is never checked for active "
            "status, so a structurally valid number belonging to a cancelled "
            "registration passes.",
            "No receipt extraction. Invoice fields arrive from the caller; "
            "nothing reads them off an image.",
            "No GSTR-2B download. The file is imported by a person.",
            "Nothing files anything. The register is exportable and GSTR-3B "
            "preparation stays with the client's CA.",
            "No vendor follow-up. Unfiled invoices are surfaced with the rupee "
            "value; nothing chases the vendor.",
        ],
    }


async def require_own_expense_claim(
    session: AsyncSession, user: User, expense_claim_id: uuid.UUID
) -> None:
    """Outside finance, a GST invoice is captured against the caller's own claim.

    Deliberately not inside `capture_credit`: capture never refuses on anything
    about the invoice, and whether this person may attach it is an access
    decision rather than a verdict. The router asks this first.
    """
    if permissions.is_hr(user):
        return
    owner = await session.scalar(
        tenant_scoped(
            select(ExpenseClaim.employee_id).where(ExpenseClaim.id == expense_claim_id),
            ExpenseClaim,
            user,
        )
    )
    if owner is None or owner != user.employee_id:
        raise permissions.forbidden("That expense claim is not yours.")


# ══════════════════════════════════════════════════════════════════════════════
# Lists for the finance screens, and the register as a file
# ══════════════════════════════════════════════════════════════════════════════


async def list_credits(
    session: AsyncSession,
    user: User,
    *,
    eligibility: GstEligibilityEnum | None = None,
    reconciliation_status: Gstr2bMatchEnum | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    limit: int = 1000,
) -> list[GstInputCredit]:
    stmt = select(GstInputCredit)
    if eligibility is not None:
        stmt = stmt.where(GstInputCredit.eligibility == eligibility)
    if reconciliation_status is not None:
        stmt = stmt.where(GstInputCredit.reconciliation_status == reconciliation_status)
    if period_start is not None:
        stmt = stmt.where(GstInputCredit.invoice_date >= period_start)
    if period_end is not None:
        stmt = stmt.where(GstInputCredit.invoice_date <= period_end)
    stmt = stmt.order_by(GstInputCredit.invoice_date.desc()).limit(limit)
    return list((await session.execute(tenant_scoped(stmt, GstInputCredit, user))).scalars())


async def claim_credits(
    session: AsyncSession, user: User, expense_claim_id: uuid.UUID
) -> list[GstInputCredit]:
    """GST invoices captured against one claim — the claimant's own, or any for finance."""
    exists = await session.scalar(
        tenant_scoped(
            select(ExpenseClaim.id).where(ExpenseClaim.id == expense_claim_id),
            ExpenseClaim,
            user,
        )
    )
    if exists is None:
        raise _not_found("No such expense claim in this tenant.")
    if not permissions.is_hr(user):
        await require_own_expense_claim(session, user, expense_claim_id)
    stmt = tenant_scoped(
        select(GstInputCredit)
        .where(GstInputCredit.expense_claim_id == expense_claim_id)
        .order_by(GstInputCredit.invoice_date),
        GstInputCredit,
        user,
    )
    return list((await session.execute(stmt)).scalars())


async def list_entity_registrations(
    session: AsyncSession, user: User
) -> list[GstEntityRegistration]:
    stmt = tenant_scoped(
        select(GstEntityRegistration).order_by(GstEntityRegistration.state_code),
        GstEntityRegistration,
        user,
    )
    return list((await session.execute(stmt)).scalars())


async def list_blocked_categories(session: AsyncSession, user: User) -> list[GstBlockedCategory]:
    stmt = tenant_scoped(
        select(GstBlockedCategory).order_by(GstBlockedCategory.category_key),
        GstBlockedCategory,
        user,
    )
    return list((await session.execute(stmt)).scalars())


async def list_gstr2b_imports(session: AsyncSession, user: User) -> list[Gstr2bImport]:
    stmt = tenant_scoped(
        select(Gstr2bImport).order_by(Gstr2bImport.imported_on.desc()), Gstr2bImport, user
    )
    return list((await session.execute(stmt)).scalars())


async def list_reconciliations(session: AsyncSession, user: User) -> list[GstReconciliation]:
    stmt = tenant_scoped(
        select(GstReconciliation).order_by(GstReconciliation.run_on.desc()),
        GstReconciliation,
        user,
    )
    return list((await session.execute(stmt)).scalars())


async def register_csv(
    session: AsyncSession,
    user: User,
    *,
    period_start: date,
    period_end: date,
    request: Request | None = None,
) -> str:
    """The register as CSV, formulas neutralised, with the download recorded."""
    register = await register_export(
        session, user, period_start=period_start, period_end=period_end
    )
    content = csv_io.register_to_csv(register)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="gst_register",
        actor=user,
        changes={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "eligible_lines": len(register["eligible"]),
            "awaiting_review": len(register["awaiting_review"]),
        },
        summary=f"GST register exported for {period_start} to {period_end}.",
        request=request,
    )
    await session.commit()
    return content
