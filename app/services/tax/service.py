"""Declaration, proof and regime-election persistence.

The arithmetic lives in `computation.py` and reads configuration. This module
gathers what the projection needs and enforces the workflow rules:

- a regime switch after payroll has run recomputes the **whole** financial
  year, not the months ahead;
- landlord PAN is required above a configured threshold, enforced at
  submission;
- a proof decision always carries a remark, always writes an audit entry, and is
  never taken on the verifier's own declaration;
- a proof is a stored, encrypted document the owner and payroll can open, and
  nobody else can.

For the payroll run: `compute_and_store` is the monthly TDS entry point. It now
includes taxable income from outside the salary structure (see `sources.py`).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.documents import IssuedLetter, LetterTemplate
from app.models.employee import Employee
from app.models.payroll import PayrollRuleSet
from app.models.tax import (
    DeclarationStatus,
    DeclarationWindow,
    HouseRentDeclaration,
    PreviousEmployerIncome,
    ProofStatus,
    TaxComputationSnapshot,
    TaxDeclaration,
    TaxProof,
    TaxRegime,
    TaxRegimeElection,
    WindowType,
)
from app.models.user import User
from app.services import audit
from app.services.documents import service as documents_service
from app.services.filings import support
from app.services.payroll.service import active_salary_on
from app.services.tax import form_12bb, sources
from app.services.tax.computation import (
    ProjectionInput,
    TaxComputation,
    TaxRuleError,
    compare_regimes,
    monthly_tds,
    months_remaining_in_year,
    project,
)

ZERO = Decimal(0)

PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()-]+")


class TaxWorkflowError(ValueError):
    """A workflow rule was violated: a closed window, a missing PAN."""


# ── Rule set resolution ─────────────────────────────────────────────────────


def financial_year_bounds(financial_year: str, start_month: int = 4) -> tuple[date, date]:
    """`"2026-27"` to its first and last day."""
    try:
        start_year = int(financial_year.split("-")[0])
    except (ValueError, IndexError) as exc:
        raise TaxWorkflowError(
            f"'{financial_year}' is not a financial year. Use the form 2026-27."
        ) from exc

    start = date(start_year, start_month, 1)
    end_month = start_month - 1 or 12
    end_year = start_year + 1 if start_month > 1 else start_year
    import calendar

    return start, date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])


async def income_tax_rules(
    session: AsyncSession, user: User, financial_year: str
) -> tuple[dict[str, Any], PayrollRuleSet]:
    """The approved TDS rule set for a financial year.

    Refuses an unapproved rule set for the same reason payroll does: a
    projection computed against rules nobody signed off is a number with no
    authority behind it.
    """
    start, _ = financial_year_bounds(financial_year)

    rule_set = await session.scalar(
        tenant_scoped(
            select(PayrollRuleSet)
            .where(
                PayrollRuleSet.rule_type == "tds",
                PayrollRuleSet.effective_from <= start,
                or_(
                    PayrollRuleSet.effective_to.is_(None),
                    PayrollRuleSet.effective_to >= start,
                ),
            )
            .order_by(PayrollRuleSet.effective_from.desc()),
            PayrollRuleSet,
            user,
        )
    )
    if rule_set is None:
        raise TaxWorkflowError(
            f"No income tax rule set is active for {financial_year}. The Compliance SME "
            "supplies slabs, section limits and HRA rules before declarations can open."
        )
    if not rule_set.is_approved:
        raise TaxWorkflowError(
            f"The income tax rule set effective {rule_set.effective_from} has not been "
            "approved by a Compliance SME. Projections will not run against it."
        )
    return rule_set.rules, rule_set


def _display_name(supplied: str | None, extension: str, fallback: str) -> str:
    """A title for a stored document. Never a path, never used on disk."""
    name = (supplied or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME.sub("_", name).strip(" ._")
    if not name:
        name = fallback
    if not name.lower().endswith(extension):
        name = f"{name}{extension}"
    return name[-support.MAX_TITLE_LENGTH :]


# ── Windows ─────────────────────────────────────────────────────────────────


async def window_for(
    session: AsyncSession, user: User, financial_year: str, window_type: WindowType
) -> DeclarationWindow | None:
    return await session.scalar(
        tenant_scoped(
            select(DeclarationWindow).where(
                DeclarationWindow.financial_year == financial_year,
                DeclarationWindow.window_type == window_type,
            ),
            DeclarationWindow,
            user,
        )
    )


async def list_windows(
    session: AsyncSession, user: User, financial_year: str | None
) -> list[DeclarationWindow]:
    stmt = tenant_scoped(
        select(DeclarationWindow).order_by(DeclarationWindow.opens_at), DeclarationWindow, user
    )
    if financial_year:
        stmt = stmt.where(DeclarationWindow.financial_year == financial_year)
    return list((await session.scalars(stmt)).all())


async def upsert_window(
    session: AsyncSession,
    user: User,
    *,
    financial_year: str,
    window_type: WindowType,
    opens_at: date,
    closes_at: date,
    allow_late_submission: bool,
    reminder_days_before: list[int] | None,
    request: Request | None = None,
) -> DeclarationWindow:
    """Set a window's dates. Audited: a moved deadline changes everyone's tax."""
    if closes_at < opens_at:
        raise TaxWorkflowError("A window cannot close before it opens")
    if reminder_days_before is not None and any(day < 0 for day in reminder_days_before):
        raise TaxWorkflowError("Reminder days are days before closing, so none can be negative")

    window = await window_for(session, user, financial_year, window_type)
    if window is None:
        window = DeclarationWindow(
            tenant_id=user.tenant_id,
            financial_year=financial_year,
            window_type=window_type,
        )
        session.add(window)

    window.opens_at = opens_at
    window.closes_at = closes_at
    window.allow_late_submission = allow_late_submission
    if reminder_days_before is not None:
        window.reminder_days_before = sorted(set(reminder_days_before), reverse=True)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="declaration_window",
        entity_id=window.id,
        actor=user,
        changes={
            "financial_year": financial_year,
            "window_type": window_type.value,
            "opens_at": opens_at.isoformat(),
            "closes_at": closes_at.isoformat(),
            "allow_late_submission": allow_late_submission,
            "reminder_days_before": window.reminder_days_before,
        },
        summary=(
            f"Set the {window_type.value} window for {financial_year}: "
            f"{opens_at.isoformat()} to {closes_at.isoformat()}"
        ),
        request=request,
    )
    return window


async def assert_window_open(
    session: AsyncSession,
    user: User,
    financial_year: str,
    window_type: WindowType,
    on: date | None = None,
) -> None:
    """Refuse a submission outside its window, unless late is permitted."""
    window = await window_for(session, user, financial_year, window_type)
    if window is None:
        raise TaxWorkflowError(
            f"No {window_type.value} window is configured for {financial_year}."
        )

    moment = on or date.today()
    if window.is_open(moment) or window.allow_late_submission:
        return

    raise TaxWorkflowError(
        f"The {window_type.value} window for {financial_year} closed on "
        f"{window.closes_at}. Contact your payroll team."
    )


# ── Regime election ─────────────────────────────────────────────────────────


async def get_election(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> TaxRegimeElection | None:
    return await session.scalar(
        tenant_scoped(
            select(TaxRegimeElection).where(
                TaxRegimeElection.employee_id == employee_id,
                TaxRegimeElection.financial_year == financial_year,
            ),
            TaxRegimeElection,
            user,
        )
    )


async def elect_regime(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    regime: TaxRegime,
    request: Request | None = None,
) -> tuple[TaxRegimeElection, bool]:
    """Elect or switch a tax regime.

    Returns `(election, needs_full_year_recompute)`.

    A switch after payroll has run is permitted but requires the whole year to
    be recomputed. Applying it forward only would leave the months already run
    computed under a regime the employee is no longer on, and the year would
    not reconcile at filing.
    """
    rules, _ = await income_tax_rules(session, user, financial_year)
    permitted_switches = int(rules.get("regime_switches_per_year", 1))

    existing = await get_election(session, user, employee_id, financial_year)

    if existing is None:
        election = TaxRegimeElection(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            financial_year=financial_year,
            regime=regime,
            elected_at=datetime.now(UTC),
        )
        session.add(election)
        await session.flush()

        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="tax_regime_election",
            entity_id=election.id,
            actor=user,
            changes={"financial_year": financial_year, "regime": regime.value},
            summary=f"Elected the {regime.value} tax regime for {financial_year}",
            request=request,
        )
        return election, False

    if existing.regime is regime:
        return existing, False

    if existing.switch_count >= permitted_switches:
        raise TaxWorkflowError(
            f"You have already changed regime {existing.switch_count} time(s) this "
            f"year, and {permitted_switches} is the limit. Contact your payroll team."
        )

    previous = existing.regime
    # A locked election means payroll has already run against it, so every
    # month of the year has to be recomputed rather than only the ones ahead.
    needs_recompute = existing.locked_at is not None

    existing.previous_regime = previous
    existing.regime = regime
    existing.switch_count += 1
    existing.elected_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tax_regime_election",
        entity_id=existing.id,
        actor=user,
        changes={
            "financial_year": financial_year,
            "from": previous.value,
            "to": regime.value,
            "switch_count": existing.switch_count,
            "full_year_recompute_required": needs_recompute,
        },
        summary=(
            f"Switched from the {previous.value} to the {regime.value} regime for "
            f"{financial_year}"
            + (
                " — payroll had already run, so the whole year is recomputed"
                if needs_recompute
                else ""
            )
        ),
        request=request,
    )
    return existing, needs_recompute


# ── Declarations ────────────────────────────────────────────────────────────


async def list_declarations(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> list[TaxDeclaration]:
    stmt = tenant_scoped(
        select(TaxDeclaration)
        .where(
            TaxDeclaration.employee_id == employee_id,
            TaxDeclaration.financial_year == financial_year,
        )
        .order_by(TaxDeclaration.section, TaxDeclaration.sub_category),
        TaxDeclaration,
        user,
    )
    return list((await session.scalars(stmt)).all())


async def _declaration(
    session: AsyncSession, user: User, declaration_id: uuid.UUID
) -> TaxDeclaration:
    declaration = await session.scalar(
        tenant_scoped(
            select(TaxDeclaration).where(TaxDeclaration.id == declaration_id),
            TaxDeclaration,
            user,
        )
    )
    if declaration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Declaration not found")
    return declaration


async def upsert_declaration(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    section: str,
    sub_category: str,
    declared_amount: Decimal,
    description: str | None = None,
    request: Request | None = None,
) -> TaxDeclaration:
    await assert_window_open(session, user, financial_year, WindowType.DECLARATION)
    section = section.upper()

    existing = await session.scalar(
        tenant_scoped(
            select(TaxDeclaration).where(
                TaxDeclaration.employee_id == employee_id,
                TaxDeclaration.financial_year == financial_year,
                TaxDeclaration.section == section,
                TaxDeclaration.sub_category == sub_category,
            ),
            TaxDeclaration,
            user,
        )
    )

    if existing is not None:
        if existing.status is DeclarationStatus.LOCKED:
            raise TaxWorkflowError(
                f"The {section} declaration is locked for {financial_year} and cannot "
                "be changed."
            )
        previous = str(existing.declared_amount)
        existing.declared_amount = declared_amount
        existing.description = description
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="tax_declaration",
            entity_id=existing.id,
            actor=user,
            changes={"section": section, "from": previous, "to": str(declared_amount)},
            summary=f"Changed the {section} declaration for {financial_year}",
            request=request,
        )
        return existing

    declaration = TaxDeclaration(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        financial_year=financial_year,
        section=section,
        sub_category=sub_category,
        description=description,
        declared_amount=declared_amount,
    )
    session.add(declaration)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="tax_declaration",
        entity_id=declaration.id,
        actor=user,
        changes={"section": section, "declared_amount": str(declared_amount)},
        summary=f"Declared {declared_amount} under {section} for {financial_year}",
        request=request,
    )
    return declaration


async def submit_declaration(
    session: AsyncSession,
    user: User,
    declaration_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> TaxDeclaration:
    """Submit a draft so it counts towards the projection and can take proofs."""
    declaration = await _declaration(session, user, declaration_id)
    permissions.require_self_or_hr(
        user, declaration.employee_id, detail="You can only manage your own tax declarations"
    )
    if declaration.status is DeclarationStatus.LOCKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This declaration is locked for the year and cannot be changed",
        )
    await assert_window_open(
        session, user, declaration.financial_year, WindowType.DECLARATION
    )

    declaration.status = DeclarationStatus.SUBMITTED
    declaration.submitted_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tax_declaration",
        entity_id=declaration.id,
        actor=user,
        changes={"status": DeclarationStatus.SUBMITTED.value},
        summary=f"Submitted the {declaration.section} declaration for {declaration.financial_year}",
        request=request,
    )
    return declaration


# ── House rent ──────────────────────────────────────────────────────────────


async def get_house_rent(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> HouseRentDeclaration | None:
    return await session.scalar(
        tenant_scoped(
            select(HouseRentDeclaration).where(
                HouseRentDeclaration.employee_id == employee_id,
                HouseRentDeclaration.financial_year == financial_year,
            ),
            HouseRentDeclaration,
            user,
        )
    )


async def submit_house_rent(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    monthly_rent: list[dict],
    landlord_name: str | None,
    landlord_pan: str | None,
    property_address: str | None,
    is_metro: bool,
    request: Request | None = None,
) -> HouseRentDeclaration:
    """Record rent paid.

    Landlord PAN is required above a configured annual threshold. Enforced here
    rather than at assessment — an employee who finds out in March that their
    HRA claim is invalid has lost the chance to obtain the PAN.
    """
    rules, _ = await income_tax_rules(session, user, financial_year)
    threshold = Decimal(str(rules.get("landlord_pan_threshold_annual", 0)))

    annual = sum((Decimal(str(row.get("rent", 0))) for row in monthly_rent), start=ZERO)
    pan = (landlord_pan or "").strip().upper()

    if pan and not PAN_PATTERN.match(pan):
        raise TaxWorkflowError(
            f"'{pan}' is not a valid PAN. A PAN is five letters, four digits and a letter."
        )
    if threshold > ZERO and annual > threshold and not pan:
        raise TaxWorkflowError(
            f"Annual rent of {annual} is above {threshold}, so your landlord's PAN is "
            "required for the HRA exemption. Without it the claim will be disallowed."
        )

    existing = await get_house_rent(session, user, employee_id, financial_year)
    if existing is None:
        existing = HouseRentDeclaration(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            financial_year=financial_year,
        )
        session.add(existing)

    existing.monthly_rent = monthly_rent
    existing.landlord_name = landlord_name
    existing.landlord_pan = pan or None
    existing.property_address = property_address
    existing.is_metro = is_metro

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="house_rent_declaration",
        entity_id=existing.id,
        actor=user,
        changes={
            "financial_year": financial_year,
            "annual_rent": str(annual),
            "is_metro": is_metro,
            "landlord_pan_supplied": bool(pan),
        },
        summary=f"Recorded rent of {annual} for {financial_year}",
        request=request,
    )
    return existing


# ── Previous employer ───────────────────────────────────────────────────────


async def get_previous_employer(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> PreviousEmployerIncome | None:
    return await session.scalar(
        tenant_scoped(
            select(PreviousEmployerIncome).where(
                PreviousEmployerIncome.employee_id == employee_id,
                PreviousEmployerIncome.financial_year == financial_year,
            ),
            PreviousEmployerIncome,
            user,
        )
    )


async def upsert_previous_employer(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    values: dict[str, Any],
    request: Request | None = None,
) -> PreviousEmployerIncome:
    """Income from a previous employer this financial year.

    Without it a mid-year joiner is taxed as though they earned nothing before
    joining, which under-deducts all year and lands them with a liability at
    filing.
    """
    record = await get_previous_employer(session, user, employee_id, financial_year)
    if record is None:
        record = PreviousEmployerIncome(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            financial_year=financial_year,
        )
        session.add(record)

    for field_name in (
        "employer_name",
        "gross_salary",
        "tds_deducted",
        "pf_deducted",
        "professional_tax",
        "exempt_allowances",
    ):
        setattr(record, field_name, values.get(field_name))
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="previous_employer_income",
        entity_id=record.id,
        actor=user,
        changes={
            "financial_year": financial_year,
            "gross_salary": str(values.get("gross_salary")),
            "tds_deducted": str(values.get("tds_deducted")),
        },
        summary=f"Recorded previous-employer income for {financial_year}",
        request=request,
    )
    return record


async def attach_previous_employer_document(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    data: bytes,
    request: Request | None = None,
) -> PreviousEmployerIncome:
    """Attach Form 16 Part B from the previous employer."""
    record = await get_previous_employer(session, user, employee_id, financial_year)
    if record is None:
        raise TaxWorkflowError(
            "Save the previous employer's figures first, then attach their Form 16."
        )
    stored = support.store_upload(user.tenant_id, "tax-form16", data)
    record.document_storage_key = stored.key
    record.status = ProofStatus.PENDING
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="previous_employer_income",
        entity_id=record.id,
        actor=user,
        changes={"document_attached": True, "size_bytes": stored.size},
        summary=f"Attached the previous employer's Form 16 for {financial_year}",
        request=request,
    )
    return record


async def previous_employer_document(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    request: Request | None = None,
) -> tuple[bytes, str, str]:
    record = await get_previous_employer(session, user, employee_id, financial_year)
    permissions.require_self_or_hr(
        user,
        record.employee_id if record else None,
        detail="You can only open your own documents",
    )
    stored = support.load_file(user.tenant_id, record.document_storage_key if record else None)
    if record is None or stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No Form 16 is attached")
    data, mime = stored
    await _record_document_read(session, user, record.employee_id, "previous_employer_income",
                                record.id, request)
    return data, mime, f"form16_{financial_year}{_extension(record.document_storage_key)}"


def _extension(key: str | None) -> str:
    return key[key.rfind(".") :] if key and "." in key else ""


async def _record_document_read(
    session: AsyncSession,
    user: User,
    owner_employee_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    request: Request | None,
) -> None:
    """Payroll opening someone else's tax evidence is a sensitive read, recorded."""
    if user.employee_id == owner_employee_id:
        return
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.SENSITIVE_READ,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=user,
        changes={"employee_id": str(owner_employee_id)},
        summary="Opened an employee's tax document",
        request=request,
    )


# ── Proofs ──────────────────────────────────────────────────────────────────


async def submit_proof(
    session: AsyncSession,
    user: User,
    *,
    declaration_id: uuid.UUID,
    proof_amount: Decimal,
    data: bytes,
    document_name: str | None,
    request: Request | None = None,
) -> TaxProof:
    """Attach a proof document to a submitted declaration.

    The file is validated by its bytes, stored encrypted under a generated key,
    and its client-supplied name is kept only as a title.
    """
    declaration = await _declaration(session, user, declaration_id)
    permissions.require_self_or_hr(
        user, declaration.employee_id, detail="You can only attach proofs to your own declarations"
    )
    if proof_amount <= ZERO:
        raise TaxWorkflowError("A proof has to be for an amount above zero.")
    if declaration.status is DeclarationStatus.LOCKED:
        raise TaxWorkflowError(
            f"The {declaration.section} declaration is locked for "
            f"{declaration.financial_year}; proofs can no longer be added."
        )
    if declaration.status is DeclarationStatus.DRAFT:
        raise TaxWorkflowError(
            f"Submit the {declaration.section} declaration before attaching proof."
        )
    await assert_window_open(session, user, declaration.financial_year, WindowType.PROOF)

    stored = support.store_upload(user.tenant_id, "tax-proofs", data)
    proof = TaxProof(
        tenant_id=user.tenant_id,
        declaration_id=declaration.id,
        proof_amount=proof_amount,
        document_storage_key=stored.key,
        document_name=_display_name(
            document_name, stored.extension, f"{declaration.section}_proof"
        ),
    )
    session.add(proof)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="tax_proof",
        entity_id=proof.id,
        actor=user,
        changes={
            "section": declaration.section,
            "proof_amount": str(proof_amount),
            "size_bytes": stored.size,
            "content_type": stored.mime_type,
        },
        summary=(
            f"Submitted a proof of {proof_amount} against {declaration.section} for "
            f"{declaration.financial_year}"
        ),
        request=request,
    )
    return proof


async def proof_document(
    session: AsyncSession,
    user: User,
    proof_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> tuple[bytes, str, str]:
    """The stored proof. The owner and payroll only; anyone else is refused."""
    proof = await session.scalar(
        tenant_scoped(select(TaxProof).where(TaxProof.id == proof_id), TaxProof, user)
    )
    if proof is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proof not found")
    declaration = await _declaration(session, user, proof.declaration_id)
    permissions.require_self_or_hr(
        user, declaration.employee_id, detail="You can only open your own proofs"
    )
    stored = support.load_file(user.tenant_id, proof.document_storage_key)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No document is stored for this proof"
        )
    data, mime = stored
    await _record_document_read(session, user, declaration.employee_id, "tax_proof", proof.id,
                                request)
    return data, mime, proof.document_name or f"proof{_extension(proof.document_storage_key)}"


async def proof_queue(
    session: AsyncSession,
    user: User,
    *,
    financial_year: str | None,
    pending_only: bool = True,
) -> list[dict[str, Any]]:
    """The payroll team's verification queue, oldest first."""
    stmt = tenant_scoped(
        select(TaxProof, TaxDeclaration, Employee)
        .join(TaxDeclaration, TaxProof.declaration_id == TaxDeclaration.id)
        .join(Employee, TaxDeclaration.employee_id == Employee.id)
        .order_by(TaxProof.created_at),
        TaxProof,
        user,
    )
    if pending_only:
        stmt = stmt.where(TaxProof.status == ProofStatus.PENDING)
    if financial_year:
        stmt = stmt.where(TaxDeclaration.financial_year == financial_year)

    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": proof.id,
            "declaration_id": declaration.id,
            "employee_id": employee.id,
            "employee_code": employee.employee_code,
            "employee_name": employee.full_name,
            "financial_year": declaration.financial_year,
            "section": declaration.section,
            "sub_category": declaration.sub_category,
            "declared_amount": Decimal(str(declaration.declared_amount)),
            "proof_amount": Decimal(str(proof.proof_amount)),
            "accepted_amount": Decimal(str(proof.accepted_amount or 0)),
            "status": proof.status,
            "verifier_remarks": proof.verifier_remarks,
            "verified_at": proof.verified_at,
            "document_name": proof.document_name,
            "has_document": bool(proof.document_storage_key),
            "created_at": proof.created_at,
        }
        for proof, declaration, employee in rows
    ]


async def verify_proof(
    session: AsyncSession,
    user: User,
    proof_id: uuid.UUID,
    *,
    status_decision: ProofStatus,
    accepted_amount: Decimal,
    remarks: str,
    request: Request | None = None,
) -> TaxProof:
    """Record a verification decision.

    `PARTIALLY_ACCEPTED` is a first-class outcome, not a workaround: someone
    declares 150,000 under 80C and proves 90,000, and accepting 90,000 with a
    remark is the correct answer. Rejecting the line outright is not, and it
    produces a support ticket.

    A remark is required on anything but a clean full acceptance. "Rejected"
    with no reason is the single most common cause of a payroll query. And a
    verifier never decides a proof against their own declaration.
    """
    proof = await session.scalar(
        tenant_scoped(select(TaxProof).where(TaxProof.id == proof_id), TaxProof, user)
    )
    if proof is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proof not found")

    owner = await session.scalar(
        tenant_scoped(
            select(TaxDeclaration.employee_id).where(TaxDeclaration.id == proof.declaration_id),
            TaxDeclaration,
            user,
        )
    )
    if user.employee_id is not None and owner == user.employee_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This proof is against your own declaration. Somebody else in payroll "
                "has to verify it."
            ),
        )

    if status_decision is ProofStatus.PENDING:
        raise TaxWorkflowError("A verification decision cannot be 'pending'")

    if status_decision is ProofStatus.REJECTED:
        accepted_amount = ZERO

    if status_decision is ProofStatus.PARTIALLY_ACCEPTED:
        if accepted_amount <= ZERO:
            raise TaxWorkflowError(
                "A partial acceptance needs an accepted amount above zero. Use "
                "'rejected' if nothing is accepted."
            )
        if accepted_amount >= Decimal(str(proof.proof_amount)):
            raise TaxWorkflowError(
                "A partial acceptance must be less than the amount proved. Use "
                "'verified' to accept it in full."
            )

    if status_decision is not ProofStatus.VERIFIED and not remarks.strip():
        raise TaxWorkflowError(
            "A remark is required when a proof is not accepted in full. The employee "
            "sees it, and 'rejected' with no reason is the most common cause of a "
            "payroll query."
        )

    if status_decision is ProofStatus.VERIFIED:
        accepted_amount = Decimal(str(proof.proof_amount))

    proof.status = status_decision
    proof.accepted_amount = accepted_amount
    proof.verifier_user_id = user.id
    proof.verifier_remarks = remarks.strip() or None
    proof.verified_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=(
            AuditAction.APPROVE
            if status_decision in {ProofStatus.VERIFIED, ProofStatus.PARTIALLY_ACCEPTED}
            else AuditAction.REJECT
        ),
        entity_type="tax_proof",
        entity_id=proof.id,
        actor=user,
        changes={
            "decision": status_decision.value,
            "proof_amount": str(proof.proof_amount),
            "accepted_amount": str(accepted_amount),
            "remarks": proof.verifier_remarks,
        },
        summary=(
            f"Proof {status_decision.value.replace('_', ' ')}: "
            f"{accepted_amount} of {proof.proof_amount} accepted"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(proof)
    return proof


async def close_proof_window(
    session: AsyncSession, user: User, financial_year: str, request: Request | None = None
) -> int:
    """Lock declarations once the proof window closes.

    Unproven amounts fall to whatever was accepted, which is usually zero and
    usually spikes March TDS. That is correct — and the reminders sent while
    the window was open are what make it not a surprise.
    """
    declarations = (
        await session.scalars(
            tenant_scoped(
                select(TaxDeclaration).where(
                    TaxDeclaration.financial_year == financial_year,
                    TaxDeclaration.status != DeclarationStatus.LOCKED,
                ),
                TaxDeclaration,
                user,
            )
        )
    ).all()

    now = datetime.now(UTC)
    unproven = 0
    for declaration in declarations:
        declaration.status = DeclarationStatus.LOCKED
        declaration.locked_at = now
        if Decimal(str(declaration.accepted_amount)) < Decimal(
            str(declaration.declared_amount)
        ):
            unproven += 1

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tax_declaration",
        actor=user,
        changes={
            "financial_year": financial_year,
            "locked": len(declarations),
            "partially_or_wholly_unproven": unproven,
        },
        summary=(
            f"Closed the {financial_year} proof window: {len(declarations)} declarations "
            f"locked, {unproven} with less proved than declared"
        ),
        request=request,
    )
    await session.commit()
    return len(declarations)


# ── Projection ──────────────────────────────────────────────────────────────


async def build_projection_input(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    financial_year: str,
    *,
    regime: TaxRegime | None = None,
    use_declared_when_unproven: bool = False,
) -> ProjectionInput:
    """Gather everything the projection needs.

    `use_declared_when_unproven` drives the live preview an employee sees while
    editing: before the proof window closes, showing them the impact of what
    they have declared is the useful answer. After it closes, only what was
    proved counts.
    """
    start, end = financial_year_bounds(financial_year)

    election = await get_election(session, user, employee_id, financial_year)
    chosen = regime or (election.regime if election else TaxRegime.NEW)

    salary = await active_salary_on(session, user, employee_id, start)
    gross = ZERO
    basic_plus_da = ZERO
    hra_received = ZERO
    employer_nps = ZERO

    if salary is not None:
        for component in salary.components:
            annual = Decimal(str(component.annual_amount))
            if component.component_type.value == "earning":
                gross += annual
            if component.counts_toward_pf:
                basic_plus_da += annual
            if component.component_code == "HRA":
                hra_received += annual
            if component.component_code in {"NPS_ER", "EMPLOYER_NPS"}:
                employer_nps += annual

    rent = await get_house_rent(session, user, employee_id, financial_year)
    previous = await get_previous_employer(session, user, employee_id, financial_year)

    declarations = (
        await session.scalars(
            tenant_scoped(
                select(TaxDeclaration).where(
                    TaxDeclaration.employee_id == employee_id,
                    TaxDeclaration.financial_year == financial_year,
                    TaxDeclaration.status != DeclarationStatus.DRAFT,
                ),
                TaxDeclaration,
                user,
            )
        )
    ).all()

    lines = tuple(
        (
            declaration.section,
            Decimal(str(declaration.declared_amount)),
            Decimal(str(declaration.declared_amount))
            if use_declared_when_unproven
            else Decimal(str(declaration.accepted_amount)),
        )
        for declaration in declarations
    )

    employee = await session.get(Employee, employee_id)
    age = None
    if employee and employee.date_of_birth:
        age = start.year - employee.date_of_birth.year - (
            (start.month, start.day) < (employee.date_of_birth.month, employee.date_of_birth.day)
        )

    other = await sources.other_taxable_income(
        session, user, employee_id, financial_year, start=start, end=end
    )

    return ProjectionInput(
        regime=chosen.value,
        gross_salary=gross,
        basic_plus_da=basic_plus_da,
        hra_received=hra_received,
        declarations=lines,
        annual_rent=Decimal(str(rent.annual_rent)) if rent else ZERO,
        is_metro=rent.is_metro if rent else False,
        age=age,
        previous_gross=Decimal(str(previous.gross_salary)) if previous else ZERO,
        previous_tds=Decimal(str(previous.tds_deducted)) if previous else ZERO,
        previous_exempt=Decimal(str(previous.exempt_allowances)) if previous else ZERO,
        employer_nps=employer_nps,
        other_income=tuple(line.as_tuple() for line in other.taxable),
        pending_if_unclaimed=tuple(line.as_tuple() for line in other.pending),
        income_notes=tuple(other.notes),
    )


async def compute_and_store(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    financial_year: str,
    *,
    for_month: int,
    tds_already_deducted: Decimal = ZERO,
    request: Request | None = None,
) -> TaxComputationSnapshot:
    """Project, level the TDS, and store the working.

    The payroll run's monthly TDS entry point. Stored rather than derived on
    demand because the *reason* matters: an employee asking why their TDS
    jumped needs the previous projection to compare against.
    """
    rules, rule_set = await income_tax_rules(session, user, financial_year)
    inputs = await build_projection_input(session, user, employee_id, financial_year)

    try:
        computation = project(inputs, rules)
    except TaxRuleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    start_month = int(rules.get("financial_year_start_month", 4))
    remaining = months_remaining_in_year(for_month, start_month)

    per_month = monthly_tds(
        annual_liability=computation.annual_tax_liability,
        already_deducted=tds_already_deducted,
        months_remaining=remaining,
        previous_employer_tds=inputs.previous_tds,
    )

    snapshot = TaxComputationSnapshot(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        financial_year=financial_year,
        regime=TaxRegime(inputs.regime),
        computed_for_month=for_month,
        gross_annual_income=computation.gross_annual_income,
        total_exemptions=computation.total_exemptions,
        total_deductions=computation.total_deductions,
        taxable_income=computation.taxable_income,
        annual_tax_liability=computation.annual_tax_liability,
        tds_already_deducted=tds_already_deducted,
        monthly_tds=per_month,
        working={**computation.as_working(), "months_remaining": remaining},
        rule_set_id=rule_set.id,
    )
    session.add(snapshot)
    await session.commit()
    await session.refresh(snapshot)
    return snapshot


async def recompute_financial_year(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    financial_year: str,
    *,
    request: Request | None = None,
) -> list[TaxComputationSnapshot]:
    """Recompute every month of a financial year.

    What a regime switch after payroll has run triggers. Applying a switch
    forward only would leave the months already run computed under the old
    regime, and the year would not reconcile at filing.
    """
    rules, _ = await income_tax_rules(session, user, financial_year)
    start_month = int(rules.get("financial_year_start_month", 4))

    snapshots: list[TaxComputationSnapshot] = []
    running_deduction = ZERO

    for offset in range(12):
        month = ((start_month - 1 + offset) % 12) + 1
        snapshot = await compute_and_store(
            session,
            user,
            employee_id,
            financial_year,
            for_month=month,
            tds_already_deducted=running_deduction,
            request=request,
        )
        snapshots.append(snapshot)
        running_deduction += Decimal(str(snapshot.monthly_tds))

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tax_computation_snapshot",
        actor=user,
        changes={
            "employee_id": str(employee_id),
            "financial_year": financial_year,
            "months_recomputed": len(snapshots),
        },
        summary=(
            f"Recomputed all {len(snapshots)} months of {financial_year} for "
            f"{employee_id} after a regime change"
        ),
        request=request,
    )
    await session.commit()
    return snapshots


async def list_snapshots(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> list[TaxComputationSnapshot]:
    stmt = tenant_scoped(
        select(TaxComputationSnapshot)
        .where(
            TaxComputationSnapshot.employee_id == employee_id,
            TaxComputationSnapshot.financial_year == financial_year,
        )
        .order_by(TaxComputationSnapshot.created_at.desc()),
        TaxComputationSnapshot,
        user,
    )
    return list((await session.scalars(stmt)).all())


async def regime_comparison(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> dict[str, Any]:
    """Both regimes, on the employee's own numbers."""
    rules, _ = await income_tax_rules(session, user, financial_year)
    inputs = await build_projection_input(
        session, user, employee_id, financial_year, use_declared_when_unproven=True
    )
    return compare_regimes(inputs, rules)


async def live_preview(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> TaxComputation:
    """The projection an employee sees while editing declarations.

    Uses declared amounts rather than proved ones — before the proof window
    closes, the impact of what they intend to claim is the useful answer.
    """
    rules, _ = await income_tax_rules(session, user, financial_year)
    inputs = await build_projection_input(
        session, user, employee_id, financial_year, use_declared_when_unproven=True
    )
    return project(inputs, rules)


# ── Form 12BB ───────────────────────────────────────────────────────────────


async def issue_form_12bb(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    parent_name: str,
    place: str,
    request: Request | None = None,
) -> IssuedLetter:
    """Issue Form 12BB through the letter engine. The caller commits.

    The form carries the employee's PAN, so an employee with none on record is
    told to fix that first rather than handed a form with a blank where the
    statute requires a number.
    """
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    if not (employee.pan or "").strip():
        raise TaxWorkflowError(
            "There is no PAN on the employee record, and Form 12BB carries it. Ask HR to "
            "add it before issuing the form."
        )

    try:
        rules, _ = await income_tax_rules(session, user, financial_year)
    except TaxWorkflowError:
        rules = None

    values = await form_12bb.claim_values(
        session, user, employee_id, financial_year, rules=rules
    )
    template = await support.ensure_letter_template(
        session, user, form_12bb.FORM_12BB_CODE, form_12bb.seed_template
    )
    return await documents_service.issue_letter(
        session,
        user,
        employee_id=employee_id,
        template_id=template.id,
        manual_values={
            **values,
            "employee_pan": employee.pan,
            "parent_name": parent_name.strip(),
            "place": place.strip(),
            # For a tenant still on an earlier template version that reads one
            # combined value. Plain text, so the engine's escaping is harmless.
            "claims_table": values["chapter_via_particulars"],
        },
        request=request,
        commit=False,
    )


async def list_form_12bb(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> list[IssuedLetter]:
    template_ids = (
        await session.scalars(
            tenant_scoped(
                select(LetterTemplate.id).where(
                    LetterTemplate.code == form_12bb.FORM_12BB_CODE
                ),
                LetterTemplate,
                user,
            )
        )
    ).all()
    if not template_ids:
        return []
    letters = (
        await session.scalars(
            tenant_scoped(
                select(IssuedLetter)
                .where(
                    IssuedLetter.employee_id == employee_id,
                    IssuedLetter.template_id.in_(template_ids),
                )
                .order_by(IssuedLetter.issued_at.desc()),
                IssuedLetter,
                user,
            )
        )
    ).all()
    return [
        letter
        for letter in letters
        if (letter.manual_values or {}).get("financial_year") == financial_year
    ]


async def form_12bb_pdf(
    session: AsyncSession, user: User, letter_id: uuid.UUID
) -> tuple[bytes, str]:
    """The issued form's PDF, for its employee or payroll."""
    letter, intact = await documents_service.verify_letter(session, user, letter_id)
    if await support.letter_template_code(session, user, letter) != form_12bb.FORM_12BB_CODE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Form 12BB not found")
    permissions.require_self_or_hr(
        user, letter.employee_id, detail="You can only open your own Form 12BB"
    )
    if not intact:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Form {letter.reference_number} does not match its stored content hash. "
                "Do not use it; issue it again."
            ),
        )
    data = documents_service.stored_pdf(letter.pdf_storage_key or "")
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The rendered PDF is not available. Issue the form again.",
        )
    return data, f"{letter.reference_number.replace('/', '_')}.pdf"
