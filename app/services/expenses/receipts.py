"""Receipt file validation. Pure: bytes in, a verdict out.

Nothing about an upload is taken on the client's word:

- **Type** is read from the file's own signature. A `.pdf` that is really an
  HTML page is refused, whatever the browser called it.
- **Size** is capped before anything else looks at the bytes.
- **Images** are opened and verified, so a truncated or crafted file that merely
  starts with the right magic bytes does not pass.
- **PDFs with active content** (JavaScript, launch actions, embedded files) are
  refused. A receipt has no business running anything.
- **The name** is reduced to a harmless label for a list. It is never used as a
  path; the stored key is generated.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass

__all__ = [
    "ALLOWED_TYPES",
    "MAX_BYTES",
    "ReceiptError",
    "ValidatedReceipt",
    "safe_display_name",
    "sniff_content_type",
    "validate_receipt",
]

MAX_BYTES = 5 * 1024 * 1024

#: Content type → the extension it is stored and served under.
ALLOWED_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

_PDF_ACTIVE_CONTENT = (b"/JavaScript", b"/JS ", b"/JS(", b"/JS<", b"/Launch", b"/EmbeddedFile")


class ReceiptError(ValueError):
    """The upload is not an acceptable receipt. `status_code` is the HTTP answer."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ValidatedReceipt:
    content_type: str
    extension: str
    size_bytes: int
    sha256: str


def sniff_content_type(data: bytes) -> str | None:
    """The type the bytes say they are, or None."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _verify_image(data: bytes) -> None:
    try:
        from PIL import Image  # local: optional at import time
    except ImportError:  # pragma: no cover - Pillow is installed in this project
        return
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except Exception as exc:  # Pillow raises a zoo of types for bad input
        raise ReceiptError(
            "This image could not be read. Take the photo again or upload a PDF.", 415
        ) from exc


def validate_receipt(data: bytes, *, max_bytes: int = MAX_BYTES) -> ValidatedReceipt:
    """Accept or refuse one uploaded receipt."""
    if not data:
        raise ReceiptError("The file is empty.", 400)
    if len(data) > max_bytes:
        raise ReceiptError(
            f"The file is larger than {max_bytes // (1024 * 1024)} MB. Upload a smaller "
            "photo or a compressed PDF.",
            413,
        )

    content_type = sniff_content_type(data)
    if content_type is None:
        raise ReceiptError(
            "A receipt must be a PDF, JPEG, PNG or WebP file. This file is none of those, "
            "whatever its name says.",
            415,
        )

    if content_type == "application/pdf":
        if any(marker in data for marker in _PDF_ACTIVE_CONTENT):
            raise ReceiptError(
                "This PDF contains scripts, launch actions or embedded files, which a "
                "receipt never needs. Print it to a plain PDF and upload that.",
                415,
            )
    else:
        _verify_image(data)

    return ValidatedReceipt(
        content_type=content_type,
        extension=ALLOWED_TYPES[content_type],
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()-]+")


def safe_display_name(filename: str | None, extension: str) -> str:
    """A label for the list — never a path, never trusted for type."""
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = _UNSAFE.sub("_", stem).strip(" ._") or "receipt"
    return f"{stem[:80]}{extension}"
