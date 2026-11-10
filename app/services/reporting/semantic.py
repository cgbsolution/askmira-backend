"""The semantic layer. Nothing above this ever names a table or a column.

═══════════════════════════════════════════════════════════════════════════════
WHY THE CATALOGUE IS THE SECURITY BOUNDARY
═══════════════════════════════════════════════════════════════════════════════

A report builder that takes table and column names from a request is a SQL
console with a nicer form. The whole safety argument here rests on one thing:
**every identifier in a generated query comes from this catalogue, and a key
that is not in it is refused.** Values are bound parameters, so they cannot be
identifiers; identifiers are catalogue lookups, so they cannot be values. There
is no third category for user input to live in.

**Sensitivity is enforced at query construction, not at render.** The difference
matters enormously. A manager who can select `employee.ctc_annual` and have it
hidden in the UI has still caused the database to return it, into a response, in
a payload they can read with a browser's network tab. Restricted fields are
removed before the SQL exists — and a report *filtered* on a restricted field is
refused too, because `WHERE ctc_annual > 2000000` leaks the answer through the
row count without ever selecting the column.

**Join paths are declared, not inferred.** Letting the builder find a route from
`employee` to `employee_salaries` means it will also find a route from
`attendance_day` to salary through employee, and a manager reading an attendance
report would reach payroll. Every join a field needs is spelled out here and the
compiler will not invent one.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "AGGREGATIONS",
    "ENTITIES",
    "DataType",
    "Entity",
    "Field",
    "JoinSpec",
    "SemanticError",
    "Sensitivity",
    "entity",
    "field_for",
    "visible_fields",
]


class SemanticError(ValueError):
    """A definition that names something the catalogue does not have."""


class Sensitivity(str, enum.Enum):
    """Who may see a field at all.

    Ordered by how much damage the field does in the wrong hands, and checked
    against the requester's role before any SQL is built.
    """

    #: Anyone in the tenant. Names, departments, joining dates.
    PUBLIC = "public"
    #: HR and admins. Exit dates, appraisal ratings, leave reasons.
    HR_ONLY = "hr_only"
    #: Payroll and admins. Salary, PF numbers, bank details, tax.
    PAYROLL_ONLY = "payroll_only"


class DataType(str, enum.Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    ENUM = "enum"


#: Aggregations allowed per data type. A `sum` over a date is meaningless and a
#: builder that offered it would produce a number somebody would try to read.
AGGREGATIONS: dict[DataType, tuple[str, ...]] = {
    DataType.STRING: ("count", "count_distinct"),
    DataType.ENUM: ("count", "count_distinct"),
    DataType.BOOLEAN: ("count", "count_distinct"),
    DataType.INTEGER: ("count", "count_distinct", "sum", "avg", "min", "max"),
    DataType.DECIMAL: ("count", "count_distinct", "sum", "avg", "min", "max"),
    DataType.DATE: ("count", "count_distinct", "min", "max"),
    DataType.DATETIME: ("count", "count_distinct", "min", "max"),
}


@dataclass(frozen=True, slots=True)
class JoinSpec:
    """One hop, declared explicitly.

    `table` is the table to join, `local_column` and `remote_column` the two
    sides. Always a LEFT join: an employee with no department must still appear
    in a headcount report, with a blank department, rather than vanishing from
    the count.
    """

    table: str
    local_table: str
    local_column: str
    remote_column: str = "id"
    #: A short name so two joins to the same table (manager and employee) do not
    #: collide.
    alias: str = ""

    @property
    def alias_name(self) -> str:
        return self.alias or self.table


@dataclass(frozen=True, slots=True)
class Field:
    """One selectable thing.

    A field is a dimension, a measure, or both. `employee.grade` groups; `net_pay`
    aggregates; `date_of_joining` does both — group by it and you get a hiring
    timeline, take its `min` and you get the earliest joiner.
    """

    key: str
    display_name: str
    table: str
    column: str
    data_type: DataType
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    is_dimension: bool = True
    is_measure: bool = False
    joins: tuple[JoinSpec, ...] = ()
    #: What this field means, shown in the builder. A field called
    #: `payable_fraction` needs a sentence or it will be used wrongly.
    description: str = ""
    #: How a value reads: "inr", "days", "hours", "minutes", "count" or "".
    unit: str = ""
    #: A month stored as two integer columns (year, month) on `table`: the
    #: field's value is the 1st of that month. `column` names the year column.
    month_from: tuple[str, str] | None = None

    @property
    def allowed_aggregations(self) -> tuple[str, ...]:
        return AGGREGATIONS.get(self.data_type, ()) if self.is_measure else ()


@dataclass(frozen=True, slots=True)
class Entity:
    """One thing a report can be about."""

    key: str
    display_name: str
    base_table: str
    #: The column on `base_table` that identifies the employee a row is about.
    #: Row-level scope needs it: without one, a manager's reporting-tree filter
    #: has nothing to attach to, and the entity is refused for scoped roles
    #: rather than silently returning everything.
    employee_column: str | None = None
    fields: tuple[Field, ...] = ()
    description: str = ""
    #: Filters every query of this entity carries, as (field key, operator,
    #: value): a cancelled payroll run is not payroll. They only ever narrow.
    always: tuple[tuple[str, str, Any], ...] = ()
    #: The date a trend of this entity runs along, unless asked otherwise.
    default_date: str | None = None
    #: The field whose distinct count is the number of people in a group.
    people_field: str | None = None
    #: Whether a row is about a person. A group of rows about fewer than five
    #: people is never read by a model (companion/data_tools.py).
    personal: bool = True

    def field(self, key: str) -> Field:
        for candidate in self.fields:
            if candidate.key == key:
                return candidate
        raise SemanticError(
            f"'{key}' is not a field on {self.display_name}. Available: "
            f"{sorted(f.key for f in self.fields)}."
        )


# ═══════════════════════════════════════════════════════════════════════════
# The catalogue
# ═══════════════════════════════════════════════════════════════════════════

_DEPARTMENT_JOIN = JoinSpec(
    table="departments", local_table="employees", local_column="department_id"
)
_LOCATION_JOIN = JoinSpec(table="locations", local_table="employees", local_column="location_id")
_PROJECT_JOIN = JoinSpec(table="projects", local_table="employees", local_column="project_id")
_MANAGER_JOIN = JoinSpec(
    table="employees", local_table="employees", local_column="manager_id", alias="manager"
)
_COMPANY_JOIN = JoinSpec(
    table="legal_entities",
    local_table="employees",
    local_column="legal_entity_id",
    alias="employee_company",
)


def _employee_dimensions(via: tuple[JoinSpec, ...] = ()) -> list[Field]:
    """Employee attributes, reusable from entities that join to employees.

    Written once because the sensitivity of `pan` must not depend on which report
    somebody reached it through.
    """
    return [
        Field(
            "employee_code",
            "Employee code",
            "employees",
            "employee_code",
            DataType.STRING,
            joins=via,
        ),
        Field("employee_name", "Employee", "employees", "display_name", DataType.STRING, joins=via),
        Field(
            "work_email",
            "Work email",
            "employees",
            "work_email",
            DataType.STRING,
            Sensitivity.HR_ONLY,
            joins=via,
        ),
        Field("designation", "Designation", "employees", "designation", DataType.STRING, joins=via),
        Field("grade", "Grade", "employees", "grade", DataType.STRING, joins=via),
        Field(
            "employment_type",
            "Employment type",
            "employees",
            "employment_type",
            DataType.ENUM,
            joins=via,
        ),
        Field("status", "Status", "employees", "status", DataType.ENUM, joins=via),
        Field(
            "gender",
            "Gender",
            "employees",
            "gender",
            DataType.ENUM,
            Sensitivity.HR_ONLY,
            joins=via,
            description="Restricted: useful for diversity reporting, not for operational lists.",
        ),
        Field(
            "date_of_joining",
            "Date of joining",
            "employees",
            "date_of_joining",
            DataType.DATE,
            is_measure=True,
            joins=via,
        ),
        Field(
            "date_of_exit",
            "Date of exit",
            "employees",
            "date_of_exit",
            DataType.DATE,
            Sensitivity.HR_ONLY,
            is_measure=True,
            joins=via,
        ),
        Field(
            "department",
            "Department",
            "departments",
            "name",
            DataType.STRING,
            joins=(*via, _DEPARTMENT_JOIN),
        ),
        Field(
            "location",
            "Location",
            "locations",
            "name",
            DataType.STRING,
            joins=(*via, _LOCATION_JOIN),
        ),
        Field(
            "project",
            "Project",
            "projects",
            "name",
            DataType.STRING,
            joins=(*via, _PROJECT_JOIN),
        ),
        Field(
            "project_client",
            "Project client",
            "projects",
            "client",
            DataType.STRING,
            joins=(*via, _PROJECT_JOIN),
        ),
        Field(
            "company",
            "Company",
            "employee_company",
            "name",
            DataType.STRING,
            joins=(*via, _COMPANY_JOIN),
            description="The legal entity (company) the employee belongs to.",
        ),
        Field(
            "state",
            "State",
            "locations",
            "state_code",
            DataType.STRING,
            joins=(*via, _LOCATION_JOIN),
        ),
        Field(
            "manager_name",
            "Manager",
            "manager",
            "display_name",
            DataType.STRING,
            joins=(*via, _MANAGER_JOIN),
        ),
        # Statutory identifiers. PAYROLL_ONLY, and the reason is worth stating:
        # a PAN or a bank account in a shared report is an identity-theft
        # payload, not a data point.
        Field(
            "pan",
            "PAN",
            "employees",
            "pan",
            DataType.STRING,
            Sensitivity.PAYROLL_ONLY,
            joins=via,
            description="Restricted. A PAN in a shared report is an identity-theft payload.",
        ),
        Field(
            "uan", "UAN", "employees", "uan", DataType.STRING, Sensitivity.PAYROLL_ONLY, joins=via
        ),
        Field(
            "bank_account_number",
            "Bank account",
            "employees",
            "bank_account_number",
            DataType.STRING,
            Sensitivity.PAYROLL_ONLY,
            joins=via,
        ),
        Field(
            "headcount",
            "Headcount",
            "employees",
            "id",
            DataType.STRING,
            is_dimension=False,
            is_measure=True,
            joins=via,
            description="Count of employees. Use with count or count_distinct.",
        ),
    ]


_EMPLOYEE = Entity(
    key="employee",
    display_name="Employees",
    base_table="employees",
    employee_column="id",
    description="One row per employee.",
    fields=tuple(
        [
            *_employee_dimensions(),
            Field(
                "notice_period_days",
                "Notice period (days)",
                "employees",
                "notice_period_days",
                DataType.INTEGER,
                Sensitivity.HR_ONLY,
                is_measure=True,
            ),
            Field(
                "probation_end_date",
                "Probation end",
                "employees",
                "probation_end_date",
                DataType.DATE,
                Sensitivity.HR_ONLY,
                is_measure=True,
            ),
            # Salary reached from the employee entity is PAYROLL_ONLY, and the
            # join is declared here rather than discovered. This is the exact
            # path the checklist gate is about.
            Field(
                "ctc_annual",
                "Annual CTC",
                "employee_salaries",
                "ctc_annual",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
                joins=(
                    JoinSpec(
                        table="employee_salaries",
                        local_table="employees",
                        local_column="id",
                        remote_column="employee_id",
                    ),
                ),
                description="Restricted. The live salary row only; superseded "
                "revisions are excluded by the entity's own filter.",
            ),
        ]
    ),
)

_PAYSLIP = Entity(
    key="payslip",
    display_name="Payslips",
    base_table="payslips",
    employee_column="employee_id",
    description="One row per employee per payroll run.",
    fields=tuple(
        [
            *_employee_dimensions(
                via=(
                    JoinSpec(
                        table="employees",
                        local_table="payslips",
                        local_column="employee_id",
                    ),
                )
            ),
            Field(
                "gross_earnings",
                "Gross earnings",
                "payslips",
                "gross_earnings",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "total_deductions",
                "Total deductions",
                "payslips",
                "total_deductions",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "net_pay",
                "Net pay",
                "payslips",
                "net_pay",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "employer_cost",
                "Employer cost",
                "payslips",
                "employer_cost",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "pf_employee",
                "PF (employee)",
                "payslips",
                "pf_employee",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "pf_employer",
                "PF (employer)",
                "payslips",
                "pf_employer",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "esi_employee",
                "ESI (employee)",
                "payslips",
                "esi_employee",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "esi_employer",
                "ESI (employer)",
                "payslips",
                "esi_employer",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "professional_tax",
                "Professional tax",
                "payslips",
                "professional_tax",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "tds",
                "TDS",
                "payslips",
                "tds",
                DataType.DECIMAL,
                Sensitivity.PAYROLL_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "lop_days",
                "Loss-of-pay days",
                "payslips",
                "lop_days",
                DataType.DECIMAL,
                Sensitivity.HR_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "paid_days",
                "Paid days",
                "payslips",
                "paid_days",
                DataType.DECIMAL,
                Sensitivity.HR_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "is_published",
                "Published",
                "payslips",
                "is_published",
                DataType.BOOLEAN,
                Sensitivity.HR_ONLY,
            ),
            Field(
                "payslip_count",
                "Payslips",
                "payslips",
                "id",
                DataType.STRING,
                Sensitivity.HR_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
        ]
    ),
)

_ATTENDANCE = Entity(
    key="attendance_day",
    display_name="Attendance",
    base_table="attendance_days",
    employee_column="employee_id",
    description="One row per employee per day.",
    fields=tuple(
        [
            *_employee_dimensions(
                via=(
                    JoinSpec(
                        table="employees",
                        local_table="attendance_days",
                        local_column="employee_id",
                    ),
                )
            ),
            Field(
                "attendance_date",
                "Date",
                "attendance_days",
                "attendance_date",
                DataType.DATE,
                is_measure=True,
            ),
            Field(
                "attendance_status", "Attendance status", "attendance_days", "status", DataType.ENUM
            ),
            Field(
                "worked_minutes",
                "Minutes worked",
                "attendance_days",
                "worked_minutes",
                DataType.INTEGER,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "late_by_minutes",
                "Late by (minutes)",
                "attendance_days",
                "late_by_minutes",
                DataType.INTEGER,
                Sensitivity.HR_ONLY,
                is_dimension=False,
                is_measure=True,
                description="Restricted. Lateness aggregated across a team is a "
                "management tool; per person it is a disciplinary "
                "record.",
            ),
            Field(
                "payable_fraction",
                "Payable fraction",
                "attendance_days",
                "payable_fraction",
                DataType.DECIMAL,
                Sensitivity.HR_ONLY,
                is_dimension=False,
                is_measure=True,
                description="1.0 is a full paid day, 0.5 a half day, 0 unpaid.",
            ),
            Field(
                "is_regularised",
                "Regularised",
                "attendance_days",
                "is_regularised",
                DataType.BOOLEAN,
                Sensitivity.HR_ONLY,
            ),
            Field(
                "day_count",
                "Days",
                "attendance_days",
                "id",
                DataType.STRING,
                is_dimension=False,
                is_measure=True,
            ),
        ]
    ),
)

_LEAVE = Entity(
    key="leave_request",
    display_name="Leave requests",
    base_table="leave_requests",
    employee_column="employee_id",
    description="One row per leave request.",
    fields=tuple(
        [
            *_employee_dimensions(
                via=(
                    JoinSpec(
                        table="employees",
                        local_table="leave_requests",
                        local_column="employee_id",
                    ),
                )
            ),
            Field(
                "leave_start",
                "Start date",
                "leave_requests",
                "start_date",
                DataType.DATE,
                is_measure=True,
            ),
            Field(
                "leave_end",
                "End date",
                "leave_requests",
                "end_date",
                DataType.DATE,
                is_measure=True,
            ),
            Field("leave_status", "Status", "leave_requests", "status", DataType.ENUM),
            Field(
                "total_days",
                "Days",
                "leave_requests",
                "total_days",
                DataType.DECIMAL,
                is_dimension=False,
                is_measure=True,
            ),
            # The reason somebody took leave is frequently medical, so HR_ONLY is
            # the floor. It stays a dimension because HR genuinely needs to read
            # it in a detail table — but grouping by free text produces one group
            # per row, so the description says not to.
            Field(
                "leave_reason",
                "Reason",
                "leave_requests",
                "reason",
                DataType.STRING,
                Sensitivity.HR_ONLY,
                description="Restricted. Leave reasons are frequently medical. "
                "Free text: read it in a table, do not group by it.",
            ),
            Field(
                "leave_count",
                "Requests",
                "leave_requests",
                "id",
                DataType.STRING,
                is_dimension=False,
                is_measure=True,
            ),
        ]
    ),
)

_APPRAISAL = Entity(
    key="appraisal",
    display_name="Appraisals",
    base_table="appraisals",
    employee_column="employee_id",
    description="One row per employee per review cycle.",
    fields=tuple(
        [
            *_employee_dimensions(
                via=(
                    JoinSpec(
                        table="employees",
                        local_table="appraisals",
                        local_column="employee_id",
                    ),
                )
            ),
            Field(
                "appraisal_status",
                "Status",
                "appraisals",
                "status",
                DataType.ENUM,
                Sensitivity.HR_ONLY,
            ),
            Field(
                "final_rating",
                "Final rating",
                "appraisals",
                "final_rating",
                DataType.STRING,
                Sensitivity.HR_ONLY,
            ),
            Field(
                "final_score",
                "Final score",
                "appraisals",
                "final_score",
                DataType.DECIMAL,
                Sensitivity.HR_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "reopened_count",
                "Times reopened",
                "appraisals",
                "reopened_count",
                DataType.INTEGER,
                Sensitivity.HR_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
            Field(
                "completed_at",
                "Completed",
                "appraisals",
                "completed_at",
                DataType.DATETIME,
                Sensitivity.HR_ONLY,
                is_measure=True,
            ),
            Field(
                "appraisal_count",
                "Appraisals",
                "appraisals",
                "id",
                DataType.STRING,
                Sensitivity.HR_ONLY,
                is_dimension=False,
                is_measure=True,
            ),
        ]
    ),
)

_CANDIDATE = Entity(
    key="candidate",
    display_name="Candidates",
    base_table="candidates",
    # No employee column: a candidate is not an employee, so the reporting-tree
    # scope has nothing to attach to and the entity is HR-only by construction.
    employee_column=None,
    description="One row per candidate. HR and recruiters only.",
    fields=(
        Field(
            "candidate_name",
            "Candidate",
            "candidates",
            "full_name",
            DataType.STRING,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "candidate_email", "Email", "candidates", "email", DataType.STRING, Sensitivity.HR_ONLY
        ),
        Field(
            "candidate_stage", "Stage", "candidates", "stage", DataType.ENUM, Sensitivity.HR_ONLY
        ),
        Field("source", "Source", "candidates", "source", DataType.STRING, Sensitivity.HR_ONLY),
        Field(
            "current_company",
            "Current company",
            "candidates",
            "current_company",
            DataType.STRING,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "experience_years",
            "Experience (years)",
            "candidates",
            "total_experience_years",
            DataType.DECIMAL,
            Sensitivity.HR_ONLY,
            is_dimension=False,
            is_measure=True,
        ),
        Field(
            "expected_ctc",
            "Expected CTC",
            "candidates",
            "expected_ctc",
            DataType.DECIMAL,
            Sensitivity.PAYROLL_ONLY,
            is_dimension=False,
            is_measure=True,
        ),
        Field(
            "notice_period_days",
            "Notice period (days)",
            "candidates",
            "notice_period_days",
            DataType.INTEGER,
            Sensitivity.HR_ONLY,
            is_dimension=False,
            is_measure=True,
        ),
        Field(
            "fit_score",
            "Fit score",
            "candidates",
            "fit_score",
            DataType.DECIMAL,
            Sensitivity.HR_ONLY,
            is_dimension=False,
            is_measure=True,
            description="An AI score. Never read it without the interview "
            "signals behind it (FS-ANL-02).",
        ),
        Field(
            "applied_on",
            "Applied on",
            "candidates",
            "created_at",
            DataType.DATETIME,
            Sensitivity.HR_ONLY,
            is_measure=True,
        ),
        Field(
            "candidate_count",
            "Candidates",
            "candidates",
            "id",
            DataType.STRING,
            Sensitivity.HR_ONLY,
            is_dimension=False,
            is_measure=True,
        ),
    ),
)

_OFFER = Entity(
    key="offer",
    display_name="Offers",
    base_table="offers",
    employee_column=None,
    description="One row per offer.",
    fields=(
        Field("offer_code", "Offer", "offers", "code", DataType.STRING, Sensitivity.HR_ONLY),
        Field("offer_status", "Status", "offers", "status", DataType.ENUM, Sensitivity.HR_ONLY),
        Field("offer_grade", "Grade", "offers", "grade", DataType.STRING, Sensitivity.HR_ONLY),
        Field(
            "offer_designation",
            "Designation",
            "offers",
            "designation",
            DataType.STRING,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "offer_ctc",
            "Offered CTC",
            "offers",
            "ctc_annual",
            DataType.DECIMAL,
            Sensitivity.PAYROLL_ONLY,
            is_dimension=False,
            is_measure=True,
        ),
        Field(
            "joining_date",
            "Joining date",
            "offers",
            "joining_date",
            DataType.DATE,
            Sensitivity.HR_ONLY,
            is_measure=True,
        ),
        Field(
            "released_at",
            "Released",
            "offers",
            "released_at",
            DataType.DATETIME,
            Sensitivity.HR_ONLY,
            is_measure=True,
        ),
        Field(
            "resigned_elsewhere",
            "Resigned elsewhere",
            "offers",
            "resigned_elsewhere",
            DataType.BOOLEAN,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "offer_count",
            "Offers",
            "offers",
            "id",
            DataType.STRING,
            Sensitivity.HR_ONLY,
            is_dimension=False,
            is_measure=True,
        ),
    ),
)

# ── More of the business: built from the same parts ─────────────────────────


def _via(table: str, column: str, *, alias: str = "") -> JoinSpec:
    """The employee a row is about, joined from `table.column`."""
    return JoinSpec(table="employees", local_table=table, local_column=column, alias=alias)


def _count(key: str, name: str, table: str, sensitivity: Sensitivity = Sensitivity.PUBLIC) -> Field:
    return Field(
        key,
        name,
        table,
        "id",
        DataType.STRING,
        sensitivity,
        is_dimension=False,
        is_measure=True,
        unit="count",
        description=f"Count of {name.lower()}. Use with count.",
    )


def _money(
    key: str,
    name: str,
    table: str,
    column: str,
    sensitivity: Sensitivity,
    joins: tuple[JoinSpec, ...] = (),
) -> Field:
    return Field(
        key,
        name,
        table,
        column,
        DataType.DECIMAL,
        sensitivity,
        is_dimension=False,
        is_measure=True,
        joins=joins,
        unit="inr",
    )


_RUN_JOIN = JoinSpec(table="payroll_runs", local_table="payslips", local_column="run_id")
_RUN_COMPANY_JOIN = JoinSpec(
    table="legal_entities",
    local_table="payroll_runs",
    local_column="legal_entity_id",
    alias="run_company",
)

_PAYSLIP = replace(
    _PAYSLIP,
    description=(
        "One row per employee per payroll run. Payroll cost is employer_cost (cost to "
        "company); cancelled runs are left out."
    ),
    fields=(
        *_PAYSLIP.fields,
        Field(
            "pay_month",
            "Pay month",
            "payroll_runs",
            "period_year",
            DataType.DATE,
            is_measure=True,
            joins=(_RUN_JOIN,),
            month_from=("period_year", "period_month"),
            description="The month the payroll run pays for. Group it by month for a trend.",
        ),
        Field(
            "run_type", "Run type", "payroll_runs", "run_type", DataType.ENUM, joins=(_RUN_JOIN,)
        ),
        Field(
            "run_status",
            "Run status",
            "payroll_runs",
            "status",
            DataType.ENUM,
            joins=(_RUN_JOIN,),
        ),
        Field(
            "paid_by_company",
            "Paid by company",
            "run_company",
            "name",
            DataType.STRING,
            joins=(_RUN_JOIN, _RUN_COMPANY_JOIN),
            description="The legal entity whose payroll run paid this.",
        ),
    ),
    always=(("run_status", "not_equals", "cancelled"),),
    default_date="pay_month",
    people_field="headcount",
)

_LEAVE_TYPE_JOIN = JoinSpec(
    table="leave_types", local_table="leave_requests", local_column="leave_type_id"
)
_LEAVE = replace(
    _LEAVE,
    fields=(
        *_LEAVE.fields,
        Field(
            "leave_type",
            "Leave type",
            "leave_types",
            "name",
            DataType.STRING,
            joins=(_LEAVE_TYPE_JOIN,),
        ),
        Field(
            "leave_is_paid",
            "Paid leave",
            "leave_types",
            "is_paid",
            DataType.BOOLEAN,
            joins=(_LEAVE_TYPE_JOIN,),
        ),
    ),
    default_date="leave_start",
    people_field="headcount",
)
_EMPLOYEE = replace(_EMPLOYEE, default_date="date_of_joining", people_field="headcount")
_ATTENDANCE = replace(_ATTENDANCE, default_date="attendance_date", people_field="headcount")
_APPRAISAL = replace(_APPRAISAL, default_date="completed_at", people_field="headcount")
_CANDIDATE = replace(_CANDIDATE, default_date="applied_on", people_field="candidate_count")
_OFFER = replace(_OFFER, default_date="released_at", people_field="offer_count")


_EXPENSE_CLAIM = Entity(
    key="expense_claim",
    display_name="Expense claims",
    base_table="expense_claims",
    employee_column="employee_id",
    description="One row per expense claim an employee made in AskMiraa.",
    default_date="incurred_on",
    people_field="headcount",
    always=(("claim_status", "not_equals", "cancelled"),),
    fields=(
        *_employee_dimensions(via=(_via("expense_claims", "employee_id"),)),
        Field(
            "expense_category",
            "Expense category",
            "expense_claims",
            "category_key",
            DataType.STRING,
        ),
        Field("claim_kind", "Claim kind", "expense_claims", "kind", DataType.ENUM),
        Field("claim_status", "Claim status", "expense_claims", "status", DataType.ENUM),
        Field(
            "incurred_on",
            "Incurred on",
            "expense_claims",
            "incurred_on",
            DataType.DATE,
            is_measure=True,
        ),
        Field(
            "submitted_at",
            "Submitted",
            "expense_claims",
            "submitted_at",
            DataType.DATETIME,
            is_measure=True,
        ),
        Field("policy_verdict", "Policy check", "expense_claims", "policy_verdict", DataType.ENUM),
        _money("claim_amount", "Amount claimed", "expense_claims", "amount", Sensitivity.PUBLIC),
        _money(
            "overage", "Over the policy limit", "expense_claims", "overage", Sensitivity.HR_ONLY
        ),
        _count("claim_count", "Claims", "expense_claims"),
    ),
)

_TICKET_CATEGORY_JOIN = JoinSpec(
    table="helpdesk_categories", local_table="helpdesk_tickets", local_column="category_id"
)
_HELPDESK_TICKET = Entity(
    key="helpdesk_ticket",
    display_name="HR helpdesk tickets",
    base_table="helpdesk_tickets",
    employee_column="employee_id",
    description=(
        "One row per ticket raised with HR in AskMiraa's helpdesk. Confidential tickets are "
        "never counted here."
    ),
    default_date="raised_on",
    people_field="headcount",
    always=(("is_confidential", "equals", False),),
    fields=(
        *_employee_dimensions(via=(_via("helpdesk_tickets", "employee_id"),)),
        Field(
            "ticket_category",
            "Category",
            "helpdesk_categories",
            "name",
            DataType.STRING,
            Sensitivity.HR_ONLY,
            joins=(_TICKET_CATEGORY_JOIN,),
        ),
        Field(
            "ticket_status",
            "Ticket status",
            "helpdesk_tickets",
            "status",
            DataType.ENUM,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "ticket_priority",
            "Priority",
            "helpdesk_tickets",
            "priority",
            DataType.ENUM,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "is_confidential",
            "Confidential",
            "helpdesk_tickets",
            "is_confidential",
            DataType.BOOLEAN,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "raised_on",
            "Raised on",
            "helpdesk_tickets",
            "created_at",
            DataType.DATETIME,
            Sensitivity.HR_ONLY,
            is_measure=True,
        ),
        Field(
            "resolved_at",
            "Resolved on",
            "helpdesk_tickets",
            "resolved_at",
            DataType.DATETIME,
            Sensitivity.HR_ONLY,
            is_measure=True,
        ),
        Field(
            "reopened_count",
            "Times reopened",
            "helpdesk_tickets",
            "reopened_count",
            DataType.INTEGER,
            Sensitivity.HR_ONLY,
            is_dimension=False,
            is_measure=True,
        ),
        Field(
            "satisfaction_rating",
            "Satisfaction (1-5)",
            "helpdesk_tickets",
            "satisfaction_rating",
            DataType.INTEGER,
            Sensitivity.HR_ONLY,
            is_dimension=False,
            is_measure=True,
        ),
        _count("ticket_count", "Tickets", "helpdesk_tickets"),
    ),
)

_ASSET = Entity(
    key="asset",
    display_name="Company assets",
    base_table="assets",
    employee_column=None,
    personal=False,
    description="One row per company asset (laptop, phone, ...).",
    default_date="purchase_date",
    always=(),
    fields=(
        Field(
            "asset_category",
            "Asset category",
            "asset_categories",
            "name",
            DataType.STRING,
            joins=(
                JoinSpec(
                    table="asset_categories", local_table="assets", local_column="category_id"
                ),
            ),
        ),
        Field(
            "asset_location",
            "Asset location",
            "asset_locations",
            "name",
            DataType.STRING,
            joins=(
                JoinSpec(
                    table="locations",
                    local_table="assets",
                    local_column="location_id",
                    alias="asset_locations",
                ),
            ),
        ),
        Field("asset_status", "Asset status", "assets", "status", DataType.ENUM),
        Field("asset_condition", "Condition", "assets", "condition", DataType.ENUM),
        Field("make", "Make", "assets", "make", DataType.STRING),
        Field(
            "purchase_date",
            "Purchased on",
            "assets",
            "purchase_date",
            DataType.DATE,
            is_measure=True,
        ),
        Field(
            "warranty_until",
            "Warranty until",
            "assets",
            "warranty_until",
            DataType.DATE,
            is_measure=True,
        ),
        _money("purchase_cost", "Purchase cost", "assets", "purchase_cost", Sensitivity.HR_ONLY),
        _count("asset_count", "Assets", "assets"),
    ),
)

_TIMESHEET_JOIN = JoinSpec(
    table="timesheets", local_table="timesheet_entries", local_column="timesheet_id"
)
_TIMESHEET_ENTRY = Entity(
    key="timesheet_entry",
    display_name="Timesheet hours",
    base_table="timesheet_entries",
    employee_column=None,
    description="One row per day's hours on a project in a weekly timesheet.",
    default_date="work_date",
    people_field="headcount",
    fields=(
        *_employee_dimensions(via=(_TIMESHEET_JOIN, _via("timesheets", "employee_id"))),
        Field(
            "work_date",
            "Work date",
            "timesheet_entries",
            "work_date",
            DataType.DATE,
            is_measure=True,
        ),
        Field(
            "timesheet_project",
            "Project worked on",
            "timesheet_projects",
            "name",
            DataType.STRING,
            joins=(
                JoinSpec(
                    table="projects",
                    local_table="timesheet_entries",
                    local_column="project_id",
                    alias="timesheet_projects",
                ),
            ),
        ),
        Field(
            "timesheet_status",
            "Timesheet status",
            "timesheets",
            "status",
            DataType.ENUM,
            joins=(_TIMESHEET_JOIN,),
        ),
        Field(
            "hours",
            "Hours",
            "timesheet_entries",
            "hours",
            DataType.DECIMAL,
            is_dimension=False,
            is_measure=True,
            unit="hours",
        ),
        _count("entry_count", "Timesheet entries", "timesheet_entries"),
    ),
)

_EXIT_RECORD = Entity(
    key="exit_record",
    display_name="Exits",
    base_table="exit_records",
    employee_column="employee_id",
    description="One row per resignation, termination or other exit; withdrawn exits are left out.",
    default_date="last_working_day",
    people_field="headcount",
    always=(("exit_status", "not_equals", "cancelled"),),
    fields=(
        *_employee_dimensions(via=(_via("exit_records", "employee_id"),)),
        Field(
            "exit_type",
            "Exit type",
            "exit_records",
            "exit_type",
            DataType.ENUM,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "exit_status",
            "Exit status",
            "exit_records",
            "status",
            DataType.ENUM,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "resignation_date",
            "Resigned on",
            "exit_records",
            "resignation_date",
            DataType.DATE,
            Sensitivity.HR_ONLY,
            is_measure=True,
        ),
        Field(
            "last_working_day",
            "Last working day",
            "exit_records",
            "approved_last_working_day",
            DataType.DATE,
            Sensitivity.HR_ONLY,
            is_measure=True,
        ),
        Field(
            "notice_shortfall_days",
            "Notice shortfall (days)",
            "exit_records",
            "notice_shortfall_days",
            DataType.INTEGER,
            Sensitivity.HR_ONLY,
            is_dimension=False,
            is_measure=True,
            unit="days",
        ),
        Field(
            "rehire_eligible",
            "Eligible for rehire",
            "exit_records",
            "is_rehire_eligible",
            DataType.BOOLEAN,
            Sensitivity.HR_ONLY,
        ),
        Field(
            "initiated_by_employee",
            "Initiated by the employee",
            "exit_records",
            "initiated_by_employee",
            DataType.BOOLEAN,
            Sensitivity.HR_ONLY,
        ),
        _count("exit_count", "Exits", "exit_records", Sensitivity.HR_ONLY),
    ),
)

_SHOUTOUT = Entity(
    key="shoutout",
    display_name="Recognition (shoutouts)",
    base_table="shoutouts",
    employee_column="to_employee_id",
    description="One row per shoutout received; private shoutouts are left out.",
    default_date="given_on",
    people_field="headcount",
    always=(("shoutout_visibility", "not_equals", "private"),),
    fields=(
        *_employee_dimensions(via=(_via("shoutouts", "to_employee_id"),)),
        Field("value_tag", "Company value", "shoutouts", "value_tag", DataType.STRING),
        Field("shoutout_visibility", "Visibility", "shoutouts", "visibility", DataType.ENUM),
        Field(
            "given_on", "Given on", "shoutouts", "created_at", DataType.DATETIME, is_measure=True
        ),
        Field(
            "points_awarded",
            "Points",
            "shoutouts",
            "points_awarded",
            DataType.INTEGER,
            is_dimension=False,
            is_measure=True,
        ),
        _count("shoutout_count", "Shoutouts", "shoutouts"),
    ),
)

_ACCOUNT_JOIN = JoinSpec(
    table="client_accounts", local_table="account_entries", local_column="account_id"
)
_ACCOUNT_ENTRY = Entity(
    key="account_entry",
    display_name="Client account billing and costs",
    base_table="account_entries",
    employee_column=None,
    personal=False,
    description=(
        "What was billed to each client account, and its other direct costs, by month "
        "(Payroll → Account P&L). kind is billing or cost."
    ),
    default_date="entry_month",
    fields=(
        Field(
            "account", "Account", "client_accounts", "name", DataType.STRING, joins=(_ACCOUNT_JOIN,)
        ),
        Field(
            "account_code",
            "Account code",
            "client_accounts",
            "code",
            DataType.STRING,
            joins=(_ACCOUNT_JOIN,),
        ),
        Field(
            "client",
            "Client",
            "client_accounts",
            "client_name",
            DataType.STRING,
            joins=(_ACCOUNT_JOIN,),
        ),
        Field(
            "account_company",
            "Company",
            "account_company",
            "name",
            DataType.STRING,
            joins=(
                _ACCOUNT_JOIN,
                JoinSpec(
                    table="legal_entities",
                    local_table="client_accounts",
                    local_column="legal_entity_id",
                    alias="account_company",
                ),
            ),
        ),
        Field("entry_kind", "Billing or cost", "account_entries", "kind", DataType.STRING),
        Field(
            "entry_month",
            "Month",
            "account_entries",
            "period_year",
            DataType.DATE,
            is_measure=True,
            month_from=("period_year", "period_month"),
        ),
        _money("entry_amount", "Amount", "account_entries", "amount", Sensitivity.PAYROLL_ONLY),
        _count("entry_count", "Entries", "account_entries", Sensitivity.HR_ONLY),
    ),
)

_COURSE_ASSIGNMENT = Entity(
    key="course_assignment",
    display_name="Learning (course assignments)",
    base_table="course_assignments",
    employee_column="employee_id",
    description="One row per course assigned to an employee.",
    default_date="due_on",
    people_field="headcount",
    always=(("course_status", "not_equals", "cancelled"),),
    fields=(
        *_employee_dimensions(via=(_via("course_assignments", "employee_id"),)),
        Field(
            "course",
            "Course",
            "courses",
            "title",
            DataType.STRING,
            joins=(
                JoinSpec(
                    table="courses", local_table="course_assignments", local_column="course_id"
                ),
            ),
        ),
        Field("course_status", "Course status", "course_assignments", "status", DataType.ENUM),
        Field("due_on", "Due on", "course_assignments", "due_on", DataType.DATE, is_measure=True),
        Field(
            "course_completed_at",
            "Completed on",
            "course_assignments",
            "completed_at",
            DataType.DATETIME,
            is_measure=True,
        ),
        Field(
            "course_score",
            "Score",
            "course_assignments",
            "score",
            DataType.INTEGER,
            Sensitivity.HR_ONLY,
            is_dimension=False,
            is_measure=True,
        ),
        _count("assignment_count", "Course assignments", "course_assignments"),
    ),
)

_GOAL = Entity(
    key="goal",
    display_name="Goals",
    base_table="goals",
    employee_column="owner_employee_id",
    description="One row per goal, with its owner, status and progress.",
    default_date="goal_due_date",
    people_field="headcount",
    always=(("goal_status", "not_equals", "cancelled"),),
    fields=(
        *_employee_dimensions(via=(_via("goals", "owner_employee_id"),)),
        Field("goal_level", "Goal level", "goals", "level", DataType.ENUM),
        Field("goal_status", "Goal status", "goals", "status", DataType.ENUM),
        Field("goal_due_date", "Due on", "goals", "due_date", DataType.DATE, is_measure=True),
        Field(
            "progress_percent",
            "Progress (%)",
            "goals",
            "progress_percent",
            DataType.DECIMAL,
            is_dimension=False,
            is_measure=True,
        ),
        _count("goal_count", "Goals", "goals"),
    ),
)

_REQUISITION = Entity(
    key="requisition",
    display_name="Hiring requisitions",
    base_table="requisitions",
    employee_column=None,
    personal=False,
    description="One row per approved or requested opening to hire for.",
    default_date="requisition_raised_on",
    fields=(
        Field(
            "requisition_department",
            "Department",
            "requisition_departments",
            "name",
            DataType.STRING,
            joins=(
                JoinSpec(
                    table="departments",
                    local_table="requisitions",
                    local_column="department_id",
                    alias="requisition_departments",
                ),
            ),
        ),
        Field(
            "requisition_location",
            "Location",
            "requisition_locations",
            "name",
            DataType.STRING,
            joins=(
                JoinSpec(
                    table="locations",
                    local_table="requisitions",
                    local_column="location_id",
                    alias="requisition_locations",
                ),
            ),
        ),
        Field("requisition_status", "Requisition status", "requisitions", "status", DataType.ENUM),
        Field("requisition_grade", "Grade", "requisitions", "grade", DataType.STRING),
        Field(
            "requisition_raised_on",
            "Raised on",
            "requisitions",
            "created_at",
            DataType.DATETIME,
            is_measure=True,
        ),
        Field(
            "target_close_date",
            "Target close date",
            "requisitions",
            "target_close_date",
            DataType.DATE,
            is_measure=True,
        ),
        Field(
            "openings",
            "Openings",
            "requisitions",
            "openings",
            DataType.INTEGER,
            is_dimension=False,
            is_measure=True,
            unit="count",
        ),
        Field(
            "filled",
            "Filled",
            "requisitions",
            "filled_count",
            DataType.INTEGER,
            is_dimension=False,
            is_measure=True,
            unit="count",
        ),
        _money(
            "budget_ctc_max",
            "Budget (max CTC)",
            "requisitions",
            "budget_ctc_max",
            Sensitivity.PAYROLL_ONLY,
        ),
        _count("requisition_count", "Requisitions", "requisitions", Sensitivity.HR_ONLY),
    ),
)

_OVERTIME = Entity(
    key="overtime_entry",
    display_name="Overtime",
    base_table="overtime_entries",
    employee_column="employee_id",
    description="One row per day of overtime recorded for an employee.",
    default_date="overtime_date",
    people_field="headcount",
    fields=(
        *_employee_dimensions(via=(_via("overtime_entries", "employee_id"),)),
        Field(
            "overtime_date",
            "Date",
            "overtime_entries",
            "attendance_date",
            DataType.DATE,
            is_measure=True,
        ),
        Field("overtime_status", "Overtime status", "overtime_entries", "status", DataType.ENUM),
        Field(
            "approved_minutes",
            "Approved overtime (minutes)",
            "overtime_entries",
            "approved_minutes",
            DataType.INTEGER,
            is_dimension=False,
            is_measure=True,
            unit="minutes",
        ),
        _money(
            "overtime_amount",
            "Overtime pay",
            "overtime_entries",
            "amount",
            Sensitivity.PAYROLL_ONLY,
        ),
        _count("overtime_count", "Overtime days", "overtime_entries"),
    ),
)

_REGULARISATION = Entity(
    key="attendance_regularisation",
    display_name="Attendance corrections",
    base_table="attendance_regularisations",
    employee_column="employee_id",
    description="One row per request to correct a day's attendance (missed punch, on duty, ...).",
    default_date="corrected_date",
    people_field="headcount",
    fields=(
        *_employee_dimensions(via=(_via("attendance_regularisations", "employee_id"),)),
        Field(
            "corrected_date",
            "Day corrected",
            "attendance_regularisations",
            "attendance_date",
            DataType.DATE,
            is_measure=True,
        ),
        Field("correction_kind", "Kind", "attendance_regularisations", "kind", DataType.ENUM),
        Field(
            "correction_status",
            "Request status",
            "attendance_regularisations",
            "status",
            DataType.ENUM,
        ),
        _count("correction_count", "Correction requests", "attendance_regularisations"),
    ),
)

_REIMBURSEMENT = Entity(
    key="reimbursement_claim",
    display_name="Salary reimbursements",
    base_table="reimbursement_claims",
    employee_column="employee_id",
    description="One row per bill claimed against a salary reimbursement (fuel, phone, ...).",
    default_date="bill_date",
    people_field="headcount",
    fields=(
        *_employee_dimensions(via=(_via("reimbursement_claims", "employee_id"),)),
        Field(
            "component_code", "Component", "reimbursement_claims", "component_code", DataType.STRING
        ),
        Field("reimbursement_status", "Status", "reimbursement_claims", "status", DataType.ENUM),
        Field(
            "bill_date",
            "Bill date",
            "reimbursement_claims",
            "bill_date",
            DataType.DATE,
            is_measure=True,
        ),
        _money(
            "bill_amount", "Bill amount", "reimbursement_claims", "amount", Sensitivity.PAYROLL_ONLY
        ),
        _money(
            "accepted_amount",
            "Accepted amount",
            "reimbursement_claims",
            "accepted_amount",
            Sensitivity.PAYROLL_ONLY,
        ),
        _count("reimbursement_count", "Bills", "reimbursement_claims"),
    ),
)

#: How each existing value reads, so a chart can say rupees, days or minutes.
_UNITS: dict[tuple[str, str], str] = {
    **{
        ("payslip", key): "inr"
        for key in (
            "gross_earnings",
            "total_deductions",
            "net_pay",
            "employer_cost",
            "pf_employee",
            "pf_employer",
            "esi_employee",
            "esi_employer",
            "professional_tax",
            "tds",
        )
    },
    ("payslip", "lop_days"): "days",
    ("payslip", "paid_days"): "days",
    ("employee", "ctc_annual"): "inr",
    ("employee", "notice_period_days"): "days",
    ("candidate", "expected_ctc"): "inr",
    ("offer", "offer_ctc"): "inr",
    ("leave_request", "total_days"): "days",
    ("attendance_day", "worked_minutes"): "minutes",
    ("attendance_day", "late_by_minutes"): "minutes",
}


def _with_units(ent: Entity) -> Entity:
    fields = []
    for f in ent.fields:
        unit = _UNITS.get((ent.key, f.key))
        if unit is None and f.key.endswith("_count") and not f.is_dimension:
            unit = "count"
        if f.key == "headcount":
            unit = "count"
        fields.append(replace(f, unit=unit) if unit and not f.unit else f)
    return replace(ent, fields=tuple(fields))


ENTITIES: dict[str, Entity] = {
    e.key: _with_units(e)
    for e in (
        _EMPLOYEE,
        _PAYSLIP,
        _ATTENDANCE,
        _LEAVE,
        _APPRAISAL,
        _CANDIDATE,
        _OFFER,
        _EXPENSE_CLAIM,
        _HELPDESK_TICKET,
        _ASSET,
        _TIMESHEET_ENTRY,
        _EXIT_RECORD,
        _SHOUTOUT,
        _ACCOUNT_ENTRY,
        _COURSE_ASSIGNMENT,
        _GOAL,
        _REQUISITION,
        _OVERTIME,
        _REGULARISATION,
        _REIMBURSEMENT,
    )
}


# ═══════════════════════════════════════════════════════════════════════════
# Lookups
# ═══════════════════════════════════════════════════════════════════════════


def entity(key: str) -> Entity:
    """Refuse anything not in the catalogue.

    This is the whole identifier-safety argument: a key that is not here never
    becomes part of a query, so no user-supplied string can reach the SQL as an
    identifier.
    """
    try:
        return ENTITIES[key]
    except KeyError as exc:
        raise SemanticError(
            f"'{key}' is not a reportable entity. Available: {sorted(ENTITIES)}."
        ) from exc


def field_for(entity_key: str, field_key: str) -> Field:
    return entity(entity_key).field(field_key)


#: What each role may see. Roles above their own level see everything below it.
_ROLE_CEILING: dict[str, Sensitivity] = {
    "super_admin": Sensitivity.PAYROLL_ONLY,
    "admin": Sensitivity.PAYROLL_ONLY,
    "sub_admin": Sensitivity.HR_ONLY,
    "manager": Sensitivity.PUBLIC,
    "employee": Sensitivity.PUBLIC,
}

_ORDER = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.HR_ONLY: 1,
    Sensitivity.PAYROLL_ONLY: 2,
}


def may_see(role: str, sensitivity: Sensitivity) -> bool:
    """Whether a role may see a field at this level.

    An unknown role gets PUBLIC rather than an exception. A new role added
    elsewhere in the system should reduce what a report can reach, never widen
    it by falling through a lookup.
    """
    ceiling = _ROLE_CEILING.get(role, Sensitivity.PUBLIC)
    return _ORDER[sensitivity] <= _ORDER[ceiling]


def visible_fields(entity_key: str, role: str) -> list[Field]:
    """What this role may put on a report of this entity.

    Used to build the field picker. The compiler checks again independently —
    a picker that hides a field is a courtesy, and the check that matters is the
    one that runs when the query is built.
    """
    return [f for f in entity(entity_key).fields if may_see(role, f.sensitivity)]


def catalogue_for(role: str) -> list[dict[str, Any]]:
    """The whole catalogue as this role sees it."""
    out: list[dict[str, Any]] = []
    for key, ent in ENTITIES.items():
        fields = visible_fields(key, role)
        if not fields:
            continue
        out.append(
            {
                "key": key,
                "display_name": ent.display_name,
                "description": ent.description,
                "supports_row_scope": ent.employee_column is not None,
                "fields": [
                    {
                        "key": f.key,
                        "display_name": f.display_name,
                        "data_type": f.data_type.value,
                        "sensitivity": f.sensitivity.value,
                        "is_dimension": f.is_dimension,
                        "is_measure": f.is_measure,
                        "aggregations": list(f.allowed_aggregations),
                        "description": f.description,
                    }
                    for f in fields
                ],
            }
        )
    return out


def validate_catalogue() -> list[str]:
    """Self-check, run as a test rather than at import.

    Catches the mistakes that are invisible at a glance: a field whose
    aggregations do not suit its type, a join that starts from a table nothing
    else in the field's chain provides, a duplicate key.
    """
    problems: list[str] = []
    for key, ent in ENTITIES.items():
        seen: set[str] = set()
        for f in ent.fields:
            if f.key in seen:
                problems.append(f"{key}.{f.key} is defined twice.")
            seen.add(f.key)

            if not f.is_dimension and not f.is_measure:
                problems.append(f"{key}.{f.key} is neither a dimension nor a measure.")

            reachable = {ent.base_table}
            for join in f.joins:
                if join.local_table not in reachable:
                    problems.append(
                        f"{key}.{f.key} joins {join.table} from {join.local_table}, "
                        f"which is not reachable at that point. Reachable: "
                        f"{sorted(reachable)}."
                    )
                reachable.add(join.alias_name)
            if f.table not in reachable:
                problems.append(
                    f"{key}.{f.key} reads {f.table}.{f.column}, but {f.table} is not "
                    f"joined. Reachable: {sorted(reachable)}."
                )
            if f.month_from is not None and f.data_type is not DataType.DATE:
                problems.append(f"{key}.{f.key} is built from a year and month but is not a date.")
        keys = {f.key for f in ent.fields}
        for field_key, _op, _value in ent.always:
            if field_key not in keys:
                problems.append(f"{key} filters on '{field_key}', which it does not have.")
        if ent.default_date and ent.default_date not in keys:
            problems.append(f"{key}'s default date '{ent.default_date}' is not a field.")
        if ent.people_field and ent.people_field not in keys:
            problems.append(f"{key}'s people field '{ent.people_field}' is not a field.")
        if ent.personal and ent.people_field is None:
            problems.append(f"{key} is about people but cannot count them (people_field).")
    return problems
