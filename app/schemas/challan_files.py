"""Challan upload files from a locked payroll run: ESIC MC template and PF ECR."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.models.employee import EmploymentStatus, EmploymentType

#: esic_mc — the ESIC portal's MC_Template.xls; pf_ecr_csv — the ECR upload
#: sheet with a header row; pf_ecr_txt — the EPFO's #~# ECR text file.
ChallanKind = Literal["esic_mc", "pf_ecr_csv", "pf_ecr_txt"]

#: joined / left — date of joining / date of exit in the run's month;
#: continuing — neither.
Movement = Literal["all", "joined", "left", "continuing"]


class ChallanFilters(BaseModel):
    """Who goes in the file. An empty list means no narrowing on that field."""

    department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    location_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    project_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    designations: list[str] = Field(default_factory=list, max_length=500)
    grades: list[str] = Field(default_factory=list, max_length=500)
    employment_types: list[EmploymentType] = Field(default_factory=list)
    statuses: list[EmploymentStatus] = Field(default_factory=list)
    movement: Movement = "all"
    #: Only these people (after the filters above).
    employee_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    #: Never these people.
    exclude_employee_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)


class ChallanRequest(BaseModel):
    kind: ChallanKind
    filters: ChallanFilters = Field(default_factory=ChallanFilters)


class FilterChoice(BaseModel):
    value: str
    label: str
    count: int


class ChallanPerson(BaseModel):
    id: uuid.UUID
    employee_code: str
    name: str


class ChallanOptions(BaseModel):
    """What the run's people can be narrowed by, each with how many hold it."""

    period_label: str
    legal_entity_name: str | None
    #: The locked runs of the month the files cover (the regular run and top-ups).
    runs_included: int
    people: int
    departments: list[FilterChoice]
    locations: list[FilterChoice]
    projects: list[FilterChoice]
    designations: list[FilterChoice]
    grades: list[FilterChoice]
    employment_types: list[FilterChoice]
    statuses: list[FilterChoice]
    joined: int
    left: int
    employees: list[ChallanPerson]


class ChallanIssue(BaseModel):
    employee_code: str
    name: str
    message: str


class ChallanTotal(BaseModel):
    label: str
    amount: Decimal


class ChallanPreview(BaseModel):
    kind: ChallanKind
    file_name: str
    period_label: str
    #: People the filters matched on the run.
    matched: int
    #: Lines the file will hold.
    rows: int
    totals: list[ChallanTotal]
    #: Matched, but no PF (or ESI) this month, so not on this file at all.
    not_covered: int
    #: Owed a contribution but left out of the file, and why (no or bad UAN / IP number).
    left_out: list[ChallanIssue]
    #: ESIC lines whose wages do not explain the ESI deducted (months imported
    #: without the other HRMS's ESI wages); each is also among `warnings`.
    wage_mismatches: int = 0
    #: In the file, but worth a look before uploading.
    warnings: list[ChallanIssue]
