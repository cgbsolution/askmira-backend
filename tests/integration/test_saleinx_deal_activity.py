"""Ask Miraa logs a call or meeting in a Saleinx deal's Activity.

The user's report (2026-11-12): "update under safeops360 deal for megafine the
following under activity - schedule a call on 23rd nov" got "I don't have a
tool to write to a separate activity field in Saleinx", and the call went into
a note instead. What these hold:

* The words become one offer of a call in the deal's Activity, found from the
  person's own way of naming it ("SafeOps360 deal for Megafine" finds
  "Megafine Pharma - Safeops"); nothing reaches Saleinx until they confirm.
* Confirmed, it goes to Saleinx as `deal.activity` (its "Log activity"), never
  as a note; a planned call also carries a call task due that day.
* One that happened (no date) is logged for today with no task.
* A viewer, a missing call/meeting, a date that is not one, a date years away
  and a deal nobody can see are answered here, and nothing is sent.
* A reminder task Saleinx refuses is reported, without saying the call failed.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.user import User
from app.services.attendance import service as attendance
from app.services.companion import service as companion
from tests.integration.conftest import requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    me,
    plan,
)
from tests.integration.test_sales_procurement_link import (  # noqa: F401  (fixtures)
    FakeProducts,
    _cards,
    _deal,
    _link,
    _offer,
    _say,
    fake,
)

pytestmark = requires_db

MEGAFINE_ID = "66666666-6666-4666-8666-666666666666"


def _saleinx_logs_activity(fake: FakeProducts, *, task_error: str | None = None) -> None:
    """Teach the fake Saleinx `deal.activity`, answering as the real one does."""
    plain = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        answer = plain(request)
        if request.url.host != "saleinx.test" or answer.status_code != 422:
            return answer
        body = json.loads(request.content)
        params = body.get("params") or {}
        deal = next((d for d in fake.deals if d["id"] == params.get("deal_id")), None)
        if body.get("kind") != "deal.activity" or deal is None:
            return answer
        task = params.get("task") or {}
        made = bool(task.get("title")) and task_error is None
        return httpx.Response(
            200,
            json={
                "kind": "deal.activity",
                "deal": deal,
                "done": [f"{params['type']} logged in the deal's Activity"],
                "task_id": "t1" if made else None,
                "task_error": task_error if task.get("title") else None,
            },
        )

    fake.handler = handler  # type: ignore[method-assign]


async def _today(session: AsyncSession, user: User) -> date:
    return await attendance.local_today(session, user)


async def _megafine(
    session: AsyncSession,
    tenant: Tenant,
    me: User,
    fake: FakeProducts,
    *,
    role: str = "member",
    task_error: str | None = None,
) -> None:
    await _link(session, tenant, "saleinx", {me.email: role})
    fake.role = role
    fake.deals = [_deal(MEGAFINE_ID, "Megafine Pharma", "Safeops", "qualified")]
    _saleinx_logs_activity(fake, task_error=task_error)


async def test_schedule_a_call_goes_in_the_deals_activity_not_a_note(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    await _megafine(session, tenant, me, fake)
    ahead = await _today(session, me) + timedelta(days=10)
    call = plan(
        "log_deal_activity",
        "This logs the call in the deal's Activity; confirm to add it.",
        deal="SafeOps360 deal for Megafine",
        activity_type="call",
        activity_date=ahead.isoformat(),
        activity_notes="Schedule a call",
    )
    words = (
        "update under safeops360 deal for megafine the following under activity - schedule a call"
    )
    offer = _offer(await _say(session, me, words, FakeModel(call)))
    assert offer["kind"] == "saleinx_activity" and offer["status"] == "proposed"
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Deal"] == "Megafine Pharma - Safeops"
    assert figures["Activity"] == "Call (planned)"
    assert figures["Date"] == f"{ahead:%a %d %b %Y}"
    assert figures["Notes"] == "Schedule a call"
    assert "Reminder" in figures
    assert not fake.acts("saleinx")  # nothing changed before the confirm

    done = await companion.confirm_offer(session, me, uuid.UUID(offer["action_id"]), request=None)
    [sent] = fake.acts("saleinx", "deal.activity")
    assert sent["params"] == {
        "deal_id": MEGAFINE_ID,
        "type": "call",
        "date": ahead.isoformat(),
        "notes": "Schedule a call",
        "task": {"title": "Call: Megafine Pharma - Safeops", "due_date": ahead.isoformat()},
    }
    assert sent["actor"]["email"] == me.email
    assert not fake.acts("saleinx", "deal.note") and not fake.acts("saleinx", "lead.save")
    text = done["message"]["text"]
    assert "Activity of Megafine Pharma - Safeops" in text and "task" in text


async def test_a_meeting_that_happened_is_logged_for_today_with_no_task(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    await _megafine(session, tenant, me, fake)
    today = await _today(session, me)
    met = plan(
        "log_deal_activity",
        "Confirm?",
        deal="Megafine Pharma",
        activity_type="meeting",
        activity_date="",
        activity_notes="Demo done, they want pricing for 3 plants",
    )
    offer = _offer(await _say(session, me, "Met Megafine today, log it", FakeModel(met)))
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Activity"] == "Meeting" and figures["Date"] == f"{today:%a %d %b %Y}"
    assert "Reminder" not in figures
    await companion.confirm_offer(session, me, uuid.UUID(offer["action_id"]), request=None)
    [sent] = fake.acts("saleinx", "deal.activity")
    assert sent["params"]["date"] == today.isoformat() and "task" not in sent["params"]


async def test_what_cannot_be_logged_is_answered_here_and_nothing_is_sent(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    await _megafine(session, tenant, me, fake)
    asks = [
        ({"deal": "Megafine", "activity_type": "none"}, "call or a meeting"),
        ({"deal": "", "activity_type": "call"}, "Which deal"),
        (
            {"deal": "Megafine", "activity_type": "call", "activity_date": "23rd Nov"},
            "YYYY-MM-DD",
        ),
        (
            {"deal": "Megafine", "activity_type": "call", "activity_date": "2062-11-23"},
            "more than a year away",
        ),
        ({"deal": "Nobody Industries", "activity_type": "call"}, "No lead or deal"),
    ]
    for inputs, expected in asks:
        result = await _say(
            session, me, "log a call", FakeModel(plan("log_deal_activity", "Hmm.", **inputs))
        )
        texts = [c.get("text", "") for c in _cards(result)]
        assert any(expected in t for t in texts), (inputs, texts)
    assert not fake.acts("saleinx")


async def test_a_viewer_is_told_they_only_read(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    await _megafine(session, tenant, me, fake, role="viewer")
    call = plan("log_deal_activity", "No.", deal="Megafine", activity_type="call")
    [card] = _cards(await _say(session, me, "log a call on megafine", FakeModel(call)))
    assert "view-only access in Saleinx" in card["text"]
    assert fake.calls == []


async def test_a_refused_reminder_is_said_without_calling_the_log_a_failure(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    await _megafine(session, tenant, me, fake, task_error="Tasks are switched off.")
    ahead = await _today(session, me) + timedelta(days=3)
    call = plan(
        "log_deal_activity",
        "Confirm?",
        deal="Megafine",
        activity_type="call",
        activity_date=ahead.isoformat(),
    )
    offer = _offer(await _say(session, me, "schedule a call with megafine", FakeModel(call)))
    done = await companion.confirm_offer(session, me, uuid.UUID(offer["action_id"]), request=None)
    text = done["message"]["text"]
    assert text.startswith("Logged in Saleinx") and "Tasks are switched off." in text
