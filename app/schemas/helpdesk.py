"""HR helpdesk contracts: categories, tickets, the thread and the SLA summary.

A ticket is read in two shapes. The requester's never carries an internal note
or a file attached to one — those are left out by the service, not hidden by
the client — and both carry the actions the caller may take next, so the portal
never offers a button the API would refuse.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.helpdesk import HelpdeskPriority, HelpdeskRequestKind, HelpdeskTicketStatus
from app.models.user import UserRole

SlaState = Literal["on_track", "due_soon", "overdue", "met", "breached", "none"]

# ══════════════════════════════════════════════════════════════════════════════
# Categories
# ══════════════════════════════════════════════════════════════════════════════


def _strip(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    owner_user_id: uuid.UUID | None = None
    sla_hours: int = Field(default=48, ge=1, le=720)
    is_confidential: bool = False
    request_kind: HelpdeskRequestKind = HelpdeskRequestKind.GENERAL
    active: bool = True
    sort_order: int = Field(default=0, ge=0, le=10_000)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("A category needs a name.")
        return stripped

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str | None) -> str | None:
        return _strip(value)


class CategoryUpdate(BaseModel):
    """Only the fields sent are changed. Send `owner_user_id: null` to hand the
    category back to HR's shared queue."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    owner_user_id: uuid.UUID | None = None
    sla_hours: int | None = Field(default=None, ge=1, le=720)
    is_confidential: bool | None = None
    request_kind: HelpdeskRequestKind | None = None
    active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)


class CategoryRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    owner_user_id: uuid.UUID | None
    owner_name: str | None
    sla_hours: int
    is_confidential: bool
    request_kind: HelpdeskRequestKind
    active: bool
    sort_order: int
    #: Every ticket ever raised in it, for HR; None for everyone else. A
    #: category with tickets is deactivated, never deleted.
    ticket_count: int | None


class StandardCategoryRead(BaseModel):
    """One of the standard set, as the admin sees it before adding it."""

    name: str
    description: str
    request_kind: HelpdeskRequestKind
    sla_hours: int
    is_confidential: bool


class StandardCategoriesResult(BaseModel):
    created: list[CategoryRead]
    #: Names already present (compared without case), left as they were.
    skipped: list[str]


# ══════════════════════════════════════════════════════════════════════════════
# Tickets
# ══════════════════════════════════════════════════════════════════════════════


class TicketAgentUpdate(BaseModel):
    """An agent's change to a ticket. `note` is posted as a public reply —
    the resolution, or why it was closed."""

    status: HelpdeskTicketStatus | None = None
    priority: HelpdeskPriority | None = None
    note: str | None = Field(default=None, max_length=10_000)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        return _strip(value)


class AssignIn(BaseModel):
    #: None unassigns.
    assignee_user_id: uuid.UUID | None = None


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        return _strip(value)


class ReopenIn(BaseModel):
    reason: str = Field(min_length=1, max_length=5000)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Say what is still wrong, so the agent can pick it up.")
        return stripped


class RateIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("comment")
    @classmethod
    def _clean_comment(cls, value: str | None) -> str | None:
        return _strip(value)


class AttachmentRead(BaseModel):
    id: uuid.UUID
    file_name: str
    content_type: str
    size_bytes: int
    created_at: datetime


class CommentRead(BaseModel):
    id: uuid.UUID
    author_user_id: uuid.UUID | None
    #: None for a line the system wrote (the scheduler closing a ticket).
    author_name: str | None
    body: str
    is_internal: bool
    is_system: bool
    #: Written by the employee whose ticket it is.
    is_from_requester: bool
    created_at: datetime
    attachments: list[AttachmentRead]


class TicketSummary(BaseModel):
    id: uuid.UUID
    ticket_number: str
    subject: str
    category_id: uuid.UUID
    category_name: str
    request_kind: HelpdeskRequestKind
    priority: HelpdeskPriority
    status: HelpdeskTicketStatus
    is_confidential: bool
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str
    raised_by_user_id: uuid.UUID | None
    #: HR raised it for the employee.
    raised_on_behalf: bool
    assignee_user_id: uuid.UUID | None
    assignee_name: str | None
    sla_hours: int
    due_at: datetime
    sla_state: SlaState
    first_response_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    satisfaction_rating: int | None
    created_at: datetime
    last_activity_at: datetime


class TicketDetail(TicketSummary):
    description: str
    cancelled_at: datetime | None
    reopened_count: int
    satisfaction_comment: str | None
    rated_at: datetime | None
    #: Files attached when the ticket was raised.
    attachments: list[AttachmentRead]
    comments: list[CommentRead]

    viewer: Literal["requester", "agent"]
    can_reply: bool
    can_add_internal_note: bool
    can_cancel: bool
    can_reopen: bool
    #: The last moment a reopen is accepted, while one could be.
    reopen_until: datetime | None
    can_rate: bool
    #: Assign, change status and priority.
    can_manage: bool


class AgentOption(BaseModel):
    user_id: uuid.UUID
    full_name: str
    role: UserRole
    #: Owns at least one category (for the ticket, when read per ticket).
    is_category_owner: bool


class QueueSummary(BaseModel):
    """The queue's figures, over the tickets the caller may work.

    Every average carries the number of tickets behind it; an average of one
    ticket is shown as exactly that.
    """

    open: int
    overdue: int
    due_soon: int
    unassigned: int
    #: First of the current month, India time. The "this month" figures start here.
    period_start: date
    resolved_this_month: int
    avg_first_response_hours: float | None
    first_response_sample: int
    avg_resolution_hours: float | None
    resolution_sample: int
    avg_satisfaction: float | None
    satisfaction_sample: int


class MySummary(BaseModel):
    """For the home dashboard card: the caller's own tickets at a glance."""

    open_count: int
    #: An agent has asked the caller something.
    waiting_on_you: int
    latest: list[TicketSummary]
    #: Works the helpdesk queue (HR, or owns a category).
    is_agent: bool
