"""Ask Mira's tools that read a linked product: AnytimeHire for hiring and
ExpenseGenie for expense claims.

Each asks the product as the person (products.data.ask): only the caller's own
workspace, and only what their role in that product shows them when they open
it. The answer becomes cards for the screen and is never given to the model.

Two house rules hold here as everywhere in AskMira:

* No AI score without its signals and a confidence (CLAUDE.md rule 7). The
  interview tool keeps no confidence, so no score, recommendation or fit score
  is asked for or shown, and the product does not send them either.
* Proctoring outcomes are observations for a person to review, never findings
  (FS-HIR-02). A status that reads like a judgement is shown as "Ended early".

Money arrives as decimal strings and is shown with Indian grouping. It never
becomes a float.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.documents.indian_numbers import group_indian
from app.services.hiring.signals import JUDGEMENT_WORDS
from app.services.products import data as products
from app.services.products.catalogue import PRODUCTS

__all__ = ["CLAIM_STATUSES", "SCOPES", "STATUS_CHOICES", "run"]

IST = ZoneInfo("Asia/Kolkata")
MAX_DAYS = 93
#: Rows asked for in a list. The caption says when there are more.
LIMIT = 25

JOB_STATUSES = ("open", "draft", "inactive", "finished")
CANDIDATE_STATUSES = ("applied", "interview", "offered", "selected", "hired", "rejected")
CLAIM_STATUSES = ("pending", "approved", "rejected", "draft")
#: Every status the model may set, for whichever tool it picks.
STATUS_CHOICES = tuple(dict.fromkeys(("none", *JOB_STATUSES, *CANDIDATE_STATUSES, *CLAIM_STATUSES)))
SCOPES = ("mine", "approvals", "company")

_CANDIDATE_ROWS = (
    ("Applied", "Candidates who applied"),
    ("Interview", "Candidates in interviews"),
    ("Offered", "Candidates offered"),
    ("Selected", "Candidates selected"),
    ("Hired", "Candidates hired"),
    ("Rejected", "Candidates rejected"),
)
_CLAIM_ORDER = {"Pending": 0, "Approved": 1, "Rejected": 2, "Draft": 3}
_SCOPE_LABEL = {
    "mine": "Your claims",
    "approvals": "Claims for your approval",
    "company": "Whole company",
}


# ── Reading what the model set ──────────────────────────────────────────────


def _status(inputs: dict[str, Any], allowed: tuple[str, ...]) -> str | None:
    value = str(inputs.get("status") or "none").strip().lower()
    return value if value in allowed else None


def _scope(inputs: dict[str, Any]) -> str:
    value = str(inputs.get("scope") or "none").strip().lower()
    return value if value in SCOPES else "mine"


def _search(inputs: dict[str, Any]) -> str:
    return str(inputs.get("query") or "").strip()[:100]


def _day_of(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError("Dates must look like 2026-10-20.") from None


def _days(inputs: dict[str, Any], default: tuple[date, date] | None) -> tuple[date, date] | None:
    """The asked range of days; one end alone means that one day; none means `default`."""
    first = str(inputs.get("from") or "").strip()
    last = str(inputs.get("to") or "").strip()
    if not first and not last:
        return default
    start = _day_of(first or last)
    end = _day_of(last or first)
    if end < start:
        raise ValueError("The last day of the range comes before the first.")
    if (end - start).days >= MAX_DAYS:
        raise ValueError(f"Ask about at most {MAX_DAYS} days at a time.")
    return start, end


def _instants(days: tuple[date, date]) -> tuple[str, str]:
    """Midnight in India starting the first day, and midnight after the last, in UTC."""
    start, end = days
    return (
        datetime.combine(start, time.min, IST).astimezone(UTC).isoformat(),
        datetime.combine(end + timedelta(days=1), time.min, IST).astimezone(UTC).isoformat(),
    )


def _span(days: tuple[date, date]) -> str:
    start, end = days
    return start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"


# ── Showing what the product sent ───────────────────────────────────────────


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value: Any, limit: int = 120) -> str:
    return "" if value is None else str(value).strip()[:limit]


def _neutral(value: Any) -> str:
    """A status as an observation. One that reads like a judgement is not repeated."""
    text = _text(value)
    if any(word in text.lower() for word in JUDGEMENT_WORDS):
        return "Ended early"
    return text


def _money(amount: Any, currency: Any) -> str:
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return ""
    code = (_text(currency, 3) or "INR").upper()
    return f"₹{group_indian(value)}" if code == "INR" else f"{code} {value:,.2f}"


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(IST)


def _when(value: Any) -> str:
    """A time in India: "Wed 22 Oct 2026, 3:30 pm"."""
    moment = _moment(value)
    if moment is None:
        return ""
    hour = moment.hour % 12 or 12
    meridiem = "am" if moment.hour < 12 else "pm"
    return f"{moment:%a} {moment.day} {moment:%b %Y}, {hour}:{moment:%M} {meridiem}"


def _day(value: Any) -> str:
    moment = _moment(value)
    return "" if moment is None else f"{moment.day} {moment:%b %Y}"


def _items(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _counts(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _caption(shown: int, total: int, noun: str, *parts: str) -> str:
    total = max(total, shown)
    plural = "" if total == 1 else "s"
    count = f"{shown} {noun}{plural}" if shown == total else f"First {shown} of {total} {noun}s"
    return " · ".join([*(part for part in parts if part), count])


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def _table(
    product_key: str,
    title: str,
    columns: list[str],
    rows: list[list[Any]],
    *,
    caption: str,
    empty: str,
) -> dict[str, Any]:
    if not rows:
        return _notice(empty)
    product = PRODUCTS[product_key]
    return {
        "type": "table",
        "title": title,
        "caption": caption,
        "columns": columns,
        "rows": rows,
        # Where the rows came from, and where to see them in full.
        "source": product.brand,
        "open": f"/launch/{product.key}",
    }


# ── Hiring (AnytimeHire) ────────────────────────────────────────────────────

Result = tuple[list[dict[str, Any]], dict[str, Any]]


async def _hiring_overview(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    data = await products.ask(session, user, "interview", "overview", {})
    jobs = _counts(data, "jobs")
    candidates = _counts(data, "candidates")
    rows: list[list[Any]] = [
        ["Open jobs", _int(jobs.get("Open"))],
        ["Open positions", _int(data.get("open_positions"))],
        ["Draft jobs", _int(jobs.get("Draft"))],
        ["Inactive jobs", _int(jobs.get("Inactive"))],
        ["Finished jobs", _int(jobs.get("Finished"))],
        *([label, _int(candidates.get(status))] for status, label in _CANDIDATE_ROWS),
        ["Interviews coming up", _int(data.get("upcoming_interviews"))],
        ["Interviews held in the last 30 days", _int(data.get("completed_interviews_30d"))],
    ]
    card = _table(
        "interview", "Hiring at a glance", ["Measure", "Count"], rows, caption="As of now", empty=""
    )
    return [card], {}


async def _hiring_jobs(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    status = _status(inputs, JOB_STATUSES)
    search = _search(inputs)
    used = {"status": status or "none", "query": search}
    data = await products.ask(
        session, user, "interview", "jobs", {"status": status, "query": search, "limit": LIMIT}
    )
    rows = [
        [
            _text(job.get("title")),
            _text(job.get("department")),
            _text(job.get("location")),
            _text(job.get("status")),
            _int(job.get("openings")),
            _int(job.get("applicants")),
            _int(job.get("interviews_held")),
            _text(job.get("deadline")),
        ]
        for job in _items(data, "jobs")
    ]
    caption = _caption(
        len(rows),
        _int(data.get("total")),
        "job",
        status.capitalize() if status else "",
        f"matching “{search}”" if search else "",
    )
    card = _table(
        "interview",
        "Jobs",
        [
            "Job",
            "Department",
            "Location",
            "Status",
            "Openings",
            "Applicants",
            "Interviews held",
            "Deadline",
        ],
        rows,
        caption=caption,
        empty="No jobs match that in AnytimeHire.",
    )
    return [card], used


async def _hiring_candidates(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    status = _status(inputs, CANDIDATE_STATUSES)
    search = _search(inputs)
    used = {"status": status or "none", "query": search}
    data = await products.ask(
        session,
        user,
        "interview",
        "candidates",
        {"status": status, "query": search, "limit": LIMIT},
    )
    rows = [
        [
            _text(person.get("name")),
            _text(person.get("job")),
            _text(person.get("status")),
            _neutral(person.get("stage")),
            _neutral(person.get("interview")),
            _when(person.get("interview_at")),
            _day(person.get("applied_at")),
        ]
        for person in _items(data, "candidates")
    ]
    caption = _caption(
        len(rows),
        _int(data.get("total")),
        "candidate",
        status.capitalize() if status else "",
        f"matching “{search}”" if search else "",
    )
    card = _table(
        "interview",
        "Candidates",
        ["Candidate", "Job", "Status", "Stage", "Interview", "Interview time", "Applied"],
        rows,
        caption=caption,
        empty="No candidates match that in AnytimeHire.",
    )
    return [card], used


async def _hiring_interviews(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    days = _days(inputs, (today, today + timedelta(days=13)))
    assert days is not None
    search = _search(inputs)
    used = {"from": days[0].isoformat(), "to": days[1].isoformat(), "query": search}
    start, end = _instants(days)
    data = await products.ask(
        session,
        user,
        "interview",
        "interviews",
        {"start": start, "end": end, "query": search, "limit": 50},
    )
    rows = [
        [
            _when(item.get("at")),
            _text(item.get("candidate")),
            _text(item.get("job")),
            _neutral(item.get("status")),
        ]
        for item in _items(data, "interviews")
    ]
    caption = _caption(
        len(rows),
        _int(data.get("total")),
        "interview",
        _span(days),
        f"matching “{search}”" if search else "",
    )
    card = _table(
        "interview",
        "Interviews",
        ["When (IST)", "Candidate", "Job", "Status"],
        rows,
        caption=caption,
        empty=f"No interviews in AnytimeHire for {_span(days)}.",
    )
    return [card], used


# ── Expense claims (ExpenseGenie) ───────────────────────────────────────────


async def _company_refused(session: AsyncSession, user: User, scope: str) -> bool:
    """Only an expense admin (HR) sees everyone's claims; the product refuses it too."""
    return scope == "company" and await products.role_in(session, user, "expense") == "employee"


_COMPANY_REFUSED = (
    "Only HR can see the whole company's claims. I can show your own claims, "
    "or the ones waiting for your approval."
)


async def _expense_claims(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    scope = _scope(inputs)
    if await _company_refused(session, user, scope):
        return [_notice(_COMPANY_REFUSED)], {"scope": scope}
    # "Waiting for your approval" means pending, unless another status was asked for.
    status = _status(inputs, CLAIM_STATUSES) or ("pending" if scope == "approvals" else None)
    days = _days(inputs, None)
    search = _search(inputs)
    used: dict[str, Any] = {"scope": scope, "status": status or "none", "query": search}
    params: dict[str, Any] = {"scope": scope, "status": status, "query": search, "limit": LIMIT}
    if days:
        params["from"], params["to"] = _instants(days)
        used |= {"from": days[0].isoformat(), "to": days[1].isoformat()}
    data = await products.ask(session, user, "expense", "claims", params)

    mine = scope == "mine"
    rows = []
    for claim in _items(data, "claims"):
        row = [
            _day(claim.get("submitted_at")),
            _text(claim.get("title")),
            _text(claim.get("category")),
            _money(claim.get("amount"), claim.get("currency")),
            _text(claim.get("status")),
            _text(claim.get("payment")),
        ]
        if not mine:
            row.insert(2, _text(claim.get("submitter")))
        rows.append(row)
    columns = ["Submitted", "Claim", "Category", "Amount", "Status", "Payment"]
    if not mine:
        columns.insert(2, "By")
    caption = _caption(
        len(rows),
        _int(data.get("total")),
        "claim",
        status.capitalize() if status else "",
        _span(days) if days else "",
        f"matching “{search}”" if search else "",
    )
    card = _table(
        "expense",
        _SCOPE_LABEL[scope],
        columns,
        rows,
        caption=caption,
        empty="No expense claims match that in ExpenseGenie.",
    )
    return [card], used


async def _expense_summary(
    session: AsyncSession, user: User, inputs: dict[str, Any], today: date
) -> Result:
    scope = _scope(inputs)
    if await _company_refused(session, user, scope):
        return [_notice(_COMPANY_REFUSED)], {"scope": scope}
    days = _days(inputs, (today.replace(day=1), today))
    assert days is not None
    status = _status(inputs, CLAIM_STATUSES)
    used = {
        "scope": scope,
        "status": status or "none",
        "from": days[0].isoformat(),
        "to": days[1].isoformat(),
    }
    start, end = _instants(days)
    data = await products.ask(
        session,
        user,
        "expense",
        "summary",
        {"scope": scope, "status": status, "from": start, "to": end},
    )

    by_status = sorted(
        _items(data, "by_status"),
        key=lambda row: (_CLAIM_ORDER.get(_text(row.get("status")), 9), _text(row.get("currency"))),
    )
    status_rows = [
        [
            _text(row.get("status")),
            _int(row.get("count")),
            _money(row.get("amount"), row.get("currency")),
        ]
        for row in by_status
    ]
    span = _span(days)
    cards = [
        _table(
            "expense",
            "Expense claims by status",
            ["Status", "Claims", "Amount"],
            status_rows,
            caption=f"{_SCOPE_LABEL[scope]} · submitted {span}",
            empty=f"No expense claims were submitted {span}.",
        )
    ]
    if status_rows:
        category_rows = [
            [
                _text(row.get("category")),
                _int(row.get("count")),
                _money(row.get("amount"), row.get("currency")),
            ]
            for row in _items(data, "by_category")
        ]
        if category_rows:
            cards.append(
                _table(
                    "expense",
                    "By category",
                    ["Category", "Claims", "Amount"],
                    category_rows,
                    caption="Largest first, up to 10",
                    empty="",
                )
            )
        unpaid = _items(data, "approved_unpaid")
        count = sum(_int(row.get("count")) for row in unpaid)
        if count:
            amounts = ", ".join(_money(row.get("amount"), row.get("currency")) for row in unpaid)
            plural = "claim" if count == 1 else "claims"
            cards.append(_notice(f"Approved but not yet paid: {count} {plural}, {amounts}."))
    return cards, used


_RUNNERS = {
    "hiring_overview": _hiring_overview,
    "hiring_jobs": _hiring_jobs,
    "hiring_candidates": _hiring_candidates,
    "hiring_interviews": _hiring_interviews,
    "expense_claims": _expense_claims,
    "expense_summary": _expense_summary,
}


async def run(
    session: AsyncSession, user: User, key: str, inputs: dict[str, Any], *, today: date
) -> Result:
    """Run one product tool as `user`. Returns the cards and the inputs it actually used."""
    try:
        return await _RUNNERS[key](session, user, inputs, today)
    except products.ProductUnavailableError as exc:
        return [_notice(str(exc), "warning")], {}
