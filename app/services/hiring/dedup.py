"""Matching a new application to a candidate we already have.

═══════════════════════════════════════════════════════════════════════════════
"APPLIED THREE TIMES, REJECTED TWICE AT FINAL ROUND"
═══════════════════════════════════════════════════════════════════════════════

That sentence is the whole point of this module. Candidates in the Indian market
apply repeatedly — across roles, across boards, over years — and a system that
creates a fresh candidate row each time throws away the single most useful thing
a recruiter could know before opening a resume.

**The asymmetry drives every decision here.** A missed match shows a recruiter
two rows for one person: annoying, visible, fixable by hand. A wrong match
silently attaches one person's interview history to another person's name, and
somebody gets rejected for a final round they were never in. So:

- Email matches on the whole address, lowercased. Gmail treats `a.b@` and `ab@`
  as one; nobody else does, and stripping dots merges strangers at every company
  running its own mail.
- Phone matches on the last ten digits, which is what an Indian mobile number
  is — `+91 98765 43210` and `09876543210` are one phone.
- **Name is never a match on its own.** There are a great many people called
  Rahul Sharma and they are not each other.

Pure. No database — the caller supplies the candidates to compare against, which
is what lets every one of these rules be tested against a table of pairs rather
than against a query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.jobboards.base import normalise_email, normalise_phone

__all__ = [
    "CandidateIdentity",
    "MatchResult",
    "identity_of",
    "match_candidate",
]


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    """The comparable part of a candidate, already normalised."""

    candidate_id: str
    email: str | None
    phone: str | None
    full_name: str = ""
    # Carried through so a match can be explained with something a recruiter
    # recognises rather than a UUID.
    label: str = ""


def identity_of(
    candidate_id: Any, *, email: str | None, phone: str | None, full_name: str = ""
) -> CandidateIdentity:
    """Normalise once, at the boundary.

    Normalising inside the comparison instead would mean the stored side and the
    incoming side went through the rule at different times, and a change to the
    rule would match rows it no longer should.
    """
    return CandidateIdentity(
        candidate_id=str(candidate_id),
        email=normalise_email(email),
        phone=normalise_phone(phone),
        full_name=(full_name or "").strip(),
        label=full_name or email or str(candidate_id),
    )


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Whether this application belongs to somebody we already have.

    `matched_on` is the field that matched, not a score. A confidence number
    here would be a made-up number: either the email is the same address or it
    is not.
    """

    candidate_id: str | None
    matched_on: str | None
    reason: str
    # More than one existing candidate matched, which means the existing data
    # already holds a duplicate. Surfaced rather than resolved silently — a
    # merge is a decision with consequences, and this module does not have
    # enough information to make it.
    ambiguous_candidate_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_match(self) -> bool:
        return self.candidate_id is not None

    @property
    def needs_a_human(self) -> bool:
        return bool(self.ambiguous_candidate_ids)


def match_candidate(
    *,
    email: str | None,
    phone: str | None,
    full_name: str = "",
    existing: list[CandidateIdentity],
) -> MatchResult:
    """Find the candidate this application belongs to, if we already have them.

    Email is checked before phone. Both are strong, but a shared phone is a real
    thing in this market — a family landline, an agency's number on a batch of
    submissions — and an email address almost never is.
    """
    incoming_email = normalise_email(email)
    incoming_phone = normalise_phone(phone)

    if incoming_email:
        hits = [c for c in existing if c.email and c.email == incoming_email]
        if len(hits) == 1:
            return MatchResult(
                candidate_id=hits[0].candidate_id,
                matched_on="email",
                reason=(
                    f"Same email address as {hits[0].label}, who is already in the "
                    "pipeline. Linked to that record so their history stays in one "
                    "place."
                ),
            )
        if len(hits) > 1:
            return MatchResult(
                candidate_id=None,
                matched_on=None,
                reason=(
                    f"{len(hits)} existing candidates share the email {incoming_email}. "
                    "That is a duplicate we already had; picking one would attach this "
                    "application to whichever row came back first. Left for a human to "
                    "merge."
                ),
                ambiguous_candidate_ids=tuple(c.candidate_id for c in hits),
            )

    if incoming_phone:
        hits = [c for c in existing if c.phone and c.phone == incoming_phone]
        if len(hits) == 1:
            return MatchResult(
                candidate_id=hits[0].candidate_id,
                matched_on="phone",
                reason=(
                    f"Same phone number as {hits[0].label}. Linked to that record — "
                    "candidates change email addresses far more often than phones."
                ),
            )
        if len(hits) > 1:
            return MatchResult(
                candidate_id=None,
                matched_on=None,
                reason=(
                    f"{len(hits)} existing candidates share this phone number. A shared "
                    "number is common enough here — a family line, or an agency putting "
                    "its own number on a batch — that guessing would be wrong often "
                    "enough to matter. Left for a human."
                ),
                ambiguous_candidate_ids=tuple(c.candidate_id for c in hits),
            )

    # Name deliberately does not reach here. There are a great many people
    # called Rahul Sharma and they are not each other; matching on a name would
    # hand one person's rejection history to another.
    return MatchResult(
        candidate_id=None,
        matched_on=None,
        reason=(
            f"No existing candidate shares this email or phone, so "
            f"{full_name or 'this applicant'} is new."
        ),
    )
