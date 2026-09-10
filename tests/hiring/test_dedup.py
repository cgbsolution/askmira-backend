"""A missed match shows two rows. A wrong match rejects the wrong person."""

from __future__ import annotations

from app.services.hiring.dedup import identity_of, match_candidate
from app.services.jobboards.base import normalise_email, normalise_phone


def _existing(*rows) -> list:
    return [
        identity_of(f"cand-{index}", email=email, phone=phone, full_name=name)
        for index, (name, email, phone) in enumerate(rows)
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Normalisation
# ═══════════════════════════════════════════════════════════════════════════


def test_an_indian_mobile_written_three_ways_is_one_phone() -> None:
    """`+91 98765 43210`, `098765 43210` and `9876543210` are the same number.

    Comparing the raw strings finds none of these equal, and a recruiter then
    sees the same person three times with three partial histories.
    """
    for written in ("+91 98765 43210", "098765 43210", "9876543210", "+91-98765-43210"):
        assert normalise_phone(written) == "9876543210", written


def test_a_number_too_short_to_be_a_phone_is_not_a_key() -> None:
    """Two different four-digit extensions matching each other would merge two
    strangers."""
    assert normalise_phone("1234") is None
    assert normalise_phone("") is None
    assert normalise_phone(None) is None


def test_email_is_lowercased_and_trimmed_and_nothing_else() -> None:
    assert normalise_email("  Rohit.Verma@Example.COM ") == "rohit.verma@example.com"


def test_dots_are_not_stripped_from_an_email() -> None:
    """Gmail treats `a.b@` and `ab@` as one address. Nobody else does, and
    stripping dots merges genuinely different people at every company running
    its own mail — which is most of them."""
    assert normalise_email("a.b@acme.com") != normalise_email("ab@acme.com")


# ═══════════════════════════════════════════════════════════════════════════
# Matching
# ═══════════════════════════════════════════════════════════════════════════


def test_a_repeat_applicant_matches_on_email() -> None:
    existing = _existing(("Rohit Verma", "rohit@example.test", "9876543210"))
    result = match_candidate(
        email="ROHIT@example.test", phone=None, full_name="Rohit Verma", existing=existing
    )
    assert result.is_match
    assert result.matched_on == "email"
    assert result.candidate_id == "cand-0"
    assert "Rohit Verma" in result.reason


def test_a_repeat_applicant_matches_on_phone_when_the_email_changed() -> None:
    """People change email addresses far more often than phones — a personal
    address after leaving a job, a new provider."""
    existing = _existing(("Rohit Verma", "rohit@oldcompany.test", "+91 98765 43210"))
    result = match_candidate(
        email="rohit.verma@gmail.test", phone="09876543210", existing=existing
    )
    assert result.is_match
    assert result.matched_on == "phone"


def test_email_wins_over_phone() -> None:
    """A shared phone is a real thing here — a family line, an agency's number
    on a batch of submissions. A shared email almost never is."""
    existing = _existing(
        ("Rohit Verma", "rohit@example.test", "9999999999"),
        ("Someone Else", "other@example.test", "9876543210"),
    )
    result = match_candidate(
        email="rohit@example.test", phone="9876543210", existing=existing
    )
    assert result.candidate_id == "cand-0"
    assert result.matched_on == "email"


def test_a_new_person_is_new() -> None:
    existing = _existing(("Rohit Verma", "rohit@example.test", "9876543210"))
    result = match_candidate(
        email="priya@example.test", phone="9000000001", full_name="Priya Nair",
        existing=existing,
    )
    assert not result.is_match
    assert "Priya Nair" in result.reason


def test_a_shared_name_is_never_a_match() -> None:
    """There are a great many people called Rahul Sharma and they are not each
    other. Matching on a name hands one person's rejection history to another.
    """
    existing = _existing(("Rahul Sharma", "rahul.sharma@one.test", "9000000001"))
    result = match_candidate(
        email="rahul.sharma@two.test", phone="9000000002", full_name="Rahul Sharma",
        existing=existing,
    )
    assert not result.is_match


def test_two_existing_candidates_sharing_an_email_is_left_for_a_human() -> None:
    """That is a duplicate we already had. Picking one attaches this
    application to whichever row came back first."""
    existing = _existing(
        ("Rohit Verma", "rohit@example.test", "9876543210"),
        ("R Verma", "rohit@example.test", "9000000002"),
    )
    result = match_candidate(email="rohit@example.test", phone=None, existing=existing)
    assert not result.is_match
    assert result.needs_a_human
    assert set(result.ambiguous_candidate_ids) == {"cand-0", "cand-1"}
    assert "merge" in result.reason


def test_two_existing_candidates_sharing_a_phone_is_left_for_a_human() -> None:
    existing = _existing(
        ("Rohit Verma", "rohit@example.test", "9876543210"),
        ("Anita Verma", "anita@example.test", "+91 98765 43210"),
    )
    result = match_candidate(email="new@example.test", phone="9876543210", existing=existing)
    assert not result.is_match
    assert result.needs_a_human
    assert "agency" in result.reason


def test_an_application_with_no_email_or_phone_matches_nothing() -> None:
    existing = _existing(("Rohit Verma", "rohit@example.test", "9876543210"))
    result = match_candidate(email=None, phone=None, full_name="Rohit Verma", existing=existing)
    assert not result.is_match


def test_an_empty_pipeline_matches_nothing_without_raising() -> None:
    result = match_candidate(email="a@b.test", phone="9000000000", existing=[])
    assert not result.is_match


def test_a_stored_candidate_with_a_malformed_phone_does_not_match_everyone() -> None:
    """A candidate whose phone was entered as "-" normalises to `None`, which
    must not become a wildcard that matches every incoming application."""
    existing = _existing(("Broken Record", "broken@example.test", "-"))
    result = match_candidate(email="new@example.test", phone="9876543210", existing=existing)
    assert not result.is_match
