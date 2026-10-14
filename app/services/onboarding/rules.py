"""The pure parts of onboarding: dates, owners, phases, progress, and the
standard checklist. No session, no clock of their own — every "today" is passed
in, so a test can say which day it is.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.employee import EmploymentStatus
from app.models.onboarding import (
    OnboardingOwnerType,
    OnboardingTaskStatus,
    OnboardingTaskType,
)

__all__ = [
    "INDIA",
    "STANDARD_TASKS",
    "STANDARD_TEMPLATE_NAME",
    "AssigneeContext",
    "Progress",
    "StandardTask",
    "TaskFacts",
    "TemplateFit",
    "due_date_for",
    "india_today",
    "is_complete",
    "joining_status",
    "phase_of",
    "pick_template",
    "progress_of",
    "resolve_assignee",
]

INDIA = ZoneInfo("Asia/Kolkata")

#: Owner types whose task goes to a named person or the HR user who started it.
DESK_OWNERS = frozenset(
    {OnboardingOwnerType.HR, OnboardingOwnerType.IT, OnboardingOwnerType.FINANCE}
)


def india_today(now: datetime | None = None) -> date:
    """Today in India. A UTC server's `date.today()` is yesterday until 05:30 IST."""
    moment = now or datetime.now(INDIA)
    return moment.astimezone(INDIA).date()


def due_date_for(joining_date: date, offset_days: int) -> date:
    """The calendar date a task is due: the joining date moved by its offset."""
    return joining_date + timedelta(days=offset_days)


def phase_of(due_date: date, joining_date: date) -> str:
    """Pre-joining (before day one), week 1 (the first seven days), or later."""
    if due_date < joining_date:
        return "pre_joining"
    if due_date < joining_date + timedelta(days=7):
        return "week_1"
    return "later"


def joining_status(joining_date: date | None, today: date) -> EmploymentStatus:
    """Pre-joining until the joining date arrives, active from it.

    An offer converted in September for an October start is somebody who has
    not joined yet: payroll, attendance and headcount must not count them.
    """
    if joining_date is not None and joining_date > today:
        return EmploymentStatus.PRE_JOINING
    return EmploymentStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class TemplateFit:
    """What choosing a checklist for a person needs to know about one."""

    id: uuid.UUID
    department_id: uuid.UUID | None
    location_id: uuid.UUID | None
    is_default: bool


def pick_template(
    templates: Iterable[TemplateFit],
    *,
    department_id: uuid.UUID | None,
    location_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """The most specific active checklist that applies to this person.

    A checklist scoped to another department or location does not apply. Among
    those that do, one matching both department and location beats one
    matching either, which beats a company-wide one; the default breaks ties.
    """
    best: tuple[int, int] | None = None
    chosen: uuid.UUID | None = None
    for fit in templates:
        if fit.department_id is not None and fit.department_id != department_id:
            continue
        if fit.location_id is not None and fit.location_id != location_id:
            continue
        score = (
            (fit.department_id is not None) + (fit.location_id is not None),
            1 if fit.is_default else 0,
        )
        if best is None or score > best:
            best, chosen = score, fit.id
    return chosen


@dataclass(frozen=True, slots=True)
class AssigneeContext:
    """The logins a plan's tasks can be resolved to."""

    joiner_user_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    buddy_user_id: uuid.UUID | None
    #: The HR user who started the plan. None when the scheduler started it.
    starter_user_id: uuid.UUID | None


def resolve_assignee(
    owner_type: OnboardingOwnerType,
    owner_user_id: uuid.UUID | None,
    ctx: AssigneeContext,
) -> uuid.UUID | None:
    """Who a task goes to, or None when that person has no login.

    HR, IT and finance work goes to the person the checklist names, and
    otherwise to whoever started the plan — somebody in HR, who can pass it on.
    """
    if owner_type is OnboardingOwnerType.NEW_JOINER:
        return ctx.joiner_user_id
    if owner_type is OnboardingOwnerType.MANAGER:
        return ctx.manager_user_id
    if owner_type is OnboardingOwnerType.BUDDY:
        return ctx.buddy_user_id
    return owner_user_id or ctx.starter_user_id


@dataclass(frozen=True, slots=True)
class Progress:
    total: int
    done: int
    skipped: int
    pending: int
    overdue: int
    mandatory_pending: int
    unassigned: int
    percent: int

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "done": self.done,
            "skipped": self.skipped,
            "pending": self.pending,
            "overdue": self.overdue,
            "mandatory_pending": self.mandatory_pending,
            "unassigned": self.unassigned,
            "percent": self.percent,
        }


@dataclass(frozen=True, slots=True)
class TaskFacts:
    """What progress needs to know about one task."""

    status: OnboardingTaskStatus
    due_date: date
    is_mandatory: bool
    owner_type: OnboardingOwnerType
    assignee_user_id: uuid.UUID | None


def progress_of(tasks: Iterable[TaskFacts], *, today: date) -> Progress:
    """Counts and a whole-number percentage, rounded down.

    Rounded down so a plan never shows 100% while a task is still open. A
    skipped task counts as resolved: HR decided it does not apply. The joiner's
    own tasks are never "unassigned" — they belong to whoever the plan is for.
    """
    rows = list(tasks)
    done = sum(1 for t in rows if t.status is OnboardingTaskStatus.DONE)
    skipped = sum(1 for t in rows if t.status is OnboardingTaskStatus.SKIPPED)
    pending_rows = [t for t in rows if t.status is OnboardingTaskStatus.PENDING]
    total = len(rows)
    return Progress(
        total=total,
        done=done,
        skipped=skipped,
        pending=len(pending_rows),
        overdue=sum(1 for t in pending_rows if t.due_date < today),
        mandatory_pending=sum(1 for t in pending_rows if t.is_mandatory),
        unassigned=sum(
            1
            for t in pending_rows
            if t.assignee_user_id is None and t.owner_type is not OnboardingOwnerType.NEW_JOINER
        ),
        percent=((done + skipped) * 100 // total) if total else 0,
    )


def is_complete(tasks: Iterable[TaskFacts]) -> bool:
    """Every mandatory task is done or skipped.

    A checklist with no mandatory task at all completes when every task is
    resolved, rather than the moment the first one is. An empty plan never
    completes on its own.
    """
    rows = list(tasks)
    if not rows:
        return False
    scope = [t for t in rows if t.is_mandatory] or rows
    return all(t.status is not OnboardingTaskStatus.PENDING for t in scope)


# ══════════════════════════════════════════════════════════════════════════════
# The standard checklist
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class StandardTask:
    title: str
    description: str
    owner_type: OnboardingOwnerType
    due_offset_days: int
    task_type: OnboardingTaskType = OnboardingTaskType.GENERAL
    document_type: str | None = None
    is_mandatory: bool = True
    form_code: str | None = None


STANDARD_TEMPLATE_NAME = "Standard onboarding (India)"

#: Built only when HR asks for it. A sensible Indian mid-market checklist, meant
#: to be edited, not a claim about any one company's process.
STANDARD_TASKS: tuple[StandardTask, ...] = (
    StandardTask(
        "Accept your offer and confirm your joining date",
        "Sign and return the offer letter, and confirm the date you will join.",
        OnboardingOwnerType.NEW_JOINER,
        -14,
        OnboardingTaskType.ACKNOWLEDGE_POLICY,
    ),
    StandardTask(
        "Upload your PAN card",
        "Needed for TDS on salary. A clear scan or photo of the card.",
        OnboardingOwnerType.NEW_JOINER,
        -7,
        OnboardingTaskType.DOCUMENT_UPLOAD,
        "id_proof",
    ),
    StandardTask(
        "Upload your Aadhaar card",
        "Needed for PF (UAN) linking. You may mask the first eight digits.",
        OnboardingOwnerType.NEW_JOINER,
        -7,
        OnboardingTaskType.DOCUMENT_UPLOAD,
        "id_proof",
    ),
    StandardTask(
        "Upload bank account proof",
        "A cancelled cheque or the first page of your passbook, for salary credit.",
        OnboardingOwnerType.NEW_JOINER,
        -7,
        OnboardingTaskType.DOCUMENT_UPLOAD,
        "other",
    ),
    StandardTask(
        "Upload education certificates",
        "Your highest qualification's degree certificate and final marksheet.",
        OnboardingOwnerType.NEW_JOINER,
        -5,
        OnboardingTaskType.DOCUMENT_UPLOAD,
        "education",
    ),
    StandardTask(
        "Upload previous employment documents",
        "Relieving letter and last three payslips from your previous employer. "
        "Skip this if this is your first job.",
        OnboardingOwnerType.NEW_JOINER,
        -5,
        OnboardingTaskType.DOCUMENT_UPLOAD,
        "experience",
        False,
    ),
    StandardTask(
        "Sign your PF and EPS nomination (Form 2)",
        "Add your Provident Fund nominees and family under Profile, then generate and sign "
        "the form here.",
        OnboardingOwnerType.NEW_JOINER,
        0,
        OnboardingTaskType.FORM,
        form_code="FORM2_STD",
    ),
    StandardTask(
        "Sign your EPF declaration (Form 11)",
        "Declares your previous PF membership and UAN, if you have one. Check your profile, "
        "then generate and sign the form here.",
        OnboardingOwnerType.NEW_JOINER,
        0,
        OnboardingTaskType.FORM,
        form_code="FORM11_STD",
    ),
    StandardTask(
        "Sign your gratuity nomination (Form F)",
        "Add your gratuity nominees under Profile, then generate and sign the form here.",
        OnboardingOwnerType.NEW_JOINER,
        7,
        OnboardingTaskType.FORM,
        form_code="FORMF_STD",
    ),
    StandardTask(
        "Sign your ESI declaration (Form 1)",
        "Only if your wages are within the ESI ceiling. Check your family details, then "
        "generate and sign the form here.",
        OnboardingOwnerType.NEW_JOINER,
        7,
        OnboardingTaskType.FORM,
        is_mandatory=False,
        form_code="ESI_DECL_STD",
    ),
    StandardTask(
        "Provision laptop and work email",
        "Laptop imaged and ready, email and core tool accounts created before day one.",
        OnboardingOwnerType.IT,
        -2,
    ),
    StandardTask(
        "Issue ID card and access card",
        "Photo ID and building access ready on the joining day.",
        OnboardingOwnerType.HR,
        0,
    ),
    StandardTask(
        "Welcome meeting with your manager",
        "Role, first-month goals and how the team works.",
        OnboardingOwnerType.MANAGER,
        1,
        OnboardingTaskType.MEETING,
    ),
    StandardTask(
        "Buddy introduction",
        "Meet your onboarding buddy: lunch, the office, and who to ask for what.",
        OnboardingOwnerType.BUDDY,
        1,
        OnboardingTaskType.MEETING,
        None,
        False,
    ),
    StandardTask(
        "Read and acknowledge company policies",
        "Code of conduct, POSH policy, IT acceptable use and leave policy.",
        OnboardingOwnerType.NEW_JOINER,
        3,
        OnboardingTaskType.ACKNOWLEDGE_POLICY,
    ),
    StandardTask(
        "Day-30 check-in",
        "A conversation with your manager on how the first month has gone.",
        OnboardingOwnerType.MANAGER,
        30,
        OnboardingTaskType.MEETING,
    ),
)
