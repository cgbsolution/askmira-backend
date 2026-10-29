"""Ask Mira's toolbox: what it can do, for whom, and what each result looks like.

A tool is offered to a person only when all three agree: the workspace's admin
switched its group on, the person's role holds its scope (the same scopes the
MCP server grants, `oauth.allowed_scopes`), and the AI feature it relies on is
on. A tool that is not offered cannot be called: the model's choice is checked
against the same list.

Every tool runs here, as the person, through code that already enforces their
access — the MCP gateway's read tools, the policy assistant, the report
compiler, payroll's payslips. Each returns cards for the screen; the model
reads a trimmed view of the same cards (views.py), within the DPDP categories
the tool declares in `reads`.

An action tool (`kind="action"`) never changes anything itself: it checks what
the module would check and saves an offer the person confirms (actions.py).

A tool that reads a linked product (AnytimeHire, ExpenseGenie, CGB Helpdesk) is offered only
when the workspace has that product linked and the person has a role in it;
it asks the product as them (product_tools.py).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.tenant import Tenant
from app.models.user import User
from app.services import mcp, oauth
from app.services.ai import analytics, assistant, governance
from app.services.companion import (
    actions,
    analysis_tools,
    hr_tools,
    people_tools,
    product_tools,
)
from app.services.companion.config import CompanionConfig, group_on
from app.services.products import data as products_data
from app.services.products.catalogue import PRODUCTS

_HIRE = PRODUCTS["interview"].brand
_EXPENSE = PRODUCTS["expense"].brand
_HELPDESK = PRODUCTS["helpdesk"].brand

__all__ = ["INPUTS", "TOOLS", "Tool", "coerce", "offered", "run", "tool_schema"]


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
    #: About other people or the company: admins and sub-admins only. Everybody
    #: else is answered about their own record and nothing more.
    hr_only: bool = False
    #: The DPDP categories the model reads in this tool's result (views.py).
    reads: tuple[str, ...] = ("organisational",)
    #: "read" looks something up; "action" offers to do something, on the person's say.
    kind: str = "read"
    #: Needs a model to hold the conversation: never offered without AI.
    needs_ai: bool = False


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
                "The asker's own record: leave balance, latest payslip, their own current "
                "salary (my_salary), their own profile (my_profile), attendance this month, or "
                "who their manager is. Set `lookup`. Only ever the asker's own."
            ),
            inputs=("lookup",),
        ),
        Tool(
            key="search_people",
            hr_only=True,
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
            hr_only=True,
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
            hr_only=True,
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
            hr_only=True,
            group="org",
            title="Departments",
            description="Every department, with its code and the department it sits under.",
            scope="org:read",
        ),
        Tool(
            key="list_locations",
            hr_only=True,
            group="org",
            title="Locations",
            description="Every office location, with its city and state.",
            scope="org:read",
        ),
        Tool(
            key="report_question",
            hr_only=True,
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
            key="employee_record",
            group="person_records",
            title="Employee record",
            description=(
                "For admins and sub-admins: one employee's current salary (monthly and annual "
                "gross and CTC, with wage types), leave balance, attendance this month, manager "
                "or profile. Put the person's name or employee code in `query` and set "
                "`person_lookup`. Leave `employee_id` empty: when several people match, AskMira "
                "shows the person a list to pick from. Never use it for the asker's own record."
            ),
            inputs=("query", "employee_id", "person_lookup"),
            hr_only=True,
        ),
        Tool(
            key="headcount",
            group="person_records",
            title="Headcount",
            description=(
                "For admins and sub-admins: how many people the company employs now, by status "
                "(active, on notice, on long leave, yet to join, exited), how many are on "
                "payroll (a salary in force today) and how many by department."
            ),
            hr_only=True,
        ),
        Tool(
            key="my_approvals",
            group="approvals",
            title="Waiting on you",
            description=(
                "What is waiting on the asker's decision across AskMira: leave, attendance "
                "corrections, timesheets, expense and travel claims, payroll runs and "
                "adjustments, loans, requisitions and offers, resignations and clearances, "
                "profile changes and documents. Set `kind` (leave, attendance, expense, payroll, "
                "hiring, exit or people) to narrow it, else none."
            ),
            inputs=("kind",),
        ),
        Tool(
            key="payroll_summary",
            hr_only=True,
            group="payroll_figures",
            title="Payroll",
            description=(
                "For admins and sub-admins who may view payroll: a month's payroll runs and "
                "what they add up to: people paid, gross, deductions, net pay, cost to company, "
                "PF, ESI, professional tax and TDS, and which runs are not paid yet. Set `from` "
                "and `to` to the month(s) asked about; both empty means the latest month run."
            ),
            inputs=("from", "to"),
        ),
        Tool(
            key="salary_totals",
            hr_only=True,
            group="payroll_figures",
            title="Salary bill",
            description=(
                "For admins and sub-admins who may view payroll: what everyone's salary in "
                "force today adds up to (monthly and annual CTC, average) and by department, "
                "from the Salary master. Use it for the total salary or CTC of the company or "
                "a department, not for one person."
            ),
        ),
        Tool(
            key="people_changes",
            hr_only=True,
            group="person_records",
            title="Joiners and exits",
            description=(
                "For admins and sub-admins: who joined and who left between `from` and `to` "
                "(both empty means this month), who is serving notice, the headcount at the "
                "start and the attrition rate for the period."
            ),
            inputs=("from", "to"),
        ),
        Tool(
            key="people_dates",
            hr_only=True,
            group="person_records",
            title="Dates coming up",
            description=(
                "For admins and sub-admins: birthdays, work anniversaries, probation periods "
                "ending and last working days between `from` and `to` (both empty means the "
                "next 30 days). Set `kind` (birthdays, anniversaries, probation or "
                "last_working_day) for one of them, else none for all four."
            ),
            inputs=("from", "to", "kind"),
        ),
        Tool(
            key="hr_tickets",
            group="hr_helpdesk",
            title="HR helpdesk",
            description=(
                "Tickets in AskMira's own HR helpdesk (salary queries, letters, ID cards, "
                "grievances), not IT tickets. Set `scope`: mine (tickets the asker raised), "
                "assigned (assigned to the asker), queue (unassigned) or company (the whole "
                "queue the asker works). Optionally `status` (new, open, in_progress, hold or "
                "closed) and `query` (a subject or person, for the queue)."
            ),
            inputs=("scope", "status", "query"),
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
            key="helpdesk_tickets",
            group="helpdesk_data",
            product="helpdesk",
            title="Helpdesk tickets",
            description=(
                f"IT tickets in {_HELPDESK}, the company's helpdesk. Set `scope`: mine (tickets "
                "the asker raised), assigned (assigned to the asker, for IT staff), queue (the "
                "unassigned queue, for IT staff) or company (all the company's, for helpdesk "
                "admins). Optionally set `status` (new, open, in_progress, hold or closed), "
                "`from` and `to` (when raised) and `query` (a subject, category or person)."
            ),
            inputs=("scope", "status", "from", "to", "query"),
        ),
        Tool(
            key="helpdesk_overview",
            group="helpdesk_data",
            product="helpdesk",
            title="Helpdesk at a glance",
            description=(
                f"Counts from {_HELPDESK}: the asker's tickets by status, and for IT staff their "
                "assigned tickets, the unassigned queue and the company's tickets; and how many "
                "assets the asker holds."
            ),
        ),
        Tool(
            key="helpdesk_assets",
            group="helpdesk_data",
            product="helpdesk",
            title="Assets",
            description=(
                f"Assets in {_HELPDESK}: laptops, monitors, phones and the like. Set `scope`: "
                "mine (assigned to the asker now) or company (the company's asset register, for "
                "IT staff). Optionally set `status` (in_use, spare or retired) and `query` (an "
                "asset code, type or model)."
            ),
            inputs=("scope", "status", "query"),
        ),
        Tool(
            key="payroll_trend",
            hr_only=True,
            group="payroll_figures",
            title="Payroll cost trend",
            description=(
                "For admins and sub-admins who may view payroll: payroll cost month by month - "
                "cost to company, gross, net pay and people paid - with the change each month, "
                "from the payslips of every run. Set `from` and `to` to the months asked about "
                "(both empty: the twelve months up to the latest payroll). Set `group` to split "
                "the cost by department, entity (company) or location, else none. Use it for "
                "any trend, comparison or 'why did payroll go up' question."
            ),
            inputs=("from", "to", "group"),
            reads=("compensation", "organisational"),
        ),
        Tool(
            key="headcount_trend",
            hr_only=True,
            group="person_records",
            title="Headcount trend",
            description=(
                "For admins and sub-admins: headcount at each month's end, who joined, who "
                "left and the attrition rate, month by month. Set `from` and `to` (both "
                "empty: the last twelve months)."
            ),
            inputs=("from", "to"),
            reads=("organisational",),
        ),
        Tool(
            key="account_pnl",
            hr_only=True,
            group="payroll_figures",
            title="Account P&L",
            description=(
                "For admins and sub-admins who may view payroll: a client account's P&L - what "
                "was billed against the people cost mapped to it (payroll cost shared by each "
                "person's account mapping) and its other direct costs, month by month, with "
                "the margin and the people on it. Put the account's name or code in `query`; "
                "leave it empty for every account side by side and the payroll cost on no "
                "account. Set `from` and `to` (empty: this financial year so far)."
            ),
            inputs=("query", "from", "to"),
            reads=("compensation", "identity", "organisational"),
        ),
        Tool(
            key="apply_leave",
            group="actions_leave",
            title="Leave request",
            kind="action",
            needs_ai=True,
            description=(
                "Prepares a leave request for the asker: checks the leave type, the days "
                "counted (weekly offs and holidays left out), the balance before and after, "
                "overlaps and notice rules, and who approves it, then shows the person an offer "
                "to send it. It does not apply anything: the person confirms. Set `leave_type` "
                "(the name or code; empty shows their balances to choose from), `from` and `to` "
                "(the same day for one day), `half_day` and `half_day_session` for a half day, "
                "and `reason` if they gave one."
            ),
            inputs=("leave_type", "from", "to", "half_day", "half_day_session", "reason"),
            reads=("leave", "identity"),
        ),
        Tool(
            key="cancel_leave",
            group="actions_leave",
            title="Withdraw leave",
            kind="action",
            needs_ai=True,
            description=(
                "Finds the asker's own pending or approved leave on the day in `from` and offers "
                "to withdraw it. The person confirms."
            ),
            inputs=("from",),
            reads=("leave",),
        ),
        Tool(
            key="request_attendance_change",
            group="actions_attendance",
            title="Attendance request",
            kind="action",
            needs_ai=True,
            description=(
                "Prepares an attendance request for the asker for one day (`from`): a missed "
                "punch (`in_time` and/or `out_time`, like 09:30), a correction of the day's "
                "status (`attendance_status`), on duty, or work from home (`attendance_kind`). "
                "`reason` is required: ask for it if they have not given one. The person "
                "confirms. Time off is leave, not this."
            ),
            inputs=(
                "from",
                "attendance_kind",
                "attendance_status",
                "in_time",
                "out_time",
                "reason",
            ),
            reads=("attendance",),
        ),
        Tool(
            key="decide_approvals",
            group="actions_approvals",
            title="Decide requests",
            kind="action",
            needs_ai=True,
            description=(
                "Offers to approve or reject requests waiting on the asker: leave, comp-off, "
                "attendance corrections, timesheets, shift swaps, overtime, expense and travel "
                "claims, asset requests. Look them up with my_approvals first and put their "
                "refs in `refs` (comma separated), `decision` approve or reject, and `note` if "
                "the person gave one. Payroll, loans, settlements and hiring are decided in "
                "the portal. The person confirms."
            ),
            inputs=("refs", "decision", "note"),
            reads=("identity", "leave", "attendance"),
        ),
        Tool(
            key="raise_hr_ticket",
            group="actions_helpdesk",
            title="HR ticket",
            kind="action",
            needs_ai=True,
            description=(
                "Prepares an HR helpdesk ticket for the asker (a salary query, a letter, a "
                "question for HR, a grievance): `category` (empty lists them), `subject` and "
                "`description` in the person's words. The person confirms."
            ),
            inputs=("category", "subject", "description"),
            reads=("ai_interaction",),
        ),
        Tool(
            key="confirm_action",
            group="actions",
            title="Done",
            kind="action",
            needs_ai=True,
            description=(
                "Carries out an offer you made earlier in this conversation, when the person's "
                "latest message is a plain yes to it. Put the offer's id in `action_id`. Never "
                "use it in the same turn as the offer, and never when they asked for a change: "
                "make a new offer instead."
            ),
            inputs=("action_id",),
            reads=("leave", "attendance", "identity"),
        ),
        Tool(
            key="cancel_action",
            group="actions",
            title="Cancelled",
            kind="action",
            needs_ai=True,
            description=(
                "Withdraws an offer you made in this conversation when the person says no or "
                "changes their mind. Put its id in `action_id`."
            ),
            inputs=("action_id",),
        ),
        Tool(
            key="product_connections",
            group="products",
            title="Linked products",
            description=(
                f"Whether {_HIRE} (interviews), {_EXPENSE} (expenses) and {_HELPDESK} (IT "
                "tickets and assets) are connected to this workspace, and what the asker may "
                "read from each. Use it when the person asks whether a product is connected, "
                "linked or set up."
            ),
        ),
        Tool(
            key="open_product",
            group="products",
            title="Open a linked product",
            description=(
                "Opens a product this workspace uses instead of an AskMira module: "
                "'interview' for hiring and interviews, 'expense' for expense claims, "
                "'helpdesk' for IT tickets and assets. Set `product`."
            ),
            inputs=("product",),
        ),
    )
}

#: Every input a tool may read, as the model fills it. One flat object, so the
#: answer's shape never depends on the tool chosen; unused fields stay empty.
INPUTS: dict[str, dict[str, Any]] = {
    "question": {"type": "string", "description": "A standalone question, or empty."},
    "lookup": {"type": "string", "enum": [*assistant.LOOKUPS, *people_tools.OWN_LOOKUPS]},
    "query": {"type": "string", "description": "Search text, or empty."},
    "include_exited": {"type": "boolean"},
    "employee_id": {"type": "string", "description": "Always empty."},
    "person_lookup": {"type": "string", "enum": ["none", *people_tools.PERSON_LOOKUPS]},
    "from": {"type": "string", "description": "First day, YYYY-MM-DD, or empty."},
    "to": {"type": "string", "description": "Last day, YYYY-MM-DD, or empty."},
    "product": {"type": "string", "enum": ["none", *PRODUCTS]},
    "status": {"type": "string", "enum": list(product_tools.STATUS_CHOICES)},
    "scope": {"type": "string", "enum": ["none", *product_tools.SCOPES]},
    "kind": {"type": "string", "enum": list(hr_tools.KINDS)},
    "group": {"type": "string", "enum": list(analysis_tools.GROUPS)},
    "leave_type": {"type": "string", "description": "A leave type's name or code, or empty."},
    "half_day": {"type": "boolean"},
    "half_day_session": {"type": "string", "enum": ["none", "first_half", "second_half"]},
    "reason": {"type": "string", "description": "In the person's own words, or empty."},
    "attendance_kind": {
        "type": "string",
        "enum": ["none", "missed_punch", "status_change", "on_duty", "work_from_home"],
    },
    "attendance_status": {"type": "string", "enum": ["present", "half_day", "absent", "lop"]},
    "in_time": {"type": "string", "description": "HH:MM, or empty."},
    "out_time": {"type": "string", "description": "HH:MM, or empty."},
    "refs": {"type": "string", "description": "Refs from my_approvals, comma separated."},
    "decision": {"type": "string", "enum": ["none", "approve", "reject"]},
    "note": {"type": "string", "description": "A note for the requester, or empty."},
    "category": {"type": "string", "description": "A helpdesk category's name, or empty."},
    "subject": {"type": "string"},
    "description": {"type": "string"},
    "action_id": {"type": "string", "description": "An offer's id, from earlier in this chat."},
}

#: What each existing lookup lets the model read (views.py strips the rest).
_READS: dict[str, tuple[str, ...]] = {
    "my_record": ("identity", "compensation", "leave", "attendance", "organisational"),
    "search_people": ("identity", "organisational"),
    "who_is_out": ("identity", "leave"),
    "attendance_summary": ("identity", "attendance"),
    "report_question": ("identity", "organisational"),
    "employee_record": ("identity", "compensation", "leave", "attendance", "organisational"),
    "my_approvals": ("identity", "leave", "attendance", "organisational"),
    "payroll_summary": ("compensation", "organisational"),
    "salary_totals": ("compensation", "organisational"),
    "people_changes": ("identity", "organisational"),
    "people_dates": ("identity", "organisational"),
    "hr_tickets": ("identity", "organisational"),
    "hiring_overview": ("recruitment",),
    "hiring_jobs": ("recruitment",),
    "hiring_candidates": ("recruitment",),
    "hiring_interviews": ("recruitment",),
    "expense_claims": ("compensation", "identity"),
    "expense_summary": ("compensation",),
    "helpdesk_tickets": ("identity", "organisational"),
    "helpdesk_overview": ("organisational",),
    "helpdesk_assets": ("identity", "organisational"),
}
TOOLS = {key: replace(tool, reads=_READS.get(key, tool.reads)) for key, tool in TOOLS.items()}

#: Inputs the model never fills: a pick from a list carries them.
_PICK_ONLY = frozenset({"employee_id"})


def tool_schema(tool: Tool) -> dict[str, Any]:
    """The tool as the model is shown it: name, what it does, and only the inputs it reads."""
    names = [name for name in tool.inputs if name not in _PICK_ONLY]
    return {
        "name": tool.key,
        "description": tool.description,
        "input_schema": {
            "type": "object",
            "properties": {name: INPUTS[name] for name in names},
            "required": [],
            "additionalProperties": False,
        },
    }


def coerce(tool: Tool, raw: dict[str, Any]) -> dict[str, Any]:
    """The model's inputs, kept to what the tool reads and to each input's type and choices."""
    out: dict[str, Any] = {}
    for name in tool.inputs:
        if name in _PICK_ONLY:
            continue
        spec = INPUTS[name]
        value = raw.get(name)
        if spec.get("type") == "boolean":
            out[name] = value is True or str(value).lower() == "true"
            continue
        text = "" if value is None else str(value).strip()[:2000]
        if "enum" in spec and text not in spec["enum"]:
            text = "none" if "none" in spec["enum"] else spec["enum"][0]
        out[name] = text
    return out


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
    hr = permissions.is_hr(user)
    out: list[Tool] = []
    for tool in TOOLS.values():
        if not group_on(config, tool.group):
            continue
        if tool.hr_only and not hr:
            continue
        if tool.scope and tool.scope not in scopes:
            continue
        if tool.feature and not (ai and getattr(settings, tool.feature, False)):
            continue
        if tool.product and not product_roles.get(tool.product):
            continue
        if tool.key == "open_product" and not launchable:
            continue
        if tool.needs_ai and not ai:
            continue
        out.append(tool)
    # Confirming and withdrawing offers come with any action that makes them.
    if not any(t.kind == "action" and t.group != "actions" for t in out):
        out = [t for t in out if t.group != "actions"]
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
    session: AsyncSession,
    user: User,
    tool: Tool,
    inputs: dict[str, Any],
    *,
    today: date,
    ctx: actions.ToolContext | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one tool as `user`. Returns the cards and the inputs it actually used."""
    try:
        if tool.kind == "action":
            if ctx is None:
                return [_notice("That can only be done in a conversation.")], {}
            return await _act(session, user, tool, inputs, ctx)
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
        if lookup in people_tools.OWN_LOOKUPS:
            cards = await people_tools.own_record(session, user, lookup, today=today)
            return cards, {"lookup": lookup}
        text, _, href = await assistant.lookup_own_record(session, user, lookup)
        return [{"type": "own_record", "text": text, "open": href}], {"lookup": lookup}

    if tool.key == "my_approvals":
        return await hr_tools.approvals(session, user, inputs)

    if tool.key == "payroll_summary":
        return await hr_tools.payroll_summary(session, user, inputs, today=today)

    if tool.key == "salary_totals":
        return await hr_tools.salary_totals(session, user, today=today)

    if tool.key == "people_changes":
        return await hr_tools.people_changes(session, user, inputs, today=today)

    if tool.key == "people_dates":
        return await hr_tools.people_dates(session, user, inputs, today=today)

    if tool.key == "hr_tickets":
        return await hr_tools.hr_tickets(session, user, inputs)

    if tool.key == "employee_record":
        return await people_tools.person_record(session, user, inputs, today=today)

    if tool.key == "payroll_trend":
        return await analysis_tools.payroll_trend(session, user, inputs, today=today)

    if tool.key == "headcount_trend":
        return await analysis_tools.headcount_trend(session, user, inputs, today=today)

    if tool.key == "account_pnl":
        return await analysis_tools.account_pnl(session, user, inputs, today=today)

    if tool.key == "headcount":
        return await people_tools.headcount(session, user, today=today)

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

    if tool.key == "product_connections":
        rows = []
        for key, product in PRODUCTS.items():
            state = await products_data.connection(session, user, key)
            rows.append([product.brand, state["server"], state["workspace"], state["access"]])
        return [
            _table(
                "Linked products",
                ["Product", "On this server", "This workspace", "Your access"],
                rows,
                caption="What Ask Mira can read for you from each product",
                empty="No products are known to AskMira.",
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


async def _act(
    session: AsyncSession,
    user: User,
    tool: Tool,
    inputs: dict[str, Any],
    ctx: actions.ToolContext,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """An action tool: an offer to make, or one to carry out or withdraw."""
    if tool.key == "apply_leave":
        return await actions.propose_leave(session, user, inputs, ctx)
    if tool.key == "cancel_leave":
        return await actions.propose_leave_cancel(session, user, inputs, ctx)
    if tool.key == "request_attendance_change":
        return await actions.propose_regularisation(session, user, inputs, ctx)
    if tool.key == "decide_approvals":
        return await actions.propose_decision(session, user, inputs, ctx)
    if tool.key == "raise_hr_ticket":
        return await actions.propose_hr_ticket(session, user, inputs, ctx)
    if tool.key in ("confirm_action", "cancel_action"):
        try:
            action_id = uuid.UUID(str(inputs.get("action_id") or "").strip())
        except ValueError:
            return [_notice("Which offer? Its id is on the offer card.")], {}
        used = {"action_id": str(action_id)}
        if tool.key == "cancel_action":
            row = await actions.cancel(
                session,
                user,
                action_id,
                owner_id=user.id,
                via="message",
                request=ctx.request,
                conversation_id=ctx.conversation_id,
            )
        else:
            # The person's message and anything offered before it are saved
            # first: carrying out an offer commits, and a failure rolls back.
            await session.commit()
            row = await actions.confirm(
                session,
                user,
                action_id,
                owner_id=user.id,
                via="message",
                request=ctx.request,
                ctx=ctx,
            )
        return [actions.card_for(row)], used
    return [_notice("That is not something I can do here.")], {}
