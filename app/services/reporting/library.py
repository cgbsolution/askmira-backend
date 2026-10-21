"""The starter reports. Clients clone these rather than starting from nothing.

Every one of them is a question an Indian HR team actually asks — the sort of
thing that otherwise arrives as a support ticket and, three tickets later, as a
custom development request. That is the margin argument for this slice, and it
only works if the starters cover the real questions rather than the easy ones.

Each carries `required_sensitivity`, so the library a role sees is already
filtered: showing a manager "Salary register" and refusing it when they click is
worse than not offering it. The compiler checks again, independently — a filtered
list is a courtesy and the check that matters is the one that runs when the query
is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.reporting.compiler import Definition, Filter, Operator, Selection
from app.services.reporting.semantic import Sensitivity, entity, may_see

__all__ = ["STARTERS", "StarterReport", "starters_for", "validate_library"]


@dataclass(frozen=True, slots=True)
class StarterReport:
    key: str
    name: str
    description: str
    #: Why somebody would run this. The difference between a report list people
    #: browse and one they scroll past.
    question: str
    definition: Definition
    chart_type: str = "table"
    category: str = "General"
    required_sensitivity: Sensitivity = Sensitivity.PUBLIC

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "question": self.question,
            "category": self.category,
            "chart_type": self.chart_type,
            "entity_key": self.definition.entity_key,
            "required_sensitivity": self.required_sensitivity.value,
        }


def _report(
    key: str,
    name: str,
    question: str,
    definition: Definition,
    *,
    description: str = "",
    chart_type: str = "table",
    category: str = "General",
    sensitivity: Sensitivity = Sensitivity.PUBLIC,
) -> StarterReport:
    return StarterReport(
        key=key,
        name=name,
        description=description or question,
        question=question,
        definition=definition,
        chart_type=chart_type,
        category=category,
        required_sensitivity=sensitivity,
    )


ACTIVE_ONLY = Filter("status", Operator.EQUALS, "active")


STARTERS: tuple[StarterReport, ...] = (
    # ── People ──────────────────────────────────────────────────────────────
    _report(
        "headcount_by_department",
        "Headcount by department",
        "How many people are in each department right now?",
        Definition(
            entity_key="employee",
            selections=(
                Selection("department"),
                Selection("headcount", "count", "People"),
            ),
            filters=(ACTIVE_ONLY,),
            group_by=("department",),
            sort_by=(("headcount", "desc"),),
        ),
        chart_type="bar",
        category="People",
    ),
    _report(
        "headcount_by_location",
        "Headcount by location",
        "Where is everybody? Needed for every state-wise statutory question.",
        Definition(
            entity_key="employee",
            selections=(Selection("location"), Selection("headcount", "count", "People")),
            filters=(ACTIVE_ONLY,),
            group_by=("location",),
            sort_by=(("headcount", "desc"),),
        ),
        chart_type="bar",
        category="People",
    ),
    _report(
        "headcount_by_grade",
        "Headcount by grade",
        "What does the grade pyramid look like?",
        Definition(
            entity_key="employee",
            selections=(Selection("grade"), Selection("headcount", "count", "People")),
            filters=(ACTIVE_ONLY,),
            group_by=("grade",),
            sort_by=(("grade", "asc"),),
        ),
        chart_type="bar",
        category="People",
    ),
    _report(
        "gender_diversity_by_department",
        "Gender diversity by department",
        "Where is the balance worst, and where is it improving?",
        Definition(
            entity_key="employee",
            selections=(
                Selection("department"),
                Selection("gender"),
                Selection("headcount", "count", "People"),
            ),
            filters=(ACTIVE_ONLY,),
            group_by=("department", "gender"),
        ),
        chart_type="stacked",
        category="People",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "new_joiners_last_90_days",
        "New joiners, last 90 days",
        "Who has joined recently, and is onboarding keeping up?",
        Definition(
            entity_key="employee",
            selections=(
                Selection("employee_code"),
                Selection("employee_name"),
                Selection("department"),
                Selection("designation"),
                Selection("date_of_joining"),
            ),
            filters=(Filter("date_of_joining", Operator.RELATIVE_RANGE, "last_90_days"),),
            sort_by=(("date_of_joining", "desc"),),
        ),
        category="People",
    ),
    _report(
        "exits_this_financial_year",
        "Exits this financial year",
        "Who has left since April, and from where?",
        Definition(
            entity_key="employee",
            selections=(
                Selection("employee_code"),
                Selection("employee_name"),
                Selection("department"),
                Selection("date_of_joining"),
                Selection("date_of_exit"),
            ),
            filters=(Filter("date_of_exit", Operator.RELATIVE_RANGE, "current_financial_year"),),
            sort_by=(("date_of_exit", "desc"),),
        ),
        category="Attrition",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "attrition_by_department",
        "Exits by department",
        "How many people has each team lost this financial year?",
        Definition(
            entity_key="employee",
            selections=(Selection("department"), Selection("headcount", "count", "Exits")),
            filters=(Filter("date_of_exit", Operator.RELATIVE_RANGE, "current_financial_year"),),
            group_by=("department",),
            sort_by=(("headcount", "desc"),),
        ),
        description=(
            "A count of exits, not a rate. For attrition as exits over average headcount, "
            "total and voluntary, open People analytics."
        ),
        chart_type="bar",
        category="Attrition",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "exits_by_grade",
        "Exits by grade",
        "Are we losing juniors or seniors? A very different problem either way.",
        Definition(
            entity_key="employee",
            selections=(Selection("grade"), Selection("headcount", "count", "Exits")),
            filters=(Filter("date_of_exit", Operator.RELATIVE_RANGE, "current_financial_year"),),
            group_by=("grade",),
            sort_by=(("headcount", "desc"),),
        ),
        chart_type="bar",
        category="Attrition",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "probation_due_soon",
        "Probation reviews due",
        "Whose probation ends in the next 30 days? Missing one confirms somebody by default.",
        Definition(
            entity_key="employee",
            selections=(
                Selection("employee_code"),
                Selection("employee_name"),
                Selection("department"),
                Selection("manager_name"),
                Selection("probation_end_date"),
            ),
            filters=(ACTIVE_ONLY,),
            sort_by=(("probation_end_date", "asc"),),
        ),
        category="People",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "employees_on_notice",
        "Employees on notice",
        "Who is leaving, and how much time is left to hand over?",
        Definition(
            entity_key="employee",
            selections=(
                Selection("employee_code"),
                Selection("employee_name"),
                Selection("department"),
                Selection("date_of_exit"),
                Selection("notice_period_days"),
            ),
            filters=(Filter("status", Operator.EQUALS, "on_notice"),),
            sort_by=(("date_of_exit", "asc"),),
        ),
        category="Attrition",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    # ── Payroll ─────────────────────────────────────────────────────────────
    _report(
        "salary_register",
        "Salary register",
        "The month's payroll, line by line. The document the finance team files.",
        Definition(
            entity_key="payslip",
            selections=(
                Selection("employee_code"),
                Selection("employee_name"),
                Selection("department"),
                Selection("paid_days"),
                Selection("gross_earnings"),
                Selection("total_deductions"),
                Selection("net_pay"),
            ),
            sort_by=(("employee_code", "asc"),),
            limit=1000,
        ),
        category="Payroll",
        sensitivity=Sensitivity.PAYROLL_ONLY,
    ),
    _report(
        "payroll_cost_by_department",
        "Payroll cost by department",
        "What does each team actually cost, including employer contributions?",
        Definition(
            entity_key="payslip",
            selections=(
                Selection("department"),
                Selection("employer_cost", "sum", "Total cost"),
                Selection("payslip_count", "count", "Employees"),
            ),
            group_by=("department",),
            sort_by=(("employer_cost", "desc"),),
        ),
        chart_type="bar",
        category="Payroll",
        sensitivity=Sensitivity.PAYROLL_ONLY,
    ),
    _report(
        "statutory_contribution_summary",
        "Statutory contribution summary",
        "PF, ESI, PT and TDS totals — the numbers that go on the returns.",
        Definition(
            entity_key="payslip",
            selections=(
                Selection("pf_employee", "sum", "PF (employee)"),
                Selection("pf_employer", "sum", "PF (employer)"),
                Selection("esi_employee", "sum", "ESI (employee)"),
                Selection("esi_employer", "sum", "ESI (employer)"),
                Selection("professional_tax", "sum", "Professional tax"),
                Selection("tds", "sum", "TDS"),
            ),
        ),
        category="Statutory",
        sensitivity=Sensitivity.PAYROLL_ONLY,
    ),
    _report(
        "statutory_by_state",
        "Statutory contributions by state",
        "PT and LWF are state rules. This is the split the filings need.",
        Definition(
            entity_key="payslip",
            selections=(
                Selection("state"),
                Selection("professional_tax", "sum", "Professional tax"),
                Selection("payslip_count", "count", "Employees"),
            ),
            group_by=("state",),
            sort_by=(("professional_tax", "desc"),),
        ),
        category="Statutory",
        sensitivity=Sensitivity.PAYROLL_ONLY,
    ),
    _report(
        "loss_of_pay_summary",
        "Loss of pay by department",
        "Where is unpaid absence concentrated?",
        Definition(
            entity_key="payslip",
            selections=(
                Selection("department"),
                Selection("lop_days", "sum", "LOP days"),
                Selection("payslip_count", "count", "Employees"),
            ),
            group_by=("department",),
            sort_by=(("lop_days", "desc"),),
        ),
        chart_type="bar",
        category="Payroll",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    # ── Attendance and leave ────────────────────────────────────────────────
    _report(
        "attendance_summary_last_30_days",
        "Attendance summary, last 30 days",
        "How many days did each team actually work?",
        Definition(
            entity_key="attendance_day",
            selections=(
                Selection("department"),
                Selection("attendance_status"),
                Selection("day_count", "count", "Days"),
            ),
            filters=(Filter("attendance_date", Operator.RELATIVE_RANGE, "last_30_days"),),
            group_by=("department", "attendance_status"),
        ),
        chart_type="stacked",
        category="Attendance",
    ),
    _report(
        "lateness_by_department",
        "Lateness by department",
        "Aggregate lateness across teams. Deliberately not per person — that is a "
        "disciplinary record, not a management report.",
        Definition(
            entity_key="attendance_day",
            selections=(
                Selection("department"),
                Selection("late_by_minutes", "avg", "Average minutes late"),
                Selection("day_count", "count", "Days"),
            ),
            filters=(Filter("attendance_date", Operator.RELATIVE_RANGE, "last_30_days"),),
            group_by=("department",),
            sort_by=(("late_by_minutes", "desc"),),
        ),
        chart_type="bar",
        category="Attendance",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "leave_taken_this_financial_year",
        "Leave taken this financial year",
        "How much leave has each team used since April?",
        Definition(
            entity_key="leave_request",
            selections=(
                Selection("department"),
                Selection("total_days", "sum", "Days"),
                Selection("leave_count", "count", "Requests"),
            ),
            filters=(
                Filter("leave_start", Operator.RELATIVE_RANGE, "current_financial_year"),
                Filter("leave_status", Operator.EQUALS, "approved"),
            ),
            group_by=("department",),
            sort_by=(("total_days", "desc"),),
        ),
        chart_type="bar",
        category="Leave",
    ),
    _report(
        "pending_leave_requests",
        "Pending leave requests",
        "What is waiting for a decision, and how long has it been waiting?",
        Definition(
            entity_key="leave_request",
            selections=(
                Selection("employee_name"),
                Selection("department"),
                Selection("manager_name"),
                Selection("leave_start"),
                Selection("leave_end"),
                Selection("total_days"),
            ),
            filters=(Filter("leave_status", Operator.EQUALS, "pending"),),
            sort_by=(("leave_start", "asc"),),
        ),
        category="Leave",
    ),
    # ── Performance ─────────────────────────────────────────────────────────
    _report(
        "appraisal_completion_by_department",
        "Appraisal completion by department",
        "Which teams have finished their reviews and which have not?",
        Definition(
            entity_key="appraisal",
            selections=(
                Selection("department"),
                Selection("appraisal_status"),
                Selection("appraisal_count", "count", "Appraisals"),
            ),
            group_by=("department", "appraisal_status"),
        ),
        chart_type="stacked",
        category="Performance",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "rating_distribution",
        "Rating distribution",
        "How are ratings spread? The input to any calibration conversation.",
        Definition(
            entity_key="appraisal",
            selections=(
                Selection("final_rating"),
                Selection("appraisal_count", "count", "People"),
            ),
            filters=(Filter("final_rating", Operator.IS_NOT_NULL),),
            group_by=("final_rating",),
            sort_by=(("appraisal_count", "desc"),),
        ),
        chart_type="pie",
        category="Performance",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    # ── Hiring ──────────────────────────────────────────────────────────────
    _report(
        "candidate_pipeline_by_stage",
        "Candidate pipeline by stage",
        "Where is everybody in the funnel, and where is it stuck?",
        Definition(
            entity_key="candidate",
            selections=(
                Selection("candidate_stage"),
                Selection("candidate_count", "count", "Candidates"),
            ),
            group_by=("candidate_stage",),
            sort_by=(("candidate_count", "desc"),),
        ),
        chart_type="bar",
        category="Hiring",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "candidates_by_source",
        "Candidates by source",
        "Which channels are actually producing people?",
        Definition(
            entity_key="candidate",
            selections=(
                Selection("source"),
                Selection("candidate_count", "count", "Candidates"),
            ),
            group_by=("source",),
            sort_by=(("candidate_count", "desc"),),
        ),
        chart_type="pie",
        category="Hiring",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "offer_pipeline",
        "Offer pipeline",
        "What is out, what came back, and what fell through?",
        Definition(
            entity_key="offer",
            selections=(
                Selection("offer_status"),
                Selection("offer_count", "count", "Offers"),
            ),
            group_by=("offer_status",),
            sort_by=(("offer_count", "desc"),),
        ),
        chart_type="bar",
        category="Hiring",
        sensitivity=Sensitivity.HR_ONLY,
    ),
    _report(
        "offer_drops",
        "Offer drops",
        "Who accepted and did not join? A material metric in this market.",
        Definition(
            entity_key="offer",
            selections=(
                Selection("offer_code"),
                Selection("offer_grade"),
                Selection("offer_designation"),
                Selection("joining_date"),
            ),
            filters=(Filter("offer_status", Operator.EQUALS, "dropped"),),
            sort_by=(("joining_date", "desc"),),
        ),
        category="Hiring",
        sensitivity=Sensitivity.HR_ONLY,
    ),
)


def starters_for(role: str) -> list[dict[str, Any]]:
    """The library this role can actually run.

    Offering a manager "Salary register" and refusing it when they click is worse
    than not offering it.
    """
    return [s.as_dict() for s in STARTERS if may_see(role, s.required_sensitivity)]


def validate_library() -> list[str]:
    """Self-check, run as a test.

    Every starter names real fields, groups consistently, and declares a
    sensitivity at least as high as the highest field it touches. That last one
    is the check that matters: a starter marked PUBLIC that selects a salary
    column would appear in a manager's library and fail when they ran it.
    """
    problems: list[str] = []
    seen: set[str] = set()

    for starter in STARTERS:
        if starter.key in seen:
            problems.append(f"{starter.key} is defined twice.")
        seen.add(starter.key)

        definition = starter.definition
        try:
            ent = entity(definition.entity_key)
        except Exception as exc:
            problems.append(f"{starter.key}: {exc}")
            continue

        keys = (
            [s.field_key for s in definition.selections]
            + [f.field_key for f in definition.filters]
            + list(definition.group_by)
            + [k for k, _ in definition.sort_by]
        )
        highest = Sensitivity.PUBLIC
        order = {
            Sensitivity.PUBLIC: 0,
            Sensitivity.HR_ONLY: 1,
            Sensitivity.PAYROLL_ONLY: 2,
        }
        for key in keys:
            try:
                spec = ent.field(key)
            except Exception as exc:
                problems.append(f"{starter.key}: {exc}")
                continue
            if order[spec.sensitivity] > order[highest]:
                highest = spec.sensitivity

        if order[starter.required_sensitivity] < order[highest]:
            problems.append(
                f"{starter.key} is marked {starter.required_sensitivity.value} but "
                f"touches a {highest.value} field. It would appear in a library the "
                "user cannot actually run it from."
            )

        if definition.group_by:
            for selection in definition.selections:
                if selection.field_key not in definition.group_by and selection.aggregation is None:
                    problems.append(
                        f"{starter.key}: '{selection.field_key}' is neither grouped nor aggregated."
                    )

        for selection in definition.selections:
            if selection.aggregation is None:
                continue
            try:
                spec = ent.field(selection.field_key)
            except Exception:
                continue
            if selection.aggregation not in spec.allowed_aggregations:
                problems.append(
                    f"{starter.key}: '{selection.aggregation}' is not allowed on "
                    f"{selection.field_key} ({spec.data_type.value})."
                )
    return problems
