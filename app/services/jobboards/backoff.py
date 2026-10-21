"""Retry scheduling for board calls, and the rule that nothing is dropped.

A rate-limit failure must not silently lose a posting. That is easy to say and
easy to get wrong in one specific way: the retry loop runs, exhausts its
attempts, logs, and returns — and the posting is now neither published nor
visible as failed anywhere a recruiter looks. The job simply is not on Naukri,
and the first anyone knows is that no applications came in.

So `plan_attempt` returns a decision object rather than a sleep duration, and
the only terminal outcomes it can produce are `retry` and `park`. There is no
"give up quietly": a parked posting stays a row with the board's own words on
it, waiting for a human.

Pure. No sleeping, no clock, no network — the caller supplies `now`, which is
what makes the schedule testable without waiting eleven minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = [
    "MAX_ATTEMPTS",
    "Decision",
    "plan_attempt",
    "retry_delay",
]

#: After this many attempts a posting is parked for a human rather than retried
#: forever. Six attempts over the schedule below is a little under an hour,
#: which covers every transient outage a board has and none of the real ones.
MAX_ATTEMPTS = 6

#: Exponential with a ceiling. The ceiling matters: doubling past ten minutes
#: means the sixth attempt lands hours later, by which point the recruiter has
#: already posted it by hand and there are two live postings.
_SCHEDULE = (30, 60, 120, 300, 600, 600)


def retry_delay(attempt: int, *, retry_after_seconds: int | None = None) -> timedelta:
    """How long to wait before attempt number `attempt` (1-based).

    A board that told us `Retry-After` wins outright. Guessing when the board
    has already said is how an account gets throttled harder, and on Naukri it
    is how an account gets suspended.
    """
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return timedelta(seconds=retry_after_seconds)
    index = max(0, min(attempt - 1, len(_SCHEDULE) - 1))
    return timedelta(seconds=_SCHEDULE[index])


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do about a failed board call.

    `action` is one of `retry` or `park`. There is deliberately no third value
    meaning "stop": a posting that stopped being tried and was not parked is a
    posting nobody knows is missing.
    """

    action: str
    attempt: int
    next_attempt_at: datetime | None
    reason: str

    @property
    def should_retry(self) -> bool:
        return self.action == "retry"

    @property
    def needs_a_human(self) -> bool:
        return self.action == "park"


def plan_attempt(
    *,
    attempt: int,
    permanent: bool,
    message: str,
    now: datetime | None = None,
    retry_after_seconds: int | None = None,
) -> Decision:
    """Decide what happens after a failed call.

    Args:
        attempt: how many attempts have now been made, 1-based.
        permanent: whether the board rejected the request outright. A rejected
            field value does not become acceptable by being sent again.
        message: the board's own words, carried into the decision so a parked
            posting says why rather than saying "failed".
        now: supplied by the caller so the schedule is testable.
        retry_after_seconds: honoured over the schedule when the board sent it.
    """
    moment = now or datetime.now(UTC)

    if permanent:
        return Decision(
            action="park",
            attempt=attempt,
            next_attempt_at=None,
            reason=(
                f"The board rejected this outright and will reject it again: {message}. "
                "Parked for a human — retrying cannot fix a value the board does not "
                "accept."
            ),
        )

    if attempt >= MAX_ATTEMPTS:
        return Decision(
            action="park",
            attempt=attempt,
            next_attempt_at=None,
            reason=(
                f"Still failing after {attempt} attempts: {message}. Parked for a human. "
                "The posting is not live on the board and is not being retried; nobody "
                "has to notice the absence of applications to find that out."
            ),
        )

    delay = retry_delay(attempt + 1, retry_after_seconds=retry_after_seconds)
    return Decision(
        action="retry",
        attempt=attempt,
        next_attempt_at=moment + delay,
        reason=(
            f"Attempt {attempt} failed: {message}. Retrying in "
            f"{int(delay.total_seconds())}s"
            + (" — the board asked us to wait that long." if retry_after_seconds else ".")
        ),
    )
