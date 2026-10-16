"""OIDC authorization code flow with PKCE.

Built on PyJWT and httpx rather than a framework integration, because the
security-relevant steps are the ones worth having in plain sight:

- **PKCE** on every flow, not only public clients. It costs nothing and it
  removes authorization-code interception entirely.
- **`state`** is generated server-side and stored. Comparing a value the client
  sent against a value the same client sent proves nothing; it has to be
  checked against server state.
- **`nonce`** is likewise stored and compared against the id token's claim.
  This is what stops an id token minted for a different session being replayed
  into ours.
- **Signature verified against the provider's JWKS**, with the issuer and
  audience checked. An unverified id token is a JSON object an attacker wrote.
- **No role or group claim is read.** Roles are assigned in AskMira. This
  module extracts identity only.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

__all__ = [
    "OidcDiscovery",
    "OidcError",
    "OidcIdentity",
    "OidcLoginStart",
    "build_authorization_url",
    "discover",
    "exchange_code",
    "start_login",
]

HTTP_TIMEOUT = 15.0


class OidcError(RuntimeError):
    """Discovery, token exchange or id-token validation failed."""


@dataclass(frozen=True, slots=True)
class OidcDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None
    end_session_endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class OidcLoginStart:
    """What the caller must persist before redirecting the browser."""

    authorization_url: str
    state: str
    nonce: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class OidcIdentity:
    """Identity extracted from a verified id token.

    No roles, no groups. See the module docstring.
    """

    subject: str
    issuer: str
    claims: dict[str, Any] = field(default_factory=dict)

    def claim(self, name: str) -> str | None:
        value = self.claims.get(name)
        if value is None:
            return None
        return str(value[0]) if isinstance(value, list) and value else str(value)


async def discover(discovery_url: str) -> OidcDiscovery:
    """Fetch the provider's OpenID configuration.

    Azure AD, Okta and Google all publish this; hard-coding endpoints per
    vendor is how integrations break when a tenant migrates.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            response = await client.get(discovery_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OidcError(
                f"Could not reach the provider's discovery document: {exc}"
            ) from exc

    document = response.json()
    required = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
    missing = [key for key in required if key not in document]
    if missing:
        raise OidcError(
            f"The discovery document is missing {', '.join(missing)}. "
            "This does not look like an OpenID Connect provider."
        )

    return OidcDiscovery(
        issuer=document["issuer"],
        authorization_endpoint=document["authorization_endpoint"],
        token_endpoint=document["token_endpoint"],
        jwks_uri=document["jwks_uri"],
        userinfo_endpoint=document.get("userinfo_endpoint"),
        end_session_endpoint=document.get("end_session_endpoint"),
    )


def _pkce_pair() -> tuple[str, str]:
    """Return `(verifier, challenge)` for S256 PKCE."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    scopes: tuple[str, ...] = ("openid", "email", "profile"),
) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    separator = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{separator}{urlencode(query)}"


def start_login(
    *, discovery: OidcDiscovery, client_id: str, redirect_uri: str
) -> OidcLoginStart:
    """Begin a flow. The caller stores state, nonce and verifier server-side."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()

    return OidcLoginStart(
        authorization_url=build_authorization_url(
            authorization_endpoint=discovery.authorization_endpoint,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            code_challenge=challenge,
        ),
        state=state,
        nonce=nonce,
        code_verifier=verifier,
    )


async def exchange_code(
    *,
    discovery: OidcDiscovery,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    expected_nonce: str,
) -> OidcIdentity:
    """Exchange an authorization code and validate the id token.

    Raises:
        OidcError: on a failed exchange, an unverifiable token, or a nonce
            mismatch. A nonce mismatch specifically means the token was minted
            for a different login attempt.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            response = await client.post(discovery.token_endpoint, data=data)
        except httpx.HTTPError as exc:
            raise OidcError(f"Could not reach the token endpoint: {exc}") from exc

    if response.status_code >= 400:
        raise OidcError(
            f"The identity provider rejected the code exchange "
            f"({response.status_code}): {response.text[:300]}"
        )

    payload = response.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise OidcError("The token response contained no id_token")

    claims = verify_id_token(
        id_token=id_token,
        jwks_uri=discovery.jwks_uri,
        issuer=discovery.issuer,
        audience=client_id,
        expected_nonce=expected_nonce,
    )

    return OidcIdentity(
        subject=claims["sub"], issuer=claims["iss"], claims=claims
    )


def verify_id_token(
    *,
    id_token: str,
    jwks_uri: str,
    issuer: str,
    audience: str,
    expected_nonce: str | None,
) -> dict[str, Any]:
    """Verify signature, issuer, audience, expiry and nonce.

    Split out from `exchange_code` so it is testable without a live provider.
    """
    try:
        signing_key = PyJWKClient(jwks_uri, cache_keys=True).get_signing_key_from_jwt(
            id_token
        )
    except Exception as exc:
        raise OidcError(f"Could not fetch the provider's signing key: {exc}") from exc

    # Microsoft's multi-tenant endpoint publishes its issuer as a template,
    # `https://login.microsoftonline.com/{tenantid}/v2.0`. The token's own `tid`
    # fills it in; the signature is still verified against Microsoft's keys, so
    # an issuer that does not match its own tenant id is refused.
    if "{tenantid}" in issuer:
        try:
            unverified = jwt.decode(id_token, options={"verify_signature": False})
        except jwt.InvalidTokenError as exc:
            raise OidcError(f"The id token is not valid: {exc}") from exc
        tenant_id = str(unverified.get("tid") or "")
        if not tenant_id:
            raise OidcError("The id token carries no tenant id to check its issuer against.")
        issuer = issuer.replace("{tenantid}", tenant_id)

    try:
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise OidcError("The id token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise OidcError(f"The id token is not valid: {exc}") from exc

    if expected_nonce is not None:
        supplied = claims.get("nonce")
        if not supplied or not secrets.compare_digest(str(supplied), expected_nonce):
            raise OidcError(
                "The id token's nonce does not match this login attempt. The token was "
                "issued for a different session."
            )

    return claims
