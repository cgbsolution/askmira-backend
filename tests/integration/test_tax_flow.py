"""Slice 07 gates that need a real database.

The headline one is the regime switch: changing regime after payroll has run
must recompute the *whole* financial year, not the months ahead. Applying it
forward only leaves months already run computed under a regime the employee is
no longer on, and the year does not reconcile at filing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee
from app.models.payroll import (
    CalculationType,
    ComponentType,
    PayrollRuleSet,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
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
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.payroll import service as salary_service
from app.services.tax import service as tax_service
from app.services.tax.service import TaxWorkflowError
from tests.integration.conftest import requires_db

pytestmark = requires_db

FY = "2026-27"


# ── Fixtures ────────────────────────────────────────────────────────────────


async def _tds_rules(session: AsyncSession, tenant: Tenant, *, approved: bool = True):
    """A TDS rule set as the Compliance SME would supply it.

    Illustrative values. The engine reads them; it does not know them.
    """
    rule_set = PayrollRuleSet(
        tenant_id=tenant.id,
        rule_type="tds",
        jurisdiction="IN",
        version=1,
        effective_from=date(2026, 4, 1),
        approved_at=datetime.now(UTC) if approved else None,
        rules={
            "regimes": {
                "old": {
                    "slabs": [
                        {"up_to": 250000, "rate": 0},
                        {"up_to": 500000, "rate": 5},
                        {"up_to": 1000000, "rate": 20},
                        {"up_to": None, "rate": 30},
                    ],
                    "standard_deduction": 50000,
                    "rebate": {"income_limit": 500000, "max_rebate": 12500},
                    "deductions_allowed": ["80C", "80D", "80CCD1B", "24B", "HRA", "PT"],
                },
                "new": {
                    "slabs": [
                        {"up_to": 300000, "rate": 0},
                        {"up_to": 700000, "rate": 5},
                        {"up_to": 1000000, "rate": 10},
                        {"up_to": None, "rate": 20},
                    ],
                    "standard_deduction": 75000,
                    "deductions_allowed": ["80CCD2"],
                },
            },
            "section_limits": {
                "80C": {"ceiling": 150000},
                "80D": {
                    "ceiling_by_age": [
                        {"max_age": 59, "ceiling": 25000},
                        {"max_age": None, "ceiling": 50000},
                    ]
                },
            },
            "hra": {
                "metro_percent": 50,
                "non_metro_percent": 40,
                "rent_less_basic_percent": 10,
            },
            "landlord_pan_threshold_annual": 100000,
            "cess_percent": 4,
            "regime_switches_per_year": 1,
            "financial_year_start_month": 4,
        },
    )
    session.add(rule_set)
    await session.commit()
    return rule_set


async def _windows(session: AsyncSession, tenant: Tenant, *, proof_open: bool = True):
    session.add_all(
        [
            DeclarationWindow(
                tenant_id=tenant.id,
                financial_year=FY,
                window_type=WindowType.DECLARATION,
                opens_at=date(2026, 4, 1),
                closes_at=date(2027, 3, 31),
                allow_late_submission=True,
            ),
            DeclarationWindow(
                tenant_id=tenant.id,
                financial_year=FY,
                window_type=WindowType.PROOF,
                opens_at=date(2026, 12, 1),
                closes_at=date(2027, 1, 31),
                allow_late_submission=proof_open,
            ),
        ]
    )
    await session.commit()


async def _salary(session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee):
    """A structure with BASIC, HRA and a balance, so HRA has something to work on."""
    basic = SalaryComponent(
        tenant_id=tenant.id,
        code="BASIC",
        name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="CTC",
        value=Decimal(40),
        counts_toward_pf=True,
        display_order=1,
    )
    hra = SalaryComponent(
        tenant_id=tenant.id,
        code="HRA",
        name="House rent allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="BASIC",
        value=Decimal(50),
        display_order=2,
    )
    special = SalaryComponent(
        tenant_id=tenant.id,
        code="SPECIAL",
        name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE,
        display_order=9,
    )
    session.add_all([basic, hra, special])
    await session.flush()

    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard",
        effective_from=date(2026, 1, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for index, component in enumerate((basic, hra, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id,
                component_id=component.id,
                component=component,
                display_order=index,
            )
        )
    session.add(structure)
    await session.commit()

    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1500000"),
            effective_from=date(2026, 4, 1),
        ),
    )
    return structure


# ═══════════════════════════════════════════════════════════════════════════
# The headline gate: a regime switch recomputes the whole year
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_regime_switch_after_payroll_has_run_recomputes_the_whole_year(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Not a forward-only change: every month of the year is recomputed, because
    the months already run were computed under a regime the employee is no
    longer on.
    """
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    await _salary(session, tenant, hr_user, employee)

    election, needs_recompute = await tax_service.elect_regime(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        regime=TaxRegime.NEW,
    )
    await session.commit()
    assert needs_recompute is False, "a first election has nothing to recompute"

    # Payroll runs, which locks the election.
    election.locked_at = datetime.now(UTC)
    await session.commit()

    _, needs_recompute = await tax_service.elect_regime(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        regime=TaxRegime.OLD,
    )
    await session.commit()
    assert needs_recompute is True

    snapshots = await tax_service.recompute_financial_year(session, hr_user, employee.id, FY)

    assert len(snapshots) == 12, "every month of the year, not just the ones ahead"
    assert {s.computed_for_month for s in snapshots} == set(range(1, 13))
    assert all(s.regime is TaxRegime.OLD for s in snapshots)

    # The first month recomputed is April — the start of the financial year,
    # not the month the switch happened.
    assert snapshots[0].computed_for_month == 4


async def test_a_regime_switch_before_payroll_runs_needs_no_recompute(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)

    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.NEW
    )
    await session.commit()

    _, needs_recompute = await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.OLD
    )
    await session.commit()
    assert needs_recompute is False


async def test_the_switch_limit_comes_from_configuration(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)

    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.NEW
    )
    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.OLD
    )
    await session.commit()

    with pytest.raises(TaxWorkflowError, match="already changed regime"):
        await tax_service.elect_regime(
            session,
            hr_user,
            employee_id=employee.id,
            financial_year=FY,
            regime=TaxRegime.NEW,
        )


async def test_a_regime_switch_is_audited_with_both_regimes_named(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)

    election, _ = await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.NEW
    )
    await session.commit()
    election.locked_at = datetime.now(UTC)
    await session.commit()

    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.OLD
    )
    await session.commit()

    entry = await session.scalar(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "tax_regime_election",
            AuditLog.action == AuditAction.UPDATE,
        )
        .order_by(AuditLog.created_at.desc())
    )
    assert entry is not None
    assert entry.changes["from"] == "new"
    assert entry.changes["to"] == "old"
    assert entry.changes["full_year_recompute_required"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Partial acceptance into TDS
# ═══════════════════════════════════════════════════════════════════════════


async def test_partial_acceptance_flows_through_to_the_tds_computation(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate, end to end.

    Declares 150,000 under 80C, proves 90,000. The projection deducts 90,000 —
    not the declared amount, and not nothing.
    """
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    await _salary(session, tenant, hr_user, employee)

    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.OLD
    )
    declaration = await tax_service.upsert_declaration(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        section="80C",
        sub_category="PPF",
        declared_amount=Decimal("150000"),
    )
    declaration.status = DeclarationStatus.SUBMITTED
    await session.commit()

    # The proof is submitted against the declared amount; the verifier decides
    # how much of it the document actually supports.
    proof = TaxProof(
        tenant_id=tenant.id,
        declaration_id=declaration.id,
        proof_amount=Decimal("150000"),
        document_name="ppf-statement.pdf",
    )
    session.add(proof)
    await session.commit()

    await tax_service.verify_proof(
        session,
        hr_user,
        proof.id,
        status_decision=ProofStatus.PARTIALLY_ACCEPTED,
        accepted_amount=Decimal("90000"),
        remarks="PPF statement supports 90,000 of the 150,000 declared.",
    )

    snapshot = await tax_service.compute_and_store(session, hr_user, employee.id, FY, for_month=1)

    deduction_80c = next(
        line for line in snapshot.working["deductions"] if line["section"] == "80C"
    )
    assert deduction_80c["declared"] == "150000.00"
    assert deduction_80c["accepted"] == "90000.00"
    assert deduction_80c["allowed"] == "90000.00"


async def test_an_unproven_declaration_contributes_nothing_to_the_projection(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """What produces the March spike, and it is correct."""
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    await _salary(session, tenant, hr_user, employee)

    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.OLD
    )
    declaration = await tax_service.upsert_declaration(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        section="80C",
        sub_category="ELSS",
        declared_amount=Decimal("150000"),
    )
    declaration.status = DeclarationStatus.SUBMITTED
    await session.commit()

    snapshot = await tax_service.compute_and_store(session, hr_user, employee.id, FY, for_month=1)
    line = next(line for line in snapshot.working["deductions"] if line["section"] == "80C")
    assert line["accepted"] == "0.00"
    assert line["allowed"] == "0.00"


async def test_the_live_preview_uses_declared_amounts_while_the_window_is_open(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """While editing, an employee wants the impact of what they intend to claim."""
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    await _salary(session, tenant, hr_user, employee)

    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.OLD
    )
    declaration = await tax_service.upsert_declaration(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        section="80C",
        sub_category="PPF",
        declared_amount=Decimal("150000"),
    )
    declaration.status = DeclarationStatus.SUBMITTED
    await session.commit()

    preview = await tax_service.live_preview(session, hr_user, employee.id, FY)
    stored = await tax_service.compute_and_store(session, hr_user, employee.id, FY, for_month=1)

    assert preview.total_deductions > Decimal(str(stored.total_deductions)), (
        "the preview counts what was declared; the stored projection counts what was proved"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Verification workflow
# ═══════════════════════════════════════════════════════════════════════════


async def _pending_proof(session, tenant, hr_user, employee, amount="100000"):
    declaration = await tax_service.upsert_declaration(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        section="80C",
        sub_category="PPF",
        declared_amount=Decimal(amount),
    )
    await session.commit()
    proof = TaxProof(
        tenant_id=tenant.id, declaration_id=declaration.id, proof_amount=Decimal(amount)
    )
    session.add(proof)
    await session.commit()
    await session.refresh(proof)
    return proof


async def test_every_verification_decision_writes_an_audit_entry_with_the_remark(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate."""
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    proof = await _pending_proof(session, tenant, hr_user, employee)

    await tax_service.verify_proof(
        session,
        hr_user,
        proof.id,
        status_decision=ProofStatus.REJECTED,
        accepted_amount=Decimal(0),
        remarks="The receipt is dated outside this financial year.",
    )

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "tax_proof", AuditLog.entity_id == proof.id)
    )
    assert entry is not None
    assert entry.actor_user_id == hr_user.id
    assert entry.changes["decision"] == "rejected"
    assert "outside this financial year" in entry.changes["remarks"]


async def test_a_rejection_without_a_remark_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """ "Rejected" with no reason is the most common cause of a payroll query."""
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    proof = await _pending_proof(session, tenant, hr_user, employee)

    with pytest.raises(TaxWorkflowError, match="remark is required"):
        await tax_service.verify_proof(
            session,
            hr_user,
            proof.id,
            status_decision=ProofStatus.REJECTED,
            accepted_amount=Decimal(0),
            remarks="",
        )


async def test_a_rejection_forces_the_accepted_amount_to_zero(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    proof = await _pending_proof(session, tenant, hr_user, employee)

    result = await tax_service.verify_proof(
        session,
        hr_user,
        proof.id,
        status_decision=ProofStatus.REJECTED,
        # An operator error: a rejection with a non-zero amount.
        accepted_amount=Decimal("50000"),
        remarks="Not supported.",
    )
    assert Decimal(str(result.accepted_amount)) == Decimal(0)


async def test_a_full_acceptance_takes_the_whole_proved_amount(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    proof = await _pending_proof(session, tenant, hr_user, employee)

    result = await tax_service.verify_proof(
        session,
        hr_user,
        proof.id,
        status_decision=ProofStatus.VERIFIED,
        accepted_amount=Decimal(0),  # ignored on a full acceptance
        remarks="",
    )
    assert Decimal(str(result.accepted_amount)) == Decimal("100000")


async def test_a_partial_acceptance_equal_to_the_proof_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Use 'verified' for a full acceptance; the distinction matters in reporting."""
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    proof = await _pending_proof(session, tenant, hr_user, employee)

    with pytest.raises(TaxWorkflowError, match="less than the amount proved"):
        await tax_service.verify_proof(
            session,
            hr_user,
            proof.id,
            status_decision=ProofStatus.PARTIALLY_ACCEPTED,
            accepted_amount=Decimal("100000"),
            remarks="All good.",
        )


async def test_a_partial_acceptance_of_zero_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    proof = await _pending_proof(session, tenant, hr_user, employee)

    with pytest.raises(TaxWorkflowError, match="above zero"):
        await tax_service.verify_proof(
            session,
            hr_user,
            proof.id,
            status_decision=ProofStatus.PARTIALLY_ACCEPTED,
            accepted_amount=Decimal(0),
            remarks="Nothing supported.",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Windows and locking
# ═══════════════════════════════════════════════════════════════════════════


async def test_closing_the_proof_window_locks_declarations_and_counts_the_unproven(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate: unproven amounts drop, and the drop is recorded."""
    await _tds_rules(session, tenant)
    await _windows(session, tenant)

    proved = await tax_service.upsert_declaration(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        section="80C",
        sub_category="PPF",
        declared_amount=Decimal("100000"),
    )
    await tax_service.upsert_declaration(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        section="80D",
        sub_category="health",
        declared_amount=Decimal("25000"),
    )
    await session.commit()

    proof = TaxProof(tenant_id=tenant.id, declaration_id=proved.id, proof_amount=Decimal("100000"))
    session.add(proof)
    await session.commit()
    await tax_service.verify_proof(
        session,
        hr_user,
        proof.id,
        status_decision=ProofStatus.VERIFIED,
        accepted_amount=Decimal("100000"),
        remarks="",
    )

    locked = await tax_service.close_proof_window(session, hr_user, FY)
    assert locked == 2

    declarations = (
        await session.scalars(select(TaxDeclaration).where(TaxDeclaration.financial_year == FY))
    ).all()
    assert all(d.status is DeclarationStatus.LOCKED for d in declarations)

    entry = await session.scalar(
        select(AuditLog)
        .where(AuditLog.entity_type == "tax_declaration")
        .order_by(AuditLog.created_at.desc())
    )
    assert entry.changes["partially_or_wholly_unproven"] == 1


async def test_a_locked_declaration_cannot_be_changed(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)

    await tax_service.upsert_declaration(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        section="80C",
        sub_category="PPF",
        declared_amount=Decimal("100000"),
    )
    await session.commit()
    await tax_service.close_proof_window(session, hr_user, FY)

    with pytest.raises(TaxWorkflowError, match="locked"):
        await tax_service.upsert_declaration(
            session,
            hr_user,
            employee_id=employee.id,
            financial_year=FY,
            section="80C",
            sub_category="PPF",
            declared_amount=Decimal("150000"),
        )


async def test_a_closed_window_refuses_a_submission_and_says_when_it_closed(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    session.add(
        DeclarationWindow(
            tenant_id=tenant.id,
            financial_year=FY,
            window_type=WindowType.DECLARATION,
            opens_at=date(2026, 4, 1),
            closes_at=date(2026, 4, 30),
            allow_late_submission=False,
        )
    )
    await session.commit()

    with pytest.raises(TaxWorkflowError) as exc:
        await tax_service.upsert_declaration(
            session,
            hr_user,
            employee_id=employee.id,
            financial_year=FY,
            section="80C",
            sub_category="PPF",
            declared_amount=Decimal("100000"),
        )
    assert "2026-04-30" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════════
# Landlord PAN
# ═══════════════════════════════════════════════════════════════════════════


async def test_landlord_pan_is_required_above_the_configured_threshold(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Enforced at submission, not at assessment — an employee who finds out in
    March has lost the chance to obtain it.
    """
    await _tds_rules(session, tenant)
    await _windows(session, tenant)

    with pytest.raises(TaxWorkflowError, match="landlord's PAN is required"):
        await tax_service.submit_house_rent(
            session,
            hr_user,
            employee_id=employee.id,
            financial_year=FY,
            monthly_rent=[{"month": m, "rent": "25000"} for m in range(1, 13)],
            landlord_name="A Landlord",
            landlord_pan=None,
            property_address="Somewhere",
            is_metro=True,
        )


async def test_landlord_pan_is_not_required_below_the_threshold(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)

    record = await tax_service.submit_house_rent(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        monthly_rent=[{"month": m, "rent": "5000"} for m in range(1, 13)],
        landlord_name="A Landlord",
        landlord_pan=None,
        property_address="Somewhere",
        is_metro=False,
    )
    await session.commit()
    assert Decimal(str(record.annual_rent)) == Decimal("60000")


async def test_the_pan_threshold_comes_from_configuration(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    rule_set = await _tds_rules(session, tenant)
    await _windows(session, tenant)

    # Raise the threshold; the same rent now needs no PAN.
    rule_set.rules = {**rule_set.rules, "landlord_pan_threshold_annual": 500000}
    await session.commit()

    record = await tax_service.submit_house_rent(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        monthly_rent=[{"month": m, "rent": "25000"} for m in range(1, 13)],
        landlord_name="A Landlord",
        landlord_pan=None,
        property_address="Somewhere",
        is_metro=True,
    )
    await session.commit()
    assert record.landlord_pan is None


# ═══════════════════════════════════════════════════════════════════════════
# Mid-year joiner and rule-set gating
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_mid_year_joiner_with_previous_employer_income_computes_correctly(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate."""
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    await _salary(session, tenant, hr_user, employee)

    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.NEW
    )
    await session.commit()

    without = await tax_service.compute_and_store(session, hr_user, employee.id, FY, for_month=10)

    session.add(
        PreviousEmployerIncome(
            tenant_id=tenant.id,
            employee_id=employee.id,
            financial_year=FY,
            employer_name="Previous Co",
            gross_salary=Decimal("600000"),
            tds_deducted=Decimal("40000"),
        )
    )
    await session.commit()

    with_previous = await tax_service.compute_and_store(
        session, hr_user, employee.id, FY, for_month=10
    )

    assert Decimal(str(with_previous.gross_annual_income)) == Decimal(
        str(without.gross_annual_income)
    ) + Decimal("600000")
    assert with_previous.annual_tax_liability > without.annual_tax_liability
    assert any("previous employer" in n for n in with_previous.working["notes"])


async def test_an_unapproved_rule_set_blocks_the_projection(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The Compliance SME gate, in code rather than in process."""
    await _tds_rules(session, tenant, approved=False)
    await _windows(session, tenant)

    with pytest.raises(TaxWorkflowError, match="not been approved by a Compliance SME"):
        await tax_service.income_tax_rules(session, hr_user, FY)


async def test_a_missing_rule_set_names_what_the_sme_must_supply(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    with pytest.raises(TaxWorkflowError) as exc:
        await tax_service.income_tax_rules(session, hr_user, FY)
    assert "Compliance SME" in str(exc.value)
    assert "slabs, section limits" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════════
# Form 12BB and isolation
# ═══════════════════════════════════════════════════════════════════════════


async def test_form_12bb_shows_declared_and_accepted_side_by_side(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """An employee signing a 12BB should see what was allowed, not just claimed."""
    from app.services.tax.form_12bb import build_claims_table

    await _tds_rules(session, tenant)
    await _windows(session, tenant)

    declaration = await tax_service.upsert_declaration(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        section="80C",
        sub_category="PPF",
        declared_amount=Decimal("150000"),
        description="Public Provident Fund",
    )
    declaration.status = DeclarationStatus.SUBMITTED
    await session.commit()

    proof = TaxProof(
        tenant_id=tenant.id, declaration_id=declaration.id, proof_amount=Decimal("150000")
    )
    session.add(proof)
    await session.commit()
    await tax_service.verify_proof(
        session,
        hr_user,
        proof.id,
        status_decision=ProofStatus.PARTIALLY_ACCEPTED,
        accepted_amount=Decimal("90000"),
        remarks="Partially supported.",
    )

    table = await build_claims_table(session, hr_user, employee.id, FY)

    assert "1,50,000.00" in table, "declared, in Indian grouping"
    assert "90,000.00" in table, "accepted"
    assert "Public Provident Fund" in table


async def test_the_claims_table_escapes_values(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    from app.services.tax.form_12bb import build_claims_table

    await _tds_rules(session, tenant)
    await _windows(session, tenant)

    await tax_service.submit_house_rent(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        monthly_rent=[{"month": 4, "rent": "5000"}],
        landlord_name="<script>alert(1)</script>",
        landlord_pan=None,
        property_address="Somewhere",
        is_metro=False,
    )
    await session.commit()

    table = await build_claims_table(session, hr_user, employee.id, FY)
    assert "<script>" not in table
    assert "&lt;script&gt;" in table


async def test_one_tenant_cannot_read_anothers_declarations(
    session: AsyncSession,
    tenant: Tenant,
    other_hr_user: User,
    hr_user: User,
    employee: Employee,
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    await tax_service.upsert_declaration(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        section="80C",
        sub_category="PPF",
        declared_amount=Decimal("100000"),
    )
    await session.commit()

    from app.core.deps import tenant_scoped

    theirs = (
        await session.scalars(tenant_scoped(select(TaxDeclaration), TaxDeclaration, other_hr_user))
    ).all()
    assert theirs == []


async def test_a_snapshot_stores_the_full_working(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """An employee asking "why did my TDS jump" needs the previous projection."""
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    await _salary(session, tenant, hr_user, employee)
    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.OLD
    )
    await tax_service.submit_house_rent(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year=FY,
        monthly_rent=[{"month": m, "rent": "25000"} for m in range(1, 13)],
        landlord_name="A Landlord",
        landlord_pan="ABCDE1234F",
        property_address="Bengaluru",
        is_metro=True,
    )
    await session.commit()

    snapshot = await tax_service.compute_and_store(session, hr_user, employee.id, FY, for_month=4)

    assert snapshot.working["hra"]["binding_constraint"]
    assert "limited by" in snapshot.working["hra"]["explanation"]
    assert snapshot.working["slabs"]
    assert snapshot.working["months_remaining"] == 12
    assert snapshot.rule_set_id is not None, "the exact rules used stay provable"


async def test_snapshots_accumulate_so_a_change_can_be_compared(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _tds_rules(session, tenant)
    await _windows(session, tenant)
    await _salary(session, tenant, hr_user, employee)
    await tax_service.elect_regime(
        session, hr_user, employee_id=employee.id, financial_year=FY, regime=TaxRegime.OLD
    )
    await session.commit()

    await tax_service.compute_and_store(session, hr_user, employee.id, FY, for_month=4)
    await tax_service.compute_and_store(session, hr_user, employee.id, FY, for_month=5)

    count = await session.scalar(select(func.count(TaxComputationSnapshot.id)))
    assert count == 2
