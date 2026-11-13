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

import jwt
from fastapi import Request, Response

from app.core.config import settings
from app.core.security import decode_token

ACCESS_COOKIE = "askmira_access"
REFRESH_COOKIE = "askmira_refresh"
CSRF_COOKIE = "askmira_csrf"
CSRF_HEADER = "X-CSRF-Token"

#: Every account signed in on this browser, for switching between them: their
#: refresh tokens, httpOnly like the session itself, sent only to the auth routes.
ACCOUNTS_COOKIE = "askmira_accounts"
#: A person who runs several workspaces' HR; six refresh tokens stay well under
#: a browser's 4 KB per cookie.
MAX_SAVED_ACCOUNTS = 6
#: Not in a JWT's alphabet (base64url and "."), and a legal cookie character.
_ACCOUNT_SEPARATOR = "~"

#: A client that sends this header with value "cookie" gets its tokens as
#: cookies only; the response body carries empty token strings.
TRANSPORT_HEADER = "X-Auth-Transport"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The refresh cookie is only ever sent to the auth routes.
REFRESH_PATH = f"{settings.API_V1_PREFIX}/auth"


def wants_cookie_transport(request: Request) -> bool:
    return request.headers.get(TRANSPORT_HEADER, "").lower() == "cookie"


def set_session_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    access_seconds: int,
    request: Request | None = None,
) -> None:
    """Start (or renew) the browser's session; with `request`, also remember the account.

    Remembering it is what lets the person switch back to it later without the
    password, from the account menu.
    """
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
    if request is not None:
        remember_account(request, response, refresh_token)


def set_access_cookie(response: Response, *, access_token: str, access_seconds: int) -> None:
    """Replace only the access cookie — entering a client workspace as an operator.

    The refresh cookie is left alone on purpose: it is still the operator's own,
    so when the operator token expires a refresh returns them home.
    """
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=access_seconds,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )


def refresh_subject(token: str | None) -> str | None:
    """Whose refresh token this is, or None when it is not a live refresh token of ours."""
    if not token:
        return None
    try:
        return str(decode_token(token, expected_type="refresh")["sub"])
    except (jwt.InvalidTokenError, KeyError):  # ExpiredSignatureError is an InvalidTokenError
        return None


def saved_account_tokens(request: Request) -> list[str]:
    """The refresh tokens of the accounts saved on this browser, oldest first."""
    raw = request.cookies.get(ACCOUNTS_COOKIE, "")
    # Bounded before anything is decoded: the cookie is the browser's to send.
    return [token for token in raw.split(_ACCOUNT_SEPARATOR) if token][: MAX_SAVED_ACCOUNTS * 2]


def set_saved_account_tokens(response: Response, tokens: list[str]) -> None:
    if not tokens:
        response.delete_cookie(ACCOUNTS_COOKIE, path=REFRESH_PATH)
        return
    response.set_cookie(
        ACCOUNTS_COOKIE,
        _ACCOUNT_SEPARATOR.join(tokens),
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path=REFRESH_PATH,
    )


def merge_account(tokens: list[str], refresh_token: str) -> list[str]:
    """The saved list with this account's newest token in it, each account once.

    An account already saved keeps its place, so the menu does not reshuffle on
    every refresh; a new one goes last. Over the limit, the oldest other account
    is dropped, never the one just signed in. Dead tokens fall out on the way.
    """
    subject = refresh_subject(refresh_token)
    if subject is None:
        return [token for token in tokens if refresh_subject(token) is not None]
    merged: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        owner = refresh_subject(token)
        if owner is None or owner in seen:
            continue
        seen.add(owner)
        merged.append(refresh_token if owner == subject else token)
    if subject not in seen:
        merged.append(refresh_token)
    while len(merged) > MAX_SAVED_ACCOUNTS:
        oldest_other = next(i for i, token in enumerate(merged) if token != refresh_token)
        merged.pop(oldest_other)
    return merged


def remember_account(request: Request, response: Response, refresh_token: str) -> None:
    set_saved_account_tokens(response, merge_account(saved_account_tokens(request), refresh_token))


def without_account(tokens: list[str], user_id: str) -> list[str]:
    return [token for token in tokens if refresh_subject(token) not in (None, user_id)]


def ensure_csrf_cookie(request: Request, response: Response) -> None:
    """A CSRF value for a page with no session yet: the account chooser on sign-in."""
    if request.cookies.get(CSRF_COOKIE):
        return
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        httponly=False,
        secure=settings.is_production,
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
