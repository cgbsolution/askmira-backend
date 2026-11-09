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

A tool that reads a linked product (AnytimeHire, ExpenseGenie, CGB Helpdesk, Saleinx,
ProcureFlow) is offered only when the workspace has that product linked and the
person has a role in it; it asks the product as them (product_tools.py,
sales_tools.py). For Saleinx and ProcureFlow that role is the product's own
(a rep, a manager, a viewer; a requester, an approver), never AskMira's.
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
    budget_tools,
    connect_tools,
    data_tools,
    equipment_tools,
    helpdesk_actions,
    hr_tools,
    order_win,
    people_tools,
    product_tools,
    sales_tools,
)
from app.services.companion.config import CompanionConfig, group_on
from app.services.products import data as products_data
from app.services.products.catalogue import PRODUCTS
from app.services.reporting.compiler import BUCKETS
from app.services.reporting.semantic import ENTITIES

_HIRE = PRODUCTS["interview"].brand
_EXPENSE = PRODUCTS["expense"].brand
_HELPDESK = PRODUCTS["helpdesk"].brand
_SALES = PRODUCTS["saleinx"].brand
_PROCURE = PRODUCTS["procurement"].brand

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
            key="joiners_equipment",
            group="person_records",
            title="Joiners and equipment",
            description=(
                "For admins and sub-admins: the people joining in the next 30 days or who joined "
                "in the last 30 (or `from`/`to`), and whether each already holds equipment in "
                "the asset register (the linked helpdesk's, else AskMira's). Use it for 'which "
                "employees still need equipment assigned?'."
            ),
            inputs=("from", "to"),
            hr_only=True,
        ),
        Tool(
            key="department_budget",
            group="budgets",
            title="Department budget",
            description=(
                "For admins and sub-admins: a department's headcount budget this financial "
                "year: approved positions and cost, what is counted and filled, and what is "
                "left. To ask whether more people can be hired, set `hires` to how many and "
                "`amount` to the yearly CTC per hire (rupees, as said). Put the department's "
                "name in `query`, or leave it empty for every budget."
            ),
            inputs=("query", "hires", "amount"),
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
                "rejected or draft), `from` and `to` (submission dates, up to a year) and `query` "
                "(a title, category or person). Lists the latest claims; for totals, who "
                "submitted how much, or month by month, use expense_summary."
            ),
            inputs=("scope", "status", "from", "to", "query"),
        ),
        Tool(
            key="expense_summary",
            group="expense_data",
            product="expense",
            title="Expense totals",
            description=(
                f"Expense totals in {_EXPENSE}: by status and category, who submitted how much, "
                "month by month (as charts), and approved claims not yet paid. This is where the "
                "company's expense claims are: use it for any expense question. Set `scope` "
                "(mine, approvals, or company for HR only), optionally `status`, and `from` and "
                "`to` - any period up to a year (the last 12 months is fine); both empty means "
                "this month so far."
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
                "unassigned queue, for IT staff), company (all the company's, for helpdesk "
                "admins) or team (raised by the people who report to the asker, for managers). "
                "Optionally set `status` (new, open, in_progress, hold or closed), "
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
            key="query_data",
            group="data_questions",
            title="Data",
            description=(
                "Any figure, trend, comparison or breakdown over AskMira's data, drawn as a chart "
                "with its table: payroll cost by month, joiners by department, leave days by "
                "type, exits by quarter, expenses by category, helpdesk tickets, assets, "
                "timesheet hours, recognition, client billing... Choose `dataset`, the "
                '`measures` ("field:aggregation", e.g. "employer_cost:sum"; empty = how many '
                "people), `group_by` (up to two fields), `bucket` for a trend over time (month, "
                "quarter, financial_year...; the dataset's date unless `time_field` says "
                "otherwise), the period (`months` = the last N months including this one, or "
                "`from`/`to`), `filters` ([{field, op, value}]; op one of equals, not_equals, in, "
                "contains, gt, gte, lt, lte, between, relative_range) and `chart`: line or area "
                "for a trend, bar or horizontal_bar to compare groups, stacked_bar for a "
                "breakdown, pie or donut for shares of a whole, number for one figure, table "
                "for a list. Rows are only what the person may see."
            ),
            inputs=(
                "dataset",
                "measures",
                "group_by",
                "bucket",
                "time_field",
                "months",
                "from",
                "to",
                "filters",
                "chart",
                "top",
            ),
            reads=("organisational",),
        ),
        Tool(
            key="payroll_trend",
            hr_only=True,
            group="payroll_figures",
            title="Payroll cost trend",
            description=(
                "For admins and sub-admins who may view payroll: payroll cost month by month - "
                "cost to company, gross, net pay and people paid - with the change each month, "
                "from the payslips of every run. Set `months` for the last N payroll months (up "
                "to the latest payroll), or `from` and `to` (all empty: the twelve months up to "
                "the latest payroll). Set `group` to split "
                "the cost by department, entity (company) or location, else none. Use it for "
                "any trend, comparison or 'why did payroll go up' question."
            ),
            inputs=("from", "to", "months", "group"),
            # The model reads totals over five or more people only (model_view).
            reads=("organisational",),
        ),
        Tool(
            key="headcount_trend",
            hr_only=True,
            group="person_records",
            title="Headcount trend",
            description=(
                "For admins and sub-admins: headcount at each month's end, who joined, who "
                "left and the attrition rate, month by month. Set `months` for the last N "
                "months, or `from` and `to` (all empty: the last twelve months)."
            ),
            inputs=("from", "to", "months"),
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
            # Billing always; cost and margin only over five or more people (model_view).
            reads=("organisational",),
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
            key="raise_it_ticket",
            group="actions_it_helpdesk",
            product="helpdesk",
            title="IT ticket",
            kind="action",
            needs_ai=True,
            description=(
                f"Prepares an IT ticket in {_HELPDESK} for the asker (a laptop, VPN, email, "
                "printer or access problem) and offers it; the person confirms. Set `subject` (a "
                "few words), `description` (their words), `priority` (low, medium or high: ask "
                "only if they didn't say), `location` if they gave one, and `category` and "
                "`sub_category` yourself when the problem makes them clear (a laptop or screen "
                "is Hardware, a VPN is Network & Connectivity, a password is Access & Identity). "
                "When the tool lists the helpdesk's categories or sub categories, pick the one "
                "that fits what they said and call it again; ask the person only if it is truly "
                "unclear. For HR matters use raise_hr_ticket."
            ),
            inputs=("subject", "description", "category", "sub_category", "priority", "location"),
            reads=("ai_interaction", "organisational"),
        ),
        Tool(
            key="work_it_ticket",
            group="actions_it_helpdesk",
            product="helpdesk",
            title="Work an IT ticket",
            kind="action",
            needs_ai=True,
            description=(
                f"Does one thing on a ticket in {_HELPDESK}, as the helpdesk lets the asker: "
                "`ticket_action` reply (`message`), acquire (pick it up from the queue), assign "
                "(`engineer`: a name or email; IT admins), hold (`reason` and `hours`: 24, 48 or "
                "72), close (`note`: an optional closing note) or reopen. Set `ticket` to its "
                "number (#1A2B3C4D) or its subject. Call it as soon as the person names a ticket "
                "and what to do, with only those if that is all you have: the helpdesk decides "
                "whether they may, and the tool says what else it needs. Never tell them they can "
                "do it, or ask for details, before the tool has checked. When it asks for "
                "something, ask the person; never make it up."
            ),
            inputs=("ticket", "ticket_action", "message", "engineer", "reason", "hours", "note"),
            reads=("ai_interaction", "organisational"),
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
            key="sales_deals",
            group="sales_data",
            product="saleinx",
            title="Deals",
            description=(
                f"Leads and deals in {_SALES}, the sales CRM, that the asker can see there (a "
                "rep sees their own and unassigned ones; a manager the whole team): stage, "
                "value, margin, close date, approval of a Won, next step. Put a deal's name, "
                "its company or both (as shown, 'Company - Deal') in `query`; set "
                "`deal_stage` to one stage, or awaiting_approval for Wons waiting on a manager. "
                "Use it to find a deal before changing it."
            ),
            inputs=("query", "deal_stage"),
        ),
        Tool(
            key="sales_pipeline",
            group="sales_data",
            product="saleinx",
            title="Pipeline",
            description=f"The asker's pipeline in {_SALES}: deals and value by stage.",
        ),
        Tool(
            key="save_lead",
            group="actions_sales",
            product="saleinx",
            title="Lead",
            kind="action",
            needs_ai=True,
            description=(
                f"Prepares a new lead in {_SALES} from what the person said or pasted, or an "
                "update to an existing one, and offers it; the person confirms. For a new lead: "
                "`company`, `contact_name`, `contact_title`, `contact_email`, `contact_phone`, "
                "`deal_name` (what they want in a few words, such as 'SafeOps360 for 3 plants'; "
                "not the company's or the contact's name: Saleinx shows it after the company's), "
                "`description` (what they want, "
                "in a sentence or two), `amount` (the budget in rupees: 1.2 crore is 12000000), "
                "`decision_by` (the date they decide), `referred_by` and `source`, "
                "`follow_up` and `follow_up_date` ('call him back this Thursday': the "
                "coming Thursday's date), `offerings` (what it sells, from the workspace's "
                "list), `note`. To change an existing lead, put its name or company in `deal` "
                "and only what changed (a new title, a new phone number). A lead has no close "
                "date in Saleinx: the decision date goes into its next step."
            ),
            inputs=(
                "deal",
                "company",
                "contact_name",
                "contact_title",
                "contact_email",
                "contact_phone",
                "deal_name",
                "description",
                "amount",
                "decision_by",
                "referred_by",
                "source",
                "follow_up",
                "follow_up_date",
                "offerings",
                "note",
            ),
            reads=("ai_interaction", "organisational"),
        ),
        Tool(
            key="move_deal",
            group="actions_sales",
            product="saleinx",
            title="Deal stage",
            kind="action",
            needs_ai=True,
            description=(
                f"Moves a deal to another stage in {_SALES}, after checking what Saleinx "
                "requires, and offers it; the person confirms. Set `deal` (its name or company) "
                "and `deal_stage`, plus what the move needs: `margin_pct` from Qualified "
                "onwards, `close_date` for Proposal, Negotiation and Won, `next_step`, for Hold "
                "a `hold_reason` and `revisit_on` (the date to look at it again), for Lost a "
                "`reason`, and for a move backwards a `rollback_reason`. When the tool asks for "
                "one of these, ask the person; never make it up. A rep's Won waits for a "
                "manager's approval."
            ),
            inputs=(
                "deal",
                "deal_stage",
                "margin_pct",
                "close_date",
                "next_step",
                "hold_reason",
                "revisit_on",
                "reason",
                "rollback_reason",
            ),
        ),
        Tool(
            key="decide_deal",
            group="actions_sales",
            product="saleinx",
            title="Approve a won deal",
            kind="action",
            needs_ai=True,
            description=(
                f"For sales managers: approves or rejects a rep's Won waiting in {_SALES}. Set "
                "`deal`, `decision` and, to reject, `reason`. The person confirms."
            ),
            inputs=("deal", "decision", "reason"),
        ),
        Tool(
            key="book_order",
            group="actions_orders",
            product="saleinx",
            title="Order win",
            kind="action",
            needs_ai=True,
            description=(
                "When a customer has signed: checks the deal is Won and approved in "
                f"{_SALES}, checks the PO's value against the team's billing, and offers to "
                f"open the positions in {_HIRE} and write the prospective P&L (billing, cost, "
                "recruitment, margin by month) on the customer's account. The person confirms; "
                "if a step fails the earlier ones are undone. Set `deal` (the deal or "
                "customer), `po_number`, `po_value` (rupees: 71.1 lakh is 7110000; empty if "
                "not given), `start_date`, `months`, `location`, `work_mode`, "
                "`recruitment_cost_per_hire`, and `roles`: one entry per role with `title`, "
                "`count`, `experience` ('4 to 6', '8+'), `allocation` (per cent of full time, "
                "100 unless said), `bill_rate` and `cost_rate` (rupees a month at full time) "
                "and `skills`. Never work out the totals yourself: repeat the tool's figures."
            ),
            inputs=(
                "deal",
                "client",
                "po_number",
                "po_value",
                "start_date",
                "months",
                "location",
                "work_mode",
                "recruitment_cost_per_hire",
                "roles",
            ),
        ),
        Tool(
            key="procurement_overview",
            group="procurement_data",
            product="procurement",
            title="Procurement at a glance",
            description=(
                f"What {_PROCURE}, the procure-to-pay system, shows the asker: approvals "
                "waiting on them, their requisitions by status, and (if their role there "
                "allows) purchase orders and invoices by status."
            ),
        ),
        Tool(
            key="procurement_approvals",
            group="procurement_data",
            product="procurement",
            title="ProcureFlow approvals",
            description=(
                f"Requisitions, purchase orders, invoices and payment runs waiting on the "
                f"asker's approval in {_PROCURE}, each with its ref for decide_procurement."
            ),
        ),
        Tool(
            key="procurement_documents",
            group="procurement_data",
            product="procurement",
            title="Procurement documents",
            description=(
                f"Requisitions, purchase orders or invoices in {_PROCURE} (set `document`), "
                "with `doc_status` and `query` (a number, a title or a vendor) to narrow. "
                "Requisitions are the asker's own unless `scope` is company and their role "
                "there sees everyone's."
            ),
            inputs=("document", "doc_status", "query", "scope"),
        ),
        Tool(
            key="procurement_budgets",
            group="procurement_data",
            product="procurement",
            title="Budgets",
            description=(
                f"This period's budgets in {_PROCURE} by cost centre: budget, committed, spent, "
                "available and how much is used, for people whose role there reads budgets."
            ),
        ),
        Tool(
            key="decide_procurement",
            group="actions_procurement",
            product="procurement",
            title="Decide in ProcureFlow",
            kind="action",
            needs_ai=True,
            description=(
                f"Offers to approve or reject what waits on the asker in {_PROCURE}: look them "
                "up with procurement_approvals and put their refs (pf:…) in `refs`, `decision` "
                "approve or reject, and `note` (required to reject). The person confirms."
            ),
            inputs=("refs", "decision", "note"),
        ),
        Tool(
            key="account_forecast",
            hr_only=True,
            group="payroll_figures",
            title="Prospective P&L",
            description=(
                "For admins and sub-admins who may view payroll: the prospective P&L booked on "
                "client accounts from won orders, month by month (billing, people cost, "
                "recruitment, margin). Put the account's name in `query`, or leave it empty "
                "for all."
            ),
            inputs=("query",),
        ),
        Tool(
            key="product_connections",
            group="products",
            title="Linked products",
            description=(
                f"Whether {_HIRE} (interviews), {_EXPENSE} (expenses), {_HELPDESK} (IT "
                f"tickets and assets), {_SALES} (sales CRM) and {_PROCURE} (procure-to-pay) are "
                "connected to this workspace, and what the asker may read from each. Use it "
                "when the person asks whether a product is connected, linked or set up."
            ),
        ),
        Tool(
            key="open_product",
            group="products",
            title="Open a linked product",
            description=(
                "Opens a product this workspace uses: 'interview' for hiring and interviews, "
                "'expense' for expense claims, 'helpdesk' for IT tickets and assets, "
                "'saleinx' for leads and deals, 'procurement' for requisitions, orders and "
                "invoices. Set `product`."
            ),
            inputs=("product",),
        ),
        Tool(
            key="connect_product",
            group="products",
            title="Connect a product",
            description=(
                "When the person asks for something in a product that is not ready for them "
                "(each message lists those), call this with `product` before you answer: "
                "'saleinx' for leads and deals, 'helpdesk' for IT tickets and assets, 'expense' "
                "for expense claims, 'interview' for jobs and candidates, 'procurement' for "
                "requisitions, orders and invoices. It shows them what is missing and what to "
                "do: an admin gets a Connect button that brings them back to this conversation, "
                "anyone else is told who can do it. Never just say you can't, without it."
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
    "sub_category": {"type": "string", "description": "A helpdesk sub category, or empty."},
    "priority": {"type": "string", "enum": ["none", "low", "medium", "high"]},
    "ticket": {"type": "string", "description": "A ticket's number like #1A2B3C4D, or subject."},
    "ticket_action": {"type": "string", "enum": ["none", *helpdesk_actions.TICKET_ACTIONS]},
    "message": {"type": "string", "description": "What to write, in the person's words."},
    "engineer": {"type": "string", "description": "An IT engineer's name or email, or empty."},
    "hours": {"type": "string", "enum": ["none", "24", "48", "72"]},
    "subject": {"type": "string"},
    "description": {"type": "string"},
    "action_id": {"type": "string", "description": "An offer's id, from earlier in this chat."},
    "dataset": {"type": "string", "enum": list(ENTITIES)},
    "measures": {
        "type": "array",
        "items": {"type": "string"},
        "description": 'Figures as "field:aggregation": sum, avg, count, count_distinct, min, max.',
    },
    "group_by": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Up to two fields to group by. A date here is grouped by `bucket`.",
    },
    "bucket": {"type": "string", "enum": ["none", *BUCKETS]},
    "time_field": {"type": "string", "description": "The date to run a trend along, or empty."},
    "hires": {"type": "integer", "description": "How many more people, or 0."},
    "months": {
        "type": "integer",
        "description": (
            "The last N months (for payroll and billing, up to the latest month with data); "
            "0 when from/to are used."
        ),
    },
    "filters": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "op": {"type": "string"},
                "value": {"type": "string"},
            },
        },
        "description": 'For example [{"field": "department", "op": "equals", "value": "Sales"}].',
    },
    "chart": {"type": "string", "enum": list(data_tools.CHARTS)},
    "top": {"type": "integer", "description": "How many groups to draw before 'Others'."},
    # Saleinx
    "deal": {
        "type": "string",
        "description": "A deal or lead: its name, its company, or 'Company - Deal' as shown.",
    },
    "deal_stage": {
        "type": "string",
        "enum": ["none", *sales_tools.DEAL_STAGES, "awaiting_approval"],
    },
    "margin_pct": {"type": "string", "description": "Gross margin in per cent, like 32, or empty."},
    "close_date": {"type": "string", "description": "Expected close date, YYYY-MM-DD, or empty."},
    "next_step": {"type": "string", "description": "What happens next on the deal, or empty."},
    "hold_reason": {"type": "string", "enum": ["none", *sales_tools.HOLD_REASONS]},
    "revisit_on": {"type": "string", "description": "When to look at it again, YYYY-MM-DD."},
    "rollback_reason": {"type": "string", "enum": ["none", *sales_tools.ROLLBACK_REASONS]},
    "company": {"type": "string", "description": "The customer company's name, or empty."},
    "contact_name": {"type": "string"},
    "contact_title": {"type": "string", "description": "Their job title, like VP IT."},
    "contact_email": {"type": "string"},
    "contact_phone": {"type": "string"},
    "deal_name": {"type": "string", "description": "A short name for what they want."},
    "amount": {"type": "string", "description": "Rupees, like 12000000, or empty."},
    "decision_by": {"type": "string", "description": "When they decide, YYYY-MM-DD, or empty."},
    "referred_by": {"type": "string", "description": "Who referred them, or empty."},
    "source": {"type": "string", "description": "Where the lead came from, or empty."},
    "follow_up": {"type": "string", "description": "The follow-up to do, like 'Call back'."},
    "follow_up_date": {"type": "string", "description": "YYYY-MM-DD, or empty."},
    "offerings": {
        "type": "array",
        "items": {"type": "string"},
        "description": "What the deal sells, from the workspace's list.",
    },
    # Order win
    "client": {"type": "string", "description": "The customer, if not the deal's company."},
    "po_number": {"type": "string", "description": "The customer's purchase order number."},
    "po_value": {"type": "string", "description": "The PO's value in rupees, or empty."},
    "start_date": {"type": "string", "description": "When the work starts, YYYY-MM-DD."},
    "location": {"type": "string", "description": "Where it is delivered from, like Pune."},
    "work_mode": {"type": "string", "description": "Onsite, hybrid or remote."},
    "recruitment_cost_per_hire": {"type": "string", "description": "Rupees per hire, or 0."},
    "roles": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "count": {"type": "string"},
                "experience": {"type": "string"},
                "allocation": {"type": "string"},
                "bill_rate": {"type": "string"},
                "cost_rate": {"type": "string"},
                "skills": {"type": "string"},
            },
        },
        "description": "One entry per role the order needs.",
    },
    # ProcureFlow
    "document": {"type": "string", "enum": list(sales_tools.PROCUREMENT_DOCUMENTS)},
    "doc_status": {
        "type": "string",
        "description": "A status there, like AWAITING_APPROVAL, APPROVED or PAID, or empty.",
    },
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
    # Counts only (hr_tools.people_changes writes its own view); names stay on screen.
    "people_changes": ("organisational",),
    "people_dates": ("identity", "organisational"),
    "hr_tickets": ("identity", "organisational"),
    "hiring_overview": ("recruitment",),
    "hiring_jobs": ("recruitment",),
    "hiring_candidates": ("recruitment",),
    "hiring_interviews": ("recruitment",),
    "expense_claims": ("compensation", "identity"),
    # Monthly totals over five or more claimants only (product_tools._expense_view).
    "expense_summary": ("organisational",),
    "helpdesk_tickets": ("identity", "organisational"),
    "helpdesk_overview": ("organisational",),
    "helpdesk_assets": ("identity", "organisational"),
    # Business records; the model's view carries no person (sales_tools.py).
    "sales_deals": ("organisational",),
    "sales_pipeline": ("organisational",),
    "procurement_overview": ("organisational",),
    "procurement_approvals": ("organisational",),
    "procurement_documents": ("organisational",),
    "procurement_budgets": ("organisational",),
    "account_forecast": ("organisational",),
    # Budgets and the budgeted ceilings on seats; no person's pay.
    "department_budget": ("organisational",),
    # Counts only (the card writes its own view); names stay on screen.
    "joiners_equipment": ("organisational",),
}
TOOLS = {key: replace(tool, reads=_READS.get(key, tool.reads)) for key, tool in TOOLS.items()}

#: Inputs the model never fills: a pick from a list carries them.
_PICK_ONLY = frozenset({"employee_id"})


def tool_schema(
    tool: Tool, *, role: str | None = None, hidden: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """The tool as the model is shown it: name, what it does, and only the inputs it reads.

    The data tool also lists the datasets and fields `role` may query.
    """
    names = [name for name in tool.inputs if name not in _PICK_ONLY]
    description = tool.description
    if tool.key == "query_data" and role:
        description += "\n\nDatasets and their fields:\n" + data_tools.catalogue_text(role, hidden)
    return {
        "name": tool.key,
        "description": description,
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
        if spec.get("type") == "integer":
            try:
                out[name] = (
                    max(0, min(int(str(value).strip()), 1000)) if value not in (None, "") else 0
                )
            except ValueError:
                out[name] = 0
            continue
        if spec.get("type") == "array":
            items = value if isinstance(value, list) else ([] if value in (None, "") else [value])
            if spec["items"].get("type") == "object":
                out[name] = [
                    {str(k)[:40]: str(v)[:200] for k, v in item.items()}
                    for item in items[:10]
                    if isinstance(item, dict)
                ] + [str(item)[:200] for item in items[:10] if isinstance(item, str)]
            else:
                out[name] = [str(item).strip()[:100] for item in items[:10] if str(item).strip()]
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
    # With a model, the data tool answers what the report question did, and more.
    if ai and any(t.key == "query_data" for t in out):
        out = [t for t in out if t.key != "report_question"]
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
    if tool.key in sales_tools.READ_TOOLS:
        return await sales_tools.run(session, user, tool.key, inputs, today=today)

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

    if tool.key == "query_data":
        cards = await data_tools.query_data(session, user, inputs, today=today)
        return cards, {k: v for k, v in inputs.items() if v not in ("", [], 0, "none", "auto")}

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

    if tool.key == "joiners_equipment":
        return await equipment_tools.joiners_equipment(session, user, inputs, today=today)

    if tool.key == "department_budget":
        return await budget_tools.department_budget(session, user, inputs, today=today)

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

    if tool.key == "connect_product":
        return await connect_tools.connect_card(session, user, str(inputs.get("product") or ""))

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
    if tool.key == "raise_it_ticket":
        return await helpdesk_actions.propose_raise(session, user, inputs, ctx)
    if tool.key == "work_it_ticket":
        return await helpdesk_actions.propose_work(session, user, inputs, ctx)
    if tool.key == "save_lead":
        return await sales_tools.propose_lead(session, user, inputs, ctx)
    if tool.key == "move_deal":
        return await sales_tools.propose_stage(session, user, inputs, ctx)
    if tool.key == "decide_deal":
        return await sales_tools.propose_deal_decision(session, user, inputs, ctx)
    if tool.key == "decide_procurement":
        return await sales_tools.propose_procurement_decision(session, user, inputs, ctx)
    if tool.key == "book_order":
        return await order_win.propose(session, user, inputs, ctx)
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
            try:
                row = await actions.confirm(
                    session,
                    user,
                    action_id,
                    owner_id=user.id,
                    via="message",
                    request=ctx.request,
                    ctx=ctx,
                )
            except HTTPException as exc:
                # Refused: say so to the person in their words, and to the model
                # plainly that nothing happened, so it never reports it as done.
                detail = exc.detail if isinstance(exc.detail, str) else "That could not be done."
                shown = (
                    "Nothing was done yet: the offer is still waiting. Press its button to go "
                    "ahead, or reply “yes”."
                    if detail.startswith("The person")
                    else detail
                )
                card = _notice(shown, "warning")
                card["model_note"] = (
                    f"Not done, nothing changed: {detail} Do not say it was done or saved."
                )
                return [card], used
        return [actions.card_for(row)], used
    return [_notice("That is not something I can do here.")], {}
