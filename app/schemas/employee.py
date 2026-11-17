"""Employee contracts (FS-CHR-01, FS-CHR-02)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.contractors import ContractorKind, TdsSection
from app.models.employee import ApproverRoute, EmploymentStatus, EmploymentType, Gender

DEFAULT_GST_RATE = Decimal("18")


def consultant_billing_problem(
    employment_type: EmploymentType,
    *,
    gst_applicable: bool,
    gst_rate: Decimal | None,
    gstin: str | None,
    tds_section: TdsSection | None,
    route: ApproverRoute | None,
    contact_name: str | None,
    contact_email: str | None,
) -> str | None:
    """What is wrong with a record's consultant fields, or None.

    Only a consultant carries them: nothing would act on them for anybody else,
    and a figure nobody acts on is worse than none.
    """
    if employment_type is not EmploymentType.CONSULTANT:
        if any(
            (
                gst_applicable,
                gst_rate is not None,
                gstin,
                tds_section is not None,
                route is not None,
                contact_name,
                contact_email,
            )
        ):
            return "GST, TDS and approver details are set only for a consultant."
        return None
    if gst_applicable:
        if gst_rate is None:
            return "Enter the GST rate, or set GST applicable to No."
        if not gstin:
            return "Enter the consultant's GSTIN, or set GST applicable to No."
    elif gst_rate is not None or gstin:
        return "A GST rate or GSTIN needs GST applicable set to Yes."
    if route is ApproverRoute.CUSTOMER and not (contact_name and contact_email):
        return "Enter the customer contact's name and email: they approve the timesheets."
    return None


def apply_consultant_defaults(employment_type: EmploymentType, values: dict[str, Any]) -> None:
    """Fill what a consultant was saved without: 194J (professional), an individual, 18% GST."""
    if employment_type is not EmploymentType.CONSULTANT:
        return
    if values.get("tds_section") is None:
        values["tds_section"] = TdsSection.S194J_PROFESSIONAL
    if values.get("payee_kind") is None:
        values["payee_kind"] = ContractorKind.INDIVIDUAL
    if values.get("gst_applicable") and values.get("gst_rate") is None:
        values["gst_rate"] = DEFAULT_GST_RATE


class EmployeeBase(BaseModel):
    first_name: str = Field(min_length=1, max_length=128)
    last_name: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=255)
    work_email: EmailStr
    personal_email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    date_of_birth: date | None = None
    gender: Gender | None = None

    legal_entity_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    #: The project they work on: a client's, or the company's own (Internal).
    project_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    #: A second, dotted-line manager. Informational: no approval rights.
    dotted_line_manager_id: uuid.UUID | None = None

    designation: str | None = Field(default=None, max_length=255)
    grade: str | None = Field(default=None, max_length=32)

    employment_type: EmploymentType = EmploymentType.FULL_TIME
    date_of_joining: date | None = None
    probation_end_date: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)

    #: Consultant billing, used by payouts only for a consultant.
    gst_applicable: bool = False

    #: Consultant payouts: how they are taxed and billed, and who approves their time.
    tds_section: TdsSection | None = None
    payee_kind: ContractorKind | None = None
    gst_rate: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    gstin: str | None = Field(default=None, max_length=15)
    contract_end_date: date | None = None
    timesheet_approver_route: ApproverRoute | None = None
    customer_contact_name: str | None = Field(default=None, max_length=200)
    customer_contact_email: EmailStr | None = None
    internal_approver_user_id: uuid.UUID | None = None


class EmployeeCreate(EmployeeBase):
    """`tenant_id` is deliberately absent — it comes from the caller's token."""

    employee_code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-/]+$")
    status: EmploymentStatus = EmploymentStatus.ACTIVE
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _dates_in_order(self) -> EmployeeCreate:
        if (
            self.date_of_joining
            and self.probation_end_date
            and self.probation_end_date < self.date_of_joining
        ):
            raise ValueError("Probation cannot end before the date of joining")
        values: dict[str, Any] = {
            "tds_section": self.tds_section,
            "payee_kind": self.payee_kind,
            "gst_applicable": self.gst_applicable,
            "gst_rate": self.gst_rate,
        }
        apply_consultant_defaults(self.employment_type, values)
        self.tds_section = values["tds_section"]
        self.payee_kind = values["payee_kind"]
        self.gst_rate = values["gst_rate"]
        problem = consultant_billing_problem(
            self.employment_type,
            gst_applicable=self.gst_applicable,
            gst_rate=self.gst_rate,
            gstin=self.gstin,
            tds_section=self.tds_section,
            route=self.timesheet_approver_route,
            contact_name=self.customer_contact_name,
            contact_email=self.customer_contact_email,
        )
        if problem:
            raise ValueError(problem)
        return self


class EmployeeUpdate(BaseModel):
    """Every field optional: a PATCH only touches what it names."""

    model_config = ConfigDict(extra="forbid")

    #: When a changed job field (department, manager, designation, grade,
    #: location) took effect, for a correction of the past. Today if left out;
    #: a future change is raised as a lifecycle event instead.
    changes_effective_from: date | None = None

    first_name: str | None = Field(default=None, min_length=1, max_length=128)
    last_name: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=255)
    work_email: EmailStr | None = None
    personal_email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32, pattern=r"^[0-9+\-() ]*$")
    date_of_birth: date | None = None
    gender: Gender | None = None

    legal_entity_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    #: The project they work on: a client's, or the company's own (Internal).
    project_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    dotted_line_manager_id: uuid.UUID | None = None

    designation: str | None = Field(default=None, max_length=255)
    grade: str | None = Field(default=None, max_length=32)
    employment_type: EmploymentType | None = None
    status: EmploymentStatus | None = None
    date_of_joining: date | None = None
    date_of_exit: date | None = None
    probation_end_date: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    gst_applicable: bool | None = None
    tds_section: TdsSection | None = None
    payee_kind: ContractorKind | None = None
    gst_rate: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    gstin: str | None = Field(default=None, max_length=15)
    contract_end_date: date | None = None
    timesheet_approver_route: ApproverRoute | None = None
    customer_contact_name: str | None = Field(default=None, max_length=200)
    customer_contact_email: EmailStr | None = None
    internal_approver_user_id: uuid.UUID | None = None

    custom_fields: dict[str, Any] | None = None


class EmployeeSelfUpdate(BaseModel):
    """What an employee may change on their own record (FS-CHR-02).

    Salary, grade, status and reporting line are absent on purpose: those stay
    read-only in self-service, and an edit that needs approval becomes a task
    for HR rather than a direct write. A photo URL is absent too: a free-form
    URL rendered on every colleague's screen is a tracking pixel waiting to
    happen, and photos belong in managed storage.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=255)
    personal_email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32, pattern=r"^[0-9+\-() ]*$")


class EmployeeRead(EmployeeBase):
    model_config = ConfigDict(from_attributes=True)

    #: Plain strings on the way out. Addresses are validated when written; re-
    #: validating a stored one on every read means a single address the
    #: validator has since come to dislike (an imported `.local` domain, a
    #: reserved TLD) turns the whole directory into a 500.
    work_email: str
    personal_email: str | None = None
    customer_contact_email: str | None = None
    #: Legacy, read-only: the old fixed monthly GST figure, shown to HR as a hint.
    #: Never accepted on create or update.
    gst_amount: Decimal | None = None

    id: uuid.UUID
    tenant_id: uuid.UUID
    employee_code: str
    status: EmploymentStatus
    date_of_exit: date | None
    photo_url: str | None
    custom_fields: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    #: Whether this person is in the caller's reporting line, at any depth: what
    #: decides the attendance and lifecycle views a manager is allowed. Set on a
    #: single-record read; lists leave it False.
    in_your_line: bool = False


class EmployeeSummary(BaseModel):
    """The compact shape used by lists, org charts and pickers."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_code: str
    first_name: str
    last_name: str | None
    display_name: str | None
    #: A plain string for the same reason as `EmployeeRead.work_email`.
    work_email: str
    designation: str | None
    legal_entity_id: uuid.UUID | None = None
    department_id: uuid.UUID | None
    project_id: uuid.UUID | None = None
    location_id: uuid.UUID | None
    manager_id: uuid.UUID | None
    dotted_line_manager_id: uuid.UUID | None = None
    photo_url: str | None
    status: EmploymentStatus


class ColleagueRead(BaseModel):
    """A colleague found by name to pick: who they are and nothing about their record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_code: str
    first_name: str
    last_name: str | None
    display_name: str | None
    designation: str | None
    photo_url: str | None


class EntityMoveRequest(BaseModel):
    """Put these people in one company (legal entity)."""

    model_config = ConfigDict(extra="forbid")

    employee_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    legal_entity_id: uuid.UUID


class EntityMoveSkip(BaseModel):
    employee_id: uuid.UUID
    employee_code: str | None = None
    name: str | None = None
    reason: str


class EntityMoveResult(BaseModel):
    legal_entity_id: uuid.UUID
    legal_entity_name: str
    #: The day the moves are dated: the workspace's today.
    effective_from: date
    #: People moved into the company now.
    moved: int
    #: People who were in it already: nothing changed for them.
    already_there: int
    #: People not moved, each with why.
    skipped: list[EntityMoveSkip]


class ProjectMoveRequest(BaseModel):
    """Put these people on one project, or on none (`project_id` null)."""

    model_config = ConfigDict(extra="forbid")

    employee_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    project_id: uuid.UUID | None


class ProjectMoveResult(BaseModel):
    project_id: uuid.UUID | None
    #: The project's name; None when they were taken off every project.
    project_name: str | None
    #: The day the moves are dated: the workspace's today.
    effective_from: date
    moved: int
    already_there: int
    skipped: list[EntityMoveSkip]


class EmployeeExportRequest(BaseModel):
    """What to export: the directory's filters, or the people ticked on screen.

    `columns` are keys from `GET /employees/export/columns`; left out, every
    column is written. `employee_ids` narrows to the ticked people.
    """

    model_config = ConfigDict(extra="forbid")

    file_type: Literal["xlsx", "csv"] = "xlsx"
    columns: list[str] | None = Field(default=None, min_length=1, max_length=2000)
    employee_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=20_000)
    search: str | None = Field(default=None, max_length=128)
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID | None = None
    without_legal_entity: bool = False
    project_id: uuid.UUID | None = None
    without_project: bool = False
    status: EmploymentStatus | None = None
    current: bool = False


class ExportColumnRead(BaseModel):
    key: str
    label: str


class ExportSectionRead(BaseModel):
    key: str
    label: str
    #: A list (family, education…): its columns repeat once per entry.
    repeats: bool
    #: What one entry is called in a header ("Family member 1 - Name").
    entry: str | None
    columns: list[ExportColumnRead]


class ExportCatalogueRead(BaseModel):
    sections: list[ExportSectionRead]
