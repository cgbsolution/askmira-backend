"""Form 12BB.

The statutory declaration an employee gives their employer for HRA, LTA,
home-loan interest and Chapter VI-A deductions. Rendered through the letter
engine from slice 03, so it inherits the same guarantees: a required field that
resolves to nothing blocks generation, values are HTML-escaped, the output is
hashed and re-verifiable, and it can be e-signed.

The structure below follows the prescribed form. No amount, limit or threshold
appears here — every figure comes from the employee's own declarations and the
SME-approved rule set.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.documents import FieldSource, LetterCategory, LetterTemplate, LetterTemplateField
from app.models.tax import (
    DeclarationStatus,
    HouseRentDeclaration,
    TaxDeclaration,
)
from app.models.user import User
from app.services.documents.indian_numbers import group_indian

FORM_12BB_CODE = "FORM_12BB"

# The four heads the prescribed form is organised under. Which *sections* fall
# under each is configuration on the rule set; this is the form's own structure.
FORM_HEADS = (
    ("hra", "1. House Rent Allowance"),
    ("lta", "2. Leave Travel Concession"),
    ("home_loan", "3. Deduction of interest on borrowing"),
    ("chapter_via", "4. Deduction under Chapter VI-A"),
)

BODY_HTML = """
<h2 style="text-align:center">FORM NO. 12BB</h2>
<p style="text-align:center;font-size:9pt">
  [See rule 26C]<br/>
  Statement showing particulars of claims by an employee for deduction of tax
  under section 192
</p>

<table class="ctc-table" cellspacing="0" cellpadding="6">
  <tr><td><strong>1. Name and address of the employee</strong></td>
      <td>{{computed.full_name}}<br/>{{employee.work_email}}</td></tr>
  <tr><td><strong>2. Permanent Account Number</strong></td>
      <td>{{manual.employee_pan}}</td></tr>
  <tr><td><strong>3. Financial year</strong></td>
      <td>{{manual.financial_year}}</td></tr>
</table>

<h3>Details of claims and evidence thereof</h3>
{{manual.claims_table}}

<h3>Verification</h3>
<p>
  I, {{computed.full_name}}, son/daughter of {{manual.parent_name}}, do hereby
  certify that the information given above is complete and correct.
</p>

<table cellspacing="0" cellpadding="6" style="width:100%;margin-top:18pt">
  <tr>
    <td>Place: {{manual.place}}<br/>Date: {{computed.letter_date}}</td>
    <td style="text-align:right">
      (Signature of the employee)<br/>
      Full Name: {{computed.full_name}}<br/>
      Designation: {{employee.designation}}
    </td>
  </tr>
</table>
"""


def seed_template(tenant_id: uuid.UUID) -> LetterTemplate:
    """The Form 12BB letter template.

    Not flagged `is_seed`: unlike an offer letter, the wording here is
    prescribed by rule 26C rather than being a starting point a client adapts.
    """
    template = LetterTemplate(
        tenant_id=tenant_id,
        code=FORM_12BB_CODE,
        name="Form 12BB — employee declaration",
        category=LetterCategory.CUSTOM,
        body_html=BODY_HTML,
        version=1,
        active=True,
        requires_approval=False,
        requires_signature=True,
        signatory_role="employee",
        is_seed=False,
    )

    for token, label, source in (
        ("computed.full_name", "Employee name", FieldSource.COMPUTED),
        ("computed.letter_date", "Date", FieldSource.COMPUTED),
        ("employee.work_email", "Work email", FieldSource.EMPLOYEE),
        ("employee.designation", "Designation", FieldSource.EMPLOYEE),
        ("manual.employee_pan", "Employee PAN", FieldSource.MANUAL),
        ("manual.financial_year", "Financial year", FieldSource.MANUAL),
        ("manual.claims_table", "Claims and evidence", FieldSource.MANUAL),
        ("manual.parent_name", "Father's / mother's name", FieldSource.MANUAL),
        ("manual.place", "Place", FieldSource.MANUAL),
    ):
        template.fields.append(
            LetterTemplateField(
                tenant_id=tenant_id, token=token, label=label, source=source, required=True
            )
        )

    return template


def _money(value: object) -> str:
    """Format an amount for the form, always to paise.

    Quantised here because the two columns can come from different sources —
    one from a database Numeric, one from an in-memory sum — and a row reading
    "1,50,000" beside "90,000.00" looks like a rendering bug on a statutory
    form.
    """
    from decimal import Decimal

    return group_indian(Decimal(str(value)).quantize(Decimal("0.01")))


def _cell(value: object) -> str:
    """Escape a value for the claims table.

    The claims table reaches the merge engine as a MANUAL value, which the
    engine escapes wholesale — so it has to be assembled as trusted markup with
    each *interpolated* value escaped here.
    """
    import html

    return html.escape(str(value))


async def build_claims_table(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> str:
    """Assemble the claims table from declarations and rent.

    Shows declared *and* accepted side by side. An employee signing a 12BB
    should see exactly what was allowed and what was not, rather than
    discovering it from a payslip.
    """
    declarations = (
        await session.scalars(
            tenant_scoped(
                select(TaxDeclaration)
                .where(
                    TaxDeclaration.employee_id == employee_id,
                    TaxDeclaration.financial_year == financial_year,
                    TaxDeclaration.status != DeclarationStatus.DRAFT,
                )
                .order_by(TaxDeclaration.section, TaxDeclaration.sub_category),
                TaxDeclaration,
                user,
            )
        )
    ).all()

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

    rows: list[str] = []

    if rent is not None and Decimal(str(rent.annual_rent)) > 0:
        rows.append(
            "<tr><td>1. House Rent Allowance</td>"
            f"<td>Rent paid: {_cell(_money(rent.annual_rent))}<br/>"
            f"Landlord: {_cell(rent.landlord_name or 'not supplied')}<br/>"
            f"PAN: {_cell(rent.landlord_pan or 'not supplied')}<br/>"
            f"Address: {_cell(rent.property_address or 'not supplied')}</td>"
            f"<td style='text-align:right'>{_cell(_money(rent.annual_rent))}</td>"
            "<td style='text-align:right'>—</td></tr>"
        )

    for declaration in declarations:
        label = declaration.section
        if declaration.sub_category:
            label += f" — {declaration.sub_category}"
        rows.append(
            f"<tr><td>{_cell(label)}</td>"
            f"<td>{_cell(declaration.description or '')}</td>"
            f"<td style='text-align:right'>"
            f"{_cell(_money(declaration.declared_amount))}</td>"
            f"<td style='text-align:right'>"
            f"{_cell(_money(declaration.accepted_amount))}</td></tr>"
        )

    if not rows:
        rows.append(
            "<tr><td colspan='4'>No claims declared for this financial year.</td></tr>"
        )

    return (
        '<table class="ctc-table" cellspacing="0" cellpadding="6">'
        "<thead><tr><th>Nature of claim</th><th>Particulars</th>"
        "<th>Amount declared</th><th>Amount accepted</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
