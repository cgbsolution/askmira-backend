"""What a file actually is, decided from its bytes.

A browser's `Content-Type` and a file's name are both whatever the uploader's
machine said. A PDF renamed `.png`, or an HTML page renamed `.pdf`, arrives with
both lies intact. So the vault decides from the first bytes of the file itself
and ignores the other two.

Pure: no I/O, no configuration. The size limit is passed in by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ALLOWED_TYPES",
    "FileRejected",
    "FileRejectedError",
    "SniffedType",
    "sniff",
    "validate_upload",
]


class FileRejectedError(ValueError):
    """The file is not one the vault stores."""


#: The name other modules already import. Kept so they do not break.
FileRejected = FileRejectedError


@dataclass(frozen=True, slots=True)
class SniffedType:
    mime_type: str
    extension: str
    label: str


#: Magic numbers of the only types the vault accepts. Deliberately short: every
#: type added is another parser somebody's browser runs on a colleague's upload.
_SIGNATURES: tuple[tuple[bytes, SniffedType], ...] = (
    (b"%PDF-", SniffedType("application/pdf", ".pdf", "PDF")),
    (b"\x89PNG\r\n\x1a\n", SniffedType("image/png", ".png", "PNG image")),
    (b"\xff\xd8\xff", SniffedType("image/jpeg", ".jpg", "JPEG image")),
)

ALLOWED_TYPES: tuple[str, ...] = tuple(sig.label for _, sig in _SIGNATURES)


def sniff(head: bytes) -> SniffedType | None:
    """The type the bytes say, or None when they match nothing accepted."""
    for magic, kind in _SIGNATURES:
        if head.startswith(magic):
            return kind
    return None


def validate_upload(data: bytes, *, max_bytes: int) -> SniffedType:
    """Refuse an empty, oversized or unrecognised file, naming why.

    Size is checked before the type, so a 2 GB upload is refused for its size
    rather than having its header inspected.
    """
    if not data:
        raise FileRejectedError("The file is empty.")
    if len(data) > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        raise FileRejectedError(
            f"The file is {len(data) / (1024 * 1024):.1f} MB. The limit is {limit_mb:g} MB."
        )
    kind = sniff(data[:16])
    if kind is None:
        raise FileRejectedError(
            "This file type is not accepted. Upload a "
            + ", ".join(ALLOWED_TYPES[:-1])
            + f" or {ALLOWED_TYPES[-1]}. The type is read from the file's contents, "
            "not its name."
        )
    return kind
