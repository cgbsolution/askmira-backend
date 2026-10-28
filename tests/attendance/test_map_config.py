"""Which map the portal draws: Google first, Leaflet as the fallback.

The key lives in the API's settings only; the portal fetches it from
GET /attendance/map-config after sign-in.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.core.deps import get_current_user, get_session
from app.main import app
from app.services.attendance import maps


def _settings(monkeypatch: pytest.MonkeyPatch, provider: str, key: str | None) -> None:
    fake = maps.MapSettings(maps_provider=provider, google_maps_api_key=key)
    monkeypatch.setattr(maps, "get_map_settings", lambda: fake)


def test_google_is_first_when_a_key_is_set(monkeypatch: pytest.MonkeyPatch):
    _settings(monkeypatch, "google", "AIza-test-key")
    config = maps.map_config()
    assert config.provider == "google"
    assert config.google_maps_api_key == "AIza-test-key"


@pytest.mark.parametrize("key", [None, "", "   "])
def test_no_key_falls_back_to_leaflet(monkeypatch: pytest.MonkeyPatch, key: str | None):
    _settings(monkeypatch, "google", key)
    config = maps.map_config()
    assert config.provider == "leaflet"
    assert config.google_maps_api_key is None


@pytest.mark.parametrize("provider", ["leaflet", "Leaflet", " LEAFLET "])
def test_leaflet_setting_wins_and_never_hands_out_the_key(
    monkeypatch: pytest.MonkeyPatch, provider: str
):
    _settings(monkeypatch, provider, "AIza-test-key")
    config = maps.map_config()
    assert config.provider == "leaflet"
    assert config.google_maps_api_key is None


def test_key_is_trimmed(monkeypatch: pytest.MonkeyPatch):
    _settings(monkeypatch, "Google", "  AIza-test-key \n")
    assert maps.map_config().google_maps_api_key == "AIza-test-key"


@pytest.mark.asyncio
async def test_endpoint_needs_a_signed_in_person():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/attendance/map-config")
    assert response.status_code == 401


class _WorkspaceWithAttendance:
    """Stands in for the database: the workspace has attendance switched on."""

    async def get(self, model, key):
        return SimpleNamespace(id=key, enabled_modules={"leave": True})


@pytest.mark.asyncio
async def test_endpoint_returns_the_config(monkeypatch: pytest.MonkeyPatch):
    _settings(monkeypatch, "google", "AIza-test-key")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid.uuid4(), tenant_id=uuid.uuid4()
    )
    app.dependency_overrides[get_session] = lambda: _WorkspaceWithAttendance()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/attendance/map-config")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_session, None)
    assert response.status_code == 200
    assert response.json() == {"provider": "google", "google_maps_api_key": "AIza-test-key"}
