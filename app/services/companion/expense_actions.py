"""Ask Mira in ExpenseGenie: file a claim from a receipt, decide claims waiting on you.

Filing: Mira reads a receipt the person attached (or what they typed) and offers
the claim line by line, checked first against ExpenseGenie's own list for this
person (kind ``categories``): the workspace's categories, which need a bill,
and their grade's cap on each — on each bill, so lines of one invoice are added
up — and against the bills they claimed before (kind ``bills``): a bill is
claimed once. On the person's yes ExpenseGenie files it as them (products.act,
kind ``claim.create``) with the same rules as its own app — it goes to their
manager, a category that needs the bill stays a draft until the original is
attached there — keyed by the offer, so a retry never files twice (one retry is
made when the answer was lost on the way). AskMira never keeps the receipt.

Deciding: what waits on the person in ExpenseGenie (kind ``claims``, scope
approvals) is listed in the offer, on the screen only; on the yes each is
approved or rejected there as them (kind ``claim.decide``), through its two
levels (manager, then finance). Each is named by its own id there and decided
only while it still waits on the person; a claim decided elsewhere meanwhile is
said, not forced. A rejection's note is the reason the submitter reads.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.companion import CompanionAction
from app.models.user import User
from app.services.companion.budget_tools import rupees_in
from app.services.documents.indian_numbers import group_indian
from app.services.products import data as products
from app.services.products.catalogue import PRODUCTS

__all__ = ["carry_out", "propose_claim", "propose_decision"]

PRODUCT = "expense"
BRAND = PRODUCTS[PRODUCT].brand
IST = ZoneInfo("Asia/Kolkata")
#: A number typed with more than two decimals ("12.345"): no bill has those paise, and
#: the model may read the dot as a thousands separator and pass 12345.
_LONG_DECIMALS = re.compile(r"(?<![\d.])\d[\d,]*\.\d{3,}(?![\d.])")
MAX_LINES = 20
MAX_DECISIONS = 20
MAX_AMOUNT = Decimal(10_000_000)

Result = tuple[list[dict[str, Any]], dict[str, Any]]


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    # The model may not read these tools' records, but it must know nothing was offered.
    return {
        "type": "notice",
        "tone": tone,
        "text": text,
        "model_note": f"Nothing was offered or done: {text} Say so; never say it is prepared.",
    }


#: Words of a request that name no claim ("Meera's auto claim" is Meera + auto).
_FILLER = frozenset(
    {"claim", "claims", "expense", "expenses", "bill", "bills", "the", "a", "an", "for", "of",
     "my", "her", "his", "their", "waiting", "on", "me", "to", "and", "please", "reject",
     "approve", "it", "them", "this", "that", "one", "ones"}
)  # fmt: skip


def _search_words(query: str) -> str:
    words = [w for w in re.split(r"\s+", query.replace(chr(0x2019), "'")) if w]
    kept = [re.sub(r"'s$", "", w) for w in words]
    return " ".join(w for w in kept if w and w.lower() not in _FILLER)


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _inr(value: Decimal) -> str:
    """₹1,250.50 — Indian grouping, paise only when there are any."""
    # To the paisa first: a product's float total 7493.999… is ₹7,494, not ₹7,493.00.
    value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole = value.quantize(Decimal("1"), rounding=ROUND_DOWN)
    paise = value - whole
    return f"₹{group_indian(whole)}" + (f"{paise:.2f}"[1:] if paise else "")


def _amount(raw: Any) -> Decimal | None:
    """Rupees as typed ("₹1,250.50", "850", "1.2 lakh"); never more than two decimals."""
    text = str(raw or "").strip()
    # "12.345" has paise that don't exist; "1e5" is not how anyone writes a bill.
    if re.search(r"\.\d{3,}$", text.replace(",", "")) or re.search(r"\d[eE][+-]?\d", text):
        return None
    value = rupees_in(text)
    if value is None or value > MAX_AMOUNT:
        return None
    return value


def _day(raw: Any, today: date) -> date | None:
    try:
        day = date.fromisoformat(str(raw or "").strip())
    except ValueError:
        return None
    if day > today or day < today - timedelta(days=366):
        return None
    return day


def _pick(names: list[str], wanted: str) -> list[str]:
    want = wanted.strip().lower()
    if not want:
        return []
    exact = [n for n in names if n.lower() == want]
    return exact or [n for n in names if want in n.lower() or n.lower() in want]


def _bill_key(line: dict[str, Any], index: int) -> str:
    """Lines of one category and invoice are one bill; a line with no invoice is its own."""
    invoice = str(line.get("invoice_number") or "").strip().lower()
    return f"{line['category'].lower()}|{invoice or f'#{index}'}"


async def propose_claim(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> Result:
    """Check a claim against ExpenseGenie's own categories and caps, and offer it."""
    from app.services.companion import actions

    title = _text(inputs.get("claim_title"), 200)
    raw_lines = [x for x in inputs.get("claim_lines") or [] if isinstance(x, dict)]
    used = {"claim_title": title, "lines": len(raw_lines)}
    typed = _LONG_DECIMALS.search(str(getattr(ctx, "latest_text", "") or ""))
    if typed:
        # Checked against the person's own words: the model may have "fixed" it already.
        return [
            _notice(
                f"“{typed.group(0)}” has more than two decimals, so it can't be an amount in "
                "rupees and paise. What is the amount?"
            )
        ], used
    try:
        answer = await products.ask(session, user, PRODUCT, "categories", {})
    except products.ProductUnavailableError as exc:
        return [_notice(str(exc), "warning")], used
    categories = {
        str(c.get("name")): c for c in answer.get("categories") or [] if isinstance(c, dict)
    }
    names = list(categories)
    if not names:
        return [_notice(f"{BRAND} has no expense categories set up yet.", "warning")], used
    if not raw_lines:
        return [
            _notice(
                "What is the claim for? Give the amount, the bill's date and what it was, or "
                "attach the receipt."
            )
        ], used
    if len(raw_lines) > MAX_LINES:
        return [_notice(f"A claim has at most {MAX_LINES} lines. Split it in two.")], used

    today = datetime.now(IST).date()
    lines: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_lines, start=1):
        where = f"Line {index}" if len(raw_lines) > 1 else "The bill"
        found = _pick(names, _text(raw.get("category"), 80))
        if len(found) != 1:
            card = _notice(f"{where}: which category is it? " + ", ".join(names) + ".")
            card["model_note"] = (
                f"{BRAND}'s categories: {', '.join(names)}. Choose the one that fits the bill "
                "and call file_expense_claim again; ask only if it is truly unclear."
            )
            return [card], used
        name = found[0]
        amount = _amount(raw.get("amount"))
        if amount is None:
            return [
                _notice(
                    f"{where}: what is the amount, in rupees with at most two decimals, up to "
                    f"{_inr(MAX_AMOUNT)}?"
                )
            ], used
        day = _day(raw.get("date"), today)
        if day is None:
            return [
                _notice(
                    f"{where}: what is the bill's date? It can't be in the future or more than "
                    "a year old."
                )
            ], used
        lines.append(
            {
                "category": name,
                "amount": f"{amount}",
                "date": day.isoformat(),
                "vendor": _text(raw.get("vendor"), 120),
                "invoice_number": _text(raw.get("invoice_number"), 60),
                "description": _text(raw.get("description"), 500),
            }
        )
    # The grade's cap holds for each bill however it is split: lines of one invoice add up.
    bills: dict[str, Decimal] = {}
    for index, line in enumerate(lines):
        key = _bill_key(line, index)
        bills[key] = bills.get(key, Decimal(0)) + Decimal(line["amount"])
    for key, amount in bills.items():
        name = next(n for n in names if n.lower() == key.split("|", 1)[0])
        cap_raw = categories[name].get("cap")
        try:
            cap = Decimal(str(cap_raw)) if cap_raw not in (None, "") else None
        except InvalidOperation:
            cap = None
        if cap is not None and amount > cap:
            grade = answer.get("grade") or "your grade"
            text = (
                f"{name} is capped at {_inr(cap)} for grade {grade} in {BRAND}; this bill is "
                f"{_inr(amount)}. Claim up to the cap on one line, or ask your manager about "
                "the rest. Splitting the bill into lines doesn't change its total."
            )
            card = _notice(text, "warning")
            card["model_note"] = (
                f"Refused: the bill is over the cap. Never offer to split one bill into lines "
                f"to get under it. {text}"
            )
            return [card], used
    try:
        seen = await products.ask(session, user, PRODUCT, "bills", {"lines": lines})
    except products.ProductUnavailableError:
        seen = {}  # an older ExpenseGenie: it still refuses a bill claimed twice on filing
    repeated = [d for d in seen.get("duplicates") or [] if isinstance(d, dict)]
    if repeated:
        first = repeated[0]
        where = f"Line {first.get('line')}" if len(lines) > 1 else "This bill"
        text = (
            f"{where} is already claimed in {BRAND}: “{_text(first.get('title'), 120)}” "
            f"({_text(first.get('status'), 40)}). A bill is claimed once."
        )
        card = _notice(text, "warning")
        card["model_note"] = f"Refused, nothing was offered or filed: {text}"
        return [card], used
    if len(title) < 2:
        title = _text(lines[0]["vendor"] or lines[0]["category"], 200) or "Expense claim"
    total = sum((Decimal(line["amount"]) for line in lines), Decimal(0))
    bills = sorted({line["category"] for line in lines if categories[line["category"]].get(
        "requires_bill", True)})  # fmt: skip
    notes = [f"Filed in {BRAND} as you; it goes to your manager there for approval."]
    if bills:
        notes.append(
            f"{', '.join(bills)} needs the bill: the claim stays a draft in {BRAND} until you "
            "attach it there and send it. AskMira does not keep the receipt."
        )
    action = await actions._offer(
        session,
        user,
        ctx,
        kind="expense_claim",
        payload={"title": title, "lines": lines},
        summary={
            "title": f"Expense claim in {BRAND}",
            "figures": [
                {"label": "Claim", "value": title},
                {"label": "Total", "value": _inr(total)},
            ],
            "table": {
                "columns": ["Category", "Bill date", "Vendor", "Amount"],
                "rows": [
                    [
                        line["category"],
                        f"{date.fromisoformat(line['date']):%d %b %Y}",
                        line["vendor"] or "—",
                        _inr(Decimal(line["amount"])),
                    ]
                    for line in lines
                ],
            },
            "notes": notes,
            "confirm_label": "Save as draft" if bills else "File claim",
            "question": "File this claim?",
        },
    )
    return [actions.card_for(action)], used


async def propose_decision(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> Result:
    """Offer to approve or reject the claims waiting on the person that match."""
    from app.services.companion import actions

    decision = str(inputs.get("decision") or "none")
    note = _text(inputs.get("note"), 500)
    search = _search_words(_text(inputs.get("query"), 100))
    used = {"decision": decision, "query": search}
    if decision not in ("approve", "reject"):
        return [_notice("Approve them or reject them?")], used
    if decision == "reject" and not note:
        return [_notice("Why are they rejected? The submitter reads the reason.")], used
    try:
        data = await products.ask(
            session,
            user,
            PRODUCT,
            "claims",
            {"scope": "approvals", "status": "pending", "query": search, "limit": MAX_DECISIONS},
        )
    except products.ProductUnavailableError as exc:
        return [_notice(str(exc), "warning")], used
    claims = [c for c in data.get("claims") or [] if isinstance(c, dict) and c.get("reference")]
    if not claims:
        matching = f" matching “{search}”" if search else ""
        return [_notice(f"Nothing in {BRAND} is waiting on your approval{matching}.")], used
    items = [
        {
            "id": _text(c.get("id"), 40),
            "reference": str(c["reference"]),
            "title": _text(c.get("title"), 120),
            "submitter": _text(c.get("submitter"), 120),
            "amount": str(c.get("amount") or "0"),
        }
        for c in claims
    ]
    verb = "Approve" if decision == "approve" else "Reject"
    total = sum((Decimal(i["amount"]) for i in items), Decimal(0))
    action = await actions._offer(
        session,
        user,
        ctx,
        kind="expense_decision",
        payload={"decision": decision, "note": note, "items": items},
        summary={
            "title": f"{verb} {len(items)} claim{'s' if len(items) != 1 else ''} in {BRAND}",
            "figures": [{"label": "Total", "value": _inr(total)}],
            "table": {
                "columns": ["Claim", "By", "Amount"],
                "rows": [[i["title"], i["submitter"], _inr(Decimal(i["amount"]))] for i in items],
            },
            "notes": ([f"Note: {note}"] if note else [])
            + [f"Decided in {BRAND} as you, through its own approval levels."],
            "confirm_label": f"{verb} {len(items)}",
            "question": f"{verb} these?",
        },
    )
    card = actions.card_for(action)
    # Claims name people and amounts: the model reads how many, never whose.
    card["model_note"] = (
        f"An offer to {decision} {len(items)} claim(s) waiting on the person is on their "
        "screen. Ask them to confirm it."
    )
    return [card], used


async def carry_out(session: AsyncSession, user: User, action: CompanionAction) -> dict[str, Any]:
    """Do it in ExpenseGenie, as the person. Its refusal is the person's answer."""
    payload = dict(action.payload)
    if action.kind == "expense_claim":
        params = {
            "key": f"askmira-{action.id}",
            "title": payload["title"],
            "lines": payload["lines"],
        }
        try:
            answer = await products.act(session, user, PRODUCT, "claim.create", params)
        except products.ProductUnavailableError as exc:
            if exc.status is not None and exc.status < 500:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            # The answer was lost (a timeout, a proxy's 502): ask once more with the
            # same key. ExpenseGenie hands back the claim it filed, never a second.
            await asyncio.sleep(2)
            try:
                answer = await products.act(session, user, PRODUCT, "claim.create", params)
            except products.ProductUnavailableError as again:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{again} It may have been filed already: check My claims in {BRAND} "
                        "before filing it again."
                    ),
                ) from again
        claim = answer.get("claim") or {}
        status = str(claim.get("status") or "")
        amount = _inr(Decimal(str(claim.get("amount") or "0")))
        if status == "Draft":
            bills = ", ".join(answer.get("needs_bill") or [])
            text = (
                f"The claim “{claim.get('title')}” for {amount} is saved as a draft in {BRAND}. "
                f"Attach the bill for {bills} there and send it."
            )
        else:
            text = (
                f"The claim “{claim.get('title')}” for {amount} is filed in {BRAND} and waiting "
                "for your manager's approval."
            )
        return {"text": text, "open": "/linked/expense/mine", "reference": claim.get("reference")}

    approve = payload["decision"] == "approve"
    done: list[str] = []
    failed: list[str] = []
    finance: list[str] = []
    for item in payload["items"]:
        params = {
            "reference": item["reference"],
            "decision": payload["decision"],
            "comment": payload.get("note") or "",
        }
        if item.get("id"):
            params["id"] = item["id"]  # the very claim the offer showed
        if not approve:
            params["reason"] = (payload.get("note") or "")[:120]  # what the submitter reads
        label = f"{item['title']} ({item['submitter']})"
        try:
            answer = await products.act(session, user, PRODUCT, "claim.decide", params)
        except products.ProductUnavailableError as exc:
            failed.append(f"{label}: {exc}")
            continue
        done.append(label)
        if (answer.get("claim") or {}).get("next_approver"):
            finance.append(label)
    if not done:
        raise HTTPException(status_code=409, detail="Nothing was decided: " + "; ".join(failed))
    verb = "approved" if approve else "rejected"
    text = f"{len(done)} claim(s) {verb} in {BRAND}."
    if finance:
        text += f" {len(finance)} now wait for finance."
    if failed:
        text += " Not decided: " + "; ".join(failed)
    return {"text": text, "open": "/linked/expense/approvals", "done": done, "failed": failed}
