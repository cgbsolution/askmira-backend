"""Ask Mira's toolbox: what it can do, for whom, and what each result looks like.

A tool is offered to a person only when all three agree: the workspace's admin
switched its group on, the person's role holds its scope (the same scopes the
MCP server grants, `oauth.allowed_scopes`), and the AI feature it relies on is
on. A tool that is not offered cannot be called: the model's choice is checked
against the same list.

Every tool runs here, as the person, through code that already enforces their
access — the MCP gateway's read tools, the policy assistant, the report
compiler. Each returns cards for the screen. Nothing a tool returns is given to
the model.

A tool that reads a linked product (AnytimeHire, ExpenseGenie) is offered only
when the workspace has that product linked and the person has a role in it;
it asks the product as them (product_tools.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.user import User
from app.services import mcp, oauth
from app.services.ai import analytics, assistant, governance
from app.services.companion import product_tools
from app.services.companion.config import CompanionConfig, group_on
from app.services.products import data as products_data
from app.services.products.catalogue import PRODUCTS

_HIRE = PRODUCTS["interview"].brand
_EXPENSE = PRODUCTS["expense"].brand

__all__ = ["INPUTS", "TOOLS", "Tool", "offered", "run"]


@dataclass(frozen=True, slots=True)
class Tool:
    key: str
    #: The admin's switch that covers it (config.TOOL_GROUPS).
    group: str
    #: What the person sees on the result.
    title: str
    #: What the model reads when choosing.
    description: str
    #: The plan fields this tool reads.
    inputs: tuple[str, ...] = ()
    #: The scope the person's role must hold (oauth.allowed_scopes), if any.
    scope: str | None = None
    #: The AI feature that must be on (governance.FEATURES), if any.
    feature: str | None = None
    #: The linked product it reads (catalogue.PRODUCTS), if any.
    product: str | None = None


TOOLS: dict[str, Tool] = {
    tool.key: tool
    for tool in (
        Tool(
            key="policy_question",
            group="policy",
            title="Company policy",
            description=(
                "Answers a question about the company's policies, leave rules or holidays from "
                "what HR published, with sources. Put the question, rewritten to stand on its "
                "own, in `question`."
            ),
            inputs=("question",),
            feature="assistant",
        ),
        Tool(
            key="my_record",
            group="own_record",
            title="Your record",
            description=(
                "The asker's own record: leave balance, latest payslip, attendance this month, "
                "or who their manager is. Set `lookup`."
            ),
            inputs=("lookup",),
        ),
        Tool(
            key="search_people",
            group="people",
            title="People",
            description=(
                "Finds colleagues in the directory by name, employee code, title or email, and "
                "shows their title, department, manager and work email. Put the search text in "
                "`query`; set `include_exited` only when asked about people who left."
            ),
            inputs=("query", "include_exited"),
            scope="employees:read",
        ),
        Tool(
            key="who_is_out",
            group="who_is_out",
            title="Who is on leave",
            description=(
                "Approved leave overlapping a date range of at most 93 days: who, which leave "
                "type and which days, never the reason. Set `from` and `to`."
            ),
            inputs=("from", "to"),
            scope="leave:read",
        ),
        Tool(
            key="attendance_summary",
            group="attendance",
            title="Attendance",
            description=(
                "Per person over a date range of at most 93 days: days present, absent, on "
                "leave, half days and late arrivals. Set `from` and `to`."
            ),
            inputs=("from", "to"),
            scope="attendance:read",
        ),
        Tool(
            key="list_departments",
            group="org",
            title="Departments",
            description="Every department, with its code and the department it sits under.",
            scope="org:read",
        ),
        Tool(
            key="list_locations",
            group="org",
            title="Locations",
            description="Every office location, with its city and state.",
            scope="org:read",
        ),
        Tool(
            key="report_question",
            group="reports",
            title="Report",
            description=(
                "Answers a counting or reporting question about the workforce — headcount, "
                "joiners, exits, by department, location or month — with the report builder, "
                "showing only what the asker's role may see. Put the question, rewritten to "
                "stand on its own, in `question`."
            ),
            inputs=("question",),
            feature="analytics_questions",
        ),
        Tool(
            key="hiring_overview",
            group="hiring_data",
            product="interview",
            title="Hiring at a glance",
            description=(
                f"Hiring at a glance from {_HIRE}, the company's interview tool: jobs by "
                "status, open positions, candidates by status, interviews coming up and "
                "interviews held in the last 30 days."
            ),
        ),
        Tool(
            key="hiring_jobs",
            group="hiring_data",
            product="interview",
            title="Jobs",
            description=(
                f"Jobs in {_HIRE} with their openings, applicants and interviews held. Set "
                "`status` (open, draft, inactive or finished) to filter, and `query` to search "
                "job titles and departments."
            ),
            inputs=("status", "query"),
        ),
        Tool(
            key="hiring_candidates",
            group="hiring_data",
            product="interview",
            title="Candidates",
            description=(
                f"Candidates in {_HIRE}: their job, status, stage and interview. Set `query` "
                "to a candidate's name or a job title, and `status` (applied, interview, "
                "offered, selected, hired or rejected) to filter. It never shows AI scores."
            ),
            inputs=("query", "status"),
        ),
        Tool(
            key="hiring_interviews",
            group="hiring_data",
            product="interview",
            title="Interviews",
            description=(
                f"Interviews in {_HIRE} scheduled or held between `from` and `to` (at most 93 "
                "days; both empty means the next 14 days). Set `query` to narrow to a job "
                "title or a candidate's name."
            ),
            inputs=("from", "to", "query"),
        ),
        Tool(
            key="expense_claims",
            group="expense_data",
            product="expense",
            title="Expense claims",
            description=(
                f"Expense claims in {_EXPENSE}, the company's expense tool. Set `scope`: mine "
                "(the asker's own claims), approvals (claims waiting for the asker's approval) "
                "or company (everyone's; HR only). Optionally set `status` (pending, approved, "
                "rejected or draft), `from` and `to` (submission dates) and `query` (a title, "
                "category or person)."
            ),
            inputs=("scope", "status", "from", "to", "query"),
        ),
        Tool(
            key="expense_summary",
            group="expense_data",
            product="expense",
            title="Expense totals",
            description=(
                f"Expense totals in {_EXPENSE} by status and category, and approved claims not "
                "yet paid. Set `scope` (mine, approvals, or company for HR only), optionally "
                "`status`, and `from` and `to`; both empty means this month so far."
            ),
            inputs=("scope", "status", "from", "to"),
        ),
        Tool(
            key="open_product",
            group="products",
            title="Open a linked product",
            description=(
                "Opens a product this workspace uses instead of an AskMira module: "
                "'interview' for hiring and interviews, 'expense' for expense claims. Set "
                "`product`."
            ),
            inputs=("product",),
        ),
    )
}

#: Every input a tool may read, as the model fills it. One flat object, so the
#: answer's shape never depends on the tool chosen; unused fields stay empty.
INPUTS: dict[str, dict[str, Any]] = {
    "question": {"type": "string", "description": "A standalone question, or empty."},
    "lookup": {"type": "string", "enum": list(assistant.LOOKUPS)},
    "query": {"type": "string", "description": "Search text, or empty."},
    "include_exited": {"type": "boolean"},
    "from": {"type": "string", "description": "First day, YYYY-MM-DD, or empty."},
    "to": {"type": "string", "description": "Last day, YYYY-MM-DD, or empty."},
    "product": {"type": "string", "enum": ["none", "interview", "expense"]},
    "status": {"type": "string", "enum": list(product_tools.STATUS_CHOICES)},
    "scope": {"type": "string", "enum": ["none", *product_tools.SCOPES]},
}


def _launchable(user: User, linked: dict[str, bool]) -> list[str]:
    is_manager = bool(getattr(user, "has_reports", False))
    return [
        key
        for key in linked
        if key in PRODUCTS and PRODUCTS[key].role_for(user.role, is_manager=is_manager)
    ]


async def offered(
    session: AsyncSession, user: User, config: CompanionConfig, *, ai: bool = True
) -> list[Tool]:
    """The tools this person may use here, now. Without `ai`, none that needs a model."""
    settings = await governance.get_settings(session, user)
    scopes = oauth.allowed_scopes(user)
    tenant = await session.get(Tenant, user.tenant_id)
    launchable = _launchable(user, tenant.linked_products if tenant else {})
    product_roles = {key: await products_data.role_in(session, user, key) for key in PRODUCTS}
    out: list[Tool] = []
    for tool in TOOLS.values():
        if not group_on(config, tool.group):
            continue
        if tool.scope and tool.scope not in scopes:
            continue
        if tool.feature and not (ai and getattr(settings, tool.feature, False)):
            continue
        if tool.product and not product_roles.get(tool.product):
            continue
        if tool.key == "open_product" and not launchable:
            continue
        out.append(tool)
    return out


# ── Cards ────────────────────────────────────────────────────────────────────


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def _table(
    title: str, columns: list[str], rows: list[list[Any]], *, caption: str, empty: str
) -> dict[str, Any]:
    if not rows:
        return _notice(empty)
    return {"type": "table", "title": title, "caption": caption, "columns": columns, "rows": rows}


def _range(inputs: dict[str, Any], today: date) -> dict[str, str]:
    """Both ends of a range; an empty end takes the other's value, both empty mean today."""
    start = str(inputs.get("from") or "").strip()
    end = str(inputs.get("to") or "").strip()
    start = start or end or today.isoformat()
    end = end or start
    return {"from": start, "to": end}


async def run(
    session: AsyncSession, user: User, tool: Tool, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one tool as `user`. Returns the cards and the inputs it actually used."""
    try:
        return await _run(session, user, tool, inputs, today=today)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "That could not be done."
        return [_notice(detail, "warning")], {}
    except (mcp.ToolError, ValueError) as exc:
        return [_notice(str(exc), "warning")], {}


async def _run(
    session: AsyncSession, user: User, tool: Tool, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if tool.product:
        return await product_tools.run(session, user, tool.key, inputs, today=today)

    if tool.key in ("policy_question", "report_question"):
        question = str(inputs.get("question") or "").strip()
        if len(question) < 3:
            return [_notice("I need the question itself to look that up.")], {}
        if tool.key == "policy_question":
            answer = await assistant.ask(session, user, question)
            return [{"type": "policy", **answer}], {"question": question}
        result = await analytics.ask(session, user, question)
        return [{"type": "report", **result}], {"question": question}

    if tool.key == "my_record":
        lookup = str(inputs.get("lookup") or "none")
        text, _, href = await assistant.lookup_own_record(session, user, lookup)
        return [{"type": "own_record", "text": text, "open": href}], {"lookup": lookup}

    if tool.key == "search_people":
        used = {
            "query": str(inputs.get("query") or "").strip(),
            "include_exited": bool(inputs.get("include_exited")),
        }
        out = await mcp.run_tool(session, user, "search_employees", {**used, "limit": 25})
        rows = [
            [p["name"], p["title"], p["department"], p["manager"], p["work_email"], p["status"]]
            for p in out["people"]
        ]
        caption = f"Matching “{used['query']}”" if used["query"] else "Everyone"
        return [
            _table(
                "People",
                ["Name", "Title", "Department", "Manager", "Work email", "Status"],
                rows,
                caption=f"{caption} · first 25 shown",
                empty="Nobody in the directory matches that.",
            )
        ], used

    if tool.key in ("who_is_out", "attendance_summary"):
        used = _range(inputs, today)
        out = await mcp.run_tool(session, user, tool.key, used)
        span = used["from"] if used["from"] == used["to"] else f"{used['from']} to {used['to']}"
        if tool.key == "who_is_out":
            rows = [
                [
                    item["name"],
                    item["leave_type"],
                    item["start_date"],
                    item["end_date"],
                    "Yes" if item["half_day"] else "No",
                ]
                for item in out["leave"]
            ]
            return [
                _table(
                    "On approved leave",
                    ["Name", "Leave", "From", "To", "Half day"],
                    rows,
                    caption=span,
                    empty=f"Nobody is on approved leave on {span}.",
                )
            ], used
        rows = [
            [p["name"], p["present"], p["absent"], p["on_leave"], p["half_day"], p["late"]]
            for p in out["people"]
        ]
        return [
            _table(
                "Attendance",
                ["Name", "Present", "Absent", "On leave", "Half days", "Late"],
                rows,
                caption=f"{span} · {out['days_in_range']} day(s)",
                empty=f"No attendance has been worked out for {span} yet.",
            )
        ], used

    if tool.key == "list_departments":
        out = await mcp.run_tool(session, user, "list_departments", {})
        rows = [[d["name"], d["code"], d["parent"]] for d in out["departments"]]
        return [
            _table(
                "Departments",
                ["Department", "Code", "Part of"],
                rows,
                caption=f"{len(rows)} department(s)",
                empty="No departments are set up yet.",
            )
        ], {}

    if tool.key == "list_locations":
        out = await mcp.run_tool(session, user, "list_locations", {})
        rows = [[loc["name"], loc["city"], loc["state_code"]] for loc in out["locations"]]
        return [
            _table(
                "Locations",
                ["Location", "City", "State"],
                rows,
                caption=f"{len(rows)} location(s)",
                empty="No office locations are set up yet.",
            )
        ], {}

    if tool.key == "open_product":
        product = str(inputs.get("product") or "none")
        tenant = await session.get(Tenant, user.tenant_id)
        if product not in _launchable(user, tenant.linked_products if tenant else {}):
            return [_notice("That product is not linked to your workspace for you.")], {}
        name = PRODUCTS[product].name
        return [
            {"type": "launch", "product": product, "name": name, "href": f"/launch/{product}"}
        ], {"product": product}

    return [_notice("That is not something I can do here.")], {}
