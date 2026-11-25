"""CGB's increment letter: fixed CTC and variable pay stated apart.

Their letter for Maganti Sivani reads "Fixed CTC : INR 10,00,000 / Variable
Pay : INR 2,00,000 ... Your Fixed CTC is INR 10 LPA, along with a target
Variable Pay of INR 2 Lakhs, payable on generating revenue of INR 1.5 Cr from
January 2027 to December 2027", then a "1. Fixed CTC" annexure whose CTC is
10,00,000 and a "2. Variable Plan" table. Here every figure comes from the
salary in force (the SALES INCENTIVE line is the variable pay), the revenue
target and period are typed, and somebody without variable pay gets the same
letter without the variable-pay passages.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documents import IssuedLetter, LetterTemplate
from app.models.employee import Employee
from app.models.performance_flow import IncrementRecommendation
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate, SalaryComponentAmountInput
from app.services.documents import service as letter_service
from app.services.documents.default_templates import INCREMENT_CGB_SPEC
from app.services.payroll import service as salary_service
from app.services.performance import service as performance
from tests.integration.conftest import requires_db
from tests.integration.test_letterhead_letters import _keep, _text
from tests.integration.test_performance_flow import make_employee, open_cycle
from tests.integration.test_sales_incentive_flow import _setup

pytestmark = requires_db

REVISED_ON = date(2026, 11, 1)

#: Maganti Sivani's annexure, typed month-wise; the variable pay a year.
WITH_VARIABLE = [
    ("BASIC", "41667", "monthly"),
    ("HRA", "16667", "monthly"),
    ("SPECIAL", "19746", "monthly"),
    ("INSURANCE", "239", "monthly"),
    ("SALES_INCENTIVE", "200000", "annual"),
]
FIXED_ONLY = WITH_VARIABLE[:-1]


async def _salary(session, user, employee, structure, lines, ctc, on=REVISED_ON):
    return await salary_service.assign_salary(
        session,
        user,
        employee.id,
        EmployeeSalaryCreate(
            entry="manual",
            structure_id=structure.id,
            ctc_amount=Decimal(ctc),
            ctc_basis="annual",
            effective_from=on,
            components=[
                SalaryComponentAmountInput(component_code=c, amount=Decimal(a), basis=b)
                for c, a, b in lines
            ],
        ),
    )


async def _cgb_template(session: AsyncSession, user: User) -> LetterTemplate:
    """Saved as HR saves an edited template: the authoring check must pass."""
    spec = INCREMENT_CGB_SPEC
    return await letter_service.create_template_version(
        session,
        user,
        code=spec["code"],
        payload={
            "name": spec["name"],
            "category": spec["category"],
            "body_html": spec["body_html"].strip(),
            "header_html": None,
            "footer_html": None,
            "requires_approval": False,
            "approver_roles": [],
            "requires_signature": False,
            "signatory_role": None,
            "use_letterhead": True,
        },
        fields=spec["fields"],
    )


@pytest.fixture
async def setup(session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee):
    structure = await _setup(session, tenant, hr_user)
    employee.designation = "RPO Recruiter"
    employee.notice_period_days = 90
    await session.commit()
    template = await _cgb_template(session, hr_user)
    return structure, template


async def test_the_letter_states_fixed_ctc_and_variable_pay_apart(
    session: AsyncSession, hr_user: User, employee: Employee, setup
) -> None:
    structure, template = setup
    await _salary(session, hr_user, employee, structure, WITH_VARIABLE, "1200000")

    letter = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=template.id,
        manual_values={
            "revenue_target": "1.5 Cr",
            "performance_period": "January 2027 to December 2027",
        },
    )
    text = _text(letter.merged_html)

    assert "Increment Letter" in text
    assert "Dear Anita Sharma ," in text
    assert "with effect from 01st November 2026" in text
    assert "Fixed CTC : INR 10,00,000" in text
    assert "Variable Pay : INR 2,00,000" in text
    assert (
        "Your Fixed CTC is INR 10 LPA, along with a target Variable Pay of INR 2 Lakhs, "
        "payable on generating revenue of INR 1.5 Cr from January 2027 to December 2027."
    ) in text
    assert "Your notice period has been revised to three months." in text
    assert "will take CGB to newer heights" in text
    assert "{{" not in letter.merged_html and "/if" not in letter.merged_html

    annexure = text.split("1. Fixed CTC")[1]
    assert "Name Anita Sharma" in annexure and "Designation RPO Recruiter" in annexure
    assert "W.E.F." not in annexure and "Effective from" not in annexure
    assert "Basic Salary 41,667 5,00,004" in annexure or "Basic Salary 41,667 5,00,000" in annexure
    assert "Medical Insurance Premium 239" in annexure
    # The table's CTC is the fixed CTC; the variable pay is the plan below.
    assert "Cost to Company (CTC) 83,333 10,00,000" in annexure
    assert "Sales Incentive" not in annexure

    plan = text.split("2. Variable Plan:")[1]
    assert "Target Variable Pay INR 2,00,000 (2 Lakhs)" in plan
    assert "Revenue Target INR 1.5 Cr" in plan
    assert "Performance Period January 2027 to December 2027" in plan
    assert "Payout At the end of the year, on achieving the revenue target" in plan
    assert "receipt of this Increment letter" in plan

    _, pdf = await letter_service.letter_pdf(session, hr_user, letter.id)
    assert pdf.startswith(b"%PDF-")
    _keep("cgb_increment_variable.pdf", pdf)


async def test_without_variable_pay_the_variable_passages_are_left_out(
    session: AsyncSession, hr_user: User, employee: Employee, setup
) -> None:
    structure, template = setup
    await _salary(session, hr_user, employee, structure, FIXED_ONLY, "1000000")

    letter = await letter_service.issue_letter(
        session, hr_user, employee_id=employee.id, template_id=template.id
    )
    text = _text(letter.merged_html)
    assert "Fixed CTC : INR 10,00,000" in text
    assert "Variable" not in text
    assert "LPA" not in text and "revenue" not in text.lower()
    assert "Cost to Company (CTC) 83,333 10,00,000" in text
    assert "1. Fixed CTC" in text and "2. Variable Plan" not in text
    # No empty paragraph where a passage was dropped.
    assert "<p></p>" not in letter.merged_html
    _, pdf = await letter_service.letter_pdf(session, hr_user, letter.id)
    _keep("cgb_increment_fixed.pdf", pdf)


async def test_without_a_revenue_target_the_plan_is_left_out(
    session: AsyncSession, hr_user: User, employee: Employee, setup
) -> None:
    """Variable pay with no target typed: the amount is stated, no plan is made up."""
    structure, template = setup
    await _salary(session, hr_user, employee, structure, WITH_VARIABLE, "1200000")

    letter = await letter_service.issue_letter(
        session, hr_user, employee_id=employee.id, template_id=template.id
    )
    text = _text(letter.merged_html)
    assert "Variable Pay : INR 2,00,000" in text
    assert "revenue" not in text.lower() and "2. Variable Plan" not in text


async def test_a_target_without_its_period_is_refused_naming_it(
    session: AsyncSession, hr_user: User, employee: Employee, setup
) -> None:
    structure, template = setup
    await _salary(session, hr_user, employee, structure, WITH_VARIABLE, "1200000")

    with pytest.raises(HTTPException) as exc:
        await letter_service.issue_letter(
            session,
            hr_user,
            employee_id=employee.id,
            template_id=template.id,
            manual_values={"revenue_target": "1.5 Cr"},
        )
    assert exc.value.status_code == 422
    assert "Performance period" in str(exc.value.detail)


async def test_a_target_for_somebody_with_no_variable_pay_is_refused(
    session: AsyncSession, hr_user: User, employee: Employee, setup
) -> None:
    """Typing a target for a person whose salary has no variable pay is a mistake."""
    structure, template = setup
    await _salary(session, hr_user, employee, structure, FIXED_ONLY, "1000000")

    with pytest.raises(HTTPException) as exc:
        await letter_service.issue_letter(
            session,
            hr_user,
            employee_id=employee.id,
            template_id=template.id,
            manual_values={"revenue_target": "1.5 Cr", "performance_period": "2027"},
        )
    assert exc.value.status_code == 422
    assert "Variable pay" in str(exc.value.detail)


async def test_no_notice_period_on_record_leaves_the_line_out(
    session: AsyncSession, hr_user: User, employee: Employee, setup
) -> None:
    structure, template = setup
    employee.notice_period_days = None
    await session.commit()
    await _salary(session, hr_user, employee, structure, FIXED_ONLY, "1000000")

    letter = await letter_service.issue_letter(
        session, hr_user, employee_id=employee.id, template_id=template.id
    )
    assert "notice period" not in _text(letter.merged_html).lower()

    typed = await letter_service.issue_letter(
        session,
        hr_user,
        employee_id=employee.id,
        template_id=template.id,
        manual_values={"notice_period": "3 months"},
    )
    assert "revised to 3 months." in _text(typed.merged_html)


async def test_the_preview_shows_the_letter_with_its_passages_decided(
    session: AsyncSession, hr_user: User, employee: Employee, setup
) -> None:
    from app.schemas.letters import PreviewRequest

    structure, template = setup
    await _salary(session, hr_user, employee, structure, WITH_VARIABLE, "1200000")
    preview = await letter_service.preview_template(
        session,
        hr_user,
        template.id,
        PreviewRequest(employee_id=employee.id, manual_values={}),
    )
    text = _text(preview.html)
    assert "Variable Pay : INR 2,00,000" in text and "2. Variable Plan" not in text
    assert "{{" not in preview.html
    assert preview.problems == []


async def test_releasing_an_increment_cycle_issues_cgbs_letter(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity, location
) -> None:
    """The cycle types nothing: the letter must still issue, for everybody."""
    structure = await _setup(session, tenant, hr_user)
    await _cgb_template(session, hr_user)
    person = await make_employee(session, tenant, legal_entity, location, "INC9")
    await _salary(
        session, hr_user, person, structure, WITH_VARIABLE, "1200000", on=date(2026, 4, 1)
    )
    cycle = await open_cycle(session, hr_user)  # effective 1 Apr 2027
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=person.id,
        rating_label="Meets",
        current_ctc=Decimal("1200000"),
        percentage=Decimal("6"),
    )
    recommendation_id = uuid.UUID(out["recommendation_id"])
    await performance.decide_recommendation(
        session, hr_user, recommendation_id=recommendation_id, approve=True, note="Approved."
    )

    result = await performance.release_letters(
        session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25)
    )
    assert result["applied"]["applied"] == 1

    rec = await session.scalar(
        select(IncrementRecommendation).where(IncrementRecommendation.id == recommendation_id)
    )
    letter = await session.scalar(select(IssuedLetter).where(IssuedLetter.id == rec.letter_id))
    text = _text(letter.merged_html)
    assert "Increment Letter" in text
    assert "with effect from 01st April 2027" in text
    assert re.search(r"Fixed CTC : INR [\d,]+", text)
    assert "2. Variable Plan" not in text
