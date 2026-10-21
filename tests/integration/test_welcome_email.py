"""A new login's welcome email: where it goes, what it carries, and what it never carries."""

from __future__ import annotations

import re
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models import AuditLog, Tenant, User, UserRole
from app.models.employee import Employee
from app.models.identity import IdentityProvider, IdpType
from app.models.notifications import Channel, Notification, NotificationPolicy
from app.models.onboarding import (
    OnboardingOwnerType,
    OnboardingPlan,
    OnboardingTask,
    OnboardingTaskStatus,
    OnboardingTaskType,
)
from app.services.identity.welcome import WELCOME_EVENT
from app.services.notifications import dispatch as notification_service
from app.services.notifications.adapters import ChannelAdapter, DeliveryResult, PermanentFailure
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

API = "/api/v1/admin"
LINK = re.compile(r"/reset-password\?token=([A-Za-z0-9_\-]+)&welcome=1")


class RecordingAdapter(ChannelAdapter):
    name = "recording"

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, message):
        self.sent.append(message)
        return DeliveryResult(provider_message_id=f"rec-{len(self.sent)}")


class RejectingAdapter(ChannelAdapter):
    name = "rejecting"

    async def send(self, message):
        raise PermanentFailure("Mailbox does not exist")


@pytest.fixture(autouse=True)
def restore_adapters():
    original = dict(notification_service._ADAPTERS)
    yield
    notification_service._ADAPTERS.clear()
    notification_service._ADAPTERS.update(original)


@pytest.fixture(autouse=True)
def no_platform_relay(monkeypatch):
    # The developer's .env may point at a real relay; no test reaches it.
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "SMTP_FROM", "")


@pytest.fixture
def mailbox() -> RecordingAdapter:
    recorder = RecordingAdapter()
    notification_service.register_adapter(Channel.EMAIL, recorder)
    return recorder


@pytest.fixture(autouse=True)
async def quiet_hours_off(session, tenant: Tenant) -> None:
    session.add(NotificationPolicy(tenant_id=tenant.id, quiet_hours_enabled=False))
    await session.commit()


async def _joiner_without_login(session, tenant: Tenant) -> Employee:
    holder = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Riya Joiner")
    employee = await session.get(Employee, holder.employee_id)
    # Free the record so a new login can link to it.
    holder.employee_id = None
    await session.commit()
    return employee


async def _create(client, owner: User, employee: Employee, **extra):
    return await client.post(
        f"{API}/users",
        json={
            "email": employee.work_email,
            "full_name": "Riya Joiner",
            "role": "employee",
            "employee_id": str(employee.id),
            **extra,
        },
        headers=bearer(owner),
    )


async def test_a_new_login_is_emailed_its_sign_in_details_at_its_work_address(
    client, session, tenant: Tenant, mailbox: RecordingAdapter
):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    employee = await _joiner_without_login(session, tenant)

    response = await _create(client, owner, employee)

    assert response.status_code == 201
    body = response.json()
    assert body["welcome_email"]["status"] == "sent"
    assert body["welcome_email"]["sent_to"] == employee.work_email
    assert body["welcome_email"]["link_expires_at"]

    assert len(mailbox.sent) == 1
    message = mailbox.sent[0]
    assert message.recipient_email == employee.work_email
    assert tenant.name in message.subject
    assert f"/signin/{tenant.slug}" in message.body
    assert f"Your login: {employee.work_email}" in message.body
    assert "set up by Owner One" in message.body
    assert LINK.search(message.body)
    # Never the password itself: rendered bodies are stored.
    assert body["temporary_password"] not in message.body
    stored = await session.scalar(
        select(Notification).where(Notification.event_key == WELCOME_EVENT)
    )
    assert body["temporary_password"] not in stored.rendered_body

    audit_row = await session.scalar(
        select(AuditLog).where(AuditLog.summary.like("Created a employee login%"))
    )
    assert audit_row.changes["welcome_email"] == "sent"


async def test_the_emailed_link_sets_their_password_once(
    client, session, tenant: Tenant, mailbox: RecordingAdapter
):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    employee = await _joiner_without_login(session, tenant)
    await _create(client, owner, employee)
    token = LINK.search(mailbox.sent[0].body).group(1)

    first = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "MyOwnPass1234"}
    )
    assert first.status_code in (200, 204)
    created = await session.scalar(select(User).where(User.email == employee.work_email))
    await session.refresh(created)
    assert created.must_change_password is False

    again = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "AnotherPass1234"}
    )
    assert again.status_code == 400


async def test_the_welcome_tells_a_joiner_about_their_onboarding_checklist(
    client, session, tenant: Tenant, mailbox: RecordingAdapter
):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    employee = await _joiner_without_login(session, tenant)
    plan = OnboardingPlan(
        tenant_id=tenant.id,
        employee_id=employee.id,
        template_name="Standard",
        joining_date=date(2026, 11, 2),
    )
    session.add(plan)
    await session.flush()
    for title, owner_type, status in (
        ("Upload PAN", OnboardingOwnerType.NEW_JOINER, OnboardingTaskStatus.PENDING),
        ("Sign the handbook", OnboardingOwnerType.NEW_JOINER, OnboardingTaskStatus.PENDING),
        ("Bank details", OnboardingOwnerType.NEW_JOINER, OnboardingTaskStatus.DONE),
        ("Laptop", OnboardingOwnerType.IT, OnboardingTaskStatus.PENDING),
    ):
        session.add(
            OnboardingTask(
                tenant_id=tenant.id,
                plan_id=plan.id,
                title=title,
                owner_type=owner_type,
                due_date=date(2026, 11, 2),
                task_type=OnboardingTaskType.GENERAL,
                status=status,
            )
        )
    await session.commit()

    await _create(client, owner, employee)

    body = mailbox.sent[0].body
    # Their own open tasks only: not the done one, not IT's.
    assert "2 task(s) for you around your joining date of 2 November 2026" in body
    assert "My onboarding" in body


async def test_an_admin_can_choose_not_to_send_it(
    client, session, tenant: Tenant, mailbox: RecordingAdapter
):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    employee = await _joiner_without_login(session, tenant)

    response = await _create(client, owner, employee, send_welcome_email=False)

    assert response.status_code == 201
    assert response.json()["welcome_email"] is None
    assert response.json()["temporary_password"]
    assert mailbox.sent == []
    assert await session.scalar(select(func.count(Notification.id))) == 0


async def test_without_a_mail_relay_the_admin_is_told_nothing_left(client, session, tenant: Tenant):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    employee = await _joiner_without_login(session, tenant)

    response = await _create(client, owner, employee)

    welcome = response.json()["welcome_email"]
    assert welcome["status"] == "no_relay"
    assert "Settings → Notifications" in welcome["detail"]


async def test_a_refused_email_still_creates_the_login_and_says_why(
    client, session, tenant: Tenant
):
    notification_service.register_adapter(Channel.EMAIL, RejectingAdapter())
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    employee = await _joiner_without_login(session, tenant)

    response = await _create(client, owner, employee)

    assert response.status_code == 201
    welcome = response.json()["welcome_email"]
    assert welcome["status"] == "failed"
    assert "Mailbox does not exist" in welcome["detail"]
    assert await session.scalar(select(User).where(User.email == employee.work_email))


async def test_resending_retires_the_old_link_and_stops_once_they_have_a_password(
    client, session, tenant: Tenant, mailbox: RecordingAdapter
):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    employee = await _joiner_without_login(session, tenant)
    created = (await _create(client, owner, employee)).json()["user"]
    old_token = LINK.search(mailbox.sent[0].body).group(1)

    resent = await client.post(f"{API}/users/{created['id']}/welcome-email", headers=bearer(owner))
    assert resent.status_code == 200
    assert resent.json()["status"] == "sent"
    new_token = LINK.search(mailbox.sent[1].body).group(1)
    assert new_token != old_token

    stale = await client.post(
        "/api/v1/auth/reset-password", json={"token": old_token, "new_password": "MyOwnPass1234"}
    )
    assert stale.status_code == 400
    fresh = await client.post(
        "/api/v1/auth/reset-password", json={"token": new_token, "new_password": "MyOwnPass1234"}
    )
    assert fresh.status_code in (200, 204)

    again = await client.post(f"{API}/users/{created['id']}/welcome-email", headers=bearer(owner))
    assert again.status_code == 400
    assert "Forgot password" in again.json()["detail"]


async def test_only_an_admin_resends_and_only_in_their_own_workspace(
    client, session, tenant: Tenant, other_tenant: Tenant, mailbox: RecordingAdapter
):
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    sub_admin = await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Sub Admin")
    target = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Emp One")
    target.must_change_password = True
    await session.commit()
    outsider = await make_person(session, other_tenant, role=UserRole.ADMIN, name="Globex Admin")

    path = f"{API}/users/{target.id}/welcome-email"
    assert (await client.post(path, headers=bearer(sub_admin))).status_code == 403
    assert (await client.post(path, headers=bearer(outsider))).status_code == 404
    assert (
        await client.post(f"{API}/users/{uuid.uuid4()}/welcome-email", headers=bearer(owner))
    ).status_code == 404
    assert mailbox.sent == []


async def test_a_workspace_on_enforced_sso_gets_no_password_link(
    client, session, tenant: Tenant, mailbox: RecordingAdapter
):
    session.add(
        IdentityProvider(
            tenant_id=tenant.id,
            name="Acme Okta",
            idp_type=IdpType.OIDC,
            enabled=True,
            discovery_url="https://acme.okta.example/.well-known/openid-configuration",
            client_id="acme",
            enforce_sso=True,
        )
    )
    await session.commit()
    owner = await make_person(session, tenant, role=UserRole.ADMIN, name="Owner One")
    employee = await _joiner_without_login(session, tenant)

    response = await _create(client, owner, employee)

    assert response.json()["welcome_email"]["link_expires_at"] is None
    body = mailbox.sent[0].body
    assert not LINK.search(body)
    assert "company account" in body
