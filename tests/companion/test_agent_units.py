"""Ask Mira's agent parts that need no database: what the model may read, the
typed-yes rule, the conversation replay, and the P&L arithmetic."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.accounts import pnl
from app.services.companion import actions, agent, views
from app.services.companion.config import CompanionConfig
from app.services.companion.tools import TOOLS, coerce, tool_schema

# ── What the model reads ────────────────────────────────────────────────────


def test_contact_details_and_identifiers_are_scrubbed() -> None:
    text = views.scrub(
        "Mail anita.sharma@acme.co.in or call +91 98765 43210 / 9876543210, Aadhaar "
        "1234 5678 9012, PAN ABCDE1234F. Cost ₹1,25,000 and 6123456789.00 stay."
    )
    assert "acme.co.in" not in text and "98765" not in text and "9876543210" not in text
    assert "1234 5678 9012" not in text and "ABCDE1234F" not in text
    assert "₹1,25,000" in text and "6123456789.00" in text


def test_reason_contact_and_id_columns_are_dropped_and_refs_kept() -> None:
    card = {
        "type": "table",
        "title": "Leave",
        "caption": "today",
        "columns": ["Name", "Work email", "Reason", "From", "PAN", "Mobile"],
        "rows": [["Anita", "anita@acme.test", "Hospital visit", "2026-11-05", "X", "9"]],
        "refs": ["leave:1"],
    }
    seen = views.view_of([card], allowed=True)
    assert "Anita" in seen and "2026-11-05" in seen and "leave:1" in seen
    assert "Hospital" not in seen and "anita@acme.test" not in seen
    assert "Work email" not in seen and "Mobile" not in seen


def test_a_hidden_result_names_only_the_offer() -> None:
    offer = {"type": "proposal", "action_id": "abc", "title": "Leave request", "figures": []}
    seen = views.view_of([offer, {"type": "notice", "text": "x"}], allowed=False)
    assert "abc" in seen and "Leave request" not in seen
    assert views.view_of([{"type": "notice", "text": "secret"}], allowed=False) == views.HIDDEN


def test_a_long_result_is_cut_and_says_so() -> None:
    rows = [[f"Person {i}", "x" * 300] for i in range(200)]
    card = {"type": "table", "title": "T", "caption": "", "columns": ["Name", "Note"], "rows": rows}
    seen = views.view_of([card], allowed=True)
    assert len(seen) <= views.MAX_VIEW_CHARS + 80 and "cut here" in seen


def test_the_register_decides_what_the_model_reads() -> None:
    on = CompanionConfig()
    assert agent.readable(TOOLS["headcount_trend"], on)
    assert agent.readable(TOOLS["policy_question"], on)
    # Pay, leave and names are not categories the model provider is registered for.
    assert not agent.readable(TOOLS["payroll_trend"], on)
    assert not agent.readable(TOOLS["my_record"], on)
    off = CompanionConfig(ai_reads_results=False)
    assert not agent.readable(TOOLS["headcount_trend"], off)


# ── Tools as the model sees them ────────────────────────────────────────────


def test_each_tool_shows_only_its_own_inputs() -> None:
    schema = tool_schema(TOOLS["apply_leave"])
    assert schema["name"] == "apply_leave"
    assert set(schema["input_schema"]["properties"]) == {
        "leave_type",
        "from",
        "to",
        "half_day",
        "half_day_session",
        "reason",
    }
    # An id that only a pick from a list may carry is never the model's to fill.
    assert "employee_id" not in tool_schema(TOOLS["employee_record"])["input_schema"]["properties"]


def test_the_models_inputs_are_kept_to_type_and_choice() -> None:
    inputs = coerce(
        TOOLS["apply_leave"],
        {"leave_type": " CL ", "half_day": "true", "half_day_session": "evening", "extra": "x"},
    )
    assert inputs["leave_type"] == "CL" and inputs["half_day"] is True
    assert inputs["half_day_session"] == "none" and "extra" not in inputs
    assert inputs["from"] == "" and inputs["reason"] == ""


def test_every_action_is_an_offer_needing_ai() -> None:
    for key in ("apply_leave", "cancel_leave", "request_attendance_change", "decide_approvals"):
        assert TOOLS[key].kind == "action" and TOOLS[key].needs_ai


# ── A yes is a yes ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "Yes please",
        "ok",
        "okay go ahead",
        "haan",
        "haan ji",
        "sure",
        "confirm",
        "do it",
        "send it",
        "approve them",
        "theek hai",
        "yes, send it!",
    ],
)
def test_a_plain_yes(text: str) -> None:
    assert actions.is_affirmative(text)


@pytest.mark.parametrize(
    "text",
    [
        "no",
        "not yet",
        "wait",
        "yes but change it to the 6th",
        "don't",
        "cancel it",
        "what is my balance?",
        "",
        "ok " + "and also " * 20,
        "nahi",
    ],
)
def test_anything_else_is_not(text: str) -> None:
    assert not actions.is_affirmative(text)


# ── The conversation replayed ───────────────────────────────────────────────


def _row(role: str, text: str = "", transcript=None, steps=None, tool=None):
    return SimpleNamespace(role=role, text=text, transcript=transcript, steps=steps, tool=tool)


def test_the_replay_has_no_reasoning_and_alternates() -> None:
    reply = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "my_record", "input": {}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
    ]
    rows = [
        _row("assistant", "orphan"),  # a reply with no question before it is dropped
        _row("user", "hi"),
        _row("assistant", "Hello", transcript=reply),
        _row("user", "again"),  # no reply saved (a failed turn)
        _row("user", "and again"),
        _row("assistant", "Old reply", tool="who_is_out"),
    ]
    out = agent.history_messages(rows)
    assert [m["role"] for m in out] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert "thinking" not in str(out)
    assert out[-1]["content"][0]["text"].startswith("Old reply [I used who_is_out")
    # Two questions in a row become one message.
    assert len(out[4]["content"]) == 2


def test_only_the_last_ten_turns_are_replayed() -> None:
    rows = []
    for n in range(15):
        rows += [_row("user", f"q{n}"), _row("assistant", f"a{n}")]
    out = agent.history_messages(rows)
    assert len(out) == 20 and "q5" in str(out[0]) and "q4" not in str(out)


def test_stored_blocks_keep_text_and_tool_calls_only() -> None:
    stored = agent._storable(
        [
            {"type": "thinking", "thinking": "", "signature": "s"},
            {"type": "text", "text": "  "},
            {"type": "tool_use", "id": "a", "name": "x", "input": {"q": 1}},
        ],
        keep_calls=True,
    )
    assert stored["content"] == [{"type": "tool_use", "id": "a", "name": "x", "input": {"q": 1}}]
    closed = agent._storable(
        [{"type": "tool_use", "id": "a", "name": "x", "input": {}}], keep_calls=False
    )
    assert closed["content"] == [{"type": "text", "text": "…"}]


# ── The P&L arithmetic ──────────────────────────────────────────────────────


def test_a_month_is_shared_by_the_days_each_mapping_covers() -> None:
    june = [(Decimal(100), date(2026, 6, 11), None)]
    assert pnl.share_of_month(june, 2026, 6) == Decimal(20) / Decimal(30)
    split = [
        (Decimal(100), date(2026, 1, 1), date(2026, 6, 10)),
        (Decimal(50), date(2026, 6, 11), None),
    ]
    expected = Decimal(10) / 30 + Decimal("0.5") * 20 / 30
    assert abs(pnl.share_of_month(split, 2026, 6) - expected) < Decimal("1e-20")
    assert pnl.share_of_month(split, 2026, 7) == Decimal("0.5")
    assert pnl.share_of_month([(Decimal(100), date(2026, 8, 1), None)], 2026, 7) == 0


def test_months_and_margins() -> None:
    assert pnl.months_between((2026, 11), (2027, 2)) == [
        (2026, 11),
        (2026, 12),
        (2027, 1),
        (2027, 2),
    ]
    with pytest.raises(ValueError):
        pnl.months_between((2026, 5), (2026, 4))
    with pytest.raises(ValueError):
        pnl.months_between((2020, 1), (2026, 1))
    assert pnl.margin_pct(Decimal(200000), Decimal(110000)) == Decimal("55.0")
    assert pnl.margin_pct(Decimal(0), Decimal(-5)) is None
    assert pnl.rupees(Decimal("10.005")) == Decimal("10.01")
