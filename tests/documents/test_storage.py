"""The storage adapter and upload validation, without a database."""

from __future__ import annotations

import uuid

import pytest

from app.services.storage import (
    FileRejectedError,
    LocalFileStorage,
    StorageError,
    sniff,
    validate_upload,
)

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"


# ── Sniffing ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "mime"),
    [(PDF, "application/pdf"), (PNG, "image/png"), (JPEG, "image/jpeg")],
)
def test_accepted_types_are_recognised_from_their_bytes(data: bytes, mime: str) -> None:
    kind = sniff(data)
    assert kind is not None and kind.mime_type == mime


def test_html_renamed_to_pdf_is_refused() -> None:
    with pytest.raises(FileRejectedError, match="not accepted"):
        validate_upload(b"<html><script>alert(1)</script></html>", max_bytes=1024)


def test_an_svg_is_refused_even_though_it_is_an_image() -> None:
    with pytest.raises(FileRejectedError):
        validate_upload(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', max_bytes=1024)


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(FileRejectedError, match="empty"):
        validate_upload(b"", max_bytes=1024)


def test_an_oversized_file_is_refused_for_its_size() -> None:
    with pytest.raises(FileRejectedError, match="limit"):
        validate_upload(PDF + b"\x00" * 2048, max_bytes=1024)


def test_a_file_at_the_limit_is_accepted() -> None:
    data = PDF + b"\x00" * (1024 - len(PDF))
    assert validate_upload(data, max_bytes=1024).extension == ".pdf"


# ── Local storage ───────────────────────────────────────────────────────────


async def test_round_trip_and_delete(tmp_path) -> None:
    storage = LocalFileStorage(tmp_path)
    key = storage.new_key(uuid.uuid4(), "documents", ".pdf")
    await storage.put(key, PDF)
    assert await storage.get(key) == PDF
    assert await storage.exists(key)
    assert await storage.delete(key) is True
    assert await storage.get(key) is None
    assert await storage.delete(key) is False


def test_generated_keys_are_unique_and_carry_no_client_name(tmp_path) -> None:
    storage = LocalFileStorage(tmp_path)
    tenant = uuid.uuid4()
    keys = {storage.new_key(tenant, "documents", ".pdf") for _ in range(200)}
    assert len(keys) == 200
    assert all(key.startswith(f"{tenant}/documents/") for key in keys)


@pytest.mark.parametrize(
    "key",
    ["../outside.pdf", "a/../../outside.pdf", "/etc/passwd", "a\\b.pdf", "", "a", "a/b c.pdf"],
)
async def test_a_key_cannot_leave_the_root(tmp_path, key: str) -> None:
    storage = LocalFileStorage(tmp_path / "root")
    with pytest.raises(StorageError):
        await storage.put(key, PDF)


@pytest.mark.parametrize(
    ("namespace", "extension"), [("../x", ".pdf"), ("docs", "pdf"), ("docs", ".p/f")]
)
def test_a_malformed_namespace_or_extension_is_refused(namespace: str, extension: str) -> None:
    with pytest.raises(StorageError):
        LocalFileStorage.new_key(uuid.uuid4(), namespace, extension)


async def test_a_legacy_letter_key_is_still_addressable(tmp_path) -> None:
    """Letters issued before the adapter recorded keys like this one."""
    storage = LocalFileStorage(tmp_path)
    key = f"letters/{uuid.uuid4()}/ACM_OFF_2026-27_00001.pdf"
    await storage.put(key, PDF)
    assert await storage.get(key) == PDF
