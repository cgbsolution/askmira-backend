"""A customer approves or rejects a consultant's week from an emailed link."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import ApproverRoute, Employee, EmploymentStatus, EmploymentType
from app.models.notifications import Notification
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.timesheets import Timesheet, TimesheetApprovalLink, TimesheetStatus
from app.models.user import User, UserRole
from app.services import timesheets
from app.services.consultants import timesheet_approval
from app.services.notifications import dispatch
from app.services.notifications.adapters import ConsoleEmailAdapter
from app.services.timesheets import EntryIn, RateIn
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import client  # noqa: F401  (fixture)

pytestmark = requires_db

MONDAY = date(2026, 9, 7)
CONTACT = "asha@client.example.com"


@pytest.fixture(autouse=True)
def _web_address(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_APP_URL", "https://app.example.test")


@pytest.fixture
def mail(monkeypatch) -> ConsoleEmailAdapter:
    adapter = ConsoleEmailAdapter()
    monkeypatch.setattr(dispatch, "_DEFAULT_EMAIL_ADAPTER", adapter)
    return adapter


async def _login(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, code: str, **extra
) -> tuple[Employee, User]:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com",
        legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE,
        **extra,
    )
    session.add(person)
    await session.flush()
    login = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}.cta@acme-demo.com",
        password_hash="x",
        full_name=code.title(),
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=person.id,
    )
    session.add(login)
    await session.commit()
    return person, await as_caller(session, login)


async def _customer_consultant(session, tenant, entity, code, *, approver=None, email=CONTACT):
    return await _login(
        session,
        tenant,
        entity,
        code,
        employment_type=EmploymentType.CONSULTANT,
        timesheet_approver_route=ApproverRoute.CUSTOMER,
        customer_contact_name="Asha",
        customer_contact_email=email,
        internal_approver_user_id=approver.id if approver else None,
    )


async def _fill(session, login, *days: tuple[int, int]) -> None:
    await timesheets.save_week(
        session,
        login,
        MONDAY,
        [EntryIn(work_date=date(2026, 9, d), hours=Decimal(h)) for d, h in days],
    )


def _token(mail: ConsoleEmailAdapter) -> str:
    """The token in the newest email's Approve button."""
    text = mail.sent[-1].html or ""
    start = text.index("/timesheet/") + len("/timesheet/")
    return text[start : text.index("?", start)]


async def _submitted(session, tenant, hr_user, entity, mail, *, approver=None):
    """A consultant on the customer route with 32 hours at 1200 an hour, submitted."""
    consultant, login = await _customer_consultant(
        session, tenant, entity, "CON1", approver=approver
    )
    await timesheets.add_rate(
        session,
        hr_user,
        RateIn(employee_id=consultant.id, rate=Decimal("1200"), effective_from=date(2026, 1, 1)),
        request=None,
    )
    await _fill(session, login, (7, 8), (8, 8), (9, 8), (10, 8))
    submitted = await timesheets.submit_week(session, login, MONDAY)
    return consultant, login, uuid.UUID(submitted["id"]), _token(mail)


async def _sheet(session, sheet_id) -> Timesheet:
    await session.rollback()
    sheet = await session.scalar(select(Timesheet).where(Timesheet.id == sheet_id))
    assert sheet is not None
    await session.refresh(sheet)
    return sheet


async def test_submit_emails_customer_without_rate_or_amount(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    _, approver = await _login(session, tenant, legal_entity, "ADMN")
    _, _, _, token = await _submitted(
        session, tenant, hr_user, legal_entity, mail, approver=approver
    )

    assert [m.recipient_email for m in mail.sent] == [CONTACT]
    message = mail.sent[0]
    assert "32" in message.body
    assert message.reply_to == approver.email
    for text in (message.subject or "", message.body, message.html or ""):
        assert "1200" not in text and "38400" not in text and "38,400" not in text
    assert f"/timesheet/{token}?answer=approve" in (message.html or "")
    assert f"/timesheet/{token}?answer=reject" in (message.html or "")

    page = await timesheet_approval.describe(session, token)
    assert page["state"] == "waiting"
    assert page["total_hours"] == "32.00"
    assert len(page["days"]) == 4
    flat = repr(page).lower()
    assert "rate" not in flat and "amount" not in flat and "1200" not in flat
    row = await session.scalar(select(TimesheetApprovalLink))
    assert row is not None and row.token_hash != token and len(row.token_hash) == 64
    assert row.sent_at is not None and row.sent_to_email == CONTACT
    assert row.expires_at > datetime.now(UTC) + timedelta(days=13)


async def test_submit_needs_a_contact_email(session, tenant, hr_user, legal_entity, mail) -> None:
    _, login = await _customer_consultant(session, tenant, legal_entity, "CON2", email="")
    await _fill(session, login, (7, 8))
    with pytest.raises(HTTPException) as refused:
        await timesheets.submit_week(session, login, MONDAY)
    assert refused.value.status_code == 422
    assert mail.sent == []


async def test_send_failure_does_not_fail_the_submit(
    session, tenant, hr_user, legal_entity, monkeypatch
) -> None:
    class Down(ConsoleEmailAdapter):
        async def send(self, message):
            raise RuntimeError("relay down")

    monkeypatch.setattr(dispatch, "_DEFAULT_EMAIL_ADAPTER", Down())
    _, login = await _customer_consultant(session, tenant, legal_entity, "CON3")
    await _fill(session, login, (7, 8))
    submitted = await timesheets.submit_week(session, login, MONDAY)
    assert submitted["status"] == "submitted"
    assert (await _sheet(session, uuid.UUID(submitted["id"]))).status is TimesheetStatus.SUBMITTED


async def test_get_does_not_decide(session, tenant, hr_user, legal_entity, mail) -> None:
    _, _, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await timesheet_approval.describe(session, token)
    await timesheet_approval.describe(session, token)
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.SUBMITTED


async def test_unknown_token_is_404(session) -> None:
    with pytest.raises(HTTPException) as missing:
        await timesheet_approval.describe(session, "not-a-token")
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as missing:
        await timesheet_approval.decide(session, "not-a-token", approve=True, reason=None, ip=None)
    assert missing.value.status_code == 404


async def test_customer_approves(session, tenant, hr_user, legal_entity, mail) -> None:
    _, login, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    consultant_login_id = login.id
    done = await timesheet_approval.decide(
        session, token, approve=True, reason=None, ip="203.0.113.9"
    )
    assert done["state"] == "decided"
    sheet = await _sheet(session, sheet_id)
    assert sheet.status is TimesheetStatus.APPROVED
    assert sheet.decided_at is not None and sheet.decided_by_user_id is None
    assert sheet.approval_proof and sheet.approval_proof["email"] == CONTACT
    link = await session.scalar(select(TimesheetApprovalLink))
    assert link is not None and link.used_at is not None and link.decided_ip == "203.0.113.9"

    # A spent link says only that, the company and the week: no name, no hours.
    stale = await timesheet_approval.describe(session, token)
    assert stale["state"] == "decided"
    assert set(stale) == {"state", "company", "week_start"}
    with pytest.raises(HTTPException) as again:
        await timesheet_approval.decide(session, token, approve=False, reason="no", ip=None)
    assert again.value.status_code == 409
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.APPROVED

    audit = await session.scalar(
        select(AuditLog).where(AuditLog.entity_id == sheet_id, AuditLog.summary.contains(CONTACT))
    )
    assert audit is not None and audit.actor_user_id is None
    told = (
        await session.scalars(
            select(Notification.recipient_user_id).where(
                Notification.event_key == "timesheets.decided"
            )
        )
    ).all()
    assert consultant_login_id in told


async def test_reject_needs_reason(session, tenant, hr_user, legal_entity, mail) -> None:
    _, _, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    with pytest.raises(HTTPException) as refused:
        await timesheet_approval.decide(session, token, approve=False, reason=None, ip=None)
    assert refused.value.status_code == 422
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.SUBMITTED

    await timesheet_approval.decide(
        session, token, approve=False, reason="Friday is wrong", ip=None
    )
    sheet = await _sheet(session, sheet_id)
    assert sheet.status is TimesheetStatus.REJECTED
    assert sheet.decision_note == "Friday is wrong"


async def test_edit_or_resend_makes_old_link_stale(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    _, login, sheet_id, old = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await timesheet_approval.decide(session, old, approve=False, reason="Fix Friday", ip=None)
    await _fill(session, login, (7, 8), (8, 8), (9, 8), (10, 4))
    resubmitted = await timesheets.submit_week(session, login, MONDAY)
    assert resubmitted["status"] == "submitted"
    new = _token(mail)
    assert new != old and len(mail.sent) == 2

    assert (await timesheet_approval.describe(session, old))["state"] == "changed"
    with pytest.raises(HTTPException) as stale:
        await timesheet_approval.decide(session, old, approve=True, reason=None, ip=None)
    assert stale.value.status_code == 409
    assert (await timesheet_approval.describe(session, new))["state"] == "waiting"
    await timesheet_approval.decide(session, new, approve=True, reason=None, ip=None)
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.APPROVED


async def test_sending_again_closes_the_earlier_link(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    consultant, _, sheet_id, old = await _submitted(session, tenant, hr_user, legal_entity, mail)
    sheet = await _sheet(session, sheet_id)
    await session.refresh(consultant)
    await session.refresh(hr_user)
    await timesheet_approval.send_to_customer(session, hr_user, sheet, consultant)
    new = _token(mail)
    assert new != old
    assert (await timesheet_approval.describe(session, old))["state"] == "changed"
    assert (await timesheet_approval.describe(session, new))["state"] == "waiting"


async def test_manual_decision_revokes_link(session, tenant, hr_user, legal_entity, mail) -> None:
    _, _, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await timesheets.decide(session, hr_user, sheet_id, approve=True, note=None)
    assert (await timesheet_approval.describe(session, token))["state"] == "decided"
    with pytest.raises(HTTPException) as refused:
        await timesheet_approval.decide(session, token, approve=True, reason=None, ip=None)
    assert refused.value.status_code == 409


async def test_reopen_revokes_link_and_revoke_is_idempotent(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    _, _, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await timesheets.decide(session, hr_user, sheet_id, approve=True, note=None)
    await timesheets.reopen_week(session, hr_user, sheet_id)
    assert (await timesheet_approval.describe(session, token))["state"] == "changed"
    await timesheet_approval.revoke_links(session, tenant.id, sheet_id)
    await timesheet_approval.revoke_links(session, tenant.id, sheet_id)
    await session.commit()
    with pytest.raises(HTTPException) as refused:
        await timesheet_approval.decide(session, token, approve=True, reason=None, ip=None)
    assert refused.value.status_code == 409


async def test_expired_link(session, tenant, hr_user, legal_entity, mail) -> None:
    _, _, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    link = await session.scalar(select(TimesheetApprovalLink))
    assert link is not None
    link.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    expired = await timesheet_approval.describe(session, token)
    assert expired["state"] == "expired"
    assert set(expired) == {"state", "company", "week_start"}
    with pytest.raises(HTTPException) as gone:
        await timesheet_approval.decide(session, token, approve=True, reason=None, ip=None)
    assert gone.value.status_code == 410
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.SUBMITTED


async def test_public_routes_are_private_and_decide_over_http(
    client,  # noqa: F811
    session,
    tenant,
    hr_user,
    legal_entity,
    mail,
) -> None:
    _, _, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    got = await client.get(f"/api/v1/public/timesheets/{token}")
    assert got.status_code == 200, got.text
    assert got.headers["cache-control"] == "private, no-store"
    assert got.headers["x-robots-tag"] == "noindex"
    assert "rate" not in got.text and "amount" not in got.text

    refused = await client.post(
        f"/api/v1/public/timesheets/{token}/decision", json={"approve": False}
    )
    assert refused.status_code == 422
    done = await client.post(f"/api/v1/public/timesheets/{token}/decision", json={"approve": True})
    assert done.status_code == 200, done.text
    assert done.headers["cache-control"] == "private, no-store"
    again = await client.post(f"/api/v1/public/timesheets/{token}/decision", json={"approve": True})
    assert again.status_code == 409
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.APPROVED


async def test_decided_sheet_cannot_be_decided_again_by_the_other_path(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    # Customer decides first: an internal decision is then refused, and the status stands.
    _, _, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await timesheet_approval.decide(session, token, approve=False, reason="Wrong", ip=None)
    with pytest.raises(HTTPException) as refused:
        await timesheets.decide(session, hr_user, sheet_id, approve=True, note=None)
    assert refused.value.status_code == 409
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.REJECTED
    link = await session.scalar(select(TimesheetApprovalLink))
    assert link is not None and link.used_at is not None


async def test_internal_decision_first_then_customer_post_is_409(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    _, _, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await timesheets.decide(session, hr_user, sheet_id, approve=True, note=None)
    with pytest.raises(HTTPException) as refused:
        await timesheet_approval.decide(session, token, approve=False, reason="Wrong", ip=None)
    assert refused.value.status_code == 409
    assert (await timesheet_approval.describe(session, token))["state"] == "decided"
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.APPROVED


async def test_email_build_failure_does_not_break_the_submit(
    session, tenant, hr_user, legal_entity, monkeypatch
) -> None:
    from sqlalchemy import text

    async def broken(sess, *args, **kwargs):
        await sess.execute(text("select 1/0"))

    monkeypatch.setattr(dispatch, "email_wording", broken)
    _, login = await _customer_consultant(session, tenant, legal_entity, "CON4")
    await _fill(session, login, (7, 8))
    submitted = await timesheets.submit_week(session, login, MONDAY)
    assert submitted["status"] == "submitted"


async def test_send_again_is_authorised_checked_and_audited(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    consultant, login, sheet_id, old = await _submitted(
        session, tenant, hr_user, legal_entity, mail
    )
    _, stranger = await _login(session, tenant, legal_entity, "STRN")
    sheet = await _sheet(session, sheet_id)
    await session.refresh(consultant)
    await session.refresh(hr_user)
    await session.refresh(stranger)
    await session.refresh(login)
    with pytest.raises(HTTPException) as refused:
        await timesheet_approval.send_to_customer(session, stranger, sheet, consultant)
    assert refused.value.status_code == 403
    with pytest.raises(HTTPException) as refused:
        await timesheet_approval.send_to_customer(session, login, sheet, consultant)
    assert refused.value.status_code == 403

    sheet = await _sheet(session, sheet_id)
    await session.refresh(consultant)
    await session.refresh(hr_user)
    await timesheet_approval.send_to_customer(session, hr_user, sheet, consultant)
    audit = await session.scalar(
        select(AuditLog).where(AuditLog.entity_id == sheet_id, AuditLog.summary.contains("again"))
    )
    assert audit is not None and audit.actor_user_id == hr_user.id and CONTACT in audit.summary

    await timesheets.decide(session, hr_user, sheet_id, approve=True, note=None)
    sheet = await _sheet(session, sheet_id)
    await session.refresh(consultant)
    await session.refresh(hr_user)
    with pytest.raises(HTTPException) as refused:
        await timesheet_approval.send_to_customer(session, hr_user, sheet, consultant)
    assert refused.value.status_code == 409


async def test_send_again_refuses_an_internal_route_week(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    consultant, login = await _login(
        session,
        tenant,
        legal_entity,
        "CON5",
        employment_type=EmploymentType.CONSULTANT,
        timesheet_approver_route=ApproverRoute.INTERNAL,
        customer_contact_email=CONTACT,
    )
    await _fill(session, login, (7, 8))
    submitted = await timesheets.submit_week(session, login, MONDAY)
    sheet = await _sheet(session, uuid.UUID(submitted["id"]))
    await session.refresh(consultant)
    await session.refresh(hr_user)
    with pytest.raises(HTTPException) as refused:
        await timesheet_approval.send_to_customer(session, hr_user, sheet, consultant)
    assert refused.value.status_code == 409
    assert mail.sent == []


async def test_revoke_links_is_tenant_scoped(session, tenant, hr_user, legal_entity, mail) -> None:
    _, _, sheet_id, token = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await timesheet_approval.revoke_links(session, uuid.uuid4(), sheet_id)
    await session.commit()
    link = await session.scalar(select(TimesheetApprovalLink))
    assert link is not None and link.revoked_at is None
    assert (await timesheet_approval.describe(session, token))["state"] == "waiting"


# ── Reminders and manual approval with proof ───────────────────────────────

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


async def _link_sent_at(session, sheet_id, sent_at: datetime) -> None:
    """Pretend the newest link went out at `sent_at`, so time is controlled, not slept."""
    await session.rollback()
    link = await session.scalar(
        select(TimesheetApprovalLink)
        .where(TimesheetApprovalLink.timesheet_id == sheet_id)
        .order_by(TimesheetApprovalLink.created_at.desc())
        .limit(1)
    )
    assert link is not None
    link.sent_at = sent_at
    await session.commit()


async def test_reminder_after_three_days_at_most_twice(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    _, _, sheet_id, first = await _submitted(session, tenant, hr_user, legal_entity, mail)
    sent = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    await _link_sent_at(session, sheet_id, sent)
    await session.refresh(hr_user)

    await session.refresh(hr_user)

    assert await timesheet_approval.send_reminders(
        session, hr_user, now=sent + timedelta(days=2)
    ) == {"reminded": 0}
    assert len(mail.sent) == 1

    await session.refresh(hr_user)

    assert await timesheet_approval.send_reminders(
        session, hr_user, now=sent + timedelta(days=3)
    ) == {"reminded": 1}
    second = _token(mail)
    assert len(mail.sent) == 2 and second != first
    assert (await timesheet_approval.describe(session, first))["state"] == "changed"
    assert (await timesheet_approval.describe(session, second))["state"] == "waiting"

    await _link_sent_at(session, sheet_id, sent + timedelta(days=3))
    await session.refresh(hr_user)
    assert await timesheet_approval.send_reminders(
        session, hr_user, now=sent + timedelta(days=6)
    ) == {"reminded": 1}
    third = _token(mail)
    assert third != second
    links = (
        await session.scalars(
            select(TimesheetApprovalLink).order_by(TimesheetApprovalLink.created_at)
        )
    ).all()
    assert [link.reminders_sent for link in links] == [0, 1, 2]

    await _link_sent_at(session, sheet_id, sent + timedelta(days=6))
    await session.refresh(hr_user)
    assert await timesheet_approval.send_reminders(
        session, hr_user, now=sent + timedelta(days=9)
    ) == {"reminded": 0}
    assert len(mail.sent) == 3


async def test_no_reminder_for_a_decided_or_never_sent_week(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    _, _, sheet_id, _ = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await session.refresh(hr_user)
    later = datetime.now(UTC) + timedelta(days=5)
    await session.rollback()
    link = await session.scalar(select(TimesheetApprovalLink))
    assert link is not None
    link.sent_at = None
    await session.commit()
    await session.refresh(hr_user)
    assert await timesheet_approval.send_reminders(session, hr_user, now=later) == {"reminded": 0}

    await _link_sent_at(session, sheet_id, datetime.now(UTC) - timedelta(days=5))
    await session.refresh(hr_user)
    await timesheets.decide(session, hr_user, sheet_id, approve=True, note=None)
    await session.refresh(hr_user)
    assert await timesheet_approval.send_reminders(session, hr_user, now=later) == {"reminded": 0}
    assert len(mail.sent) == 1


async def test_manual_approval_with_proof(session, tenant, hr_user, legal_entity, mail) -> None:
    _, approver = await _login(session, tenant, legal_entity, "APPR")
    _, _, sheet_id, _ = await _submitted(
        session, tenant, hr_user, legal_entity, mail, approver=approver
    )
    approver_id = str(approver.id)
    await session.refresh(approver)
    await timesheets.decide(
        session,
        approver,
        sheet_id,
        approve=True,
        note=None,
        proof=("approval.pdf", PDF),
    )
    sheet = await _sheet(session, sheet_id)
    assert sheet.status is TimesheetStatus.APPROVED
    proof = sheet.approval_proof or {}
    assert proof["name"] == "approval.pdf"
    assert proof["uploaded_by"] == approver_id
    assert proof["key"] and proof["uploaded_at"]


async def test_proof_must_be_a_real_document_on_a_consultant_week(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    _, _, sheet_id, _ = await _submitted(session, tenant, hr_user, legal_entity, mail)
    with pytest.raises(HTTPException) as refused:
        await timesheets.decide(
            session, hr_user, sheet_id, approve=True, note=None, proof=("x.pdf", b"not a pdf")
        )
    assert refused.value.status_code == 422
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.SUBMITTED

    await session.refresh(tenant)
    await session.refresh(legal_entity)
    await session.refresh(hr_user)
    staff, staff_login = await _login(session, tenant, legal_entity, "STAF")
    await _fill(session, staff_login, (7, 8))
    week = await timesheets.submit_week(session, staff_login, MONDAY)
    with pytest.raises(HTTPException) as other:
        await timesheets.decide(
            session, hr_user, uuid.UUID(week["id"]), approve=True, note=None, proof=("a.pdf", PDF)
        )
    assert other.value.status_code == 422


async def test_consultant_cannot_approve_own_week(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    _, login, sheet_id, _ = await _submitted(session, tenant, hr_user, legal_entity, mail)
    with pytest.raises(HTTPException) as refused:
        await timesheets.decide(session, login, sheet_id, approve=True, note=None)
    assert refused.value.status_code == 403
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.SUBMITTED


async def test_manual_decide_route_takes_a_file(
    client,  # noqa: F811
    session,
    tenant,
    hr_user,
    legal_entity,
    mail,
) -> None:
    from app.core.deps import get_current_user
    from app.main import app

    _, _, sheet_id, _ = await _submitted(session, tenant, hr_user, legal_entity, mail)
    app.dependency_overrides[get_current_user] = lambda: hr_user
    try:
        bad = await client.post(
            f"/api/v1/timesheets/{sheet_id}/decide/manual",
            data={"approve": "false"},
        )
        assert bad.status_code == 422
        done = await client.post(
            f"/api/v1/timesheets/{sheet_id}/decide/manual",
            data={"approve": "true"},
            files={"file": ("approval.pdf", PDF, "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert done.status_code == 200, done.text
    assert (await _sheet(session, sheet_id)).approval_proof["name"] == "approval.pdf"


async def test_one_failing_week_does_not_stop_other_reminders(
    session, tenant, hr_user, legal_entity, mail, monkeypatch
) -> None:
    _, _, first_id, _ = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await session.refresh(tenant)
    await session.refresh(legal_entity)
    _, other_login = await _customer_consultant(session, tenant, legal_entity, "CON2")
    await _fill(session, other_login, (7, 8))
    second_id = uuid.UUID((await timesheets.submit_week(session, other_login, MONDAY))["id"])
    old = datetime.now(UTC) - timedelta(days=5)
    for sheet_id in (first_id, second_id):
        await _link_sent_at(session, sheet_id, old)

    real = timesheet_approval.issue_link

    async def flaky(sess, sheet, employee, **kwargs):
        if sheet.id == first_id:
            raise RuntimeError("boom")
        return await real(sess, sheet, employee, **kwargs)

    monkeypatch.setattr(timesheet_approval, "issue_link", flaky)
    before = len(mail.sent)
    await session.refresh(hr_user)
    result = await timesheet_approval.send_reminders(session, hr_user, now=datetime.now(UTC))
    assert result == {"reminded": 1}
    assert len(mail.sent) == before + 1


async def test_reminders_never_touch_another_workspace(
    session, tenant, hr_user, legal_entity, mail, other_tenant, other_hr_user
) -> None:
    _, _, sheet_id, _ = await _submitted(session, tenant, hr_user, legal_entity, mail)
    await _link_sent_at(session, sheet_id, datetime.now(UTC) - timedelta(days=5))
    await session.refresh(other_hr_user)
    sent_before = len(mail.sent)

    result = await timesheet_approval.send_reminders(session, other_hr_user, now=datetime.now(UTC))

    assert result == {"reminded": 0}
    assert len(mail.sent) == sent_before
    links = (await session.scalars(select(TimesheetApprovalLink))).all()
    assert len(links) == 1 and links[0].revoked_at is None


async def test_a_reminder_goes_to_whoever_the_week_was_sent_to(
    session, tenant, hr_user, legal_entity, mail
) -> None:
    consultant, _, sheet_id, _ = await _submitted(session, tenant, hr_user, legal_entity, mail)
    consultant_id = consultant.id
    sent = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    await _link_sent_at(session, sheet_id, sent)
    # The contact changes after the week went out: it applies to weeks sent later.
    person = await session.get(Employee, consultant_id)
    person.customer_contact_name = "Someone Else"
    person.customer_contact_email = "someone.else@client.example.com"
    await session.commit()
    await session.refresh(hr_user)

    assert await timesheet_approval.send_reminders(
        session, hr_user, now=sent + timedelta(days=3)
    ) == {"reminded": 1}
    assert mail.sent[-1].recipient_email == CONTACT
    newest = await session.scalar(
        select(TimesheetApprovalLink).order_by(TimesheetApprovalLink.created_at.desc()).limit(1)
    )
    assert newest is not None and newest.sent_to_email == CONTACT
