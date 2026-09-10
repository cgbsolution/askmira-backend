"""Microsoft 365 Calendar, through Graph.

Graph's `getSchedule` is a genuinely different shape from Google's freeBusy, and
the difference is the reason this adapter exists rather than a shared one with a
flag.

**Availability comes back as a string of digit codes**, one character per
interval — `"0"` free, `"1"` tentative, `"2"` busy, `"3"` out of office, `"4"`
working elsewhere. The `scheduleItems` array is richer but is omitted when the
caller has only free/busy permission, which is the permission most tenants
grant. So the string is the reliable source and it is decoded here.

**Tentative is treated as busy.** A tentatively-accepted meeting is a meeting
somebody expects to attend; scheduling an interview over it produces a conflict
they resolve by missing one of the two. "Working elsewhere" is *not* busy — that
is somebody at a different desk, not somebody unavailable.

═══════════════════════════════════════════════════════════════════════════════
NOT TESTED AGAINST A REAL MICROSOFT 365 TENANT.
Written against the documented Graph shape and exercised only against a
transport double. Consent, scopes and the Teams link behaviour need verifying on
a live tenant — a standing human gate.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.calendar.base import (
    CalendarAdapter,
    CalendarPermanentFailure,
    CalendarProvider,
    CalendarTransientFailure,
    EventResult,
    EventSpec,
    FreeBusyResult,
    Transport,
)

__all__ = ["AVAILABILITY_CODES", "MicrosoftCalendarAdapter"]

#: Graph's availabilityView codes.
AVAILABILITY_CODES = {
    "0": "free",
    "1": "tentative",
    "2": "busy",
    "3": "out_of_office",
    "4": "working_elsewhere",
}

#: What counts as unavailable for scheduling an interview.
#:
#: Tentative is in: a tentatively-accepted meeting is one somebody expects to
#: attend, and booking over it produces a clash they resolve by missing one.
#: Working elsewhere is out: that is a different desk, not a busy person.
BLOCKING_CODES = frozenset({"1", "2", "3"})

#: The interval each character of availabilityView represents. Graph's minimum
#: and the finest resolution worth asking for.
INTERVAL_MINUTES = 30


class MicrosoftCalendarAdapter(CalendarAdapter):
    provider = CalendarProvider.MICROSOFT

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _check(self, code: int, body: dict[str, Any]) -> None:
        if 200 <= code < 300:
            return
        error = body.get("error") or {}
        message = str(error.get("message") or body.get("message") or f"HTTP {code}")
        if code in (401, 403):
            raise CalendarPermanentFailure(
                f"Microsoft refused this: {message}. Usually a revoked consent or a "
                "missing scope — the interviewer has to reconnect their calendar."
            )
        if code in (400, 404, 410):
            raise CalendarPermanentFailure(f"Microsoft rejected this: {message}")
        if code in (429, 503):
            raise CalendarTransientFailure(f"Microsoft throttled us: {message}")
        raise CalendarTransientFailure(f"Microsoft returned {code}: {message}")

    @staticmethod
    def decode_availability(
        view: str, *, start: datetime, interval_minutes: int = INTERVAL_MINUTES
    ) -> tuple[tuple[datetime, datetime], ...]:
        """Turn `"002200"` into busy intervals.

        Consecutive blocking characters are merged, so a two-hour meeting is one
        interval rather than four adjacent ones — which matters because the
        availability engine pads each block with a buffer, and four blocks would
        pad the middle of a single meeting three times.
        """
        base = start.astimezone(UTC)
        step = timedelta(minutes=interval_minutes)
        out: list[tuple[datetime, datetime]] = []

        run_start: datetime | None = None
        for index, character in enumerate(view):
            blocking = character in BLOCKING_CODES
            moment = base + step * index
            if blocking and run_start is None:
                run_start = moment
            elif not blocking and run_start is not None:
                out.append((run_start, moment))
                run_start = None
        if run_start is not None:
            out.append((run_start, base + step * len(view)))
        return tuple(out)

    async def free_busy(
        self, accounts: list[str], *, start: datetime, end: datetime, token: str
    ) -> list[FreeBusyResult]:
        code, body = await self._transport.request(
            "POST",
            "/v1.0/me/calendar/getSchedule",
            json={
                "schedules": accounts,
                "startTime": {
                    "dateTime": start.astimezone(UTC).isoformat(),
                    "timeZone": "UTC",
                },
                "endTime": {
                    "dateTime": end.astimezone(UTC).isoformat(),
                    "timeZone": "UTC",
                },
                "availabilityViewInterval": INTERVAL_MINUTES,
            },
            headers=self._headers(token),
        )
        self._check(code, body)

        by_account = {
            entry.get("scheduleId"): entry for entry in body.get("value", [])
        }
        results: list[FreeBusyResult] = []
        for account in accounts:
            entry = by_account.get(account)
            if entry is None:
                results.append(
                    FreeBusyResult(
                        account=account,
                        ok=False,
                        error=(
                            "Microsoft returned no schedule for this address. It may not "
                            "exist in the tenant, or the token may not cover it."
                        ),
                    )
                )
                continue

            # Graph reports per-mailbox errors inside a 200, the same way Google
            # does. Same rule: not readable is not free.
            error = entry.get("error")
            if error:
                results.append(
                    FreeBusyResult(
                        account=account,
                        ok=False,
                        error=(
                            f"Microsoft could not read this mailbox: "
                            f"{error.get('message', error)}"
                        ),
                    )
                )
                continue

            view = entry.get("availabilityView") or ""
            results.append(
                FreeBusyResult(
                    account=account,
                    busy=self.decode_availability(view, start=start),
                    ok=True,
                )
            )
        return results

    def _event_body(self, spec: EventSpec) -> dict[str, Any]:
        body: dict[str, Any] = {
            "subject": spec.summary,
            "body": {"contentType": "HTML", "content": spec.description},
            "start": {
                "dateTime": spec.start.astimezone(UTC).isoformat(),
                "timeZone": "UTC",
            },
            "end": {"dateTime": spec.end.astimezone(UTC).isoformat(), "timeZone": "UTC"},
            "attendees": [
                {"emailAddress": {"address": address}, "type": "required"}
                for address in spec.attendees
            ],
        }
        if spec.location:
            body["location"] = {"displayName": spec.location}
        if spec.with_conference:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        if spec.correlation_id:
            body["singleValueExtendedProperties"] = [
                {
                    "id": "String {66f5a359-4659-4830-9070-00047ec6ac6e} "
                    "Name askmira_interview_id",
                    "value": spec.correlation_id,
                }
            ]
        return body

    def _result(self, body: dict[str, Any], spec: EventSpec) -> EventResult:
        meeting = body.get("onlineMeeting") or {}
        join = meeting.get("joinUrl")

        warnings: list[str] = []
        if spec.with_conference and not join:
            warnings.append(
                "Microsoft created the event without a Teams link. The joining "
                "instructions would say 'video call' with no way to join — check that "
                "online meetings are enabled for this mailbox."
            )

        return EventResult(
            external_event_id=str(body.get("id") or ""),
            join_url=join,
            html_link=body.get("webLink"),
            warnings=tuple(warnings),
        )

    async def create_event(self, spec: EventSpec, *, token: str) -> EventResult:
        code, body = await self._transport.request(
            "POST", "/v1.0/me/events", json=self._event_body(spec),
            headers=self._headers(token),
        )
        self._check(code, body)
        result = self._result(body, spec)
        if not result.external_event_id:
            raise CalendarPermanentFailure(
                "Microsoft accepted the event but returned no id, so nothing here could "
                "move or cancel it later."
            )
        return result

    async def update_event(
        self, external_event_id: str, spec: EventSpec, *, token: str
    ) -> EventResult:
        """PATCH the existing event, never delete-then-create."""
        code, body = await self._transport.request(
            "PATCH", f"/v1.0/me/events/{external_event_id}",
            json=self._event_body(spec), headers=self._headers(token),
        )
        self._check(code, body)
        return self._result(body, spec)

    async def cancel_event(self, external_event_id: str, *, token: str) -> None:
        code, body = await self._transport.request(
            "DELETE", f"/v1.0/me/events/{external_event_id}",
            headers=self._headers(token),
        )
        if code in (404, 410):
            return
        self._check(code, body)

    async def responses(self, external_event_id: str, *, token: str) -> dict[str, str]:
        code, body = await self._transport.request(
            "GET", f"/v1.0/me/events/{external_event_id}", headers=self._headers(token)
        )
        self._check(code, body)
        # Graph's vocabulary differs from Google's; normalised here so the
        # service never has to know which provider an interview was booked on.
        mapping = {
            "accepted": "accepted",
            "tentativelyAccepted": "tentative",
            "declined": "declined",
            "notResponded": "needsAction",
            "none": "needsAction",
            "organizer": "accepted",
        }
        return {
            attendee["emailAddress"]["address"]: mapping.get(
                (attendee.get("status") or {}).get("response", "none"), "needsAction"
            )
            for attendee in body.get("attendees", [])
            if attendee.get("emailAddress", {}).get("address")
        }
