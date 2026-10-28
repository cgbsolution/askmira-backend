"""Which map the portal draws for attendance.

Google Maps first; Leaflet (OpenStreetMap, no key) when Google is switched off
or has no key. The portal also falls back to Leaflet by itself when Google
refuses the key or fails to load in the browser.

The one module that knows map providers, so it reads its own settings (as
`identity/social.py` does for sign-in providers). They live in the API's .env
only; the portal fetches them from GET /attendance/map-config after sign-in.
Google's map runs in the browser, so the key reaches it there: restrict it in
Google Cloud to the portal's domains and to the Maps JavaScript API.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


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
