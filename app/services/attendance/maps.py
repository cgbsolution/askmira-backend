"""Which map the portal draws for attendance.

Google Maps first; Leaflet (OpenStreetMap, no key) when Google is switched off
or has no key. The portal also falls back to Leaflet by itself when Google
refuses the key or fails to load in the browser.

The one module that knows map providers, so it reads its own settings (as
`identity/social.py` does for sign-in providers). They live in the API's .env
only; the portal fetches them from GET /attendance/map-config after sign-in.
Google's map runs in the browser, so the key reaches it there: restrict it in
Google Cloud to the portal's domains and to the Maps JavaScript API.

It also turns a punch's coordinates into a street address (Google's Geocoding
API), from the server: only the coordinates are sent, never a name or an id.
The Geocoding API must be enabled for the same key. With the map switched to
Leaflet, or no key, nothing is looked up and punches show no address.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal

import httpx
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("askmira.maps")


class _HideKeys(logging.Filter):
    """Masks `key=` in the HTTP client's request log lines.

    Google's Geocoding API takes its key only in the URL (a header is refused),
    and httpx logs every request URL at INFO. Without this the Maps key would
    be written to the server log on each address lookup.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(_masked(arg) for arg in record.args)
        return True


def _masked(value: object) -> object:
    if isinstance(value, httpx.URL) and "key" in value.params:
        return value.copy_set_param("key", "***")
    return value


_httpx_log = logging.getLogger("httpx")
if not any(isinstance(f, _HideKeys) for f in _httpx_log.filters):
    _httpx_log.addFilter(_HideKeys())

#: Google's reverse geocoding endpoint.
_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


class MapSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    maps_provider: str = "google"
    google_maps_api_key: str | None = None


class MapConfigRead(BaseModel):
    """Which map the portal draws; Leaflet needs no key."""

    provider: Literal["google", "leaflet"]
    google_maps_api_key: str | None = None


def get_map_settings() -> MapSettings:
    return MapSettings()


def map_config() -> MapConfigRead:
    settings = get_map_settings()
    key = (settings.google_maps_api_key or "").strip()
    wants_google = settings.maps_provider.strip().lower() != "leaflet"
    if wants_google and key:
        return MapConfigRead(provider="google", google_maps_api_key=key)
    return MapConfigRead(provider="leaflet", google_maps_api_key=None)


# ── Addresses ───────────────────────────────────────────────────────────────


class AddressLookupError(Exception):
    """The lookup could not be made (refused key, quota, network): try again later."""


def geocoding_enabled() -> bool:
    """Whether punch addresses can be looked up: Google is the map and has a key."""
    return map_config().provider == "google"


def maps_link(latitude: float | Decimal, longitude: float | Decimal) -> str:
    """A Google Maps link to a point. Needs no key; nothing loads until it is opened."""
    return (
        "https://www.google.com/maps/search/?api=1&query="
        f"{float(latitude):.6f},{float(longitude):.6f}"
    )


async def reverse_geocode(
    client: httpx.AsyncClient, latitude: Decimal, longitude: Decimal
) -> str | None:
    """The street address at a point, or None when there is none.

    Raises `AddressLookupError` when Google cannot answer now (the key
    refused, the quota spent, the network down): the caller tries again on a
    later read rather than recording "no address".
    """
    key = (get_map_settings().google_maps_api_key or "").strip()
    if not key:
        raise AddressLookupError("no key")
    try:
        response = await client.get(
            _GEOCODE_URL,
            params={
                "latlng": f"{latitude},{longitude}",
                "key": key,
                "language": "en",
                "region": "in",
            },
        )
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AddressLookupError(str(exc)) from exc
    answer = body.get("status")
    if answer == "ZERO_RESULTS":
        return None
    if answer != "OK":
        # REQUEST_DENIED (key or API not enabled), OVER_QUERY_LIMIT, UNKNOWN_ERROR.
        logger.warning(
            "Address lookup refused: %s %s", answer, (body.get("error_message") or "")[:200]
        )
        raise AddressLookupError(str(answer))
    results = body.get("results") or []
    # A plus code ("8WRQ+X2 Bengaluru") is not an address a person can read.
    readable = [r for r in results if set(r.get("types") or []) != {"plus_code"}]
    chosen = (readable or results)[:1]
    address = (chosen[0].get("formatted_address") or "").strip() if chosen else ""
    return address[:512] or None
