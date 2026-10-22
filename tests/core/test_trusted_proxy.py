"""The portal's forwarded client address is believed only with the shared secret."""

from __future__ import annotations

import asyncio

from app.core.proxy import TrustedProxyMiddleware

SECRET = "s3cret-shared-with-the-portal-0123456789"
VERCEL = ("76.76.21.21", 443)


def _client_seen(headers: dict[str, str], *, secret: str | None = SECRET) -> tuple[str, int]:
    seen: dict[str, tuple[str, int]] = {}

    async def app(scope, receive, send) -> None:
        seen["client"] = scope["client"]

    scope = {
        "type": "http",
        "client": VERCEL,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(TrustedProxyMiddleware(app, secret=secret)(scope, None, None))
    return seen["client"]


def test_the_portal_vouches_for_the_address() -> None:
    client = _client_seen(
        {"x-askmira-proxy-secret": SECRET, "x-askmira-client-ip": "203.0.113.7"}
    )

    assert client == ("203.0.113.7", 443)


def test_ipv6_is_accepted() -> None:
    client = _client_seen(
        {"x-askmira-proxy-secret": SECRET, "x-askmira-client-ip": "2406:da1a::e36f"}
    )

    assert client[0] == "2406:da1a::e36f"


def test_a_wrong_secret_is_ignored() -> None:
    client = _client_seen(
        {"x-askmira-proxy-secret": "guess", "x-askmira-client-ip": "203.0.113.7"}
    )

    assert client == VERCEL


def test_an_address_without_the_secret_is_ignored() -> None:
    assert _client_seen({"x-askmira-client-ip": "203.0.113.7"}) == VERCEL


def test_something_that_is_not_an_address_is_ignored() -> None:
    client = _client_seen(
        {"x-askmira-proxy-secret": SECRET, "x-askmira-client-ip": "203.0.113.7, 10.0.0.1"}
    )

    assert client == VERCEL


def test_with_no_secret_configured_nothing_is_believed() -> None:
    client = _client_seen(
        {"x-askmira-proxy-secret": "", "x-askmira-client-ip": "203.0.113.7"}, secret=None
    )

    assert client == VERCEL
