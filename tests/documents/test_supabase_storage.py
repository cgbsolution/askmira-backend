"""The Supabase Storage backend, against an in-process fake of its REST API.

No network: httpx's MockTransport stands in for Supabase, and the fake answers
the way Storage does, including the older "400 with statusCode 404 in the body"
form of not-found.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.services.storage import (
    LocalFileStorage,
    StorageError,
    SupabaseStorage,
    get_storage,
)

URL = "https://project-ref.supabase.co"
KEY = "service-role-key-for-tests"
BUCKET = "askmira-files"
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
PREFIX = f"/storage/v1/object/{BUCKET}/"


class FakeSupabase:
    """Just enough of Supabase Storage to exercise the backend."""

    def __init__(self, *, legacy_not_found: bool = False, bucket_public: bool | None = False):
        self.objects: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.legacy_not_found = legacy_not_found
        #: None means the bucket does not exist yet.
        self.bucket_public = bucket_public

    def _not_found(self, method: str) -> httpx.Response:
        if self.legacy_not_found:
            body = {"statusCode": "404", "error": "not_found", "message": "Object not found"}
            return httpx.Response(400, json=body) if method != "HEAD" else httpx.Response(400)
        return httpx.Response(404, json={"statusCode": "404", "error": "not_found"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("apikey") != KEY:
            return httpx.Response(403, json={"statusCode": "403", "error": "Unauthorized"})
        path = request.url.path

        if path == f"/storage/v1/bucket/{BUCKET}" and request.method == "GET":
            if self.bucket_public is None:
                return self._not_found("GET")
            return httpx.Response(200, json={"id": BUCKET, "public": self.bucket_public})
        if path == "/storage/v1/bucket" and request.method == "POST":
            self.bucket_public = json.loads(request.content)["public"]
            return httpx.Response(200, json={"name": BUCKET})
        if path == f"/storage/v1/object/{BUCKET}" and request.method == "DELETE":
            names = json.loads(request.content)["prefixes"]
            removed = [{"name": n} for n in names if self.objects.pop(n, None) is not None]
            return httpx.Response(200, json=removed)

        assert path.startswith(PREFIX), path
        key = path[len(PREFIX) :]
        if request.method == "POST":
            self.objects[key] = request.content
            return httpx.Response(200, json={"Key": f"{BUCKET}/{key}"})
        if key not in self.objects:
            return self._not_found(request.method)
        if request.method == "GET":
            return httpx.Response(200, content=self.objects[key])
        if request.method == "HEAD":
            return httpx.Response(200)
        raise AssertionError(f"unexpected {request.method} {path}")


def _storage(fake, *, url: str | None = URL, key: str | None = KEY) -> SupabaseStorage:
    return SupabaseStorage(url, key, BUCKET, transport=httpx.MockTransport(fake))


# ── Operations ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("legacy_not_found", [False, True])
async def test_round_trip_and_delete(legacy_not_found: bool) -> None:
    fake = FakeSupabase(legacy_not_found=legacy_not_found)
    storage = _storage(fake)
    key = storage.new_key(uuid.uuid4(), "documents", ".pdf")

    await storage.put(key, PDF)
    assert await storage.get(key) == PDF
    assert await storage.exists(key)
    assert await storage.delete(key) is True
    assert await storage.get(key) is None
    assert not await storage.exists(key)
    assert await storage.delete(key) is False


async def test_every_request_carries_the_service_key_and_an_upload_overwrites() -> None:
    fake = FakeSupabase()
    storage = _storage(fake)
    key = storage.new_key(uuid.uuid4(), "letters", ".pdf")

    await storage.put(key, PDF)
    await storage.put(key, PDF + b"re-rendered")

    assert await storage.get(key) == PDF + b"re-rendered"
    upload = fake.requests[0]
    assert upload.method == "POST"
    assert upload.url == httpx.URL(f"{URL}/storage/v1/object/{BUCKET}/{key}")
    assert upload.headers["x-upsert"] == "true"
    assert upload.headers["content-type"] == "application/octet-stream"
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in fake.requests)


async def test_a_legacy_letter_key_is_still_addressable() -> None:
    storage = _storage(FakeSupabase())
    key = f"letters/{uuid.uuid4()}/ACM_OFF_2026-27_00001.pdf"
    await storage.put(key, PDF)
    assert await storage.get(key) == PDF


@pytest.mark.parametrize("key", ["../outside.pdf", "a/../../b.pdf", "/etc/passwd", "a", "a/b c"])
async def test_a_malformed_key_is_refused_before_any_request(key: str) -> None:
    fake = FakeSupabase()
    storage = _storage(fake)
    for operation in (storage.get, storage.exists, storage.delete):
        with pytest.raises(StorageError):
            await operation(key)
    with pytest.raises(StorageError):
        await storage.put(key, PDF)
    assert fake.requests == []


@pytest.mark.parametrize(("url", "key"), [(None, KEY), (URL, None), ("", "")])
async def test_missing_configuration_fails_loudly_without_a_request(url, key) -> None:
    fake = FakeSupabase()
    storage = _storage(fake, url=url, key=key)
    with pytest.raises(StorageError, match="SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY"):
        await storage.put("t/documents/a.pdf", PDF)
    assert fake.requests == []
    assert "NOT CONFIGURED" in storage.describe()


async def test_a_refusal_is_an_error_not_a_missing_file() -> None:
    """A 400 that is not "not found" must not read as an absent file."""

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"statusCode": "400", "error": "InvalidKey"})

    storage = SupabaseStorage(URL, KEY, BUCKET, transport=httpx.MockTransport(refuse))
    with pytest.raises(StorageError, match="400"):
        await storage.get("t/documents/a.pdf")


async def test_a_wrong_key_is_reported_without_leaking_the_key() -> None:
    storage = _storage(FakeSupabase(), key="not-the-service-key")
    with pytest.raises(StorageError, match="403") as caught:
        await storage.put("t/documents/a.pdf", PDF)
    assert "not-the-service-key" not in str(caught.value)
    assert KEY not in _storage(FakeSupabase()).describe()


async def test_an_unreachable_service_is_a_storage_error() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    storage = SupabaseStorage(URL, KEY, BUCKET, transport=httpx.MockTransport(unreachable))
    with pytest.raises(StorageError, match="could not be reached"):
        await storage.get("t/documents/a.pdf")


async def test_a_bucket_name_that_could_change_the_path_is_refused() -> None:
    fake = FakeSupabase()
    storage = SupabaseStorage(URL, KEY, "../other", transport=httpx.MockTransport(fake))
    with pytest.raises(StorageError, match="bucket name"):
        await storage.get("t/documents/a.pdf")
    assert fake.requests == []


# ── Bucket ──────────────────────────────────────────────────────────────────


async def test_a_missing_bucket_is_created_private() -> None:
    fake = FakeSupabase(bucket_public=None)
    assert await _storage(fake).ensure_private_bucket() == "created"
    assert fake.bucket_public is False
    assert await _storage(fake).ensure_private_bucket() == "exists"


async def test_a_public_bucket_is_refused() -> None:
    with pytest.raises(StorageError, match="is public"):
        await _storage(FakeSupabase(bucket_public=True)).ensure_private_bucket()


# ── Selection ───────────────────────────────────────────────────────────────


def test_the_backend_follows_the_setting(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    assert isinstance(get_storage(), LocalFileStorage)

    monkeypatch.setattr(settings, "STORAGE_BACKEND", "supabase")
    monkeypatch.setattr(settings, "SUPABASE_URL", URL)
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_ROLE_KEY", KEY)
    storage = get_storage()
    assert isinstance(storage, SupabaseStorage)
    assert storage.bucket == settings.SUPABASE_STORAGE_BUCKET


def test_the_setting_is_case_insensitive_and_a_typo_fails_at_boot() -> None:
    assert Settings(_env_file=None, STORAGE_BACKEND=" Supabase ").STORAGE_BACKEND == "supabase"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, STORAGE_BACKEND="supabse")
