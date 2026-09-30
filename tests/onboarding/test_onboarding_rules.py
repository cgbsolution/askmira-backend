"""Onboarding's pure rules: due dates, owners, phases, progress, completion."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.models.employee import EmploymentStatus
from app.models.onboarding import OnboardingOwnerType, OnboardingTaskStatus
from app.schemas.onboarding import TemplateTaskIn
from app.services.onboarding import rules

JOINING = date(2026, 10, 12)

JOINER, MANAGER, BUDDY, STARTER, NAMED = (uuid.uuid4() for _ in range(5))
CTX = rules.AssigneeContext(
    joiner_user_id=JOINER, manager_user_id=MANAGER, buddy_user_id=BUDDY, starter_user_id=STARTER
)


# ── Due dates and phases ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(-14, date(2026, 9, 28)), (0, JOINING), (1, date(2026, 10, 13)), (30, date(2026, 11, 11))],
)
def test_due_date_is_the_joining_date_moved_by_the_offset(offset, expected) -> None:
    assert rules.due_date_for(JOINING, offset) == expected


def test_a_due_date_crosses_month_and_year_boundaries() -> None:
    assert rules.due_date_for(date(2026, 12, 28), 7) == date(2027, 1, 4)
    assert rules.due_date_for(date(2027, 1, 3), -7) == date(2026, 12, 27)


@pytest.mark.parametrize(
    ("due", "phase"),
    [
        (date(2026, 10, 11), "pre_joining"),
        (JOINING, "week_1"),
        (date(2026, 10, 18), "week_1"),
        (date(2026, 10, 19), "later"),
    ],
)
def test_phase_is_relative_to_the_joining_date(due, phase) -> None:
    assert rules.phase_of(due, JOINING) == phase


def test_today_is_india_today_not_the_servers() -> None:
    # 20:00 UTC on 30 September is 01:30 on 1 October in India.
    assert rules.india_today(datetime(2026, 9, 30, 20, 0, tzinfo=UTC)) == date(2026, 10, 1)
    assert rules.india_today(datetime(2026, 9, 30, 18, 0, tzinfo=UTC)) == date(2026, 9, 30)


# ── Assignees ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        (OnboardingOwnerType.NEW_JOINER, JOINER),
        (OnboardingOwnerType.MANAGER, MANAGER),
        (OnboardingOwnerType.BUDDY, BUDDY),
        (OnboardingOwnerType.HR, STARTER),
        (OnboardingOwnerType.IT, STARTER),
        (OnboardingOwnerType.FINANCE, STARTER),
    ],
)
def test_each_owner_type_resolves_to_its_person(owner, expected) -> None:
    assert rules.resolve_assignee(owner, None, CTX) == expected


def test_a_named_owner_wins_for_desk_work() -> None:
    assert rules.resolve_assignee(OnboardingOwnerType.IT, NAMED, CTX) == NAMED


def test_a_named_owner_does_not_take_the_joiners_or_managers_task() -> None:
    assert rules.resolve_assignee(OnboardingOwnerType.NEW_JOINER, NAMED, CTX) == JOINER
    assert rules.resolve_assignee(OnboardingOwnerType.MANAGER, NAMED, CTX) == MANAGER


def test_somebody_with_no_login_leaves_the_task_unassigned() -> None:
    ctx = rules.AssigneeContext(
        joiner_user_id=None, manager_user_id=None, buddy_user_id=None, starter_user_id=None
    )
    for owner in OnboardingOwnerType:
        assert rules.resolve_assignee(owner, None, ctx) is None


# ── Progress and completion ─────────────────────────────────────────────────


def _task(
    status: OnboardingTaskStatus,
    *,
    due: date = JOINING,
    mandatory: bool = True,
    owner: OnboardingOwnerType = OnboardingOwnerType.NEW_JOINER,
    assignee: uuid.UUID | None = JOINER,
) -> rules.TaskFacts:
    return rules.TaskFacts(
        status=status,
        due_date=due,
        is_mandatory=mandatory,
        owner_type=owner,
        assignee_user_id=assignee,
    )


def test_progress_counts_and_rounds_down() -> None:
    tasks = [
        _task(OnboardingTaskStatus.DONE),
        _task(OnboardingTaskStatus.SKIPPED),
        _task(OnboardingTaskStatus.PENDING, due=date(2026, 10, 1)),
        _task(OnboardingTaskStatus.PENDING, due=date(2026, 10, 30), mandatory=False),
        _task(
            OnboardingTaskStatus.PENDING,
            owner=OnboardingOwnerType.IT,
            assignee=None,
        ),
        _task(OnboardingTaskStatus.PENDING, assignee=None),  # the joiner's own: not "unassigned"
    ]
    progress = rules.progress_of(tasks, today=date(2026, 10, 5))
    assert progress.total == 6
    assert progress.done == 1
    assert progress.skipped == 1
    assert progress.pending == 4
    assert progress.overdue == 1
    assert progress.mandatory_pending == 3
    assert progress.unassigned == 1
    # 2 of 6 is 33.3%, shown as 33 — never rounded up past what is true.
    assert progress.percent == 33


def test_progress_never_shows_100_with_a_task_open() -> None:
    tasks = [_task(OnboardingTaskStatus.DONE)] * 199 + [_task(OnboardingTaskStatus.PENDING)]
    assert rules.progress_of(tasks, today=JOINING).percent == 99


def test_an_empty_plan_is_zero_percent_and_never_complete() -> None:
    assert rules.progress_of([], today=JOINING).percent == 0
    assert rules.is_complete([]) is False


def test_complete_when_every_mandatory_task_is_done_or_skipped() -> None:
    tasks = [
        _task(OnboardingTaskStatus.DONE),
        _task(OnboardingTaskStatus.SKIPPED),
        _task(OnboardingTaskStatus.PENDING, mandatory=False),
    ]
    assert rules.is_complete(tasks) is True
    assert rules.is_complete([*tasks, _task(OnboardingTaskStatus.PENDING)]) is False


def test_a_checklist_of_optional_tasks_completes_only_when_all_are_resolved() -> None:
    optional = [
        _task(OnboardingTaskStatus.DONE, mandatory=False),
        _task(OnboardingTaskStatus.PENDING, mandatory=False),
    ]
    assert rules.is_complete(optional) is False
    optional[1] = _task(OnboardingTaskStatus.DONE, mandatory=False)
    assert rules.is_complete(optional) is True


# ── Joining status and choosing a checklist ─────────────────────────────────


def test_a_future_joining_date_is_pre_joining() -> None:
    today = date(2026, 9, 30)
    assert rules.joining_status(date(2026, 10, 1), today) is EmploymentStatus.PRE_JOINING
    assert rules.joining_status(today, today) is EmploymentStatus.ACTIVE
    assert rules.joining_status(date(2026, 9, 1), today) is EmploymentStatus.ACTIVE
    assert rules.joining_status(None, today) is EmploymentStatus.ACTIVE


def test_the_most_specific_applicable_checklist_is_picked() -> None:
    eng, sales, blr = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    company = rules.TemplateFit(uuid.uuid4(), None, None, is_default=True)
    engineering = rules.TemplateFit(uuid.uuid4(), eng, None, is_default=False)
    eng_blr = rules.TemplateFit(uuid.uuid4(), eng, blr, is_default=False)
    sales_only = rules.TemplateFit(uuid.uuid4(), sales, None, is_default=False)
    all_four = [company, engineering, eng_blr, sales_only]

    assert rules.pick_template(all_four, department_id=eng, location_id=blr) == eng_blr.id
    assert rules.pick_template(all_four, department_id=eng, location_id=None) == engineering.id
    assert rules.pick_template(all_four, department_id=None, location_id=blr) == company.id
    assert rules.pick_template([sales_only], department_id=eng, location_id=None) is None


# ── The standard checklist ──────────────────────────────────────────────────


def test_the_standard_checklist_validates_and_covers_the_indian_basics() -> None:
    tasks = [
        TemplateTaskIn(
            title=t.title,
            description=t.description,
            owner_type=t.owner_type,
            due_offset_days=t.due_offset_days,
            task_type=t.task_type,
            document_type=t.document_type,
            is_mandatory=t.is_mandatory,
        )
        for t in rules.STANDARD_TASKS
    ]
    titles = " ".join(t.title for t in tasks).lower()
    for needle in ("pan", "aadhaar", "bank", "form 2", "form 11", "laptop", "id card", "day-30"):
        assert needle in titles, needle
    owners = {t.owner_type for t in tasks}
    assert {
        OnboardingOwnerType.NEW_JOINER,
        OnboardingOwnerType.MANAGER,
        OnboardingOwnerType.IT,
        OnboardingOwnerType.HR,
        OnboardingOwnerType.BUDDY,
    } <= owners
    assert any(t.due_offset_days < 0 for t in tasks), "no pre-boarding at all"


def test_an_upload_task_must_say_what_it_collects() -> None:
    with pytest.raises(ValueError, match="document type"):
        TemplateTaskIn(
            title="Upload PAN",
            owner_type=OnboardingOwnerType.NEW_JOINER,
            task_type="document_upload",
        )
