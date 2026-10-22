"""Adding a new joiner in one go: record, checklist and login together.

1. HR adds a joiner with a checklist, a buddy and a login: the record is
   pre-joining, the plan is dated from the joining date, the login is linked,
   and each step is audited.
2. It is one transaction: a checklist that cannot start leaves no record and
   no login behind.
3. The guard rails: only an admin creates the login, a buddy needs a
   checklist, a joiner needs a joining date, and no checklist is allowed.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.models import (
    AuditLog,
    Employee,
    EmploymentStatus,
    OnboardingPlan,
    OnboardingTask,
    OnboardingTemplate,
    User,
    UserRole,
)
from app.services.onboarding import rules
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

API = "/api/v1/onboarding"


def _checklist() -> dict:
    return {
        "name": "Standard onboarding",
        "is_default": True,
        "tasks": [
            {"title": "Sign the offer letter", "owner_type": "new_joiner", "due_offset_days": -5},
            {"title": "Welcome meeting", "owner_type": "manager", "due_offset_days": 1},
            {"title": "Show them around", "owner_type": "buddy", "due_offset_days": 1},
        ],
    }


def _joiner(code: str, *, manager_id=None, joining=None) -> dict:
    joining = joining or rules.india_today() + timedelta(days=14)
    return {
        "employee_code": code,
        "first_name": "Kavya",
        "last_name": "Menon",
        "work_email": f"{code.lower()}@acme-demo.com",
        "employment_type": "full_time",
        "designation": "Software Engineer",
        "date_of_joining": joining.isoformat(),
        "manager_id": str(manager_id) if manager_id else None,
    }


async def _count(session, model, *where) -> int:
    return await session.scalar(select(func.count(model.id)).where(*where))


async def test_a_joiner_is_added_with_checklist_buddy_and_login(client, session, tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    manager = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Manager")
    buddy = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Bala Buddy")
    template = await client.post(f"{API}/templates", json=_checklist(), headers=bearer(hr))
    assert template.status_code == 201, template.text

    added = await client.post(
        f"{API}/joiners",
        json={
            "employee": _joiner("NJ001", manager_id=manager.employee_id),
            "template_id": template.json()["id"],
            "buddy_employee_id": str(buddy.employee_id),
            "create_login": True,
            "send_welcome_email": False,
        },
        headers=bearer(hr),
    )
    assert added.status_code == 201, added.text
    body = added.json()
    assert body["employee_code"] == "NJ001"
    assert body["employee_name"] == "Kavya Menon"
    # Joining in a fortnight: not counted as working here yet.
    assert body["status"] == "pre_joining"
    assert body["checklist_name"] == "Standard onboarding"
    assert body["login"]["temporary_password"]
    assert body["login"]["welcome_email"] is None

    employee = await session.get(Employee, body["employee_id"])
    assert employee.status is EmploymentStatus.PRE_JOINING
    plan = await session.get(OnboardingPlan, body["plan_id"])
    assert plan.employee_id == employee.id
    assert plan.buddy_employee_id == buddy.employee_id
    assert plan.joining_date == employee.date_of_joining
    assert await _count(session, OnboardingTask, OnboardingTask.plan_id == plan.id) == 3

    login = await session.scalar(select(User).where(User.employee_id == employee.id))
    assert login is not None
    assert login.role is UserRole.EMPLOYEE
    assert login.email == "nj001@acme-demo.com"
    assert login.must_change_password is True

    audited = set(
        (
            await session.scalars(
                select(AuditLog.entity_type).where(AuditLog.tenant_id == tenant.id)
            )
        ).all()
    )
    assert {"employee", "onboarding_plan", "user"} <= audited

    # The joiner shows up on HR's list of new joiners.
    plans = await client.get(f"{API}/plans", headers=bearer(hr))
    assert [row["employee_code"] for row in plans.json()] == ["NJ001"]


async def test_a_checklist_that_cannot_start_leaves_nothing_behind(client, session, tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    template = await client.post(f"{API}/templates", json=_checklist(), headers=bearer(hr))
    row = await session.get(OnboardingTemplate, template.json()["id"])
    row.is_default = False
    row.active = False
    await session.commit()

    refused = await client.post(
        f"{API}/joiners",
        json={
            "employee": _joiner("NJ002"),
            "template_id": str(row.id),
            "create_login": True,
            "send_welcome_email": False,
        },
        headers=bearer(hr),
    )
    assert refused.status_code == 409, refused.text
    assert "inactive" in refused.json()["detail"]
    # What the request's session dependency does on an error.
    await session.rollback()

    assert await _count(session, Employee, Employee.employee_code == "NJ002") == 0
    assert await _count(session, User, User.email == "nj002@acme-demo.com") == 0


async def test_the_guard_rails(client, session, tenant):
    sub_admin = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Sana Ops")
    employee = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Esha Staff")
    # Read before any rollback below expires the objects.
    hr_headers, staff_headers = bearer(sub_admin), bearer(employee)
    staff_employee_id = str(employee.employee_id)
    template = await client.post(f"{API}/templates", json=_checklist(), headers=hr_headers)
    template_id = template.json()["id"]

    # Logins are an admin's to give.
    refused = await client.post(
        f"{API}/joiners",
        json={"employee": _joiner("NJ003"), "template_id": template_id, "create_login": True},
        headers=hr_headers,
    )
    assert refused.status_code == 403, refused.text
    await session.rollback()

    # Without one, HR adds the joiner.
    added = await client.post(
        f"{API}/joiners",
        json={"employee": _joiner("NJ003"), "template_id": template_id},
        headers=hr_headers,
    )
    assert added.status_code == 201, added.text
    assert added.json()["login"] is None

    # A buddy is named on a checklist.
    no_checklist_buddy = await client.post(
        f"{API}/joiners",
        json={"employee": _joiner("NJ004"), "buddy_employee_id": staff_employee_id},
        headers=hr_headers,
    )
    assert no_checklist_buddy.status_code == 422

    # A joiner joins on a date.
    undated = _joiner("NJ005")
    undated["date_of_joining"] = None
    no_date = await client.post(
        f"{API}/joiners", json={"employee": undated}, headers=hr_headers
    )
    assert no_date.status_code == 422
    await session.rollback()

    # No checklist is allowed: someone joining today is simply active.
    plain = await client.post(
        f"{API}/joiners",
        json={"employee": _joiner("NJ006", joining=rules.india_today())},
        headers=hr_headers,
    )
    assert plain.status_code == 201, plain.text
    assert plain.json()["plan_id"] is None
    assert plain.json()["status"] == "active"

    # Not for employees.
    forbidden = await client.post(
        f"{API}/joiners", json={"employee": _joiner("NJ007")}, headers=staff_headers
    )
    assert forbidden.status_code == 403
