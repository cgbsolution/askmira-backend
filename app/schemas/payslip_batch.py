"""Payslips for chosen people over the last 1, 3, 6 or 12 months, downloaded or emailed."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

PeriodMonths = Literal[1, 3, 6, 12]


class PayslipBatchCreate(BaseModel):
    employee_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    months: PeriodMonths
    #: The last month covered (any day in it). Empty: the latest approved month.
    to: date | None = None


class PayslipEmailCreate(PayslipBatchCreate):
    #: `personal` sends to the personal address where there is one, else the work one.
    send_to: Literal["work", "personal"] = "work"


class PayslipPersonRead(BaseModel):
    employee_id: uuid.UUID
    employee_code: str
    full_name: str
    department: str | None
    designation: str | None
    employment_status: str
    date_of_exit: date | None
    work_email: str | None
    personal_email: str | None
    #: Payslips in approved or paid months within the period (what a download holds).
    payslips: int
    #: Of those, the ones released to the person (what an email sends).
    released: int


class PayslipPeopleRead(BaseModel):
    #: The latest month with an approved or paid payroll; None before the first.
    latest_month: date | None
    months: int
    period_start: date | None
    period_end: date | None
    period_label: str
    #: Whether email can go out: False while no mail server is set up.
    email_ready: bool
    #: Whether payslips go out password-protected, and with which password.
    password_protected: bool
    password_rule: str
    people: list[PayslipPersonRead]


class PayslipBatchResultRead(BaseModel):
    employee_id: str
    employee_code: str | None = None
    name: str | None = None
    #: added (download) | sent (email) | skipped | failed
    status: str
    payslips: int = 0
    email: str | None = None
    detail: str | None = None


class PayslipBatchRead(BaseModel):
    id: uuid.UUID
    action: Literal["download", "email"]
    months: int
    period_start: date
    period_end: date
    period_label: str
    send_to: str | None
    status: Literal["queued", "running", "completed", "failed"]
    total_people: int
    total_payslips: int
    processed: int
    succeeded: int
    skipped: int
    failed: int
    error: str | None
    file_name: str | None
    file_size: int | None
    file_available: bool
    file_expires_at: datetime | None
    requested_by_name: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    #: Each person's outcome. Left empty in the list of recent batches.
    results: list[PayslipBatchResultRead] = []
