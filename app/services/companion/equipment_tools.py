"""Ask Mira: which new joiners still have no equipment assigned.

The joiners are AskMira's: people joining in the next 30 days or who joined in
the last 30 (or the window asked about). What they hold comes from the asset
register — CGB Helpdesk's when it is linked (kind ``holdings``, by work email),
else AskMira's own assignments. Only HR asks; names stay on the screen.

With ProcureFlow linked (and a role there), each joiner's purchase is shown too:
the requisition whose title names them — their employee code as a whole word,
or else their whole name ("Raj Kumar" is not "Raj Kumari", and a title naming
someone else's code is theirs) — and where it stands: approved, waiting for
approval, a draft not sent, or refused (which counts as no live requisition).
So "which joiners still need a laptop, and is the purchase approved?" is one
answer.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.assets import AssetAssignment
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department
from app.models.tenant import Tenant
from app.models.user import User
from app.services.products import data as products
from app.services.products.catalogue import PRODUCTS

__all__ = ["joiners_equipment"]

WINDOW = timedelta(days=30)
#: ProcureFlow requisition statuses that mean the purchase is approved.
APPROVED = frozenset({"APPROVED", "PARTIALLY_ISSUED", "FULLY_ISSUED", "CONVERTED", "CLOSED"})
#: Turned down or withdrawn: no live purchase.
REFUSED = frozenset({"REJECTED", "CANCELLED", "CANCELED", "WITHDRAWN"})
#: Saved but never sent for approval.
DRAFTS = frozenset({"DRAFT"})
#: Something that looks like an employee code in a title ("EMP0311", "ACM-42").
_CODE = re.compile(r"\b[A-Z]{2,6}-?\d{2,6}\b")


def standing(status: str) -> str:
    """approved, refused, draft or waiting."""
    if status in APPROVED:
        return "approved"
    if status in REFUSED:
        return "refused"
    if status in DRAFTS:
        return "draft"
    return "waiting"


def _label(status: str) -> str:
    return status.replace("_", " ").capitalize()


async def _requisitions(session: AsyncSession, user: User) -> tuple[list[dict[str, Any]], str]:
    """ProcureFlow's recent requisitions this person may see, and whose they are."""
    try:
        data = await products.ask(
            session, user, "procurement", "requisitions", {"scope": "company", "limit": 50}
        )
        return list(data.get("requisitions") or []), "the company's"
    except products.ProductUnavailableError:
        pass
    # Their role there shows them their own only.
    data = await products.ask(session, user, "procurement", "requisitions", {"limit": 50})
    return list(data.get("requisitions") or []), "your own"


def _purchase_for(
    name: str, code: str, requisitions: list[dict[str, Any]]
) -> tuple[dict | None, bool]:
    """The joiner's requisition, and whether it was found by name only (a guess).

    Their code as a whole word settles it; else their whole name, as words, in a
    title that names no other code. A live one (approved or waiting) comes before
    a draft or a refused one.
    """
    by_code: list[dict[str, Any]] = []
    by_name: list[dict[str, Any]] = []
    code_word = (
        re.compile(rf"(?<![\w-]){re.escape(code)}(?![\w-])", re.I) if len(code) >= 3 else None
    )
    words = [w for w in name.split() if w]
    name_words = (
        re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)", re.I)
        if len(" ".join(words)) >= 3
        else None
    )
    for req in requisitions:
        title = str(req.get("title") or "")
        if code_word is not None and code_word.search(title):
            by_code.append(req)
        elif name_words is not None and name_words.search(title):
            others = {c.upper() for c in _CODE.findall(title)} - {code.upper()}
            if not others:
                by_name.append(req)
    for found, guessed in ((by_code, False), (by_name, True)):
        if found:
            found.sort(
                key=lambda r: standing(str(r.get("status") or "")) not in ("approved", "waiting")
            )
            return found[0], guessed
    return None, False


def _day(value: Any, fallback: date) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return fallback


async def joiners_equipment(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start = _day(inputs.get("from") or "", today - WINDOW)
    end = _day(inputs.get("to") or "", today + WINDOW)
    if end < start:
        start, end = end, start
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    Employee.id,
                    Employee.display_name,
                    Employee.first_name,
                    Employee.last_name,
                    Employee.work_email,
                    Employee.employee_code,
                    Employee.date_of_joining,
                    Department.name.label("department"),
                )
                .outerjoin(Department, Department.id == Employee.department_id)
                .where(
                    Employee.date_of_joining >= start,
                    Employee.date_of_joining <= end,
                    Employee.status != EmploymentStatus.EXITED,
                )
                .order_by(Employee.date_of_joining, Employee.first_name),
                Employee,
                user,
            )
        )
    ).all()
    used = {"from": start.isoformat(), "to": end.isoformat()}
    span = f"{start:%d %b} to {end:%d %b %Y}"
    if not rows:
        return [
            {"type": "notice", "tone": "info", "text": f"Nobody joins or joined between {span}."}
        ], used

    tenant = await session.get(Tenant, user.tenant_id)
    source = "AskMira's asset register"
    held: dict[str, int] = {}
    if tenant and (tenant.linked_products or {}).get("helpdesk"):
        source = f"{PRODUCTS['helpdesk'].brand}'s asset register"
        emails = [str(r.work_email).lower() for r in rows if r.work_email][:200]
        answer = await products.ask(session, user, "helpdesk", "holdings", {"emails": emails})
        held = {
            str(h.get("email")).lower(): int(h.get("assets") or 0)
            for h in answer.get("holdings") or []
            if isinstance(h, dict)
        }
        count_of = {r.id: held.get(str(r.work_email).lower(), 0) for r in rows}
    else:
        counted = (
            await session.execute(
                tenant_scoped(
                    select(AssetAssignment.employee_id, func.count())
                    .where(
                        AssetAssignment.employee_id.in_([r.id for r in rows]),
                        AssetAssignment.returned_at.is_(None),
                    )
                    .group_by(AssetAssignment.employee_id),
                    AssetAssignment,
                    user,
                )
            )
        ).all()
        by_id = {emp: int(n) for emp, n in counted}
        count_of = {r.id: by_id.get(r.id, 0) for r in rows}

    # Their purchases in ProcureFlow, where it is linked for this person.
    procurement: list[dict[str, Any]] | None = None
    whose = ""
    problem = ""
    if (
        tenant
        and (tenant.linked_products or {}).get("procurement")
        and await products.role_in(session, user, "procurement")
    ):
        try:
            procurement, whose = await _requisitions(session, user)
        except products.ProductUnavailableError as exc:
            problem = str(exc)

    table_rows = []
    waiting = 0
    approved = pending = drafts = refused = unbought = 0
    for r in rows:
        name = r.display_name or " ".join(p for p in (r.first_name, r.last_name) if p)
        n = count_of[r.id]
        waiting += n == 0
        row = [
            name,
            r.department or "—",
            f"{r.date_of_joining:%d %b %Y}",
            str(n),
            "Needs equipment" if n == 0 else "Has equipment",
        ]
        if procurement is not None:
            req, guessed = _purchase_for(name, str(r.employee_code or ""), procurement)
            state = standing(str(req.get("status") or "")) if req is not None else "none"
            if req is None:
                row.append("No requisition found")
            else:
                status = str(req.get("status") or "")
                said = {
                    "approved": " (approved)",
                    "waiting": " (waiting for approval)",
                    "draft": " (not sent for approval)",
                    "refused": " (no live purchase)",
                }[state]
                row.append(
                    f"{req.get('number')} · {_label(status)}{said}"
                    + (" · matched by name" if guessed else "")
                )
            approved += state == "approved"
            pending += state == "waiting"
            drafts += state == "draft"
            refused += state == "refused"
            unbought += n == 0 and state in ("none", "refused")
        table_rows.append(row)
    table_rows.sort(key=lambda row: (row[4] != "Needs equipment", row[2]))
    columns = ["Joiner", "Department", "Joins on", "Assets held", "Status"]
    caption = f"Joining {span} · {source}"
    view = (
        f"{len(rows)} joiner(s) between {span}; {waiting} have no equipment assigned in {source}."
    )
    if procurement is not None:
        columns.append("Purchase (ProcureFlow)")
        caption += (
            f" · purchases from {whose} ProcureFlow requisitions whose title names the joiner"
        )
        view += (
            f" ProcureFlow ({whose} requisitions): {approved} joiner(s) have an approved "
            f"purchase, {pending} wait for approval, {drafts} are drafts not sent, {refused} "
            f"were refused or withdrawn; {unbought} without equipment have no live "
            "requisition."
        )
    elif problem:
        caption += f" · ProcureFlow could not be read: {problem}"
    cards: list[dict[str, Any]] = [
        {
            "type": "table",
            "title": "Joiners and their equipment",
            "caption": caption,
            "columns": columns,
            "rows": table_rows,
            # Who joins is a name list: the model reads the counts only.
            "model_view": view + " Their names are on the person's screen.",
        }
    ]
    return cards, used
