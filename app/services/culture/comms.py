"""Announcements, acknowledgement tracking, and the events calendar.

═══════════════════════════════════════════════════════════════════════════════
ACKNOWLEDGEMENT IS AN AUDIT ARTEFACT, NOT A READ RECEIPT
═══════════════════════════════════════════════════════════════════════════════

For a POSH or IT-policy announcement, the question a regulator or an internal
auditor asks is not "did we send it" — it is "who has confirmed they read it,
and can you show me". So an acknowledgement records the person, the moment, and
**which version of the text they acknowledged**.

That last part is the one products get wrong. Edit the body of a policy
announcement after two hundred people have acknowledged it, and those two
hundred acknowledgements now attest to a document none of them saw. So the text
is hashed at publication, the hash is stored on each acknowledgement, and an
edit that changes the body invalidates the acknowledgements rather than silently
inheriting them.

═══════════════════════════════════════════════════════════════════════════════
BIRTHDAYS ARE OPT-IN
═══════════════════════════════════════════════════════════════════════════════

A date of birth is personal data, and a company feed announcing it is a
disclosure the employee did not choose. Some people actively do not want it —
because of age discrimination, because of a religion that does not mark them,
because they simply do not want thirty "HBD!!" messages. Opt-out puts the burden
on the person who least wants to be noticed to go and find a setting.

So the default is **False**, and this module will not emit a birthday event
without an explicit opt-in. A work anniversary is a separate opt-in from a
birthday: they are different disclosures and consenting to one is not consenting
to the other.

Pure. `today` is always supplied.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

__all__ = [
    "Acknowledgement",
    "Announcement",
    "AnnouncementError",
    "Audience",
    "EventKind",
    "EventPrivacy",
    "acknowledgement_report",
    "body_hash",
    "calendar_events",
    "is_visible_to",
    "resolve_audience",
]


class AnnouncementError(ValueError):
    """A communications operation this module refuses."""


def body_hash(title: str, body: str) -> str:
    """The fingerprint an acknowledgement attests to.

    Title and body together: a POSH announcement retitled "Updated POSH policy"
    with the same body is a different communication, and an acknowledgement of
    the old title does not cover it.
    """
    digest = hashlib.sha256()
    digest.update(title.strip().encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(body.strip().encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class Audience:
    """Who an announcement is for.

    Every list empty means everybody. Unconfigured and nobody have to be
    different states: an announcement saved before departments were picked must
    not silently reach zero people while appearing to be published.
    """

    department_ids: tuple[str, ...] = ()
    location_ids: tuple[str, ...] = ()
    grades: tuple[str, ...] = ()

    @property
    def is_everyone(self) -> bool:
        return not (self.department_ids or self.location_ids or self.grades)

    def describe(self) -> str:
        if self.is_everyone:
            return "everyone"
        parts = []
        if self.department_ids:
            parts.append(f"{len(self.department_ids)} department(s)")
        if self.location_ids:
            parts.append(f"{len(self.location_ids)} location(s)")
        if self.grades:
            parts.append(f"grade(s) {', '.join(sorted(self.grades))}")
        return " and ".join(parts)


@dataclass(frozen=True, slots=True)
class Recipient:
    """One person the audience rules are evaluated against."""

    employee_id: str
    department_id: str | None = None
    location_id: str | None = None
    grade: str | None = None


def is_visible_to(audience: Audience, recipient: Recipient) -> bool:
    """Whether one person is in the audience.

    The dimensions are **and**-ed. "Engineering" and "Bengaluru" means
    Engineering in Bengaluru, not everybody in either — which is what somebody
    picking two filters means, and getting it wrong sends a location-specific
    notice to a whole department in another city.
    """
    if audience.is_everyone:
        return True
    if audience.department_ids and recipient.department_id not in audience.department_ids:
        return False
    if audience.location_ids and recipient.location_id not in audience.location_ids:
        return False
    return not (audience.grades and recipient.grade not in audience.grades)


def resolve_audience(audience: Audience, recipients: list[Recipient]) -> list[Recipient]:
    return [r for r in recipients if is_visible_to(audience, r)]


@dataclass(frozen=True, slots=True)
class Announcement:
    """One published communication."""

    announcement_id: str
    title: str
    body: str
    audience: Audience
    publish_at: datetime
    expires_at: datetime | None = None
    requires_acknowledgement: bool = False
    channels: tuple[str, ...] = ("in_app",)

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.body.strip():
            raise AnnouncementError("An announcement needs a title and a body.")
        if self.expires_at is not None and self.expires_at <= self.publish_at:
            raise AnnouncementError("An announcement cannot expire before it is published.")
        if self.requires_acknowledgement and self.expires_at is not None:
            # An acknowledgement register that disappears is not a register.
            # The audit question is asked months later, and "it expired" is not
            # an answer a POSH auditor accepts.
            raise AnnouncementError(
                "An announcement requiring acknowledgement cannot be given an "
                "expiry. The register has to still be there when somebody asks "
                "who read the policy, which is months after it stopped being "
                "news."
            )

    @property
    def content_hash(self) -> str:
        return body_hash(self.title, self.body)

    def is_live(self, moment: datetime) -> bool:
        if moment < self.publish_at:
            return False
        return self.expires_at is None or moment < self.expires_at


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    """One person confirming they read one version of one announcement."""

    announcement_id: str
    employee_id: str
    acknowledged_at: datetime
    #: The hash of the text at the moment they confirmed it.
    content_hash: str

    def covers(self, announcement: Announcement) -> bool:
        """Whether this acknowledgement attests to the current text.

        An edit to the body after publication makes every earlier
        acknowledgement attest to a document nobody saw. Inheriting them is how
        a company ends up telling an auditor that two hundred people read a
        policy that did not exist when they signed.
        """
        return self.content_hash == announcement.content_hash


def acknowledgement_report(
    *,
    announcement: Announcement,
    recipients: list[Recipient],
    acknowledgements: list[Acknowledgement],
) -> dict[str, Any]:
    """Who has acknowledged the current text, and who has not.

    The outstanding list is **named**, not counted: "43 outstanding" cannot be
    chased, and the entire point of the register is that somebody chases it
    before the audit rather than during it.
    """
    if not announcement.requires_acknowledgement:
        raise AnnouncementError(
            "This announcement does not require acknowledgement, so a "
            "compliance register for it would report an obligation nobody was "
            "given."
        )

    audience = resolve_audience(announcement.audience, recipients)
    expected = {r.employee_id for r in audience}

    current = {
        a.employee_id
        for a in acknowledgements
        if a.announcement_id == announcement.announcement_id and a.covers(announcement)
    }
    stale = {
        a.employee_id
        for a in acknowledgements
        if a.announcement_id == announcement.announcement_id and not a.covers(announcement)
    } - current

    outstanding = sorted(expected - current)

    return {
        "announcement_id": announcement.announcement_id,
        "content_hash": announcement.content_hash,
        "audience": announcement.audience.describe(),
        "expected": len(expected),
        "acknowledged": len(current & expected),
        "outstanding": outstanding,
        "stale": sorted(stale & expected),
        "complete": not outstanding,
        "note": (
            f"{len(stale & expected)} person(s) acknowledged an earlier version "
            "of this text. Their acknowledgement does not carry over - the "
            "document changed after they signed."
            if stale & expected
            else "Every acknowledgement on file is against the current text."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Events calendar
# ══════════════════════════════════════════════════════════════════════════════


class EventKind(str, enum.Enum):
    BIRTHDAY = "birthday"
    WORK_ANNIVERSARY = "work_anniversary"
    HOLIDAY = "holiday"
    COMPANY_EVENT = "company_event"

    @property
    def is_personal(self) -> bool:
        """Whether broadcasting it discloses something about a person."""
        return self in (EventKind.BIRTHDAY, EventKind.WORK_ANNIVERSARY)


@dataclass(frozen=True, slots=True)
class EventPrivacy:
    """What one employee has agreed to have announced.

    Both default to **False**. A date of birth is personal data and a feed post
    is a disclosure the person did not choose; opt-out puts the burden on the
    person who least wants to be noticed to go and find the setting.

    They are separate flags because they are separate disclosures: somebody
    happy for their five years of service to be marked may not want their age
    inferable from a birthday post.
    """

    employee_id: str
    share_birthday: bool = False
    share_work_anniversary: bool = False
    #: Where the birthday may be shown even when shared - some people are happy
    #: for their team to know and not the whole company.
    birthday_scope: str = "team"


@dataclass(frozen=True, slots=True)
class PersonEvent:
    """A dated personal event, before privacy is applied."""

    employee_id: str
    kind: EventKind
    on_date: date
    label: str
    years: int | None = None
    group_key: str = ""


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """An event that may actually be shown."""

    kind: EventKind
    on_date: date
    label: str
    employee_id: str | None = None
    years: int | None = None
    scope: str = "company"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "on_date": self.on_date.isoformat(),
            "label": self.label,
            "employee_id": self.employee_id,
            "years": self.years,
            "scope": self.scope,
        }


def calendar_events(
    *,
    person_events: list[PersonEvent],
    privacy: dict[str, EventPrivacy],
    company_events: list[CalendarEvent] = (),
    default_privacy: EventPrivacy | None = None,
) -> list[CalendarEvent]:
    """The events that may be published, with unshared personal ones removed.

    An employee with no privacy record is treated as **not** having opted in.
    That is the whole point of opt-in: the absence of a record is a refusal, not
    a gap to be filled with a default of yes.
    """
    if default_privacy is not None and (
        default_privacy.share_birthday or default_privacy.share_work_anniversary
    ):
        raise AnnouncementError(
            "The default privacy passed in shares a personal event. Opt-in "
            "means the default is no; a caller that can pass a permissive "
            "default has an opt-out setting wearing an opt-in's name."
        )

    out: list[CalendarEvent] = list(company_events)

    for event in person_events:
        if not event.kind.is_personal:
            out.append(
                CalendarEvent(
                    kind=event.kind,
                    on_date=event.on_date,
                    label=event.label,
                    employee_id=None,
                )
            )
            continue

        setting = privacy.get(event.employee_id)
        if setting is None:
            continue

        allowed = (
            setting.share_birthday
            if event.kind is EventKind.BIRTHDAY
            else setting.share_work_anniversary
        )
        if not allowed:
            continue

        out.append(
            CalendarEvent(
                kind=event.kind,
                on_date=event.on_date,
                label=event.label,
                employee_id=event.employee_id,
                years=event.years,
                scope=(setting.birthday_scope if event.kind is EventKind.BIRTHDAY else "company"),
            )
        )

    return sorted(out, key=lambda e: (e.on_date, e.kind.value, e.label))
