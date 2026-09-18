"""File storage behind one interface: generated keys, validated content, atomic writes."""

from app.services.storage.base import FileStorage, StorageError
from app.services.storage.local import DEFAULT_ROOT, LocalFileStorage
from app.services.storage.sniff import (
    FileRejected,
    FileRejectedError,
    SniffedType,
    sniff,
    validate_upload,
)
from app.services.storage.supabase import SupabaseStorage

__all__ = [
    "FileRejected",
    "FileRejectedError",
    "FileStorage",
    "LocalFileStorage",
    "SniffedType",
    "StorageError",
    "SupabaseStorage",
    "get_storage",
    "sniff",
    "validate_upload",
]


def get_storage() -> FileStorage:
    """The configured storage backend.

    Read from settings on each call rather than cached at import, so a test can
    point `FILE_STORAGE_ROOT` at a temporary directory.
    """
    from app.core.config import settings

    if settings.STORAGE_BACKEND == "supabase":
        return SupabaseStorage(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_ROLE_KEY,
            settings.SUPABASE_STORAGE_BUCKET,
        )
    return LocalFileStorage(settings.FILE_STORAGE_ROOT or DEFAULT_ROOT)
