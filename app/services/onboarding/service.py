"""Onboarding: checklists, plans, the tasks on them, and who may do what.

The idiom is exit clearance's, turned around. HR keeps checklists; starting
onboarding for a person copies one into a plan, resolving every task's due date
from the joining date and its assignee from the people around the joiner.

Who may do what, decided here rather than in a router:

- **HR** manages checklists, starts and cancels plans, and may reassign, re-date,
  skip or complete any task — except an acknowledgement, which only the person
  acknowledging can give (`permissions.require_self`'s rule), and except on
  their own plan: nobody starts or runs their own onboarding.
- **The assignee** (manager, buddy, IT, finance, a named HR person) completes
  their own task.
- **The new joiner** completes their own tasks — decided by whose plan it is,
  not by the assignee column, so a pre-joiner who is given a login after the
  plan started still has their tasks.
- **A document upload task** is completed by uploading, into the joiner's
  document vault through the shared storage service. Nobody can tick it off
  without a file except HR, who may have received it on paper.

A plan completes itself when its last mandatory task is done or skipped.
"""

from __future__ import annotations

import contextlib
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.config import settings
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import (
    DocumentVerification,
    Employee,
    EmployeeDocument,
    EmploymentStatus,
)
from app.models.notifications import Channel, DigestMode
from app.models.onboarding import (
    OnboardingOwnerType,
    OnboardingPlan,
    OnboardingPlanStatus,
    OnboardingTask,
    OnboardingTaskStatus,
    OnboardingTaskType,
    OnboardingTemplate,
    OnboardingTemplateTask,
)
from app.models.org import Department, Location
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.onboarding import PlanCreate, TemplateCreate, TemplateTaskIn, TemplateUpdate
from app.services import audit
from app.services.notifications.dispatch import notify, register_event
from app.services.onboarding import rules
from app.services.storage import FileRejectedError, StorageError, get_storage, validate_upload

__all__ = [
    "activate_due_joiners",
    "auto_start_on_conversion",
    "cancel_plan",
    "complete_task",
    "create_standard_template",
    "create_template",
    "delete_template",
    "get_template",
    "list_plans",
    "list_templates",
    "my_onboarding",
    "my_summary",
    "my_tasks",
    "owner_candidates",
    "plan_detail",
    "send_overdue_reminders",
    "skip_task",
    "start_plan",
    "task_document",
    "task_read",
    "update_task",
    "update_template",
    "upload_for_task",
]

# ── Events ──────────────────────────────────────────────────────────────────
# A pre-joiner may not have signed in yet, so the welcome and the assignment go
# by email as well as in the app.

_EMAIL_AND_APP = {Channel.EMAIL: DigestMode.IMMEDIATE, Channel.IN_APP: DigestMode.IMMEDIATE}

register_event(
    "onboarding.plan.started",
    "Welcome to {company}, {first_name}",
    "Your onboarding checklist is ready: {task_count} task(s) around your joining date of "
    "{joining_date}, {before_joining} of them before day one. Open My onboarding in AskMira "
    "to get started.",
    preferences=_EMAIL_AND_APP,
)
register_event(
    "onboarding.task.assigned",
    "Onboarding: {count} task(s) for {employee_name}",
    "{employee_name} joins on {joining_date}. You have {count} onboarding task(s) for them; "
    "the first, '{first_title}', is due on {first_due}. Open My tasks under Onboarding.",
    preferences=_EMAIL_AND_APP,
)
register_event(
    "onboarding.task.overdue",
    "Onboarding: {count} task(s) overdue",
    "'{first_title}' for {employee_name} was due on {first_due}. You have {count} overdue "
    "onboarding task(s) in all. Open My tasks under Onboarding.",
    preferences=_EMAIL_AND_APP,
)

_TEMPLATE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Onboarding checklist not found"
)
_PLAN_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Onboarding plan not found"
)
_TASK_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Onboarding task not found"
)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _day(value: date) -> str:
    """'7 Oct 2026' — the way a date reads in a notification."""
    return f"{value.day} {value.strftime('%b %Y')}"


# ═══════════════════════════════════════════════════════════════════════════
# Lookups
# ═══════════════════════════════════════════════════════════════════════════


async def _user_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = await session.execute(
        tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids)), User, user)
    )
    return {row_id: name for row_id, name in rows.all()}


async def _employees(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, Employee]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = await session.scalars(
        tenant_scoped(select(Employee).where(Employee.id.in_(ids)), Employee, user)
    )
    return {row.id: row for row in rows.all()}


async def _logins(
    session: AsyncSession, user: User, employee_ids: set[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Each employee's active login, where they have one."""
    employee_ids = {i for i in employee_ids if i}
    if not employee_ids:
        return {}
    rows = await session.execute(
        tenant_scoped(
            select(User.employee_id, User.id).where(
                User.employee_id.in_(employee_ids), User.is_active.is_(True)
            ),
            User,
            user,
        )
    )
    return {employee_id: user_id for employee_id, user_id in rows.all()}


async def _names_by_id(
    session: AsyncSession, user: User, model: type[Department] | type[Location], ids: set
) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = await session.execute(
        tenant_scoped(select(model.id, model.name).where(model.id.in_(ids)), model, user)
    )
    return {row_id: name for row_id, name in rows.all()}


async def _active_user(session: AsyncSession, user: User, user_id: uuid.UUID) -> User:
    found = await session.scalar(
        tenant_scoped(select(User).where(User.id == user_id, User.is_active.is_(True)), User, user)
    )
    if found is None:
        raise _unprocessable("Choose an active login in this workspace.")
    return found


# ═══════════════════════════════════════════════════════════════════════════
# Checklists
# ═══════════════════════════════════════════════════════════════════════════


async def _template(
    session: AsyncSession, user: User, template_id: uuid.UUID
) -> OnboardingTemplate:
    template = await session.scalar(
        tenant_scoped(
            select(OnboardingTemplate).where(OnboardingTemplate.id == template_id),
            OnboardingTemplate,
            user,
        )
    )
    if template is None:
        raise _TEMPLATE_NOT_FOUND
    return template


async def _template_tasks(
    session: AsyncSession, user: User, template_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[OnboardingTemplateTask]]:
    out: dict[uuid.UUID, list[OnboardingTemplateTask]] = defaultdict(list)
    if not template_ids:
        return out
    rows = await session.scalars(
        tenant_scoped(
            select(OnboardingTemplateTask)
            .where(OnboardingTemplateTask.template_id.in_(template_ids))
            .order_by(OnboardingTemplateTask.sort_order, OnboardingTemplateTask.created_at),
            OnboardingTemplateTask,
            user,
        )
    )
    for row in rows.all():
        out[row.template_id].append(row)
    return out


async def _template_reads(
    session: AsyncSession, user: User, templates: list[OnboardingTemplate]
) -> list[dict[str, Any]]:
    ids = [t.id for t in templates]
    tasks = await _template_tasks(session, user, ids)
    plan_counts: dict[uuid.UUID, int] = {}
    if ids:
        rows = await session.execute(
            tenant_scoped(
                select(OnboardingPlan.template_id, func.count(OnboardingPlan.id))
                .where(OnboardingPlan.template_id.in_(ids))
                .group_by(OnboardingPlan.template_id),
                OnboardingPlan,
                user,
            )
        )
        plan_counts = {template_id: count for template_id, count in rows.all()}
    owners = await _user_names(
        session, user, {t.owner_user_id for rows in tasks.values() for t in rows if t.owner_user_id}
    )
    departments = await _names_by_id(
        session, user, Department, {t.department_id for t in templates}
    )
    locations = await _names_by_id(session, user, Location, {t.location_id for t in templates})
    out = []
    for template in templates:
        rows = tasks.get(template.id, [])
        out.append(
            {
                "id": template.id,
                "name": template.name,
                "description": template.description,
                "department_id": template.department_id,
                "department_name": departments.get(template.department_id)
                if template.department_id
                else None,
                "location_id": template.location_id,
                "location_name": locations.get(template.location_id)
                if template.location_id
                else None,
                "active": template.active,
                "is_default": template.is_default,
                "task_count": len(rows),
                "plan_count": plan_counts.get(template.id, 0),
                "tasks": [
                    {
                        "id": row.id,
                        "title": row.title,
                        "description": row.description,
                        "owner_type": row.owner_type,
                        "owner_user_id": row.owner_user_id,
                        "owner_name": owners.get(row.owner_user_id) if row.owner_user_id else None,
                        "due_offset_days": row.due_offset_days,
                        "task_type": row.task_type,
                        "document_type": row.document_type,
                        "sort_order": row.sort_order,
                        "is_mandatory": row.is_mandatory,
                    }
                    for row in rows
                ],
                "created_at": template.created_at,
                "updated_at": template.updated_at,
            }
        )
    return out


async def list_templates(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    permissions.require_hr(user, detail="Only HR manages onboarding checklists.")
    templates = (
        await session.scalars(
            tenant_scoped(
                select(OnboardingTemplate).order_by(
                    OnboardingTemplate.is_default.desc(),
                    OnboardingTemplate.active.desc(),
                    OnboardingTemplate.name,
                ),
                OnboardingTemplate,
                user,
            )
        )
    ).all()
    return await _template_reads(session, user, list(templates))


async def get_template(session: AsyncSession, user: User, template_id: uuid.UUID) -> dict[str, Any]:
    permissions.require_hr(user, detail="Only HR manages onboarding checklists.")
    template = await _template(session, user, template_id)
    return (await _template_reads(session, user, [template]))[0]


async def _check_placement(
    session: AsyncSession,
    user: User,
    department_id: uuid.UUID | None,
    location_id: uuid.UUID | None,
) -> None:
    if department_id is not None:
        found = await session.scalar(
            tenant_scoped(
                select(Department.id).where(Department.id == department_id), Department, user
            )
        )
        if found is None:
            raise _unprocessable("That department does not exist in this workspace.")
    if location_id is not None:
        found = await session.scalar(
            tenant_scoped(select(Location.id).where(Location.id == location_id), Location, user)
        )
        if found is None:
            raise _unprocessable("That location does not exist in this workspace.")


async def _check_name_free(
    session: AsyncSession, user: User, name: str, *, except_id: uuid.UUID | None = None
) -> None:
    stmt = select(OnboardingTemplate.id).where(
        func.lower(OnboardingTemplate.name) == name.strip().lower()
    )
    if except_id is not None:
        stmt = stmt.where(OnboardingTemplate.id != except_id)
    if await session.scalar(tenant_scoped(stmt, OnboardingTemplate, user)) is not None:
        raise _conflict(f"A checklist called '{name.strip()}' already exists.")


async def _clear_other_defaults(session: AsyncSession, user: User, keep_id: uuid.UUID) -> None:
    others = (
        await session.scalars(
            tenant_scoped(
                select(OnboardingTemplate).where(
                    OnboardingTemplate.is_default.is_(True), OnboardingTemplate.id != keep_id
                ),
                OnboardingTemplate,
                user,
            )
        )
    ).all()
    for other in others:
        other.is_default = False


async def _write_tasks(
    session: AsyncSession,
    user: User,
    template: OnboardingTemplate,
    tasks: list[TemplateTaskIn],
) -> None:
    named = {t.owner_user_id for t in tasks if t.owner_user_id}
    for owner_id in named:
        await _active_user(session, user, owner_id)
    for index, task in enumerate(tasks):
        session.add(
            OnboardingTemplateTask(
                tenant_id=user.tenant_id,
                template_id=template.id,
                title=task.title.strip(),
                description=(task.description or "").strip() or None,
                owner_type=task.owner_type,
                owner_user_id=task.owner_user_id,
                due_offset_days=task.due_offset_days,
                task_type=task.task_type,
                document_type=task.document_type.value if task.document_type else None,
                sort_order=index,
                is_mandatory=task.is_mandatory,
            )
        )


async def create_template(
    session: AsyncSession,
    user: User,
    payload: TemplateCreate,
    *,
    request: Request | None = None,
) -> OnboardingTemplate:
    permissions.require_hr(user, detail="Only HR manages onboarding checklists.")
    await _check_name_free(session, user, payload.name)
    await _check_placement(session, user, payload.department_id, payload.location_id)
    if payload.is_default and not payload.active:
        raise _unprocessable("An inactive checklist cannot be the default.")

    template = OnboardingTemplate(
        tenant_id=user.tenant_id,
        name=payload.name.strip(),
        description=(payload.description or "").strip() or None,
        department_id=payload.department_id,
        location_id=payload.location_id,
        active=payload.active,
        is_default=payload.is_default,
    )
    session.add(template)
    await session.flush()
    if payload.is_default:
        await _clear_other_defaults(session, user, template.id)
    await _write_tasks(session, user, template, payload.tasks)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="onboarding_template",
        entity_id=template.id,
        summary=(
            f"Created the onboarding checklist '{template.name}' with {len(payload.tasks)} task(s)"
        ),
        changes={"tasks": len(payload.tasks), "is_default": payload.is_default},
        request=request,
    )
    return template


async def update_template(
    session: AsyncSession,
    user: User,
    template_id: uuid.UUID,
    payload: TemplateUpdate,
    *,
    request: Request | None = None,
) -> OnboardingTemplate:
    """Edit a checklist. Plans already started keep the tasks they started with."""
    permissions.require_hr(user, detail="Only HR manages onboarding checklists.")
    template = await _template(session, user, template_id)
    sent = payload.model_fields_set
    changes: dict[str, Any] = {}

    if payload.name is not None and payload.name.strip() != template.name:
        await _check_name_free(session, user, payload.name, except_id=template.id)
        changes["name"] = payload.name.strip()
        template.name = payload.name.strip()
    if "description" in sent:
        template.description = (payload.description or "").strip() or None
        changes["description"] = "updated"
    if "department_id" in sent or "location_id" in sent:
        department_id = payload.department_id if "department_id" in sent else template.department_id
        location_id = payload.location_id if "location_id" in sent else template.location_id
        await _check_placement(session, user, department_id, location_id)
        template.department_id = department_id
        template.location_id = location_id
        changes["applies_to"] = {
            "department_id": str(department_id) if department_id else None,
            "location_id": str(location_id) if location_id else None,
        }
    if payload.active is not None:
        template.active = payload.active
        changes["active"] = payload.active
        if not payload.active:
            template.is_default = False
    if payload.is_default is not None:
        if payload.is_default and not template.active:
            raise _unprocessable("An inactive checklist cannot be the default.")
        template.is_default = payload.is_default
        changes["is_default"] = payload.is_default
        if payload.is_default:
            await _clear_other_defaults(session, user, template.id)
    if payload.tasks is not None:
        existing = (
            await session.scalars(
                tenant_scoped(
                    select(OnboardingTemplateTask).where(
                        OnboardingTemplateTask.template_id == template.id
                    ),
                    OnboardingTemplateTask,
                    user,
                )
            )
        ).all()
        for row in existing:
            await session.delete(row)
        await session.flush()
        await _write_tasks(session, user, template, payload.tasks)
        changes["tasks"] = len(payload.tasks)

    template.updated_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="onboarding_template",
        entity_id=template.id,
        summary=f"Edited the onboarding checklist '{template.name}'",
        changes=changes,
        request=request,
    )
    return template


async def delete_template(
    session: AsyncSession,
    user: User,
    template_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> None:
    """Delete a checklist nobody has been onboarded with. Otherwise deactivate it."""
    permissions.require_hr(user, detail="Only HR manages onboarding checklists.")
    template = await _template(session, user, template_id)
    used = await session.scalar(
        tenant_scoped(
            select(func.count(OnboardingPlan.id)).where(OnboardingPlan.template_id == template.id),
            OnboardingPlan,
            user,
        )
    )
    if used:
        raise _conflict(
            f"{used} onboarding plan(s) were started from this checklist, so it is kept for "
            "their history. Deactivate it instead."
        )
    name = template.name
    await session.delete(template)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.DELETE,
        entity_type="onboarding_template",
        entity_id=template_id,
        summary=f"Deleted the unused onboarding checklist '{name}'",
        request=request,
    )


async def create_standard_template(
    session: AsyncSession, user: User, *, request: Request | None = None
) -> OnboardingTemplate:
    """Build the standard Indian checklist — only because HR asked for it."""
    permissions.require_hr(user, detail="Only HR manages onboarding checklists.")
    has_default = await session.scalar(
        tenant_scoped(
            select(OnboardingTemplate.id).where(OnboardingTemplate.is_default.is_(True)),
            OnboardingTemplate,
            user,
        )
    )
    payload = TemplateCreate(
        name=rules.STANDARD_TEMPLATE_NAME,
        description=(
            "Offer acceptance, KYC and bank proof, education and previous-employment "
            "documents, PF nomination and Form 11, IT provisioning, ID card, manager and "
            "buddy introductions, policy acknowledgements and a day-30 check-in. Edit it "
            "to match how your company onboards."
        ),
        active=True,
        is_default=has_default is None,
        tasks=[
            TemplateTaskIn(
                title=task.title,
                description=task.description,
                owner_type=task.owner_type,
                due_offset_days=task.due_offset_days,
                task_type=task.task_type,
                document_type=task.document_type,  # type: ignore[arg-type]
                is_mandatory=task.is_mandatory,
            )
            for task in rules.STANDARD_TASKS
        ],
    )
    return await create_template(session, user, payload, request=request)


async def owner_candidates(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Active logins a task can be given to (IT admins often hold an employee role)."""
    permissions.require_hr(user, detail="Only HR assigns onboarding work.")
    rows = (
        await session.scalars(
            tenant_scoped(
                select(User).where(User.is_active.is_(True)).order_by(User.full_name),
                User,
                user,
            )
        )
    ).all()
    return [
        {"user_id": row.id, "full_name": row.full_name, "email": row.email, "role": row.role.value}
        for row in rows
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Plans: reading
# ═══════════════════════════════════════════════════════════════════════════


def _facts(task: OnboardingTask) -> rules.TaskFacts:
    return rules.TaskFacts(
        status=task.status,
        due_date=task.due_date,
        is_mandatory=task.is_mandatory,
        owner_type=task.owner_type,
        assignee_user_id=task.assignee_user_id,
    )


async def _plan(session: AsyncSession, user: User, plan_id: uuid.UUID) -> OnboardingPlan:
    plan = await session.scalar(
        tenant_scoped(
            select(OnboardingPlan).where(OnboardingPlan.id == plan_id), OnboardingPlan, user
        )
    )
    if plan is None:
        raise _PLAN_NOT_FOUND
    return plan


async def _plan_tasks(
    session: AsyncSession, user: User, plan_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[OnboardingTask]]:
    out: dict[uuid.UUID, list[OnboardingTask]] = defaultdict(list)
    if not plan_ids:
        return out
    rows = await session.scalars(
        tenant_scoped(
            select(OnboardingTask)
            .where(OnboardingTask.plan_id.in_(plan_ids))
            .order_by(OnboardingTask.due_date, OnboardingTask.sort_order),
            OnboardingTask,
            user,
        )
    )
    for row in rows.all():
        out[row.plan_id].append(row)
    return out


def _is_joiner(user: User, plan: OnboardingPlan) -> bool:
    return user.employee_id is not None and user.employee_id == plan.employee_id


def can_complete(user: User, plan: OnboardingPlan, task: OnboardingTask) -> bool:
    """Whether this caller may mark this task done (or upload for it)."""
    if plan.status is not OnboardingPlanStatus.IN_PROGRESS:
        return False
    if task.status is not OnboardingTaskStatus.PENDING:
        return False
    joiner = _is_joiner(user, plan)
    is_assignee = user.id is not None and task.assignee_user_id == user.id
    if task.owner_type is OnboardingOwnerType.NEW_JOINER:
        if joiner:
            return True
        # An acknowledgement given by somebody else is not an acknowledgement.
        if task.task_type is OnboardingTaskType.ACKNOWLEDGE_POLICY:
            return False
        return permissions.is_hr(user)
    # Work other people do for the joiner is never ticked off by the joiner.
    if joiner:
        return False
    if task.task_type is OnboardingTaskType.ACKNOWLEDGE_POLICY:
        return is_assignee
    return is_assignee or permissions.is_hr(user)


def can_manage(user: User, plan: OnboardingPlan) -> bool:
    """HR may reassign, re-date and skip — never on their own plan."""
    return (
        permissions.is_hr(user)
        and not _is_joiner(user, plan)
        and plan.status is OnboardingPlanStatus.IN_PROGRESS
    )


async def _task_reads(
    session: AsyncSession,
    user: User,
    plans: dict[uuid.UUID, OnboardingPlan],
    tasks: list[OnboardingTask],
    *,
    today: date,
) -> list[dict[str, Any]]:
    names = await _user_names(
        session,
        user,
        {t.assignee_user_id for t in tasks if t.assignee_user_id}
        | {t.completed_by_user_id for t in tasks if t.completed_by_user_id},
    )
    document_ids = {t.document_id for t in tasks if t.document_id}
    documents: dict[uuid.UUID, str] = {}
    if document_ids:
        rows = await session.execute(
            tenant_scoped(
                select(EmployeeDocument.id, EmployeeDocument.title).where(
                    EmployeeDocument.id.in_(document_ids)
                ),
                EmployeeDocument,
                user,
            )
        )
        documents = {doc_id: title for doc_id, title in rows.all()}
    out = []
    for task in tasks:
        plan = plans[task.plan_id]
        out.append(
            {
                "id": task.id,
                "plan_id": task.plan_id,
                "title": task.title,
                "description": task.description,
                "owner_type": task.owner_type,
                "assignee_user_id": task.assignee_user_id,
                "assignee_name": names.get(task.assignee_user_id)
                if task.assignee_user_id
                else None,
                "due_date": task.due_date,
                "phase": rules.phase_of(task.due_date, plan.joining_date),
                "is_overdue": task.status is OnboardingTaskStatus.PENDING and task.due_date < today,
                "task_type": task.task_type,
                "document_type": task.document_type,
                "sort_order": task.sort_order,
                "is_mandatory": task.is_mandatory,
                "status": task.status,
                "completed_by_name": names.get(task.completed_by_user_id)
                if task.completed_by_user_id
                else None,
                "completed_at": task.completed_at,
                "note": task.note,
                "document_id": task.document_id,
                "document_title": documents.get(task.document_id) if task.document_id else None,
                "can_complete": can_complete(user, plan, task),
                "can_manage": can_manage(user, plan),
            }
        )
    return out


async def _plan_reads(
    session: AsyncSession,
    user: User,
    plans: list[OnboardingPlan],
    *,
    with_tasks: bool,
) -> list[dict[str, Any]]:
    if not plans:
        return []
    today = rules.india_today()
    tasks = await _plan_tasks(session, user, [p.id for p in plans])
    employees = await _employees(
        session,
        user,
        {p.employee_id for p in plans}
        | {p.buddy_employee_id for p in plans if p.buddy_employee_id},
    )
    managers = await _employees(
        session, user, {e.manager_id for e in employees.values() if e.manager_id}
    )
    departments = await _names_by_id(
        session, user, Department, {e.department_id for e in employees.values()}
    )
    starters = await _user_names(session, user, {p.started_by_user_id for p in plans})
    by_id = {p.id: p for p in plans}

    out = []
    for plan in plans:
        employee = employees.get(plan.employee_id)
        buddy = employees.get(plan.buddy_employee_id) if plan.buddy_employee_id else None
        manager = managers.get(employee.manager_id) if employee and employee.manager_id else None
        rows = tasks.get(plan.id, [])
        progress = rules.progress_of((_facts(t) for t in rows), today=today)
        read: dict[str, Any] = {
            "id": plan.id,
            "employee_id": plan.employee_id,
            "employee_name": employee.full_name if employee else "Unknown employee",
            "employee_code": employee.employee_code if employee else "",
            "employee_status": employee.status.value if employee else "unknown",
            "designation": employee.designation if employee else None,
            "department_id": employee.department_id if employee else None,
            "department_name": departments.get(employee.department_id)
            if employee and employee.department_id
            else None,
            "manager_name": manager.full_name if manager else None,
            "template_id": plan.template_id,
            "template_name": plan.template_name,
            "joining_date": plan.joining_date,
            "buddy_employee_id": plan.buddy_employee_id,
            "buddy_name": buddy.full_name if buddy else None,
            "status": plan.status,
            "started_by_name": starters.get(plan.started_by_user_id)
            if plan.started_by_user_id
            else None,
            "completed_at": plan.completed_at,
            "cancelled_at": plan.cancelled_at,
            "cancel_reason": plan.cancel_reason,
            "progress": progress.as_dict(),
            "created_at": plan.created_at,
        }
        if with_tasks:
            read["tasks"] = await _task_reads(session, user, by_id, rows, today=today)
        out.append(read)
    return out


async def list_plans(
    session: AsyncSession,
    user: User,
    *,
    plan_status: OnboardingPlanStatus | None = None,
    department_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Every plan in the workspace, newest joiners first (HR)."""
    permissions.require_hr(user, detail="Only HR sees every new joiner's onboarding.")
    stmt = (
        select(OnboardingPlan)
        .join(Employee, Employee.id == OnboardingPlan.employee_id)
        .order_by(OnboardingPlan.joining_date.desc(), OnboardingPlan.created_at.desc())
    )
    if plan_status is not None:
        stmt = stmt.where(OnboardingPlan.status == plan_status)
    if department_id is not None:
        stmt = stmt.where(Employee.department_id == department_id)
    plans = (await session.scalars(tenant_scoped(stmt, OnboardingPlan, user))).all()
    return await _plan_reads(session, user, list(plans), with_tasks=False)


async def _visible_plan(session: AsyncSession, user: User, plan_id: uuid.UUID) -> OnboardingPlan:
    """HR, the joiner, their buddy, their line, or anyone holding a task on it."""
    plan = await _plan(session, user, plan_id)
    if permissions.is_hr(user) or _is_joiner(user, plan):
        return plan
    if user.employee_id is not None and user.employee_id == plan.buddy_employee_id:
        return plan
    holds_task = await session.scalar(
        tenant_scoped(
            select(OnboardingTask.id).where(
                OnboardingTask.plan_id == plan.id, OnboardingTask.assignee_user_id == user.id
            ),
            OnboardingTask,
            user,
        )
    )
    if holds_task is not None:
        return plan
    if await permissions.manages(session, user, plan.employee_id):
        return plan
    raise _PLAN_NOT_FOUND


async def plan_detail(session: AsyncSession, user: User, plan_id: uuid.UUID) -> dict[str, Any]:
    plan = await _visible_plan(session, user, plan_id)
    return (await _plan_reads(session, user, [plan], with_tasks=True))[0]


# ═══════════════════════════════════════════════════════════════════════════
# Plans: starting and cancelling
# ═══════════════════════════════════════════════════════════════════════════


async def start_plan(
    session: AsyncSession,
    user: User,
    payload: PlanCreate,
    *,
    request: Request | None = None,
) -> OnboardingPlan:
    """Copy a checklist onto one person, resolving dates and owners, and tell them."""
    permissions.require_hr(user, detail="Only HR starts onboarding.")
    if user.employee_id is not None and payload.employee_id == user.employee_id:
        raise permissions.forbidden("Somebody else in HR starts your own onboarding.")

    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(
                Employee.id == payload.employee_id, Employee.deleted_at.is_(None)
            ),
            Employee,
            user,
        )
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    if employee.status is EmploymentStatus.EXITED:
        raise _conflict(f"{employee.full_name} has left; there is nobody to onboard.")

    template = await _template(session, user, payload.template_id)
    if not template.active:
        raise _conflict(f"The checklist '{template.name}' is inactive. Activate it first.")
    template_tasks = (await _template_tasks(session, user, [template.id])).get(template.id, [])
    if not template_tasks:
        raise _conflict(f"The checklist '{template.name}' has no tasks yet. Add some first.")

    joining_date = payload.joining_date or employee.date_of_joining
    if joining_date is None:
        raise _unprocessable(
            f"{employee.full_name} has no date of joining on record. Give one to date the "
            "checklist from."
        )

    existing = await session.scalar(
        tenant_scoped(
            select(OnboardingPlan.id).where(
                OnboardingPlan.employee_id == employee.id,
                OnboardingPlan.status == OnboardingPlanStatus.IN_PROGRESS,
            ),
            OnboardingPlan,
            user,
        )
    )
    if existing is not None:
        raise _conflict(
            f"{employee.full_name} already has onboarding in progress. Cancel it before "
            "starting another."
        )

    buddy: Employee | None = None
    if payload.buddy_employee_id is not None:
        if payload.buddy_employee_id == employee.id:
            raise _unprocessable("A new joiner cannot be their own buddy.")
        buddy = await session.scalar(
            tenant_scoped(
                select(Employee).where(
                    Employee.id == payload.buddy_employee_id, Employee.deleted_at.is_(None)
                ),
                Employee,
                user,
            )
        )
        if buddy is None:
            raise _unprocessable("The buddy must be an employee in this workspace.")
        if buddy.status in (EmploymentStatus.EXITED, EmploymentStatus.PRE_JOINING):
            raise _unprocessable(
                f"{buddy.full_name} is {buddy.status.value.replace('_', '-')}; a buddy has "
                "to be somebody already working here."
            )

    logins = await _logins(
        session,
        user,
        {employee.id, employee.manager_id, buddy.id if buddy else None},
    )
    # A named owner who has since been deactivated falls back to the starter.
    named = {t.owner_user_id for t in template_tasks if t.owner_user_id}
    live_named: set[uuid.UUID] = set()
    if named:
        live_named = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(User.id).where(User.id.in_(named), User.is_active.is_(True)),
                        User,
                        user,
                    )
                )
            ).all()
        )
    ctx = rules.AssigneeContext(
        joiner_user_id=logins.get(employee.id),
        manager_user_id=logins.get(employee.manager_id) if employee.manager_id else None,
        buddy_user_id=logins.get(buddy.id) if buddy else None,
        starter_user_id=user.id,
    )

    plan = OnboardingPlan(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        template_id=template.id,
        template_name=template.name,
        joining_date=joining_date,
        buddy_employee_id=buddy.id if buddy else None,
        status=OnboardingPlanStatus.IN_PROGRESS,
        started_by_user_id=user.id,
    )
    session.add(plan)
    await session.flush()

    created: list[OnboardingTask] = []
    for index, source in enumerate(template_tasks):
        owner_user_id = source.owner_user_id if source.owner_user_id in live_named else None
        task = OnboardingTask(
            tenant_id=user.tenant_id,
            plan_id=plan.id,
            template_task_id=source.id,
            title=source.title,
            description=source.description,
            owner_type=source.owner_type,
            assignee_user_id=rules.resolve_assignee(source.owner_type, owner_user_id, ctx),
            due_date=rules.due_date_for(joining_date, source.due_offset_days),
            task_type=source.task_type,
            document_type=source.document_type,
            sort_order=index,
            is_mandatory=source.is_mandatory,
            status=OnboardingTaskStatus.PENDING,
        )
        session.add(task)
        created.append(task)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="onboarding_plan",
        entity_id=plan.id,
        summary=(
            f"Started onboarding for {employee.employee_code} from '{template.name}', "
            f"joining {joining_date}"
        ),
        changes={
            "employee_id": str(employee.id),
            "template_id": str(template.id),
            "joining_date": joining_date.isoformat(),
            "buddy_employee_id": str(buddy.id) if buddy else None,
            "tasks": len(created),
            "unassigned": sum(
                1
                for t in created
                if t.assignee_user_id is None and t.owner_type is not OnboardingOwnerType.NEW_JOINER
            ),
        },
        request=request,
    )
    await _notify_started(session, user, plan, employee, created)
    return plan


async def _notify_started(
    session: AsyncSession,
    user: User,
    plan: OnboardingPlan,
    employee: Employee,
    tasks: list[OnboardingTask],
) -> None:
    """Welcome the joiner; tell every other assignee what is theirs. Never the actor."""
    tenant = await session.get(Tenant, user.tenant_id)
    joiner_login = (await _logins(session, user, {employee.id})).get(employee.id)
    if joiner_login is not None and joiner_login != user.id:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="onboarding.plan.started",
            recipient_user_ids=[joiner_login],
            payload={
                "company": tenant.name if tenant else "the team",
                "first_name": employee.first_name,
                "joining_date": _day(plan.joining_date),
                "task_count": sum(
                    1 for t in tasks if t.owner_type is OnboardingOwnerType.NEW_JOINER
                ),
                "before_joining": sum(
                    1
                    for t in tasks
                    if t.owner_type is OnboardingOwnerType.NEW_JOINER
                    and t.due_date < plan.joining_date
                ),
            },
            commit=False,
        )
    await _notify_assigned(
        session,
        user,
        plan,
        employee,
        [t for t in tasks if t.owner_type is not OnboardingOwnerType.NEW_JOINER],
    )


async def _notify_assigned(
    session: AsyncSession,
    user: User,
    plan: OnboardingPlan,
    employee: Employee,
    tasks: list[OnboardingTask],
) -> None:
    grouped: dict[uuid.UUID, list[OnboardingTask]] = defaultdict(list)
    for task in tasks:
        if task.assignee_user_id is not None and task.assignee_user_id != user.id:
            grouped[task.assignee_user_id].append(task)
    for recipient, theirs in grouped.items():
        first = min(theirs, key=lambda t: (t.due_date, t.sort_order))
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="onboarding.task.assigned",
            recipient_user_ids=[recipient],
            payload={
                "employee_name": employee.full_name,
                "joining_date": _day(plan.joining_date),
                "count": len(theirs),
                "first_title": first.title,
                "first_due": _day(first.due_date),
            },
            commit=False,
        )


async def cancel_plan(
    session: AsyncSession,
    user: User,
    plan_id: uuid.UUID,
    *,
    reason: str,
    request: Request | None = None,
) -> OnboardingPlan:
    """Stop a plan (an offer that fell through, a restart). Nothing is deleted."""
    permissions.require_hr(user, detail="Only HR cancels onboarding.")
    plan = await _plan(session, user, plan_id)
    if _is_joiner(user, plan):
        raise permissions.forbidden("Somebody else in HR changes your own onboarding.")
    if plan.status is not OnboardingPlanStatus.IN_PROGRESS:
        raise _conflict(f"This plan is already {plan.status.value.replace('_', ' ')}.")
    plan.status = OnboardingPlanStatus.CANCELLED
    plan.cancelled_at = datetime.now(UTC)
    plan.cancel_reason = reason.strip()
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="onboarding_plan",
        entity_id=plan.id,
        summary=f"Cancelled onboarding: {reason.strip()}",
        changes={"status": OnboardingPlanStatus.CANCELLED.value, "reason": reason.strip()},
        request=request,
    )
    return plan


# ═══════════════════════════════════════════════════════════════════════════
# Tasks
# ═══════════════════════════════════════════════════════════════════════════


async def _task_and_plan(
    session: AsyncSession, user: User, task_id: uuid.UUID
) -> tuple[OnboardingTask, OnboardingPlan]:
    task = await session.scalar(
        tenant_scoped(
            select(OnboardingTask).where(OnboardingTask.id == task_id), OnboardingTask, user
        )
    )
    if task is None:
        raise _TASK_NOT_FOUND
    try:
        plan = await _visible_plan(session, user, task.plan_id)
    except HTTPException as exc:
        raise _TASK_NOT_FOUND from exc
    return task, plan


async def task_read(session: AsyncSession, user: User, task_id: uuid.UUID) -> dict[str, Any]:
    """One task as the caller now sees it."""
    task, plan = await _task_and_plan(session, user, task_id)
    reads = await _task_reads(session, user, {plan.id: plan}, [task], today=rules.india_today())
    return reads[0]


def _refuse_completion(user: User, plan: OnboardingPlan, task: OnboardingTask) -> None:
    """The reason a caller may not complete this task, in words they can act on."""
    if plan.status is not OnboardingPlanStatus.IN_PROGRESS:
        raise _conflict(f"This onboarding plan is {plan.status.value.replace('_', ' ')}.")
    if task.status is not OnboardingTaskStatus.PENDING:
        raise _conflict(f"'{task.title}' is already {task.status.value}.")
    if not can_complete(user, plan, task):
        if task.task_type is OnboardingTaskType.ACKNOWLEDGE_POLICY:
            raise permissions.forbidden(
                "Only the person acknowledging can record an acknowledgement. HR can skip it "
                "with a reason instead."
            )
        if _is_joiner(user, plan):
            raise permissions.forbidden(
                f"'{task.title}' is done for you by somebody else; they mark it done."
            )
        raise permissions.forbidden(f"'{task.title}' is marked done by its assignee or by HR.")


async def _maybe_complete_plan(
    session: AsyncSession, user: User, plan: OnboardingPlan, *, request: Request | None
) -> bool:
    tasks = (await _plan_tasks(session, user, [plan.id])).get(plan.id, [])
    if plan.status is not OnboardingPlanStatus.IN_PROGRESS or not rules.is_complete(
        _facts(t) for t in tasks
    ):
        return False
    plan.status = OnboardingPlanStatus.COMPLETED
    plan.completed_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="onboarding_plan",
        entity_id=plan.id,
        summary="Onboarding completed: every mandatory task is done or skipped",
        changes={"status": OnboardingPlanStatus.COMPLETED.value},
        request=request,
    )
    return True


async def _mark_done(
    session: AsyncSession,
    user: User,
    plan: OnboardingPlan,
    task: OnboardingTask,
    *,
    note: str | None,
    document_id: uuid.UUID | None = None,
    request: Request | None,
) -> None:
    task.status = OnboardingTaskStatus.DONE
    task.completed_by_user_id = user.id
    task.completed_at = datetime.now(UTC)
    task.note = (note or "").strip() or None
    if document_id is not None:
        task.document_id = document_id
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="onboarding_task",
        entity_id=task.id,
        summary=f"Marked onboarding task '{task.title}' done",
        changes={
            "status": OnboardingTaskStatus.DONE.value,
            "note": task.note,
            "document_id": str(document_id) if document_id else None,
        },
        request=request,
    )
    await _maybe_complete_plan(session, user, plan, request=request)


async def complete_task(
    session: AsyncSession,
    user: User,
    task_id: uuid.UUID,
    *,
    note: str | None = None,
    request: Request | None = None,
) -> OnboardingTask:
    task, plan = await _task_and_plan(session, user, task_id)
    _refuse_completion(user, plan, task)
    if task.task_type is OnboardingTaskType.DOCUMENT_UPLOAD and not permissions.is_hr(user):
        raise _unprocessable(f"'{task.title}' is completed by uploading the document.")
    await _mark_done(session, user, plan, task, note=note, request=request)
    return task


async def skip_task(
    session: AsyncSession,
    user: User,
    task_id: uuid.UUID,
    *,
    reason: str,
    request: Request | None = None,
) -> OnboardingTask:
    """HR decides a task does not apply to this person, and says why."""
    task, plan = await _task_and_plan(session, user, task_id)
    if not can_manage(user, plan):
        raise permissions.forbidden(
            "Only HR can skip an onboarding task, and not on their own plan."
        )
    if task.status is not OnboardingTaskStatus.PENDING:
        raise _conflict(f"'{task.title}' is already {task.status.value}.")
    task.status = OnboardingTaskStatus.SKIPPED
    task.completed_by_user_id = user.id
    task.completed_at = datetime.now(UTC)
    task.note = reason.strip()
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="onboarding_task",
        entity_id=task.id,
        summary=f"Skipped onboarding task '{task.title}': {reason.strip()}",
        changes={"status": OnboardingTaskStatus.SKIPPED.value, "reason": reason.strip()},
        request=request,
    )
    await _maybe_complete_plan(session, user, plan, request=request)
    return task


async def update_task(
    session: AsyncSession,
    user: User,
    task_id: uuid.UUID,
    *,
    assignee_user_id: uuid.UUID | None,
    due_date: date | None,
    request: Request | None = None,
) -> OnboardingTask:
    """HR hands a task to somebody else, or moves its date."""
    task, plan = await _task_and_plan(session, user, task_id)
    if not can_manage(user, plan):
        raise permissions.forbidden(
            "Only HR can reassign an onboarding task, and not on their own plan."
        )
    if task.status is not OnboardingTaskStatus.PENDING:
        raise _conflict(f"'{task.title}' is already {task.status.value}.")
    changes: dict[str, Any] = {}
    newly_assigned = False
    if assignee_user_id is not None and assignee_user_id != task.assignee_user_id:
        if task.owner_type is OnboardingOwnerType.NEW_JOINER:
            raise _conflict(
                f"'{task.title}' is the new joiner's own task and stays with them. Move its "
                "date, skip it with a reason, or mark it done as HR."
            )
        await _active_user(session, user, assignee_user_id)
        changes["assignee_user_id"] = {
            "from": str(task.assignee_user_id) if task.assignee_user_id else None,
            "to": str(assignee_user_id),
        }
        task.assignee_user_id = assignee_user_id
        task.overdue_notified_at = None
        newly_assigned = True
    if due_date is not None and due_date != task.due_date:
        changes["due_date"] = {"from": task.due_date.isoformat(), "to": due_date.isoformat()}
        task.due_date = due_date
        task.overdue_notified_at = None
    if not changes:
        return task
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="onboarding_task",
        entity_id=task.id,
        summary=f"Reassigned or re-dated onboarding task '{task.title}'",
        changes=changes,
        request=request,
    )
    if newly_assigned:
        employee = (await _employees(session, user, {plan.employee_id})).get(plan.employee_id)
        if employee is not None:
            await _notify_assigned(session, user, plan, employee, [task])
    return task


async def upload_for_task(
    session: AsyncSession,
    user: User,
    task_id: uuid.UUID,
    *,
    data: bytes,
    request: Request | None = None,
) -> OnboardingTask:
    """Complete a document task by uploading it into the joiner's vault.

    The same checks as the vault: size and type read from the bytes, a
    generated storage key, the file written before the row and removed again if
    the row does not commit. Commits, because the file cleanup depends on it.
    """
    task, plan = await _task_and_plan(session, user, task_id)
    if task.task_type is not OnboardingTaskType.DOCUMENT_UPLOAD:
        raise _conflict(f"'{task.title}' does not collect a document.")
    _refuse_completion(user, plan, task)

    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"The file is larger than the {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                "limit."
            ),
        )
    try:
        kind = validate_upload(data, max_bytes=settings.MAX_UPLOAD_BYTES)
    except FileRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    employee = (await _employees(session, user, {plan.employee_id})).get(plan.employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    storage = get_storage()
    key = storage.new_key(user.tenant_id, "documents", kind.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    # What the joiner uploads is a claim until HR has looked: it joins the
    # vault's verification queue. A copy HR files for them needs no check.
    verification = (
        DocumentVerification.PENDING
        if _is_joiner(user, plan)
        else DocumentVerification.NOT_REQUIRED
    )
    try:
        document = EmployeeDocument(
            tenant_id=user.tenant_id,
            employee_id=employee.id,
            category=task.document_type or "other",
            title=task.title[:255],
            file_url=key,
            file_size_bytes=len(data),
            mime_type=kind.mime_type,
            is_employee_visible=True,
            uploaded_by_user_id=user.id,
            verification_status=verification.value,
        )
        session.add(document)
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="employee_document",
            entity_id=document.id,
            actor=user,
            changes={
                "employee_code": employee.employee_code,
                "category": document.category,
                "title": document.title,
                "mime_type": kind.mime_type,
                "size_bytes": len(data),
                "verification_status": verification.value,
                "onboarding_task_id": str(task.id),
            },
            summary=(
                f"Uploaded '{document.title}' to {employee.employee_code}'s vault during onboarding"
            ),
            request=request,
        )
        await _mark_done(
            session, user, plan, task, note=None, document_id=document.id, request=request
        )
        await session.commit()
    except BaseException:
        with contextlib.suppress(StorageError):
            await storage.delete(key)
        raise
    return task


async def task_document(
    session: AsyncSession, user: User, task_id: uuid.UUID, *, request: Request | None = None
) -> tuple[str, str, bytes]:
    """The file a document task collected — for the joiner and HR, as in the vault."""
    from app.services.documents import vault

    task, _plan_row = await _task_and_plan(session, user, task_id)
    if task.document_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No document has been uploaded for this task.",
        )
    return await vault.download_document(session, user, task.document_id, request=request)


# ═══════════════════════════════════════════════════════════════════════════
# The caller's own view
# ═══════════════════════════════════════════════════════════════════════════


async def _own_plan(
    session: AsyncSession, user: User, employee_id: uuid.UUID | None
) -> OnboardingPlan | None:
    """The caller's live plan, else their most recent completed one."""
    if employee_id is None:
        return None
    return await session.scalar(
        tenant_scoped(
            select(OnboardingPlan)
            .where(
                OnboardingPlan.employee_id == employee_id,
                OnboardingPlan.status != OnboardingPlanStatus.CANCELLED,
            )
            .order_by(
                (OnboardingPlan.status == OnboardingPlanStatus.IN_PROGRESS).desc(),
                OnboardingPlan.created_at.desc(),
            )
            .limit(1),
            OnboardingPlan,
            user,
        )
    )


async def my_onboarding(session: AsyncSession, user: User) -> dict[str, Any] | None:
    plan = await _own_plan(session, user, user.employee_id)
    if plan is None:
        return None
    return (await _plan_reads(session, user, [plan], with_tasks=True))[0]


async def _assigned_to_me(
    session: AsyncSession, user: User, assignee_id: uuid.UUID | None, *, include_done: bool
) -> list[tuple[OnboardingTask, OnboardingPlan]]:
    """Tasks on other people's live plans that are assigned to the caller."""
    if assignee_id is None:
        return []
    stmt = (
        select(OnboardingTask, OnboardingPlan)
        .join(OnboardingPlan, OnboardingPlan.id == OnboardingTask.plan_id)
        .where(
            OnboardingTask.assignee_user_id == assignee_id,
            OnboardingPlan.status == OnboardingPlanStatus.IN_PROGRESS,
        )
        .order_by(OnboardingTask.due_date, OnboardingTask.sort_order)
    )
    # Tasks on the caller's own plan are on their own onboarding page instead.
    if user.employee_id is not None:
        stmt = stmt.where(OnboardingPlan.employee_id != user.employee_id)
    if not include_done:
        stmt = stmt.where(OnboardingTask.status == OnboardingTaskStatus.PENDING)
    rows = (await session.execute(tenant_scoped(stmt, OnboardingTask, user))).all()
    return [(task, plan) for task, plan in rows]


async def my_tasks(
    session: AsyncSession, user: User, *, include_done: bool = False
) -> list[dict[str, Any]]:
    rows = await _assigned_to_me(session, user, user.id, include_done=include_done)
    if not rows:
        return []
    today = rules.india_today()
    plans = {plan.id: plan for _task, plan in rows}
    reads = await _task_reads(session, user, plans, [task for task, _plan in rows], today=today)
    employees = await _employees(session, user, {plan.employee_id for plan in plans.values()})
    out = []
    for read in reads:
        plan = plans[read["plan_id"]]
        employee = employees.get(plan.employee_id)
        read.update(
            {
                "employee_id": plan.employee_id,
                "employee_name": employee.full_name if employee else "Unknown employee",
                "employee_code": employee.employee_code if employee else "",
                "designation": employee.designation if employee else None,
                "joining_date": plan.joining_date,
            }
        )
        out.append(read)
    # Pending first, then by due date.
    out.sort(key=lambda r: (r["status"] is not OnboardingTaskStatus.PENDING, r["due_date"]))
    return out


async def my_summary(session: AsyncSession, user: User) -> dict[str, Any]:
    """The Home card: my plan's progress and the onboarding work waiting on me."""
    today = rules.india_today()
    plan = await _own_plan(session, user, user.employee_id)
    progress = None
    next_task = None
    if plan is not None:
        tasks = (await _plan_tasks(session, user, [plan.id])).get(plan.id, [])
        progress = rules.progress_of((_facts(t) for t in tasks), today=today).as_dict()
        mine = [
            t
            for t in tasks
            if t.status is OnboardingTaskStatus.PENDING
            and t.owner_type is OnboardingOwnerType.NEW_JOINER
        ]
        if mine:
            first = min(mine, key=lambda t: (t.due_date, t.sort_order))
            next_task = {
                "id": first.id,
                "title": first.title,
                "due_date": first.due_date,
                "is_overdue": first.due_date < today,
            }
    assigned = [
        task for task, _plan in await _assigned_to_me(session, user, user.id, include_done=False)
    ]
    return {
        "has_plan": plan is not None,
        "plan_id": plan.id if plan else None,
        "plan_status": plan.status if plan else None,
        "joining_date": plan.joining_date if plan else None,
        "progress": progress,
        "next_task": next_task,
        "assigned_pending": len(assigned),
        "assigned_overdue": sum(1 for t in assigned if t.due_date < today),
        "assigned_next": [
            {
                "id": t.id,
                "title": t.title,
                "due_date": t.due_date,
                "is_overdue": t.due_date < today,
            }
            for t in assigned[:3]
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Joining: offer conversion and the scheduler
# ═══════════════════════════════════════════════════════════════════════════


async def auto_start_on_conversion(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    request: Request | None = None,
) -> OnboardingPlan | None:
    """Start the tenant's checklist for a converted offer, when it has a default.

    The most specific active checklist for the joiner's department and location
    is used. A tenant with no default checklist has not asked for automatic
    onboarding, so nothing happens. A refusal (no tasks, say) never blocks the
    conversion itself: it runs in a savepoint and is dropped.
    """
    templates = (
        await session.scalars(
            tenant_scoped(
                select(OnboardingTemplate).where(OnboardingTemplate.active.is_(True)),
                OnboardingTemplate,
                user,
            )
        )
    ).all()
    if not any(t.is_default for t in templates):
        return None
    chosen = rules.pick_template(
        (
            rules.TemplateFit(
                id=t.id,
                department_id=t.department_id,
                location_id=t.location_id,
                is_default=t.is_default,
            )
            for t in templates
        ),
        department_id=employee.department_id,
        location_id=employee.location_id,
    )
    if chosen is None:
        return None
    try:
        async with session.begin_nested():
            return await start_plan(
                session,
                user,
                PlanCreate(employee_id=employee.id, template_id=chosen),
                request=request,
            )
    except HTTPException:
        return None


async def activate_due_joiners(session: AsyncSession, actor: User, *, on: date) -> dict[str, Any]:
    """Pre-joiners whose joining date has arrived become active. Audited, each one."""
    due = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.status == EmploymentStatus.PRE_JOINING,
                    Employee.date_of_joining.is_not(None),
                    Employee.date_of_joining <= on,
                    Employee.deleted_at.is_(None),
                ),
                Employee,
                actor,
            )
        )
    ).all()
    for employee in due:
        employee.status = EmploymentStatus.ACTIVE
        await audit.record(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            actor_type="system",
            action=AuditAction.UPDATE,
            entity_type="employee",
            entity_id=employee.id,
            summary=(
                f"{employee.employee_code} joined on {employee.date_of_joining}: status "
                "changed from pre-joining to active"
            ),
            changes={
                "status": {
                    "from": EmploymentStatus.PRE_JOINING.value,
                    "to": EmploymentStatus.ACTIVE.value,
                }
            },
        )
    await session.flush()
    return {"date": on.isoformat(), "activated": len(due)}


async def send_overdue_reminders(session: AsyncSession, actor: User, *, on: date) -> dict[str, Any]:
    """Tell each assignee, once, about their onboarding tasks that went overdue."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(OnboardingTask, OnboardingPlan)
                .join(OnboardingPlan, OnboardingPlan.id == OnboardingTask.plan_id)
                .where(
                    OnboardingPlan.status == OnboardingPlanStatus.IN_PROGRESS,
                    OnboardingTask.status == OnboardingTaskStatus.PENDING,
                    OnboardingTask.due_date < on,
                    OnboardingTask.overdue_notified_at.is_(None),
                )
                .order_by(OnboardingTask.due_date, OnboardingTask.sort_order),
                OnboardingTask,
                actor,
            )
        )
    ).all()
    if not rows:
        return {"date": on.isoformat(), "overdue": 0, "people_notified": 0}

    # A joiner given a login after the plan started still hears about theirs.
    joiner_logins = await _logins(
        session,
        actor,
        {
            plan.employee_id
            for task, plan in rows
            if task.assignee_user_id is None and task.owner_type is OnboardingOwnerType.NEW_JOINER
        },
    )
    employees = await _employees(session, actor, {plan.employee_id for _task, plan in rows})
    grouped: dict[uuid.UUID, list[tuple[OnboardingTask, OnboardingPlan]]] = defaultdict(list)
    for task, plan in rows:
        recipient = task.assignee_user_id
        if recipient is None and task.owner_type is OnboardingOwnerType.NEW_JOINER:
            recipient = joiner_logins.get(plan.employee_id)
        if recipient is not None:
            grouped[recipient].append((task, plan))

    now = datetime.now(UTC)
    for recipient, theirs in grouped.items():
        first_task, first_plan = theirs[0]
        employee = employees.get(first_plan.employee_id)
        await notify(
            session,
            tenant_id=actor.tenant_id,
            event_key="onboarding.task.overdue",
            recipient_user_ids=[recipient],
            payload={
                "count": len(theirs),
                "first_title": first_task.title,
                "employee_name": employee.full_name if employee else "a new joiner",
                "first_due": _day(first_task.due_date),
            },
            commit=False,
        )
        for task, _plan in theirs:
            task.overdue_notified_at = now
    await session.flush()
    return {
        "date": on.isoformat(),
        "overdue": len(rows),
        "people_notified": len(grouped),
    }
