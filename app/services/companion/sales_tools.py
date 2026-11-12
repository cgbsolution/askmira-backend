"""Ask Miraa's tools for Saleinx (sales CRM) and ProcureFlow (procure-to-pay), and the forecast P&L.

Each asks the product as the person (products.data.ask / act): only the
caller's own workspace, and only what their role *in that product* allows —
a Saleinx rep sees their own and unassigned deals, a viewer only reads, a
ProcureFlow requester sees their own requisitions. The product checks again on
every call; these tools only avoid offering what it would refuse.

Every change is an offer first (actions.py): the tool checks what Saleinx or
ProcureFlow will check — a margin from Qualified onwards, a close date from
Proposal, Hold's reason and revisit date, why a deal goes back, that an
approval still waits on the person — and the person confirms. Then the
product's own code makes the change, as them; its words come back if it
refuses.

What the model reads of a deal is business data (company, stage, value,
margin, dates). Contact people and their emails and phones stay on the screen
(views.py drops those columns), and owners' names are not in the model's view.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.companion import CompanionAction
from app.models.user import User
from app.services.companion.order_win import inr, rupees
from app.services.products import data as products

__all__ = [
    "DEAL_STAGES",
    "HOLD_REASONS",
    "PROCUREMENT_DOCUMENTS",
    "ROLLBACK_REASONS",
    "carry_out",
    "propose_activity",
    "propose_deal_decision",
    "propose_lead",
    "propose_procurement_decision",
    "propose_stage",
    "run",
]

SALEINX = "saleinx"
PROCUREMENT = "procurement"

DEAL_STAGES = ("lead", "qualified", "proposal", "negotiation", "hold", "won", "lost")
STAGE_LABEL = {
    "lead": "Lead",
    "qualified": "Qualified",
    "proposal": "Proposal",
    "negotiation": "Negotiation",
    "hold": "Hold",
    "won": "Won",
    "lost": "Lost",
}
#: Saleinx's own rules (lib/deals/validation.ts, stage-governance.ts, hold.ts).
MARGIN_OPTIONAL = ("lead", "hold", "lost")
CLOSE_DATE_REQUIRED = ("proposal", "negotiation", "won")
FUNNEL = ("lead", "qualified", "proposal", "negotiation", "won")
HOLD_REASONS = (
    "budget_deferred",
    "champion_left",
    "timing",
    "competitor_evaluation",
    "client_unresponsive",
    "other",
)
HOLD_LABEL = {
    "budget_deferred": "Budget deferred",
    "champion_left": "Champion left",
    "timing": "Timing",
    "competitor_evaluation": "Evaluating competitor",
    "client_unresponsive": "Client unresponsive",
    "other": "Other",
}
ROLLBACK_REASONS = (
    "requirement_changed",
    "budget_lost",
    "champion_left",
    "wrong_stage_advance",
    "competitor_reintroduced",
    "other",
)
ROLLBACK_LABEL = {
    "requirement_changed": "Requirement changed",
    "budget_lost": "Budget lost",
    "champion_left": "Champion left",
    "wrong_stage_advance": "Wrong stage advance",
    "competitor_reintroduced": "Competitor reintroduced",
    "other": "Other",
}
APPROVER_ROLES = ("owner", "admin", "manager")
PROCUREMENT_DOCUMENTS = ("requisitions", "purchase_orders", "invoices")

Result = tuple[list[dict[str, Any]], dict[str, Any]]


def _notice(text: str, tone: str = "info", *, note: bool = False) -> dict[str, Any]:
    card: dict[str, Any] = {"type": "notice", "tone": tone, "text": text}
    if note:
        # Said to the model even where it may not read the result itself.
        card["model_note"] = text
    return card


def _text(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _when(value: Any) -> str:
    day = _day(value)
    return f"{day:%a %d %b %Y}" if day else "—"


def _money(amount: Any, currency: Any = "INR") -> str:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return "—"
    return inr(value) if str(currency or "INR").upper() == "INR" else f"{currency} {value:,.2f}"


def _pct(value: Any) -> str:
    if value in (None, ""):
        return "Not set"
    text = f"{Decimal(str(value)).normalize():f}"
    return f"{text}%"


def _table(
    product: str,
    title: str,
    columns: list[str],
    rows: list[list[Any]],
    *,
    caption: str,
    empty: str,
    view: str | None = None,
) -> dict[str, Any]:
    if not rows:
        return _notice(empty)
    card: dict[str, Any] = {
        "type": "table",
        "title": title,
        "caption": caption,
        "columns": columns,
        "rows": rows,
        "source": "Saleinx" if product == SALEINX else "ProcureFlow",
        "open": f"/launch/{product}",
    }
    if view is not None:
        card["model_view"] = view
    return card


# ── Saleinx: reading ────────────────────────────────────────────────────────


def _deal_view(deals: list[dict[str, Any]], title: str) -> str:
    """What the model reads of deals: business figures only, no people."""
    lines = [f"Table: {title}", "deal | stage | value | margin | close date | approval | next step"]
    for d in deals[:40]:
        lines.append(
            " | ".join(
                [
                    _text(d.get("title"), 160),
                    str(d.get("stage_label") or d.get("stage")),
                    _money(d.get("amount"), d.get("currency")),
                    _pct(d.get("margin_pct")),
                    str(d.get("expected_close_date") or "none"),
                    str(d.get("approval_status") or "none"),
                    _text(d.get("next_step"), 120) or "none",
                ]
            )
        )
    return "\n".join(lines)


def _deal_rows(deals: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            d.get("title"),
            d.get("stage_label"),
            _money(d.get("amount"), d.get("currency")),
            _pct(d.get("margin_pct")),
            d.get("expected_close_date") or "—",
            {"pending": "Waiting for a manager", "approved": "Approved"}.get(
                str(d.get("approval_status")), "—"
            ),
            d.get("owner") or "Unassigned",
            d.get("contact") or "—",
        ]
        for d in deals
    ]


_DEAL_COLUMNS = ["Deal", "Stage", "Value", "Margin", "Close date", "Approval", "Owner", "Contact"]


async def _deals(session: AsyncSession, user: User, inputs: dict[str, Any], today: date) -> Result:
    stage = str(inputs.get("deal_stage") or "none")
    params: dict[str, Any] = {"query": _text(inputs.get("query"), 120), "limit": 25}
    used: dict[str, Any] = {k: v for k, v in params.items() if v and k != "limit"}
    if stage == "awaiting_approval":
        params["awaiting_approval"] = True
        used["deal_stage"] = stage
    elif stage in DEAL_STAGES:
        params["stage"] = stage
        used["deal_stage"] = stage
    data = await products.ask(session, user, SALEINX, "deals", params)
    deals = [d for d in data.get("deals") or [] if isinstance(d, dict)]
    actor = data.get("actor") or {}
    scope = (
        "your deals and unassigned ones"
        if actor.get("sees") == "own_and_unassigned"
        else "the whole workspace"
    )
    total = int(data.get("total") or len(deals))
    caption = f"{len(deals)} of {total} · {scope} · you are {actor.get('role', '—')} in Saleinx"
    card = _table(
        SALEINX,
        "Deals in Saleinx",
        _DEAL_COLUMNS,
        _deal_rows(deals),
        caption=caption,
        empty=f"No deal you can see in Saleinx matches that ({scope}).",
        view=_deal_view(deals, f"Deals in Saleinx ({caption})") if deals else None,
    )
    return [card], used


async def _pipeline(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    data = await products.ask(session, user, SALEINX, "overview", {})
    rows = [
        [s.get("stage_label"), s.get("deals"), _money(s.get("amount"), s.get("currency"))]
        for s in data.get("stages") or []
        if isinstance(s, dict)
    ]
    order = {label: i for i, label in enumerate(STAGE_LABEL.values())}
    rows.sort(key=lambda r: order.get(str(r[0]), 99))
    actor = data.get("actor") or {}
    scope = (
        "your deals and unassigned ones"
        if actor.get("sees") == "own_and_unassigned"
        else "the whole workspace"
    )
    return [
        _table(
            SALEINX,
            "Pipeline by stage",
            ["Stage", "Deals", "Value"],
            rows,
            caption=f"Saleinx · {scope}",
            empty="No deals in Saleinx yet.",
        )
    ], {}


# ── ProcureFlow: reading ────────────────────────────────────────────────────

_DOC_LABEL = {
    "REQUISITION": "Requisition",
    "PURCHASE_ORDER": "Purchase order",
    "GRN": "Goods receipt",
    "INVOICE": "Invoice",
    "PAYMENT_RUN": "Payment run",
    "CONTRACT": "Contract",
}


def _status(value: Any) -> str:
    return str(value or "").replace("_", " ").capitalize() or "—"


async def _procurement_overview(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    data = await products.ask(session, user, PROCUREMENT, "overview", {})
    rows: list[list[Any]] = [["Waiting on your approval", data.get("waiting_on_me") or 0]]
    label = "Your requisitions" if data.get("requisitions_scope") == "mine" else "Requisitions"
    for status, count in sorted((data.get("requisitions") or {}).items()):
        rows.append([f"{label}: {_status(status)}", count])
    for status, count in sorted((data.get("purchase_orders") or {}).items()):
        rows.append([f"Purchase orders: {_status(status)}", count])
    for status, count in sorted((data.get("invoices") or {}).items()):
        rows.append([f"Invoices: {_status(status)}", count])
    return [
        _table(
            PROCUREMENT,
            "Procurement at a glance",
            ["Measure", "Count"],
            rows,
            caption="ProcureFlow · what your role there shows you",
            empty="Nothing in ProcureFlow yet.",
        )
    ], {}


async def _procurement_approvals(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    data = await products.ask(session, user, PROCUREMENT, "approvals", {})
    items = [a for a in data.get("approvals") or [] if isinstance(a, dict)]
    rows = [
        [
            _DOC_LABEL.get(str(a.get("document")), _status(a.get("document"))),
            a.get("number"),
            a.get("title"),
            a.get("submitted_by") or "—",
            _money(a.get("amount"), a.get("currency")) if a.get("amount") else "—",
            a.get("days_pending"),
        ]
        for a in items
    ]
    card = _table(
        PROCUREMENT,
        "Waiting on your approval in ProcureFlow",
        ["Document", "Number", "Title", "Raised by", "Amount", "Days waiting"],
        rows,
        caption=f"{len(items)} waiting on you",
        empty="Nothing in ProcureFlow is waiting on your approval.",
    )
    if items:
        # The model reads documents and refs, never who raised them.
        card["model_view"] = "\n".join(
            [
                f"Table: waiting on the person's approval in ProcureFlow ({len(items)})",
                "document | number | title | amount | days waiting | ref",
                *(
                    " | ".join(
                        [
                            _DOC_LABEL.get(str(a.get("document")), "Document"),
                            str(a.get("number")),
                            _text(a.get("title"), 120),
                            _money(a.get("amount"), a.get("currency")) if a.get("amount") else "-",
                            str(a.get("days_pending")),
                            f"pf:{a.get('ref')}",
                        ]
                    )
                    for a in items[:40]
                ),
            ]
        )
    return [card], {}


async def _procurement_documents(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    kind = str(inputs.get("document") or "requisitions")
    if kind not in PROCUREMENT_DOCUMENTS:
        kind = "requisitions"
    params: dict[str, Any] = {"query": _text(inputs.get("query"), 100), "limit": 25}
    status = _text(inputs.get("doc_status"), 40)
    if status and status != "none":
        params["status"] = status
    if kind == "requisitions" and str(inputs.get("scope") or "") == "company":
        params["scope"] = "company"
    data = await products.ask(session, user, PROCUREMENT, kind, params)
    used = {"document": kind, **{k: v for k, v in params.items() if v and k != "limit"}}
    total = int(data.get("total") or 0)
    view: str | None = None
    if kind == "requisitions":
        items = data.get("requisitions") or []
        rows = [
            [
                r.get("number"),
                r.get("title") or "—",
                _status(r.get("status")),
                r.get("requester"),
                r.get("cost_center"),
                _money(r.get("amount"), r.get("currency")),
                r.get("needed_by") or "—",
            ]
            for r in items
        ]
        columns = ["Number", "Title", "Status", "Requester", "Cost centre", "Amount", "Needed by"]
        title = "Your requisitions" if data.get("scope") == "mine" else "Requisitions"
        # Requesters' names stay on the screen.
        view = "\n".join(
            [
                f"Table: {title} in ProcureFlow",
                "number | title | status | cost centre | amount | needed by",
                *(
                    " | ".join(str(c) for c in (r[0], r[1], r[2], r[4], r[5], r[6]))
                    for r in rows[:40]
                ),
            ]
        )
    elif kind == "purchase_orders":
        items = data.get("purchase_orders") or []
        rows = [
            [p.get("number"), p.get("vendor"), _status(p.get("status")),
             _money(p.get("amount"), p.get("currency")), p.get("expected_on") or "—"]
            for p in items
        ]  # fmt: skip
        columns = ["Number", "Vendor", "Status", "Amount", "Expected"]
        title = "Purchase orders"
    else:
        items = data.get("invoices") or []
        rows = [
            [i.get("number"), i.get("vendor_ref"), i.get("vendor"), _status(i.get("status")),
             _money(i.get("amount"), i.get("currency")), i.get("invoice_date") or "—"]
            for i in items
        ]  # fmt: skip
        columns = ["Number", "Vendor's no.", "Vendor", "Status", "Amount", "Dated"]
        title = "Invoices"
    return [
        _table(
            PROCUREMENT,
            f"{title} in ProcureFlow",
            columns,
            rows,
            caption=f"{len(rows)} of {max(total, len(rows))}",
            empty=f"No {title.lower()} in ProcureFlow match that.",
            view=view if rows else None,
        )
    ], used


async def _procurement_budgets(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    data = await products.ask(session, user, PROCUREMENT, "budgets", {})
    rows = [
        [
            b.get("cost_center"),
            b.get("period"),
            _money(b.get("allocated"), b.get("currency")),
            _money(b.get("committed"), b.get("currency")),
            _money(b.get("consumed"), b.get("currency")),
            _money(b.get("available"), b.get("currency")),
            f"{b.get('used_pct')}%" if b.get("used_pct") else "—",
        ]
        for b in data.get("budgets") or []
        if isinstance(b, dict)
    ]
    return [
        _table(
            PROCUREMENT,
            "Budgets in ProcureFlow",
            ["Cost centre", "Period", "Budget", "Committed", "Spent", "Available", "Used"],
            rows,
            caption="This period's active budgets",
            empty="No budget in ProcureFlow covers today.",
        )
    ], {}


# ── The prospective P&L (AskMiraa's own ledger) ──────────────────────────────


async def _account_forecast(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    from app.services.accounts import service as accounts
    from app.services.finance import list_forecasts

    permissions.require_hr(
        user,
        detail="Client accounts and their P&L are for admins and sub-admins with payroll rights.",
    )
    query = _text(inputs.get("query"), 120)
    account_ids = None
    if query:
        found = await accounts.find_accounts(session, user, query)
        if not found:
            return [_notice(f"No client account matches “{query}”.")], {"query": query}
        account_ids = {a.id for a in found}
    forecasts = [
        f
        for f in await list_forecasts(session, user)
        if account_ids is None or f["account_id"] in {str(i) for i in account_ids}
    ]
    if not forecasts:
        return [_notice("There is no prospective P&L on that account yet.")], {"query": query}
    cards: list[dict[str, Any]] = []
    for f in forecasts[:5]:
        rows = [
            [m["month"], _money(m["billing"]), _money(m["people_cost"]), _money(m["recruitment"]),
             _money(m["margin"])]
            for m in f["by_month"]
        ]  # fmt: skip
        cards.append(
            {
                "type": "table",
                "title": f"Prospective P&L: {f['account_name']} ({f['reference']})",
                "caption": (
                    f"{f['months']} months from {f['start_month']} · billing {_money(f['billing'])}"
                    f" · cost {_money(f['cost'])} · margin {_money(f['margin'])}"
                    f" ({f['margin_pct']}%)"
                ),
                "columns": ["Month", "Billing", "People cost", "Recruitment", "Margin"],
                "rows": rows,
                "open": "/payroll/accounts",
            }
        )
    return cards, {"query": query} if query else {}


# ── Saleinx: changes (offers) ───────────────────────────────────────────────


async def _role(session: AsyncSession, user: User) -> str | None:
    return await products.role_in(session, user, SALEINX)


def _viewer_refusal(role: str) -> dict[str, Any]:
    return _notice(
        f"You have view-only access in Saleinx ({role}), so you can't change deals there. Ask "
        "the deal's owner or a sales manager to do it.",
        "warning",
        note=True,
    )


#: Words that name no deal on their own: the word-by-word search skips them.
_FILLER = frozenset(
    {"deal", "deals", "lead", "leads", "under", "with", "from", "this", "that", "opportunity"}
    | {"company", "private", "limited", "pvt", "ltd", "the", "for", "and"}
)


async def _search_deals(session: AsyncSession, user: User, text: str) -> list[dict[str, Any]]:
    data = await products.ask(session, user, SALEINX, "deals", {"query": text, "limit": 10})
    return [d for d in data.get("deals") or [] if isinstance(d, dict)]


def _hits(deal: dict[str, Any], words: list[str]) -> int:
    """How many of the words name this deal ("safeops360" names "Megafine - Safeops")."""
    title = " ".join(str(deal.get(k) or "") for k in ("title", "company", "name")).lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", title) if len(t) >= 4]
    return sum(
        1 for w in words if w in title or any(w.startswith(t) or t.startswith(w) for t in tokens)
    )


async def _find_deal(
    session: AsyncSession, user: User, wanted: str, *, stages: tuple[str, ...] = ()
) -> dict[str, Any] | list[dict[str, Any]]:
    """The one deal the person means, or the deals it could be (empty: none)."""
    deals = await _search_deals(session, user, wanted)
    if not deals:
        # Saleinx matches the text whole, so "SafeOps360 deal for Megafine" finds nothing:
        # look word by word and keep the deals most of the words point to.
        words = [
            w
            for w in dict.fromkeys(re.findall(r"[a-z0-9]+", wanted.lower()))
            if len(w) >= 4 and w not in _FILLER
        ][:4]
        if len(words) > 1:
            seen: dict[str, dict[str, Any]] = {}
            for word in words:
                for d in await _search_deals(session, user, word):
                    seen.setdefault(str(d.get("id")), d)
            best = max((_hits(d, words) for d in seen.values()), default=0)
            deals = [d for d in seen.values() if _hits(d, words) == best] if best else []
    if stages:
        deals = [d for d in deals if d.get("stage") in stages] or deals
    want = " ".join(wanted.lower().translate({0x2013: "-", 0x2014: "-"}).split())
    exact = [
        d
        for d in deals
        if want
        in (
            str(d.get("title") or "").lower(),
            str(d.get("name") or "").lower(),
        )
    ]
    if len(exact) == 1:
        return exact[0]
    if len(deals) == 1:
        return deals[0]
    return exact or deals


def _which(deals: list[dict[str, Any]], wanted: str) -> list[dict[str, Any]]:
    text = f"More than one deal matches “{wanted}”. Which one did you mean?"
    card = _table(
        SALEINX,
        f"Deals matching “{wanted}”",
        _DEAL_COLUMNS,
        _deal_rows(deals),
        caption="Say which one",
        empty="",
        view=_deal_view(deals, f"Deals matching “{wanted}”") + "\n" + text,
    )
    return [card, _notice(text)]


async def propose_stage(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> Result:
    """Check a stage move the way Saleinx will, and offer it."""
    from app.services.companion import actions

    role = await _role(session, user)
    if role is None:
        return [_notice("Saleinx is not linked to your workspace for you.", "warning")], {}
    if role == "viewer":
        return [_viewer_refusal(role)], {}
    wanted = _text(inputs.get("deal"), 160)
    stage = str(inputs.get("deal_stage") or "none")
    used: dict[str, Any] = {"deal": wanted, "deal_stage": stage}
    if stage not in DEAL_STAGES:
        return [_notice("Which stage should it move to?")], used
    if not wanted:
        return [_notice("Which deal?")], used
    found = await _find_deal(session, user, wanted)
    if isinstance(found, list):
        if not found:
            return [
                _notice(f"No deal you can see in Saleinx matches “{wanted}”.", "warning", note=True)
            ], used
        return _which(found, wanted), used
    deal = found
    current = str(deal.get("stage"))
    label = STAGE_LABEL[stage]
    if current == stage:
        return [_notice(f"{deal.get('title')} is already at {label}.", note=True)], used

    margin = _text(inputs.get("margin_pct"), 10).rstrip("%").strip()
    close = _day(inputs.get("close_date"))
    if inputs.get("close_date") and close is None:
        return [_notice("Give the expected close date as YYYY-MM-DD.")], used
    if margin:
        try:
            value = Decimal(margin)
        except InvalidOperation:
            return [_notice("Give the margin as a percentage, like 32.")], used
        if not Decimal(0) < value <= 100:
            return [_notice("A margin is above 0 and at most 100 per cent.")], used
    asks: list[str] = []
    if stage not in MARGIN_OPTIONAL and not deal.get("margin_pct") and not margin:
        asks.append(
            "Saleinx needs the deal's margin from Qualified onwards: what is it, in per cent?"
        )
    if stage in CLOSE_DATE_REQUIRED and not deal.get("expected_close_date") and close is None:
        asks.append(f"{label} needs an expected close date: when will it close?")
    hold_reason = str(inputs.get("hold_reason") or "none")
    revisit = _day(inputs.get("revisit_on"))
    if stage == "hold":
        if hold_reason not in HOLD_REASONS:
            asks.append("Why is it on hold? One of: " + ", ".join(HOLD_LABEL.values()) + ".")
        if revisit is None:
            asks.append("When should it be looked at again (the revisit date)?")
    lost_reason = _text(inputs.get("reason"), 1000)
    if stage == "lost" and not lost_reason:
        asks.append("Why was it lost?")
    rollback = str(inputs.get("rollback_reason") or "none")
    backward = current in FUNNEL and stage in FUNNEL and FUNNEL.index(stage) < FUNNEL.index(current)
    if backward and rollback not in ROLLBACK_REASONS:
        asks.append(
            f"Moving it back from {deal.get('stage_label')} to {label} needs a reason: "
            + ", ".join(ROLLBACK_LABEL.values())
            + "."
        )
    if asks:
        text = " ".join(asks)
        return [_notice(text, note=True)], used

    params: dict[str, Any] = {"deal_id": deal["id"], "stage": stage}
    figures = [
        {"label": "Deal", "value": str(deal.get("title"))},
        {"label": "Move", "value": f"{deal.get('stage_label')} → {label}"},
    ]
    notes: list[str] = []
    if margin:
        params["margin_pct"] = margin
        figures.append({"label": "Margin", "value": f"{margin}%"})
    elif deal.get("margin_pct"):
        figures.append({"label": "Margin", "value": _pct(deal.get("margin_pct"))})
    if close:
        params["expected_close_date"] = close.isoformat()
        figures.append({"label": "Expected close", "value": _when(close.isoformat())})
    next_step = _text(inputs.get("next_step"), 500)
    if next_step:
        params["next_step"] = next_step
        figures.append({"label": "Next step", "value": next_step})
    if stage == "hold":
        params.update(hold_reason=hold_reason, hold_revisit_on=revisit.isoformat())  # type: ignore[union-attr]
        figures.append({"label": "Why on hold", "value": HOLD_LABEL[hold_reason]})
        figures.append({"label": "Revisit on", "value": _when(revisit.isoformat())})  # type: ignore[union-attr]
        note = _text(inputs.get("reason"), 1000)
        if note:
            params["hold_note"] = note
            notes.append(f"Note: {note}")
    if stage == "lost":
        params["lost_reason"] = lost_reason
        figures.append({"label": "Why lost", "value": lost_reason})
    if backward:
        params["rollback_reason"] = rollback
        figures.append({"label": "Why it goes back", "value": ROLLBACK_LABEL[rollback]})
        note = _text(inputs.get("reason"), 1000)
        if note and stage != "lost":
            params["rollback_note"] = note
    if stage == "lead" and deal.get("expected_close_date"):
        params["clear_close_date"] = True
        notes.append("A lead carries no close date in Saleinx: moving it to Lead clears it.")
    if stage == "won" and role not in APPROVER_ROLES:
        notes.append(
            "As a rep, your Won waits for a manager's approval in Saleinx; it counts toward "
            "targets, and an order can be booked, once a manager approves it."
        )
    elif stage == "won":
        notes.append("You can approve deals in Saleinx, so this Won is approved as you make it.")
    action = await actions._offer(
        session,
        user,
        ctx,
        kind="saleinx_stage",
        payload=params,
        summary={
            "title": f"Move {deal.get('title')} to {label}",
            "figures": figures,
            "notes": notes,
            "confirm_label": f"Move to {label}",
            "question": f"Move it to {label}?",
        },
    )
    return [actions.card_for(action)], used


def _split_name(full: str) -> tuple[str, str]:
    parts = full.split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")


#: Between a company's name and the rest: "Kestrel Foods - SafeOps360", "SafeOps360 for Kestrel".
_JOIN = r"(?:[-:|,(\u2013\u2014]|\bfor\b|\bat\b|\bwith\b)"
#: Words that name no deal on their own.
_NO_NAME = frozenset({"opportunity", "deal", "lead", "new lead", "new deal", "new opportunity"})


def _deal_name(name: str, company: str, contact: str | None, sells: list[str]) -> str:
    """What a new deal is for, in a few words. Saleinx shows it after the company's name
    ("Kestrel Foods - SafeOps360"), so the company, or a name that is only the contact's,
    is left out of it; with nothing left, what it sells names it."""
    if company:
        known = re.escape(company.strip())
        name = re.sub(rf"^\s*{known}(?!\w)\s*{_JOIN}?\s*", "", name, flags=re.IGNORECASE)
        name = re.sub(rf"\s*{_JOIN}?\s*(?<!\w){known}\s*\)?\s*$", "", name, flags=re.IGNORECASE)
    name = name.strip(" -:|,\u2013\u2014")
    if name.lower() in _NO_NAME or (contact and name.lower() == contact.strip().lower()):
        name = ""
    return name or ", ".join(sells) or "New opportunity"


async def propose_lead(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> Result:
    """A new lead from what the person typed, or changes to one — checked, then offered."""
    from app.services.companion import actions

    role = await _role(session, user)
    if role is None:
        return [_notice("Saleinx is not linked to your workspace for you.", "warning")], {}
    if role == "viewer":
        return [_viewer_refusal(role)], {}
    company = _text(inputs.get("company"), 200)
    existing = _text(inputs.get("deal"), 160)
    contact = {
        "name": _text(inputs.get("contact_name"), 200) or None,
        "title": _text(inputs.get("contact_title"), 100) or None,
        "email": _text(inputs.get("contact_email"), 255).lower() or None,
        "phone": _text(inputs.get("contact_phone"), 50) or None,
    }
    if contact["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact["email"]):
        return [_notice(f"“{contact['email']}” is not an email address.")], {}
    follow_day = _day(inputs.get("follow_up_date"))
    if inputs.get("follow_up_date") and follow_day is None:
        return [_notice("Give the follow-up date as YYYY-MM-DD.")], {}
    try:
        amount = rupees(inputs.get("amount")) if _text(inputs.get("amount")) else None
    except ValueError as exc:
        return [_notice(str(exc))], {}
    used: dict[str, Any] = {"company": company, "deal": existing}

    deal: dict[str, Any] | None = None
    if existing:
        found = await _find_deal(session, user, existing)
        if isinstance(found, list):
            if not found:
                return [
                    _notice(
                        f"No lead or deal you can see in Saleinx matches “{existing}”.",
                        "warning",
                        note=True,
                    )
                ], used
            return _which(found, existing), used
        deal = found
    elif not company:
        return [_notice("Which company is the lead for?")], used
    elif not (contact["name"] or contact["email"]):
        return [_notice("Who is the contact at the company?")], used

    options = await products.ask(session, user, SALEINX, "options", {})
    offerings_on = bool(options.get("offerings_required"))
    wanted = [str(w).strip().lower() for w in inputs.get("offerings") or [] if str(w).strip()]
    catalog = [o for o in options.get("offerings") or [] if isinstance(o, dict)]
    picked = [o["name"] for o in catalog if str(o.get("name", "")).lower() in wanted]
    if not picked:
        # A word of what they sell, in the deal's own words ("Power Platform and OutSystems").
        about = " ".join(
            [_text(inputs.get("deal_name")), _text(inputs.get("description"), 2000)]
        ).lower()
        picked = [o["name"] for o in catalog if str(o.get("name", "")).lower() in about]
    if deal is None and offerings_on and not picked:
        names = ", ".join(str(o.get("name")) for o in catalog) or "none set up"
        return [
            _notice(
                f"Saleinx needs to know what a new deal sells. Which of these: {names}?", note=True
            )
        ], used

    figures: list[dict[str, str]] = []
    notes: list[str] = []
    params: dict[str, Any] = {}
    if deal is not None:
        params["deal_id"] = deal["id"]
        figures.append({"label": "Lead", "value": str(deal.get("title"))})
    else:
        params["company"] = company
        figures.append({"label": "Company", "value": company})
    if any(contact.values()):
        first, last = _split_name(contact["name"] or "")
        params["contact"] = {
            k: v
            for k, v in {
                "first_name": first or None,
                "last_name": last or None,
                "title": contact["title"],
                "email": contact["email"],
                "phone": contact["phone"],
            }.items()
            if v
        }
        if contact["name"]:
            figures.append({"label": "Contact", "value": contact["name"]})
        if contact["title"]:
            figures.append({"label": "Title", "value": contact["title"]})
        if contact["email"]:
            figures.append({"label": "Email", "value": contact["email"]})
        if contact["phone"]:
            figures.append({"label": "Phone", "value": contact["phone"]})
    deal_in: dict[str, Any] = {}
    name = _text(inputs.get("deal_name"), 200)
    if deal is None:
        deal_in["name"] = _deal_name(name, company, contact["name"], picked)
        figures.append({"label": "Deal", "value": deal_in["name"]})
        figures.append({"label": "Stage", "value": "Lead"})
    description = _text(inputs.get("description"), 2000)
    if description and deal is None:
        deal_in["description"] = description
    if amount is not None:
        deal_in["amount"] = f"{amount}"
        figures.append({"label": "Value", "value": inr(amount)})
    source = _text(inputs.get("source"), 60)
    referred = _text(inputs.get("referred_by"), 200)
    if referred and not source:
        source = "Referral"
    if source:
        deal_in["source"] = source
        figures.append(
            {"label": "Source", "value": source + (f" ({referred})" if referred else "")}
        )
    decision = _day(inputs.get("decision_by"))
    next_step = _text(inputs.get("next_step"), 500)
    if decision and not next_step:
        next_step = f"Decision expected by {decision:%d %b %Y}"
    if next_step:
        deal_in["next_step"] = next_step
        figures.append({"label": "Next step", "value": next_step})
    if decision and deal is None:
        notes.append(
            "A lead carries no close date in Saleinx, so the decision date goes in the next step. "
            "Set the close date when it moves to Qualified or later."
        )
    if picked:
        deal_in["offerings"] = picked
        figures.append({"label": "Sells", "value": ", ".join(picked)})
    if deal_in:
        params["deal"] = deal_in
    follow = _text(inputs.get("follow_up"), 280)
    who = contact["name"] or company or (str(deal.get("title") or "") if deal else "")
    if follow and who:
        # A task list shows the title alone: "Call her" must still say who.
        names = [part for part in (who.split()[0], company) if part]
        if not any(part.lower() in follow.lower() for part in names):
            follow = f"{follow} ({who})"
    if follow_day or follow:
        params["follow_up"] = {
            "title": follow or f"Follow up with {who or 'the lead'}",
            "due_date": follow_day.isoformat() if follow_day else None,
            "type": "call" if re.search(r"\bcall|phone|ring\b", follow.lower()) else "followup",
        }
        figures.append(
            {
                "label": "Follow-up",
                "value": params["follow_up"]["title"]
                + (f" on {_when(follow_day.isoformat())}" if follow_day else ""),
            }
        )
    note_parts = [
        p
        for p in (f"Referred by {referred}." if referred else "", _text(inputs.get("note"), 4000))
        if p
    ]
    if note_parts:
        params["note"] = " ".join(note_parts)
        notes.append(f"Note on the deal: {params['note'][:300]}")
    if deal is not None and len(params) == 1:
        return [_notice("What should change on the lead?")], used

    action = await actions._offer(
        session,
        user,
        ctx,
        kind="saleinx_lead",
        payload=params,
        summary={
            "title": (
                f"Update {deal.get('title')} in Saleinx"
                if deal
                else f"New lead in Saleinx: {company}"
            ),
            "figures": figures,
            "notes": notes,
            "confirm_label": "Update the lead" if deal else "Add the lead",
            "question": "Update it?" if deal else "Add this lead?",
        },
    )
    return [actions.card_for(action)], used


async def propose_deal_decision(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> Result:
    """A manager approves or rejects a rep's Won that waits on them."""
    from app.services.companion import actions

    role = await _role(session, user)
    if role is None:
        return [_notice("Saleinx is not linked to your workspace for you.", "warning")], {}
    if role not in APPROVER_ROLES:
        return [
            _notice(
                "Approving a won deal is for sales managers, admins and owners in Saleinx.",
                "warning",
                note=True,
            )
        ], {}
    decision = str(inputs.get("decision") or "none")
    wanted = _text(inputs.get("deal"), 160)
    used = {"deal": wanted, "decision": decision}
    if decision not in ("approve", "reject"):
        return [_notice("Approve it or reject it?")], used
    data = await products.ask(session, user, SALEINX, "approvals", {})
    waiting = [d for d in data.get("deals") or [] if isinstance(d, dict)]
    if wanted:
        low = wanted.lower()
        waiting = [d for d in waiting if low in str(d.get("title") or "").lower()] or waiting
    if not waiting:
        return [_notice("No won deal is waiting for approval in Saleinx.", note=True)], used
    if len(waiting) > 1:
        return _which(waiting, wanted or "waiting for approval"), used
    deal = waiting[0]
    reason = _text(inputs.get("reason"), 500)
    if decision == "reject" and not reason:
        return [_notice("Why is it rejected? The rep sees the reason.")], used
    verb = "Approve" if decision == "approve" else "Reject"
    action = await actions._offer(
        session,
        user,
        ctx,
        kind="saleinx_decision",
        payload={"deal_id": deal["id"], "decision": decision, "reason": reason or None},
        summary={
            "title": f"{verb} the Won on {deal.get('title')}",
            "figures": [
                {"label": "Deal", "value": str(deal.get("title"))},
                {"label": "Value", "value": _money(deal.get("amount"), deal.get("currency"))},
                {"label": "Margin", "value": _pct(deal.get("margin_pct"))},
            ],
            "notes": [
                "Approved, it counts toward targets and its order can be booked."
                if decision == "approve"
                else f"Rejected, it goes back to Negotiation for the rep. Reason: {reason}"
            ],
            "confirm_label": verb,
            "question": f"{verb} it?",
        },
    )
    return [actions.card_for(action)], used


#: How far from today a call or meeting may be dated, either way: "2062" is a typo.
ACTIVITY_REACH_DAYS = 366
ACTIVITY_LABEL = {"call": "Call", "meeting": "Meeting"}


async def propose_activity(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> Result:
    """A call or meeting in a deal's Activity, as Saleinx's "Log activity" writes it.

    One still to come ("schedule a call on 23rd Nov") is logged for that day and also
    goes on the person's Saleinx Tasks for it, so it reminds them; the offer says so.
    """
    from app.services.attendance import service as attendance
    from app.services.companion import actions

    role = await _role(session, user)
    if role is None:
        return [_notice("Saleinx is not linked to your workspace for you.", "warning")], {}
    if role == "viewer":
        return [_viewer_refusal(role)], {}
    wanted = _text(inputs.get("deal"), 160)
    kind = _text(inputs.get("activity_type"), 20).lower()
    raw_day = _text(inputs.get("activity_date"), 40)
    notes = _text(inputs.get("activity_notes"), 4000)
    used = {"deal": wanted, "activity_type": kind, "activity_date": raw_day}
    if not wanted:
        return [_notice("Which deal is it for?")], used
    if kind not in ACTIVITY_LABEL:
        return [_notice("Is it a call or a meeting?")], used
    today = await attendance.local_today(session, user)
    on = _day(raw_day) if raw_day else today
    if on is None:
        return [_notice("Give the date as YYYY-MM-DD, like 2026-11-23.")], used
    if abs((on - today).days) > ACTIVITY_REACH_DAYS:
        return [_notice(f"{_when(on.isoformat())} is more than a year away. Which date?")], used
    found = await _find_deal(session, user, wanted)
    if isinstance(found, list):
        if not found:
            return [
                _notice(
                    f"No lead or deal you can see in Saleinx matches “{wanted}”.",
                    "warning",
                    note=True,
                )
            ], used
        return _which(found, wanted), used
    deal = found
    label = ACTIVITY_LABEL[kind]
    ahead = on > today
    params: dict[str, Any] = {
        "deal_id": deal["id"],
        "type": kind,
        "date": on.isoformat(),
        "notes": notes or None,
    }
    figures = [
        {"label": "Deal", "value": str(deal.get("title"))},
        {"label": "Activity", "value": label + (" (planned)" if ahead else "")},
        {"label": "Date", "value": _when(on.isoformat())},
    ]
    if notes:
        figures.append({"label": "Notes", "value": notes})
    remarks = [f"It shows in the deal's Activity in Saleinx as a {kind} on {on:%d %b %Y}."]
    if ahead:
        params["task"] = {"title": f"{label}: {deal.get('title')}", "due_date": on.isoformat()}
        figures.append({"label": "Reminder", "value": f"{label} task in your Saleinx Tasks"})
        remarks.append(f"The date is ahead, so a {kind} task due that day is added for you too.")
    action = await actions._offer(
        session,
        user,
        ctx,
        kind="saleinx_activity",
        payload=params,
        summary={
            "title": f"Log a {kind} on {deal.get('title')} in Saleinx",
            "figures": figures,
            "notes": remarks,
            "confirm_label": f"Log the {kind}",
            "question": "Log it?",
        },
    )
    return [actions.card_for(action)], used


async def propose_procurement_decision(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> Result:
    """Approve or reject what waits on the person in ProcureFlow, by the refs it showed."""
    from app.services.companion import actions

    if await products.role_in(session, user, PROCUREMENT) is None:
        return [_notice("ProcureFlow is not linked to your workspace for you.", "warning")], {}
    decision = str(inputs.get("decision") or "none")
    refs = [
        part.split(":", 1)[1]
        for part in re.split(r"[,\s]+", str(inputs.get("refs") or ""))
        if part.startswith("pf:") and part[3:]
    ]
    used = {"decision": decision, "refs": ",".join(f"pf:{r}" for r in refs)}
    if decision not in ("approve", "reject"):
        return [_notice("Approve or reject?")], used
    if not refs:
        return [
            _notice("Which ones? Look them up first with procurement_approvals; each has a ref.")
        ], used
    note = _text(inputs.get("note"), 2000)
    if decision == "reject" and not note:
        return [_notice("Why are they rejected? The requester sees the reason.")], used
    data = await products.ask(session, user, PROCUREMENT, "approvals", {})
    waiting = {str(a.get("ref")): a for a in data.get("approvals") or [] if isinstance(a, dict)}
    items = [waiting[r] for r in dict.fromkeys(refs) if r in waiting][:20]
    if not items:
        return [
            _notice(
                "None of those is waiting on you in ProcureFlow any more.", "warning", note=True
            )
        ], used
    verb = "Approve" if decision == "approve" else "Reject"
    action = await actions._offer(
        session,
        user,
        ctx,
        kind="procurement_decision",
        payload={
            "decision": decision,
            "note": note or None,
            "items": [
                {"ref": str(a.get("ref")), "label": f"{a.get('number')}: {a.get('title')}"}
                for a in items
            ],
        },
        summary={
            "title": f"{verb} {len(items)} in ProcureFlow",
            "figures": [
                {
                    "label": f"{_DOC_LABEL.get(str(a.get('document')), 'Document')} "
                    f"{a.get('number')}",
                    "value": f"{a.get('title')} · "
                    + (_money(a.get("amount"), a.get("currency")) if a.get("amount") else "—"),
                }
                for a in items
            ],
            "notes": [f"Your note: {note}"] if note else [],
            "confirm_label": verb,
            "question": f"{verb} {'it' if len(items) == 1 else 'them'}?",
        },
    )
    return [actions.card_for(action)], used


# ── Carrying out (from actions.confirm) ─────────────────────────────────────


def _unavailable(exc: products.ProductUnavailableError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


async def carry_out(session: AsyncSession, user: User, action: CompanionAction) -> dict[str, Any]:
    """Do the Saleinx or ProcureFlow change the person confirmed, through the product, as them."""
    payload = dict(action.payload)
    if action.kind == "saleinx_lead":
        try:
            answer = await products.act(session, user, SALEINX, "lead.save", payload)
        except products.ProductUnavailableError as exc:
            raise _unavailable(exc) from exc
        deal = answer.get("deal") or {}
        done = "; ".join(str(d) for d in answer.get("done") or [])
        # Only the first letter is raised: the rest are names and dates as Saleinx gave them.
        text = f"Saved in Saleinx: {deal.get('title')} ({deal.get('stage_label')})." + (
            f" {done[:1].upper()}{done[1:]}." if done else ""
        )
        return {"text": text, "open": "/launch/saleinx", "reference": deal.get("id")}

    if action.kind == "saleinx_stage":
        try:
            answer = await products.act(session, user, SALEINX, "deal.stage", payload)
        except products.ProductUnavailableError as exc:
            raise _unavailable(exc) from exc
        deal = answer.get("deal") or {}
        if answer.get("awaiting_approval"):
            text = (
                f"{deal.get('title')} is marked Won in Saleinx and waits for a manager's "
                "approval. It counts once they approve it."
            )
        else:
            text = f"{deal.get('title')} is now at {deal.get('stage_label')} in Saleinx."
        return {"text": text, "open": "/launch/saleinx", "reference": deal.get("id")}

    if action.kind == "saleinx_activity":
        try:
            answer = await products.act(session, user, SALEINX, "deal.activity", payload)
        except products.ProductUnavailableError as exc:
            raise _unavailable(exc) from exc
        deal = answer.get("deal") or {}
        kind = str(payload.get("type") or "call")
        text = (
            f"Logged in Saleinx: a {kind} on {_when(payload.get('date'))}, in the Activity "
            f"of {deal.get('title')}."
        )
        if answer.get("task_id"):
            text += f" A {kind} task due that day is on your Saleinx Tasks."
        elif answer.get("task_error"):
            text += f" The reminder task could not be added: {answer['task_error']}"
        return {"text": text, "open": "/launch/saleinx", "reference": deal.get("id")}

    if action.kind == "saleinx_decision":
        try:
            answer = await products.act(session, user, SALEINX, "deal.decide", payload)
        except products.ProductUnavailableError as exc:
            raise _unavailable(exc) from exc
        deal = answer.get("deal") or {}
        text = (
            f"The Won on {deal.get('title')} is approved."
            if payload.get("decision") == "approve"
            else f"The Won on {deal.get('title')} is rejected; it is back at "
            f"{deal.get('stage_label')}."
        )
        return {"text": text, "open": "/launch/saleinx", "reference": deal.get("id")}

    if action.kind == "procurement_decision":
        done: list[str] = []
        failed: list[str] = []
        for item in payload.get("items") or []:
            try:
                await products.act(
                    session,
                    user,
                    PROCUREMENT,
                    "approval.decide",
                    {
                        "approval_id": item["ref"],
                        "decision": payload["decision"],
                        "comment": payload.get("note"),
                    },
                )
                done.append(item["label"])
            except products.ProductUnavailableError as exc:
                failed.append(f"{item['label']} ({exc})")
        if not done:
            raise HTTPException(status_code=409, detail="Nothing was decided: " + "; ".join(failed))
        verb = "approved" if payload["decision"] == "approve" else "rejected"
        text = f"{len(done)} {verb} in ProcureFlow."
        if failed:
            text += " Not decided: " + "; ".join(failed)
        return {"text": text, "open": "/launch/procurement", "done": done, "failed": failed}

    raise HTTPException(status_code=422, detail="This offer cannot be carried out.")


# ── Read tools, by key (tools.py) ───────────────────────────────────────────

_RUNNERS = {
    "sales_deals": _deals,
    "sales_pipeline": _pipeline,
    "procurement_overview": _procurement_overview,
    "procurement_approvals": _procurement_approvals,
    "procurement_documents": _procurement_documents,
    "procurement_budgets": _procurement_budgets,
    "account_forecast": _account_forecast,
}


READ_TOOLS = frozenset(_RUNNERS)


async def run(
    session: AsyncSession, user: User, key: str, inputs: dict[str, Any], *, today: date
) -> Result:
    try:
        return await _RUNNERS[key](session, user, inputs, today)
    except products.ProductUnavailableError as exc:
        return [_notice(str(exc), "warning")], {}
