"""Form 12BB.

The statutory declaration an employee gives their employer for HRA, LTA,
home-loan interest and Chapter VI-A deductions. Issued through the letter engine
from slice 03, so it inherits the same guarantees: a required field that
resolves to nothing blocks generation, values are HTML-escaped, the output is
hashed and re-verifiable, and it can be e-signed.

The merge engine escapes **every** merged value, markup included — which is the
right rule, and it means a claims table cannot be passed in as one value (it
would print as literal tags). So the template carries the prescribed form's four
heads as fixed rows, and each head's amount and particulars arrive as separate
plain-text values from `claim_values`.

No amount, limit or threshold appears here — every figure comes from the
employee's own declarations. Which sections belong to the LTA and home-loan heads
is read from the rule set's `form_12bb_heads` where the SME has supplied it.
"""

from __future__ import annotations

import html
import uuid
from decimal import Decimal
from typing import Any

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

# The four heads the prescribed form is organised under.
FORM_HEADS = (
    ("hra", "1. House Rent Allowance"),
    ("lta", "2. Leave Travel Concession"),
    ("home_loan", "3. Deduction of interest on borrowing"),
    ("chapter_via", "4. Deduction under Chapter VI-A"),
)

#: Section codes as the statute names them, used only where the rule set does not
#: say which sections fall under the LTA and home-loan heads.
DEFAULT_HEAD_SECTIONS: dict[str, tuple[str, ...]] = {
    "hra": ("HRA",),
    "lta": ("LTA",),
    "home_loan": ("24B",),
}

NOT_CLAIMED = "Not claimed"

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
  <tr><td><strong>2. Permanent Account Number of the employee</strong></td>
      <td>{{manual.employee_pan}}</td></tr>
  <tr><td><strong>3. Financial year</strong></td>
      <td>{{manual.financial_year}}</td></tr>
</table>

<h3>Details of claims and evidence thereof</h3>
<table class="ctc-table" cellspacing="0" cellpadding="6">
  <thead>
    <tr><th>Nature of claim</th><th>Amount (Rs.)</th><th>Evidence / particulars</th></tr>
  </thead>
  <tbody>
    <tr>
      <td>1. House Rent Allowance: rent paid to the landlord; name, address and
          permanent account number of the landlord</td>
      <td>{{manual.hra_rent_paid}}</td>
      <td>Landlord: {{manual.landlord_name}}<br/>
          Address: {{manual.landlord_address}}<br/>
          PAN: {{manual.landlord_pan}}</td>
    </tr>
    <tr>
      <td>2. Leave travel concessions or assistance</td>
      <td>{{manual.lta_amount}}</td>
      <td>{{manual.lta_particulars}}</td>
    </tr>
    <tr>
      <td>3. Deduction of interest on borrowing: interest payable or paid to the
          lender; name, address and permanent account number of the lender</td>
      <td>{{manual.home_loan_interest}}</td>
      <td>{{manual.home_loan_particulars}}</td>
    </tr>
    <tr>
      <td>4. Deduction under Chapter VI-A</td>
      <td>{{manual.chapter_via_total}}</td>
      <td>{{manual.chapter_via_particulars}}</td>
    </tr>
  </tbody>
</table>

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

_REQUIRED_MANUAL = (
    ("employee_pan", "Employee PAN"),
    ("financial_year", "Financial year"),
    ("hra_rent_paid", "Rent paid"),
    ("landlord_name", "Landlord name"),
    ("landlord_address", "Landlord address"),
    ("landlord_pan", "Landlord PAN"),
    ("lta_amount", "Leave travel amount"),
    ("lta_particulars", "Leave travel particulars"),
    ("home_loan_interest", "Home loan interest"),
    ("home_loan_particulars", "Home loan particulars"),
    ("chapter_via_total", "Chapter VI-A total"),
    ("chapter_via_particulars", "Chapter VI-A particulars"),
    ("parent_name", "Father's / mother's name"),
    ("place", "Place"),
)


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

    fields: list[tuple[str, str, FieldSource, bool]] = [
        ("computed.full_name", "Employee name", FieldSource.COMPUTED, True),
        ("computed.letter_date", "Date", FieldSource.COMPUTED, True),
        ("employee.work_email", "Work email", FieldSource.EMPLOYEE, True),
        # Optional: an employee with no designation on record can still declare.
        ("employee.designation", "Designation", FieldSource.EMPLOYEE, False),
    ]
    fields += [
        (f"manual.{token}", label, FieldSource.MANUAL, True) for token, label in _REQUIRED_MANUAL
    ]
    for token, label, source, required in fields:
        template.fields.append(
            LetterTemplateField(
                tenant_id=tenant_id, token=token, label=label, source=source, required=required
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
    return group_indian(Decimal(str(value)).quantize(Decimal("0.01")))


def _cell(value: object) -> str:
    """Escape a value for the HTML preview table."""
    return html.escape(str(value))


async def _declarations_and_rent(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> tuple[list[TaxDeclaration], HouseRentDeclaration | None]:
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
    return list(declarations), rent


async def build_claims_table(
    session: AsyncSession, user: User, employee_id: uuid.UUID, financial_year: str
) -> str:
    """The claims as an escaped HTML table, for the on-screen preview.

    Shows declared *and* accepted side by side. An employee signing a 12BB
    should see exactly what was allowed and what was not, rather than
    discovering it from a payslip. The portal renders this only inside a
    sandboxed frame.
    """
    declarations, rent = await _declarations_and_rent(session, user, employee_id, financial_year)

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
        rows.append("<tr><td colspan='4'>No claims declared for this financial year.</td></tr>")

    return (
        '<table class="ctc-table" cellspacing="0" cellpadding="6">'
        "<thead><tr><th>Nature of claim</th><th>Particulars</th>"
        "<th>Amount declared</th><th>Amount accepted</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _head_sections(rules: dict[str, Any] | None) -> dict[str, set[str]]:
    configured = (rules or {}).get("form_12bb_heads") or {}
    return {
        head: {str(code).upper() for code in configured.get(head, default)}
        for head, default in DEFAULT_HEAD_SECTIONS.items()
    }


async def claim_values(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    financial_year: str,
    *,
    rules: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Plain-text values for each head of the form, declared and accepted.

    Every value is filled — "Not claimed" rather than blank — because a
    required field that resolves to nothing blocks the letter, and a blank on a
    statutory form reads as an omission rather than a nil claim.
    """
    declarations, rent = await _declarations_and_rent(session, user, employee_id, financial_year)
    heads = _head_sections(rules)

    def _particulars(lines: list[TaxDeclaration]) -> tuple[str, str]:
        if not lines:
            return _money(0), NOT_CLAIMED
        total = sum((Decimal(str(line.declared_amount)) for line in lines), start=Decimal(0))
        parts = []
        for line in lines:
            label = line.section + (f" ({line.sub_category})" if line.sub_category else "")
            parts.append(
                f"{label}: declared {_money(line.declared_amount)}, accepted "
                f"{_money(line.accepted_amount)}"
                + (f" — {line.description}" if line.description else "")
            )
        return _money(total), "; ".join(parts)

    lta = [d for d in declarations if d.section.upper() in heads["lta"]]
    home_loan = [d for d in declarations if d.section.upper() in heads["home_loan"]]
    excluded = heads["lta"] | heads["home_loan"] | heads["hra"]
    chapter_via = [d for d in declarations if d.section.upper() not in excluded]

    lta_amount, lta_text = _particulars(lta)
    loan_amount, loan_text = _particulars(home_loan)
    via_amount, via_text = _particulars(chapter_via)

    has_rent = rent is not None and Decimal(str(rent.annual_rent)) > 0
    return {
        "financial_year": financial_year,
        "hra_rent_paid": _money(rent.annual_rent) if has_rent else _money(0),
        "landlord_name": (rent.landlord_name if has_rent else None) or NOT_CLAIMED,
        "landlord_address": (rent.property_address if has_rent else None) or NOT_CLAIMED,
        "landlord_pan": (rent.landlord_pan if has_rent else None)
        or ("Not supplied" if has_rent else NOT_CLAIMED),
        "lta_amount": lta_amount,
        "lta_particulars": lta_text,
        "home_loan_interest": loan_amount,
        "home_loan_particulars": loan_text,
        "chapter_via_total": via_amount,
        "chapter_via_particulars": via_text,
    }
