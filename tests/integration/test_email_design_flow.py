"""Branded email, every email editable, and approving from the email itself.

What these hold, against a real database:

- the admin sees every email the workspace can send, edits one as a new
  version, and can go back to the built-in wording without losing history;
- the email design (colour, sign-off, footer) is saved, audited, and used in
  the preview and in what is actually sent;
- an approval email carries Approve / Reject links for its approver; the link
  only describes the request, the POST decides it as the approver, the audit
  row says it came from email, and a second use is refused;
- a link dies when the approver is signed out everywhere, and no buttons are
  sent when the workspace turned them off or requires two-step sign-in.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from app.core.config import settings
from app.models.audit import AuditLog
from app.models.leave import LeavePolicy, LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.notifications import Channel, NotificationPolicy
from app.models.opsec import TenantSessionPolicy
from app.models.user import UserRole
from app.services.approvals import email_actions
from app.services.leave import service as leave_service
from app.services.notifications import admin as notification_admin
from app.services.notifications import dispatch, email_admin
from app.services.notifications.adapters import ChannelAdapter, DeliveryResult
from app.services.notifications.email_brand import BrandSettings
from tests.integration.conftest import requires_db
from tests.integration.test_leave_flow import _payload, _person

pytestmark = requires_db


class RecordingAdapter(ChannelAdapter):
    name = "recording"

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, message):
        self.sent.append(message)
        return DeliveryResult(provider_message_id=f"rec-{len(self.sent)}")


def _request(method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/v1/public/email-actions/x",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 5000),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


@pytest.fixture(autouse=True)
def restore_adapters():
    original = dict(dispatch._ADAPTERS)
    yield
    dispatch._ADAPTERS.clear()
    dispatch._ADAPTERS.update(original)


@pytest.fixture
def mail(monkeypatch) -> RecordingAdapter:
    monkeypatch.setattr(settings, "WEB_APP_URL", "https://portal.test")
    adapter = RecordingAdapter()
    dispatch.register_adapter(Channel.EMAIL, adapter)
    return adapter


@pytest.fixture
async def org(session, tenant, legal_entity, location):
    session.add(NotificationPolicy(tenant_id=tenant.id, quiet_hours_enabled=False))
    _hr_emp, hr = await _person(
        session, tenant, legal_entity, location, code="HRA", name="Asha Admin", role=UserRole.ADMIN
    )
    boss_emp, boss = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="MGR",
        name="Mala Manager",
        role=UserRole.EMPLOYEE,
    )
    meera_emp, meera = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="EMP",
        name="Meera Nair",
        role=UserRole.EMPLOYEE,
        manager=boss_emp,
    )
    earned = LeaveType(tenant_id=tenant.id, name="Earned Leave", code="EL")
    session.add(earned)
    await session.flush()
    session.add(
        LeavePolicy(
            tenant_id=tenant.id,
            leave_type_id=earned.id,
            name="Everyone",
            annual_quota_days=Decimal(24),
            accrual_frequency="annual",
            effective_from=datetime(2020, 1, 1, tzinfo=UTC).date(),
        )
    )
    await session.commit()
    return {"hr": hr, "boss": boss, "meera": meera, "meera_emp": meera_emp, "earned": earned}


def _token(html: str, action: str) -> str:
    found = re.search(rf"https://portal\.test/act/([^?\"]+)\?do={action}", html)
    assert found, "no action link in the email"
    return found.group(1)


# ── Every email, editable ──────────────────────────────────────────────────


async def test_every_email_is_listed_and_an_edit_can_be_undone(session, org) -> None:
    hr = org["hr"]
    listed = {row["event_key"]: row for row in await email_admin.catalogue(session, hr)}
    assert set(dispatch.DEFAULT_TEMPLATES) <= set(listed)
    leave = listed["leave.approval.requested"]
    assert leave["custom"] == {}
    assert "employee_name" in leave["placeholders"]
    assert leave["group"] == "Time off"

    await notification_admin.save_template(
        session,
        hr,
        event_key="leave.approval.requested",
        channel=Channel.EMAIL,
        locale="en-IN",
        subject="Please review {employee_name}'s leave",
        body="{employee_name} asked for {leave_type}.",
    )
    listed = {row["event_key"]: row for row in await email_admin.catalogue(session, hr)}
    assert listed["leave.approval.requested"]["custom"]["email"]["version"] == 1

    result = await email_admin.reset_template(
        session, hr, event_key="leave.approval.requested", channel=Channel.EMAIL
    )
    assert result["retired"] == 1
    listed = {row["event_key"]: row for row in await email_admin.catalogue(session, hr)}
    assert listed["leave.approval.requested"]["custom"] == {}
    audits = (
        await session.scalars(
            select(AuditLog.summary).where(AuditLog.entity_type == "notification_template")
        )
    ).all()
    assert any("built-in wording" in (summary or "") for summary in audits)


async def test_the_design_is_saved_audited_and_previewed(session, org) -> None:
    hr = org["hr"]
    saved = await email_admin.save_branding(
        session,
        hr,
        BrandSettings(
            accent_colour="#7A3E9D",
            sign_off="Warm regards,\nPeople team",
            footer_text="Acme Technologies · Prestige Tech Park, Bengaluru",
        ),
        request=None,
    )
    assert saved["accent_colour"] == "#7A3E9D"
    assert saved["company_name"] == "Acme Technologies"
    assert saved["has_logo"] is False

    shown = await email_admin.preview(
        session,
        hr,
        event_key="leave.approval.requested",
        subject="Leave approval needed",
        body="{employee_name} asked for {leave_type} from {start_date}.",
    )
    assert "Meera Nair asked for Earned Leave from 5 Nov 2026." in shown["html"]
    assert "#7A3E9D" in shown["html"]
    assert "Warm regards,<br>People team" in shown["html"]
    assert "Powered by AskMira" in shown["text"]

    audit = await session.scalar(select(AuditLog).where(AuditLog.entity_type == "email_branding"))
    assert audit is not None and audit.changes["after"]["accent_colour"] == "#7A3E9D"


async def test_sent_email_is_branded(session, org, mail) -> None:
    await email_admin.save_branding(
        session, org["hr"], BrandSettings(sign_off="People team"), request=None
    )
    await dispatch.notify(
        session,
        tenant_id=org["hr"].tenant_id,
        event_key="system.announcement",
        recipient_user_ids=[org["meera"].id],
        payload={"title": "Office closed", "message": "Closed on Friday for Diwali."},
    )
    message = mail.sent[-1]
    assert message.html and "Acme Technologies" in message.html
    assert "People team" in message.html
    assert "Closed on Friday for Diwali." in message.body
    assert "Powered by AskMira" in message.body


# ── Approve or reject from the email ───────────────────────────────────────


async def test_the_approver_decides_from_the_email(session, org, mail) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))
    to_boss = [m for m in mail.sent if m.recipient_email == org["boss"].email]
    assert to_boss, "the manager was not emailed"
    html = to_boss[-1].html
    assert "do=approve" in html and "do=reject" in html
    token = _token(html, "approve")

    shown = await email_actions.describe(session, token, _request())
    assert shown["state"] == "waiting"
    assert shown["approver_name"] == "Mala Manager"
    assert shown["subject_name"] == "Meera Nair"

    done = await email_actions.decide(
        session, token, _request("POST"), approve=True, comment="Enjoy the wedding."
    )
    assert done["decision"] == "approved"
    leave = await session.get(LeaveRequest, created.id)
    await session.refresh(leave)
    assert leave.status is LeaveRequestStatus.APPROVED

    via = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == created.id, AuditLog.actor_user_id == org["boss"].id
            )
        )
    ).all()
    assert any((row.changes or {}).get("via") == "email_link" for row in via)

    with pytest.raises(HTTPException) as again:
        await email_actions.decide(session, token, _request("POST"), approve=False, comment=None)
    assert again.value.status_code == 409
    assert (await email_actions.describe(session, token, _request()))["state"] == "not_waiting"


async def test_rejecting_from_the_email(session, org, mail) -> None:
    created = await leave_service.apply(session, org["meera"], _payload(org))
    html = next(m.html for m in reversed(mail.sent) if m.recipient_email == org["boss"].email)
    await email_actions.decide(
        session, _token(html, "reject"), _request("POST"), approve=False, comment="Release week."
    )
    leave = await session.get(LeaveRequest, created.id)
    await session.refresh(leave)
    assert leave.status is LeaveRequestStatus.REJECTED


async def test_a_link_dies_when_the_approver_is_signed_out_everywhere(session, org, mail) -> None:
    await leave_service.apply(session, org["meera"], _payload(org))
    html = next(m.html for m in reversed(mail.sent) if m.recipient_email == org["boss"].email)
    token = _token(html, "approve")
    org["boss"].tokens_valid_from = datetime.now(UTC) + timedelta(seconds=5)
    await session.commit()
    with pytest.raises(HTTPException) as gone:
        await email_actions.describe(session, token, _request())
    assert gone.value.status_code == 410


async def test_a_forged_or_foreign_token_is_refused(session, org) -> None:
    with pytest.raises(HTTPException) as bad:
        await email_actions.describe(session, "not-a-token", _request())
    assert bad.value.status_code == 410


async def test_no_buttons_when_switched_off_or_two_step_is_required(session, org, mail) -> None:
    approval = {"queue": "leave", "item_id": "00000000-0000-0000-0000-000000000001"}
    assert await email_actions.buttons_for(session, org["boss"], approval)

    await email_admin.save_branding(
        session, org["hr"], BrandSettings(approve_from_email=False), request=None
    )
    assert await email_actions.buttons_for(session, org["boss"], approval) == ()

    await email_admin.save_branding(session, org["hr"], BrandSettings(), request=None)
    session.add(TenantSessionPolicy(tenant_id=org["hr"].tenant_id, require_two_step="everyone"))
    await session.commit()
    assert await email_actions.buttons_for(session, org["boss"], approval) == ()

    # The email still goes, with the portal button instead.
    await leave_service.apply(session, org["meera"], _payload(org))
    html = next(m.html for m in reversed(mail.sent) if m.recipient_email == org["boss"].email)
    assert "/act/" not in html
    assert ">Open AskMira<" in html
