"""Ask Mira's rule-based planner: the words a person types, matched to one tool."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.companion import direct
from app.services.companion.tools import TOOLS

TODAY = date(2026, 10, 21)  # a Wednesday
#: Everything direct mode can offer: the tools that need no model.
ALL = [tool for tool in TOOLS.values() if not tool.feature]


def _plan(text: str, tools=ALL) -> direct.DirectPlan:
    return direct.plan(text, tools, today=TODAY)


@pytest.mark.parametrize(
    ("text", "tool", "inputs"),
    [
        ("Who is on leave this week?", "who_is_out", {"from": "2026-10-19", "to": "2026-10-25"}),
        ("who's out tomorrow", "who_is_out", {"from": "2026-10-22", "to": "2026-10-22"}),
        ("How many leaves do I have left?", "my_record", {"lookup": "leave_balance"}),
        ("show my latest payslip", "my_record", {"lookup": "latest_payslip"}),
        ("Who is in the finance team?", "search_people", {"query": "finance"}),
        ("Rohan Verma", "search_people", {"query": "rohan verma"}),
        ("attendance last month", "attendance_summary", {"from": "2026-09-01", "to": "2026-09-30"}),
        ("List departments", "list_departments", {}),
        ("Show open jobs", "hiring_jobs", {"status": "open", "query": ""}),
        ("open jobs in AnytimeHire", "hiring_jobs", {"status": "open", "query": ""}),
        ("Open AnytimeHire", "open_product", {"product": "interview"}),
        ('candidates for "Python developer"', "hiring_candidates", {"query": "python developer"}),
        ("Candidates offered", "hiring_candidates", {"status": "offered"}),
        ("interviews next 7 days", "hiring_interviews", {"from": "2026-10-21", "to": "2026-10-27"}),
        ("hiring overview", "hiring_overview", {}),
        ("My pending expense claims", "expense_claims", {"scope": "mine", "status": "pending"}),
        ("claims waiting for my approval", "expense_claims", {"scope": "approvals"}),
        (
            "company expenses last month",
            "expense_claims",
            {"scope": "company", "from": "2026-09-01"},
        ),
        ("Expense total this month", "expense_summary", {"scope": "mine", "to": "2026-10-31"}),
    ],
)
def test_words_are_matched_to_a_tool(text: str, tool: str, inputs: dict) -> None:
    plan = _plan(text)
    assert plan.tool == tool
    assert inputs.items() <= plan.inputs.items()
    assert plan.reply


def test_only_an_offered_tool_is_ever_chosen() -> None:
    without_hiring = [tool for tool in ALL if tool.group != "hiring_data"]
    plan = _plan("Show open jobs", without_hiring)
    assert plan.tool is None
    assert "AnytimeHire is not available to you" in plan.reply
    without_expenses = [tool for tool in ALL if tool.group != "expense_data"]
    assert _plan("my expense claims", without_expenses).tool is None


def test_policy_and_report_questions_wait_for_ai() -> None:
    for text in ("What is the leave policy?", "How many people joined this year?"):
        plan = _plan(text)
        assert plan.tool is None and "answered by AI" in plan.reply


def test_a_greeting_or_anything_unmatched_says_what_can_be_asked() -> None:
    hello = _plan("hello")
    assert hello.tool is None and "Who is on leave this week?" in hello.reply
    other = _plan("what is the weather like in delhi today")
    assert other.tool is None and other.reply.startswith("I could not match that")
    assert _plan("hi", []).reply.endswith("No lookups are switched on for you yet.")
