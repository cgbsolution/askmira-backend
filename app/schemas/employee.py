"""Employee contracts (FS-CHR-01, FS-CHR-02)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

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

    designation: str | None = Field(default=None, max_length=255)
    grade: str | None = Field(default=None, max_length=32)

    employment_type: EmploymentType = EmploymentType.FULL_TIME
    date_of_joining: date | None = None
    probation_end_date: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)


class EmployeeCreate(EmployeeBase):
    """`tenant_id` is deliberately absent — it comes from the caller's token."""

    employee_code: str = Field(min_length=1, max_length=32)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class EmployeeUpdate(BaseModel):
    """Every field optional: a PATCH only touches what it names."""

    model_config = ConfigDict(extra="forbid")

    first_name: str | None = Field(default=None, min_length=1, max_length=128)
    last_name: str | None = None
    display_name: str | None = None
    work_email: EmailStr | None = None
    personal_email: EmailStr | None = None
    phone: str | None = None
    date_of_birth: date | None = None
    gender: Gender | None = None

    legal_entity_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None

    designation: str | None = None
    grade: str | None = None
    employment_type: EmploymentType | None = None
    status: EmploymentStatus | None = None
    date_of_joining: date | None = None
    date_of_exit: date | None = None
    probation_end_date: date | None = None
    notice_period_days: int | None = None

    custom_fields: dict[str, Any] | None = None


class EmployeeSelfUpdate(BaseModel):
    """What an employee may change on their own record (FS-CHR-02).

    Salary, grade, status and reporting line are absent on purpose: those stay
    read-only in self-service, and an edit that needs approval becomes a task
    for HR rather than a direct write.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    personal_email: EmailStr | None = None
    phone: str | None = None
    photo_url: str | None = None


class EmployeeRead(EmployeeBase):
    model_config = ConfigDict(from_attributes=True)

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


class EmployeeSummary(BaseModel):
    """The compact shape used by lists, org charts and pickers."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_code: str
    first_name: str
    last_name: str | None
    display_name: str | None
    work_email: EmailStr
    designation: str | None
    department_id: uuid.UUID | None
    manager_id: uuid.UUID | None
    photo_url: str | None
    status: EmploymentStatus
