"""The exposed schema: versioned separately from the tables underneath it.

═══════════════════════════════════════════════════════════════════════════════
A CLIENT'S DASHBOARD BREAKS WHEN WE RENAME A COLUMN
═══════════════════════════════════════════════════════════════════════════════

That is the whole reason this module exists. Once a client points Power BI at an
export, our internal column names become their contract — and the next
refactor, which is entirely reasonable on our side, silently breaks a dashboard
somebody presents to their board on Monday.

So the exposed schema is a **separate, versioned surface**. `ExposedField.source`
names the internal field it currently reads from, and that mapping is the only
thing a rename touches. An invariant checks that renaming an internal field
cannot change an exposed name.

═══════════════════════════════════════════════════════════════════════════════
AND IT REUSES THE REPORT BUILDER'S SENSITIVITY, IT DOES NOT RESTATE IT
═══════════════════════════════════════════════════════════════════════════════

A second copy of "which fields are payroll-only" is a second copy that drifts,
and the copy that drifts is the one nobody is testing. Every exposed field
carries the `Sensitivity` of the semantic field it reads from, resolved at
definition time from `services.reporting.semantic` — so a field that becomes
more sensitive there becomes more sensitive here without anybody editing this
file.

═══════════════════════════════════════════════════════════════════════════════
A BI CONNECTOR IS A BULK EGRESS PATH
═══════════════════════════════════════════════════════════════════════════════

It is the single largest quantity of personal data that leaves this system, on a
schedule, unattended. Everything here is shaped by that: the field list is
explicit, the sensitivity is enforced per field, and nothing is exported without
an audit row naming the fields and the row count.

Pure. No session, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.reporting.semantic import (
    ENTITIES,
    Sensitivity,
    may_see,
)

__all__ = [
    "EXPOSED_SCHEMA",
    "SCHEMA_VERSION",
    "ExposedEntity",
    "ExposedField",
    "SchemaError",
    "exposed_entity",
    "schema_document",
    "visible_columns",
]


class SchemaError(ValueError):
    """An exposed-schema request this module refuses."""


#: Bumped only for a **breaking** change to the exposed surface: a removed or
#: renamed column, or a type change. Adding a column is additive and does not
#: bump it - a client's dashboard does not break because a column appeared.
SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class ExposedField:
    """One column a client's BI tool sees.

    `name` is the client's contract. `source` is where it currently comes from,
    and is the only half a refactor touches.
    """

    name: str
    entity_key: str
    source: str
    description: str

    @property
    def sensitivity(self) -> Sensitivity:
        """Resolved from the semantic layer, never restated here.

        A second copy of "which fields are payroll-only" is a second copy that
        drifts, and the one that drifts is the one nobody is testing.
        """
        entity = ENTITIES.get(self.entity_key)
        if entity is None:
            raise SchemaError(
                f"Exposed field {self.name!r} reads from entity "
                f"{self.entity_key!r}, which the semantic layer does not "
                "define. An exposed column with no semantic field behind it has "
                "no sensitivity, and 'no sensitivity' defaults to visible."
            )
        field = next((f for f in entity.fields if f.key == self.source), None)
        if field is None:
            raise SchemaError(
                f"Exposed field {self.name!r} reads from "
                f"{self.entity_key}.{self.source!r}, which no longer exists. "
                "This is exactly the rename that breaks a client's dashboard - "
                "repoint the mapping rather than removing the exposed column."
            )
        return field.sensitivity

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "sensitivity": self.sensitivity.value,
            # The internal source is deliberately **not** published. A client
            # who learns the internal name starts depending on it, and then the
            # indirection this module exists for stops working.
        }


@dataclass(frozen=True, slots=True)
class ExposedEntity:
    """One view a client's BI tool can read."""

    name: str
    entity_key: str
    description: str
    fields: tuple[ExposedField, ...]
    #: The column an incremental export tracks changes by.
    change_key: str = "updated_at"


def _f(name: str, entity_key: str, source: str, description: str) -> ExposedField:
    return ExposedField(
        name=name, entity_key=entity_key, source=source, description=description
    )


EXPOSED_SCHEMA: dict[str, ExposedEntity] = {
    "dim_employee": ExposedEntity(
        name="dim_employee",
        entity_key="employee",
        description="One row per employee, current state.",
        fields=(
            _f("employee_code", "employee", "employee_code", "Stable identifier."),
            _f("full_name", "employee", "employee_name", "Display name."),
            _f("department", "employee", "department", "Department name."),
            _f("location", "employee", "location", "Work location."),
            _f("state", "employee", "state", "State, for statutory grouping."),
            _f("grade", "employee", "grade", "Grade band."),
            _f("designation", "employee", "designation", "Job title."),
            _f("employment_type", "employee", "employment_type", "Employment type."),
            _f("joined_on", "employee", "date_of_joining", "Date of joining."),
            _f("status", "employee", "status", "Employment status."),
            _f("exited_on", "employee", "date_of_exit", "Date of exit, if any."),
            _f("manager_name", "employee", "manager_name", "Reporting manager."),
            _f("ctc_annual", "employee", "ctc_annual", "Annual cost to company."),
        ),
    ),
    "fact_payslip": ExposedEntity(
        name="fact_payslip",
        entity_key="payslip",
        description="One row per employee per payroll period.",
        fields=(
            _f("employee_code", "payslip", "employee_code", "Stable identifier."),
            _f("department", "payslip", "department", "Department name."),
            _f("location", "payslip", "location", "Work location."),
            _f("gross_pay", "payslip", "gross_earnings", "Gross earnings."),
            _f("total_deductions", "payslip", "total_deductions", "Deductions."),
            _f("net_pay", "payslip", "net_pay", "Net pay."),
            _f("employer_cost", "payslip", "employer_cost", "Total employer cost."),
            _f("paid_days", "payslip", "paid_days", "Paid days in the period."),
            _f("lop_days", "payslip", "lop_days", "Loss-of-pay days."),
        ),
    ),
    "fact_attendance": ExposedEntity(
        name="fact_attendance",
        entity_key="attendance_day",
        description="One row per employee per day.",
        fields=(
            _f(
                "employee_code",
                "attendance_day",
                "employee_code",
                "Stable identifier.",
            ),
            _f("department", "attendance_day", "department", "Department name."),
            _f("work_date", "attendance_day", "attendance_date", "Calendar date."),
            _f(
                "attendance_status",
                "attendance_day",
                "attendance_status",
                "Attendance status.",
            ),
            _f(
                "worked_minutes",
                "attendance_day",
                "worked_minutes",
                "Minutes worked.",
            ),
            _f(
                "payable_fraction",
                "attendance_day",
                "payable_fraction",
                "Payable fraction of the day.",
            ),
        ),
    ),
    "fact_leave": ExposedEntity(
        name="fact_leave",
        entity_key="leave_request",
        description="One row per leave request.",
        fields=(
            _f(
                "employee_code",
                "leave_request",
                "employee_code",
                "Stable identifier.",
            ),
            _f("department", "leave_request", "department", "Department name."),
            _f("from_date", "leave_request", "leave_start", "First day."),
            _f("to_date", "leave_request", "leave_end", "Last day."),
            _f("days", "leave_request", "total_days", "Days taken."),
            _f("leave_status", "leave_request", "leave_status", "Request status."),
            # `leave_reason` is deliberately not exposed: it is free text an
            # employee wrote about why they were away, it is routinely medical,
            # and a BI extract is the last place it should end up.
        ),
    ),
    "fact_appraisal": ExposedEntity(
        name="fact_appraisal",
        entity_key="appraisal",
        description="One row per appraisal.",
        fields=(
            _f("employee_code", "appraisal", "employee_code", "Stable identifier."),
            _f("department", "appraisal", "department", "Department name."),
            _f("grade", "appraisal", "grade", "Grade band."),
            _f(
                "appraisal_status",
                "appraisal",
                "appraisal_status",
                "Cycle status.",
            ),
            _f("final_rating", "appraisal", "final_rating", "Final rating label."),
            _f("completed_at", "appraisal", "completed_at", "When it completed."),
        ),
    ),
}


def exposed_entity(name: str) -> ExposedEntity:
    """Refuse anything not in the exposed catalogue.

    The same identifier-safety argument as the report builder: a name that is
    not here never reaches a query, so no client-supplied string can become an
    identifier.
    """
    if name not in EXPOSED_SCHEMA:
        raise SchemaError(
            f"{name!r} is not an exposed view. The connector serves the "
            f"published schema only: {sorted(EXPOSED_SCHEMA)}. Raw tables are "
            "deliberately not reachable - they are ours to rename and a client "
            "cannot build on them."
        )
    return EXPOSED_SCHEMA[name]


def visible_columns(entity_name: str, role: str) -> list[ExposedField]:
    """The columns this role may export from this view.

    Enforced per field against the *semantic layer's* sensitivity, so the
    connector and the report builder cannot disagree about who may see a salary.
    """
    return [
        field
        for field in exposed_entity(entity_name).fields
        if may_see(role, field.sensitivity)
    ]


def schema_document(role: str | None = None) -> dict[str, Any]:
    """The published schema, with its stability guarantee stated.

    Given a role, it is filtered to what that role may see - so a client with a
    reporting-only credential is documented the surface they actually have,
    rather than one that half-fails at export time.
    """
    entities = []
    for entity in EXPOSED_SCHEMA.values():
        fields = (
            [f for f in entity.fields if may_see(role, f.sensitivity)]
            if role is not None
            else list(entity.fields)
        )
        entities.append(
            {
                "name": entity.name,
                "description": entity.description,
                "change_key": entity.change_key,
                "columns": [f.as_dict() for f in fields],
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "entities": entities,
        "stability": (
            "Column names in this document are a contract. They are versioned "
            "separately from our internal tables: an internal rename repoints a "
            "mapping and changes nothing here. A removed or renamed exposed "
            "column, or a type change, bumps the schema version and is "
            "announced; a new column is additive and does not."
        ),
        "sensitivity_note": (
            "Field-level sensitivity is the report builder's, not a second copy "
            "of it. A credential sees exactly the fields the same role would see "
            "in a report."
        ),
    }
