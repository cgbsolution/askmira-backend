"""Letters on the workspace's letterhead, the default letters, and their fields.

The default templates are HR's own letters (offer, promotion with its salary
annexure, experience, joining form). These tests issue each one against a real
employee with a real salary and PF rules, and check the values that must come
out of the record rather than be typed: name, designation, CTC, notice period
in words, the annexure's figures.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documents import IssuedLetter, LetterTemplate
from app.models.employee import Employee, Gender
from app.models.payroll import (
    CalculationType,
    ComponentType,
    PayrollRuleSet,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.letters import LetterPdfPreviewRequest
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.documents import letterhead as letterhead_service
from app.services.documents import service as letter_service
from app.services.documents.default_templates import DEFAULT_TEMPLATES
from app.services.documents.letterhead import LetterheadSettings
from app.services.payroll import service as salary_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

PF_RULES = {
    "wage_ceiling": 15000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_wage_ceiling": 15000,
    "edli_wage_ceiling": 15000,
    "eps_rate": 8.33,
    "apply_wage_ceiling": True,
}


async def _structure(session: AsyncSession, tenant: Tenant) -> SalaryStructure:
    """Basic 50% of CTC, HRA 40% of Basic, gratuity 4.81% of Basic, Special the rest."""
    specs = [
        ("BASIC", "BASIC SALARY", ComponentType.EARNING, CalculationType.PERCENTAGE_OF, "CTC",
         Decimal(50), None, True),
        ("HRA", "HOUSE RENT ALLOWANCE", ComponentType.EARNING, CalculationType.PERCENTAGE_OF,
         "BASIC", Decimal(40), None, False),
        ("GRATUITY", "GRATUITY AMOUNT MANUAL", ComponentType.EMPLOYER_CONTRIBUTION,
         CalculationType.FORMULA, None, None, "BASIC * 0.0481", False),
        ("SPECIAL", "SPECIAL ALLOWANCE", ComponentType.EARNING, CalculationType.BALANCE, None,
         None, None, False),
    ]  # fmt: skip
    components = []
    for order, (code, name, kind, calc, base, value, formula, pf) in enumerate(specs):
        component = SalaryComponent(
            tenant_id=tenant.id,
            code=code,
            name=name,
            component_type=kind,
            calculation_type=calc,
            base_component_code=base,
            value=value,
            formula=formula,
            counts_toward_pf=pf,
            display_order=order,
        )
        components.append(component)
    session.add_all(components)
    await session.flush()
    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard CTC",
        effective_from=date(2020, 1, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for order, component in enumerate(components):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id,
                component_id=component.id,
                component=component,
                display_order=order,
            )
        )
    session.add(structure)
    session.add(
        PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="pf",
            jurisdiction="IN",
            effective_from=date(2020, 1, 1),
            rules=PF_RULES,
            approved_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await session.refresh(structure)
    return structure


async def _promoted(
    session: AsyncSession, hr_user: User, employee: Employee, tenant: Tenant
) -> date:
    """Anita on 5,00,000, revised to 6,20,000 from the first of next month."""
    structure = await _structure(session, tenant)
    today = date.today()
    raise_on = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    for ctc, effective in (("500000", date(today.year - 1, 4, 1)), ("620000", raise_on)):
        await salary_service.assign_salary(
            session,
            hr_user,
            employee.id,
            EmployeeSalaryCreate(
                structure_id=structure.id, ctc_annual=Decimal(ctc), effective_from=effective
            ),
        )
    employee.gender = Gender.FEMALE
    employee.designation = "Software Engineer"
    employee.notice_period_days = 90
    await session.commit()
    return raise_on


async def _default(session: AsyncSession, hr_user: User, code: str) -> LetterTemplate:
    await letter_service.list_templates(session, hr_user)
    template = await session.scalar(
        select(LetterTemplate).where(
            LetterTemplate.tenant_id == hr_user.tenant_id,
            LetterTemplate.code == code,
            LetterTemplate.active.is_(True),
        )
    )
    assert template is not None, code
    return template


def _text(markup: str) -> str:
    """The letter's words: tags dropped (a raised "st" joins its number), entities read."""
    import html

    joined = re.sub(r"</?sup>", "", markup)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", joined)))


def _keep(name: str, data: bytes) -> None:
    """Write a PDF for a person to look at, when LETTER_PDF_DIR is set."""
    folder = os.environ.get("LETTER_PDF_DIR")
    if folder:
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(data)


# ── Defaults ────────────────────────────────────────────────────────────────


async def test_the_default_letters_are_installed_once(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    rows = await letter_service.list_templates(session, hr_user)
    codes = {t.code for t in rows}
    assert {spec["code"] for spec in DEFAULT_TEMPLATES} <= codes

    # Switched off by HR: the next look does not put it back.
    promotion = next(t for t in rows if t.code == "PROMOTION")
    promotion.active = False
    await session.commit()
    again = await letter_service.list_templates(session, hr_user)
    assert "PROMOTION" not in {t.code for t in again}
    total = await session.scalar(
        select(func.count(LetterTemplate.id)).where(LetterTemplate.tenant_id == tenant.id)
    )
    assert total == len(DEFAULT_TEMPLATES)


async def test_a_default_template_saves_as_its_next_version_unchanged(
    session: AsyncSession, hr_user: User
) -> None:
    """Every default must pass the authoring check, or HR could never edit it."""
    for spec in DEFAULT_TEMPLATES:
        template = await _default(session, hr_user, spec["code"])
        saved = await letter_service.create_template_version(
            session,
            hr_user,
            code=template.code,
            payload={
                "name": template.name,
                "category": template.category,
                "body_html": template.body_html,
                "header_html": None,
                "footer_html": None,
                "requires_approval": False,
                "approver_roles": [],
                "requires_signature": False,
                "signatory_role": None,
                "use_letterhead": True,
            },
            fields=[
                {
                    "token": f.token,
                    "label": f.label,
                    "source": f.source,
                    "source_path": f.source_path,
                    "required": f.required,
                }
                for f in template.fields
            ],
        )
        assert saved.version == 2


# ── The promotion letter ────────────────────────────────────────────────────


async def test_a_promotion_letter_carries_the_new_salary_and_its_annexure(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    raise_on = await _promoted(session, hr_user, employee, tenant)
    template = await _default(session, hr_user, "PROMOTION")

    letter = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=template.id,
        manual_values={"designation": "Senior Software Engineer"},
    )
    text = _text(letter.merged_html)

    # The raise starts next month; the letter is written today and still
    # carries the new figures.
    assert "Rs. 6,20,000 per annum" in text
    assert "Promotion to Senior Software Engineer" in text
    assert "notice period has been revised to three months" in text
    assert raise_on.strftime("%B %Y") in text
    assert "Anita Sharma" in text and "Bengaluru" in text

    # The annexure: Basic 3,10,000 a year (25,833 a month), PF at 12% of the
    # 15,000 ceiling, gratuity from the structure, and the recorded CTC.
    assert "Basic Salary 25,833 3,10,000" in text
    assert "House Rent Allowance (HRA) 10,333 1,24,000" in text
    assert re.search(r"Employee's Contributions Provident Fund 1,800 21,600", text)
    # 14,911 a year is 1,242.58 a month, shown as 1,243.
    assert "Gratuity 1,243 14,911" in text
    assert "Cost to Company (CTC) 51,667 6,20,000" in text
    assert "Senior Software Engineer" in text.split("Annexure")[1]
    gross = re.search(r"Gross Salary ([\d,]+) ([\d,]+)", text)
    net = re.search(r"Net Salary / Take Home ([\d,]+) ([\d,]+)", text)
    assert gross and net
    monthly_gross = int(gross.group(1).replace(",", ""))
    assert int(net.group(1).replace(",", "")) == monthly_gross - 1800

    # Printed on the letterhead, which the letter keeps a copy of.
    assert letter.letterhead is not None
    assert letter.letterhead["design"] == "wave"
    assert letter.manual_values["notice_period"] == "three months"
    _, pdf = await letter_service.letter_pdf(session, hr_user, letter.id)
    assert pdf.startswith(b"%PDF-")
    _keep("promotion.pdf", pdf)
    _keep("promotion.html", letter.merged_html.encode())


async def test_a_pre_filled_value_can_be_overwritten(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _promoted(session, hr_user, employee, tenant)
    template = await _default(session, hr_user, "PROMOTION")

    prefill = await letter_service.prefill(session, hr_user, template.id, employee.id)
    assert prefill.values == {"designation": "Software Engineer", "notice_period": "three months"}
    assert prefill.as_of > date.today()

    letter = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=template.id,
        manual_values={"designation": "Tech Lead", "notice_period": "two months"},
    )
    assert "revised to two months" in _text(letter.merged_html)


async def test_the_annexure_says_why_when_payroll_rules_are_missing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _promoted(session, hr_user, employee, tenant)
    await session.execute(
        PayrollRuleSet.__table__.delete().where(PayrollRuleSet.tenant_id == tenant.id)
    )
    await session.commit()
    template = await _default(session, hr_user, "PROMOTION")

    with pytest.raises(HTTPException) as caught:
        await letter_service.issue_letter(
            session, hr_user, employee_id=employee.id, template_id=template.id
        )
    assert caught.value.status_code == 422
    assert "salary annexure could not be worked out" in str(caught.value.detail)
    assert await session.scalar(select(func.count(IssuedLetter.id))) == 0


# ── Other defaults ──────────────────────────────────────────────────────────


async def test_an_offer_letter_uses_mr_ms_when_gender_is_unknown(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _promoted(session, hr_user, employee, tenant)
    employee.gender = None
    employee.probation_end_date = employee.date_of_joining.replace(month=7)
    await session.commit()
    template = await _default(session, hr_user, "OFFER_CLIENT")

    letter = await letter_service.issue_letter(
        session, hr_user, employee_id=employee.id, template_id=template.id
    )
    text = _text(letter.merged_html)
    assert "Dear Mr. / Ms. Anita Sharma" in text
    assert "Probation Period : six months" in text
    assert "giving three months" in text and "notice in writing" in text
    assert "Thank you for showing interest in Acme Technologies!" in text
    _, pdf = await letter_service.letter_pdf(session, hr_user, letter.id)
    _keep("offer-client.pdf", pdf)


async def test_an_experience_letter_needs_the_last_working_day(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    employee.designation = "Analyst"
    await session.commit()
    template = await _default(session, hr_user, "EXPERIENCE")

    with pytest.raises(HTTPException) as caught:
        await letter_service.issue_letter(
            session, hr_user, employee_id=employee.id, template_id=template.id
        )
    assert "needs Last working day, which is empty on Anita Sharma's record" in str(
        caught.value.detail
    )

    employee.date_of_exit = date(2026, 3, 31)
    employee.gender = Gender.MALE
    await session.commit()
    letter = await letter_service.issue_letter(
        session, hr_user, employee_id=employee.id, template_id=template.id
    )
    text = _text(letter.merged_html)
    assert "Mr. Anita Sharma" in text
    assert "31st March 2026" in text
    assert "“Analyst”" in text
    _, pdf = await letter_service.letter_pdf(session, hr_user, letter.id)
    _keep("experience.pdf", pdf)


async def test_the_joining_form_prints_with_blanks_where_the_record_is_empty(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    template = await _default(session, hr_user, "JOINING_FORM")
    letter = await letter_service.issue_letter(
        session, hr_user, employee_id=employee.id, template_id=template.id
    )
    text = _text(letter.merged_html)
    assert "Full Name: Anita Sharma" in text
    assert "ACM001" in text
    assert "Name of the Organisation" in text
    _, pdf = await letter_service.letter_pdf(session, hr_user, letter.id)
    _keep("joining-form.pdf", pdf)


# ── The letterhead ──────────────────────────────────────────────────────────


async def test_the_letterhead_starts_from_the_workspace_and_round_trips(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    first = await letterhead_service.get_settings(session, hr_user)
    assert first.company_name == "Acme Technologies"
    assert first.design == "wave"

    saved = await letterhead_service.save_settings(
        session,
        hr_user,
        LetterheadSettings(
            design="classic",
            primary_colour="#7D0712",
            company_name="Acme Technologies Pvt Ltd",
            address="1 MG Road, Bengaluru 560001",
            phone="+91 80 1234 5678",
            email="hr@acme.test",
            website="www.acme.test",
            signatory_title="People Team",
        ),
        request=None,
    )
    again = await letterhead_service.get_settings(session, hr_user)
    assert again == saved
    assert again.primary_colour == "#7D0712"


async def test_an_issued_letter_keeps_the_letterhead_it_was_printed_on(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    template = await _default(session, hr_user, "JOINING_FORM")
    letter = await letter_service.issue_letter(
        session, hr_user, employee_id=employee.id, template_id=template.id
    )
    _, original = await letter_service.letter_pdf(session, hr_user, letter.id)

    await letterhead_service.save_settings(
        session,
        hr_user,
        LetterheadSettings(primary_colour="#000000", company_name="Renamed Ltd"),
        request=None,
    )
    tenant = await session.get(Tenant, hr_user.tenant_id)
    rendered = await letter_service.render_letter_pdf(
        letter.merged_html, letter.reference_number, letter.letterhead, tenant
    )
    assert rendered == original
    assert letter.letterhead["company_name"] == "Acme Technologies"


async def test_a_template_can_print_without_the_letterhead(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    template = await _default(session, hr_user, "JOINING_FORM")
    template.use_letterhead = False
    await session.commit()
    letter = await letter_service.issue_letter(
        session, hr_user, employee_id=employee.id, template_id=template.id
    )
    assert letter.letterhead["design"] == "none"


async def test_only_an_image_is_accepted_as_a_logo_or_signature(
    session: AsyncSession, hr_user: User
) -> None:
    with pytest.raises(HTTPException) as caught:
        await letterhead_service.save_image(
            session, hr_user, "signature", b"%PDF-1.4 not an image", request=None
        )
    assert caught.value.status_code == 422
    with pytest.raises(HTTPException):
        await letterhead_service.save_image(session, hr_user, "logo", b"", request=None)


async def test_a_signature_is_embedded_in_the_letter(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (120, 40), "white").save(buffer, format="PNG")
    await letterhead_service.save_image(
        session, hr_user, "signature", buffer.getvalue(), request=None
    )
    employee.date_of_exit = date(2026, 3, 31)
    employee.designation = "Analyst"
    await session.commit()
    template = await _default(session, hr_user, "EXPERIENCE")
    letter = await letter_service.issue_letter(
        session, hr_user, employee_id=employee.id, template_id=template.id
    )
    assert 'src="data:image/png;base64,' in letter.merged_html


async def test_a_pdf_preview_issues_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _promoted(session, hr_user, employee, tenant)
    template = await _default(session, hr_user, "PROMOTION")
    data = await letter_service.pdf_preview(
        session,
        hr_user,
        LetterPdfPreviewRequest(template_id=template.id, employee_id=employee.id),
    )
    assert data.startswith(b"%PDF-")
    assert await session.scalar(select(func.count(IssuedLetter.id))) == 0
    _keep("promotion-preview.pdf", data)
