"""Ask Mira without a model: the person's words matched to one tool, by rule.

Used whenever the AI planner cannot be: the server has no AI provider, the
workspace switched AI off, or the person has not agreed to it. Nothing leaves
AskMira in this mode: the message is matched here, the tool runs here as the
person (tools.run), and the result is shown as cards, as with the model.

Only the tools that need no model of their own are offered here (tools.offered
with `ai=False`): the policy and report questions are answered by a model, so
they wait for AI to be on.

The matching is deliberately plain — keywords, a few date phrases and statuses.
When nothing matches, the reply says what can be asked, with examples drawn from
the tools this person actually has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.services.companion import product_tools
from app.services.companion.tools import Tool
from app.services.products.catalogue import PRODUCTS

__all__ = ["DirectPlan", "examples", "plan"]

_HIRE = PRODUCTS["interview"].brand
_EXPENSE = PRODUCTS["expense"].brand
_HELPDESK = PRODUCTS["helpdesk"].brand


@dataclass(frozen=True, slots=True)
class DirectPlan:
    reply: str
    tool: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)


# ── Reading the words ────────────────────────────────────────────────────────


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(rf"\b{pattern}", text) for pattern in patterns)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return first, following - timedelta(days=1)


def dates(text: str, today: date) -> tuple[str, str]:
    """The range of days the words name, as ISO dates; ("", "") when they name none."""
    found = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if found:
        return found[0], found[1] if len(found) > 1 else found[0]

    span = re.search(r"\b(next|last|past|coming)\s+(\d{1,2})\s+days?\b", text)
    if span:
        count = max(1, min(int(span.group(2)), product_tools.MAX_DAYS))
        if span.group(1) in ("next", "coming"):
            return today.isoformat(), (today + timedelta(days=count - 1)).isoformat()
        return (today - timedelta(days=count - 1)).isoformat(), today.isoformat()

    start: date | None = None
    end: date | None = None
    if _has(text, "today", "now", "right now"):
        start = end = today
    elif _has(text, "tomorrow"):
        start = end = today + timedelta(days=1)
    elif _has(text, "yesterday"):
        start = end = today - timedelta(days=1)
    elif _has(text, r"next week"):
        start = _monday(today) + timedelta(days=7)
        end = start + timedelta(days=6)
    elif _has(text, r"last week", r"previous week"):
        start = _monday(today) - timedelta(days=7)
        end = start + timedelta(days=6)
    elif _has(text, r"this week", r"week\b"):
        start = _monday(today)
        end = start + timedelta(days=6)
    elif _has(text, r"next month"):
        start, end = _month(_month(today)[1] + timedelta(days=1))
    elif _has(text, r"last month", r"previous month"):
        start, end = _month(today.replace(day=1) - timedelta(days=1))
    elif _has(text, r"this month", r"month\b"):
        start, end = _month(today)
    if start is None or end is None:
        return "", ""
    return start.isoformat(), end.isoformat()


def _span(inputs: dict[str, Any], fallback: str) -> str:
    start, end = inputs.get("from") or "", inputs.get("to") or ""
    if not start:
        return fallback
    return f"on {start}" if start == end else f"from {start} to {end}"


_STOP = {
    "a",
    "an",
    "the",
    "of",
    "for",
    "in",
    "on",
    "at",
    "to",
    "me",
    "my",
    "all",
    "any",
    "please",
    "show",
    "list",
    "find",
    "search",
    "get",
    "give",
    "tell",
    "who",
    "whos",
    "is",
    "are",
    "was",
    "what",
    "which",
    "with",
    "and",
    "or",
    "about",
    "from",
    "do",
    "does",
    "we",
    "our",
    "there",
    "can",
    "you",
    "i",
    "named",
    "called",
    "matching",
    "team",
    "department",
    "dept",
    "people",
    "person",
    "employee",
    "employees",
    "colleague",
    "colleagues",
    "staff",
    "directory",
    "job",
    "jobs",
    "role",
    "roles",
    "position",
    "positions",
    "opening",
    "openings",
    "candidate",
    "candidates",
    "applicant",
    "applicants",
    "interview",
    "interviews",
    "scheduled",
    "hiring",
    "claim",
    "claims",
    "expense",
    "expenses",
    "reimbursement",
    "reimbursements",
    "details",
    "contact",
    "email",
    "this",
    "next",
    "last",
    "week",
    "month",
    "today",
    "tomorrow",
    "yesterday",
    "how",
    "many",
    "much",
    "have",
    "has",
    "had",
    "be",
    "been",
    "it",
    "that",
    "these",
    "those",
    "status",
    "currently",
    "now",
    "working",
    "works",
    "work",
    "here",
    "everyone",
    "whole",
    "company",
    "everybody",
    "mine",
    "where",
    "anytimehire",
    "anytime",
    "hire",
    "expensegenie",
    "genie",
    "ticket",
    "tickets",
    "helpdesk",
    "help",
    "desk",
    "support",
    "asset",
    "assets",
    "raised",
    "assigned",
    "queue",
    "unassigned",
    "register",
    "hold",
    "progress",
    "cgb",
    "askmira",
    "mira",
}


def _search_text(text: str, drop: tuple[str, ...] = ()) -> str:
    """What is left to search by once commands, dates, statuses and filler are taken out."""
    quoted = re.search(r"[\"“](.+?)[\"”]", text)
    if quoted:
        return quoted.group(1).strip()[:100]
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    cleaned = re.sub(r"\b(next|last|past|coming)\s+\d{1,2}\s+days?\b", " ", cleaned)
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9.@+'-]*", cleaned) if w.strip("'")]
    kept = [w for w in words if w not in _STOP and w not in drop and not w.endswith("'s")]
    return " ".join(kept)[:100]


def _ticket_status(text: str) -> str:
    if _has(text, "in[ -]?progress", "being worked", "working on"):
        return "in_progress"
    if _has(text, "on hold", "hold\\b", "paused"):
        return "hold"
    if _has(text, "closed", "resolved", "done\\b", "fixed"):
        return "closed"
    if _has(text, "new\\b", "not picked", "unacknowledged"):
        return "new"
    if _has(text, "open\\b", "pending", "unresolved", "active"):
        return "open"
    return "none"


def _asset_status(text: str) -> str:
    if _has(text, "in use", "in-use", "allocated"):
        return "in_use"
    if _has(text, "spare", "unused", "free\\b", "available"):
        return "spare"
    if _has(text, "retired", "scrapped", "disposed"):
        return "retired"
    return "none"


def _asset_search(words: str) -> str:
    """Asset words to search by, singular: "laptops" finds "Laptop"."""
    text = _search_text(
        words,
        (
            "in",
            "use",
            "spare",
            "retired",
            "unused",
            "available",
            "free",
            "allocated",
            "held",
            "hold",
        ),
    )
    return " ".join(w[:-1] if len(w) > 4 and w.endswith("s") else w for w in text.split())


def _status(text: str, allowed: tuple[str, ...]) -> str:
    synonyms = {"closed": "finished", "filled": "finished", "active": "open", "paused": "inactive"}
    for word in re.findall(r"[a-z]+", text):
        word = synonyms.get(word, word)
        if word in allowed:
            return word
    return "none"


# ── Choosing ─────────────────────────────────────────────────────────────────


_GREETING = re.compile(
    r"^(hi|hii+|hello|hey|namaste|good (morning|afternoon|evening)|thanks|thank you|ok|okay|"
    r"help|what can you do)\b[\s!.?]*$"
)


def examples(tools: list[Tool]) -> list[str]:
    """Questions this person can ask here, one per tool they have."""
    keys = {tool.key for tool in tools}
    sample = [
        ("who_is_out", "Who is on leave this week?"),
        ("my_record", "How many leaves do I have left?"),
        ("search_people", "Find Rohan"),
        ("attendance_summary", "Attendance this month"),
        ("list_departments", "List departments"),
        ("hiring_overview", "Hiring overview"),
        ("hiring_jobs", "Show open jobs"),
        ("hiring_candidates", "Candidates offered"),
        ("hiring_interviews", "Interviews this week"),
        ("expense_claims", "My pending expense claims"),
        ("expense_summary", "Expense total this month"),
        ("helpdesk_tickets", "My open tickets"),
        ("helpdesk_assets", "What assets do I have?"),
        ("helpdesk_overview", "Helpdesk overview"),
        ("product_connections", f"Is {_HIRE} connected?"),
        ("open_product", f"Open {_HIRE}"),
    ]
    return [text for key, text in sample if key in keys]


def _help(tools: list[Tool], name: str, *, prefix: str = "") -> DirectPlan:
    if not tools:
        return DirectPlan(reply=f"{prefix} No lookups are switched on for you yet.".strip())
    tried = "\n".join(f"• {text}" for text in examples(tools)[:8])
    return DirectPlan(reply=f"{prefix} Here is what you can ask {name}:\n{tried}".strip())


def _unavailable(product: str) -> DirectPlan:
    return DirectPlan(
        reply=(
            f"Reading {product} is not available to you here: it is not linked to this "
            "workspace, you have no role in it, or an admin switched it off for Ask Mira."
        )
    )


def plan(text: str, tools: list[Tool], *, today: date, name: str = "Mira") -> DirectPlan:
    """Match the person's message to one of `tools`, or reply with what can be asked."""
    offered = {tool.key for tool in tools}
    words = " ".join(text.lower().replace("\u2019", "'").split())
    words = re.sub(r"\bwho's\b", "who is", words)
    start, end = dates(words, today)
    when = {"from": start, "to": end}

    def pick(key: str, reply: str, **inputs: Any) -> DirectPlan | None:
        return DirectPlan(reply=reply, tool=key, inputs=inputs) if key in offered else None

    if _GREETING.match(words):
        return _help(tools, name, prefix=f"Hi! I'm {name}.")

    chosen: DirectPlan | None = None

    # Whether the products are connected at all.
    if _has(words, "connect", "linked", "integrat", "set ?up", "configured") and _has(
        words,
        "an?y? ?time",
        "hire",
        "expense",
        "genie",
        "help ?desk",
        "products?\\b",
        "tools?\\b",
        "portals?",
    ):
        chosen = pick(
            "product_connections",
            f"Here is how {_HIRE}, {_EXPENSE} and {_HELPDESK} are connected for you.",
        )
        if chosen:
            return chosen

    # Raising a ticket happens on the Helpdesk page, where the form is.
    if re.match(
        r"^(please |can you |could you |i want to |i need to |i'd like to |help me |"
        r"how (do|can) i )?"
        r"(raise|create|open|log|file|submit|make|register) (a |an |one )?(new )?(it |helpdesk )?"
        # Singular: "open tickets" asks to see them.
        r"(ticket|request|complaint|issue)\b",
        words,
    ):
        if "helpdesk_tickets" in offered:
            return DirectPlan(
                reply=(
                    "Raise it under Helpdesk → My tickets → Raise a ticket. It goes straight to "
                    f"{_HELPDESK}, and you can follow the replies there or here."
                )
            )
        return _unavailable(_HELPDESK)

    # Opening a linked product — unless the words ask for its data.
    opening = re.match(r"^(please |can you )?(open|launch|go to|take me to)\b", words)
    if opening and not _has(
        words,
        r"jobs?\b",
        "candidate",
        "applicant",
        r"interviews\b",
        r"claims?\b",
        r"expenses\b",
        r"tickets\b",
        r"assets\b",
    ):
        if _has(words, "anytime", "hire", "interview tool", "hiring tool", "recruit"):
            chosen = pick("open_product", f"Opening {_HIRE}.", product="interview")
        elif _has(words, "expense"):
            chosen = pick("open_product", f"Opening {_EXPENSE}.", product="expense")
        elif _has(words, "help ?desk", "cgb", "it support", "it portal", "ticket"):
            chosen = pick("open_product", f"Opening {_HELPDESK}.", product="helpdesk")
        if chosen:
            return chosen

    # Expenses (ExpenseGenie).
    if _has(words, "expense", "claim", "reimburs", "spend", "spent", "bills?\\b"):
        scope = "mine"
        if _has(
            words,
            "to approve",
            "my approval",
            "for approval",
            "awaiting",
            "waiting",
            "approvals",
            "approve\\b",
            "team",
        ):
            scope = "approvals"
        elif _has(
            words,
            "company",
            "everyone",
            "everybody",
            "all employees",
            "whole",
            "organi[sz]ation",
            "org\\b",
            "all claims",
            "all expense",
        ):
            scope = "company"
        status = _status(words, product_tools.CLAIM_STATUSES)
        if scope == "approvals" and status == "none" and _has(words, "pending", "waiting"):
            status = "pending"
        if _has(
            words,
            "total",
            "how much",
            "summary",
            "sum",
            "overview",
            "spent",
            "spend",
            "unpaid",
            "breakdown",
            "by category",
        ):
            chosen = pick(
                "expense_summary",
                f"Here are the expense totals from {_EXPENSE}.",
                scope=scope,
                status=status,
                **when,
            )
        else:
            chosen = pick(
                "expense_claims",
                f"Here are the expense claims from {_EXPENSE}.",
                scope=scope,
                status=status,
                **when,
                query=_search_text(
                    words,
                    (
                        "pending",
                        "approved",
                        "rejected",
                        "draft",
                        "approval",
                        "approvals",
                        "approve",
                        "waiting",
                        "total",
                        "submitted",
                    ),
                ),
            )
        if chosen:
            return chosen
        return _unavailable(_EXPENSE)

    # IT tickets and assets (CGB Helpdesk).
    if _has(words, "tickets?\\b", "help ?desk", "it support", "it issues?", "support requests?"):
        if _has(words, "overview", "summary", "at a glance", "how many", "count", "stats"):
            chosen = pick(
                "helpdesk_overview", f"Here is the helpdesk at a glance from {_HELPDESK}."
            )
        else:
            scope = "mine"
            if _has(
                words, "assigned to me", "my queue", "on my plate", "i am working", "i'm working"
            ):
                scope = "assigned"
            elif _has(words, "unassigned", "queue", "not assigned", "nobody"):
                scope = "queue"
            elif _has(
                words,
                "company",
                "everyone",
                "everybody",
                "all tickets",
                "all the tickets",
                "whole",
                "organi[sz]ation",
                "org\\b",
            ):
                scope = "company"
            chosen = pick(
                "helpdesk_tickets",
                f"Here are the tickets from {_HELPDESK}.",
                scope=scope,
                status=_ticket_status(words),
                **when,
                query=_search_text(
                    words,
                    (
                        "open",
                        "closed",
                        "new",
                        "pending",
                        "resolved",
                        "done",
                        "fixed",
                        "raised",
                        "unresolved",
                        "active",
                        "about",
                        "regarding",
                    ),
                ),
            )
        if chosen:
            return chosen
        return _unavailable(_HELPDESK)
    if _has(
        words,
        "assets?\\b",
        "laptops?\\b",
        "devices?\\b",
        "equipment",
        "hardware",
        "monitors?\\b",
    ):
        company = _has(
            words, "register", "company", "all assets", "everyone", "whole", "inventory", "spare"
        )
        chosen = pick(
            "helpdesk_assets",
            f"Here are the assets from {_HELPDESK}.",
            scope="company" if company else "mine",
            status=_asset_status(words),
            query=_asset_search(words) if company else "",
        )
        if chosen:
            return chosen
        return _unavailable(_HELPDESK)

    # Hiring (AnytimeHire).
    asked_hiring = True
    if _has(words, "candidate", "applicant", "shortlist"):
        chosen = pick(
            "hiring_candidates",
            f"Here are the candidates from {_HIRE}.",
            status=_status(words, product_tools.CANDIDATE_STATUSES),
            query=_search_text(words, product_tools.CANDIDATE_STATUSES),
        )
    elif _has(words, "interview") and not _has(words, "interview tool"):
        chosen = pick(
            "hiring_interviews",
            f"Here are the interviews from {_HIRE}.",
            **when,
            query=_search_text(words, ("upcoming", "held", "coming", "up")),
        )
    elif _has(words, "jobs?\\b", "opening", "vacanc", "position", "requisition"):
        chosen = pick(
            "hiring_jobs",
            f"Here are the jobs from {_HIRE}.",
            status=_status(words, product_tools.JOB_STATUSES),
            query=_search_text(words, (*product_tools.JOB_STATUSES, "closed", "active")),
        )
    elif _has(words, "hiring", "recruit", "pipeline", "anytime"):
        chosen = pick("hiring_overview", f"Here is hiring at a glance from {_HIRE}.")
    else:
        asked_hiring = False
    if chosen:
        return chosen
    if asked_hiring:
        return _unavailable(_HIRE)

    # Policy and report questions need a model of their own.
    if _has(
        words,
        "polic",
        "rules?\\b",
        "entitle",
        "handbook",
        "holiday list",
        "holidays\\b",
        "how many (people|employees)",
        "headcount",
        "joiners",
        "attrition",
    ):
        return DirectPlan(
            reply=(
                "Questions like that are answered by AI, which is not switched on here. "
                "Ask HR and the handbook have the company's policies."
            )
        )

    # The person's own record.
    if _has(words, "payslip", "pay slip", "salary", "my pay\\b", "ctc"):
        chosen = pick("my_record", "Here is your latest payslip.", lookup="latest_payslip")
    elif _has(words, "my manager", "reporting manager", "report to", "my boss", "my lead"):
        chosen = pick("my_record", "Here is who your manager is.", lookup="my_manager")
    elif _has(
        words,
        "balance",
        "leaves? left",
        "leaves? do i have",
        "leaves? i have",
        "my leaves?\\b",
        "remaining leave",
    ):
        chosen = pick("my_record", "Here is your leave balance.", lookup="leave_balance")
    elif _has(words, "my attendance", "i present", "my presence"):
        chosen = pick(
            "my_record", "Here is your attendance this month.", lookup="attendance_this_month"
        )
    if chosen:
        return chosen

    # Who is out, and attendance.
    if _has(
        words,
        "on leave",
        "who is out",
        "out of office",
        "on holiday",
        "off today",
        "away",
        "leave\\b",
        "vacation",
        "wfh",
    ):
        chosen = pick(
            "who_is_out",
            f"Here is who is on approved leave {_span(when, 'today')}.",
            **when,
        )
    elif _has(words, "attendance", "present", "absent", "late\\b"):
        chosen = pick("attendance_summary", f"Here is attendance {_span(when, 'today')}.", **when)
    if chosen:
        return chosen

    # The organisation.
    if _has(words, "departments?\\b", "teams\\b"):
        chosen = pick("list_departments", "Here are the departments.")
    elif _has(words, "locations?\\b", "offices?\\b", "branch"):
        chosen = pick("list_locations", "Here are the office locations.")
    if chosen:
        return chosen

    # People: an explicit search, or a short message that reads like a name.
    query = _search_text(words, ("exited", "left", "former", "ex"))
    searching = _has(
        words,
        "find",
        "search",
        "who is",
        "contact",
        "email",
        "phone",
        "people",
        "employee",
        "colleague",
        "directory",
        "works? in",
        "team",
    )
    if query and (searching or len(words.split()) <= 3):
        chosen = pick(
            "search_people",
            f"Here is who matches “{query}”.",
            query=query,
            include_exited=_has(words, "exited", "left the company", "former", "ex-"),
        )
        if chosen:
            return chosen

    return _help(tools, name, prefix="I could not match that to something I can look up.")
