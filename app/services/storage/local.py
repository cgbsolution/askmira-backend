"""Files on a local directory, addressed by keys the server generates.

The interface is what matters; the local directory is the first backend behind
it. Object storage replaces this class, not its callers.

Three rules:

1. **Keys are generated, never taken from a request.** A client filename never
   reaches the filesystem, so `../../etc/passwd` and `invoice.pdf.exe` are both
   just titles in a database column.
2. **A key cannot leave the root.** Keys are checked against a strict pattern and
   the resolved path is checked to still be under the root, so a key that
   somehow came from elsewhere still cannot walk out of it.
3. **Writes are atomic.** A temporary file is written and renamed into place, so
   a reader never sees half a document.
"""

from __future__ import annotations

import os
import re
import secrets
import uuid
from pathlib import Path

__all__ = ["LocalFileStorage", "StorageError", "get_storage"]

#: Tenant id, namespace, random name, extension. Nothing a person typed.
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*(/[A-Za-z0-9][A-Za-z0-9_\-.]*)+$")
_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_\-]{0,31}$")
_EXTENSION_PATTERN = re.compile(r"^\.[a-z0-9]{1,8}$")


class StorageError(RuntimeError):
    """A key was malformed, or the backend could not read or write."""


class LocalFileStorage:
    """A directory on the API host."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    # ── Keys ────────────────────────────────────────────────────────────────

    @staticmethod
    def new_key(tenant_id: uuid.UUID, namespace: str, extension: str) -> str:
        """A fresh key: `<tenant>/<namespace>/<random><ext>`."""
        if not _NAMESPACE_PATTERN.match(namespace):
            raise StorageError(f"'{namespace}' is not a valid storage namespace")
        if not _EXTENSION_PATTERN.match(extension):
            raise StorageError(f"'{extension}' is not a valid file extension")
        return f"{tenant_id}/{namespace}/{secrets.token_hex(16)}{extension}"

    def _path(self, key: str) -> Path:
        if not key or ".." in key or not _KEY_PATTERN.match(key):
            raise StorageError("The storage key is not valid")
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise StorageError("The storage key is not valid")
        return path

    # ── Operations ──────────────────────────────────────────────────────────

    def put(self, key: str, data: bytes) -> None:
        """Write bytes at a key, atomically. Overwrites nothing silently: the
        caller owns the key and decides whether rewriting it is legitimate."""
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
            with open(temporary, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise StorageError(f"The file could not be stored: {exc.strerror}") from exc

    def get(self, key: str) -> bytes | None:
        """The bytes at a key, or None when nothing is stored there."""
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(f"The file could not be read: {exc.strerror}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        """Remove a stored file. True when something was removed."""
        path = self._path(key)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StorageError(f"The file could not be removed: {exc.strerror}") from exc


def get_storage() -> LocalFileStorage:
    """The configured storage backend.

    Read from settings on each call rather than cached at import, so a test can
    point `FILE_STORAGE_ROOT` at a temporary directory.
    """
    from app.core.config import settings

    return LocalFileStorage(settings.FILE_STORAGE_ROOT or DEFAULT_ROOT)


#: backend/uploads, which the repository ignores.
DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "uploads"
