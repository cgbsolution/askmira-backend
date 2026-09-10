"""Adapter lookup and OAuth token handling for calendars.

The scheduling service asks for the adapter for a provider and gets a
`CalendarAdapter`. It never imports `GoogleCalendarAdapter`, and an invariant
checks that no module outside `app/services/calendar/` names a provider in code.

Tokens are decrypted here and nowhere else. A calendar OAuth token reads
somebody's entire diary — every meeting title, every attendee — which is a good
deal more personal than most of what this system holds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.crypto import decrypt_secret
from app.services.calendar.base import (
    CalendarAdapter,
    CalendarPermanentFailure,
    CalendarProvider,
    Transport,
)
from app.services.calendar.google import GoogleCalendarAdapter
from app.services.calendar.microsoft import MicrosoftCalendarAdapter

__all__ = [
    "CalendarUnavailable",
    "access_token_for",
    "adapter_class",
    "get_adapter",
    "register_adapter",
    "supported_providers",
]

#: Refresh a token this long before it expires. A token that expires between the
#: check and the call fails the call, and the failure looks like revoked consent.
REFRESH_MARGIN = timedelta(minutes=5)


class CalendarUnavailable(RuntimeError):
    """No adapter, no connection, or no usable token."""


_BUILDERS: dict[CalendarProvider, Any] = {
    CalendarProvider.GOOGLE: GoogleCalendarAdapter,
    CalendarProvider.MICROSOFT: MicrosoftCalendarAdapter,
}

_OVERRIDES: dict[CalendarProvider, CalendarAdapter] = {}


def register_adapter(provider: CalendarProvider, adapter: CalendarAdapter) -> None:
    _OVERRIDES[provider] = adapter


def supported_providers() -> tuple[CalendarProvider, ...]:
    return tuple(_BUILDERS)


def adapter_class(provider: CalendarProvider) -> type | None:
    return _BUILDERS.get(provider)


def get_adapter(
    provider: CalendarProvider, transport: Transport | None = None
) -> CalendarAdapter:
    if provider in _OVERRIDES:
        return _OVERRIDES[provider]
    builder = _BUILDERS.get(provider)
    if builder is None:
        raise CalendarUnavailable(f"No adapter for '{provider}'.")
    if transport is None:
        raise CalendarUnavailable(
            f"No HTTP transport is configured for {provider.value}. This deployment "
            "cannot reach the calendar — nothing was read and nothing was booked."
        )
    return builder(transport)


def access_token_for(connection: Any, tenant_id: uuid.UUID, *, now: datetime | None = None) -> str:
    """The usable access token for one connection.

    Decrypted at the point of use and never attached to the model — a calendar
    token on an ORM object ends up in a debug repr or an error report, and this
    one reads somebody's entire diary.

    An expired token raises rather than being sent. Sending it produces a 401
    that is indistinguishable from revoked consent, and the interviewer gets
    told to reconnect a calendar that was working fine.
    """
    moment = now or datetime.now(UTC)

    if getattr(connection, "revoked_at", None) is not None:
        raise CalendarUnavailable(
            f"This {connection.provider.value} connection was disconnected. Nothing was "
            "read; the interviewer has to reconnect."
        )
    if not connection.access_token_encrypted:
        raise CalendarUnavailable(
            f"No {connection.provider.value} token is stored for this interviewer."
        )

    expires_at = getattr(connection, "expires_at", None)
    if expires_at is not None and expires_at - REFRESH_MARGIN <= moment:
        raise CalendarPermanentFailure(
            f"The stored {connection.provider.value} token expired at {expires_at}. It "
            "needs refreshing before this call — sending it would produce a 401 that "
            "looks exactly like revoked consent, and the interviewer would be told to "
            "reconnect a calendar that is fine."
        )

    token = decrypt_secret(connection.access_token_encrypted, tenant_id)
    if not token:
        raise CalendarUnavailable(
            "The stored calendar token could not be decrypted. This usually means the "
            "encryption key changed since it was saved; the calendar has to be "
            "reconnected."
        )
    return token
