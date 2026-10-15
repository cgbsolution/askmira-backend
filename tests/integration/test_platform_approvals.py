"""The approvals inbox shows each person exactly what they may decide."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AttendanceRegularisation,
    AttendanceStatus,
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    Tenant,
    UserRole,
)
from app.services.approvals import service as approvals
from tests.integration import platform_support
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client


@pytest.fixture
async def org(session: AsyncSession, tenant: Tenant) -> dict:
    boss = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Kavya Boss")
    boss_emp_id = boss.employee_id
    from app.models import Employee

    boss_emp = await session.get(Employee, boss_emp_id)
    report = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Ravi Report", manager=boss_emp
    )
    stranger = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Sita Other")
    other_manager = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Omar Mgr")
    hr_ops = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Hari Ops")
    hr_admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")

    for person in (report, stranger, hr_admin):
        session.add(
            AttendanceRegularisation(
                tenant_id=tenant.id,
                employee_id=person.employee_id,
                attendance_date=date(2026, 9, 1),
                requested_status=AttendanceStatus.PRESENT,
                reason="Forgot to punch out",
            )
        )
    session.add(
        LifecycleEvent(
            tenant_id=tenant.id,
            employee_id=report.employee_id,
            event_type=LifecycleEventType.PROMOTION,
            effective_from=date(2026, 10, 1),
            reason="Strong year",
            status=LifecycleStatus.PENDING_APPROVAL,
        )
    )
    await session.commit()
    for person in (boss, report, stranger, other_manager, hr_ops, hr_admin):
        await as_caller(session, person)
    return {
        "boss": boss,
        "report": report,
        "stranger": stranger,
        "other_manager": other_manager,
        "sub_admin": hr_ops,
        "admin": hr_admin,
    }


def _by_source(inbox) -> dict[str, list]:
    out: dict[str, list] = {}
    for item in inbox.items:
        out.setdefault(item.source, []).append(item)
    return out


async def test_an_employee_has_nothing_to_decide(session, org):
    inbox = await approvals.inbox(session, org["report"])
    assert inbox.total == 0
    # Sources an employee cannot decide are not even listed. Anybody can be named
    # on an approval chain (leave, corrections, timesheets, lifecycle), own an
    # exit clearance item or be asked for 360 feedback.
    assert {s.key for s in inbox.sources} <= {
        "leave", "attendance_regularisation", "timesheet", "lifecycle_event",
        "clearance_task", "feedback_request",
    }


async def test_a_manager_sees_only_their_reporting_line(session, org):
    inbox = _by_source(await approvals.inbox(session, org["boss"]))
    regs = inbox.get("attendance_regularisation", [])
    assert [r.subject_name for r in regs] == ["Ravi Report"]
    assert all(r.link.startswith("/time/attendance-review") for r in regs)
    # Lifecycle decisions are admin-only.
    assert "lifecycle_event" not in inbox

    other = _by_source(await approvals.inbox(session, org["other_manager"]))
    assert other.get("attendance_regularisation", []) == []


async def test_hr_ops_sees_everyone_but_not_admin_only_decisions(session, org):
    inbox = _by_source(await approvals.inbox(session, org["sub_admin"]))
    names = sorted(r.subject_name for r in inbox["attendance_regularisation"])
    assert names == ["Asha Admin", "Ravi Report", "Sita Other"]
    assert "lifecycle_event" not in inbox


async def test_nobody_sees_their_own_request(session, org):
    inbox = _by_source(await approvals.inbox(session, org["admin"]))
    names = sorted(r.subject_name for r in inbox["attendance_regularisation"])
    assert names == ["Ravi Report", "Sita Other"]
    assert [e.subject_name for e in inbox["lifecycle_event"]] == ["Ravi Report"]


async def test_another_tenant_sees_nothing(session, org, other_hr_user):
    inbox = await approvals.inbox(session, other_hr_user)
    assert inbox.total == 0


async def test_the_inbox_is_served_over_http_with_a_count(client, org):
    response = await client.get("/api/v1/approvals", headers=bearer(org["boss"]))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["source"] == "attendance_regularisation"

    count = await client.get("/api/v1/approvals/count", headers=bearer(org["report"]))
    assert count.status_code == 200
    assert count.json() == {"count": 0}
