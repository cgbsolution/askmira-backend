"""The itemised settlement statement, issued through the letter engine (slice 13 §4).

Every earning, recovery and tax line the settlement can carry has a fixed row,
with its amount and the basis it was computed on. A line the settlement does not
carry reads "Not applicable" rather than being left out, so the employee sees
every head was considered.

The rows are fixed because the merge engine escapes every value it merges — a
table passed in as one value would print as literal tags — and because the
settlement engine's heads are a fixed list too.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.documents import FieldSource, LetterCategory, LetterTemplate, LetterTemplateField
from app.services.documents.indian_numbers import group_indian
from app.services.exit_workflow import settlement as engine

STATEMENT_CODE = "FNF_STATEMENT"
NOT_APPLICABLE = "Not applicable to this settlement."

#: (code, label, line type) in statement order.
HEADS: tuple[tuple[str, str, str], ...] = (
    *[(code, label, "earning") for code, label, _, _ in engine._EARNINGS],
    *[(code, label, "recovery") for code, label, _, _ in engine._RECOVERIES],
    ("TDS_FINAL", "Income tax on the final computation", "tax"),
)


def _token(code: str) -> str:
    return code.lower()


def _rows(line_type: str) -> str:
    return "\n".join(
        f"    <tr><td>{label}</td>"
        f"<td style=\"text-align:right\">{{{{manual.{_token(code)}_amount}}}}</td>"
        f"<td>{{{{manual.{_token(code)}_basis}}}}</td></tr>"
        for code, label, kind in HEADS
        if kind == line_type
    )


BODY_HTML = f"""
<p style="text-align:right">{{{{computed.letter_date}}}}</p>
<h2 style="text-align:center">Full and Final Settlement Statement</h2>

<table class="ctc-table" cellspacing="0" cellpadding="6">
  <tr><td><strong>Employee</strong></td>
      <td>{{{{computed.full_name}}}} ({{{{employee.employee_code}}}})</td></tr>
  <tr><td><strong>Separation</strong></td><td>{{{{manual.separation}}}}</td></tr>
</table>

<h3>Earnings</h3>
<table class="ctc-table" cellspacing="0" cellpadding="6">
  <thead><tr><th>Head</th><th>Amount (Rs.)</th><th>How it was computed</th></tr></thead>
  <tbody>
{_rows("earning")}
  </tbody>
  <tfoot><tr><td><strong>Total earnings</strong></td>
      <td style="text-align:right"><strong>{{{{manual.total_earnings}}}}</strong></td>
      <td></td></tr></tfoot>
</table>

<h3>Recoveries</h3>
<table class="ctc-table" cellspacing="0" cellpadding="6">
  <thead><tr><th>Head</th><th>Amount (Rs.)</th><th>How it was computed</th></tr></thead>
  <tbody>
{_rows("recovery")}
  </tbody>
  <tfoot><tr><td><strong>Total recoveries</strong></td>
      <td style="text-align:right"><strong>{{{{manual.total_recoveries}}}}</strong></td>
      <td></td></tr></tfoot>
</table>

<h3>Tax</h3>
<table class="ctc-table" cellspacing="0" cellpadding="6">
  <tbody>
{_rows("tax")}
  </tbody>
</table>

<h3>Net settlement</h3>
<p><strong>{{{{manual.net_label}}}}: Rs. {{{{manual.net_amount}}}}</strong></p>
<p>{{{{manual.notes}}}}</p>

<p style="font-size:9pt">Each amount above states the basis it was computed on. Please raise
any question about a line with HR before accepting the settlement.</p>
"""


def seed_template(tenant_id: uuid.UUID) -> LetterTemplate:
    template = LetterTemplate(
        tenant_id=tenant_id,
        code=STATEMENT_CODE,
        name="Full and final settlement statement",
        category=LetterCategory.CUSTOM,
        body_html=BODY_HTML,
        version=1,
        active=True,
        requires_approval=False,
        requires_signature=False,
        is_seed=False,
    )
    tokens: list[tuple[str, str, FieldSource]] = [
        ("computed.full_name", "Employee name", FieldSource.COMPUTED),
        ("computed.letter_date", "Date", FieldSource.COMPUTED),
        ("employee.employee_code", "Employee code", FieldSource.EMPLOYEE),
        ("manual.separation", "Separation", FieldSource.MANUAL),
        ("manual.total_earnings", "Total earnings", FieldSource.MANUAL),
        ("manual.total_recoveries", "Total recoveries", FieldSource.MANUAL),
        ("manual.net_label", "Net label", FieldSource.MANUAL),
        ("manual.net_amount", "Net amount", FieldSource.MANUAL),
        ("manual.notes", "Notes", FieldSource.MANUAL),
    ]
    for code, label, _ in HEADS:
        tokens.append((f"manual.{_token(code)}_amount", f"{label} amount", FieldSource.MANUAL))
        tokens.append((f"manual.{_token(code)}_basis", f"{label} basis", FieldSource.MANUAL))
    for token, label, source in tokens:
        template.fields.append(
            LetterTemplateField(
                tenant_id=tenant_id, token=token, label=label, source=source, required=True
            )
        )
    return template


def _money(value: object) -> str:
    return group_indian(Decimal(str(value)).quantize(Decimal("0.01")))


def manual_values(
    *,
    lines: list[tuple[str, Decimal, str]],
    total_earnings: Decimal,
    total_recoveries: Decimal,
    net_amount: Decimal,
    separation: str,
    notes: list[str],
) -> dict[str, str]:
    """Every token the template needs, from the settlement's own stored lines.

    `lines` is `[(code, amount, basis)]`. A refund on the tax line is shown with
    its sign so the reader knows which way it goes.
    """
    values: dict[str, str] = {}
    by_code = {code: (amount, basis) for code, amount, basis in lines}
    for code, _, _ in HEADS:
        amount, basis = by_code.get(code, (None, None))
        values[f"{_token(code)}_amount"] = _money(amount) if amount is not None else "—"
        values[f"{_token(code)}_basis"] = basis or NOT_APPLICABLE
    is_demand = net_amount < 0
    values.update(
        {
            "separation": separation,
            "total_earnings": _money(total_earnings),
            "total_recoveries": _money(total_recoveries),
            "net_label": (
                "Amount due from the employee to the company"
                if is_demand
                else "Amount payable to the employee"
            ),
            "net_amount": _money(abs(net_amount)),
            "notes": " ".join(notes) or "No further notes.",
        }
    )
    return values
