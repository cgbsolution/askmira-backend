"""Slice 19 gates that need a real database.

The engines are proved in `tests/hiring/`. What only a database settles: that
the offer letter's breakup is byte-identical to what payroll computes, that
editing an approved offer really does void the approval rows, that JOINED
conversion re-keys nothing, and that a revocation actually puts the seat back.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documents import (
    FieldSource,
    IssuedLetter,
    LetterCategory,
    LetterTemplate,
    LetterTemplateField,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.hiring import Candidate, CandidateStage, Requisition, RequisitionStatus
from app.models.offer import (
    ApprovalChainRule,
    ApprovalDecision,
    ApprovalScope,
    Offer,
    OfferApproval,
    OfferComponentKind,
    OfferEvent,
    OfferStatus,
)
from app.models.org import Department, LegalEntity, Location
from app.models.payroll import (
    CalculationType,
    ComponentType,
    EmployeeSalary,
    EmployeeSalaryComponent,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.hiring import offers as offer_service
from app.services.hiring import requisitions as requisition_service
from app.services.payroll import service as payroll_service
from app.services.payroll.salary_structure import resolve_ctc
from tests.integration.conftest import requires_db

pytestmark = requires_db

BAND_MIN = Decimal("1500000")
BAND_MAX = Decimal("2000000")


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


async def _structure(session: AsyncSession, tenant: Tenant) -> SalaryStructure:
    """The same shape payroll uses everywhere else: a percentage, an HRA off
    that percentage, and a BALANCE component that absorbs the residual."""
    basic = SalaryComponent(
        tenant_id=tenant.id, code="BASIC", name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="CTC", value=Decimal(40),
        counts_toward_pf=True, display_order=1,
    )
    hra = SalaryComponent(
        tenant_id=tenant.id, code="HRA", name="House rent allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="BASIC", value=Decimal(50), display_order=2,
    )
    special = SalaryComponent(
        tenant_id=tenant.id, code="SPECIAL", name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE, display_order=9,
    )
    session.add_all([basic, hra, special])
    await session.flush()

    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=date(2026, 1, 1),
        applicable_grades=[], applicable_locations=[],
    )
    for index, component in enumerate((basic, hra, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id, component_id=component.id,
                component=component, display_order=index,
            )
        )
    session.add(structure)
    await session.commit()
    await session.refresh(structure)
    return structure


async def _offer_template(session: AsyncSession, tenant: Tenant) -> LetterTemplate:
    template = LetterTemplate(
        tenant_id=tenant.id, code="OFFER_19", name="Offer letter",
        category=LetterCategory.OFFER,
        body_html=(
            "<p>Dear {{employee.first_name}},</p>"
            "<p>We are pleased to offer you the role of {{employee.designation}} "
            "joining on {{employee.date_of_joining}}.</p>"
            "{{computed.ctc_table}}"
        ),
        version=1, active=True,
    )
    template.fields.append(
        LetterTemplateField(
            tenant_id=tenant.id, token="employee.first_name", label="First name",
            source=FieldSource.EMPLOYEE, required=True,
        )
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


async def _chain(session: AsyncSession, tenant: Tenant) -> None:
    """A complete rule set: two bands, an unbounded top, and a band step."""
    session.add_all(
        [
            ApprovalChainRule(
                tenant_id=tenant.id, scope=scope, sequence=sequence,
                approver_role=role, ctc_min=lo, ctc_max=hi,
                only_when_over_band=over_band, label=role,
            )
            for scope in (ApprovalScope.REQUISITION, ApprovalScope.OFFER)
            for sequence, role, lo, hi, over_band in (
                (1, "hiring_manager", Decimal(0), Decimal("2000000"), False),
                (2, "tenant_admin", Decimal("2000000"), None, False),
                (3, "finance_head", Decimal(0), None, True),
            )
        ]
    )
    await session.commit()


async def _requisition(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity,
    *, code: str = "REQ-001", openings: int = 1, headcount: int | None = 5,
) -> Requisition:
    requisition = Requisition(
        tenant_id=tenant.id, code=code, title="Senior Engineer",
        openings=openings, grade="L5",
        budget_ctc_min=BAND_MIN, budget_ctc_max=BAND_MAX,
        justification="Backfill for the platform team after two exits.",
        approved_headcount=headcount,
        status=RequisitionStatus.OPEN,
    )
    session.add(requisition)
    await session.commit()
    await session.refresh(requisition)
    return requisition


async def _candidate(
    session: AsyncSession, tenant: Tenant, requisition: Requisition,
    *, name: str = "Rohit Verma", email: str = "rohit@example.test",
) -> Candidate:
    candidate = Candidate(
        tenant_id=tenant.id, requisition_id=requisition.id, full_name=name,
        email=email, phone="+91 90000 12345", current_company="Acme Labs",
        current_ctc=Decimal("1400000"), expected_ctc=Decimal("1900000"),
        notice_period_days=90, source="referral", fit_score=Decimal("82.5"),
        stage=CandidateStage.HUMAN_INTERVIEW,
    )
    session.add(candidate)
    await session.commit()
    await session.refresh(candidate)
    return candidate


async def _draft(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    *, ctc: Decimal = Decimal("1800000"), code: str = "OFF-001",
    requisition: Requisition | None = None, candidate: Candidate | None = None,
    components: list[dict] | None = None,
) -> tuple[Offer, SalaryStructure, Candidate, Requisition]:
    structure = await _structure(session, tenant)
    requisition = requisition or await _requisition(session, tenant, legal_entity)
    candidate = candidate or await _candidate(session, tenant, requisition)
    offer = await offer_service.create_offer(
        session, hr_user,
        candidate_id=candidate.id, code=code,
        salary_structure_id=structure.id, ctc_annual=ctc,
        joining_date=date(2026, 11, 3), designation="Senior Engineer",
        grade="L5", legal_entity_id=legal_entity.id,
        valid_until=date(2026, 10, 15), components=components or [],
    )
    await session.commit()
    # The commit expired everything. Reload through awaits so the selectin
    # loaders run inside the greenlet rather than on first attribute access.
    await session.refresh(offer)
    structure = await payroll_service.get_structure(session, hr_user, structure.id)
    return offer, structure, candidate, requisition


async def _through_approval(
    session: AsyncSession, tenant: Tenant, hr_user: User, offer: Offer
) -> None:
    await _chain(session, tenant)
    await offer_service.submit_offer(session, hr_user, offer_id=offer.id)
    await session.commit()
    while offer.status is OfferStatus.PENDING_APPROVAL:
        await offer_service.approve_offer(session, hr_user, offer_id=offer.id, approve=True)
        await session.commit()


# ═══════════════════════════════════════════════════════════════════════════
# The breakup — the gate that matters most
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_offer_breakup_is_what_payroll_computes_for_the_same_structure(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    A mismatch between the offer letter and the first payslip is a trust
    incident on day one, and it is common in the market.
    """
    offer, structure, _, _ = await _draft(session, tenant, hr_user, legal_entity)

    payroll_breakdown = resolve_ctc(structure, Decimal("1800000"))
    stored = {c.component_code: c for c in offer.salary_components}

    assert set(stored) == {c.code for c in payroll_breakdown.components}
    for component in payroll_breakdown.components:
        row = stored[component.code]
        assert Decimal(str(row.annual_amount)) == component.annual_amount, component.code
        assert Decimal(str(row.monthly_amount)) == component.monthly_amount, component.code
        assert row.part_of_ctc == component.part_of_ctc


async def test_the_stored_breakup_reconciles_to_the_offered_ctc(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    total = sum(
        Decimal(str(c.annual_amount)) for c in offer.salary_components if c.part_of_ctc
    )
    assert total == Decimal("1800000.00")


async def test_changing_the_ctc_recomputes_the_breakup_through_the_same_engine(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    offer, structure, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    await offer_service.update_offer_terms(
        session, hr_user, offer_id=offer.id, changes={"ctc_annual": Decimal("2100000")}
    )
    await session.commit()
    await session.refresh(offer)

    structure = await payroll_service.get_structure(session, hr_user, structure.id)
    expected = resolve_ctc(structure, Decimal("2100000"))
    stored = {c.component_code: Decimal(str(c.annual_amount)) for c in offer.salary_components}
    assert stored == {c.code: c.annual_amount for c in expected.components}


# ═══════════════════════════════════════════════════════════════════════════
# The approval view
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_approval_payload_leads_with_the_budget_deviation(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: flagged prominently, not buried.

    Approvers approve. A deviation that is merely present on the page is a
    deviation that gets approved.
    """
    offer, _, _, _ = await _draft(
        session, tenant, hr_user, legal_entity, ctc=Decimal("2400000")
    )
    payload = await offer_service.approval_payload(session, hr_user, offer_id=offer.id)

    deviation = payload["budget_deviation"]
    assert deviation["is_over_band"]
    assert deviation["requires_prominent_flag"]
    assert deviation["amount_over"] == "400000.00"
    # It comes back with the offer, not from a second call the screen might
    # render before.
    assert list(payload).index("budget_deviation") < list(payload).index("salary_breakup")


async def test_internal_parity_returns_the_right_comparable_employees(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    location: Location,
) -> None:
    """The checklist gate.

    Same grade, same department. Nobody approving an offer knows what the other
    eleven L5s earn, and paying a new hire above existing staff is the most
    expensive offer mistake there is.
    """
    engineering = Department(tenant_id=tenant.id, name="Engineering", code="ENG")
    sales = Department(tenant_id=tenant.id, name="Sales", code="SLS")
    session.add_all([engineering, sales])
    await session.flush()

    structure = await _structure(session, tenant)
    # Three L5 engineers, one L5 in sales, one L3 engineer. Only the three
    # should count.
    people = [
        ("CMP001", "L5", engineering, Decimal("1700000")),
        ("CMP002", "L5", engineering, Decimal("1800000")),
        ("CMP003", "L5", engineering, Decimal("1900000")),
        ("CMP004", "L5", sales, Decimal("3000000")),
        ("CMP005", "L3", engineering, Decimal("800000")),
    ]
    for code, grade, department, ctc in people:
        person = Employee(
            tenant_id=tenant.id, employee_code=code, first_name="Comparable",
            last_name=code, work_email=f"{code.lower()}@acme.test",
            legal_entity_id=legal_entity.id, location_id=location.id,
            department_id=department.id, grade=grade,
            status=EmploymentStatus.ACTIVE, date_of_joining=date(2025, 4, 1),
        )
        session.add(person)
        await session.flush()
        await payroll_service.assign_salary(
            session, hr_user, person.id,
            EmployeeSalaryCreate(
                structure_id=structure.id, ctc_annual=ctc, effective_from=date(2026, 1, 1)
            ),
        )

    requisition = await _requisition(session, tenant, legal_entity)
    candidate = await _candidate(session, tenant, requisition)
    offer = await offer_service.create_offer(
        session, hr_user, candidate_id=candidate.id, code="OFF-PAR",
        salary_structure_id=structure.id, ctc_annual=Decimal("2200000"),
        joining_date=date(2026, 11, 3), designation="Senior Engineer",
        grade="L5", department_id=engineering.id, legal_entity_id=legal_entity.id,
        valid_until=date(2026, 10, 15),
    )
    await session.commit()

    payload = await offer_service.approval_payload(session, hr_user, offer_id=offer.id)
    parity = payload["internal_parity"]

    assert parity["comparable_count"] == 3, "the sales L5 and the L3 are not comparable"
    assert parity["minimum"] == "1700000.00"
    assert parity["median"] == "1800000.00"
    assert parity["maximum"] == "1900000.00"
    assert parity["above_every_comparable"] is True


async def test_parity_ignores_superseded_salary_revisions(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """A closed revision is what somebody used to earn. Comparing against
    history would make every offer look generous."""
    structure = await _structure(session, tenant)
    employee.grade = "L5"
    await session.flush()
    await payroll_service.assign_salary(
        session, hr_user, employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id, ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 1, 1),
        ),
    )
    await payroll_service.assign_salary(
        session, hr_user, employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id, ctc_annual=Decimal("1600000"),
            effective_from=date(2026, 4, 1), revision_reason="Annual increment",
        ),
    )

    requisition = await _requisition(session, tenant, legal_entity)
    candidate = await _candidate(session, tenant, requisition)
    offer = await offer_service.create_offer(
        session, hr_user, candidate_id=candidate.id, code="OFF-REV",
        salary_structure_id=structure.id, ctc_annual=Decimal("1800000"),
        joining_date=date(2026, 11, 3), designation="Senior Engineer",
        grade="L5", legal_entity_id=legal_entity.id, valid_until=date(2026, 10, 15),
    )
    await session.commit()

    parity = (
        await offer_service.approval_payload(session, hr_user, offer_id=offer.id)
    )["internal_parity"]
    assert parity["comparable_count"] == 1
    assert parity["median"] == "1600000.00", "the live revision, not the superseded one"


# ═══════════════════════════════════════════════════════════════════════════
# Approval voiding
# ═══════════════════════════════════════════════════════════════════════════


async def test_editing_a_material_term_voids_the_approval(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    An approval is a decision about specific numbers. Reusing it after those
    numbers change puts the approver on record for something they never saw.
    """
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    assert offer.status is OfferStatus.APPROVED

    result = await offer_service.update_offer_terms(
        session, hr_user, offer_id=offer.id, changes={"ctc_annual": Decimal("2100000")}
    )
    await session.commit()

    assert result["material_changes"] == ["ctc_annual"]
    assert result["approvals_voided"] >= 1
    await session.refresh(offer)
    assert offer.status is OfferStatus.PENDING_APPROVAL, "it goes back round"

    approvals = (
        await session.scalars(select(OfferApproval).where(OfferApproval.offer_id == offer.id))
    ).all()
    assert any(a.decision is ApprovalDecision.VOIDED for a in approvals)
    assert all(
        a.voided_by_changes == ["ctc_annual"]
        for a in approvals
        if a.decision is ApprovalDecision.VOIDED
    ), "the approver is told which term moved, not that 'the offer was edited'"


async def test_a_voided_offer_cannot_be_released(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)

    await offer_service.update_offer_terms(
        session, hr_user, offer_id=offer.id, changes={"ctc_annual": Decimal("2100000")}
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await offer_service.release_offer(
            session, hr_user, offer_id=offer.id, template_id=template.id
        )
    assert exc.value.status_code == 409
    assert "Only an approved offer can be released" in exc.value.detail


async def test_editing_a_non_material_field_leaves_the_approval_standing(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Voiding over a corrected validity date would train everyone to treat
    re-approval as noise — which is how a real CTC change slips through."""
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)

    result = await offer_service.update_offer_terms(
        session, hr_user, offer_id=offer.id, changes={"valid_until": date(2026, 10, 31)}
    )
    await session.commit()

    assert result["material_changes"] == []
    assert result["approvals_voided"] == 0
    await session.refresh(offer)
    assert offer.status is OfferStatus.APPROVED


async def test_going_over_the_band_adds_the_finance_step(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    offer, _, _, _ = await _draft(
        session, tenant, hr_user, legal_entity, ctc=Decimal("2400000")
    )
    await _chain(session, tenant)
    result = await offer_service.submit_offer(session, hr_user, offer_id=offer.id)
    await session.commit()

    assert result["over_band"] is True
    assert [step["role"] for step in result["chain"]] == ["tenant_admin", "finance_head"]


# ═══════════════════════════════════════════════════════════════════════════
# Release, letter and lapse
# ═══════════════════════════════════════════════════════════════════════════


async def test_release_produces_a_letter_through_the_letter_engine(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Same numbering series, same content hash, same template versioning.

    A parallel offer-letter renderer would have drifted from the appointment
    letter inside a release.
    """
    offer, _, candidate, requisition = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)

    result = await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()

    letter = await session.get(IssuedLetter, result["letter_id"])
    assert letter is not None
    assert letter.employee_id is None
    assert letter.candidate_id == candidate.id
    assert letter.content_hash
    assert letter.template_version == template.version
    assert "/OFR/" in letter.reference_number or letter.reference_number

    # The breakup on the letter is the breakup on the offer.
    for component in offer.salary_components:
        if component.part_of_ctc:
            assert component.component_name in letter.merged_html

    await session.refresh(candidate)
    assert candidate.stage is CandidateStage.OFFER
    await session.refresh(requisition)
    assert requisition.filled_count == 1
    assert requisition.status is RequisitionStatus.FULFILLED


async def test_an_unapproved_offer_cannot_be_released(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    template = await _offer_template(session, tenant)
    with pytest.raises(HTTPException) as exc:
        await offer_service.release_offer(
            session, hr_user, offer_id=offer.id, template_id=template.id
        )
    assert "exactly what the chain is for" in exc.value.detail


async def test_a_second_offer_cannot_take_a_seat_that_is_already_held(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Two offers released against one opening is a real thing that happens
    under pressure, and much cheaper to catch here than after both accept."""
    offer, structure, _, requisition = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()

    second_candidate = await _candidate(
        session, tenant, requisition, name="Priya Nair", email="priya@example.test"
    )
    second = await offer_service.create_offer(
        session, hr_user, candidate_id=second_candidate.id, code="OFF-002",
        salary_structure_id=structure.id, ctc_annual=Decimal("1800000"),
        joining_date=date(2026, 11, 3), designation="Senior Engineer",
        grade="L5", legal_entity_id=legal_entity.id, valid_until=date(2026, 10, 15),
    )
    await session.commit()
    await offer_service.submit_offer(session, hr_user, offer_id=second.id)
    await session.commit()
    while second.status is OfferStatus.PENDING_APPROVAL:
        await offer_service.approve_offer(session, hr_user, offer_id=second.id, approve=True)
        await session.commit()

    with pytest.raises(HTTPException) as exc:
        await offer_service.release_offer(
            session, hr_user, offer_id=second.id, template_id=template.id
        )
    assert "all of them already held" in exc.value.detail


async def test_an_offer_lapses_on_its_validity_date_and_reopens_the_seat(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    An offer that never lapses shows a candidate who went quiet in March as
    still live in September, and the requisition as still filled.
    """
    offer, _, _, requisition = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()

    result = await offer_service.lapse_due_offers(
        session, hr_user, as_of=offer.valid_until + timedelta(days=1)
    )
    await session.commit()

    assert result["lapsed"] == 1
    await session.refresh(offer)
    assert offer.status is OfferStatus.LAPSED
    await session.refresh(requisition)
    assert requisition.open_vacancies == 1
    assert requisition.status is RequisitionStatus.OPEN


async def test_an_offer_inside_its_validity_does_not_lapse(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()

    result = await offer_service.lapse_due_offers(
        session, hr_user, as_of=offer.valid_until - timedelta(days=1)
    )
    assert result["lapsed"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Drop and revocation
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_drop_records_a_reason_and_the_facts_needed_to_report_on_it(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate: offer-drop reason captured and queryable.

    Drop rate by department, recruiter and notice-period length is a material
    metric in this market, and a reason in a recruiter's memory cannot be
    reported on.
    """
    offer, _, _, requisition = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()
    await offer_service.accept_offer(session, hr_user, offer_id=offer.id)
    await session.commit()

    await offer_service.record_drop(
        session, hr_user, offer_id=offer.id,
        reason="Counter-offered by current employer at 20% above.",
    )
    await session.commit()

    await session.refresh(offer)
    assert offer.status is OfferStatus.DROPPED
    assert "Counter-offered" in offer.outcome_reason

    events = (
        await session.scalars(
            select(OfferEvent)
            .where(OfferEvent.offer_id == offer.id, OfferEvent.to_status == OfferStatus.DROPPED)
        )
    ).all()
    assert len(events) == 1
    assert events[0].facts["notice_period_days"] == 90, "snapshotted at the time"
    assert events[0].facts["grade"] == "L5"

    await session.refresh(requisition)
    assert requisition.open_vacancies == 1


async def test_a_drop_without_a_reason_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()
    await offer_service.accept_offer(session, hr_user, offer_id=offer.id)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await offer_service.record_drop(session, hr_user, offer_id=offer.id, reason="   ")
    assert "material metric" in exc.value.detail


async def test_a_drop_is_not_a_decline(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """A decline costs a search. A drop costs a search plus a start date the
    team already planned around."""
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()

    # Still only released: a drop needs an acceptance first.
    with pytest.raises(HTTPException) as exc:
        await offer_service.record_drop(
            session, hr_user, offer_id=offer.id, reason="Did not join"
        )
    assert "anything else is a decline" in exc.value.detail


async def test_revocation_reopens_the_vacancy(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate."""
    offer, _, _, requisition = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()
    await session.refresh(requisition)
    assert requisition.open_vacancies == 0

    await offer_service.revoke_offer(
        session, hr_user, offer_id=offer.id, reason="Role withdrawn after a budget freeze."
    )
    await session.commit()

    await session.refresh(offer)
    assert offer.status is OfferStatus.REVOKED
    await session.refresh(requisition)
    assert requisition.open_vacancies == 1
    assert requisition.status is RequisitionStatus.OPEN


async def test_revoking_on_someone_who_already_resigned_escalates_to_hr(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Legal and reputational exposure, so it is surfaced rather than logged."""
    offer, _, candidate, _ = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()
    await offer_service.accept_offer(session, hr_user, offer_id=offer.id)
    await session.commit()

    result = await offer_service.revoke_offer(
        session, hr_user, offer_id=offer.id,
        reason="Requisition cancelled.", candidate_resigned_elsewhere=True,
    )
    await session.commit()

    assert "hr_escalation" in result
    assert candidate.full_name in result["hr_escalation"]
    assert "without a job" in result["hr_escalation"]
    await session.refresh(offer)
    assert offer.resigned_elsewhere is True


# ═══════════════════════════════════════════════════════════════════════════
# Conversion — nothing re-keyed
# ═══════════════════════════════════════════════════════════════════════════


async def test_joining_creates_an_employee_with_nothing_re_keyed(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    location: Location,
) -> None:
    """The checklist gate, verified field by field.

    Only the employee code and work email are supplied at conversion, because
    neither exists before joining. Everything else already does, and typing it
    again is how a joining date becomes 2026-06-01 in one system and 2026-01-06
    in another.
    """
    engineering = Department(tenant_id=tenant.id, name="Engineering", code="ENG")
    session.add(engineering)
    await session.flush()

    structure = await _structure(session, tenant)
    requisition = await _requisition(session, tenant, legal_entity)
    candidate = await _candidate(session, tenant, requisition)
    offer = await offer_service.create_offer(
        session, hr_user, candidate_id=candidate.id, code="OFF-JOIN",
        salary_structure_id=structure.id, ctc_annual=Decimal("1800000"),
        joining_date=date(2026, 11, 3), designation="Senior Engineer",
        grade="L5", joining_location_id=location.id, department_id=engineering.id,
        legal_entity_id=legal_entity.id, probation_months=6, notice_period_days=90,
        valid_until=date(2026, 10, 15),
    )
    await session.commit()
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()
    await offer_service.accept_offer(session, hr_user, offer_id=offer.id)
    await session.commit()

    result = await offer_service.convert_to_employee(
        session, hr_user, offer_id=offer.id,
        employee_code="ACM900", work_email="rohit@acme.test",
    )
    await session.commit()

    employee = await session.get(Employee, result["employee_id"])
    assert employee is not None

    # Field by field, against the candidate and the offer.
    assert employee.first_name == "Rohit"
    assert employee.last_name == "Verma"
    assert employee.display_name == candidate.full_name
    assert employee.personal_email == candidate.email
    assert employee.phone == candidate.phone
    assert employee.designation == offer.designation
    assert employee.grade == offer.grade
    assert employee.date_of_joining == offer.joining_date
    assert employee.notice_period_days == offer.notice_period_days
    assert employee.location_id == offer.joining_location_id
    assert employee.department_id == offer.department_id
    assert employee.legal_entity_id == offer.legal_entity_id
    # Six months in months, not 180 days.
    assert employee.probation_end_date == date(2027, 5, 3)

    # The pre-hire record travels with them rather than being thrown away at
    # the moment they become an employee.
    assert employee.custom_fields["candidate_id"] == str(candidate.id)
    assert employee.custom_fields["fit_score"] == "82.50"
    assert employee.custom_fields["source"] == "referral"
    assert employee.custom_fields["notice_period_days_at_offer"] == 90

    await session.refresh(candidate)
    assert candidate.converted_employee_id == employee.id
    assert candidate.stage is CandidateStage.HIRED
    await session.refresh(offer)
    assert offer.status is OfferStatus.JOINED


async def test_the_joined_employees_salary_matches_the_offer_component_for_component(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The point of the whole slice: the first payslip resolves from the same
    inputs the offer letter did."""
    offer, structure, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    await _through_approval(session, tenant, hr_user, offer)
    template = await _offer_template(session, tenant)
    await offer_service.release_offer(
        session, hr_user, offer_id=offer.id, template_id=template.id
    )
    await session.commit()
    await offer_service.accept_offer(session, hr_user, offer_id=offer.id)
    await session.commit()
    result = await offer_service.convert_to_employee(
        session, hr_user, offer_id=offer.id,
        employee_code="ACM901", work_email="joined@acme.test",
    )
    await session.commit()

    salary = await session.scalar(
        select(EmployeeSalary).where(EmployeeSalary.employee_id == result["employee_id"])
    )
    assert salary is not None
    assert Decimal(str(salary.ctc_annual)) == Decimal(str(offer.ctc_annual))
    assert salary.structure_id == offer.salary_structure_id
    assert salary.effective_from == offer.joining_date

    payroll_rows = (
        await session.scalars(
            select(EmployeeSalaryComponent).where(
                EmployeeSalaryComponent.employee_salary_id == salary.id
            )
        )
    ).all()
    offered = {c.component_code: Decimal(str(c.annual_amount)) for c in offer.salary_components}
    actual = {c.component_code: Decimal(str(c.annual_amount)) for c in payroll_rows}
    assert offered == actual


async def test_an_unaccepted_offer_cannot_be_converted(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    with pytest.raises(HTTPException) as exc:
        await offer_service.convert_to_employee(
            session, hr_user, offer_id=offer.id,
            employee_code="ACM902", work_email="nope@acme.test",
        )
    assert "Only an accepted offer converts" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Requisition approval
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_requisition_over_approved_headcount_flags_at_approval(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate.

    This is where salary creep starts — one individually defensible approval at
    a time.
    """
    await _chain(session, tenant)
    requisition = await _requisition(
        session, tenant, legal_entity, openings=3, headcount=5
    )
    requisition.status = RequisitionStatus.DRAFT
    await session.commit()

    result = await requisition_service.submit_requisition(
        session, hr_user, requisition_id=requisition.id, live_headcount=4
    )
    await session.commit()

    headcount_flags = [f for f in result["flags"] if f["kind"] == "headcount"]
    assert len(headcount_flags) == 1
    assert "2 unbudgeted seats" in headcount_flags[0]["message"]
    assert headcount_flags[0]["severity"] == "blocking_attention"

    # And every approver's own row records that it was put in front of them.
    from app.models.offer import RequisitionApproval

    steps = (
        await session.scalars(
            select(RequisitionApproval).where(
                RequisitionApproval.requisition_id == requisition.id
            )
        )
    ).all()
    assert steps
    assert all(any(f["kind"] == "headcount" for f in s.flags_shown) for s in steps)


async def test_a_requisition_inside_its_headcount_raises_no_headcount_flag(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    await _chain(session, tenant)
    requisition = await _requisition(
        session, tenant, legal_entity, openings=1, headcount=5
    )
    requisition.status = RequisitionStatus.DRAFT
    await session.commit()

    result = await requisition_service.submit_requisition(
        session, hr_user, requisition_id=requisition.id, live_headcount=3
    )
    await session.commit()
    assert not [f for f in result["flags"] if f["kind"] == "headcount"]


async def test_no_configured_headcount_says_so_rather_than_passing_silently(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Slice 23 is not built for every tenant. A silent pass would read as a
    check that succeeded."""
    await _chain(session, tenant)
    requisition = await _requisition(session, tenant, legal_entity, headcount=None)
    requisition.status = RequisitionStatus.DRAFT
    await session.commit()

    result = await requisition_service.submit_requisition(
        session, hr_user, requisition_id=requisition.id
    )
    await session.commit()
    kinds = {f["kind"] for f in result["flags"]}
    assert "headcount_unknown" in kinds


async def test_a_requisition_with_no_justification_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    await _chain(session, tenant)
    requisition = await _requisition(session, tenant, legal_entity)
    requisition.status = RequisitionStatus.DRAFT
    requisition.justification = None
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await requisition_service.submit_requisition(
            session, hr_user, requisition_id=requisition.id
        )
    assert "signing a number" in exc.value.detail


async def test_a_rejection_ends_the_chain(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Collecting the rest of the signatures on something already refused
    wastes everyone's time and makes the refusal look negotiable."""
    await _chain(session, tenant)
    requisition = await _requisition(session, tenant, legal_entity)
    requisition.status = RequisitionStatus.DRAFT
    await session.commit()
    await requisition_service.submit_requisition(
        session, hr_user, requisition_id=requisition.id
    )
    await session.commit()

    result = await requisition_service.approve_requisition(
        session, hr_user, requisition_id=requisition.id, approve=False,
        note="Hire into the existing vacancy first.",
    )
    await session.commit()

    assert result["status"] == RequisitionStatus.REJECTED.value
    assert result["remaining_steps"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Offer-level components and isolation
# ═══════════════════════════════════════════════════════════════════════════


async def test_offer_level_components_are_material_but_not_salary_components(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """A joining bonus is part of what was agreed. Putting it in the structure
    would fold it into every month's PF base."""
    offer, _, _, _ = await _draft(
        session, tenant, hr_user, legal_entity,
        components=[
            {
                "kind": OfferComponentKind.JOINING_BONUS,
                "label": "Joining bonus",
                "amount": Decimal("200000"),
                "payable_terms": "With the first full month's salary.",
                "counts_toward_ctc": False,
            }
        ],
    )
    await session.refresh(offer)
    assert len(offer.components) == 1
    assert offer.components[0].amount == Decimal("200000.00")
    assert "JOINING_BONUS" not in {c.component_code for c in offer.salary_components}
    # The CTC still reconciles: the bonus is outside it.
    total = sum(
        Decimal(str(c.annual_amount)) for c in offer.salary_components if c.part_of_ctc
    )
    assert total == Decimal("1800000.00")


async def test_a_candidate_cannot_hold_two_live_offers(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    offer, structure, candidate, _ = await _draft(session, tenant, hr_user, legal_entity)
    with pytest.raises(HTTPException) as exc:
        await offer_service.create_offer(
            session, hr_user, candidate_id=candidate.id, code="OFF-DUP",
            salary_structure_id=structure.id, ctc_annual=Decimal("1800000"),
            joining_date=date(2026, 11, 3), designation="Senior Engineer",
            grade="L5", legal_entity_id=legal_entity.id, valid_until=date(2026, 10, 15),
        )
    assert "already has a live offer" in exc.value.detail


async def test_offers_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User,
    legal_entity: LegalEntity,
) -> None:
    offer, _, _, _ = await _draft(session, tenant, hr_user, legal_entity)
    with pytest.raises(HTTPException) as exc:
        await offer_service.approval_payload(session, other_hr_user, offer_id=offer.id)
    assert exc.value.status_code == 404
