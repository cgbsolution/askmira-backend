"""Google Calendar.

Two things about Google's API shape drive this file.

**`freeBusy.query` reports per-calendar errors inside a 200 response.** A
calendar the token cannot see comes back as `{"errors": [...]}` alongside the
calendars that worked, and the outer request succeeded. Code that reads
`calendars[email]["busy"]` and moves on treats "I could not look" as "nothing
booked" — so every entry is checked for `errors` before its `busy` list is
believed.

**A conference link needs `conferenceDataVersion=1` and a request id.** Omit
either and the event is created without a Meet link and without complaining,
which produces an interview whose joining instructions say "video call" and
carry no way to join.

═══════════════════════════════════════════════════════════════════════════════
NOT TESTED AGAINST A REAL GOOGLE CALENDAR.
Written against the documented v3 shape and exercised only against a transport
double. OAuth consent, scope grants and the conference-link behaviour all need
verifying on a live account — a standing human gate.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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

__all__ = ["GoogleCalendarAdapter"]


class GoogleCalendarAdapter(CalendarAdapter):
    provider = CalendarProvider.GOOGLE

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
                f"Google refused this: {message}. Usually a revoked or expired consent — "
                "the interviewer has to reconnect their calendar."
            )
        if code in (400, 404, 410):
            raise CalendarPermanentFailure(f"Google rejected this: {message}")
        if code == 429:
            raise CalendarTransientFailure(f"Google throttled us: {message}")
        raise CalendarTransientFailure(f"Google returned {code}: {message}")

    async def free_busy(
        self, accounts: list[str], *, start: datetime, end: datetime, token: str
    ) -> list[FreeBusyResult]:
        code, body = await self._transport.request(
            "POST",
            "/calendar/v3/freeBusy",
            json={
                "timeMin": start.astimezone(UTC).isoformat(),
                "timeMax": end.astimezone(UTC).isoformat(),
                "items": [{"id": account} for account in accounts],
            },
            headers=self._headers(token),
        )
        self._check(code, body)

        calendars = body.get("calendars", {})
        results: list[FreeBusyResult] = []
        for account in accounts:
            entry = calendars.get(account)
            if entry is None:
                # Asked for, not answered. Not the same as free.
                results.append(
                    FreeBusyResult(
                        account=account,
                        ok=False,
                        error=(
                            "Google returned no entry for this calendar. It may not "
                            "exist, or the token may not cover it."
                        ),
                    )
                )
                continue

            # Per-calendar errors arrive inside a 200. Reading `busy` without
            # checking here is how "I could not look" becomes "nothing booked".
            errors = entry.get("errors")
            if errors:
                reasons = ", ".join(str(e.get("reason", e)) for e in errors)
                results.append(
                    FreeBusyResult(
                        account=account,
                        ok=False,
                        error=f"Google could not read this calendar: {reasons}",
                    )
                )
                continue

            busy = tuple(
                (
                    datetime.fromisoformat(block["start"].replace("Z", "+00:00")),
                    datetime.fromisoformat(block["end"].replace("Z", "+00:00")),
                )
                for block in entry.get("busy", [])
            )
            results.append(FreeBusyResult(account=account, busy=busy, ok=True))
        return results

    def _event_body(self, spec: EventSpec) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": spec.summary,
            "description": spec.description,
            "start": {"dateTime": spec.start.astimezone(UTC).isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": spec.end.astimezone(UTC).isoformat(), "timeZone": "UTC"},
            "attendees": [{"email": address} for address in spec.attendees],
            "reminders": {"useDefault": True},
        }
        if spec.location:
            body["location"] = spec.location
        if spec.correlation_id:
            # Written into the event so it can be found again if our stored id
            # is ever lost — which is how orphans happen.
            body["extendedProperties"] = {
                "private": {"askmira_interview_id": spec.correlation_id}
            }
        if spec.with_conference:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": str(uuid.uuid4()),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        return body

    def _result(self, body: dict[str, Any], spec: EventSpec) -> EventResult:
        conference = body.get("conferenceData") or {}
        entry_points = conference.get("entryPoints") or []
        join = next(
            (e.get("uri") for e in entry_points if e.get("entryPointType") == "video"),
            None,
        )

        warnings: list[str] = []
        if spec.with_conference and not join:
            warnings.append(
                "Google created the event without a Meet link. The joining instructions "
                "would say 'video call' with no way to join — add a link by hand or "
                "check the calendar's conferencing settings."
            )

        rejected = tuple(
            attendee.get("email", "")
            for attendee in body.get("attendees", [])
            if attendee.get("responseStatus") == "declined"
            and attendee.get("self") is not True
            and attendee.get("comment") == "invalid"
        )

        return EventResult(
            external_event_id=str(body.get("id") or ""),
            join_url=join,
            html_link=body.get("htmlLink"),
            rejected_attendees=rejected,
            warnings=tuple(warnings),
        )

    async def create_event(self, spec: EventSpec, *, token: str) -> EventResult:
        code, body = await self._transport.request(
            "POST",
            # `conferenceDataVersion=1` is required or the conference request is
            # silently ignored and the event has no link.
            "/calendar/v3/calendars/primary/events"
            "?conferenceDataVersion=1&sendUpdates=all",
            json=self._event_body(spec),
            headers=self._headers(token),
        )
        self._check(code, body)
        result = self._result(body, spec)
        if not result.external_event_id:
            raise CalendarPermanentFailure(
                "Google accepted the event but returned no id, so nothing here could "
                "move or cancel it later. Treated as a failure rather than an event we "
                "cannot manage."
            )
        return result

    async def update_event(
        self, external_event_id: str, spec: EventSpec, *, token: str
    ) -> EventResult:
        """PATCH the existing event.

        Not delete-then-create: attendees lose their responses, the join link
        changes under anyone already holding it, and a failure between the two
        leaves the interview with no calendar entry at all.
        """
        code, body = await self._transport.request(
            "PATCH",
            f"/calendar/v3/calendars/primary/events/{external_event_id}"
            "?conferenceDataVersion=1&sendUpdates=all",
            json=self._event_body(spec),
            headers=self._headers(token),
        )
        self._check(code, body)
        return self._result(body, spec)

    async def cancel_event(self, external_event_id: str, *, token: str) -> None:
        code, body = await self._transport.request(
            "DELETE",
            f"/calendar/v3/calendars/primary/events/{external_event_id}?sendUpdates=all",
            headers=self._headers(token),
        )
        if code == 410:
            # Already gone. The desired state is "no event", and it is met.
            return
        self._check(code, body)

    async def responses(self, external_event_id: str, *, token: str) -> dict[str, str]:
        code, body = await self._transport.request(
            "GET",
            f"/calendar/v3/calendars/primary/events/{external_event_id}",
            headers=self._headers(token),
        )
        self._check(code, body)
        return {
            attendee.get("email", ""): attendee.get("responseStatus", "needsAction")
            for attendee in body.get("attendees", [])
            if attendee.get("email")
        }
