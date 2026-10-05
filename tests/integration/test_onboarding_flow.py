"""Onboarding end to end: checklist, plan, the people on it, and joining day.

Three flows, each on a freshly built schema:

1. A checklist becomes a plan with the right people and dates; the joiner, IT,
   the manager and HR each do their part through the API; the plan completes.
2. Joining day: a pre-joiner is activated by the scheduler job, audited, and a
   converted hire starts on the tenant's default checklist.
3. The guard rails: one live plan per person, no running your own onboarding,
   reassign / skip / cancel, overdue reminders sent once, tenant isolation.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models import (
    AuditLog,
    Channel,
    Employee,
    EmployeeDocument,
    EmploymentStatus,
    Notification,
    OnboardingPlan,
    OnboardingPlanStatus,
    OnboardingTask,
    OnboardingTemplate,
    User,
    UserRole,
)
from app.services.onboarding import rules
from app.services.onboarding import service as onboarding
from app.services.scheduler.jobs import system_actor
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

API = "/api/v1/onboarding"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path / "store"))


async def _employee(session, user: User) -> Employee:
    return await session.get(Employee, user.employee_id)


async def _events(session, user: User, key: str) -> int:
    """In-app notifications only: one per event, whatever else was sent."""
    return await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.recipient_user_id == user.id,
            Notification.event_key == key,
            Notification.channel == Channel.IN_APP,
        )
    )


def _checklist(it_user: User) -> dict:
    return {
        "name": "Engineering onboarding",
        "is_default": True,
        "tasks": [
            {
                "title": "Upload your PAN card",
                "owner_type": "new_joiner",
                "due_offset_days": -7,
                "task_type": "document_upload",
                "document_type": "id_proof",
            },
            {
                "title": "Acknowledge the code of conduct",
                "owner_type": "new_joiner",
                "due_offset_days": 3,
                "task_type": "acknowledge_policy",
            },
            {
                "title": "Provision laptop and email",
                "owner_type": "it",
                "owner_user_id": str(it_user.id),
                "due_offset_days": -2,
            },
            {
                "title": "Welcome meeting",
                "owner_type": "manager",
                "due_offset_days": 1,
                "task_type": "meeting",
            },
            {
                "title": "Buddy introduction",
                "owner_type": "buddy",
                "due_offset_days": 1,
                "is_mandatory": False,
            },
            {"title": "Issue ID card", "owner_type": "hr", "due_offset_days": 0},
        ],
    }


async def test_a_checklist_becomes_a_plan_that_the_people_on_it_complete(client, session, tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    manager = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Manager")
    buddy = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Bala Buddy")
    it_user = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ishan It")
    joiner = await make_person(
        session,
        tenant,
        role=UserRole.EMPLOYEE,
        name="Neha Joiner",
        manager=await _employee(session, manager),
    )
    joining = rules.india_today() + timedelta(days=10)
    joiner_emp = await _employee(session, joiner)
    joiner_emp.status = EmploymentStatus.PRE_JOINING
    joiner_emp.date_of_joining = joining
    await session.commit()

    # ── HR builds a checklist and starts the plan ───────────────────────────
    created = await client.post(f"{API}/templates", json=_checklist(it_user), headers=bearer(hr))
    assert created.status_code == 201, created.text
    template = created.json()
    assert [t["sort_order"] for t in template["tasks"]] == list(range(6))
    assert template["is_default"] is True

    started = await client.post(
        f"{API}/plans",
        json={
            "employee_id": str(joiner.employee_id),
            "template_id": template["id"],
            "buddy_employee_id": str(buddy.employee_id),
        },
        headers=bearer(hr),
    )
    assert started.status_code == 201, started.text
    plan = started.json()
    assert plan["joining_date"] == joining.isoformat()
    assert plan["employee_status"] == "pre_joining"
    tasks = {t["title"]: t for t in plan["tasks"]}

    # Dates from the joining date; people from around the joiner.
    expected = {
        "Upload your PAN card": (joiner, -7),
        "Acknowledge the code of conduct": (joiner, 3),
        "Provision laptop and email": (it_user, -2),
        "Welcome meeting": (manager, 1),
        "Buddy introduction": (buddy, 1),
        "Issue ID card": (hr, 0),
    }
    for title, (person, offset) in expected.items():
        assert tasks[title]["assignee_user_id"] == str(person.id), title
        assert tasks[title]["due_date"] == (joining + timedelta(days=offset)).isoformat()
    assert tasks["Upload your PAN card"]["phase"] == "pre_joining"
    assert tasks["Welcome meeting"]["phase"] == "week_1"

    # Everybody was told, except the HR user who started it.
    assert await _events(session, joiner, "onboarding.plan.started") >= 1
    for person in (it_user, manager, buddy):
        assert await _events(session, person, "onboarding.task.assigned") >= 1
    assert await _events(session, hr, "onboarding.task.assigned") == 0

    # ── The pre-joiner signs in and sees their plan ─────────────────────────
    me = (await client.get(f"{API}/me", headers=bearer(joiner))).json()["plan"]
    assert me["id"] == plan["id"]
    mine = {t["title"]: t for t in me["tasks"]}
    assert mine["Upload your PAN card"]["can_complete"] is True
    assert mine["Provision laptop and email"]["can_complete"] is False

    # Somebody else's work is not the joiner's to tick off.
    refused = await client.post(
        f"{API}/tasks/{tasks['Provision laptop and email']['id']}/complete",
        json={},
        headers=bearer(joiner),
    )
    assert refused.status_code == 403
    # A document task is completed by uploading the document.
    no_file = await client.post(
        f"{API}/tasks/{tasks['Upload your PAN card']['id']}/complete",
        json={},
        headers=bearer(joiner),
    )
    assert no_file.status_code == 422
    # HR cannot acknowledge a policy on the joiner's behalf.
    forged = await client.post(
        f"{API}/tasks/{tasks['Acknowledge the code of conduct']['id']}/complete",
        json={},
        headers=bearer(hr),
    )
    assert forged.status_code == 403

    uploaded = await client.post(
        f"{API}/tasks/{tasks['Upload your PAN card']['id']}/document",
        files={"file": ("pan.png", PNG, "image/png")},
        headers=bearer(joiner),
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["status"] == "done"
    document = await session.get(EmployeeDocument, uploaded.json()["document_id"])
    assert document is not None
    assert document.employee_id == joiner.employee_id
    assert document.category == "id_proof"
    assert document.is_employee_visible is True
    # A self-upload joins the vault's verification queue.
    assert document.verification_status == "pending"

    ack = await client.post(
        f"{API}/tasks/{tasks['Acknowledge the code of conduct']['id']}/complete",
        json={"note": "Read and understood."},
        headers=bearer(joiner),
    )
    assert ack.status_code == 200, ack.text

    # ── Assignees work from "my tasks" ──────────────────────────────────────
    it_tasks = (await client.get(f"{API}/my-tasks", headers=bearer(it_user))).json()
    assert [t["title"] for t in it_tasks] == ["Provision laptop and email"]
    assert it_tasks[0]["employee_name"] == "Neha Joiner"
    done = await client.post(
        f"{API}/tasks/{it_tasks[0]['id']}/complete",
        json={"note": "Laptop LT-0142 imaged"},
        headers=bearer(it_user),
    )
    assert done.status_code == 200
    # The joiner's own tasks are on their onboarding page, not in "my tasks".
    assert (await client.get(f"{API}/my-tasks", headers=bearer(joiner))).json() == []

    for person, title in ((manager, "Welcome meeting"), (hr, "Issue ID card")):
        response = await client.post(
            f"{API}/tasks/{tasks[title]['id']}/complete", json={}, headers=bearer(person)
        )
        assert response.status_code == 200, (title, response.text)

    # ── Every mandatory task done: the plan completes itself ────────────────
    detail = (await client.get(f"{API}/plans/{plan['id']}", headers=bearer(hr))).json()
    assert detail["status"] == "completed"
    assert detail["completed_at"] is not None
    # The optional buddy introduction is still open: 5 of 6 resolved.
    assert detail["progress"]["percent"] == 83
    assert detail["progress"]["pending"] == 1

    summary = (await client.get(f"{API}/me/summary", headers=bearer(joiner))).json()
    assert summary["has_plan"] is True
    assert summary["plan_status"] == "completed"

    listed = (await client.get(f"{API}/plans?status=completed", headers=bearer(hr))).json()
    assert [row["id"] for row in listed] == [plan["id"]]

    audited = await session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.entity_type.in_(("onboarding_plan", "onboarding_task"))
        )
    )
    # Started, five tasks done, completed.
    assert audited >= 7

    # Employees reach nothing HR-only.
    assert (await client.get(f"{API}/plans", headers=bearer(joiner))).status_code == 403
    assert (await client.get(f"{API}/templates", headers=bearer(manager))).status_code == 403


async def test_joining_day_activates_pre_joiners_and_a_hire_starts_on_the_default(session, tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    today = rules.india_today()
    arrived = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Arun Arrived")
    waiting = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Wasim Waiting")
    for person, joining in ((arrived, today), (waiting, today + timedelta(days=5))):
        employee = await _employee(session, person)
        employee.status = EmploymentStatus.PRE_JOINING
        employee.date_of_joining = joining
    await session.commit()

    actor = system_actor(tenant.id)
    result = await onboarding.activate_due_joiners(session, actor, on=today)
    await session.commit()
    assert result["activated"] == 1
    assert (await _employee(session, arrived)).status is EmploymentStatus.ACTIVE
    assert (await _employee(session, waiting)).status is EmploymentStatus.PRE_JOINING
    audit_row = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "employee", AuditLog.entity_id == arrived.employee_id
        )
    )
    assert audit_row is not None and audit_row.actor_type == "system"
    # Running it again changes nothing.
    assert (await onboarding.activate_due_joiners(session, actor, on=today))["activated"] == 0

    # A converted hire: nothing starts without a default checklist…
    hire = await _employee(session, waiting)
    assert await onboarding.auto_start_on_conversion(session, hr, hire) is None

    # …and the default one starts when the tenant keeps one.
    await onboarding.create_standard_template(session, hr)
    await session.commit()
    plan = await onboarding.auto_start_on_conversion(session, hr, hire)
    await session.commit()
    assert plan is not None
    assert plan.template_name == rules.STANDARD_TEMPLATE_NAME
    assert plan.joining_date == hire.date_of_joining
    count = await session.scalar(
        select(func.count(OnboardingTask.id)).where(OnboardingTask.plan_id == plan.id)
    )
    assert count == len(rules.STANDARD_TASKS)
    # The standard checklist became the default because there was none.
    assert (await session.scalar(select(OnboardingTemplate.is_default))) is True


async def test_guard_rails_reassignment_skips_overdue_and_isolation(
    client, session, tenant, other_tenant
):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    ops = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Omar Ops")
    manager = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Manager")
    joiner = await make_person(
        session,
        tenant,
        role=UserRole.EMPLOYEE,
        name="Neha Joiner",
        manager=await _employee(session, manager),
    )
    outsider = await make_person(session, other_tenant, role=UserRole.ADMIN, name="Sam Other")
    # Joined five days ago, so pre-boarding tasks are already overdue.
    joiner_emp = await _employee(session, joiner)
    joiner_emp.date_of_joining = rules.india_today() - timedelta(days=5)
    await session.commit()

    template = (await client.post(f"{API}/templates/standard", headers=bearer(hr))).json()
    assert template["task_count"] == len(rules.STANDARD_TASKS)
    again = await client.post(f"{API}/templates/standard", headers=bearer(hr))
    assert again.status_code == 409

    # Nobody starts their own onboarding, HR included.
    own = await client.post(
        f"{API}/plans",
        json={"employee_id": str(hr.employee_id), "template_id": template["id"]},
        headers=bearer(hr),
    )
    assert own.status_code == 403

    body = {"employee_id": str(joiner.employee_id), "template_id": template["id"]}
    first = await client.post(f"{API}/plans", json=body, headers=bearer(hr))
    assert first.status_code == 201, first.text
    plan = first.json()
    duplicate = await client.post(f"{API}/plans", json=body, headers=bearer(hr))
    assert duplicate.status_code == 409

    # Another company's HR is told it does not exist.
    assert (
        await client.get(f"{API}/plans/{plan['id']}", headers=bearer(outsider))
    ).status_code == 404
    assert (await client.get(f"{API}/plans", headers=bearer(outsider))).json() == []
    # A colleague with no part in it is told the same.
    stranger = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Sita Stranger")
    assert (
        await client.get(f"{API}/plans/{plan['id']}", headers=bearer(stranger))
    ).status_code == 404
    # The joiner's manager may look.
    assert (
        await client.get(f"{API}/plans/{plan['id']}", headers=bearer(manager))
    ).status_code == 200

    tasks = {t["title"]: t for t in plan["tasks"]}
    assert plan["progress"]["overdue"] > 0
    # No buddy chosen, so the buddy task waits for HR to hand it to somebody.
    buddy_task = tasks["Buddy introduction"]
    assert buddy_task["assignee_user_id"] is None
    assert plan["progress"]["unassigned"] == 1

    moved = await client.patch(
        f"{API}/tasks/{buddy_task['id']}",
        json={"assignee_user_id": str(ops.id)},
        headers=bearer(hr),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["assignee_name"] == "Omar Ops"
    assert await _events(session, ops, "onboarding.task.assigned") == 1

    # A skip needs a reason, and only HR may skip.
    previous = tasks["Upload previous employment documents"]
    assert (
        await client.post(f"{API}/tasks/{previous['id']}/skip", json={}, headers=bearer(hr))
    ).status_code == 422
    assert (
        await client.post(
            f"{API}/tasks/{previous['id']}/skip",
            json={"reason": "First job"},
            headers=bearer(joiner),
        )
    ).status_code == 403
    skipped = await client.post(
        f"{API}/tasks/{previous['id']}/skip",
        json={"reason": "First job after graduation"},
        headers=bearer(hr),
    )
    assert skipped.json()["status"] == "skipped"

    # Overdue reminders: once per assignee, not every morning.
    actor = system_actor(tenant.id)
    first_run = await onboarding.send_overdue_reminders(session, actor, on=rules.india_today())
    await session.commit()
    assert first_run["overdue"] > 0
    assert await _events(session, joiner, "onboarding.task.overdue") == 1
    second_run = await onboarding.send_overdue_reminders(session, actor, on=rules.india_today())
    await session.commit()
    assert second_run["overdue"] == 0
    assert await _events(session, joiner, "onboarding.task.overdue") == 1

    # Cancel with a reason; nothing is deleted, and a fresh start is allowed.
    cancelled = await client.post(
        f"{API}/plans/{plan['id']}/cancel",
        json={"reason": "Joining postponed; restarting with the new date"},
        headers=bearer(hr),
    )
    assert cancelled.json()["status"] == "cancelled"
    assert (await session.get(OnboardingPlan, plan["id"])).status is OnboardingPlanStatus.CANCELLED
    assert (await client.get(f"{API}/me", headers=bearer(joiner))).json()["plan"] is None
    restarted = await client.post(f"{API}/plans", json=body, headers=bearer(hr))
    assert restarted.status_code == 201

    # A checklist that has been used is kept; deleting it is refused.
    assert (
        await client.delete(f"{API}/templates/{template['id']}", headers=bearer(hr))
    ).status_code == 409


async def test_someone_with_no_plan_gets_an_honest_empty_answer(client, session, tenant):
    person = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ravi Regular")
    assert (await client.get(f"{API}/me", headers=bearer(person))).json() == {"plan": None}
    summary = (await client.get(f"{API}/me/summary", headers=bearer(person))).json()
    assert summary["has_plan"] is False
    assert summary["progress"] is None
    assert summary["assigned_pending"] == 0
    assert (await client.get(f"{API}/my-tasks", headers=bearer(person))).json() == []
