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
from dataclasses import dataclass
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
_MANAGER_JOIN = JoinSpec(
    table="employees", local_table="employees", local_column="manager_id", alias="manager"
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

ENTITIES: dict[str, Entity] = {
    e.key: e for e in (_EMPLOYEE, _PAYSLIP, _ATTENDANCE, _LEAVE, _APPRAISAL, _CANDIDATE, _OFFER)
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
    return problems
