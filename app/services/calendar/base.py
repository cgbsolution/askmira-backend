"""The calendar interface. Google and Microsoft live behind it and nowhere else.

Same argument as the job boards: the scheduling service knows about `EventSpec`
and `BusyInterval`, not about Graph API attendee objects or Google's
`conferenceData`. Two providers with genuinely different shapes — Google's
free/busy is a batch query, Microsoft's `getSchedule` is per-user and returns a
string of availability codes — and a service that knew about either would grow a
branch for both.

**Two things this interface refuses to paper over.**

*A calendar that could not be read is not an empty calendar.* `free_busy`
returns a per-account result carrying whether the read succeeded, rather than an
optimistic merged list. Treating a failed read as "free" books an interview over
somebody's existing meeting, and the first anyone knows is at the interview.

*A reschedule updates the event; it never creates a second one.* Providers make
the wrong thing easy — create is idempotent-looking and update needs the id you
saved. An orphan event is a calendar entry nobody deletes, an attendee who turns
up, and a candidate who joined a call with nobody in it.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

__all__ = [
    "CalendarAdapter",
    "CalendarPermanentFailure",
    "CalendarProvider",
    "CalendarTransientFailure",
    "EventResult",
    "EventSpec",
    "FreeBusyResult",
    "Transport",
]


class CalendarTransientFailure(RuntimeError):
    """Worth retrying: a timeout, a 5xx, a throttle."""


class CalendarPermanentFailure(RuntimeError):
    """Not worth retrying: revoked consent, a deleted calendar, a bad address."""


class CalendarProvider(str, enum.Enum):
    GOOGLE = "google"
    MICROSOFT = "microsoft"


class Transport(Protocol):
    """The HTTP call, injected — so the mapping is testable without a provider."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class FreeBusyResult:
    """One account's busy blocks, and whether we actually got them.

    `ok` is the field that matters. Merging a failed read into a busy list as
    "nothing busy" is the single most expensive mistake this module can make.
    """

    account: str
    #: `(start, end)` pairs in UTC.
    busy: tuple[tuple[datetime, datetime], ...] = ()
    ok: bool = True
    error: str = ""


@dataclass(frozen=True, slots=True)
class EventSpec:
    """One interview, in our terms."""

    summary: str
    description: str
    start: datetime
    end: datetime
    #: Email addresses. The candidate is an attendee like anyone else — an
    #: interview the candidate is not invited to is a meeting about them.
    attendees: tuple[str, ...] = ()
    organiser: str = ""
    location: str = ""
    #: Ask the provider to mint a Meet/Teams link with the event.
    with_conference: bool = True
    timezone_name: str = "Asia/Kolkata"
    #: Our own id, written into the provider's extended properties so an event
    #: can be found again if the stored id is ever lost.
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class EventResult:
    external_event_id: str
    join_url: str | None = None
    html_link: str | None = None
    #: Attendees the provider rejected outright — a misspelled address, an
    #: external domain the tenant blocks. Surfaced rather than swallowed: an
    #: interviewer who never got the invitation is a no-show nobody predicted.
    rejected_attendees: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = ()


class CalendarAdapter(ABC):
    provider: CalendarProvider

    @abstractmethod
    async def free_busy(
        self, accounts: list[str], *, start: datetime, end: datetime, token: str
    ) -> list[FreeBusyResult]: ...

    @abstractmethod
    async def create_event(self, spec: EventSpec, *, token: str) -> EventResult: ...

    @abstractmethod
    async def update_event(
        self, external_event_id: str, spec: EventSpec, *, token: str
    ) -> EventResult:
        """Move an existing event.

        Never a delete-then-create. Attendees lose their responses, the join link
        changes under anybody already holding it, and a failure between the two
        leaves nothing at all.
        """

    @abstractmethod
    async def cancel_event(self, external_event_id: str, *, token: str) -> None: ...

    @abstractmethod
    async def responses(self, external_event_id: str, *, token: str) -> dict[str, str]:
        """Attendee response status, keyed by email.

        Values are `accepted`, `declined`, `tentative` or `needsAction`. Polled
        rather than waited for: a declined interviewer discovered at the
        interview is the failure this whole feature exists to prevent.
        """
