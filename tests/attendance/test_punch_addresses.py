"""Reading an address out of the map provider's answer, and rounding a punch to a place."""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pytest

from app.services import sheets
from app.services.attendance import addresses, maps


def _client(body: dict) -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        assert "latlng=12.9716%2C77.5946" in str(request.url)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setenv("MAPS_PROVIDER", "google")


async def test_a_street_address_is_preferred_to_a_plus_code() -> None:
    body = {
        "status": "OK",
        "results": [
            {"types": ["plus_code"], "formatted_address": "WRQ2+2X Bengaluru"},
            {"types": ["street_address"], "formatted_address": "12, MG Road, Bengaluru 560001"},
        ],
    }
    async with _client(body) as client:
        found = await maps.reverse_geocode(client, Decimal("12.9716"), Decimal("77.5946"))
    assert found == "12, MG Road, Bengaluru 560001"


async def test_no_result_is_no_address_and_a_refusal_is_retried_later() -> None:
    async with _client({"status": "ZERO_RESULTS", "results": []}) as client:
        assert await maps.reverse_geocode(client, Decimal("12.9716"), Decimal("77.5946")) is None
    async with _client({"status": "REQUEST_DENIED", "error_message": "API not enabled"}) as c:
        with pytest.raises(maps.AddressLookupError):
            await maps.reverse_geocode(c, Decimal("12.9716"), Decimal("77.5946"))


def test_lookups_follow_the_map_setting(monkeypatch) -> None:
    assert maps.geocoding_enabled() is True
    monkeypatch.setenv("MAPS_PROVIDER", "leaflet")
    assert maps.geocoding_enabled() is False
    monkeypatch.setenv("MAPS_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")
    assert maps.geocoding_enabled() is False


def test_a_place_is_the_point_rounded_to_about_eleven_metres() -> None:
    assert addresses.place_of(12.97163, 77.59456) == (Decimal("12.9716"), Decimal("77.5946"))
    assert addresses.place_of(None, 77.5) is None
    assert addresses.place_of(95, 77.5) is None
    link = maps.maps_link(12.9716, 77.5946)
    assert link == "https://www.google.com/maps/search/?api=1&query=12.971600,77.594600"


def test_a_csv_cell_never_runs_as_a_formula() -> None:
    columns = [sheets.Column("Note"), sheets.Column("Map", "link")]
    content = sheets.csv_bytes(columns, [["=HYPERLINK(1)", sheets.Link("https://x.test")]])
    text = content.decode("utf-8-sig")
    assert "'=HYPERLINK(1)" in text
    assert "https://x.test" in text


async def test_the_key_never_reaches_the_request_log(caplog) -> None:
    body = {"status": "OK", "results": [{"types": ["route"], "formatted_address": "MG Road"}]}
    with caplog.at_level(logging.INFO, logger="httpx"):
        async with _client(body) as client:
            await maps.reverse_geocode(client, Decimal("12.9716"), Decimal("77.5946"))
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert lines, "httpx logged no request line"
    assert all("test-key" not in line for line in lines)
    assert any("key=%2A%2A%2A" in line or "key=***" in line for line in lines)
