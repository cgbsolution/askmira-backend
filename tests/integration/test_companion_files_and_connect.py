"""Ask Mira reading attached files, and offering to connect a product that is not ready.

What these hold:

* A LinkedIn screenshot is read by the workspace's model (a recorded transfer of
  what the person sent, nothing more), shown as a chip on their message, and
  given to the model fenced as data with the person it shows; ID numbers are
  left out first. The lead is then offered with what the file showed and what
  the person added, and only the confirmed offer reaches Saleinx. Later turns
  still know what the file said.
* Without AI nothing is sent anywhere: a PDF's own text is read here, a
  picture is not read, and the reply says why.
* Files are checked before anything runs: at most three, pictures and PDFs
  only, none empty; a message needs words or a file.
* An identity document is not used; a read that fails is recorded and said.
* A product that is not ready (not connected, switched off, not a member
  there) is listed for the model with every message, and connect_product shows
  the person what to do: Connect for an admin, who to ask for anyone else. In
  quick-lookup mode the same card answers.
* Connect started in a conversation comes back to it, and only to Ask Mira.
"""

from __future__ import annotations

import io
import json
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from reportlab.pdfgen import canvas
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.models.dpdp import CrossBorderTransfer
from app.models.grounding import AgentInvocation, InvocationOutcome
from app.models.product_link import ProductLink
from app.models.tenant import Tenant
from app.models.user import User
from app.services.ai import governance
from app.services.ai.client import AiUnavailableError, Completion, use_backend
from app.services.companion import connect_tools
from app.services.companion import service as companion
from app.services.products import connectors
from tests.integration.conftest import requires_db
from tests.integration.test_companion_flow import (  # noqa: F401  (fixtures)
    FakeModel,
    _bearer,
    _consent,
    client,
    me,
    plan,
)
from tests.integration.test_sales_procurement_link import (  # noqa: F401  (fixtures)
    SECRETS,
    FakeProducts,
    _link,
    fake,
    hr,
)

pytestmark = requires_db

PRIYA = {
    "kind": "linkedin_profile",
    "text": (
        "Priya Nair\nHead of Procurement at Kestrel Foods\nPune, Maharashtra, India\n"
        "500+ connections\nExperience\nHead of Procurement · Kestrel Foods\nPAN ABCDE1234F"
    ),
    "contact": {
        "name": "Priya Nair",
        "headline": "Head of Procurement at Kestrel Foods",
        "title": "Head of Procurement",
        "company": "Kestrel Foods",
        "location": "Pune, Maharashtra, India",
        "email": "",
        "phone": "",
        "website": "",
        "profile_url": "linkedin.com/in/priya-nair-kestrel",
    },
}


class SeeingModel(FakeModel):
    """FakeModel that can also read pictures: answers each with `reading` (or raises it)."""

    def __init__(self, *replies: Any, reading: Any = None) -> None:
        super().__init__(*replies)
        self.reading = PRIYA if reading is None else reading
        self.seen: list[dict[str, Any]] = []

    async def read_images(
        self,
        *,
        system: str,
        prompt: str,
        images: list[tuple[str, bytes]],
        schema: dict,
        max_tokens: int,
        effort: str,
    ) -> Completion:
        self.seen.append({"system": system, "prompt": prompt, "images": images})
        if isinstance(self.reading, Exception):
            raise self.reading
        return Completion(
            text=json.dumps(self.reading), model="claude-test", input_tokens=900, output_tokens=150
        )


def _png(size: tuple[int, int] = (900, 500)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (246, 248, 250)).save(out, "PNG")
    return out.getvalue()


def _text_pdf(*lines: str) -> bytes:
    out = io.BytesIO()
    pdf = canvas.Canvas(out)
    y = 780
    for line in lines:
        pdf.drawString(72, y, line)
        y -= 18
    pdf.showPage()
    pdf.save()
    return out.getvalue()


async def _send(
    session: AsyncSession,
    user: User,
    words: str,
    model: FakeModel,
    files: list[tuple[str, bytes]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    with use_backend(model):
        return await companion.send(
            session,
            user,
            companion.MessageIn(text=words, **extra),
            request=None,
            files=files,
        )


def _cards(result: dict[str, Any], index: int = 1) -> list[dict[str, Any]]:
    return result["messages"][index]["cards"]


async def _file_transfers(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(CrossBorderTransfer.id)).where(
                CrossBorderTransfer.description.like("%attached to a message%")
            )
        )
        or 0
    )


@pytest.fixture(autouse=True)
def _connect_can_be_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connect_tools, "can_encrypt", lambda: True)


# ── A LinkedIn screenshot becomes a lead ────────────────────────────────────


async def test_a_linkedin_screenshot_is_read_asked_about_and_offered_as_a_lead(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    await _link(session, tenant, "saleinx", {me.email: "member"})
    fake.role = "member"
    asked = (
        "I read Priya Nair, Head of Procurement at Kestrel Foods in Pune. Kestrel Foods isn't "
        "in Saleinx yet. What's her email or phone, what do they need, and when should we "
        "follow up? Or say add it as is."
    )
    model = SeeingModel(plan("sales_deals", asked, query="Kestrel Foods"))
    first = await _send(session, me, "add this lead", model, files=[("priya-linkedin.png", _png())])

    # Read once, by the workspace's model, as a picture the model is allowed to see.
    [read] = model.seen
    assert [media for media, _ in read["images"]] == ["image/jpeg"]
    assert "priya-linkedin.png" in read["prompt"]
    # The person sees what was read on their own message; the file itself is not kept.
    [chip] = _cards(first, 0)
    assert chip["type"] == "attachment" and chip["label"] == "LinkedIn profile"
    assert chip["read"] == "picture" and chip["note"] == "ID numbers in it were left out."
    # The model is given the person on the file, fenced as data, without the PAN.
    sent = model.calls[0]["user"]
    assert "Priya Nair" in sent and "Kestrel Foods" in sent and "linkedin.com/in/priya" in sent
    assert 'source=\\"attachment\\"' in sent
    assert "ABCDE1234F" not in model.everything_sent()
    assert first["messages"][1]["text"] == asked
    # The reading is a recorded transfer of what the person sent, and an invocation.
    assert await _file_transfers(session) == 1
    reading = await session.scalar(
        select(AgentInvocation).where(AgentInvocation.question.like("Read the attached file%"))
    )
    assert reading is not None and reading.outcome is InvocationOutcome.ANSWERED

    # She adds what the file did not show; Mira offers the lead; nothing is sent yet.
    lead = plan(
        "save_lead",
        "Here is the lead. Confirm and I'll add it.",
        company="Kestrel Foods",
        contact_name="Priya Nair",
        contact_title="Head of Procurement",
        contact_email="priya.nair@kestrel.example",
        source="LinkedIn",
        note="LinkedIn: linkedin.com/in/priya-nair-kestrel",
        offerings=["OutSystems"],
        description="A vendor portal for their suppliers.",
        # Models like to name the deal after the company and the person, and to write
        # follow-ups as "call her": neither reads well in Saleinx's own lists.
        deal_name="Kestrel Foods - Priya Nair",
        follow_up="Call her",
        follow_up_date="2026-11-12",
    )
    model = SeeingModel(lead)
    second = await _send(
        session,
        me,
        "email priya.nair@kestrel.example, they want a vendor portal on OutSystems",
        model,
        conversation_id=uuid.UUID(first["conversation"]["id"]),
    )
    # The next turn still knows what the file said (from the saved message).
    assert "Head of Procurement at Kestrel Foods" in model.calls[0]["user"]
    assert model.seen == []  # the file is not read again
    [offer] = [c for c in _cards(second) if c["type"] == "proposal"]
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Company"] == "Kestrel Foods" and figures["Contact"] == "Priya Nair"
    assert figures["Source"] == "LinkedIn"
    assert fake.acts("saleinx") == []

    await companion.confirm_offer(session, me, uuid.UUID(offer["action_id"]), request=None)
    [saved] = fake.acts("saleinx", "lead.save")
    params = saved["params"]
    assert params["company"] == "Kestrel Foods"
    assert params["contact"] == {
        "first_name": "Priya",
        "last_name": "Nair",
        "title": "Head of Procurement",
        "email": "priya.nair@kestrel.example",
    }
    assert params["deal"]["source"] == "LinkedIn"
    assert "linkedin.com/in/priya-nair-kestrel" in params["note"]
    assert params["deal"]["name"] == "OutSystems"  # Saleinx shows "Kestrel Foods - OutSystems"
    assert params["follow_up"]["title"] == "Call her (Priya Nair)"


async def test_a_file_alone_with_no_words_is_still_a_message(
    session: AsyncSession, tenant: Tenant, me: User, fake: FakeProducts
) -> None:
    model = SeeingModel(plan(reply="This is Priya Nair's profile. Shall I add her as a lead?"))
    result = await _send(session, me, "", model, files=[("profile.png", _png())])
    assert result["messages"][0]["text"] == ""
    assert result["conversation"]["title"] == "File: profile.png"
    assert "only the attached file" in model.calls[0]["user"]


# ── Without AI, nothing leaves ──────────────────────────────────────────────


async def test_without_ai_a_pdf_is_read_here_and_a_picture_is_not_read(
    session: AsyncSession, me: User, hr_user: User
) -> None:
    await governance.set_settings(
        session, hr_user, governance.AiSettings(enabled=False), request=None
    )
    model = SeeingModel()
    rfq = _text_pdf("Request for quotation", "Kestrel Foods Pvt Ltd, Pune", "Need: 40 laptops")
    result = await _send(session, me, "", model, files=[("rfq.pdf", rfq), ("card.png", _png())])
    pdf, picture = _cards(result, 0)
    assert pdf["read"] == "text" and pdf["label"] == "Document" and pdf["note"] is None
    assert picture["read"] == "none" and "needs AI" in picture["note"]
    [notice] = _cards(result)
    assert notice["type"] == "notice" and "needs AI" in notice["text"]
    assert model.seen == [] and model.calls == []
    assert await _file_transfers(session) == 0


# ── Files are checked first ─────────────────────────────────────────────────


async def test_files_are_checked_before_anything_runs(session: AsyncSession, me: User) -> None:
    model = SeeingModel()
    cases = [
        ([("a.png", _png())] * 4, 422, "up to 3 files"),
        ([("notes.txt", b"just some words, not a picture")], 415, "PDF, PNG image or JPEG"),
        ([("empty.png", b"")], 422, "is empty"),
        ([], 422, "Write a message"),
    ]
    for files, code, words in cases:
        with pytest.raises(HTTPException) as refused:
            await _send(session, me, "", model, files=files)
        assert refused.value.status_code == code, (files, refused.value.detail)
        assert words in str(refused.value.detail)
    with pytest.raises(HTTPException) as refused:
        await _send(
            session,
            me,
            "Anita",
            model,
            files=[("a.png", _png())],
            pick=companion.PickIn(tool="search_people", inputs={"query": "x"}),
        )
    assert refused.value.status_code == 422
    assert model.seen == [] and model.calls == []
    # Nothing was saved for any of them.
    assert await companion.list_conversations(session, me) == []


async def test_an_identity_document_is_not_used_and_a_failed_read_is_said(
    session: AsyncSession, me: User
) -> None:
    aadhaar = {"kind": "identity_document", "text": "Government of India 2345 6789 0123"}
    model = SeeingModel(plan(reply="I can't use that one."), reading=aadhaar)
    result = await _send(session, me, "is this ok", model, files=[("id.jpg", _png())])
    [chip] = _cards(result, 0)
    assert chip["read"] == "none" and "identity document" in chip["note"]
    assert "Government of India" not in model.everything_sent()

    model = SeeingModel(plan(reply="I couldn't read it."), reading=AiUnavailableError("busy"))
    result = await _send(session, me, "and this", model, files=[("x.png", _png())])
    [chip] = _cards(result, 0)
    assert chip["read"] == "none" and chip["note"] == "It could not be read: busy"
    withheld = await session.scalar(
        select(func.count(AgentInvocation.id)).where(
            AgentInvocation.outcome == InvocationOutcome.WITHHELD
        )
    )
    assert withheld == 1


async def test_files_over_http(
    client: httpx.AsyncClient,
    me: User,
) -> None:
    model = SeeingModel(plan(reply="That's Priya Nair from Kestrel Foods."))
    with use_backend(model):
        sent = await client.post(
            "/api/v1/companion/messages/files",
            data={"text": "who is this?", "client_ref": "files-0001-abcdef"},
            files=[("files", ("priya.png", _png(), "image/png"))],
            headers=_bearer(me),
        )
        assert sent.status_code == 200, sent.text
        mine, reply = sent.json()["messages"]
        assert mine["cards"][0]["label"] == "LinkedIn profile"
        assert reply["text"] == "That's Priya Nair from Kestrel Foods."
        again = await client.post(
            "/api/v1/companion/messages/files",
            data={"text": "who is this?", "client_ref": "files-0001-abcdef"},
            files=[("files", ("priya.png", _png(), "image/png"))],
            headers=_bearer(me),
        )
        assert again.json()["repeat"] is True and len(model.seen) == 1
        many = await client.post(
            "/api/v1/companion/messages/files",
            files=[("files", (f"{n}.png", _png(), "image/png")) for n in range(4)],
            headers=_bearer(me),
        )
        assert many.status_code == 422


# ── A product that is not ready ─────────────────────────────────────────────


async def test_a_product_not_connected_is_told_to_the_model_and_offered_to_connect(
    session: AsyncSession,
    me: User,
    hr: User,
    hr_user: User,
    fake: FakeProducts,
) -> None:
    said = "Saleinx isn't connected yet, so I can't add the lead. I've kept Priya's details."
    model = SeeingModel(plan("connect_product", said, product="saleinx"))
    result = await _send(session, me, "add this lead", model, files=[("p.png", _png())])
    first = model.calls[0]
    assert "connect_product" in first["tools"] and "save_lead" not in first["tools"]
    assert "Products not ready for this person: " in first["user"]
    assert "Saleinx (leads, deals and the sales pipeline): not connected" in first["user"]
    [card] = [c for c in _cards(result) if c["type"] == "connect"]
    assert card["state"] == "not_connected" and card["action"] is None
    assert hr_user.full_name in card["text"]  # an employee is told which admin to ask

    model = SeeingModel(plan("connect_product", said, product="saleinx"))
    result = await _send(session, hr, "add a lead for Kestrel Foods", model)
    [card] = [c for c in _cards(result) if c["type"] == "connect"]
    assert card["action"] == "connect" and "come straight back to this chat" in card["text"]
    assert fake.calls == []


async def test_connected_but_not_a_member_switched_off_and_ready(
    session: AsyncSession, tenant: Tenant, me: User, hr: User, fake: FakeProducts
) -> None:
    await _link(session, tenant, "saleinx", {})
    [card], _ = await connect_tools.connect_card(session, hr, "saleinx")
    assert card["state"] == "not_member" and card["action"] == "sync"
    assert hr.email in card["text"]
    [card], _ = await connect_tools.connect_card(session, me, "saleinx")
    assert card["state"] == "not_member" and card["action"] is None

    row = await session.scalar(select(ProductLink).where(ProductLink.product == "saleinx"))
    row.status = "disabled"
    await session.commit()
    [card], _ = await connect_tools.connect_card(session, hr, "saleinx")
    assert card["state"] == "switched_off" and card["action"] == "connect"

    [old], _ = await connect_tools.connect_card(session, me, "saleinx")
    row.status, row.member_roles = "active", {me.email: "member"}
    await session.commit()
    [card], _ = await connect_tools.connect_card(session, me, "saleinx")
    assert card["state"] == "ready"
    assert "saleinx" not in await connect_tools.not_ready(session, me)
    # A card saved before reads as connected now, with nothing left to press.
    [fresh] = await connect_tools.refresh_cards(session, me, [old])
    assert fresh["state"] == "ready" and fresh["action"] is None
    assert fresh["title"] == "Saleinx is connected"


async def test_quick_lookups_answer_with_the_same_card(
    session: AsyncSession, me: User, hr_user: User, fake: FakeProducts
) -> None:
    await governance.set_settings(
        session, hr_user, governance.AiSettings(enabled=False), request=None
    )
    result = await _send(session, me, "show my deals", SeeingModel())
    reply = result["messages"][1]
    [card] = reply["cards"]
    assert card["type"] == "connect" and card["product"] == "saleinx"
    assert reply["text"] == ""  # the card says it all


# ── Connect from a conversation comes back to it ────────────────────────────


async def test_connect_from_ask_mira_comes_back_to_the_conversation(
    session: AsyncSession, hr: User, fake: FakeProducts, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connectors, "can_encrypt", lambda: True)
    monkeypatch.setattr(crypto, "can_encrypt", lambda: True)
    back = f"/ask-mira?c={uuid.uuid4()}"
    started = await connectors.start(session, hr, "saleinx", back=back)
    state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]
    assert json.loads(crypto.decrypt_field(state) or "")["b"] == back

    for wrong in (
        "https://evil.example/ask-mira",
        "//evil.example",
        "/ask-mira/../settings",
        "/ask-mira?c=not-a-uuid",
        "/settings/connectors?next=https://evil.example",
    ):
        with pytest.raises(HTTPException) as refused:
            await connectors.start(session, hr, "saleinx", back=wrong)
        assert refused.value.status_code == 422, wrong

    async def token(api_url: str, product: str, **_: Any) -> dict[str, Any]:
        return {"secret": SECRETS["saleinx"], "organization": "Acme Sales", "external_ref": "o1"}

    monkeypatch.setattr(connectors.client, "connect_token", token)
    done = await connectors.complete(
        session, hr, "saleinx", code="c" * 24, state=state, request=None
    )
    assert done["connected"] is True and done["back"] == back

    # Pressed on Connectors itself: back to Connectors, as before.
    plain = await connectors.start(session, hr, "procurement")
    state = parse_qs(urlparse(plain["authorize_url"]).query)["state"][0]
    assert "b" not in json.loads(crypto.decrypt_field(state) or "")
