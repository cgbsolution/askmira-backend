"""The showcase story: the order-to-day-one flow a scanned QR code opens.

Pure: no database. Every figure the story states is checked against arithmetic
done here, so a demo at an exhibition never shows a number that does not add up.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.services.showcase import story

TODAY = date(2026, 11, 13)


def _scenario(**overrides: Any) -> story.Scenario:
    values: dict[str, Any] = {
        "company": "CGB Solutions",
        "client": "Acme Technologies",
        "role": "Java Developer",
        "headcount": 6,
        "months": 12,
        "order_value": Decimal("5000000"),
        "team_budget": Decimal("3000000"),
        "laptop_price": Decimal("55000"),
        "payment_days": 30,
    }
    values.update(overrides)
    return story.Scenario(**values)


def _play(state: dict[str, Any], *, until: str | None = None, steps: int = 60) -> dict[str, Any]:
    """Take the booth screen's own next step until the story ends (or a chapter is reached)."""
    for _ in range(steps):
        if until and until in state["done"]:
            return state
        step = story.view(state)["autoplay"]
        if step is None:
            return state
        if "say" in step:
            story.say(state, step["say"])
        else:
            story.decide(state, step["decide"], step["confirm"])
    raise AssertionError("the story did not finish")


def _cards(state: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [c for m in state["messages"] for c in m["cards"] if c["type"] == kind]


def _last(state: dict[str, Any]) -> dict[str, Any]:
    return state["messages"][-1]


def _walk(value: Any) -> None:
    assert not isinstance(value, float), f"a float in the story's state: {value!r}"
    if isinstance(value, dict):
        for item in value.values():
            _walk(item)
    elif isinstance(value, list):
        for item in value:
            _walk(item)


# ── The whole story ──────────────────────────────────────────────────────────


def test_the_whole_story_runs_from_signature_to_cash() -> None:
    state = _play(story.start(_scenario(), today=TODAY))
    view = story.view(state)
    assert view["finished"]
    assert [c["state"] for c in view["chapters"]] == ["done"] * len(story.CHAPTERS)
    funnel = {f["key"]: f["count"] for f in view["board"]["funnel"]}
    assert funnel == {
        "matched": 20,
        "invited": 20,
        "interviewed": 17,
        "shortlisted": 8,
        "offered": 6,
        "accepted": 6,
        "hired": 6,
    }
    # Every system the brochure names took part.
    seen = {s for m in state["messages"] for s in m["systems"]}
    assert {
        "saleinx",
        "hrms",
        "anytimehire",
        "procureflow",
        "helpdesk",
        "expensegenie",
        "accounts",
    } <= seen


def test_the_figures_add_up_to_the_rupee() -> None:
    state = _play(story.start(_scenario(), today=TODAY))
    board = story.view(state)["board"]
    # Team after the negotiation: 4.4 + 4.8 + 5.0 + 5.0 + 5.0 (capped) + 5.8 (Rohan) lakh.
    assert board["budget"]["committed"] == "3000000.00"
    assert board["margin"] == {"floor": "40.0", "now": "40.0", "basis": "offers"}
    pnl = board["pnl"]
    travel = (
        Decimal("9850") + Decimal("6400") + Decimal("10120") + Decimal("6400") + Decimal("2250")
    )
    assert pnl["laptops"] == "330000.00"
    assert Decimal(pnl["travel"]) == travel
    net = Decimal("5000000") - Decimal("3000000") - Decimal("330000") - travel
    assert Decimal(pnl["net"]) == net
    cash = board["cash"]
    # Month one pays a month's salary, every laptop and the travel before any money comes in.
    assert Decimal(cash["lowest"]) == -(Decimal("250000") + Decimal("330000") + travel)
    assert cash["lowest_at"] == "Dec 2026"
    assert cash["positive_from"] == "Apr 2027"
    assert Decimal(cash["final"]) == net
    assert len(cash["months"]) == 13  # twelve months of work, and the last month's payment
    assert sum(Decimal(m["inflow"]) for m in cash["months"]) == Decimal("5000000")


def test_the_state_is_plain_json_with_no_floats() -> None:
    state = _play(story.start(_scenario(), today=TODAY))
    _walk(state)
    _walk(story.view(state))
    assert json.loads(json.dumps(state)) == state


def test_joining_is_the_monday_three_weeks_out() -> None:
    state = story.start(_scenario(), today=TODAY)  # a Friday
    assert state["joining"] == "2026-12-07"
    assert date.fromisoformat(state["joining"]).weekday() == 0


# ── Offers and the budget ────────────────────────────────────────────────────


def _to_counter() -> dict[str, Any]:
    state = _play(story.start(_scenario(), today=TODAY), until="offer")
    assert state["counter"] is not None
    return state


def test_offers_fit_the_budget_with_some_kept_back() -> None:
    state = _play(story.start(_scenario(), today=TODAY), until="shortlist")
    story.say(state, "Make offers within the budget.")
    offer = _cards(state, "proposal")[-1]
    figures = {f["label"]: f["value"] for f in offer["figures"]}
    assert figures["Team, a year"] == "₹29,20,000"
    assert figures["Kept for negotiation"] == "₹80,000"
    assert figures["Margin"] == "41.6%"
    offered = [row[2] for row in offer["table"]["rows"]]
    assert max(offered) == "₹5,00,000"  # nobody above the ceiling


def test_a_counter_that_breaks_the_margin_floor_is_refused() -> None:
    state = _to_counter()
    counter = _cards(state, "record")[-1]
    figures = {f["label"]: f["value"] for f in counter["figures"]}
    assert figures == {
        "Asked": "₹6,20,000",
        "Offered": "₹5,00,000",
        "Most within budget": "₹5,80,000",
        "Team at the ask": "₹30,40,000",
        "Margin at the ask": "39.2%",
        "Floor": "40.0%",
    }
    story.say(state, "Agree to 6.2 lakh")
    assert "can't send ₹6.2 L" in _last(state)["text"]
    assert state["counter"] is not None
    assert state["offers"][str(2)] == "500000.00"  # Rohan's offer unchanged


def test_the_most_within_budget_is_accepted_and_lands_on_the_floor() -> None:
    state = _to_counter()
    story.say(state, "Offer Rohan ₹5,80,000")
    assert state["counter"] is None
    assert "negotiate" in state["done"]
    assert story.view(state)["board"]["margin"]["now"] == "40.0"


def test_holding_firm_hands_the_seat_to_the_back_up() -> None:
    state = _to_counter()
    story.say(state, "Hold at 5 lakh.")
    assert "Priya Nair" in _last(state)["text"]
    names = {story._POOL[int(i)].name for i in state["offers"]}
    assert "Rohan Mehta" not in names and "Priya Nair" in names
    assert story.view(state)["board"]["margin"]["now"] == "42.2"


def test_an_offer_below_the_midpoint_is_declined() -> None:
    state = _to_counter()
    story.say(state, "offer rohan 5.2 lakh")
    assert "declined" in _last(state)["text"]
    assert "Rohan Mehta" not in {story._POOL[int(i)].name for i in state["offers"]}


def test_raising_the_budget_needs_approval_then_the_ask_goes_through() -> None:
    state = _to_counter()
    story.say(state, "Raise the budget to 30.4 lakh")
    proposal = _cards(state, "proposal")[-1]
    assert proposal["kind"] == "budget" and proposal["status"] == "proposed"
    assert state["budget"] is None  # nothing changes before the yes
    story.say(state, "yes")
    assert state["budget"] == "3040000.00"
    story.say(state, "agree")
    board = story.view(state)["board"]
    assert board["margin"]["now"] == "39.2"
    assert board["budget"]["raised"] is True
    assert state["offers"]["2"] == "620000.00"


def test_a_budget_at_or_above_the_order_is_refused() -> None:
    state = _to_counter()
    story.say(state, "raise the budget to 50 lakh")
    assert "no margin" in _last(state)["text"]
    assert state["pending"] is None


# ── Talking to Miraa ─────────────────────────────────────────────────────────


def test_steps_out_of_order_are_explained_not_run() -> None:
    state = story.start(_scenario(), today=TODAY)
    story.say(state, "How did the interviews go?")
    assert _last(state)["text"].startswith("Not yet")
    assert state["done"] == []


def test_a_cancelled_requisition_can_be_asked_for_again() -> None:
    state = story.start(_scenario(), today=TODAY)
    story.say(state, "Acme signed. Book the order.")
    story.say(state, "cancel")
    assert state["pending"] is None and "job" not in state["done"]
    story.say(state, "Approve the requisition")
    assert state["pending"]["kind"] == "requisition"
    story.say(state, "yes")
    assert "job" in state["done"]


def test_a_waiting_offer_must_be_answered_first() -> None:
    state = story.start(_scenario(), today=TODAY)
    story.say(state, "Book the order")
    story.say(state, "Find matches in the résumé database")
    assert "waiting" in _last(state)["text"]
    assert "match" not in state["done"]


def test_a_card_button_for_an_old_offer_is_refused() -> None:
    state = story.start(_scenario(), today=TODAY)
    story.say(state, "Book the order")
    action = state["pending"]["id"]
    story.decide(state, action, True)
    with pytest.raises(story.StoryError):
        story.decide(state, action, True)


def test_empty_and_long_messages_are_refused() -> None:
    state = story.start(_scenario(), today=TODAY)
    with pytest.raises(story.StoryError):
        story.say(state, "   ")
    with pytest.raises(story.StoryError):
        story.say(state, "x" * 501)


def test_off_script_questions_point_to_the_next_step() -> None:
    state = story.start(_scenario(), today=TODAY)
    story.say(state, "What's the weather in Pune?")
    assert "can't look that up here" in _last(state)["text"]
    assert "Book the order" in _last(state)["text"]


def test_dropping_a_name_changes_the_shortlist() -> None:
    state = _play(story.start(_scenario(), today=TODAY), until="shortlist")
    story.say(state, "drop Rohan")
    names = [story._POOL[i].name for i in state["shortlist"]]
    assert "Rohan Mehta" not in names and len(names) == 7


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("offer 5.8 lakh", Decimal("580000")),
        ("₹5,80,000", Decimal("580000")),
        ("580000", Decimal("580000")),
        ("5.8L", Decimal("580000")),
        ("Rs 1.2 crore", Decimal("12000000")),
        ("580k", Decimal("580000")),
        ("shortlist the top 8", None),
        ("no amount here", None),
    ],
)
def test_amounts_are_read_as_people_write_them(text: str, amount: Decimal | None) -> None:
    assert story._amount(text) == amount


# ── AI scores, other scenarios ───────────────────────────────────────────────


def test_every_ai_score_shows_its_signals_and_confidence() -> None:
    state = _play(story.start(_scenario(), today=TODAY), until="interview")
    ranking = next(c for c in _cards(state, "table") if c["title"] == "AI interview ranking")
    columns = ranking["columns"]
    assert {"Score", "Confidence", "Strongest", "Weakest"} <= set(columns)
    for row in ranking["rows"]:
        values = dict(zip(columns, row, strict=True))
        assert values["Confidence"].startswith(("High (", "Medium ("))
        assert any(ch.isdigit() for ch in values["Strongest"])
    notices = [c["text"] for c in _cards(state, "notice")]
    assert any("advice for HR, never a decision" in text for text in notices)


@pytest.mark.parametrize(
    ("headcount", "months", "budget", "order"),
    [(4, 6, "1200000", "2000000"), (10, 24, "9000000", "15000000"), (1, 3, "150000", "300000")],
)
def test_other_orders_run_to_the_end_inside_their_budget(
    headcount: int, months: int, budget: str, order: str
) -> None:
    sc = _scenario(
        headcount=headcount,
        months=months,
        team_budget=Decimal(budget),
        order_value=Decimal(order),
        role="Python Developer",
    )
    state = _play(story.start(sc, today=TODAY))
    board = story.view(state)["board"]
    assert story.view(state)["finished"]
    assert Decimal(board["budget"]["committed"]) <= Decimal(budget)
    assert Decimal(board["margin"]["now"]) >= sc.margin_floor.quantize(Decimal("0.1")) - Decimal(
        "0.05"
    )
    assert {f["key"]: f["count"] for f in board["funnel"]}["hired"] == headcount
    job = next(c for c in _cards(state, "record") if c["title"].endswith("job description"))
    assert any("Python 3" in f["value"] for f in job["figures"])


def test_a_team_over_ten_is_refused() -> None:
    with pytest.raises(story.StoryError):
        story.start(_scenario(headcount=11), today=TODAY)


def test_lakh_and_rupee_formats() -> None:
    assert story.lakh(Decimal("580000")) == "₹5.8 L"
    assert story.lakh(Decimal("12500000")) == "₹1.25 Cr"
    assert story.lakh(Decimal("35020")) == "₹35,020"
    assert story.lakh(Decimal("-615020")) == "-₹6.15 L"
    assert story.inr(Decimal("3000000")) == "₹30,00,000"
