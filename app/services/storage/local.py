"""Files on a local directory, addressed by keys the server generates.

For development and tests. On a container platform the directory lives only as
long as the container, so a deployment uses the Supabase backend instead
(`STORAGE_BACKEND=supabase`) unless `FILE_STORAGE_ROOT` is a mounted volume.

Beyond the key rules in `base.py`:

1. **A key cannot leave the root.** The resolved path is checked to still be
   under the root, so a key that somehow came from elsewhere still cannot walk
   out of it.
2. **Writes are atomic.** A temporary file is written and renamed into place, so
   a reader never sees half a document.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from app.services.storage.base import FileStorage, StorageError, check_key

__all__ = ["DEFAULT_ROOT", "LocalFileStorage"]

#: backend/uploads, which the repository ignores.
DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "uploads"


class LocalFileStorage(FileStorage):
    """A directory on the API host."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def describe(self) -> str:
        return f"local directory {self._root}"

    def _path(self, key: str) -> Path:
        path = (self._root / check_key(key)).resolve()
        if self._root not in path.parents:
            raise StorageError("The storage key is not valid")
        return path

    # ── Operations ──────────────────────────────────────────────────────────

    async def put(self, key: str, data: bytes) -> None:
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

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(f"The file could not be read: {exc.strerror}") from exc

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    async def list_keys(self, prefix: str) -> list[str]:
        base = (self._root / prefix.strip("/")).resolve()
        if self._root not in base.parents or not base.is_dir():
            return []
        return sorted(
            path.relative_to(self._root).as_posix()
            for path in base.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        )

    async def delete(self, key: str) -> bool:
        path = self._path(key)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StorageError(f"The file could not be removed: {exc.strerror}") from exc
