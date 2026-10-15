"""Slice 05 gates that need a real database."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notifications import (
    Channel,
    DigestMode,
    Notification,
    NotificationDigest,
    NotificationPolicy,
    NotificationPreference,
    NotificationStatus,
    NotificationTemplate,
    WhatsAppConsent,
    WhatsAppTemplate,
    WhatsAppTemplateStatus,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.notifications import dispatch as notification_service
from app.services.notifications.adapters import (
    ChannelAdapter,
    DeliveryResult,
    PermanentFailure,
    TransientFailure,
)
from tests.integration.conftest import requires_db

pytestmark = requires_db


class AlwaysFailsAdapter(ChannelAdapter):
    """Stands in for a provider outage."""

    name = "failing"

    def __init__(self, permanent: bool = False) -> None:
        self.permanent = permanent
        self.attempts = 0

    async def send(self, message):
        self.attempts += 1
        if self.permanent:
            raise PermanentFailure("Address rejected")
        raise TransientFailure("Provider is down")


class RecordingAdapter(ChannelAdapter):
    name = "recording"

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, message):
        self.sent.append(message)
        return DeliveryResult(provider_message_id=f"rec-{len(self.sent)}")


@pytest.fixture(autouse=True)
def restore_adapters():
    """Each test gets the real registry back afterwards."""
    original = dict(notification_service._ADAPTERS)
    yield
    notification_service._ADAPTERS.clear()
    notification_service._ADAPTERS.update(original)


async def _template(
    session: AsyncSession,
    tenant: Tenant,
    event_key: str,
    channel: Channel,
    *,
    whatsapp_template_id=None,
) -> NotificationTemplate:
    template = NotificationTemplate(
        tenant_id=tenant.id,
        event_key=event_key,
        channel=channel,
        # Varies with the payload, so a digest of several items is
        # distinguishable rather than three identical lines.
        subject="About {thing}",
        body="Hello {name}, this is about {thing}.",
        whatsapp_template_id=whatsapp_template_id,
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


@pytest.fixture(autouse=True)
async def quiet_hours_off(session: AsyncSession, tenant: Tenant) -> NotificationPolicy:
    """Every test starts with quiet hours disabled.

    The shipped default is 21:00-08:00 IST, so without this the suite passes or
    fails depending on the wall-clock time it runs at — which is worse than
    useless. Tests that are *about* quiet hours re-enable them explicitly.
    """
    policy = NotificationPolicy(tenant_id=tenant.id, quiet_hours_enabled=False)
    session.add(policy)
    await session.commit()
    return policy


async def _policy(session: AsyncSession, tenant: Tenant, **overrides) -> NotificationPolicy:
    """Adjust the tenant's policy. One row per tenant, so this updates."""
    policy = await session.scalar(
        select(NotificationPolicy).where(NotificationPolicy.tenant_id == tenant.id)
    )
    if policy is None:
        policy = NotificationPolicy(tenant_id=tenant.id)
        session.add(policy)
    for key, value in overrides.items():
        setattr(policy, key, value)
    await session.commit()
    return policy


# ═══════════════════════════════════════════════════════════════════════════
# Preferences
# ═══════════════════════════════════════════════════════════════════════════


async def test_preference_off_suppresses_dispatch_entirely(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate. Not queued, not deferred — nothing at all."""
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    session.add(
        NotificationPreference(
            tenant_id=tenant.id,
            user_id=hr_user.id,
            event_key="system.announcement",
            channel=Channel.EMAIL,
            enabled=False,
            digest_mode=DigestMode.OFF,
        )
    )
    await session.commit()

    outcome = await notification_service.notify(
        session,
        tenant_id=tenant.id,
        event_key="system.announcement",
        recipient_user_ids=[hr_user.id],
        payload={"name": "Priya", "thing": "the office move"},
    )

    assert outcome.sent == 0
    assert outcome.suppressed >= 1
    assert await session.scalar(select(func.count(Notification.id))) == 0


async def test_daily_digest_accumulates_and_sends_once(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: three events, one email."""
    await _template(session, tenant, "culture.shoutout.received", Channel.EMAIL)
    session.add(
        NotificationPreference(
            tenant_id=tenant.id,
            user_id=hr_user.id,
            event_key="culture.shoutout.received",
            channel=Channel.EMAIL,
            enabled=True,
            digest_mode=DigestMode.DAILY_DIGEST,
        )
    )
    await session.commit()

    for index in range(3):
        await notification_service.notify(
            session,
            tenant_id=tenant.id,
            event_key="culture.shoutout.received",
            recipient_user_ids=[hr_user.id],
            payload={"name": "Priya", "thing": f"shoutout {index}"},
        )

    # Nothing sent yet, and exactly one digest holding three items.
    assert await session.scalar(select(func.count(Notification.id))) == 0
    digest = await session.scalar(select(NotificationDigest))
    assert digest is not None
    assert len(digest.items) == 3

    # Its time comes.
    digest.scheduled_for = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    recorder = RecordingAdapter()
    notification_service.register_adapter(Channel.EMAIL, recorder)
    sent = await notification_service.send_digests(session, tenant.id)

    assert sent == 1
    assert len(recorder.sent) == 1, "three events produced exactly one email"
    assert "shoutout 0" in recorder.sent[0].body
    assert "shoutout 2" in recorder.sent[0].body


async def test_an_empty_digest_sends_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A daily "nothing happened" email trains people to ignore the channel."""
    session.add(
        NotificationDigest(
            tenant_id=tenant.id,
            recipient_user_id=hr_user.id,
            scheduled_for=datetime.now(UTC) - timedelta(minutes=1),
            items=[],
        )
    )
    await session.commit()

    recorder = RecordingAdapter()
    notification_service.register_adapter(Channel.EMAIL, recorder)

    assert await notification_service.send_digests(session, tenant.id) == 0
    assert recorder.sent == []


async def test_the_shipped_defaults_apply_when_a_user_has_set_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _template(session, tenant, "leave.approval.requested", Channel.EMAIL)
    await _template(session, tenant, "leave.approval.requested", Channel.IN_APP)

    recorder = RecordingAdapter()
    notification_service.register_adapter(Channel.EMAIL, recorder)
    notification_service.register_adapter(Channel.IN_APP, RecordingAdapter())

    outcome = await notification_service.notify(
        session,
        tenant_id=tenant.id,
        event_key="leave.approval.requested",
        recipient_user_ids=[hr_user.id],
        payload={"name": "Priya", "thing": "leave"},
    )

    # Default for approvals is immediate on email and in-app.
    assert outcome.sent == 2
    assert len(recorder.sent) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Channel isolation
# ═══════════════════════════════════════════════════════════════════════════


async def test_one_channel_failing_leaves_the_others_delivered(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate."""
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    await _template(session, tenant, "system.announcement", Channel.IN_APP)

    failing = AlwaysFailsAdapter()
    working = RecordingAdapter()
    notification_service.register_adapter(Channel.EMAIL, failing)
    notification_service.register_adapter(Channel.IN_APP, working)

    outcome = await notification_service.notify(
        session,
        tenant_id=tenant.id,
        event_key="system.announcement",
        recipient_user_ids=[hr_user.id],
        payload={"name": "Priya", "thing": "the office move"},
    )

    assert outcome.sent == 1, "in-app still went"
    assert outcome.failed == 1, "email did not"
    assert len(working.sent) == 1

    rows = (await session.scalars(select(Notification))).all()
    by_channel = {row.channel: row.status for row in rows}
    assert by_channel[Channel.IN_APP] in {
        NotificationStatus.SENT, NotificationStatus.DELIVERED
    }
    assert by_channel[Channel.EMAIL] is NotificationStatus.QUEUED  # awaiting retry


async def test_one_bad_recipient_does_not_lose_the_rest_of_a_bulk_send(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A 500-person announcement must not be lost to one bad address."""
    await _template(session, tenant, "system.announcement", Channel.EMAIL)

    good = [hr_user.id]
    for index in range(3):
        person = User(
            tenant_id=tenant.id,
            email=f"person{index}@acme.test",
            password_hash="!x",
            full_name=f"Person {index}",
            role=UserRole.EMPLOYEE,
        )
        session.add(person)
        await session.flush()
        good.append(person.id)

    # One user with an address the adapter will reject.
    broken = User(
        tenant_id=tenant.id, email="not-an-email", password_hash="!x",
        full_name="Broken Address", role=UserRole.EMPLOYEE,
    )
    session.add(broken)
    await session.commit()

    outcome = await notification_service.notify(
        session,
        tenant_id=tenant.id,
        event_key="system.announcement",
        recipient_user_ids=[*good, broken.id],
        payload={"name": "Everyone", "thing": "the office move"},
    )

    # Four emails and five in-app notices: the default for an announcement is both.
    assert outcome.sent == 9
    assert outcome.failed == 1

    failed = await session.scalar(
        select(Notification).where(Notification.recipient_user_id == broken.id)
    )
    assert failed.status is NotificationStatus.FAILED
    assert "Permanent" in failed.failure_reason


# ═══════════════════════════════════════════════════════════════════════════
# Retry, backoff and dead-lettering
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_transient_failure_schedules_a_retry(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    notification_service.register_adapter(Channel.EMAIL, AlwaysFailsAdapter())

    await notification_service.notify(
        session, tenant_id=tenant.id, event_key="system.announcement",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
    )

    row = await session.scalar(select(Notification))
    assert row.status is NotificationStatus.QUEUED
    assert row.attempt_count == 1
    assert row.next_attempt_at is not None
    assert "Transient" in row.failure_reason


async def test_a_permanent_failure_does_not_schedule_a_retry(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Retrying an unsubscribe is worse than not sending."""
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    notification_service.register_adapter(
        Channel.EMAIL, AlwaysFailsAdapter(permanent=True)
    )

    await notification_service.notify(
        session, tenant_id=tenant.id, event_key="system.announcement",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
    )

    row = await session.scalar(select(Notification))
    assert row.status is NotificationStatus.FAILED
    assert row.next_attempt_at is None


async def test_retries_are_bounded_and_dead_letter(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: bounded, then findable."""
    await _policy(session, tenant, max_attempts=3, quiet_hours_enabled=False)
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    adapter = AlwaysFailsAdapter()
    notification_service.register_adapter(Channel.EMAIL, adapter)

    await notification_service.notify(
        session, tenant_id=tenant.id, event_key="system.announcement",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
    )

    # Drive the retries.
    for _ in range(5):
        row = await session.scalar(select(Notification))
        if row.status is NotificationStatus.DEAD_LETTER:
            break
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        await notification_service.process_due_notifications(session, tenant.id)

    row = await session.scalar(select(Notification))
    assert row.status is NotificationStatus.DEAD_LETTER
    assert row.attempt_count == 3, "capped at max_attempts, not retried forever"
    assert row.next_attempt_at is None
    assert "Gave up after" in row.failure_reason


async def test_a_failed_notification_is_queryable_not_only_logged(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: dead-letter visibility."""
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    notification_service.register_adapter(
        Channel.EMAIL, AlwaysFailsAdapter(permanent=True)
    )

    await notification_service.notify(
        session, tenant_id=tenant.id, event_key="system.announcement",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
    )

    findable = (
        await session.scalars(
            select(Notification).where(
                Notification.status.in_(
                    [NotificationStatus.FAILED, NotificationStatus.DEAD_LETTER]
                )
            )
        )
    ).all()
    assert len(findable) == 1
    assert findable[0].failure_reason


# ═══════════════════════════════════════════════════════════════════════════
# Quiet hours and rate limiting
# ═══════════════════════════════════════════════════════════════════════════


async def test_quiet_hours_defer_non_urgent_and_pass_urgent(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate."""
    # A window covering every hour of the day, so the test does not depend on
    # when it runs.
    await _policy(
        session, tenant, quiet_hours_enabled=True,
        quiet_start=time(0, 0), quiet_end=time(23, 59),
    )
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    await _template(session, tenant, "payroll.run.failed", Channel.EMAIL)

    recorder = RecordingAdapter()
    notification_service.register_adapter(Channel.EMAIL, recorder)

    ordinary = await notification_service.notify(
        session, tenant_id=tenant.id, event_key="system.announcement",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
    )
    assert ordinary.deferred == 2, "both the email and the in-app notice wait"
    assert ordinary.sent == 0
    assert recorder.sent == []

    urgent = await notification_service.notify(
        session, tenant_id=tenant.id, event_key="payroll.run.failed",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
    )
    assert urgent.sent == 2, "a payroll failure does not wait until morning"
    assert len(recorder.sent) == 1


async def test_a_deferred_notification_records_when_it_will_send(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _policy(
        session, tenant, quiet_hours_enabled=True,
        quiet_start=time(0, 0), quiet_end=time(23, 59),
    )
    await _template(session, tenant, "system.announcement", Channel.EMAIL)

    await notification_service.notify(
        session, tenant_id=tenant.id, event_key="system.announcement",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
    )

    row = await session.scalar(select(Notification))
    assert row.status is NotificationStatus.DEFERRED
    assert row.next_attempt_at is not None
    assert row.next_attempt_at > datetime.now(UTC)


async def test_the_rate_cap_stops_a_bulk_operation_flooding_one_person(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate."""
    await _policy(session, tenant, rate_limit_per_hour=5, quiet_hours_enabled=False)
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    recorder = RecordingAdapter()
    notification_service.register_adapter(Channel.EMAIL, recorder)

    for index in range(20):
        await notification_service.notify(
            session, tenant_id=tenant.id, event_key="system.announcement",
            recipient_user_ids=[hr_user.id],
            payload={"name": "P", "thing": f"item {index}"},
        )

    assert len(recorder.sent) == 5, "capped, not 20"

    deferred = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.status == NotificationStatus.DEFERRED,
            Notification.channel == Channel.EMAIL,
        )
    )
    assert deferred == 15, "the rest are held, not lost"


async def test_urgent_bypasses_the_rate_cap(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _policy(session, tenant, rate_limit_per_hour=2, quiet_hours_enabled=False)
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    await _template(session, tenant, "payroll.run.failed", Channel.EMAIL)
    recorder = RecordingAdapter()
    notification_service.register_adapter(Channel.EMAIL, recorder)

    for _ in range(5):
        await notification_service.notify(
            session, tenant_id=tenant.id, event_key="system.announcement",
            recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
        )

    urgent = await notification_service.notify(
        session, tenant_id=tenant.id, event_key="payroll.run.failed",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "failure"},
    )
    assert urgent.sent == 2, "email and in-app, both past the cap"


# ═══════════════════════════════════════════════════════════════════════════
# WhatsApp gates — at enqueue, not at send
# ═══════════════════════════════════════════════════════════════════════════


async def _whatsapp_setup(
    session: AsyncSession, tenant: Tenant, status: WhatsAppTemplateStatus
) -> WhatsAppTemplate:
    meta = WhatsAppTemplate(
        tenant_id=tenant.id,
        meta_name="payslip_ready_v1",
        status=status,
        parameter_names=["name", "period"],
        rejection_reason="Contains promotional content" if status
        is WhatsAppTemplateStatus.REJECTED else None,
    )
    session.add(meta)
    await session.commit()
    await session.refresh(meta)
    return meta


async def test_whatsapp_without_recorded_consent_is_rejected_before_enqueue(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate.

    Rejected at enqueue means the caller sees it. Rejected at send means it is
    invisible until someone reads a log — and by then the number may be
    blocked.
    """
    meta = await _whatsapp_setup(session, tenant, WhatsAppTemplateStatus.APPROVED)
    await _template(
        session, tenant, "payroll.payslip.published", Channel.WHATSAPP,
        whatsapp_template_id=meta.id,
    )
    session.add(
        NotificationPreference(
            tenant_id=tenant.id, user_id=hr_user.id,
            event_key="payroll.payslip.published", channel=Channel.WHATSAPP,
            enabled=True, digest_mode=DigestMode.IMMEDIATE,
        )
    )
    await session.commit()

    outcome = await notification_service.notify(
        session, tenant_id=tenant.id, event_key="payroll.payslip.published",
        recipient_user_ids=[hr_user.id], payload={"name": "Priya", "period": "April"},
    )

    assert outcome.queued == 0, "nothing was enqueued"
    assert outcome.suppressed == 1
    assert any("not opted in" in e for e in outcome.errors)
    assert await session.scalar(select(func.count(Notification.id))) == 0


async def test_an_unapproved_whatsapp_template_fails_at_enqueue_with_a_named_error(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: the error names the template and its status."""
    meta = await _whatsapp_setup(session, tenant, WhatsAppTemplateStatus.REJECTED)
    await _template(
        session, tenant, "payroll.payslip.published", Channel.WHATSAPP,
        whatsapp_template_id=meta.id,
    )
    session.add_all(
        [
            NotificationPreference(
                tenant_id=tenant.id, user_id=hr_user.id,
                event_key="payroll.payslip.published", channel=Channel.WHATSAPP,
                enabled=True, digest_mode=DigestMode.IMMEDIATE,
            ),
            WhatsAppConsent(
                tenant_id=tenant.id, user_id=hr_user.id, phone_e164="+919000000000",
                opted_in=True, opted_in_at=datetime.now(UTC), source="ess_settings",
            ),
        ]
    )
    await session.commit()

    outcome = await notification_service.notify(
        session, tenant_id=tenant.id, event_key="payroll.payslip.published",
        recipient_user_ids=[hr_user.id], payload={"name": "Priya", "period": "April"},
    )

    assert outcome.queued == 0
    error = " ".join(outcome.errors)
    assert "payslip_ready_v1" in error
    assert "rejected" in error
    assert "Contains promotional content" in error


async def test_whatsapp_with_consent_and_an_approved_template_enqueues(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    meta = await _whatsapp_setup(session, tenant, WhatsAppTemplateStatus.APPROVED)
    await _template(
        session, tenant, "payroll.payslip.published", Channel.WHATSAPP,
        whatsapp_template_id=meta.id,
    )
    session.add_all(
        [
            NotificationPreference(
                tenant_id=tenant.id, user_id=hr_user.id,
                event_key="payroll.payslip.published", channel=Channel.WHATSAPP,
                enabled=True, digest_mode=DigestMode.IMMEDIATE,
            ),
            WhatsAppConsent(
                tenant_id=tenant.id, user_id=hr_user.id, phone_e164="+919000000000",
                opted_in=True, opted_in_at=datetime.now(UTC), source="ess_settings",
            ),
        ]
    )
    await session.commit()

    recorder = RecordingAdapter()
    notification_service.register_adapter(Channel.WHATSAPP, recorder)

    outcome = await notification_service.notify(
        session, tenant_id=tenant.id, event_key="payroll.payslip.published",
        recipient_user_ids=[hr_user.id], payload={"name": "Priya", "period": "April"},
    )

    assert outcome.sent == 1
    assert recorder.sent[0].whatsapp_template_name == "payslip_ready_v1"
    # Parameters are mapped onto Meta's positional slots in declared order.
    assert recorder.sent[0].whatsapp_parameters == ["Priya", "April"]


async def test_a_withdrawn_consent_blocks_further_whatsapp(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    meta = await _whatsapp_setup(session, tenant, WhatsAppTemplateStatus.APPROVED)
    await _template(
        session, tenant, "payroll.payslip.published", Channel.WHATSAPP,
        whatsapp_template_id=meta.id,
    )
    session.add_all(
        [
            NotificationPreference(
                tenant_id=tenant.id, user_id=hr_user.id,
                event_key="payroll.payslip.published", channel=Channel.WHATSAPP,
                enabled=True, digest_mode=DigestMode.IMMEDIATE,
            ),
            WhatsAppConsent(
                tenant_id=tenant.id, user_id=hr_user.id, phone_e164="+919000000000",
                opted_in=True, opted_in_at=datetime.now(UTC) - timedelta(days=30),
                opted_out_at=datetime.now(UTC), source="ess_settings",
            ),
        ]
    )
    await session.commit()

    outcome = await notification_service.notify(
        session, tenant_id=tenant.id, event_key="payroll.payslip.published",
        recipient_user_ids=[hr_user.id], payload={"name": "Priya", "period": "April"},
    )
    assert outcome.queued == 0
    assert any("not opted in" in e for e in outcome.errors)


# ═══════════════════════════════════════════════════════════════════════════
# Isolation
# ═══════════════════════════════════════════════════════════════════════════


async def test_notifications_are_tenant_scoped(
    session: AsyncSession, tenant: Tenant, other_tenant: Tenant,
    hr_user: User, other_hr_user: User,
) -> None:
    await _template(session, tenant, "system.announcement", Channel.EMAIL)
    notification_service.register_adapter(Channel.EMAIL, RecordingAdapter())

    await notification_service.notify(
        session, tenant_id=tenant.id, event_key="system.announcement",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
    )

    theirs = (
        await session.scalars(
            select(Notification).where(Notification.tenant_id == other_tenant.id)
        )
    ).all()
    assert theirs == []


async def test_a_missing_template_suppresses_that_channel_with_an_explanation(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """No template is a configuration gap, and it should say so.

    An event with neither a tenant template nor built-in wording — one a tenant
    added and never worded.
    """
    outcome = await notification_service.notify(
        session, tenant_id=tenant.id, event_key="custom.never_worded",
        recipient_user_ids=[hr_user.id], payload={"name": "P", "thing": "x"},
    )
    assert outcome.sent == 0
    assert any("No in_app template" in e for e in outcome.errors)


async def test_seeding_installs_the_default_templates(
    session: AsyncSession, tenant: Tenant
) -> None:
    created = await notification_service.seed_default_templates(session, tenant.id)
    assert len(created) >= 16  # 8 events x 2 channels

    # Idempotent — a second seed adds nothing.
    assert await notification_service.seed_default_templates(session, tenant.id) == []


async def test_push_goes_to_the_phone_the_app_registered_and_stops_when_gone(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    from app.models.notifications import PushDevice
    from app.services.notifications import admin

    await _template(session, tenant, "system.announcement", Channel.PUSH)
    session.add(
        NotificationPreference(
            tenant_id=tenant.id, user_id=hr_user.id, event_key="system.announcement",
            channel=Channel.PUSH, enabled=True, digest_mode=DigestMode.IMMEDIATE,
        )
    )
    await session.commit()
    with pytest.raises(HTTPException):
        await admin.register_device(session, hr_user, push_token="not-a-token", platform="expo")
    token = "ExponentPushToken[abc123]"
    await admin.register_device(session, hr_user, push_token=token, platform="expo")
    stored = await session.scalar(select(PushDevice))
    assert stored.push_token_hash != token, "the token is not stored in the clear"

    recorder = RecordingAdapter()
    notification_service.register_adapter(Channel.PUSH, recorder)
    outcome = await notification_service.notify(
        session, tenant_id=tenant.id, event_key="system.announcement",
        recipient_user_ids=[hr_user.id], payload={"name": "Priya", "thing": "the move"},
    )
    assert outcome.sent == 1 and recorder.sent[0].push_token == token

    class Gone(ChannelAdapter):
        async def send(self, message):
            raise PermanentFailure("DeviceNotRegistered: the app is gone from that phone")

    notification_service.register_adapter(Channel.PUSH, Gone())
    await notification_service.notify(
        session, tenant_id=tenant.id, event_key="system.announcement",
        recipient_user_ids=[hr_user.id], payload={"name": "Priya", "thing": "again"},
    )
    await session.refresh(stored)
    assert stored.revoked_at is not None, "a phone Expo calls gone is not pushed to again"
