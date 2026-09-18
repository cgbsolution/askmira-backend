"""The cross-module approvals inbox."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel


class ApprovalItemRead(BaseModel):
    """One decision waiting on the caller. Decided on the owning module's page."""

    source: str
    source_label: str
    id: uuid.UUID
    title: str
    subject_name: str | None = None
    detail: str | None = None
    submitted_at: datetime
    due_on: date | None = None
    #: A portal route where this item is decided. Always relative.
    link: str


class ApprovalSourceRead(BaseModel):
    key: str
    label: str
    count: int
    available: bool = True
    note: str | None = None


class ApprovalInboxRead(BaseModel):
    items: list[ApprovalItemRead]
    sources: list[ApprovalSourceRead]
    total: int
