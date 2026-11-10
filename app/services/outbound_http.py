"""The HTTPS transport behind the calendar and job board adapters.

Those adapters are written against a small `Transport` protocol — one
`request(method, path, json=, headers=) -> (status, body)` call — so their
mapping can be tested without a network. Until this module existed nothing in
production supplied one, and every booking or posting stopped with "no HTTP
transport is configured". This is the one that reaches the network.

What it guarantees, because the adapters rely on it:

- **https only.** A relative path joins the adapter's base address. An absolute
  address (a résumé link a board hands back) is followed only when its host is
  one of the adapter's own hosts, so a bearer token is never sent anywhere else.
  Redirects are followed by hand on GET, three at most, under the same rule, and
  the Authorization header is dropped when a redirect changes host.
- **Failures come back as status codes, never as exceptions.** Each adapter
  already turns a status into its own typed failure (transient or permanent), so
  a network error is a 503 ("try again") and a refused address a 400 ("this will
  never work"), each with a message a person can read.
- **One body shape.** A JSON object is returned as is; a JSON array as
  `{"items": [...]}`; an empty body as `{}`; anything else (a PDF résumé) as
  `{"content": bytes}`. On a 429 the `Retry-After` header is copied into
  `retryAfter` when the body has none, so the adapters' back-off hears it.
- **A size cap.** A response over `MAX_BYTES` is refused rather than read.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

__all__ = ["MAX_BYTES", "HttpsTransport", "TransportConfigError", "resolve_base_url"]

#: Large enough for a scanned résumé, small enough that a misbehaving server
#: cannot fill the worker's memory.
MAX_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 20.0


class TransportConfigError(ValueError):
    """The base address is unusable: not https, or not one of the provider's hosts."""


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


def resolve_base_url(
    default: str | None, override: str | None, allowed_hosts: tuple[str, ...], *, label: str
) -> str:
    """The base address an adapter calls: its own, or a customer-supplied one.

    A customer may point an adapter at another address (a board that issues an
    API host with the contract), but only an https address on one of the
    provider's own hosts. Anything else would send the customer's login to a
    server that is not the provider's.
    """
    chosen = (override or "").strip() or (default or "")
    if not chosen:
        raise TransportConfigError(
            f"No API address is set for {label}. Add the address the provider gave you "
            "with your API access, then try again. Nothing was sent."
        )
    parsed = urlparse(chosen)
    if parsed.scheme != "https" or not parsed.hostname:
        raise TransportConfigError(
            f"The API address for {label} must start with https://. Nothing was sent."
        )
    if allowed_hosts and not _host_allowed(parsed.hostname, allowed_hosts):
        raise TransportConfigError(
            f"{parsed.hostname} is not an address of {label} "
            f"({', '.join(allowed_hosts)}). Nothing was sent."
        )
    return chosen.rstrip("/")


def _body(response: httpx.Response) -> dict[str, Any]:
    raw = response.content
    if not raw:
        return {}
    kind = response.headers.get("content-type", "").lower()
    if "json" in kind or raw[:1] in (b"{", b"["):
        try:
            parsed = jsonlib.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    if kind.startswith("text/") and response.status_code >= 400:
        return {"message": raw[:500].decode("utf-8", errors="replace")}
    return {"content": raw}


class HttpsTransport:
    """`Transport` over httpx, bound to one provider's base address and hosts."""

    def __init__(
        self,
        base_url: str,
        *,
        allowed_hosts: tuple[str, ...],
        label: str,
        timeout: float = TIMEOUT_SECONDS,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = resolve_base_url(base_url, None, allowed_hosts, label=label)
        self._hosts = allowed_hosts
        self._label = label
        self._timeout = timeout
        # Injected only by tests (httpx.MockTransport); production uses the network.
        self._http_transport = http_transport

    @property
    def base_url(self) -> str:
        return self._base

    def _target(self, path: str) -> str | None:
        """The full address for a path, or None when it is not ours to call."""
        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = self._base + (path if path.startswith("/") else "/" + path)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        if not _host_allowed(parsed.hostname, self._hosts):
            return None
        return url

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        headers: dict | None = None,
        form: dict | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = self._target(path)
        if url is None:
            return 400, {
                "message": (
                    f"Refused to call {urlparse(path).hostname or path}: it is not an "
                    f"address of {self._label}, so no credentials were sent there."
                )
            }
        send_headers = dict(headers or {})
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._http_transport,
            ) as client:
                for _hop in range(MAX_REDIRECTS + 1):
                    response = await client.request(
                        method, url, json=json, data=form, headers=send_headers
                    )
                    if method.upper() != "GET" or response.status_code not in (
                        301,
                        302,
                        303,
                        307,
                        308,
                    ):
                        break
                    location = response.headers.get("location")
                    if not location:
                        break
                    following = urljoin(url, location)
                    next_host = urlparse(following).hostname or ""
                    if urlparse(following).scheme != "https" or not _host_allowed(
                        next_host, self._hosts
                    ):
                        return 400, {
                            "message": (
                                f"{self._label} redirected to {next_host or following}, which "
                                "is not one of its addresses. Nothing was fetched from there."
                            )
                        }
                    if next_host != urlparse(url).hostname:
                        send_headers.pop("Authorization", None)
                        send_headers.pop("authorization", None)
                    url = following
                else:
                    return 502, {"message": f"{self._label} redirected too many times."}
        except httpx.TimeoutException:
            return 503, {"message": f"{self._label} did not answer in time. Try again shortly."}
        except httpx.HTTPError as exc:
            return 503, {
                "message": (
                    f"Could not reach {self._label} ({type(exc).__name__}). Try again shortly."
                )
            }

        declared = response.headers.get("content-length")
        if (declared and declared.isdigit() and int(declared) > MAX_BYTES) or len(
            response.content
        ) > MAX_BYTES:
            return 413, {
                "message": (
                    f"{self._label} sent more than {MAX_BYTES // (1024 * 1024)} MB; not read."
                )
            }

        body = _body(response)
        if response.status_code == 429 and "retryAfter" not in body:
            retry = response.headers.get("retry-after", "")
            if retry.isdigit():
                body["retryAfter"] = int(retry)
        return response.status_code, body
