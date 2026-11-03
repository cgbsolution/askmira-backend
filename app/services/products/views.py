"""A linked product's records as pages in the portal: Expenses, Hiring, Helpdesk,
Assets, Sales and Procurement, read live.

When a workspace links ExpenseGenie, AnytimeHire or CGB Helpdesk, its Expenses,
Hiring, Helpdesk or Assets section shows that product's own records — the
person's claims, what waits on their approval, jobs, candidates, interviews,
tickets, the assets they hold — instead of a lone launcher.

Nothing is copied into AskMira. Each page asks the product as the person, with
the same lookups Ask Mira uses (companion.product_tools): the caller's own
workspace only, only what their role there shows them, the same wording rules
(no AI scores, proctoring outcomes as observations), money as decimal strings.
The answer comes back as cards for the screen, as in Ask Mira.

A view fixes what the page is (whose claims, which list); the person may only
narrow it with a status, a search and a date range, and turn its pages.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.companion import product_tools, sales_tools
from app.services.products import data as products_data
from app.services.products.catalogue import PRODUCTS

__all__ = ["VIEWS", "render"]


def _notice(text: str) -> dict[str, Any]:
    return {"type": "notice", "tone": "warning", "text": text}


IST = ZoneInfo("Asia/Kolkata")

#: (product, view) → the lookup it runs and the inputs the page fixes.
VIEWS: dict[tuple[str, str], tuple[str, dict[str, str]]] = {
    ("expense", "mine"): ("expense_claims", {"scope": "mine"}),
    ("expense", "approvals"): ("expense_claims", {"scope": "approvals"}),
    ("expense", "company"): ("expense_claims", {"scope": "company"}),
    ("expense", "totals"): ("expense_summary", {}),
    ("interview", "overview"): ("hiring_overview", {}),
    ("interview", "jobs"): ("hiring_jobs", {}),
    ("interview", "candidates"): ("hiring_candidates", {}),
    ("interview", "interviews"): ("hiring_interviews", {}),
    ("helpdesk", "tickets"): ("helpdesk_tickets", {"scope": "mine"}),
    ("helpdesk", "assigned"): ("helpdesk_tickets", {"scope": "assigned"}),
    ("helpdesk", "queue"): ("helpdesk_tickets", {"scope": "queue"}),
    ("helpdesk", "all"): ("helpdesk_tickets", {"scope": "company"}),
    ("helpdesk", "overview"): ("helpdesk_overview", {}),
    ("helpdesk", "assets"): ("helpdesk_assets", {"scope": "mine"}),
    ("helpdesk", "register"): ("helpdesk_assets", {"scope": "company"}),
    ("saleinx", "deals"): ("sales_deals", {}),
    ("saleinx", "pipeline"): ("sales_pipeline", {}),
    ("saleinx", "approvals"): ("sales_deals", {"deal_stage": "awaiting_approval"}),
    ("procurement", "overview"): ("procurement_overview", {}),
    ("procurement", "approvals"): ("procurement_approvals", {}),
    ("procurement", "requisitions"): ("procurement_documents", {"document": "requisitions"}),
    ("procurement", "orders"): ("procurement_documents", {"document": "purchase_orders"}),
    ("procurement", "invoices"): ("procurement_documents", {"document": "invoices"}),
    ("procurement", "budgets"): ("procurement_budgets", {}),
}

#: What a person may set on a page. Anything else in the query string is ignored.
NARROWING = ("status", "query", "from", "to", "page", "stage", "scope")


async def render(
    session: AsyncSession, user: User, product: str, view: str, params: dict[str, str]
) -> dict[str, Any]:
    """The cards for one page of a linked product, as `user` sees it."""
    if product not in PRODUCTS or (product, view) not in VIEWS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such page.")
    key, fixed = VIEWS[(product, view)]
    if await products_data.role_in(session, user, product) is None:
        # Not linked, not set up, or no role there for this person: the product is never asked.
        brand = PRODUCTS[product].brand
        text = f"{brand} is not linked to your workspace for you."
        return {"product": product, "view": view, "cards": [_notice(text)], "used": {}}
    inputs: dict[str, Any] = {name: str(params.get(name) or "").strip() for name in NARROWING}
    if key == "expense_summary":
        # Totals are the person's own unless HR asks for the whole company.
        inputs["scope"] = "company" if params.get("scope") == "company" else "mine"
    if key == "procurement_documents":
        inputs["doc_status"] = inputs.pop("status", "")
        inputs["scope"] = "company" if params.get("scope") == "company" else "mine"
    if key == "sales_deals" and "deal_stage" not in fixed:
        inputs["deal_stage"] = str(params.get("stage") or "none")
    inputs.update(fixed)
    runner = sales_tools.run if key in sales_tools.READ_TOOLS else product_tools.run
    try:
        cards, used = await runner(session, user, key, inputs, today=datetime.now(IST).date())
    except ValueError as exc:  # a malformed or too-long date range
        cards, used = [_notice(str(exc))], {}
    return {"product": product, "view": view, "cards": cards, "used": used}
