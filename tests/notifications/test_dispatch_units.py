"""Slice 05: pieces of the pipeline that need no database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

import pytest

from app.models.notifications import Channel, DigestMode, NotificationPolicy
from app.services.notifications.adapters import (
    ConsoleEmailAdapter,
    InAppAdapter,
    NullPushAdapter,
    OutboundMessage,
    PermanentFailure,
    TransientFailure,
    WhatsAppCloudAdapter,
)
from app.services.notifications.dispatch import (
    DEFAULT_PREFERENCES,
    URGENT_EVENT_KEYS,
    _backoff,
    _in_quiet_hours,
    _next_window_open,
    effective_channels,
    render,
)


def policy(**overrides) -> NotificationPolicy:
    base = {
        "tenant_id": uuid.uuid4(),
        "quiet_hours_enabled": True,
        "quiet_start": time(21, 0),
        "quiet_end": time(8, 0),
        "timezone": "Asia/Kolkata",
        "rate_limit_per_hour": 20,
        "max_attempts": 5,
    }
    base.update(overrides)
    return NotificationPolicy(**base)


def ist(hour: int, minute: int = 0) -> datetime:
    """A UTC instant corresponding to that IST wall-clock time."""
    from zoneinfo import ZoneInfo

    local = datetime(2026, 4, 6, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata"))
    return local.astimezone(UTC)


# ═══════════════════════════════════════════════════════════════════════════
# Quiet hours — the wrap-around case is the one that breaks
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "hour,expected",
    [
        (22, True),   # inside, before midnight
        (23, True),
        (2, True),    # inside, after midnight
        (7, True),    # inside, just before the window closes
        (8, False),   # exactly at the boundary — the window is open
        (12, False),
        (20, False),  # just before it opens
        (21, True),   # exactly at the start
    ],
)
def test_quiet_hours_wrap_around_midnight(hour: int, expected: bool) -> None:
    """21:00 to 08:00 wraps. A naive start <= now <= end gets this backwards."""
    assert _in_quiet_hours(policy(), ist(hour)) is expected


def test_a_non_wrapping_window_also_works() -> None:
    daytime = policy(quiet_start=time(12, 0), quiet_end=time(14, 0))
    assert _in_quiet_hours(daytime, ist(13)) is True
    assert _in_quiet_hours(daytime, ist(15)) is False


def test_quiet_hours_can_be_switched_off() -> None:
    assert _in_quiet_hours(policy(quiet_hours_enabled=False), ist(2)) is False


def test_an_identical_start_and_end_means_no_quiet_period() -> None:
    """Rather than a 24-hour blackout, which is how that gets misconfigured."""
    assert _in_quiet_hours(policy(quiet_start=time(9), quiet_end=time(9)), ist(9)) is False


def test_the_deferred_time_is_when_the_window_next_opens() -> None:
    from zoneinfo import ZoneInfo

    reopens = _next_window_open(policy(), ist(23)).astimezone(ZoneInfo("Asia/Kolkata"))
    assert (reopens.hour, reopens.minute) == (8, 0)
    assert reopens.day == 7, "23:00 on the 6th defers to 08:00 on the 7th"


def test_a_message_at_two_am_defers_to_the_same_morning() -> None:
    from zoneinfo import ZoneInfo

    reopens = _next_window_open(policy(), ist(2)).astimezone(ZoneInfo("Asia/Kolkata"))
    assert reopens.day == 6
    assert reopens.hour == 8


# ═══════════════════════════════════════════════════════════════════════════
# Backoff
# ═══════════════════════════════════════════════════════════════════════════


def test_backoff_is_exponential() -> None:
    minutes = [_backoff(n).total_seconds() / 60 for n in range(1, 6)]
    assert minutes == [1, 2, 4, 8, 16]


def test_backoff_is_capped() -> None:
    """An uncapped backoff pushes the next attempt days out, by which point
    the notification is not worth sending."""
    assert _backoff(20).total_seconds() / 60 == 60


# ═══════════════════════════════════════════════════════════════════════════
# Rendering
# ═══════════════════════════════════════════════════════════════════════════


def test_placeholders_are_substituted() -> None:
    assert render("Hello {name}, you have {count} items", {"name": "Anita", "count": 3}) == (
        "Hello Anita, you have 3 items"
    )


def test_an_unknown_placeholder_is_left_alone_rather_than_raising() -> None:
    """A notification that arrives slightly wrong beats one that never arrives."""
    assert render("Hello {name}, {missing}", {"name": "Anita"}) == "Hello Anita, {missing}"


def test_the_same_placeholder_twice_is_substituted_twice() -> None:
    assert render("{a} and {a}", {"a": "x"}) == "x and x"


# ═══════════════════════════════════════════════════════════════════════════
# Defaults
# ═══════════════════════════════════════════════════════════════════════════


def test_approvals_are_immediate_and_social_is_digested() -> None:
    """The checklist gate: the shipped default set is reviewed.

    Approvals reach someone who is blocked waiting. Social traffic is pleasant
    but not urgent, and an immediate shoutout notification is how a channel
    gets muted permanently.
    """
    approvals = DEFAULT_PREFERENCES["leave.approval.requested"]
    assert approvals[Channel.EMAIL] is DigestMode.IMMEDIATE
    assert approvals[Channel.IN_APP] is DigestMode.IMMEDIATE

    shoutout_posted = DEFAULT_PREFERENCES["culture.shoutout.posted"]
    assert shoutout_posted[Channel.IN_APP] is DigestMode.DAILY_DIGEST
    assert shoutout_posted[Channel.EMAIL] is DigestMode.OFF

    payslip = DEFAULT_PREFERENCES["payroll.payslip.published"]
    assert payslip[Channel.EMAIL] is DigestMode.IMMEDIATE


def test_the_urgent_set_is_short_and_only_contains_real_emergencies() -> None:
    """The value of an urgent channel is that it is rare."""
    assert len(URGENT_EVENT_KEYS) <= 8
    assert "payroll.run.failed" in URGENT_EVENT_KEYS
    assert "security.break_glass_used" in URGENT_EVENT_KEYS
    assert "culture.shoutout.received" not in URGENT_EVENT_KEYS
    assert "culture.shoutout.posted" not in URGENT_EVENT_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Adapters
# ═══════════════════════════════════════════════════════════════════════════


def message(**overrides) -> OutboundMessage:
    base = {
        "recipient_name": "Anita Sharma",
        "recipient_email": "anita@acme.test",
        "recipient_phone": "+919000000000",
        "subject": "Hello",
        "body": "Body text",
        "event_key": "system.announcement",
    }
    base.update(overrides)
    return OutboundMessage(**base)


async def test_the_email_adapter_sends_and_returns_a_message_id() -> None:
    adapter = ConsoleEmailAdapter()
    result = await adapter.send(message())

    assert result.provider_message_id
    assert len(adapter.sent) == 1


@pytest.mark.parametrize("bad", ["", "not-an-email", "@nodomain.test", "   "])
async def test_a_malformed_address_is_a_permanent_failure(bad: str) -> None:
    """Permanent, not transient: retrying a bad address burns rate limit."""
    adapter = ConsoleEmailAdapter()
    with pytest.raises(PermanentFailure):
        await adapter.send(message(recipient_email=bad))


async def test_the_in_app_adapter_reports_delivered_immediately() -> None:
    """The row is the message, so there is nothing in flight."""
    result = await InAppAdapter().send(message())
    assert result.delivered is True


async def test_push_without_a_device_token_fails_permanently() -> None:
    """A push nobody could receive must not be recorded as delivered."""
    with pytest.raises(PermanentFailure, match="no registered device token"):
        await NullPushAdapter().send(message())


async def test_push_with_a_token_succeeds() -> None:
    adapter = NullPushAdapter()
    result = await adapter.send(message(push_token="device-abc"))
    assert result.provider_message_id


def test_whatsapp_refuses_to_construct_without_credentials() -> None:
    with pytest.raises(PermanentFailure, match="phone number id"):
        WhatsAppCloudAdapter(phone_number_id="", access_token="")


async def test_whatsapp_refuses_a_message_with_no_template() -> None:
    """A business-initiated message must name an approved Meta template."""
    adapter = WhatsAppCloudAdapter(phone_number_id="123", access_token="tok")
    with pytest.raises(PermanentFailure, match="approved template"):
        await adapter.send(message(whatsapp_template_name=None))


async def test_whatsapp_refuses_a_message_with_no_phone_number() -> None:
    adapter = WhatsAppCloudAdapter(phone_number_id="123", access_token="tok")
    with pytest.raises(PermanentFailure, match="no phone number"):
        await adapter.send(
            message(recipient_phone=None, whatsapp_template_name="payslip_ready")
        )


def test_the_permanent_whatsapp_error_codes_are_the_ones_that_never_succeed() -> None:
    """Classifying these as transient would retry forever against Meta."""
    codes = WhatsAppCloudAdapter.PERMANENT_CODES
    assert 131_047 in codes, "outside the 24-hour window — a retry cannot fix it"
    assert 132_001 in codes, "template does not exist"
    assert 132_000 in codes, "parameter count mismatch"


def test_transient_and_permanent_are_distinct_types() -> None:
    """The distinction is the point of having two exception classes."""
    assert not issubclass(TransientFailure, PermanentFailure)
    assert not issubclass(PermanentFailure, TransientFailure)


async def test_the_expo_adapter_classifies_what_expo_says(monkeypatch) -> None:
    import httpx

    from app.services.notifications.adapters import (
        ExpoPushAdapter,
        OutboundMessage,
        PermanentFailure,
        TransientFailure,
    )

    replies = []

    def handler(request: httpx.Request) -> httpx.Response:
        return replies.pop(0)

    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    message = OutboundMessage(
        recipient_name="A", recipient_email=None, recipient_phone=None, subject="Hi",
        body="Body", event_key="x", push_token="ExponentPushToken[abc]",
    )
    adapter = ExpoPushAdapter()

    replies.append(httpx.Response(200, json={"data": {"status": "ok", "id": "t-1"}}))
    assert (await adapter.send(message)).provider_message_id == "t-1"

    replies.append(
        httpx.Response(
            200,
            json={"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}},
        )
    )
    with pytest.raises(PermanentFailure):
        await adapter.send(message)

    replies.append(httpx.Response(503))
    with pytest.raises(TransientFailure):
        await adapter.send(message)

    with pytest.raises(PermanentFailure):
        await adapter.send(
            OutboundMessage(
                recipient_name="A", recipient_email=None, recipient_phone=None, subject=None,
                body="b", event_key="x",
            )
        )


# ── Effective channels ──────────────────────────────────────────────────────


def test_a_saved_choice_overrides_only_its_own_channel() -> None:
    """Email off for leave approvals must leave in-app and push as they were."""
    channels = effective_channels(
        "leave.approval.requested", [(Channel.EMAIL, False, DigestMode.IMMEDIATE)]
    )
    assert channels[Channel.EMAIL] is DigestMode.OFF
    assert channels[Channel.IN_APP] is DigestMode.IMMEDIATE
    assert channels[Channel.PUSH] is DigestMode.IMMEDIATE


def test_a_channel_the_default_lacks_can_be_switched_on() -> None:
    channels = effective_channels(
        "leave.approval.decided", [(Channel.PUSH, True, DigestMode.IMMEDIATE)]
    )
    assert channels[Channel.PUSH] is DigestMode.IMMEDIATE
    assert channels[Channel.EMAIL] is DigestMode.IMMEDIATE


def test_nothing_saved_means_the_shipped_default() -> None:
    assert effective_channels("leave.approval.decided", []) == DEFAULT_PREFERENCES[
        "leave.approval.decided"
    ]
    assert effective_channels("some.unknown.event", []) == {Channel.IN_APP: DigestMode.IMMEDIATE}
