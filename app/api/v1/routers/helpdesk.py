"""HR helpdesk: tickets, their conversation, the agents' queue and categories.

Thin: parse, gate, delegate to `app.services.helpdesk`, serialise.

Who may call what:

- Everybody: raise a ticket for themselves (HR also on an employee's behalf),
  list and read their own, reply, cancel before resolution, reopen within 7
  days of it, and rate it.
- Agents — HR, and the owner of a category for that category's tickets: the
  queue, the SLA figures, assign, change status and priority, reply publicly or
  add an internal note. Nobody works a ticket they raised themselves.
- Confidential tickets (grievances): only the requester, the category owner,
  the assignee and tenant/HR admins.
- Tenant and HR admins: categories.

Which ticket a caller may touch is decided in the service, not here; every
ungated route is declared with its rule in `app/core/access_policies/helpdesk.py`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.helpdesk import HelpdeskPriority
from app.models.user import UserRole
from app.schemas.helpdesk import (
    AgentOption,
    AssignIn,
    CancelIn,
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
    MySummary,
    QueueSummary,
    RateIn,
    ReopenIn,
    StandardCategoriesResult,
    StandardCategoryRead,
    TicketAgentUpdate,
    TicketDetail,
    TicketSummary,
)
from app.services.helpdesk import categories, service

router = APIRouter(prefix="/helpdesk", tags=["helpdesk"])

ADMIN_ONLY = [Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN))]


async def _read_upload(file: UploadFile | None) -> tuple[bytes | None, str | None]:
    """The upload's bytes, or nothing when no file (or an empty part) was sent.

    One byte past the limit is read, which is enough to know it is over.
    """
    if file is None:
        return None, None
    data = await file.read(settings.MAX_UPLOAD_BYTES + 1)
    if not data and not file.filename:
        return None, None
    return data, file.filename


# ── Categories ──────────────────────────────────────────────────────────────


@router.get("/categories", response_model=list[CategoryRead])
async def list_categories(
    user: CurrentUser, session: DbSession, include_inactive: bool = False
) -> list[CategoryRead]:
    return await categories.list_categories(session, user, include_inactive=include_inactive)


@router.get(
    "/categories/standard",
    response_model=list[StandardCategoryRead],
    dependencies=ADMIN_ONLY,
)
async def preview_standard_categories(user: CurrentUser) -> list[StandardCategoryRead]:
    """The standard set, as "Add the standard categories" would add it."""
    return categories.standard_categories()


@router.post(
    "/categories/standard",
    response_model=StandardCategoriesResult,
    dependencies=ADMIN_ONLY,
)
async def add_standard_categories(
    user: CurrentUser, session: DbSession, request: Request
) -> StandardCategoriesResult:
    """Add the usual Indian-HR categories, skipping names already present."""
    return await categories.add_standard_categories(session, user, request=request)


@router.post(
    "/categories",
    response_model=CategoryRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
)
async def create_category(
    payload: CategoryCreate, user: CurrentUser, session: DbSession, request: Request
) -> CategoryRead:
    return await categories.create_category(session, user, payload, request=request)


@router.patch("/categories/{category_id}", response_model=CategoryRead, dependencies=ADMIN_ONLY)
async def update_category(
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> CategoryRead:
    return await categories.update_category(session, user, category_id, payload, request=request)


@router.delete(
    "/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=ADMIN_ONLY,
)
async def delete_category(
    category_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await categories.delete_category(session, user, category_id, request=request)


# ── My tickets ──────────────────────────────────────────────────────────────


@router.get("/me/summary", response_model=MySummary)
async def my_summary(user: CurrentUser, session: DbSession) -> MySummary:
    return await service.my_summary(session, user)


@router.post("/tickets", response_model=TicketSummary, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    category_id: Annotated[uuid.UUID, Form()],
    subject: Annotated[str, Form(min_length=1, max_length=200)],
    description: Annotated[str, Form(min_length=1, max_length=10_000)],
    priority: Annotated[HelpdeskPriority, Form()] = HelpdeskPriority.MEDIUM,
    employee_id: Annotated[uuid.UUID | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> TicketSummary:
    """Raise a ticket, optionally with one PDF, PNG or JPEG attached. HR may
    pass `employee_id` to raise it on an employee's behalf."""
    data, name = await _read_upload(file)
    return await service.create_ticket(
        session,
        user,
        category_id=category_id,
        subject=subject,
        description=description,
        priority=priority,
        employee_id=employee_id,
        file_data=data,
        file_name=name,
        request=request,
    )


@router.get("/tickets/mine", response_model=list[TicketSummary])
async def my_tickets(
    user: CurrentUser,
    session: DbSession,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[TicketSummary]:
    """`status` is a ticket status, or `active` (open, in progress, waiting on
    you) or `done` (closed or cancelled)."""
    return await service.list_my_tickets(session, user, status_filter=status_filter)


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
async def get_ticket(ticket_id: uuid.UUID, user: CurrentUser, session: DbSession) -> TicketDetail:
    return await service.get_ticket(session, user, ticket_id)


@router.post("/tickets/{ticket_id}/comments", response_model=TicketDetail)
async def add_comment(
    ticket_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    body: Annotated[str, Form(min_length=1, max_length=10_000)],
    is_internal: Annotated[bool, Form()] = False,
    file: Annotated[UploadFile | None, File()] = None,
) -> TicketDetail:
    """Reply on a ticket. Agents may set `is_internal` for a note the employee
    never sees."""
    data, name = await _read_upload(file)
    return await service.add_comment(
        session,
        user,
        ticket_id,
        body=body,
        is_internal=is_internal,
        file_data=data,
        file_name=name,
        request=request,
    )


@router.post("/tickets/{ticket_id}/cancel", response_model=TicketDetail)
async def cancel_ticket(
    ticket_id: uuid.UUID,
    payload: CancelIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TicketDetail:
    return await service.cancel_ticket(
        session, user, ticket_id, reason=payload.reason, request=request
    )


@router.post("/tickets/{ticket_id}/reopen", response_model=TicketDetail)
async def reopen_ticket(
    ticket_id: uuid.UUID,
    payload: ReopenIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TicketDetail:
    return await service.reopen_ticket(
        session, user, ticket_id, reason=payload.reason, request=request
    )


@router.post("/tickets/{ticket_id}/rate", response_model=TicketDetail)
async def rate_ticket(
    ticket_id: uuid.UUID,
    payload: RateIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TicketDetail:
    return await service.rate_ticket(
        session, user, ticket_id, rating=payload.rating, comment=payload.comment, request=request
    )


@router.get("/attachments/{attachment_id}")
async def download_attachment(
    attachment_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    filename, mime, data = await service.download_attachment(
        session, user, attachment_id, request=request
    )
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── The agents' queue ───────────────────────────────────────────────────────


@router.get("/queue", response_model=list[TicketSummary])
async def queue(
    user: CurrentUser,
    session: DbSession,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    category_id: uuid.UUID | None = None,
    assignee: Annotated[str | None, Query(max_length=64)] = None,
    priority: HelpdeskPriority | None = None,
    overdue: bool = False,
    search: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[TicketSummary]:
    """`assignee` is `me`, `unassigned` or a user id."""
    return await service.list_queue(
        session,
        user,
        status_filter=status_filter,
        category_id=category_id,
        assignee=assignee,
        priority=priority,
        overdue=overdue,
        search=search,
        limit=limit,
    )


@router.get("/queue/summary", response_model=QueueSummary)
async def queue_summary(user: CurrentUser, session: DbSession) -> QueueSummary:
    return await service.queue_summary(session, user)


@router.get("/agents", response_model=list[AgentOption])
async def agents(user: CurrentUser, session: DbSession) -> list[AgentOption]:
    return await service.list_agents(session, user)


@router.get("/tickets/{ticket_id}/assignees", response_model=list[AgentOption])
async def ticket_assignees(
    ticket_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[AgentOption]:
    return await service.ticket_assignees(session, user, ticket_id)


@router.post("/tickets/{ticket_id}/assign", response_model=TicketDetail)
async def assign_ticket(
    ticket_id: uuid.UUID,
    payload: AssignIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TicketDetail:
    return await service.assign_ticket(
        session, user, ticket_id, assignee_user_id=payload.assignee_user_id, request=request
    )


@router.patch("/tickets/{ticket_id}", response_model=TicketDetail)
async def update_ticket(
    ticket_id: uuid.UUID,
    payload: TicketAgentUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TicketDetail:
    return await service.update_ticket(session, user, ticket_id, payload, request=request)
