"""Ask Mira's department budget answers: what a headcount budget still allows.

"Can Sales afford two more people at 9 lakh?" is the question the positions
module already answers when someone creates a seat: it refuses a seat past the
department's approved positions or cost (positions.service.enforce_budget).
This asks the same engine before anyone asks for the seat, and says what is left.

Cost is the budgeted ceiling on each seat — what the budget was approved
against — never a person's salary, so the model may read it.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.documents.indian_numbers import group_indian
from app.services.positions import service as positions

__all__ = ["department_budget", "rupees_in"]


def rupees_in(text: Any) -> Decimal | None:
    """A rupee amount as typed ("9 lakh", "₹12,00,000", "1.2 cr", "900000"), or None."""
    raw = str(text or "").strip().lower().replace(",", "").replace("₹", "")
    raw = re.sub(r"\s*(per annum|a year|per year|each|p\.?a\.?)$", "", raw)
    raw = re.sub(r"^(rs\.?|inr)\s*", "", raw).replace(" ", "")
    if not raw:
        return None
    scale = Decimal(1)
    for words, factor in (
        (("crores", "crore", "cr"), Decimal(10_000_000)),
        (("lakhs", "lakh", "lacs", "lac", "lpa", "l"), Decimal(100_000)),
        (("thousand", "k"), Decimal(1_000)),
    ):
        for word in words:
            if raw.endswith(word):
                raw, scale = raw[: -len(word)].strip(), factor
                break
        if scale != 1:
            break
    try:
        value = Decimal(raw) * scale
    except InvalidOperation:
        return None
    if value <= 0 or value > Decimal("1e11"):
        return None
    return value.quantize(Decimal("0.01"))


def _inr(value: str | None) -> str:
    if value is None:
        return "No cost budget"
    amount = Decimal(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{group_indian(abs(amount).quantize(Decimal('1')))}"


async def department_budget(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    department = str(inputs.get("query") or "").strip()
    try:
        hires = max(0, min(int(inputs.get("hires") or 0), 500))
    except (TypeError, ValueError):
        hires = 0
    ctc = rupees_in(inputs.get("amount"))
    answer = await positions.budget_room(
        session, user, on=today, department=department or None, hires=hires, ctc_per_hire=ctc
    )
    used = {
        k: v
        for k, v in {"query": department, "hires": hires, "amount": str(ctc) if ctc else ""}.items()
        if v
    }
    budgets = answer["budgets"]
    if not budgets:
        return [
            {
                "type": "notice",
                "tone": "info",
                "text": (
                    f"No headcount budget is set for {answer['financial_year']} yet. An admin "
                    "sets one under Employees → Positions → Budget."
                ),
            }
        ], used

    # The same year's spend from AskMira and the linked products, beside each budget.
    from app.services import budget_year as spend

    try:
        year = await spend.department_year(session, user, on=today)
        spent = {row["department_id"]: row for row in year["departments"]}
        expense_source = year["expenses"]["source"]
    except Exception:  # a product that cannot answer never hides the budget itself
        spent, expense_source = {}, "AskMira"

    cards: list[dict[str, Any]] = []
    if department and all(b["department_id"] is None for b in budgets):
        cards.append(
            {
                "type": "notice",
                "tone": "info",
                "text": (
                    f"There is no headcount budget for “{department}” in "
                    f"{answer['financial_year']}, so this is the company's."
                ),
            }
        )
    for budget in budgets[:12]:
        figures = [
            {"label": "Approved positions", "value": str(budget["approved_positions"])},
            {"label": "Positions counted", "value": budget["positions_counted"]},
            {"label": "Filled", "value": budget["positions_filled"]},
            {"label": "Positions left", "value": budget["positions_left"]},
            {"label": "Approved cost", "value": _inr(budget["approved_cost"])},
        ]
        if budget["approved_cost"] is not None:
            figures += [
                {"label": "Committed on positions", "value": _inr(budget["cost_committed"])},
                {"label": "Cost left", "value": _inr(budget["cost_left"])},
            ]
        row = spent.get(budget["department_id"]) if budget["department_id"] else None
        spend_figures: list[dict[str, str]] = []
        if row is not None:
            if row.get("payroll_paid") is not None:
                spend_figures.append(
                    {"label": "Payroll paid this year", "value": _inr(row["payroll_paid"])}
                )
            if row.get("expenses") is not None:
                spend_figures.append(
                    {
                        "label": f"Expenses this year ({expense_source})",
                        "value": _inr(row["expenses"]),
                    }
                )
            purchase = row.get("purchase_budget")
            if purchase:
                spend_figures.append(
                    {
                        "label": "Purchasing available (ProcureFlow)",
                        "value": f"{_inr(purchase['available'])} of {_inr(purchase['allocated'])}",
                    }
                )
        note = None
        if budget["fits"] is True:
            per = f" at {_inr(answer['ctc_per_hire'])} a year each" if ctc else ""
            fits = "hire fits" if hires == 1 else "hires fit"
            note = f"{hires} more {fits}{per} inside this budget."
        elif budget["fits"] is False:
            note = " ".join(budget["problems"])
        elif budget["unpriced_seats"]:
            note = (
                f"{len(budget['unpriced_seats'])} position(s) have no budgeted CTC ceiling, so "
                "they are not in the committed cost."
            )
        cards.append(
            {
                "type": "record",
                "title": f"{budget['scope']} · headcount budget",
                "subtitle": f"{answer['financial_year']}, as of {answer['as_of']}",
                "figures": figures,
                "note": note,
                "open": "/people/positions",
            }
        )
        if spend_figures:
            # A small department's payroll or claims are a person's: on screen only.
            cards.append(
                {
                    "type": "record",
                    "title": f"{budget['scope']} · spent this year",
                    "subtitle": "From payroll and the linked products",
                    "figures": spend_figures,
                    "open": "/people/positions",
                    "model_view": "",
                }
            )
    if hires and ctc is None and any(b["approved_cost"] for b in budgets):
        cards.append(
            {
                "type": "notice",
                "tone": "info",
                "text": (
                    "Tell me the yearly CTC per hire to check them against the cost budget too."
                ),
            }
        )
    return cards, used
