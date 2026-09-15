"""Session cookies for the web app.

The browser never holds a token it can read. The access and refresh tokens live
in `httpOnly` cookies, so a script injected into the page — by a dependency, a
browser extension, or a bug in our own rendering — cannot lift a session.

Cookie auth brings cross-site request forgery with it, so it comes with a
double-submit token: a random value in a cookie the page *can* read, which the
page echoes in a header on every state-changing request. Another site can make
the browser send our cookies; it cannot read the value to put in the header.

Bearer tokens keep working for the mobile app and scripts. A request that
authenticates with a Bearer header is not a CSRF risk, since a browser never
attaches one on its own.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Request, Response

from app.core.config import settings

ACCESS_COOKIE = "askmira_access"
REFRESH_COOKIE = "askmira_refresh"
CSRF_COOKIE = "askmira_csrf"
CSRF_HEADER = "X-CSRF-Token"

#: A client that sends this header with value "cookie" gets its tokens as
#: cookies only; the response body carries empty token strings.
TRANSPORT_HEADER = "X-Auth-Transport"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The refresh cookie is only ever sent to the auth routes.
REFRESH_PATH = f"{settings.API_V1_PREFIX}/auth"


def wants_cookie_transport(request: Request) -> bool:
    return request.headers.get(TRANSPORT_HEADER, "").lower() == "cookie"


def set_session_cookies(
    response: Response, *, access_token: str, refresh_token: str, access_seconds: int
) -> None:
    secure = settings.is_production
    refresh_seconds = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=access_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=refresh_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path=REFRESH_PATH,
    )
    # Readable by the page on purpose: the page copies it into CSRF_HEADER.
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=refresh_seconds,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH)
    response.delete_cookie(CSRF_COOKIE, path="/")


def csrf_ok(request: Request) -> bool:
    """The double-submit check for a cookie-authenticated, state-changing request."""
    if request.method in SAFE_METHODS:
        return True
    sent = request.headers.get(CSRF_HEADER)
    expected = request.cookies.get(CSRF_COOKIE)
    return bool(sent and expected and hmac.compare_digest(sent, expected))
