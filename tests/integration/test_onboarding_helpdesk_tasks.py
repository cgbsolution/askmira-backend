"""A joiner's IT tasks go to CGB Helpdesk when it is linked.

* Starting onboarding raises one helpdesk ticket listing the IT tasks, as the
  HR person who started it (signed use=act, create_ticket), in the category
  that fits; each IT task carries the ticket's number.
* A helpdesk that refuses never stops onboarding: the plan starts, the IT tasks
  say it was not sent, the audit log says why.
* Without the helpdesk linked, or with no IT tasks, nothing is sent.
* It is raised only once the onboarding is saved: a save that rolls back
  raises nothing, and a plan is sent once however often it is committed.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditLog,
    Employee,
    OnboardingOwnerType,
    OnboardingTask,
    Tenant,
    UserRole,
)
from app.schemas.onboarding import PlanCreate
from app.services.onboarding import helpdesk_tasks, rules
from app.services.onboarding import service as onboarding
from tests.integration.conftest import requires_db
from tests.integration.platform_support import make_person
from tests.integration.test_helpdesk_link import FakeHelpdesk, _link, desk  # noqa: F401

pytestmark = requires_db


class _Same:
    """The follow-up's own session, in a test: the test's session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def __call__(self) -> _Same:
        return self

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *_: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _own_session(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helpdesk_tasks, "session_factory", _Same(session))


OPTIONS = {
    "categories": [
        {"name": "Software", "sub_categories": ["Email", "VPN"]},
        {"name": "Hardware", "sub_categories": ["Laptop Issue", "New Laptop"]},
    ]
}


async def _start(session: AsyncSession, tenant: Tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    joiner = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Arun Joiner")
    employee = await session.get(Employee, joiner.employee_id)
    employee.date_of_joining = rules.india_today()
    await session.commit()
    template = await onboarding.create_standard_template(session, hr)
    await session.commit()
    plan = await onboarding.start_plan(
        session, hr, PlanCreate(employee_id=employee.id, template_id=template.id)
    )
    await session.commit()
    await helpdesk_tasks.drain()
    tasks = (
        await session.scalars(select(OnboardingTask).where(OnboardingTask.plan_id == plan.id))
    ).all()
    it = [t for t in tasks if t.owner_type is OnboardingOwnerType.IT]
    return hr, employee, plan, it


async def test_the_it_tasks_become_one_helpdesk_ticket(
    session: AsyncSession,
    tenant: Tenant,
    desk: FakeHelpdesk,  # noqa: F811
) -> None:
    await _link(session, tenant)
    desk.answers[("query", "options")] = OPTIONS
    desk.answers[("act", "create_ticket")] = {"ticket": {"id": "t-1", "ref": "481"}}
    hr, employee, plan, it = await _start(session, tenant)
    assert it, "the standard checklist has IT tasks"

    [sent] = [c for c in desk.calls if c["body"]["kind"] == "create_ticket"]
    assert sent["use"] == "act"
    params = sent["body"]["params"]
    assert sent["body"]["actor"]["email"] == hr.email
    assert employee.employee_code in params["subject"] and "IT setup" in params["subject"]
    assert params["category"] == "Hardware" and params["sub_category"] == "New Laptop"
    for task in it:
        assert task.title in params["description"]
        assert "In CGB Helpdesk as ticket #481." in (task.description or "")
    summaries = (
        await session.scalars(select(AuditLog.summary).where(AuditLog.entity_id == plan.id))
    ).all()
    assert any("sent to CGB Helpdesk as ticket #481" in s for s in summaries)


async def test_a_refusing_helpdesk_never_stops_onboarding(
    session: AsyncSession,
    tenant: Tenant,
    desk: FakeHelpdesk,  # noqa: F811
) -> None:
    await _link(session, tenant)
    desk.answers[("query", "options")] = OPTIONS
    desk.answers[("act", "create_ticket")] = httpx.Response(
        422, json={"detail": "That category is closed for new tickets."}
    )
    _hr, employee, plan, it = await _start(session, tenant)
    assert plan.status.value == "in_progress"
    for task in it:
        assert "Not sent to CGB Helpdesk" in (task.description or "")
        assert "Raise it there by hand." in (task.description or "")
    summaries = (
        await session.scalars(select(AuditLog.summary).where(AuditLog.entity_id == plan.id))
    ).all()
    assert any("could not be sent to CGB Helpdesk" in s for s in summaries)


async def test_nothing_is_sent_without_the_helpdesk_linked(
    session: AsyncSession,
    tenant: Tenant,
    desk: FakeHelpdesk,  # noqa: F811
) -> None:
    _hr, _employee, _plan, it = await _start(session, tenant)
    assert desk.calls == []
    assert all("Helpdesk" not in (t.description or "") for t in it)


async def test_a_save_that_rolls_back_raises_no_ticket_and_a_plan_is_sent_once(
    session: AsyncSession,
    tenant: Tenant,
    desk: FakeHelpdesk,  # noqa: F811
) -> None:
    await _link(session, tenant)
    desk.answers[("query", "options")] = OPTIONS
    desk.answers[("act", "create_ticket")] = {"ticket": {"id": "t-1", "ref": "481"}}
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    joiner = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Arun Joiner")
    employee = await session.get(Employee, joiner.employee_id)
    employee.date_of_joining = rules.india_today()
    await session.commit()
    template = await onboarding.create_standard_template(session, hr)
    await session.commit()
    asked = PlanCreate(employee_id=employee.id, template_id=template.id)
    tenant_id, hr_id = tenant.id, hr.id

    # The joiner's save fails after the plan was started: nothing reaches the helpdesk.
    await onboarding.start_plan(session, hr, asked)
    await session.rollback()
    await helpdesk_tasks.drain()
    assert [c for c in desk.calls if c["body"]["kind"] == "create_ticket"] == []

    # Saved: one ticket, and committing again sends nothing more.
    await session.refresh(hr)
    plan = await onboarding.start_plan(session, hr, asked)
    await session.commit()
    await helpdesk_tasks.drain()
    await session.commit()
    await helpdesk_tasks.drain()
    assert len([c for c in desk.calls if c["body"]["kind"] == "create_ticket"]) == 1
    await helpdesk_tasks._send_later(tenant_id, hr_id, plan.id)
    assert len([c for c in desk.calls if c["body"]["kind"] == "create_ticket"]) == 1
