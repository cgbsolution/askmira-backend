"""Slice 03 gates that need a real database.

The headline one is concurrency: reference numbers on legal documents must be
unique under parallel issuance. That cannot be tested against a fake — it needs
real row locks in a real transaction manager.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.documents import (
    FieldSource,
    IssuedLetter,
    LetterCategory,
    LetterTemplate,
    LetterTemplateField,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.payroll import (
    CalculationType,
    ComponentType,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.documents import service as letter_service
from app.services.documents.service import financial_year
from app.services.payroll import service as salary_service
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _template(
    session: AsyncSession,
    tenant: Tenant,
    *,
    code: str = "OFFER_STD",
    body: str = "<p>Dear {{employee.first_name}}, welcome.</p>",
    category: LetterCategory = LetterCategory.OFFER,
) -> LetterTemplate:
    template = LetterTemplate(
        tenant_id=tenant.id,
        code=code,
        name="Offer letter",
        category=category,
        body_html=body,
        version=1,
        active=True,
    )
    template.fields.append(
        LetterTemplateField(
            tenant_id=tenant.id,
            token="employee.first_name",
            label="First name",
            source=FieldSource.EMPLOYEE,
            required=True,
        )
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


async def _salary_structure(session: AsyncSession, tenant: Tenant) -> SalaryStructure:
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
    special = SalaryComponent(
        tenant_id=tenant.id,
        code="SPECIAL",
        name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE,
        display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()

    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard",
        effective_from=date(2026, 1, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for index, component in enumerate((basic, special)):
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
    await session.refresh(structure)
    return structure


# ═══════════════════════════════════════════════════════════════════════════
# Reference numbering
# ═══════════════════════════════════════════════════════════════════════════


async def test_reference_numbers_are_sequential_and_formatted(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    template = await _template(session, tenant)

    references = []
    for _ in range(3):
        letter = await letter_service.issue_letter(
            session,
            hr_user,
            employee_id=employee.id,
            template_id=template.id,
            as_of=date(2026, 6, 1),
            generate_pdf=False,
        )
        references.append(letter.reference_number)

    prefix = tenant.slug[:3].upper()
    assert references[0] == f"{prefix}/OFF/2026-27/00001"
    assert references[1] == f"{prefix}/OFF/2026-27/00002"
    assert references[2] == f"{prefix}/OFF/2026-27/00003"


async def test_fifty_parallel_issuances_produce_fifty_unique_references(
    engine, session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Each task gets its own session, which is what makes this a genuine
    concurrency test — a shared session would serialise everything and prove
    nothing.
    """
    template = await _template(session, tenant)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def issue_one() -> str:
        async with factory() as own_session:
            letter = await letter_service.issue_letter(
                own_session,
                hr_user,
                employee_id=employee.id,
                template_id=template.id,
                as_of=date(2026, 6, 1),
                generate_pdf=False,
            )
            return letter.reference_number

    references = await asyncio.gather(*(issue_one() for _ in range(50)))

    assert len(references) == 50
    assert len(set(references)) == 50, (
        "duplicate reference numbers under concurrency — the row lock is not holding"
    )

    stored = await session.scalar(select(func.count(IssuedLetter.id)))
    assert stored == 50


async def test_the_financial_year_runs_april_to_march() -> None:
    assert financial_year(date(2026, 4, 1)) == "2026-27"
    assert financial_year(date(2027, 3, 31)) == "2026-27"
    assert financial_year(date(2027, 4, 1)) == "2027-28"
    assert financial_year(date(2026, 2, 15)) == "2025-26"


async def test_categories_number_independently(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    offer = await _template(session, tenant, code="OFF", category=LetterCategory.OFFER)
    experience = await _template(session, tenant, code="EXP", category=LetterCategory.EXPERIENCE)

    first = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=offer.id,
        as_of=date(2026, 6, 1),
        generate_pdf=False,
    )
    second = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=experience.id,
        as_of=date(2026, 6, 1),
        generate_pdf=False,
    )

    assert first.reference_number.endswith("00001")
    assert second.reference_number.endswith("00001")
    assert "/OFF/" in first.reference_number
    assert "/EXP/" in second.reference_number


# ═══════════════════════════════════════════════════════════════════════════
# Template versioning immutability
# ═══════════════════════════════════════════════════════════════════════════


async def test_editing_a_template_leaves_an_issued_letter_untouched(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate: `merged_html` and the stored hash do not move."""
    template = await _template(
        session, tenant, body="<p>Version one for {{employee.first_name}}</p>"
    )

    letter = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=template.id,
        as_of=date(2026, 6, 1),
        generate_pdf=False,
    )
    original_html = letter.merged_html
    original_hash = letter.content_hash
    original_version = letter.template_version

    # A new version of the same code.
    await letter_service.create_template_version(
        session,
        hr_user,
        code=template.code,
        payload={
            "name": "Offer letter",
            "category": LetterCategory.OFFER,
            "body_html": "<p>Completely different text</p>",
            "header_html": None,
            "footer_html": None,
            "requires_approval": False,
            "approver_roles": [],
            "requires_signature": False,
            "signatory_role": None,
        },
    )

    await session.refresh(letter)
    assert letter.merged_html == original_html
    assert letter.content_hash == original_hash
    assert letter.template_version == original_version == 1

    _, intact = await letter_service.verify_letter(session, hr_user, letter.id)
    assert intact


async def test_a_new_version_deactivates_the_previous_one(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    template = await _template(session, tenant)
    await letter_service.create_template_version(
        session,
        hr_user,
        code=template.code,
        payload={
            "name": "Offer letter v2",
            "category": LetterCategory.OFFER,
            "body_html": "<p>v2</p>",
            "header_html": None,
            "footer_html": None,
            "requires_approval": False,
            "approver_roles": [],
            "requires_signature": False,
            "signatory_role": None,
        },
    )

    await session.refresh(template)
    assert template.active is False

    versions = (
        await session.scalars(select(LetterTemplate).where(LetterTemplate.code == template.code))
    ).all()
    assert len(versions) == 2
    assert sum(1 for v in versions if v.active) == 1


# ═══════════════════════════════════════════════════════════════════════════
# As-of resolution
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_letter_for_a_past_date_shows_the_historical_salary(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A relieving letter for a past exit must not show today's salary."""
    structure = await _salary_structure(session, tenant)
    for effective, ctc in ((date(2026, 4, 1), "1200000"), (date(2026, 10, 1), "1800000")):
        await salary_service.assign_salary(
            session,
            hr_user,
            employee.id,
            EmployeeSalaryCreate(
                structure_id=structure.id,
                ctc_annual=Decimal(ctc),
                effective_from=effective,
            ),
        )

    template = await _template(session, tenant, body="<p>CTC: {{salary.ctc_annual}}</p>")

    old = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=template.id,
        as_of=date(2026, 6, 30),
        generate_pdf=False,
    )
    current = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=template.id,
        as_of=date(2026, 12, 31),
        generate_pdf=False,
    )

    assert "12,00,000" in old.merged_html
    assert "18,00,000" not in old.merged_html
    assert "18,00,000" in current.merged_html


# ═══════════════════════════════════════════════════════════════════════════
# Bulk issuance
# ═══════════════════════════════════════════════════════════════════════════


async def test_bulk_issuance_reports_per_employee_outcomes(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """One bad record must not block the rest of an increment cycle."""
    template = await _template(session, tenant, body="<p>Hi {{employee.first_name}}</p>")

    good = []
    for index in range(5):
        person = Employee(
            tenant_id=tenant.id,
            employee_code=f"BULK{index:03d}",
            first_name=f"Person{index}",
            work_email=f"p{index}@acme.test",
            status=EmploymentStatus.ACTIVE,
        )
        session.add(person)
        good.append(person)
    await session.commit()

    import uuid as uuid_module

    ids = [p.id for p in good] + [uuid_module.uuid4()]  # one that does not exist
    job = await letter_service.issue_bulk(
        session, hr_user, template_id=template.id, employee_ids=ids, as_of=date(2026, 6, 1)
    )

    assert job.total == 6
    assert job.succeeded == 5
    assert job.failed == 1
    assert job.status == "completed"

    failures = [r for r in job.results if r["error"]]
    assert len(failures) == 1
    assert "not found" in failures[0]["error"].lower()


async def test_bulk_issuance_of_two_hundred_letters_completes(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: 200 letters without timing out.

    Rendering PDFs for 200 letters is the slow part and belongs in a background
    worker; this asserts the issuance path itself scales.
    """
    template = await _template(session, tenant, body="<p>Hi {{employee.first_name}}</p>")

    people = [
        Employee(
            tenant_id=tenant.id,
            employee_code=f"MASS{index:04d}",
            first_name=f"Person{index}",
            work_email=f"mass{index}@acme.test",
            status=EmploymentStatus.ACTIVE,
        )
        for index in range(200)
    ]
    session.add_all(people)
    await session.commit()

    job = await letter_service.issue_bulk(
        session,
        hr_user,
        template_id=template.id,
        employee_ids=[p.id for p in people],
        as_of=date(2026, 6, 1),
    )

    assert job.succeeded == 200
    assert job.failed == 0

    references = {r["reference_number"] for r in job.results if r["reference_number"]}
    assert len(references) == 200, "every letter in a bulk run needs its own reference"


# ═══════════════════════════════════════════════════════════════════════════
# Integrity and isolation
# ═══════════════════════════════════════════════════════════════════════════


async def test_tampering_with_stored_html_is_detected(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    template = await _template(session, tenant)
    letter = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=template.id,
        as_of=date(2026, 6, 1),
        generate_pdf=False,
    )

    _, intact = await letter_service.verify_letter(session, hr_user, letter.id)
    assert intact

    letter.merged_html = letter.merged_html.replace("welcome", "you are dismissed")
    await session.commit()

    _, still_intact = await letter_service.verify_letter(session, hr_user, letter.id)
    assert still_intact is False


async def test_one_tenant_cannot_read_anothers_letters(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
) -> None:
    template = await _template(session, tenant)
    letter = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=template.id,
        as_of=date(2026, 6, 1),
        generate_pdf=False,
    )

    with pytest.raises(HTTPException) as exc:
        await letter_service.verify_letter(session, other_hr_user, letter.id)
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException):
        await letter_service.get_template(session, other_hr_user, template.id)


async def test_a_required_manual_field_blocks_issuance_with_a_usable_message(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    template = LetterTemplate(
        tenant_id=tenant.id,
        code="NEEDS_MANUAL",
        name="Needs a manual field",
        category=LetterCategory.OFFER,
        body_html="<p>Joining on {{manual.joining_date}}</p>",
        version=1,
        active=True,
    )
    template.fields.append(
        LetterTemplateField(
            tenant_id=tenant.id,
            token="manual.joining_date",
            label="Date of joining",
            source=FieldSource.MANUAL,
            required=True,
        )
    )
    session.add(template)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await letter_service.issue_letter(
            session,
            hr_user,
            employee_id=employee.id,
            template_id=template.id,
            generate_pdf=False,
        )
    assert exc.value.status_code == 422
    assert "Date of joining" in exc.value.detail
