"""Offer construction, approval, release, tracking and conversion.

═══════════════════════════════════════════════════════════════════════════════
THE OFFER LETTER AND THE FIRST PAYSLIP CANNOT DISAGREE
═══════════════════════════════════════════════════════════════════════════════

This module never computes a salary component. It calls `resolve_ctc` — the
same function `assign_salary` calls before every payroll run — and stores what
comes back. Identity by construction, not by two implementations that were
checked once and then maintained separately.

It is worth being precise about why this matters more than it sounds. A
candidate reads the offer letter breakup carefully; it is the only document
they have. If the first payslip shows a different basic, they conclude the
company got their money wrong on day one, and they are not wrong to. It is a
common failure in this market and it is entirely avoidable.

On JOINED, conversion carries every field forward from the candidate and the
offer — including the salary, which is assigned through payroll's own
`assign_salary` with the offer's structure and CTC. Nothing is re-keyed, so
nothing can be re-keyed wrongly.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from dateutil.relativedelta import relativedelta
from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.documents import IssuedLetter, LetterStatus
from app.models.employee import Employee, EmployeeProfile, EmploymentStatus
from app.models.hiring import Candidate, CandidateStage, Requisition
from app.models.offer import (
    VACANCY_RELEASING_STATUSES,
    ApprovalDecision,
    ApprovalScope,
    Offer,
    OfferApproval,
    OfferComponent,
    OfferEvent,
    OfferSalaryComponent,
    OfferStatus,
)
from app.models.org import Location
from app.models.payroll import EmployeeSalary, SalaryStructure
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.payroll import EmployeeSalaryCreate
from app.services import audit
from app.services.documents import letter_merge, pdf
from app.services.documents import service as document_service
from app.services.hiring import (
    approval_chain,
    approvers,
    candidate_comms,
    careers,
    offer_terms,
    requisitions,
)
from app.services.notifications import dispatch
from app.services.payroll import service as payroll_service
from app.services.payroll.salary_structure import (
    SalaryStructureError,
    resolve_ctc,
)

__all__ = [
    "accept_offer",
    "approval_payload",
    "approve_offer",
    "convert_to_employee",
    "create_offer",
    "decline_offer",
    "lapse_due_offers",
    "record_drop",
    "release_offer",
    "revoke_offer",
    "submit_offer",
    "update_offer_terms",
]


# ═══════════════════════════════════════════════════════════════════════════
# Loading
# ═══════════════════════════════════════════════════════════════════════════


async def _offer(session: AsyncSession, user: User, offer_id: uuid.UUID) -> Offer:
    offer = await session.scalar(
        tenant_scoped(select(Offer).where(Offer.id == offer_id), Offer, user)
    )
    if offer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")
    return offer


async def _requisition(session: AsyncSession, user: User, requisition_id: uuid.UUID) -> Requisition:
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")
    return requisition


async def _structure(session: AsyncSession, user: User, structure_id: uuid.UUID) -> SalaryStructure:
    structure = await session.scalar(
        tenant_scoped(
            select(SalaryStructure).where(SalaryStructure.id == structure_id),
            SalaryStructure,
            user,
        )
    )
    if structure is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Salary structure not found"
        )
    return structure


async def _components(session: AsyncSession, user: User, offer: Offer) -> list[OfferComponent]:
    rows = await session.scalars(
        tenant_scoped(
            select(OfferComponent)
            .where(OfferComponent.offer_id == offer.id)
            .order_by(OfferComponent.kind),
            OfferComponent,
            user,
        )
    )
    return list(rows.all())


async def _material_terms(session: AsyncSession, user: User, offer: Offer) -> dict[str, Any]:
    """The offer's terms in the shape the fingerprint expects."""
    components = await _components(session, user, offer)
    return {
        "ctc_annual": Decimal(str(offer.ctc_annual)),
        "salary_structure_id": offer.salary_structure_id,
        "grade": offer.grade,
        "designation": offer.designation,
        "joining_date": offer.joining_date,
        "joining_location_id": offer.joining_location_id,
        "probation_months": offer.probation_months,
        "notice_period_days": offer.notice_period_days,
        "offer_components": [
            {
                "kind": c.kind.value,
                "amount": Decimal(str(c.amount)),
                "counts_toward_ctc": c.counts_toward_ctc,
            }
            for c in sorted(components, key=lambda c: (c.kind.value, str(c.id)))
        ],
    }


async def _record_event(
    session: AsyncSession,
    user: User,
    offer: Offer,
    *,
    to_status: OfferStatus,
    reason: str | None = None,
    facts: dict | None = None,
) -> None:
    """Insert-only status history.

    Drop rate by department, recruiter and notice period is a slice 36 report,
    and it can only be computed from transitions that were written down when
    they happened.
    """
    session.add(
        OfferEvent(
            tenant_id=user.tenant_id,
            offer_id=offer.id,
            from_status=offer.status,
            to_status=to_status,
            reason=reason,
            actor_user_id=user.id,
            occurred_at=datetime.now(UTC),
            facts=facts or {},
        )
    )


# ═══════════════════════════════════════════════════════════════════════════
# Construction
# ═══════════════════════════════════════════════════════════════════════════


async def _write_breakup(
    session: AsyncSession, user: User, offer: Offer, structure: SalaryStructure
) -> None:
    """Resolve the CTC through payroll's own engine and store the result.

    Every amount on the offer letter comes from `resolve_ctc`. This module does
    no arithmetic on components at all — the moment it did, the letter and the
    payslip would be two implementations of the same rule, and they would drift.
    """
    try:
        breakdown = resolve_ctc(structure, Decimal(str(offer.ctc_annual)))
    except SalaryStructureError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    existing = await session.scalars(
        tenant_scoped(
            select(OfferSalaryComponent).where(OfferSalaryComponent.offer_id == offer.id),
            OfferSalaryComponent,
            user,
        )
    )
    for row in existing.all():
        await session.delete(row)
    await session.flush()

    for resolved in breakdown.components:
        session.add(
            OfferSalaryComponent(
                tenant_id=user.tenant_id,
                offer_id=offer.id,
                component_code=resolved.code,
                component_name=resolved.name,
                annual_amount=resolved.annual_amount,
                monthly_amount=resolved.monthly_amount,
                part_of_ctc=resolved.part_of_ctc,
                display_order=resolved.display_order,
            )
        )
    await session.flush()


async def create_offer(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    salary_structure_id: uuid.UUID,
    ctc_annual: Decimal,
    joining_date: date,
    designation: str,
    code: str,
    grade: str | None = None,
    joining_location_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    probation_months: int = 6,
    notice_period_days: int = 60,
    valid_until: date | None = None,
    components: list[dict] | None = None,
    request: Request | None = None,
) -> Offer:
    """Draft an offer, resolving its breakup through payroll's engine."""
    candidate = await session.scalar(
        tenant_scoped(select(Candidate).where(Candidate.id == candidate_id), Candidate, user)
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

    requisition = await _requisition(session, user, candidate.requisition_id)
    structure = await _structure(session, user, salary_structure_id)

    live = await session.scalar(
        tenant_scoped(
            select(func.count())
            .select_from(Offer)
            .where(
                Offer.candidate_id == candidate_id,
                Offer.status.notin_(list(VACANCY_RELEASING_STATUSES)),
            ),
            Offer,
            user,
        )
    )
    if live:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This candidate already has a live offer. Revoke it before drafting "
                "another — two live offers to one person is a conversation nobody "
                "wants to have."
            ),
        )

    offer = Offer(
        tenant_id=user.tenant_id,
        code=code,
        candidate_id=candidate.id,
        requisition_id=requisition.id,
        salary_structure_id=structure.id,
        ctc_annual=ctc_annual,
        joining_date=joining_date,
        joining_location_id=joining_location_id or requisition.location_id,
        department_id=department_id or requisition.department_id,
        legal_entity_id=legal_entity_id,
        grade=grade or requisition.grade,
        designation=designation,
        probation_months=probation_months,
        notice_period_days=notice_period_days,
        valid_until=valid_until,
        created_by_user_id=user.id,
    )
    session.add(offer)
    await session.flush()

    for spec in components or []:
        session.add(
            OfferComponent(
                tenant_id=user.tenant_id,
                offer_id=offer.id,
                kind=spec["kind"],
                label=spec.get("label") or str(spec["kind"]),
                amount=Decimal(str(spec["amount"])),
                payable_terms=spec.get("payable_terms"),
                counts_toward_ctc=bool(spec.get("counts_toward_ctc", False)),
            )
        )
    await session.flush()

    await _write_breakup(session, user, offer, structure)
    await _record_event(session, user, offer, to_status=OfferStatus.DRAFT)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="offer",
        entity_id=offer.id,
        summary=f"Offer {offer.code} drafted for {candidate.full_name} at {ctc_annual}",
        request=request,
    )
    return offer


async def update_offer_terms(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    changes: dict[str, Any],
    request: Request | None = None,
) -> dict:
    """Edit an offer, voiding every approval if a material term moved.

    Voided, not flagged. An approval is a decision about specific numbers, and
    reusing it after those numbers change means the approver is on record for
    something they never saw. A warning here would be closed; a void sends it
    back round.
    """
    offer = await _offer(session, user, offer_id)
    if offer.status in (
        OfferStatus.ACCEPTED,
        OfferStatus.JOINED,
        OfferStatus.DECLINED,
        OfferStatus.REVOKED,
        OfferStatus.LAPSED,
        OfferStatus.DROPPED,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This offer is {offer.status.value}. Editing the terms of an offer the "
                "candidate has already responded to is a new offer, not an edit."
            ),
        )

    before = await _material_terms(session, user, offer)

    editable = {
        "ctc_annual",
        "salary_structure_id",
        "grade",
        "designation",
        "joining_date",
        "joining_location_id",
        "probation_months",
        "notice_period_days",
        "valid_until",
    }
    unknown = set(changes) - editable
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"These fields cannot be edited on an offer: {sorted(unknown)}",
        )
    for field, value in changes.items():
        setattr(offer, field, value)
    await session.flush()

    if "ctc_annual" in changes or "salary_structure_id" in changes:
        structure = await _structure(session, user, offer.salary_structure_id)
        await _write_breakup(session, user, offer, structure)

    after = await _material_terms(session, user, offer)
    changed = offer_terms.material_changes(before, after)

    voided = 0
    if changed:
        approvals = await session.scalars(
            tenant_scoped(
                select(OfferApproval).where(OfferApproval.offer_id == offer.id),
                OfferApproval,
                user,
            )
        )
        for approval in approvals.all():
            if approval.decision is ApprovalDecision.APPROVED:
                approval.decision = ApprovalDecision.VOIDED
                approval.voided_by_changes = changed
                voided += 1
            elif approval.decision is ApprovalDecision.PENDING:
                approval.voided_by_changes = changed
        if voided and offer.status is OfferStatus.APPROVED:
            offer.status = OfferStatus.PENDING_APPROVAL
        await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="offer",
        entity_id=offer.id,
        summary=(
            f"Offer {offer.code} edited"
            + (
                f"; {voided} approval(s) voided because these terms changed: {', '.join(changed)}"
                if voided
                else ""
            )
        ),
        changes={"material_changes": changed},
        request=request,
    )
    return {
        "offer_id": str(offer.id),
        "status": offer.status.value,
        "material_changes": changed,
        "approvals_voided": voided,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Approval
# ═══════════════════════════════════════════════════════════════════════════


async def _comparable_ctcs(session: AsyncSession, user: User, offer: Offer) -> list[Decimal]:
    """What comparable employees currently earn.

    Same grade, same department where the offer names one. Only live salary
    rows — a closed revision is what someone used to earn, and comparing
    against history would make every offer look generous.
    """
    if not offer.grade:
        return []

    conditions = [
        Employee.grade == offer.grade,
        Employee.status != EmploymentStatus.EXITED,
        EmployeeSalary.effective_to.is_(None),
    ]
    if offer.department_id is not None:
        conditions.append(Employee.department_id == offer.department_id)

    rows = await session.scalars(
        tenant_scoped(
            select(EmployeeSalary.ctc_annual)
            .join(Employee, Employee.id == EmployeeSalary.employee_id)
            .where(*conditions),
            EmployeeSalary,
            user,
        )
    )
    return [Decimal(str(value)) for value in rows.all()]


async def approval_payload(session: AsyncSession, user: User, *, offer_id: uuid.UUID) -> dict:
    """Everything an approver needs, assembled server-side.

    Deviation and parity come back in one call with the offer. Fetched
    separately they arrive at different moments, and the approver reads the
    number before the flag lands underneath it.
    """
    offer = await _offer(session, user, offer_id)
    requisition = await _requisition(session, user, offer.requisition_id)
    candidate = await session.get(Candidate, offer.candidate_id)

    deviation = offer_terms.budget_deviation(
        offered_ctc=Decimal(str(offer.ctc_annual)),
        band_min=(
            None if requisition.budget_ctc_min is None else Decimal(str(requisition.budget_ctc_min))
        ),
        band_max=(
            None if requisition.budget_ctc_max is None else Decimal(str(requisition.budget_ctc_max))
        ),
    )
    parity = offer_terms.internal_parity(
        offered_ctc=Decimal(str(offer.ctc_annual)),
        comparable_ctcs=await _comparable_ctcs(session, user, offer),
        grade=offer.grade,
    )
    breakup = await session.scalars(
        tenant_scoped(
            select(OfferSalaryComponent)
            .where(OfferSalaryComponent.offer_id == offer.id)
            .order_by(OfferSalaryComponent.display_order),
            OfferSalaryComponent,
            user,
        )
    )
    approvals = await session.scalars(
        tenant_scoped(
            select(OfferApproval)
            .where(OfferApproval.offer_id == offer.id)
            .order_by(OfferApproval.sequence),
            OfferApproval,
            user,
        )
    )

    return {
        "offer": {
            "id": str(offer.id),
            "code": offer.code,
            "status": offer.status.value,
            "ctc_annual": str(offer.ctc_annual),
            "grade": offer.grade,
            "designation": offer.designation,
            "joining_date": offer.joining_date.isoformat(),
        },
        "candidate": {
            "id": str(candidate.id) if candidate else None,
            "name": candidate.full_name if candidate else None,
            "current_ctc": (
                str(candidate.current_ctc) if candidate and candidate.current_ctc else None
            ),
            "notice_period_days": candidate.notice_period_days if candidate else None,
        },
        "requisition": {
            "id": str(requisition.id),
            "code": requisition.code,
            "budget_ctc_min": (
                str(requisition.budget_ctc_min) if requisition.budget_ctc_min else None
            ),
            "budget_ctc_max": (
                str(requisition.budget_ctc_max) if requisition.budget_ctc_max else None
            ),
            "open_vacancies": requisition.open_vacancies,
        },
        # First, deliberately: this is the thing that gets approved without
        # being noticed when it is anywhere else on the page.
        "budget_deviation": deviation.as_dict(),
        "internal_parity": parity.as_dict(),
        "salary_breakup": [
            {
                "code": c.component_code,
                "name": c.component_name,
                "monthly": str(c.monthly_amount),
                "annual": str(c.annual_amount),
                "part_of_ctc": c.part_of_ctc,
            }
            for c in breakup.all()
        ],
        "approvals": [
            {
                "sequence": a.sequence,
                "role": a.approver_role,
                "decision": a.decision.value,
                "reason": a.reason,
                "voided_by_changes": a.voided_by_changes,
            }
            for a in approvals.all()
        ],
    }


async def submit_offer(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    request: Request | None = None,
) -> dict:
    """Send an offer for approval, with the chain resolved from the decision."""
    offer = await _offer(session, user, offer_id)
    if offer.status not in (OfferStatus.DRAFT, OfferStatus.PENDING_APPROVAL):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This offer is {offer.status.value}, not a draft.",
        )

    context = await approval_payload(session, user, offer_id=offer_id)
    over_band = context["budget_deviation"]["is_over_band"]

    rules = await requisitions.load_rules(session, user, ApprovalScope.OFFER)
    try:
        approval_chain.validate_rules(rules)
        chain = approval_chain.resolve_chain(
            rules,
            ctc_annual=Decimal(str(offer.ctc_annual)),
            grade=offer.grade,
            over_band=over_band,
        )
    except approval_chain.ApprovalChainError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    terms = await _material_terms(session, user, offer)
    fingerprint = offer_terms.terms_fingerprint(terms)

    existing = await session.scalars(
        tenant_scoped(
            select(OfferApproval).where(OfferApproval.offer_id == offer.id),
            OfferApproval,
            user,
        )
    )
    for row in existing.all():
        await session.delete(row)
    await session.flush()

    shown = {
        "budget_deviation": context["budget_deviation"],
        "internal_parity": context["internal_parity"],
    }
    for step in chain:
        session.add(
            OfferApproval(
                tenant_id=user.tenant_id,
                offer_id=offer.id,
                sequence=step.sequence,
                approver_role=step.approver_role,
                label=step.label,
                reason=step.reason,
                terms_fingerprint=fingerprint,
                context_shown=shown,
            )
        )

    await _record_event(session, user, offer, to_status=OfferStatus.PENDING_APPROVAL)
    offer.status = OfferStatus.PENDING_APPROVAL
    offer.approval_context = shown
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="offer",
        entity_id=offer.id,
        summary=(
            f"Offer {offer.code} sent for approval to {len(chain)} approver(s)"
            + (" — over the requisition's band" if over_band else "")
        ),
        changes={"chain": [s.approver_role for s in chain], "over_band": over_band},
        request=request,
    )
    return {
        "offer_id": str(offer.id),
        "status": offer.status.value,
        "over_band": over_band,
        "chain": [
            {"sequence": s.sequence, "role": s.approver_role, "reason": s.reason} for s in chain
        ],
        "terms_fingerprint": fingerprint,
    }


async def approve_offer(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> dict:
    """Record one approver's decision against the terms in front of them."""
    offer = await _offer(session, user, offer_id)
    if offer.status is not OfferStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This offer is {offer.status.value}, not awaiting approval.",
        )

    steps = (
        await session.scalars(
            tenant_scoped(
                select(OfferApproval)
                .where(OfferApproval.offer_id == offer.id)
                .order_by(OfferApproval.sequence),
                OfferApproval,
                user,
            )
        )
    ).all()
    pending = next(
        (s for s in steps if s.decision in (ApprovalDecision.PENDING, ApprovalDecision.VOIDED)),
        None,
    )
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Every approval step on this offer has already been decided.",
        )

    # Nobody signs an offer they drafted, and a step is signed only by somebody
    # holding the role it names. Without this the route let any manager decide
    # any step, which made the chain a list of names rather than signatures.
    requisition = await _requisition(session, user, offer.requisition_id)
    authority = approvers.can_decide_step(
        approver_role=pending.approver_role,
        user_role=user.role.value,
        has_reports=user.has_reports,
        is_hiring_manager=(
            user.employee_id is not None and user.employee_id == requisition.hiring_manager_id
        ),
        is_originator=offer.created_by_user_id is not None and offer.created_by_user_id == user.id,
    )
    if not authority.allowed:
        raise permissions.forbidden(authority.message)
    if not approve and not (note or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Sending an offer back needs a note. Whoever drafted it has to know what "
                "would change the answer."
            ),
        )
    if authority.on_behalf_of:
        note = f"{note or ''} [signed on behalf of {authority.on_behalf_of}]".strip()

    # The approver signs the terms as they stand right now, not as they stood
    # when the step was created.
    fingerprint = offer_terms.terms_fingerprint(await _material_terms(session, user, offer))
    pending.decision = ApprovalDecision.APPROVED if approve else ApprovalDecision.REJECTED
    pending.decided_by_user_id = user.id
    pending.decided_at = datetime.now(UTC)
    pending.note = note
    pending.terms_fingerprint = fingerprint
    pending.voided_by_changes = []

    if not approve:
        await _record_event(session, user, offer, to_status=OfferStatus.DRAFT, reason=note)
        offer.status = OfferStatus.DRAFT
        remaining = 0
    else:
        remaining = sum(
            1 for s in steps if s.decision in (ApprovalDecision.PENDING, ApprovalDecision.VOIDED)
        )
        if remaining == 0:
            await _record_event(session, user, offer, to_status=OfferStatus.APPROVED)
            offer.status = OfferStatus.APPROVED

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="offer",
        entity_id=offer.id,
        summary=(
            f"Offer {offer.code} {'approved' if approve else 'rejected'} at step "
            f"{pending.sequence} ({pending.approver_role})"
        ),
        changes={"note": note, "context_shown": pending.context_shown},
        request=request,
    )
    return {
        "offer_id": str(offer.id),
        "status": offer.status.value,
        "step": pending.sequence,
        "remaining_steps": remaining,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Release and response
# ═══════════════════════════════════════════════════════════════════════════


def _place(location: Location | None) -> str | None:
    """ "Bengaluru HQ, Bengaluru"; just "Bengaluru" when the name already says it."""
    if location is None:
        return None
    if location.city and location.city.lower() not in location.name.lower():
        return f"{location.name}, {location.city}"
    return location.name


async def _offer_letter(
    session: AsyncSession,
    user: User,
    offer: Offer,
    candidate: Candidate,
    *,
    template_id: uuid.UUID,
) -> IssuedLetter:
    """Render the offer letter through the letter engine.

    Same merge engine, same numbering series, same content hash, same template
    versioning as every other letter. A separate offer-letter renderer would
    have drifted from this one inside a release — and the CTC table is the same
    computed handler an appointment letter uses, so the two cannot disagree
    either.
    """
    template = await document_service.get_template(session, user, template_id)
    breakup = await session.scalars(
        tenant_scoped(
            select(OfferSalaryComponent)
            .where(OfferSalaryComponent.offer_id == offer.id)
            .order_by(OfferSalaryComponent.display_order),
            OfferSalaryComponent,
            user,
        )
    )
    components = [
        {
            "code": c.component_code,
            "name": c.component_name,
            "monthly_amount": Decimal(str(c.monthly_amount)),
            "annual_amount": Decimal(str(c.annual_amount)),
            "part_of_ctc": c.part_of_ctc,
        }
        for c in breakup.all()
    ]
    if not components:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This offer has no salary breakup; it cannot produce a letter.",
        )

    tenant = await session.get(Tenant, user.tenant_id)
    location = (
        await session.scalar(
            tenant_scoped(
                select(Location).where(Location.id == offer.joining_location_id), Location, user
            )
        )
        if offer.joining_location_id
        else None
    )

    context = letter_merge.MergeContext(
        as_of=date.today(),
        # The candidate stands in the employee slot: they are the recipient,
        # and every offer-letter token an HR admin writes reads naturally as
        # {{ employee.first_name }} because that is what the other templates use.
        employee={
            "first_name": candidate.full_name.split(" ")[0],
            "last_name": " ".join(candidate.full_name.split(" ")[1:]),
            "display_name": candidate.full_name,
            "employee_code": offer.code,
            "work_email": candidate.email,
            "designation": offer.designation,
            "grade": offer.grade,
            "date_of_joining": offer.joining_date,
            "probation_end_date": None,
            "notice_period_days": offer.notice_period_days,
        },
        salary={
            "ctc_annual": Decimal(str(offer.ctc_annual)),
            "currency": offer.currency,
            "effective_from": offer.joining_date,
        },
        tenant={"name": tenant.name if tenant else "", "slug": tenant.slug if tenant else ""},
        # An offer is released, not issued, so nobody types a template's manual
        # fields in: the ones an offer letter asks for come from the offer.
        manual={
            "valid_until": offer.valid_until,
            "offer_code": offer.code,
            "joining_date": offer.joining_date,
            "work_location": _place(location),
            "acceptance_deadline": offer.valid_until,
        },
        salary_components=components,
    )

    try:
        merged = letter_merge.render_template(
            body_html=template.body_html,
            header_html=template.header_html,
            footer_html=template.footer_html,
            context=context,
            required_tokens={f.token for f in template.fields if f.required} or None,
        )
    except letter_merge.LetterMergeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    reference = await document_service.allocate_reference_number(
        session, user, template.category, date.today()
    )
    letter = IssuedLetter(
        tenant_id=user.tenant_id,
        employee_id=None,
        candidate_id=candidate.id,
        template_id=template.id,
        template_version=template.version,
        category=template.category,
        reference_number=reference,
        merged_html=merged,
        content_hash=pdf.content_hash(merged),
        manual_values={"offer_code": offer.code},
        status=LetterStatus.SENT,
        issued_by_user_id=user.id,
        issued_at=datetime.now(UTC),
    )
    session.add(letter)
    await session.flush()
    return letter


async def release_offer(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    template_id: uuid.UUID,
    valid_until: date | None = None,
    request: Request | None = None,
) -> dict:
    """Generate the letter, take the vacancy, and start the validity clock."""
    offer = await _offer(session, user, offer_id)
    if offer.status is not OfferStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This offer is {offer.status.value}. Only an approved offer can be "
                "released — releasing an unapproved one is exactly what the chain is for."
            ),
        )

    candidate = await session.get(Candidate, offer.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

    requisition = await _requisition(session, user, offer.requisition_id)
    await requisitions.reserve_vacancy(session, user, requisition=requisition)

    # Before the letter is rendered: its acceptance deadline is this date.
    if valid_until is not None:
        offer.valid_until = valid_until
    letter = await _offer_letter(session, user, offer, candidate, template_id=template_id)

    if offer.valid_until is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "An offer needs a validity date. Without one it never lapses, and a "
                "candidate who went quiet in March still shows as live in September."
            ),
        )

    await _record_event(session, user, offer, to_status=OfferStatus.RELEASED)
    offer.status = OfferStatus.RELEASED
    offer.released_at = datetime.now(UTC)
    offer.letter_id = letter.id
    candidate.stage = CandidateStage.OFFER
    await session.flush()

    await _notify(
        session,
        offer,
        requisition,
        event_key="offer.released",
        payload={"offer_code": offer.code, "candidate": candidate.full_name},
    )
    _portal, portal_token = await careers.issue_portal_token(
        session, user, candidate_id=candidate.id
    )
    await candidate_comms.on_portal_link(
        session,
        user,
        candidate=candidate,
        token=portal_token,
        event_key=candidate_comms.OFFER_RELEASED,
        payload={"valid_until": f"{offer.valid_until:%d %B %Y}"},
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="offer",
        entity_id=offer.id,
        summary=(
            f"Offer {offer.code} released to {candidate.full_name}, valid until "
            f"{offer.valid_until}; letter {letter.reference_number}"
        ),
        request=request,
    )
    return {
        "offer_id": str(offer.id),
        "status": offer.status.value,
        "letter_id": str(letter.id),
        "reference_number": letter.reference_number,
        "valid_until": offer.valid_until.isoformat(),
    }


async def _notify(
    session: AsyncSession,
    offer: Offer,
    requisition: Requisition,
    *,
    event_key: str,
    payload: dict,
) -> None:
    """Tell the recruiter and the hiring manager.

    Best-effort: a missing notification template must not roll back an offer
    transition that already happened. The transition is the record; the message
    is a courtesy on top of it.
    """
    employee_ids = [
        e for e in (requisition.recruiter_id, requisition.hiring_manager_id) if e is not None
    ]
    if not employee_ids:
        return
    recipients = list(
        (
            await session.scalars(
                select(User.id).where(
                    tenant_filter(User, offer.tenant_id),
                    User.employee_id.in_(employee_ids),
                )
            )
        ).all()
    )
    if not recipients:
        return
    await dispatch.notify(
        session,
        tenant_id=offer.tenant_id,
        event_key=event_key,
        recipient_user_ids=recipients,
        payload=payload,
        commit=False,
    )


async def _respond(
    session: AsyncSession,
    user: User,
    offer: Offer,
    *,
    to_status: OfferStatus,
    reason: str | None,
    request: Request | None,
) -> dict:
    """Shared terminal transition: history, vacancy, audit."""
    requisition = await _requisition(session, user, offer.requisition_id)
    candidate = await session.get(Candidate, offer.candidate_id)

    facts = {
        # Snapshotted so drop rate by notice period stays computable years
        # later, whatever happens to the candidate record.
        "notice_period_days": candidate.notice_period_days if candidate else None,
        "department_id": str(offer.department_id) if offer.department_id else None,
        "recruiter_id": str(requisition.recruiter_id) if requisition.recruiter_id else None,
        "grade": offer.grade,
        "days_since_release": (
            (datetime.now(UTC) - offer.released_at).days if offer.released_at else None
        ),
    }
    await _record_event(session, user, offer, to_status=to_status, reason=reason, facts=facts)
    offer.status = to_status
    offer.responded_at = datetime.now(UTC)
    offer.outcome_reason = reason

    if to_status in VACANCY_RELEASING_STATUSES:
        await requisitions.release_vacancy(
            session, user, requisition=requisition, reason=f"offer {to_status.value}"
        )
    await session.flush()

    await _notify(
        session,
        offer,
        requisition,
        event_key=f"offer.{to_status.value}",
        payload={"offer_code": offer.code, "reason": reason or ""},
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="offer",
        entity_id=offer.id,
        summary=f"Offer {offer.code} {to_status.value}" + (f": {reason}" if reason else ""),
        request=request,
    )
    return {"offer_id": str(offer.id), "status": offer.status.value, "reason": reason}


async def accept_offer(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    signature_reference: str | None = None,
    request: Request | None = None,
) -> dict:
    offer = await _offer(session, user, offer_id)
    if offer.status is not OfferStatus.RELEASED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This offer is {offer.status.value}; only a released offer can be accepted.",
        )
    if offer.valid_until and date.today() > offer.valid_until:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This offer lapsed on {offer.valid_until}. Extend the validity date "
                "rather than accepting an expired offer — the extension is a decision "
                "somebody should make on purpose."
            ),
        )
    result = await _respond(
        session,
        user,
        offer,
        to_status=OfferStatus.ACCEPTED,
        reason=signature_reference,
        request=request,
    )
    return result


async def decline_offer(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> dict:
    offer = await _offer(session, user, offer_id)
    if offer.status is not OfferStatus.RELEASED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This offer is {offer.status.value}; only a released offer can be declined.",
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A decline needs a reason. Decline rate by reason is how a company "
                "finds out its offers are 15% under market, and it cannot be "
                "reconstructed later."
            ),
        )
    return await _respond(
        session, user, offer, to_status=OfferStatus.DECLINED, reason=reason, request=request
    )


async def record_drop(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> dict:
    """Accepted, then did not join.

    A distinct status from DECLINED because the two cost completely different
    amounts: a decline costs a search, a drop costs a search plus a start date
    the team already planned around.
    """
    offer = await _offer(session, user, offer_id)
    if offer.status is not OfferStatus.ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This offer is {offer.status.value}. A drop is an accepted offer that "
                "did not join; anything else is a decline."
            ),
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A drop needs a reason. Offer-drop rate by department, recruiter and "
                "notice period is a material metric in this market, and a reason that "
                "lives in a recruiter's memory cannot be reported on."
            ),
        )
    return await _respond(
        session, user, offer, to_status=OfferStatus.DROPPED, reason=reason, request=request
    )


async def revoke_offer(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    reason: str,
    candidate_resigned_elsewhere: bool = False,
    request: Request | None = None,
) -> dict:
    """Withdraw an offer before joining, reopening the vacancy.

    Where the candidate has already resigned elsewhere the revocation carries
    legal and reputational exposure, so it comes back as a flag for HR rather
    than a line in a log nobody reads.
    """
    offer = await _offer(session, user, offer_id)
    if offer.status in (OfferStatus.JOINED, OfferStatus.DROPPED, OfferStatus.DECLINED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This offer is {offer.status.value} and cannot be revoked.",
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A revocation needs a reason.",
        )

    offer.resigned_elsewhere = candidate_resigned_elsewhere
    result = await _respond(
        session, user, offer, to_status=OfferStatus.REVOKED, reason=reason, request=request
    )

    if candidate_resigned_elsewhere:
        candidate = await session.get(Candidate, offer.candidate_id)
        result["hr_escalation"] = (
            f"{candidate.full_name if candidate else 'This candidate'} has already "
            "resigned from their current employer against this offer. Revoking it "
            "leaves them without a job and carries legal and reputational exposure. "
            "HR should be involved before this is communicated."
        )
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="offer",
            entity_id=offer.id,
            summary=(
                f"Offer {offer.code} revoked after the candidate resigned elsewhere — "
                "escalated to HR"
            ),
            request=request,
        )
    return result


async def lapse_due_offers(session: AsyncSession, user: User, *, as_of: date | None = None) -> dict:
    """Expire released offers past their validity date.

    Run on a schedule. An offer that never lapses shows a candidate who went
    quiet in March as still live in September, and the requisition as still
    filled.
    """
    today = as_of or date.today()
    due = (
        await session.scalars(
            tenant_scoped(
                select(Offer).where(
                    Offer.status == OfferStatus.RELEASED,
                    Offer.valid_until.is_not(None),
                    Offer.valid_until < today,
                ),
                Offer,
                user,
            )
        )
    ).all()

    lapsed = []
    for offer in due:
        await _respond(
            session,
            user,
            offer,
            to_status=OfferStatus.LAPSED,
            reason=f"Validity expired on {offer.valid_until}; no response received.",
            request=None,
        )
        lapsed.append(offer.code)
    return {"lapsed": len(lapsed), "codes": lapsed}


# ═══════════════════════════════════════════════════════════════════════════
# Conversion
# ═══════════════════════════════════════════════════════════════════════════


async def convert_to_employee(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    employee_code: str,
    work_email: str,
    request: Request | None = None,
) -> dict:
    """Turn an accepted offer into an employee, re-keying nothing.

    Every field comes from the candidate or the offer. The salary is assigned
    through payroll's own `assign_salary` with the offer's structure and CTC, so
    the first payslip resolves from the identical inputs the offer letter did.

    Only the employee code and the work email are supplied here, because
    neither exists before joining — everything else already does, and typing it
    again is how a joining date becomes 2026-06-01 in one system and 2026-01-06
    in another.
    """
    offer = await _offer(session, user, offer_id)
    if offer.status is not OfferStatus.ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This offer is {offer.status.value}. Only an accepted offer converts "
                "to an employee."
            ),
        )
    if offer.converted_employee_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This offer has already been converted.",
        )

    candidate = await session.get(Candidate, offer.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

    names = candidate.full_name.split(" ")
    # Months, not 30-day blocks: a naive 30-day month puts a six-month
    # probation six days early, which is a real date somebody is confirmed on.
    probation_end = offer.joining_date + relativedelta(months=offer.probation_months)
    # Pre-joining until the joining date arrives (India's today, not the
    # server's); the onboarding scheduler job activates them on the day.
    from app.services.onboarding import rules as onboarding_rules

    employment_status = onboarding_rules.joining_status(
        offer.joining_date, onboarding_rules.india_today()
    )

    employee = Employee(
        tenant_id=user.tenant_id,
        employee_code=employee_code,
        first_name=names[0],
        last_name=" ".join(names[1:]) or None,
        display_name=candidate.full_name,
        work_email=work_email,
        personal_email=candidate.email,
        phone=candidate.phone,
        legal_entity_id=offer.legal_entity_id,
        location_id=offer.joining_location_id,
        department_id=offer.department_id,
        designation=offer.designation,
        grade=offer.grade,
        status=employment_status,
        date_of_joining=offer.joining_date,
        probation_end_date=probation_end,
        notice_period_days=offer.notice_period_days,
    )
    session.add(employee)
    await session.flush()

    # The pre-hire record travels with them, which is what makes interview
    # signal queryable against post-hire outcomes rather than thrown away at
    # the moment they become an employee. It goes into the HR-only
    # `hiring_snapshot`, not `custom_fields`: custom fields are shown on the
    # person's own profile, and their fit score and the CTC they were offered
    # against are HR's working, not self-service.
    session.add(
        EmployeeProfile(
            tenant_id=user.tenant_id,
            employee_id=employee.id,
            personal={},
            addresses={},
            emergency_contacts=[],
            family_members=[],
            nominees=[],
            education=[],
            previous_employment=[],
            hiring_snapshot={
                "source": candidate.source,
                "offer_code": offer.code,
                "candidate_id": str(candidate.id),
                "expected_ctc": (str(candidate.expected_ctc) if candidate.expected_ctc else None),
                "current_ctc_at_offer": (
                    str(candidate.current_ctc) if candidate.current_ctc else None
                ),
                "notice_period_days_at_offer": candidate.notice_period_days,
                "fit_score": str(candidate.fit_score) if candidate.fit_score else None,
            },
        )
    )
    await session.flush()

    # Payroll's own assignment path, with the offer's structure and CTC. Not a
    # copy of the offer's stored breakup: running the same inputs through the
    # same function is what makes the two provably identical rather than
    # identical-looking.
    await payroll_service.assign_salary(
        session,
        user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=offer.salary_structure_id,
            ctc_annual=Decimal(str(offer.ctc_annual)),
            effective_from=offer.joining_date,
            revision_reason=f"Joining under offer {offer.code}",
        ),
        request=request,
    )

    candidate.converted_employee_id = employee.id
    candidate.stage = CandidateStage.HIRED
    await _record_event(session, user, offer, to_status=OfferStatus.JOINED)
    offer.status = OfferStatus.JOINED
    offer.converted_employee_id = employee.id
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="employee",
        entity_id=employee.id,
        summary=(
            f"{candidate.full_name} joined under offer {offer.code} as {employee_code}; "
            "employee record and salary carried forward from the offer"
        ),
        changes={
            "offer_id": str(offer.id),
            "candidate_id": str(candidate.id),
            "status": employment_status.value,
        },
        request=request,
    )

    # The tenant's onboarding checklist, when it keeps a default one. Never
    # blocks the conversion: a refusal there is dropped in its own savepoint.
    from app.services.onboarding import service as onboarding_service

    plan = await onboarding_service.auto_start_on_conversion(
        session, user, employee, request=request
    )
    return {
        "offer_id": str(offer.id),
        "employee_id": str(employee.id),
        "employee_code": employee.employee_code,
        "status": offer.status.value,
        # Said, not implied: HR knows whether onboarding is under way or theirs to start.
        "onboarding_plan_id": str(plan.id) if plan else None,
        "onboarding_checklist": plan.template_name if plan else None,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Reading
# ═══════════════════════════════════════════════════════════════════════════

_TERMINAL = frozenset(
    {
        OfferStatus.ACCEPTED,
        OfferStatus.JOINED,
        OfferStatus.DECLINED,
        OfferStatus.REVOKED,
        OfferStatus.LAPSED,
        OfferStatus.DROPPED,
    }
)
_NOT_REVOCABLE = frozenset(
    {
        OfferStatus.JOINED,
        OfferStatus.DROPPED,
        OfferStatus.DECLINED,
        OfferStatus.REVOKED,
        OfferStatus.LAPSED,
    }
)


async def _user_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids)), User, user)
    )
    return {row.id: row.full_name for row in rows}


async def _approval_steps(session: AsyncSession, user: User, offer: Offer) -> list[OfferApproval]:
    rows = await session.scalars(
        tenant_scoped(
            select(OfferApproval)
            .where(OfferApproval.offer_id == offer.id)
            .order_by(OfferApproval.sequence),
            OfferApproval,
            user,
        )
    )
    return list(rows.all())


def _my_authority(
    user: User, offer: Offer, requisition: Requisition, steps: list[OfferApproval]
) -> dict[str, Any]:
    pending = next(
        (s for s in steps if s.decision in (ApprovalDecision.PENDING, ApprovalDecision.VOIDED)),
        None,
    )
    if offer.status is not OfferStatus.PENDING_APPROVAL or pending is None:
        return {"can_decide": False, "step": None, "message": None}
    authority = approvers.can_decide_step(
        approver_role=pending.approver_role,
        user_role=user.role.value,
        has_reports=user.has_reports,
        is_hiring_manager=(
            user.employee_id is not None and user.employee_id == requisition.hiring_manager_id
        ),
        is_originator=offer.created_by_user_id is not None and offer.created_by_user_id == user.id,
    )
    return {
        "can_decide": authority.allowed,
        "step": pending.sequence,
        "step_role": pending.approver_role,
        "message": authority.message or None,
    }


async def approval_view(
    session: AsyncSession, user: User, *, offer_id: uuid.UUID
) -> dict[str, Any]:
    """The approval payload, for HR or the requisition's hiring manager.

    An offer's CTC and the parity view of what comparable employees earn are not
    something every manager in the tenant may read.
    """
    offer = await _offer(session, user, offer_id)
    requisition = await _requisition(session, user, offer.requisition_id)
    is_hiring_manager = (
        user.employee_id is not None and user.employee_id == requisition.hiring_manager_id
    )
    if not permissions.is_hr(user) and not is_hiring_manager:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")
    payload = await approval_payload(session, user, offer_id=offer_id)
    payload["my_authority"] = _my_authority(
        user, offer, requisition, await _approval_steps(session, user, offer)
    )
    return payload


async def list_offer_summaries(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID | None = None,
    offer_status: OfferStatus | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(Offer, Candidate.full_name, Requisition.code)
        .join(Candidate, Candidate.id == Offer.candidate_id)
        .join(Requisition, Requisition.id == Offer.requisition_id)
        .order_by(Offer.created_at.desc())
    )
    if requisition_id is not None:
        stmt = stmt.where(Offer.requisition_id == requisition_id)
    if offer_status is not None:
        stmt = stmt.where(Offer.status == offer_status)
    rows = (await session.execute(tenant_scoped(stmt, Offer, user))).all()
    return [
        {
            "id": str(offer.id),
            "code": offer.code,
            "candidate_id": str(offer.candidate_id),
            "candidate_name": name,
            "requisition_id": str(offer.requisition_id),
            "requisition_code": code,
            "designation": offer.designation,
            "grade": offer.grade,
            "ctc_annual": str(offer.ctc_annual),
            "joining_date": offer.joining_date.isoformat(),
            "status": offer.status.value,
            "valid_until": offer.valid_until.isoformat() if offer.valid_until else None,
            "released_at": offer.released_at.isoformat() if offer.released_at else None,
            "responded_at": offer.responded_at.isoformat() if offer.responded_at else None,
            "outcome_reason": offer.outcome_reason,
            "resigned_elsewhere": offer.resigned_elsewhere,
            "converted_employee_id": (
                str(offer.converted_employee_id) if offer.converted_employee_id else None
            ),
            "created_at": offer.created_at.isoformat(),
        }
        for offer, name, code in rows
    ]


async def offer_history(
    session: AsyncSession, user: User, *, offer_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Every transition, with the facts snapshotted at the time."""
    offer = await _offer(session, user, offer_id)
    events = (
        await session.scalars(
            tenant_scoped(
                select(OfferEvent)
                .where(OfferEvent.offer_id == offer.id)
                .order_by(OfferEvent.occurred_at),
                OfferEvent,
                user,
            )
        )
    ).all()
    names = await _user_names(session, user, {e.actor_user_id for e in events if e.actor_user_id})
    return [
        {
            "from_status": e.from_status.value if e.from_status else None,
            "to_status": e.to_status.value,
            "reason": e.reason,
            "by": names.get(e.actor_user_id) if e.actor_user_id else None,
            "occurred_at": e.occurred_at.isoformat(),
            "facts": e.facts,
        }
        for e in events
    ]


async def offer_detail(session: AsyncSession, user: User, *, offer_id: uuid.UUID) -> dict[str, Any]:
    """The whole offer for HR: terms, breakup, approvals, history and what can be done."""
    offer = await _offer(session, user, offer_id)
    requisition = await _requisition(session, user, offer.requisition_id)
    candidate = await session.get(Candidate, offer.candidate_id)
    components = await _components(session, user, offer)
    breakup = (
        await session.scalars(
            tenant_scoped(
                select(OfferSalaryComponent)
                .where(OfferSalaryComponent.offer_id == offer.id)
                .order_by(OfferSalaryComponent.display_order),
                OfferSalaryComponent,
                user,
            )
        )
    ).all()
    steps = await _approval_steps(session, user, offer)
    names = await _user_names(
        session,
        user,
        {s.decided_by_user_id for s in steps if s.decided_by_user_id}
        | ({offer.created_by_user_id} if offer.created_by_user_id else set()),
    )
    is_admin = user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN)

    return {
        "offer": {
            "id": str(offer.id),
            "code": offer.code,
            "status": offer.status.value,
            "ctc_annual": str(offer.ctc_annual),
            "currency": offer.currency,
            "designation": offer.designation,
            "grade": offer.grade,
            "joining_date": offer.joining_date.isoformat(),
            "joining_location_id": (
                str(offer.joining_location_id) if offer.joining_location_id else None
            ),
            "department_id": str(offer.department_id) if offer.department_id else None,
            "salary_structure_id": str(offer.salary_structure_id),
            "probation_months": offer.probation_months,
            "notice_period_days": offer.notice_period_days,
            "valid_until": offer.valid_until.isoformat() if offer.valid_until else None,
            "released_at": offer.released_at.isoformat() if offer.released_at else None,
            "responded_at": offer.responded_at.isoformat() if offer.responded_at else None,
            "outcome_reason": offer.outcome_reason,
            "resigned_elsewhere": offer.resigned_elsewhere,
            "letter_id": str(offer.letter_id) if offer.letter_id else None,
            "converted_employee_id": (
                str(offer.converted_employee_id) if offer.converted_employee_id else None
            ),
            "created_by": names.get(offer.created_by_user_id) if offer.created_by_user_id else None,
        },
        "candidate": {
            "id": str(offer.candidate_id),
            "name": candidate.full_name if candidate else None,
            "email": candidate.email if candidate else None,
            "notice_period_days": candidate.notice_period_days if candidate else None,
        },
        "requisition": {
            "id": str(requisition.id),
            "code": requisition.code,
            "title": requisition.title,
            "budget_ctc_min": (
                str(requisition.budget_ctc_min) if requisition.budget_ctc_min is not None else None
            ),
            "budget_ctc_max": (
                str(requisition.budget_ctc_max) if requisition.budget_ctc_max is not None else None
            ),
            "open_vacancies": requisition.open_vacancies,
        },
        "components": [
            {
                "kind": c.kind.value,
                "label": c.label,
                "amount": str(c.amount),
                "payable_terms": c.payable_terms,
                "counts_toward_ctc": c.counts_toward_ctc,
            }
            for c in components
        ],
        "salary_breakup": [
            {
                "code": c.component_code,
                "name": c.component_name,
                "monthly": str(c.monthly_amount),
                "annual": str(c.annual_amount),
                "part_of_ctc": c.part_of_ctc,
            }
            for c in breakup
        ],
        "approvals": [
            {
                "sequence": a.sequence,
                "role": a.approver_role,
                "label": a.label,
                "reason": a.reason,
                "decision": a.decision.value,
                "decided_by": names.get(a.decided_by_user_id) if a.decided_by_user_id else None,
                "decided_at": a.decided_at.isoformat() if a.decided_at else None,
                "note": a.note,
                "voided_by_changes": a.voided_by_changes,
            }
            for a in steps
        ],
        "history": await offer_history(session, user, offer_id=offer.id),
        "actions": {
            "can_edit_terms": offer.status not in _TERMINAL,
            "can_submit": offer.status in (OfferStatus.DRAFT, OfferStatus.PENDING_APPROVAL),
            "can_release": offer.status is OfferStatus.APPROVED,
            "can_record_response": offer.status is OfferStatus.RELEASED,
            "can_record_drop": offer.status is OfferStatus.ACCEPTED,
            "can_convert": offer.status is OfferStatus.ACCEPTED
            and offer.converted_employee_id is None,
            "can_revoke": is_admin and offer.status not in _NOT_REVOCABLE,
        },
        "my_authority": _my_authority(user, offer, requisition, steps),
    }
