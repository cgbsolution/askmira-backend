"""Small pure rules: a 360 reviewer's relationship, and when an anniversary falls."""

from __future__ import annotations

from datetime import date

from app.services.culture.milestones import _anniversary_today
from app.services.performance.feedback_engine import relationship_of

ME, BOSS, BIGBOSS, REPORT, PEER, STRANGER = "me", "boss", "bigboss", "rep", "peer", "x"


def _rel(reviewer: str, reviewer_manager: str | None) -> str:
    return relationship_of(
        subject_id=ME, subject_manager_id=BOSS, skip_level_id=BIGBOSS,
        reviewer_id=reviewer, reviewer_manager_id=reviewer_manager,
    )


def test_relationships_follow_the_org_chart() -> None:
    assert _rel(BOSS, BIGBOSS) == "manager"
    assert _rel(BIGBOSS, None) == "skip_level"
    assert _rel(REPORT, ME) == "direct_report"
    assert _rel(PEER, BOSS) == "peer"
    assert _rel(STRANGER, "elsewhere") == "other"


def test_nobody_is_a_peer_of_somebody_with_no_manager() -> None:
    rel = relationship_of(
        subject_id=ME, subject_manager_id=None, skip_level_id=None,
        reviewer_id=STRANGER, reviewer_manager_id=None,
    )
    assert rel == "other"


def test_an_anniversary_is_the_same_day_and_month() -> None:
    assert _anniversary_today(date(2021, 10, 14), date(2026, 10, 14))
    assert not _anniversary_today(date(2021, 10, 15), date(2026, 10, 14))


def test_a_leap_day_joiner_is_marked_on_the_28th_in_other_years() -> None:
    assert _anniversary_today(date(2020, 2, 29), date(2025, 2, 28))
    assert not _anniversary_today(date(2020, 2, 29), date(2024, 2, 28))
    assert _anniversary_today(date(2020, 2, 29), date(2024, 2, 29))
