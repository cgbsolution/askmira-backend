"""Ask Mira's actions on CGB Helpdesk tickets: raise one, and work one.

`raise_it_ticket` prepares an IT ticket from what the person said (a laptop
that won't start, a VPN that drops), checked against the helpdesk's own
categories, sub categories and priorities, and offers it.

`work_it_ticket` does what the helpdesk's ticket page does, for the person who
may: write on the conversation, pick up a ticket from the queue, give it to an
engineer, put it on hold, close it or reopen it. It finds the ticket among
those the person can see there, reads what the helpdesk lets *them* do on it
(its `rights`, the same rules as its own page), and only offers that.

Nothing happens until the person confirms (actions.py). Then the helpdesk does
it, as them (`products.data.act`, signed `use=act`), with its own notifications
and emails; its refusal comes back in its own words. AskMira writes an audit
entry with the ticket's reference and what was done — no subject, message or
reason text, which stay in the helpdesk.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction
from app.models.companion import CompanionAction
from app.models.user import User
from app.services import audit
from app.services.products import data as products

__all__ = [
    "HOLD_HOURS",
    "TICKET_ACTIONS",
    "carry_out",
    "propose_raise",
    "propose_work",
]

PRODUCT = "helpdesk"
BRAND = "CGB Helpdesk"
TICKET_ACTIONS = ("reply", "acquire", "assign", "hold", "close", "reopen")
HOLD_HOURS = (24, 48, 72)
PRIORITIES = ("low", "medium", "high")

_LABEL = {
    "reply": "Reply",
    "acquire": "Pick up",
    "assign": "Assign",
    "hold": "Put on hold",
    "close": "Close",
    "reopen": "Reopen",
}
_RIGHT = {
    "reply": "reply",
    "acquire": "acquire",
    "assign": "reassign",
    "hold": "hold",
    "close": "close",
    "reopen": "reopen",
}
_REF = re.compile(r"#?\b([0-9a-f]{8})\b", re.IGNORECASE)

Result = tuple[list[dict[str, Any]], dict[str, Any]]


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def _text(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _status(value: Any) -> str:
    return str(value or "").replace("_", " ").capitalize()


def _unlinked(exc: products.ProductUnavailableError) -> list[dict[str, Any]]:
    return [_notice(str(exc), "warning")]


# ── Raising a ticket ────────────────────────────────────────────────────────


def _pick(options: list[str], wanted: str) -> list[str]:
    """Options matching what the person said: an exact name first, else those containing it."""
    want = wanted.strip().lower()
    if not want:
        return []
    exact = [o for o in options if o.lower() == want]
    return exact or [o for o in options if want in o.lower() or o.lower() in want]


async def propose_raise(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> Result:
    """Check a new IT ticket against the helpdesk's own choices, and offer it."""
    from app.services.companion import actions

    subject = _text(inputs.get("subject"), 200)
    description = str(inputs.get("description") or "").strip()[:5000]
    used = {"subject": subject, "category": _text(inputs.get("category"), 80)}
    try:
        options = await products.ask(session, user, PRODUCT, "options", {})
    except products.ProductUnavailableError as exc:
        return _unlinked(exc), used
    categories = {
        str(c.get("name")): [str(s) for s in c.get("sub_categories") or []]
        for c in options.get("categories") or []
        if isinstance(c, dict) and c.get("name")
    }
    if len(subject) < 3:
        return [_notice("What is the problem, in a few words (the ticket's subject)?")], used
    found = _pick(list(categories), str(inputs.get("category") or ""))
    wanted_sub = _text(inputs.get("sub_category"), 80)
    if len(found) != 1 and wanted_sub:
        # A sub category names its category: "Laptop Issue" is Hardware.
        owners = [c for c, subs in categories.items() if len(_pick(subs, wanted_sub)) == 1]
        found = owners if len(owners) == 1 else found
    if len(found) != 1:
        listed = "; ".join(f"{c} ({', '.join(subs)})" for c, subs in categories.items())
        card = _notice("Which category fits best: " + ", ".join(categories) + "?")
        card["model_note"] = (
            f"{BRAND}'s categories, with their sub categories: {listed}. Choose the one that "
            "fits what the person said and call raise_it_ticket again; ask them only if it is "
            "truly unclear."
        )
        return [card], used
    category = found[0]
    subs = categories[category]
    sub = ""
    if _text(inputs.get("sub_category"), 80):
        matched = _pick(subs, str(inputs.get("sub_category")))
        if len(matched) != 1:
            return [_notice(f"Which kind of {category} problem: " + ", ".join(subs) + "?")], used
        sub = matched[0]
    priority = str(inputs.get("priority") or "").strip().lower()
    if priority not in PRIORITIES:
        return [_notice("How urgent is it: low, medium or high?")], used
    fields: dict[str, Any] = {
        "subject": subject,
        "description": description or subject,
        "category": category,
        "priority": priority,
    }
    if sub:
        fields["sub_category"] = sub
    location = _text(inputs.get("location"), 120)
    if location:
        fields["location"] = location
    figures = [
        {"label": "Subject", "value": subject},
        {"label": "Category", "value": f"{category} / {sub}" if sub else category},
        {"label": "Priority", "value": priority.capitalize()},
    ]
    if location:
        figures.append({"label": "Location", "value": location})
    action = await actions._offer(
        session,
        user,
        ctx,
        kind="helpdesk_ticket",
        payload=fields,
        summary={
            "title": f"IT ticket in {BRAND}",
            "figures": figures,
            "notes": [description[:300] + ("…" if len(description) > 300 else "")]
            if description
            else [],
            "confirm_label": "Raise ticket",
            "question": "Raise this ticket?",
        },
    )
    return [actions.card_for(action)], used


# ── Working a ticket ────────────────────────────────────────────────────────


async def _tickets(session: AsyncSession, user: User, query: str) -> list[dict[str, Any]]:
    """Tickets matching `query` among all the person can see: their company's, else their own."""
    seen: dict[str, dict[str, Any]] = {}
    for scope in ("company", "assigned", "mine"):
        try:
            data = await products.ask(
                session, user, PRODUCT, "tickets", {"scope": scope, "query": query, "limit": 25}
            )
        except products.ProductUnavailableError as exc:
            if exc.status == 403:  # not their list there (not IT staff or HR)
                continue
            raise
        for t in data.get("tickets") or []:
            if isinstance(t, dict) and t.get("id"):
                seen.setdefault(str(t["id"]), t)
    return list(seen.values())


async def _find(
    session: AsyncSession, user: User, wanted: str
) -> dict[str, Any] | list[dict[str, Any]]:
    """The one ticket the person means, or those it could be (empty: none)."""
    ref = _REF.search(wanted)
    found = await _tickets(session, user, ref.group(1) if ref else wanted)
    if ref:
        exact = [t for t in found if str(t.get("ref") or "").lower() == ref.group(1).lower()]
        if len(exact) == 1:
            return exact[0]
    if len(found) == 1:
        return found[0]
    want = wanted.lower().strip()
    exact = [t for t in found if str(t.get("subject") or "").lower() == want]
    return exact[0] if len(exact) == 1 else found


def _choices(found: list[dict[str, Any]], wanted: str) -> list[dict[str, Any]]:
    rows = [
        [
            f"#{t.get('ref')}",
            t.get("subject"),
            _status(t.get("status")),
            t.get("requester"),
            t.get("assignee") or "Nobody",
        ]
        for t in found[:10]
    ]
    example = found[0].get("ref")
    text = f"More than one ticket matches “{wanted}”. Which one? Say its number, like #{example}."
    return [
        {
            "type": "table",
            "title": f"Tickets matching “{wanted}”",
            "columns": ["Ticket", "Subject", "Status", "Raised by", "With"],
            "rows": rows,
            "caption": "Say which one",
            "source": BRAND,
        },
        _notice(text),
    ]


def _not_allowed(action: str, ticket: dict[str, Any]) -> str:
    status = str(ticket.get("status") or "")
    if status == "closed" and action not in ("reopen",):
        return "This ticket is closed. It has to be reopened first."
    if action == "reopen" and status not in ("closed", "hold"):
        return "This ticket is already open."
    if action == "hold" and status == "hold":
        return "This ticket is already on hold."
    if action == "acquire":
        return (
            f"It is already with {ticket.get('assignee')}."
            if ticket.get("assignee")
            else f"Only {BRAND}'s IT team for this company can pick it up."
        )
    if action == "assign":
        return f"Only an IT admin in {BRAND} can assign tickets."
    if action == "reply":
        return "You can read this ticket but not write on it."
    return f"Only the engineer working on it or an IT admin in {BRAND} can do that."


async def propose_work(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> Result:
    """Find the ticket, check the helpdesk lets this person do it, and offer it."""
    from app.services.companion import actions

    wanted = _text(inputs.get("ticket"), 160)
    action = str(inputs.get("ticket_action") or "none")
    used: dict[str, Any] = {"ticket": wanted, "ticket_action": action}
    if action not in TICKET_ACTIONS:
        return [
            _notice("What should I do with it: reply, pick it up, assign, hold, close or reopen?")
        ], used
    if not wanted:
        return [_notice("Which ticket? Its number, like #1A2B3C4D, or its subject.")], used
    try:
        found = await _find(session, user, wanted)
        if isinstance(found, list):
            if not found:
                return [
                    _notice(f"No ticket you can see in {BRAND} matches “{wanted}”.", "warning")
                ], used
            return _choices(found, wanted), used
        detail = await products.ask(session, user, PRODUCT, "ticket", {"id": found["id"]})
    except products.ProductUnavailableError as exc:
        return _unlinked(exc), used
    ticket = detail.get("ticket") or {}
    if not isinstance(ticket.get("rights"), dict):
        return [
            _notice(
                f"{BRAND} needs its latest update before tickets can be worked from here.",
                "warning",
            )
        ], used
    rights = ticket["rights"]
    if not rights.get(_RIGHT[action]):
        return [_notice(_not_allowed(action, ticket), "warning")], used

    ref = str(ticket.get("ref") or "")
    params: dict[str, Any] = {"ticket_id": str(ticket.get("id"))}
    figures = [
        {"label": "Ticket", "value": f"#{ref} {ticket.get('subject')}"},
        {
            "label": "Before",
            "value": _status(ticket.get("status"))
            + (f", with {ticket.get('assignee')}" if ticket.get("assignee") else ", not assigned"),
        },
    ]
    notes: list[str] = []
    if action == "reply":
        message = str(inputs.get("message") or "").strip()[:4000]
        if not message:
            return [_notice("What should the reply say?")], used
        params["message"] = message
        notes.append(message[:300] + ("…" if len(message) > 300 else ""))
    elif action == "assign":
        who = _text(inputs.get("engineer"), 120)
        try:
            staff = (
                await products.ask(
                    session, user, PRODUCT, "assignees", {"ticket_id": params["ticket_id"]}
                )
            ).get("staff") or []
        except products.ProductUnavailableError as exc:
            return _unlinked(exc), used
        staff = [s for s in staff if isinstance(s, dict)]
        names = ", ".join(str(s.get("name")) for s in staff) or "nobody"
        if not who:
            return [_notice(f"Who should get it? IT staff for this company: {names}.")], used
        want = who.lower()
        match = [
            s
            for s in staff
            if want in (str(s.get("email") or "").lower(), str(s.get("name") or "").lower())
        ]
        match = match or [s for s in staff if want in str(s.get("name") or "").lower()]
        if len(match) != 1:
            return [_notice(f"Who exactly? IT staff for this company: {names}.")], used
        params["engineer_id"] = str(match[0]["id"])
        figures.append({"label": "Give it to", "value": str(match[0].get("name"))})
    elif action == "hold":
        reason = str(inputs.get("reason") or "").strip()[:2000]
        try:
            hours = int(str(inputs.get("hours") or "0").strip())
        except ValueError:
            hours = 0
        asks = []
        if not reason:
            asks.append("Why is it going on hold?")
        if hours not in HOLD_HOURS:
            asks.append("For how long: 24, 48 or 72 hours?")
        if asks:
            return [_notice(" ".join(asks))], used
        params.update(reason=reason, hours=hours)
        figures.append({"label": "On hold for", "value": f"{hours} hours"})
        notes.append(f"Reason: {reason[:300]}")
    elif action == "close":
        comment = str(inputs.get("note") or "").strip()[:2000]
        if comment:
            params["comment"] = comment
            notes.append(f"Closing note: {comment[:300]}")
    notes.append(f"{BRAND} tells the requester, as it does when this is done there.")

    offer = await actions._offer(
        session,
        user,
        ctx,
        kind="helpdesk_work",
        payload={"kind": action, "ref": ref, "params": params},
        summary={
            "title": f"{_LABEL[action]} ticket #{ref}",
            "figures": figures,
            "notes": notes,
            "confirm_label": _LABEL[action],
            "question": f"{_LABEL[action]} ticket #{ref}?",
        },
    )
    return [actions.card_for(offer)], used


# ── Carrying out what the person confirmed ─────────────────────────────────


def _id(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def carry_out(session: AsyncSession, user: User, action: CompanionAction) -> dict[str, Any]:
    """Do it in the helpdesk, as the person. Its refusal is the person's answer."""
    payload = dict(action.payload)
    if action.kind == "helpdesk_ticket":
        kind, params = "create_ticket", payload
    else:
        kind, params = str(payload["kind"]), dict(payload["params"])
    try:
        answer = await products.act(session, user, PRODUCT, kind, params)
    except products.ProductUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    ticket = answer.get("ticket") or {}
    ref = str(ticket.get("ref") or payload.get("ref") or "")[:12]
    ticket_id = str(ticket.get("id") or params.get("ticket_id") or "")
    text = {
        "create_ticket": f"Ticket #{ref} is raised in {BRAND}. The IT team picks it up from there.",
        "reply": f"Your reply is on ticket #{ref}.",
        "acquire": f"Ticket #{ref} is yours now.",
        "assign": f"Ticket #{ref} is with {ticket.get('assignee') or 'the engineer'} now.",
        "hold": f"Ticket #{ref} is on hold for {params.get('hours')} hours.",
        "close": f"Ticket #{ref} is closed.",
        "reopen": f"Ticket #{ref} is open again.",
    }[kind]
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE if kind in ("create_ticket", "reply") else AuditAction.UPDATE,
        entity_type="helpdesk_ticket" if kind != "reply" else "helpdesk_reply",
        entity_id=_id(ticket_id),
        actor=user,
        changes={"product": PRODUCT, "ticket": ref, "did": kind, "via": "ask_mira"},
        summary=f"{_LABEL.get(kind, 'Raised')} {BRAND} ticket #{ref} through Ask Mira",
        request=None,
    )
    return {
        "text": text,
        "open": f"/linked/{PRODUCT}/tickets/{ticket_id}"
        if ticket_id
        else f"/linked/{PRODUCT}/tickets",
        "reference": ref,
    }
