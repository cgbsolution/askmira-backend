"""Saleinx asks which company or contact a new lead means; the offer's card answers it.

The user's report (2026-11-21): "New lead in Saleinx: L&T Technology" came back
"Not done: Saleinx could not do that: Companies like "L&T Technology" are
already in Saleinx: Speridian Technology, Yash Technologies, Dixon
Technologies. Use one of them, or create a new company?" Saleinx was asking
(HTTP 409 with `needs_choice`, its docs/askmira/lead-save-choices.md), and
AskMiraa took the question for a refusal, with no way to answer it. (The three
names matching at all was Saleinx's matcher scoring the shared word
"technology"; fixed there.) What these hold:

* A question leaves the offer open, showing the question and its options on
  the card; nothing is created in Saleinx and nothing is marked failed.
* The answer goes back with the same params plus `company_choice` /
  `contact_choice`; an earlier answer is kept when the next question comes.
* "Create new" is refused where Saleinx says it can't be (a contact with the
  same email), and so is an id that was not one of the options.
* A press with no answer while the question waits is refused, and so is an
  answer on an offer that asks nothing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.user import User
from app.services.companion import service as companion
from tests.integration.conftest import requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    me,
    plan,
)
from tests.integration.test_sales_procurement_link import (  # noqa: F401  (fixtures)
    FakeProducts,
    _deal,
    _link,
    _offer,
    _say,
    fake,
)

pytestmark = requires_db

SPERIDIAN = "11111111-1111-4111-8111-111111111111"
YASH = "22222222-2222-4222-8222-222222222222"
JIKESH = "33333333-3333-4333-8333-333333333333"


def _saleinx_asks(fake: FakeProducts, *, contact_question: bool = False) -> None:
    """Teach the fake Saleinx lead.save's questions, worded as the real one words them."""
    plain = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        answer = plain(request)  # checks the signature and records the call
        if request.url.host != "saleinx.test":
            return answer
        body = json.loads(request.content)
        params = body.get("params") or {}
        if body.get("kind") != "lead.save" or not request.url.path.endswith("/act"):
            return answer
        company = params.get("company_choice")
        if company is None:
            message = (
                f'Companies like "{params["company"]}" are already in Saleinx: '
                "Speridian Technology, Yash Technologies. Use one of them, or create a new company?"
            )
            choice = {
                "field": "company",
                "message": message,
                "options": [
                    {"id": SPERIDIAN, "label": "Speridian Technology", "detail": "similar name"},
                    {"id": YASH, "label": "Yash Technologies", "detail": "similar name"},
                ],
                "can_create_new": True,
                "answer_with": "company_choice",
            }
            return httpx.Response(409, json={"error": message, "needs_choice": choice})
        if contact_question and company != "new" and params.get("contact_choice") is None:
            message = (
                "Jikesh Krishna (jikesh@example.com) is already in Saleinx with this email. "
                "Use Jikesh Krishna for this lead?"
            )
            choice = {
                "field": "contact",
                "message": message,
                "options": [
                    {"id": JIKESH, "label": "Jikesh Krishna", "detail": "jikesh@example.com"}
                ],
                "can_create_new": False,
                "answer_with": "contact_choice",
            }
            return httpx.Response(409, json={"error": message, "needs_choice": choice})
        made = _deal(str(uuid.uuid4()), params["company"], params["deal"]["name"], "lead")
        return httpx.Response(
            200, json={"kind": "lead.save", "deal": made, "done": ["lead added at Lead"]}
        )

    fake.handler = handler  # type: ignore[method-assign]


async def _lead_offer(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts, **asks: bool
) -> dict[str, Any]:
    await _link(session, tenant, "saleinx", {me.email: "member"})
    fake.role = "member"
    _saleinx_asks(fake, **asks)
    lead = plan(
        "save_lead",
        "Here is the lead; confirm to add it.",
        company="L&T Technology",
        contact_name="Jikesh Krishna",
        contact_email="jikesh@example.com",
        deal_name="Recruitment services",
        offerings=["OutSystems"],
        follow_up="Follow up on company profile shared",
        follow_up_date="2026-11-23",
    )
    return _offer(await _say(session, me, "New lead: L&T Technology, Jikesh", FakeModel(lead)))


async def _confirm(
    session: AsyncSession, me: User, offer: dict[str, Any], choice: str | None = None
) -> dict[str, Any]:
    return await companion.confirm_offer(
        session, me, uuid.UUID(offer["action_id"]), request=None, choice=choice
    )


async def test_a_question_keeps_the_offer_open_and_creating_new_adds_the_lead(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    offer = await _lead_offer(session, tenant, me, fake)

    asked = await _confirm(session, me, offer)
    card = asked["action"]
    assert card["status"] == "proposed"  # not "failed": nothing was refused
    assert card["choice"]["field"] == "company" and card["choice"]["can_create_new"] is True
    assert [o["label"] for o in card["choice"]["options"]] == [
        "Speridian Technology",
        "Yash Technologies",
    ]
    assert "Pick one on the card" in asked["message"]["text"]
    assert "could not be done" not in asked["message"]["text"]

    done = await _confirm(session, me, offer, "new")
    assert done["action"]["status"] == "done" and "choice" not in done["action"]
    first, second = fake.acts("saleinx", "lead.save")
    assert "company_choice" not in first["params"]
    assert second["params"]["company_choice"] == "new"
    # The same lead as before, only with the answer added.
    assert {k: v for k, v in second["params"].items() if k != "company_choice"} == first["params"]


async def test_each_answer_is_kept_for_the_next_question(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    offer = await _lead_offer(session, tenant, me, fake, contact_question=True)
    await _confirm(session, me, offer)

    contact = await _confirm(session, me, offer, YASH)
    card = contact["action"]
    assert card["status"] == "proposed" and card["choice"]["field"] == "contact"
    assert card["choice"]["can_create_new"] is False

    # Saleinx can't make a second contact with one email: "new" is refused here.
    with pytest.raises(HTTPException) as refused:
        await _confirm(session, me, offer, "new")
    assert refused.value.status_code == 422

    done = await _confirm(session, me, offer, JIKESH)
    assert done["action"]["status"] == "done"
    last = fake.acts("saleinx", "lead.save")[-1]["params"]
    assert last["company_choice"] == YASH and last["contact_choice"] == JIKESH


async def test_answers_that_are_not_on_the_card_are_refused(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    offer = await _lead_offer(session, tenant, me, fake)
    await _confirm(session, me, offer)
    calls = len(fake.acts("saleinx", "lead.save"))

    with pytest.raises(HTTPException) as made_up:
        await _confirm(session, me, offer, str(uuid.uuid4()))
    assert made_up.value.status_code == 422

    # Confirm with no answer while the question waits: asked to pick on the card.
    with pytest.raises(HTTPException) as unanswered:
        await _confirm(session, me, offer)
    assert unanswered.value.status_code == 409
    assert "buttons on the card" in str(unanswered.value.detail)
    assert len(fake.acts("saleinx", "lead.save")) == calls  # nothing more was sent

    await _confirm(session, me, offer, SPERIDIAN)
    assert fake.acts("saleinx", "lead.save")[-1]["params"]["company_choice"] == SPERIDIAN


async def test_an_answer_on_an_offer_that_asks_nothing_is_refused(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    offer = await _lead_offer(session, tenant, me, fake)
    with pytest.raises(HTTPException) as refused:
        await _confirm(session, me, offer, "new")
    assert refused.value.status_code == 409
    assert not fake.acts("saleinx", "lead.save")
