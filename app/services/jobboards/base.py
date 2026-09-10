"""The job board interface. Every board SDK lives behind it and nowhere else.

═══════════════════════════════════════════════════════════════════════════════
WHY THE ABSTRACTION COMES FIRST
═══════════════════════════════════════════════════════════════════════════════

Naukri is the Indian recruiting market, so the temptation is to write Naukri
into the hiring module and add the others later. That never happens later. What
happens is that "functional area" — a Naukri taxonomy field with no equivalent
on LinkedIn — ends up on the requisition, and the LinkedIn adapter is then
written to ignore a field it cannot use while the careers page renders it
because it is there.

So the hiring module knows about `JobSpec` and `RawApplication`, and nothing
else. An invariant greps for board names outside their own adapter.

**Failures are typed, and the distinction is the point.** A rate limit is not a
rejected posting. Treating everything as permanent drops a job that would have
posted fine ninety seconds later; treating everything as transient retries a
posting the board will never accept until somebody notices the queue. Neither
failure is ever swallowed: a posting that could not be published stays visible
as failed with the board's own words attached.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

__all__ = [
    "BoardCredentials",
    "BoardPermanentFailure",
    "BoardTransientFailure",
    "JobBoardAdapter",
    "JobSpec",
    "PublishResult",
    "RateLimited",
    "RawApplication",
    "Transport",
    "normalise_email",
    "normalise_phone",
]


class BoardTransientFailure(RuntimeError):
    """Worth retrying: a timeout, a 5xx, a rate limit."""


class BoardPermanentFailure(RuntimeError):
    """Not worth retrying: a rejected field value, an expired subscription."""


class RateLimited(BoardTransientFailure):
    """The board asked us to slow down.

    Carries `retry_after_seconds` when the board said so, because guessing
    when a board has already told us is how an account gets suspended.
    """

    def __init__(self, message: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class Transport(Protocol):
    """The HTTP call, injected into every adapter.

    Keeping the transport out of the adapters is what lets the *mapping* — the
    part that is actually ours and actually hard — be tested without a network
    or a board account. It lives here rather than in one board's module because
    every adapter takes one, and importing it from a sibling would mean the
    LinkedIn adapter depended on the Naukri adapter for no reason.
    """

    async def request(
        self, method: str, path: str, *, json: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class BoardCredentials:
    """Decrypted at the call site, never held on a model.

    `extras` carries whatever a particular board needs — Naukri's recruiter ID,
    LinkedIn's organisation URN — so adding a board does not add a column.
    """

    username: str
    secret: str
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JobSpec:
    """One job, in our vocabulary rather than any board's.

    Adapters translate this into their own taxonomy. Nothing board-shaped is
    allowed in here — the moment "functional_area" appears, every other board's
    adapter has to decide what to do with a field it has no concept of.
    """

    requisition_code: str
    title: str
    description_html: str
    skills: tuple[str, ...] = ()
    location: str = ""
    state_code: str | None = None
    experience_min_years: int | None = None
    experience_max_years: int | None = None
    ctc_min: Decimal | None = None
    ctc_max: Decimal | None = None
    # Whether the client wants money on the advert at all. Indian postings often
    # hide it, and a board that defaults to showing it would publish a salary
    # the client deliberately kept off the page.
    show_salary: bool = False
    employment_type: str = "full_time"
    openings: int = 1
    department: str | None = None
    apply_url: str | None = None
    expires_on: date | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    external_job_id: str
    published_at: datetime
    expires_at: datetime | None = None
    # What the board actually accepted, after its own normalisation. Kept so a
    # recruiter can see that "Bengaluru" was posted as "Bangalore" without
    # opening the board.
    accepted_as: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RawApplication:
    """One application, normalised out of whatever the board returned.

    Every source lands in this shape before it touches the candidate pipeline,
    which is what makes deduplication possible across boards at all: two
    applications for the same person from Naukri and LinkedIn are only
    comparable once they have the same field names.
    """

    external_application_id: str
    board: str
    external_job_id: str
    full_name: str
    email: str
    phone: str | None = None
    resume_url: str | None = None
    resume_bytes: bytes | None = None
    current_company: str | None = None
    current_ctc: Decimal | None = None
    expected_ctc: Decimal | None = None
    total_experience_years: Decimal | None = None
    notice_period_days: int | None = None
    applied_at: datetime | None = None
    # Anything the board sent that we have no column for. Kept rather than
    # discarded: a field nobody mapped is still evidence when a recruiter is
    # asking why a candidate looks wrong.
    raw: dict[str, Any] = field(default_factory=dict)


class JobBoardAdapter(ABC):
    """What every board must be able to do.

    Five methods, deliberately. A board that cannot do one of them raises
    `BoardPermanentFailure` with a sentence explaining which subscription tier
    it needs — silently returning nothing would look identical to a board with
    no applications today.
    """

    #: Lowercase board identifier. Also the key in the registry.
    board: str = ""

    #: Requests per minute this board tolerates. Used by the caller's throttle;
    #: a board that has not published a limit gets a conservative one rather
    #: than none.
    rate_limit_per_minute: int = 30

    @abstractmethod
    async def publish_job(
        self, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult: ...

    @abstractmethod
    async def update_job(
        self, external_job_id: str, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult: ...

    @abstractmethod
    async def close_job(
        self, external_job_id: str, credentials: BoardCredentials
    ) -> None: ...

    @abstractmethod
    async def fetch_applications(
        self,
        external_job_id: str,
        credentials: BoardCredentials,
        *,
        since: datetime | None = None,
    ) -> list[RawApplication]: ...

    @abstractmethod
    async def fetch_resume(
        self, application: RawApplication, credentials: BoardCredentials
    ) -> bytes: ...

    # ── Optional capabilities ───────────────────────────────────────────────

    async def refresh_job(
        self, external_job_id: str, credentials: BoardCredentials
    ) -> datetime:
        """Boost a posting back up the listing.

        Not every board has the concept. The default says so rather than
        pretending it worked — a scheduled refresh that silently does nothing
        is worse than no refresh, because the recruiter stops checking.
        """
        raise BoardPermanentFailure(
            f"{self.board} has no refresh or boost operation. A posting here does not "
            "decay in visibility, so there is nothing to schedule."
        )

    async def search_resumes(
        self, query: str, credentials: BoardCredentials, *, limit: int = 25
    ) -> list[RawApplication]:
        """Search the board's resume database.

        Gated on subscription almost everywhere, which is why the default
        explains rather than returns an empty list. An empty list is
        indistinguishable from "no matches", and a recruiter would conclude the
        database was empty rather than that it was never queried.
        """
        raise BoardPermanentFailure(
            f"{self.board} resume database search is not available on this account. It "
            "usually needs a separate subscription; nothing was searched."
        )


# ═══════════════════════════════════════════════════════════════════════════
# Identity normalisation — shared, because dedup depends on it being one rule
# ═══════════════════════════════════════════════════════════════════════════

_NON_DIGITS = re.compile(r"\D")


def normalise_email(value: str | None) -> str | None:
    """Lowercase and trim. Nothing cleverer, deliberately.

    Gmail treats `a.b@gmail.com` and `ab@gmail.com` as one address; most other
    providers do not. Stripping dots would merge two genuinely different people
    at any company running its own mail, which is most of them. A false merge is
    far worse than a false duplicate: one loses a candidate's history into
    somebody else's record, the other shows a recruiter two rows.
    """
    if not value:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def normalise_phone(value: str | None) -> str | None:
    """The last ten digits, which is what an Indian mobile number is.

    `+91 98765 43210`, `098765 43210` and `9876543210` are one phone. Comparing
    the raw strings finds none of those equal, and a recruiter then sees the
    same person three times with three partial histories.

    Numbers that do not resolve to ten digits come back as `None` rather than as
    a short key: two different four-digit extensions matching each other would
    merge two strangers.
    """
    if not value:
        return None
    digits = _NON_DIGITS.sub("", value)
    if len(digits) < 10:
        return None
    return digits[-10:]
