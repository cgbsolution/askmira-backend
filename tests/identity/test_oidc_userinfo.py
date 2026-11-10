"""OIDC: claims a provider returns only from UserInfo (OpenID Connect Core §5.4).

Found against a certified provider (panva/oidc-provider): the id token carried
no email, so a valid sign-in was refused with "no email". The email is now read
from UserInfo — only for the same subject as the verified id token.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.identity import oidc

DISCOVERY = oidc.OidcDiscovery(
    issuer="https://idp.example",
    authorization_endpoint="https://idp.example/auth",
    token_endpoint="https://idp.example/token",
    jwks_uri="https://idp.example/jwks",
    userinfo_endpoint="https://idp.example/me",
)


def _patch(monkeypatch, userinfo: dict) -> list[str]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/token":
            return httpx.Response(200, json={"id_token": "x.y.z", "access_token": "at-1"})
        assert request.headers["authorization"] == "Bearer at-1"
        return httpx.Response(200, json=userinfo)

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(oidc.httpx, "AsyncClient", client)
    monkeypatch.setattr(
        oidc, "verify_id_token", lambda **_: {"sub": "u-1", "iss": "https://idp.example"}
    )
    return seen


async def _exchange():
    return await oidc.exchange_code(
        discovery=DISCOVERY,
        client_id="c",
        client_secret="s",
        redirect_uri="https://hr.example/signin/sso",
        code="code",
        code_verifier="v",
        expected_nonce="n",
    )


async def test_an_email_only_in_userinfo_is_read_from_there(monkeypatch) -> None:
    seen = _patch(monkeypatch, {"sub": "u-1", "email": "rohan@acme.test", "iss": "spoof"})
    identity = await _exchange()
    assert identity.claim("email") == "rohan@acme.test"
    # A verified id-token claim is never overridden by UserInfo.
    assert identity.issuer == "https://idp.example"
    assert seen == ["/token", "/me"]


async def test_userinfo_for_another_person_is_refused(monkeypatch) -> None:
    _patch(monkeypatch, {"sub": "someone-else", "email": "boss@acme.test"})
    with pytest.raises(oidc.OidcError, match="different person"):
        await _exchange()
