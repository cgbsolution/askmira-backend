"""The live HTTPS transport behind the calendar and job board adapters.

Before it existed nothing in production handed those adapters a transport, so
every booking and posting stopped with "no HTTP transport is configured". These
tests pin what it promises: the right address, credentials never sent to a host
that is not the provider's, failures as status codes the adapters already map,
and the calendar token renewed instead of expiring an hour after connecting.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.interview import CalendarProviderKind
from app.models.jobboard import JobBoard
from app.services.calendar import registry as calendar_registry
from app.services.calendar.base import CalendarProvider
from app.services.interviews import calendar_oauth
from app.services.jobboards import registry as board_registry
from app.services.jobboards.base import BoardCredentials
from app.services.outbound_http import HttpsTransport, TransportConfigError, resolve_base_url


def _transport(handler, *, hosts=("api.example.com",), base="https://api.example.com"):
    return HttpsTransport(
        base, allowed_hosts=hosts, label="Example", http_transport=httpx.MockTransport(handler)
    )


# ── The transport ───────────────────────────────────────────────────────────


async def test_a_relative_path_joins_the_base_and_json_comes_back_as_a_dict() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "E1"})

    code, body = await _transport(handler).request(
        "POST", "/v1/things?x=1", json={"a": 1}, headers={"Authorization": "Bearer t"}
    )
    assert code == 200 and body == {"id": "E1"}
    assert seen["url"] == "https://api.example.com/v1/things?x=1"
    assert seen["auth"] == "Bearer t"


async def test_a_foreign_host_is_refused_without_sending_anything() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    code, body = await _transport(handler).request(
        "GET", "https://evil.example.net/cv.pdf", headers={"Authorization": "Bearer t"}
    )
    assert code == 400 and "no credentials were sent" in body["message"]
    assert calls == []


async def test_plain_http_is_refused() -> None:
    code, _ = await _transport(lambda r: httpx.Response(200)).request(
        "GET", "http://api.example.com/x"
    )
    assert code == 400


async def test_a_redirect_off_the_providers_hosts_is_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.net/x"})

    code, body = await _transport(handler).request("GET", "/cv")
    assert code == 400 and "not one of its addresses" in body["message"]


async def test_a_redirect_to_another_provider_host_drops_the_token() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "api.example.com":
            return httpx.Response(302, headers={"location": "https://media.example.com/cv.pdf"})
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})

    code, body = await _transport(handler, hosts=("example.com",)).request(
        "GET", "/cv", headers={"Authorization": "Bearer t"}
    )
    assert code == 200 and body == {"content": b"%PDF-1.4"}
    assert seen == [("api.example.com", "Bearer t"), ("media.example.com", None)]


async def test_body_shapes_list_empty_and_retry_after() -> None:
    responses = iter(
        [
            httpx.Response(200, json=[1, 2]),
            httpx.Response(204),
            httpx.Response(429, json={"message": "slow down"}, headers={"retry-after": "42"}),
        ]
    )
    transport = _transport(lambda r: next(responses))
    assert await transport.request("GET", "/a") == (200, {"items": [1, 2]})
    assert await transport.request("GET", "/b") == (204, {})
    assert await transport.request("GET", "/c") == (
        429,
        {"message": "slow down", "retryAfter": 42},
    )


async def test_a_network_failure_is_a_503_the_adapters_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    code, body = await _transport(handler).request("GET", "/a")
    assert code == 503 and "Could not reach Example" in body["message"]


def test_a_customer_address_must_be_https_on_the_providers_hosts() -> None:
    assert (
        resolve_base_url(None, "https://api.naukri.com/", ("naukri.com",), label="N")
        == "https://api.naukri.com"
    )
    with pytest.raises(TransportConfigError, match="https"):
        resolve_base_url(None, "http://api.naukri.com", ("naukri.com",), label="N")
    with pytest.raises(TransportConfigError, match="not an address"):
        resolve_base_url(None, "https://naukri.com.evil.net", ("naukri.com",), label="N")
    with pytest.raises(TransportConfigError, match="No API address"):
        resolve_base_url(None, None, ("naukri.com",), label="N")


# ── The registries build it ─────────────────────────────────────────────────


@pytest.mark.parametrize("provider", list(CalendarProvider))
def test_every_calendar_gets_a_live_transport(provider) -> None:
    adapter = calendar_registry.get_adapter(provider)
    assert isinstance(adapter._transport, HttpsTransport)
    assert adapter._transport.base_url.startswith("https://")


def test_linkedin_gets_its_own_address() -> None:
    adapter = board_registry.get_adapter(JobBoard.LINKEDIN)
    assert isinstance(adapter._transport, HttpsTransport)


def test_naukri_needs_the_address_from_its_api_agreement() -> None:
    with pytest.raises(board_registry.AdapterUnavailable, match="No API address"):
        board_registry.get_adapter(
            JobBoard.NAUKRI, credentials=BoardCredentials(username="u", secret="s")
        )
    with pytest.raises(board_registry.AdapterUnavailable, match="not an address"):
        board_registry.get_adapter(
            JobBoard.NAUKRI,
            credentials=BoardCredentials(
                username="u", secret="s", extras={"api_base_url": "https://attacker.example"}
            ),
        )
    adapter = board_registry.get_adapter(
        JobBoard.NAUKRI,
        credentials=BoardCredentials(
            username="u", secret="s", extras={"api_base_url": "https://api.naukri.com"}
        ),
    )
    assert adapter._transport.base_url == "https://api.naukri.com"


def test_a_feed_board_needs_no_transport_and_no_login() -> None:
    adapter = board_registry.get_adapter(JobBoard.INDEED)
    assert adapter._transport is None
    tenant = uuid.uuid4()
    account = SimpleNamespace(
        board=JobBoard.INDEED, credentials_encrypted=None, username=None, settings={}
    )
    creds = board_registry.credentials_for(account, tenant)
    assert creds.secret == ""


# ── Calendar token renewal ──────────────────────────────────────────────────


class _Session:
    async def flush(self) -> None:
        return None


def _connection(tenant_id, *, expires_in_minutes: int, refresh: str | None = "r-1"):
    return SimpleNamespace(
        provider=CalendarProviderKind.GOOGLE,
        revoked_at=None,
        access_token_encrypted=encrypt_secret("old-token", tenant_id),
        refresh_token_encrypted=encrypt_secret(refresh, tenant_id) if refresh else None,
        expires_at=datetime.now(UTC) + timedelta(minutes=expires_in_minutes),
        last_error=None,
    )


@pytest.fixture
def _oauth_env(monkeypatch):
    prefix = "CALENDAR_OAUTH_GOOGLE_"
    monkeypatch.setenv(prefix + "CLIENT_ID", "cid")
    monkeypatch.setenv(prefix + "CLIENT_SECRET", "csecret")
    monkeypatch.setenv(prefix + "AUTHORIZE_URL", "https://auth.example/authorize")
    monkeypatch.setenv(prefix + "TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv(prefix + "SCOPES", "openid email calendar")
    monkeypatch.setenv("CALENDAR_OAUTH_REDIRECT_URI", "https://hr.example/hiring/interviews")


async def test_a_fresh_token_is_used_as_is(_oauth_env) -> None:
    tenant = uuid.uuid4()
    connection = _connection(tenant, expires_in_minutes=50)

    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("refreshed a token that was still good")

    token = await calendar_oauth.usable_access_token(
        _Session(), connection, tenant, transport=httpx.MockTransport(handler)
    )
    assert token == "old-token"


async def test_an_expiring_token_is_renewed_and_the_rotated_refresh_token_kept(_oauth_env) -> None:
    tenant = uuid.uuid4()
    connection = _connection(tenant, expires_in_minutes=2)
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = request.content.decode()
        return httpx.Response(
            200, json={"access_token": "new-token", "refresh_token": "r-2", "expires_in": 3600}
        )

    token = await calendar_oauth.usable_access_token(
        _Session(), connection, tenant, transport=httpx.MockTransport(handler)
    )
    assert token == "new-token"
    assert "grant_type=refresh_token" in sent["body"] and "refresh_token=r-1" in sent["body"]
    assert decrypt_secret(connection.refresh_token_encrypted, tenant) == "r-2"
    assert connection.expires_at > datetime.now(UTC) + timedelta(minutes=50)
    assert connection.last_error is None


async def test_a_refused_renewal_says_reconnect_and_sends_no_stale_token(_oauth_env) -> None:
    tenant = uuid.uuid4()
    connection = _connection(tenant, expires_in_minutes=-5)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(calendar_registry.CalendarPermanentFailure, match="expired"):
        await calendar_oauth.usable_access_token(
            _Session(), connection, tenant, transport=httpx.MockTransport(handler)
        )
    assert "invalid_grant" in connection.last_error
    assert "Reconnect" in connection.last_error
