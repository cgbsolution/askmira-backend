"""Files in a private Supabase Storage bucket, over its REST API.

No SDK: the calls below are plain HTTP through httpx, which the app already
uses, and the Supabase client libraries would bring Supabase Auth with them.

The bucket is private and the service-role key never leaves the server. Every
download still goes through the API, which checks who is asking and writes the
audit row, so no signed or public URL is ever issued: Supabase never serves a
file to a browser directly.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.services.storage.base import FileStorage, StorageError, check_key

__all__ = ["SupabaseStorage"]

logger = logging.getLogger(__name__)

#: A 10 MB upload over a slow link, with room to spare. Connecting is quick or
#: not at all, so that part fails fast.
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

#: The bucket name goes into the URL path, so it is held to a strict shape.
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.\-]{1,62}$")


class SupabaseStorage(FileStorage):
    """A private bucket in the project's Supabase Storage."""

    def __init__(
        self,
        url: str | None,
        service_key: str | None,
        bucket: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = (url or "").rstrip("/")
        self._key = service_key or ""
        self._bucket = bucket
        # Tests hand in an httpx.MockTransport; production leaves it unset.
        self._transport = transport

    @property
    def bucket(self) -> str:
        return self._bucket

    def describe(self) -> str:
        if not self._url or not self._key:
            return (
                f"Supabase Storage bucket '{self._bucket}' - NOT CONFIGURED, uploads will fail "
                "until SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set"
            )
        return f"Supabase Storage bucket '{self._bucket}' at {self._url}"

    # ── Operations ──────────────────────────────────────────────────────────

    async def put(self, key: str, data: bytes) -> None:
        # Every object is sent as opaque bytes: some are encrypted before they
        # arrive here, whatever their extension says, and none is ever served by
        # Supabase, so a content type would only be a claim nobody checks.
        response = await self._send(
            "POST",
            self._object(key),
            content=data,
            headers={"content-type": "application/octet-stream", "x-upsert": "true"},
        )
        if not response.is_success:
            raise _failure(response, "stored")

    async def get(self, key: str) -> bytes | None:
        response = await self._send("GET", self._object(key))
        if response.is_success:
            return response.content
        if _is_missing(response):
            return None
        raise _failure(response, "read")

    async def exists(self, key: str) -> bool:
        response = await self._send("HEAD", self._object(key))
        if response.is_success:
            return True
        # A HEAD response has no body to say why, and Supabase has answered a
        # missing object with 400 as well as 404.
        if response.status_code in (400, 404):
            return False
        raise _failure(response, "checked")

    async def delete(self, key: str) -> bool:
        response = await self._send(
            "DELETE", f"/object/{self._checked_bucket()}", json={"prefixes": [check_key(key)]}
        )
        if not response.is_success:
            raise _failure(response, "removed")
        # The answer lists what was removed: empty when nothing was there.
        removed = response.json()
        return isinstance(removed, list) and len(removed) > 0

    # ── Bucket ──────────────────────────────────────────────────────────────

    async def ensure_private_bucket(self) -> str:
        """Create the bucket if it is missing. Returns "created" or "exists".

        Refuses a bucket that exists but is public: these are ID proofs,
        payslips and tax documents.
        """
        bucket = self._checked_bucket()
        response = await self._send("GET", f"/bucket/{bucket}")
        if response.is_success:
            if _body(response).get("public"):
                raise StorageError(
                    f"The bucket '{bucket}' is public. Make it private in the Supabase "
                    "dashboard (Storage -> bucket -> Edit) before storing anything in it."
                )
            return "exists"
        if not _is_missing(response):
            raise _failure(response, "checked")

        response = await self._send(
            "POST", "/bucket", json={"id": bucket, "name": bucket, "public": False}
        )
        if not response.is_success:
            raise _failure(response, "created")
        return "created"

    # ── Plumbing ────────────────────────────────────────────────────────────

    def _checked_bucket(self) -> str:
        if not _BUCKET_PATTERN.match(self._bucket):
            raise StorageError(f"'{self._bucket}' is not a valid Supabase Storage bucket name")
        return self._bucket

    def _object(self, key: str) -> str:
        return f"/object/{self._checked_bucket()}/{check_key(key)}"

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._url or not self._key:
            raise StorageError(
                "Supabase Storage is not configured: SUPABASE_URL or "
                "SUPABASE_SERVICE_ROLE_KEY is not set."
            )
        async with httpx.AsyncClient(
            base_url=f"{self._url}/storage/v1",
            headers={"apikey": self._key, "Authorization": f"Bearer {self._key}"},
            timeout=_TIMEOUT,
            transport=self._transport,
        ) as client:
            try:
                return await client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                logger.warning("Supabase Storage %s %s failed: %r", method, path, exc)
                raise StorageError(
                    "The file storage service could not be reached. Nothing was changed; try again."
                ) from exc


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _is_missing(response: httpx.Response) -> bool:
    """A missing object or bucket: 404, or 400 with the real status in the body,
    which is how older Supabase Storage versions answer."""
    if response.status_code == 404:
        return True
    return response.status_code == 400 and str(_body(response).get("statusCode")) == "404"


def _failure(response: httpx.Response, verb: str) -> StorageError:
    body = _body(response)
    reason = str(body.get("error") or body.get("message") or "")[:120]
    logger.warning(
        "Supabase Storage %s %s answered %s: %s",
        response.request.method,
        response.request.url.path,
        response.status_code,
        body.get("message") or reason,
    )
    suffix = f" ({reason})" if reason else ""
    return StorageError(
        f"The file could not be {verb}: the storage service answered "
        f"{response.status_code}{suffix}."
    )
