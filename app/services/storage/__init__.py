"""File storage behind one interface: generated keys, validated content, atomic writes."""

from app.services.storage.local import LocalFileStorage, StorageError, get_storage
from app.services.storage.sniff import (
    FileRejected,
    FileRejectedError,
    SniffedType,
    sniff,
    validate_upload,
)

__all__ = [
    "FileRejected",
    "FileRejectedError",
    "LocalFileStorage",
    "SniffedType",
    "StorageError",
    "get_storage",
    "sniff",
    "validate_upload",
]
