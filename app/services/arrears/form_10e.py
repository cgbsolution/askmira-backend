"""Form 10E — the working behind a section 89(1) relief claim.

Issued through the letter engine (slice 03) so it is numbered, hashed and
re-verifiable. The engine escapes every merged value, so the year-by-year
allocation arrives as one plain-text value rather than a table.

The figures are the relief computation's own; nothing is recomputed here. The
form is prepared for the employee, who files it on the income tax portal —
nothing in this build submits it anywhere, and a tax advisor should review it.
"""

from __future__ import annotations

import uuid

from app.models.documents import FieldSource, LetterCategory, LetterTemplate, LetterTemplateField

FORM_10E_CODE = "FORM_10E"

BODY_HTML = """
<h2 style="text-align:center">FORM NO. 10E</h2>
<p style="text-align:center;font-size:9pt">
  [See rule 21AA]<br/>
  Form for furnishing particulars of income under section 192(2A) for the year
  ending 31st March for claiming relief under section 89(1) — working prepared by
  the employer
</p>

<table class="ctc-table" cellspacing="0" cellpadding="6">
  <tr><td><strong>1. Name and address of the employee</strong></td>
      <td>{{computed.full_name}}<br/>{{employee.work_email}}</td></tr>
  <tr><td><strong>2. Permanent Account Number</strong></td>
      <td>{{manual.employee_pan}}</td></tr>
  <tr><td><strong>3. Financial year of receipt</strong></td>
      <td>{{manual.receipt_year}}</td></tr>
  <tr><td><strong>4. Salary received in arrears</strong></td>
      <td>{{manual.arrear_amount}}</td></tr>
</table>

<h3>Annexure I — arrears of salary</h3>
<table class="ctc-table" cellspacing="0" cellpadding="6">
  <tr><td>Tax on total income of the year of receipt, with the arrears</td>
      <td>less tax without them: {{manual.tax_without_relief}}</td></tr>
  <tr><td>Additional tax had the arrears been received in the years they relate to</td>
      <td>{{manual.tax_with_spreading}}</td></tr>
  <tr><td><strong>Relief under section 89(1)</strong></td>
      <td><strong>{{manual.relief_amount}}</strong></td></tr>
</table>

<h3>Year-by-year working</h3>
<p>{{manual.allocations}}</p>
<p>{{manual.outcome}}</p>

<p style="font-size:9pt">
  Prepared by the employer from its payroll records for the employee to verify and
  file. Figures for years before the employee joined are as declared by the
  employee.
</p>
<p>Date: {{computed.letter_date}}</p>
"""

_MANUAL = (
    ("employee_pan", "Employee PAN"),
    ("receipt_year", "Financial year of receipt"),
    ("arrear_amount", "Arrears received"),
    ("tax_without_relief", "Tax on the arrears in the year of receipt"),
    ("tax_with_spreading", "Tax had the arrears been spread"),
    ("relief_amount", "Relief"),
    ("allocations", "Year-by-year working"),
    ("outcome", "Outcome"),
)


def seed_template(tenant_id: uuid.UUID) -> LetterTemplate:
    template = LetterTemplate(
        tenant_id=tenant_id,
        code=FORM_10E_CODE,
        name="Form 10E — section 89(1) relief working",
        category=LetterCategory.CUSTOM,
        body_html=BODY_HTML,
        version=1,
        active=True,
        requires_approval=False,
        requires_signature=False,
        is_seed=False,
    )
    fields = [
        ("computed.full_name", "Employee name", FieldSource.COMPUTED),
        ("computed.letter_date", "Date", FieldSource.COMPUTED),
        ("employee.work_email", "Work email", FieldSource.EMPLOYEE),
    ] + [(f"manual.{token}", label, FieldSource.MANUAL) for token, label in _MANUAL]
    for token, label, source in fields:
        template.fields.append(
            LetterTemplateField(
                tenant_id=tenant_id, token=token, label=label, source=source, required=True
            )
        )
    return template
