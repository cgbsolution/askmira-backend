"""Exit interviews, rehire eligibility, work anniversaries and attendance nudges.

What these hold:

- an exit interview is the leaver's and HR's — never their manager's — and the
  summary is withheld until there are enough to keep it anonymous;
- "not eligible for rehire" needs a reason, and HR sees it when the person applies again;
- a work anniversary is marked once, publicly only for somebody who shares theirs;
- an approver is reminded of corrections waiting on them, HR of an unlocked month.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.attendance import AttendanceRegularisation, AttendanceStatus
from app.models.culture import MilestoneRule, Shoutout, ShoutoutVisibility
from app.models.culture_depth import EventPrivacyPreference
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import ExitRecord, ExitStatus, ExitType
from app.models.notifications import Notification
from app.models.user import User, UserRole
from app.services.attendance import reminders
from app.services.culture import milestones
from app.services.exit_workflow import interviews
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def _person(session, tenant, entity, location, code, *, manager=None, joined=None,
                  status=EmploymentStatus.ACTIVE, email=None, login_role=None):
    employee = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code.title(), last_name="Test",
        work_email=email or f"{code.lower()}@acme-demo.com", legal_entity_id=entity.id,
        location_id=location.id, manager_id=manager.id if manager else None, status=status,
        date_of_joining=joined or date(2022, 1, 1),
        date_of_exit=date(2026, 3, 31) if status is EmploymentStatus.EXITED else None,
    )
    session.add(employee)
    await session.flush()
    user = None
    if login_role is not None:
        user = User(
            tenant_id=tenant.id, email=f"{code.lower()}.login@acme-demo.com", password_hash="x",
            full_name=f"{code.title()} Test", role=login_role, is_active=True,
            employee_id=employee.id,
        )
        session.add(user)
    await session.commit()
    if user is not None:
        user = await as_caller(session, user)
    return employee, user


async def test_exit_interviews_are_the_leavers_and_hrs_only(
    session, tenant, hr_user, legal_entity, location
) -> None:
    boss, boss_login = await _person(session, tenant, legal_entity, location, "BOSS",
                                     login_role=UserRole.EMPLOYEE)
    leaver, leaver_login = await _person(session, tenant, legal_entity, location, "LEAVER",
                                         manager=boss, login_role=UserRole.EMPLOYEE)
    record = ExitRecord(
        tenant_id=tenant.id, employee_id=leaver.id, exit_type=ExitType.RESIGNATION,
        status=ExitStatus.NOTICE_PERIOD, resignation_date=date(2026, 9, 1),
    )
    session.add(record)
    await session.commit()

    answers = {"primary_reason": "manager", "ratings": {"manager": 2, "growth": 3},
               "would_return": False, "comments": "Little feedback."}
    saved = await interviews.record_interview(session, leaver_login, record.id, answers)
    await session.commit()
    assert saved["mode"] == "self"

    with pytest.raises(HTTPException) as exc:
        await interviews.get_interview(session, boss_login, record.id)
    assert exc.value.status_code == 403, "never the manager"
    assert (await interviews.get_interview(session, hr_user, record.id))["comments"]

    with pytest.raises(HTTPException) as exc:
        await interviews.record_interview(
            session, leaver_login, record.id, {"primary_reason": "astrology"}
        )
    assert exc.value.status_code == 422

    summary = await interviews.interview_summary(
        session, hr_user, start=date(2026, 1, 1), end=date(2026, 12, 31)
    )
    assert summary["withheld"] is True, "one interview is one named person"


async def test_a_no_rehire_needs_a_reason_and_follows_the_person(
    session, tenant, hr_user, legal_entity, location
) -> None:
    gone, _ = await _person(session, tenant, legal_entity, location, "GONE",
                            status=EmploymentStatus.EXITED, email="gone@acme-demo.com")
    record = ExitRecord(
        tenant_id=tenant.id, employee_id=gone.id, exit_type=ExitType.TERMINATION,
        status=ExitStatus.SETTLED, resignation_date=date(2026, 3, 1),
    )
    session.add(record)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await interviews.set_rehire_eligibility(session, hr_user, record.id, eligible=False,
                                                note="  ")
    assert exc.value.status_code == 422
    await interviews.set_rehire_eligibility(
        session, hr_user, record.id, eligible=False, note="Policy breach, March 2026"
    )
    await session.commit()

    match = await interviews.former_employee(
        session, hr_user, email="GONE@acme-demo.com", phone=None
    )
    assert match is not None and match["is_rehire_eligible"] is False
    assert match["rehire_note"] == "Policy breach, March 2026"


async def test_an_anniversary_is_marked_once_and_privately_without_consent(
    session, tenant, hr_user, legal_entity, location
) -> None:
    today = date(2026, 10, 14)
    sharer, _ = await _person(session, tenant, legal_entity, location, "SHARE",
                              joined=date(2021, 10, 14))
    quiet, _ = await _person(session, tenant, legal_entity, location, "QUIET",
                             joined=date(2021, 10, 14))
    session.add_all([
        EventPrivacyPreference(tenant_id=tenant.id, employee_id=sharer.id,
                               share_work_anniversary=True),
        MilestoneRule(tenant_id=tenant.id, years=5, message="Five years, {name}!"),
    ])
    await session.commit()

    assert (await milestones.celebrate_due(session, hr_user, today=today))["posted"] == 2
    posts = {
        p.to_employee_id: p
        for p in (await session.scalars(select(Shoutout))).all()
    }
    assert posts[sharer.id].visibility is ShoutoutVisibility.COMPANY
    assert posts[quiet.id].visibility is ShoutoutVisibility.PRIVATE
    assert posts[sharer.id].milestone_years == 5 and "Share" in posts[sharer.id].message

    assert (await milestones.celebrate_due(session, hr_user, today=today))["posted"] == 0


async def test_approvers_and_hr_are_nudged(
    session, tenant, hr_user, legal_entity, location
) -> None:
    boss, boss_login = await _person(session, tenant, legal_entity, location, "LEAD",
                                     login_role=UserRole.EMPLOYEE)
    worker, _ = await _person(session, tenant, legal_entity, location, "WORK", manager=boss)
    row = AttendanceRegularisation(
        tenant_id=tenant.id, employee_id=worker.id, attendance_date=date(2026, 9, 29),
        requested_status=AttendanceStatus.PRESENT, reason="Forgot to punch",
    )
    session.add(row)
    await session.commit()

    now = datetime.now(UTC) + timedelta(days=3)
    outcome = await reminders.send_attendance_reminders(
        session, hr_user, today=date(2026, 10, 5), now=now
    )
    assert outcome["approvers_reminded"] == 1
    assert outcome["hr_reminded_unlocked"] >= 1, "September is not locked"
    events = {
        (n.event_key, n.recipient_user_id)
        for n in (await session.scalars(select(Notification))).all()
    }
    assert ("attendance.regularisation.reminder", boss_login.id) in events

