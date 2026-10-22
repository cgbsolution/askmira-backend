"""The real client address when the portal's proxy forwards a request.

The browser calls the portal (Vercel), which forwards `/api/v1/*` here. To this
API every such request comes from one of Vercel's addresses, so everything
keyed on `request.client` treats all users as one: sign-in rate limits run out
for everybody at once, the audit log records Vercel, and a company's IP
allowlist sees Vercel instead of the office.

The portal therefore adds two headers: the address Vercel saw, and a secret
shared with this API. Only with the right secret is the address believed.
Anyone can call this API directly and set any header; without the secret the
headers are ignored and the socket peer stands, as before.
"""

from __future__ import annotations

import hmac
import ipaddress

from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["CLIENT_IP_HEADER", "PROXY_SECRET_HEADER", "TrustedProxyMiddleware"]

CLIENT_IP_HEADER = b"x-askmira-client-ip"
PROXY_SECRET_HEADER = b"x-askmira-proxy-secret"


class TrustedProxyMiddleware:
    """Replaces the client address with the one the portal vouches for."""

    def __init__(self, app: ASGIApp, *, secret: str | None) -> None:
        self.app = app
        self._secret = secret.encode() if secret else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._secret and scope["type"] in ("http", "websocket"):
            address = self._vouched_address(scope)
            if address is not None:
                port = scope["client"][1] if scope.get("client") else 0
                scope["client"] = (address, port)
        await self.app(scope, receive, send)

    def _vouched_address(self, scope: Scope) -> str | None:
        secret = address = None
        for name, value in scope.get("headers", []):
            if name == PROXY_SECRET_HEADER:
                secret = value
            elif name == CLIENT_IP_HEADER:
                address = value
        if secret is None or address is None:
            return None
        if not hmac.compare_digest(secret, self._secret):
            return None
        try:
            return str(ipaddress.ip_address(address.decode("latin-1").strip()))
        except ValueError:
            return None
