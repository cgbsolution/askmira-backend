"""1:1 meeting schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from pydantic import BaseModel, Field

from app.models.one_on_one import OneOnOneItemKind, OneOnOneStatus


class OneOnOneCreate(BaseModel):
    #: The other person: somebody in your reporting line, or your own manager.
    with_employee_id: uuid.UUID
    scheduled_on: date
    start_time: time | None = None
    duration_minutes: int = Field(default=30, gt=0, le=480)
    #: Talking points to start the agenda with.
    agenda: list[str] = Field(default_factory=list, max_length=20)
    #: Every this many weeks; the next is booked when this one is held or cancelled.
    repeat_every_weeks: int | None = Field(default=None, ge=1, le=12)


class OneOnOneUpdate(BaseModel):
    """Reschedule. Only a meeting that has not happened yet."""

    scheduled_on: date | None = None
    start_time: time | None = None
    clear_start_time: bool = False
    duration_minutes: int | None = Field(default=None, gt=0, le=480)


class OneOnOneItemCreate(BaseModel):
    kind: OneOnOneItemKind = OneOnOneItemKind.TALKING_POINT
    body: str = Field(min_length=1, max_length=2000)
    #: Action items: one of the two participants. Defaults to whoever adds it.
    owner_employee_id: uuid.UUID | None = None
    due_on: date | None = None


class OneOnOneItemUpdate(BaseModel):
    body: str | None = Field(default=None, min_length=1, max_length=2000)
    done: bool | None = None
    owner_employee_id: uuid.UUID | None = None
    due_on: date | None = None


class OneOnOneNoteWrite(BaseModel):
    body: str = Field(max_length=10000)


class OneOnOneComplete(BaseModel):
    summary: str | None = Field(default=None, max_length=10000)


class OneOnOneCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class OneOnOneItemRead(BaseModel):
    id: uuid.UUID
    one_on_one_id: uuid.UUID
    kind: OneOnOneItemKind
    body: str
    added_by_name: str | None
    is_mine: bool
    owner_employee_id: uuid.UUID | None
    owner_name: str | None
    due_on: date | None
    done_at: datetime | None
    #: Where an open action item came from an earlier meeting.
    from_meeting_on: date | None = None


class OneOnOneRead(BaseModel):
    id: uuid.UUID
    manager_employee_id: uuid.UUID
    manager_name: str
    report_employee_id: uuid.UUID
    report_name: str
    #: The other participant, from the caller's side.
    with_employee_id: uuid.UUID
    with_name: str
    #: "manager" or "report": which side of the meeting the caller is on.
    my_side: str
    scheduled_on: date
    start_time: time | None
    duration_minutes: int
    repeat_every_weeks: int | None = None
    status: OneOnOneStatus
    summary: str | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    talking_points: int
    open_action_items: int
    created_at: datetime


class OneOnOneDetail(OneOnOneRead):
    items: list[OneOnOneItemRead]
    #: Open action items from earlier meetings between the same two people.
    carried_over: list[OneOnOneItemRead]
    #: The caller's own private note. Nobody else's is ever returned.
    my_note: str | None
    my_note_updated_at: datetime | None


class OneOnOnePartner(BaseModel):
    """Somebody the caller can schedule a 1:1 with."""

    employee_id: uuid.UUID
    name: str
    employee_code: str
    designation: str | None
    #: "manager" — the caller's own manager; "report" — a direct report.
    relation: str


class ReportOneOnOneSummary(BaseModel):
    """One direct report, from their manager's side."""

    employee_id: uuid.UUID
    name: str
    employee_code: str
    designation: str | None
    last_held_on: date | None
    next_scheduled_on: date | None
    next_one_on_one_id: uuid.UUID | None
    held_count: int
    open_action_items: int
