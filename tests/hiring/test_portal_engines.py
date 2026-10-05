"""Pure engines behind the hiring web portal: pipeline, approvers, throttle, text."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.hiring import approvers, pipeline, plain_text, public_guard
from app.services.recruiting import offer_outcomes

# ── Pipeline ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        ("applied", "screening"),
        ("screening", "ai_interview"),
        ("screening", "human_interview"),
        ("ai_interview", "human_interview"),
    ],
)
def test_forward_moves_are_allowed_without_a_reason(src, dst) -> None:
    move = pipeline.check_transition(src, dst, reason=None)
    assert move.forward is True
    assert move.reason_required is False


@pytest.mark.parametrize("dst", ["offer", "hired"])
def test_offer_and_hired_cannot_be_reached_by_hand(dst) -> None:
    with pytest.raises(pipeline.PipelineError, match="Offers"):
        pipeline.check_transition("human_interview", dst, reason="x")


def test_a_candidate_at_offer_is_moved_through_the_offer() -> None:
    with pytest.raises(pipeline.PipelineError, match="offer controls"):
        pipeline.check_transition("offer", "rejected", reason="x")


def test_skipping_backwards_is_refused() -> None:
    with pytest.raises(pipeline.PipelineError, match="cannot move"):
        pipeline.check_transition("human_interview", "applied", reason="x")


@pytest.mark.parametrize("dst", ["rejected", "withdrawn"])
def test_closing_needs_a_reason(dst) -> None:
    with pytest.raises(pipeline.PipelineError, match="needs a reason"):
        pipeline.check_transition("screening", dst, reason="  ")
    move = pipeline.check_transition("screening", dst, reason="Not enough experience")
    assert move.reason_required


def test_reopening_needs_a_reason_and_goes_to_screening() -> None:
    with pytest.raises(pipeline.PipelineError):
        pipeline.check_transition("rejected", "screening", reason="")
    assert pipeline.allowed_targets("rejected") == ["screening"]
    reopened = pipeline.check_transition("rejected", "screening", reason="New role fits")
    assert reopened.forward is False


def test_the_same_stage_is_not_a_move() -> None:
    with pytest.raises(pipeline.PipelineError, match="already"):
        pipeline.check_transition("screening", "screening", reason=None)


def test_candidate_facing_status_is_coarse() -> None:
    assert pipeline.application_status_for("ai_interview") == "interviewing"
    assert pipeline.application_status_for("human_interview") == "interviewing"
    assert pipeline.application_status_for("rejected") == "closed"
    assert pipeline.application_status_for("hired") is None


# ── Approvers ───────────────────────────────────────────────────────────────


def _can(step, role, *, reports=False, hm=False, originator=False):
    return approvers.can_decide_step(
        approver_role=step,
        user_role=role,
        has_reports=reports,
        is_hiring_manager=hm,
        is_originator=originator,
    )


@pytest.mark.parametrize("role", ["admin", "sub_admin", "employee", "super_admin"])
def test_nobody_approves_what_they_raised(role) -> None:
    result = _can("hiring_manager", role, reports=True, hm=True, originator=True)
    assert result.allowed is False
    assert "You raised this" in result.message


def test_the_hiring_manager_step_is_the_hiring_manager_or_hr() -> None:
    assert _can("hiring_manager", "employee", reports=True, hm=True).allowed
    assert not _can("hiring_manager", "employee", reports=True, hm=False).allowed
    assert _can("hiring_manager", "sub_admin").allowed


def test_a_manager_step_is_anybody_with_reports_or_hr() -> None:
    """Managing people is a place in the org chart, not a role."""
    assert _can("manager", "employee", reports=True).allowed
    assert not _can("manager", "employee").allowed
    assert _can("manager", "sub_admin").allowed


def test_an_admin_step_needs_an_admin() -> None:
    assert _can("admin", "admin").allowed
    assert not _can("admin", "sub_admin").allowed
    assert not _can("admin", "employee", reports=True).allowed


def test_a_role_nobody_holds_is_signed_by_an_admin_on_its_behalf() -> None:
    result = _can("finance_head", "admin")
    assert result.allowed and result.on_behalf_of == "finance_head"
    assert not _can("finance_head", "sub_admin").allowed
    assert not _can("finance_head", "employee", reports=True).allowed


# ── Throttle and honeypot ───────────────────────────────────────────────────


def test_the_limiter_refuses_past_the_limit_and_recovers() -> None:
    limiter = public_guard.RateLimiter()
    for i in range(3):
        assert limiter.hit("k", limit=3, window_seconds=60, now=100.0 + i) == 0
    wait = limiter.hit("k", limit=3, window_seconds=60, now=103.0)
    assert wait > 0
    assert limiter.hit("k", limit=3, window_seconds=60, now=161.0) == 0


def test_the_limiter_keeps_keys_apart_and_bounded() -> None:
    limiter = public_guard.RateLimiter(max_keys=2)
    limiter.hit("a", limit=1, window_seconds=60, now=0)
    assert limiter.hit("b", limit=1, window_seconds=60, now=0) == 0
    limiter.hit("c", limit=1, window_seconds=60, now=0)
    # "a" was evicted, so it starts again rather than growing memory forever.
    assert limiter.hit("a", limit=1, window_seconds=60, now=1) == 0


def test_the_honeypot_refuses_anything_filled_in() -> None:
    public_guard.refuse_honeypot(None)
    public_guard.refuse_honeypot("   ")
    with pytest.raises(HTTPException) as exc:
        public_guard.refuse_honeypot("http://spam.example")
    assert exc.value.status_code == 400


# ── Plain text ──────────────────────────────────────────────────────────────


def test_html_becomes_paragraphs_with_no_markup_left() -> None:
    html = (
        "<h2>About</h2><p>We build <b>payroll</b>.</p><script>alert(1)</script>"
        "<ul><li>Remote</li><li>Bengaluru</li></ul><style>p{}</style>"
        '<img src=x onerror="alert(2)">&lt;tag&gt;'
    )
    paragraphs = plain_text.html_to_paragraphs(html)
    assert paragraphs == ["About", "We build payroll.", "• Remote", "• Bengaluru", "<tag>"]
    joined = " ".join(paragraphs)
    assert "alert" not in joined and "onerror" not in joined


def test_empty_html_is_no_paragraphs() -> None:
    assert plain_text.html_to_paragraphs(None) == []
    assert plain_text.html_to_paragraphs("<p> </p>") == []


# ── Offer outcomes ──────────────────────────────────────────────────────────


def test_offer_outcome_rates_keep_decline_and_drop_apart() -> None:
    rows = [
        offer_outcomes.OfferOutcomeFacts(str(i), status, department_id="eng", notice_period_days=90)
        for i, status in enumerate(
            ["accepted", "joined", "joined", "dropped", "declined", "lapsed", "joined", "revoked"]
        )
    ]
    result = offer_outcomes.offer_outcomes(rows, minimum=5)
    overall = result["overall"]
    assert overall["accepted"] == 5
    assert overall["declined_or_lapsed"] == 2
    assert overall["acceptance_rate"] == round(5 / 7, 3)
    assert overall["dropped"] == 1
    assert overall["drop_rate"] is None, "four settled offers is below the floor"
    assert result["by_notice_period"][0]["key"] == "61-90 days"
