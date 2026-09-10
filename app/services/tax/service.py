"""Declaration, proof and regime-election persistence.

The arithmetic lives in `computation.py` and reads configuration. This module
gathers what the projection needs and enforces the workflow rules:

- a regime switch after payroll has run recomputes the **whole** financial
  year, not the months ahead;
- landlord PAN is required above a configured threshold, enforced at
  submission;
- a proof decision always carries a remark, and always writes an audit entry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
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
from app.services.payroll.service import active_salary_on
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

    existing = await session.scalar(
        tenant_scoped(
            select(TaxRegimeElection).where(
                TaxRegimeElection.employee_id == employee_id,
                TaxRegimeElection.financial_year == financial_year,
            ),
            TaxRegimeElection,
            user,
        )
    )

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
        existing.declared_amount = declared_amount
        existing.description = description
        return existing

    declaration = TaxDeclaration(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        financial_year=financial_year,
        section=section.upper(),
        sub_category=sub_category,
        description=description,
        declared_amount=declared_amount,
    )
    session.add(declaration)
    await session.flush()
    return declaration


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

    if threshold > ZERO and annual > threshold and not (landlord_pan or "").strip():
        raise TaxWorkflowError(
            f"Annual rent of {annual} is above {threshold}, so your landlord's PAN is "
            "required for the HRA exemption. Without it the claim will be disallowed."
        )

    existing = await session.scalar(
        tenant_scoped(
            select(HouseRentDeclaration).where(
                HouseRentDeclaration.employee_id == employee_id,
                HouseRentDeclaration.financial_year == financial_year,
            ),
            HouseRentDeclaration,
            user,
        )
    )

    if existing is None:
        existing = HouseRentDeclaration(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            financial_year=financial_year,
        )
        session.add(existing)

    existing.monthly_rent = monthly_rent
    existing.landlord_name = landlord_name
    existing.landlord_pan = (landlord_pan or "").upper() or None
    existing.property_address = property_address
    existing.is_metro = is_metro

    await session.flush()
    return existing


# ── Proof verification ──────────────────────────────────────────────────────


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
    with no reason is the single most common cause of a payroll query.
    """
    proof = await session.scalar(
        tenant_scoped(select(TaxProof).where(TaxProof.id == proof_id), TaxProof, user)
    )
    if proof is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proof not found")

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
    start, _ = financial_year_bounds(financial_year)

    election = await session.scalar(
        tenant_scoped(
            select(TaxRegimeElection).where(
                TaxRegimeElection.employee_id == employee_id,
                TaxRegimeElection.financial_year == financial_year,
            ),
            TaxRegimeElection,
            user,
        )
    )
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

    rent = await session.scalar(
        tenant_scoped(
            select(HouseRentDeclaration).where(
                HouseRentDeclaration.employee_id == employee_id,
                HouseRentDeclaration.financial_year == financial_year,
            ),
            HouseRentDeclaration,
            user,
        )
    )

    previous = await session.scalar(
        tenant_scoped(
            select(PreviousEmployerIncome).where(
                PreviousEmployerIncome.employee_id == employee_id,
                PreviousEmployerIncome.financial_year == financial_year,
            ),
            PreviousEmployerIncome,
            user,
        )
    )

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

    Stored rather than derived on demand because the *reason* matters: an
    employee asking why their TDS jumped needs the previous projection to
    compare against.
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
