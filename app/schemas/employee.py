"""Employee contracts (FS-CHR-01, FS-CHR-02)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.employee import EmploymentStatus, EmploymentType, Gender


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
    manager_id: uuid.UUID | None = None
    #: A second, dotted-line manager. Informational: no approval rights.
    dotted_line_manager_id: uuid.UUID | None = None

    designation: str | None = Field(default=None, max_length=255)
    grade: str | None = Field(default=None, max_length=32)

    employment_type: EmploymentType = EmploymentType.FULL_TIME
    date_of_joining: date | None = None
    probation_end_date: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)


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
    location_id: uuid.UUID | None
    manager_id: uuid.UUID | None
    dotted_line_manager_id: uuid.UUID | None = None
    photo_url: str | None
    status: EmploymentStatus


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
    #: People moved into the company now.
    moved: int
    #: People who were in it already: nothing changed for them.
    already_there: int
    #: People not moved, each with why.
    skipped: list[EntityMoveSkip]
