"""The storage interface, and the key rules every backend shares.

Keys are generated here, never taken from a request, and every backend checks
them against the same strict pattern before touching anything. A client
filename never becomes a path or an object name, so `../../etc/passwd` and
`invoice.pdf.exe` are both just titles in a database column.

Every operation is awaited: a backend may be a network hop away, and a
blocking call would stall every other request on the event loop.
"""

from __future__ import annotations

import re
import secrets
import uuid
from abc import ABC, abstractmethod

__all__ = ["FileStorage", "StorageError", "check_key"]

#: Tenant id, namespace, random name, extension. Nothing a person typed.
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*(/[A-Za-z0-9][A-Za-z0-9_\-.]*)+$")
_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_\-]{0,31}$")
_EXTENSION_PATTERN = re.compile(r"^\.[a-z0-9]{1,8}$")


class StorageError(RuntimeError):
    """A key was malformed, or the backend could not read or write."""


def check_key(key: str) -> str:
    """The key, if it matches the pattern every generated key has."""
    if not key or ".." in key or not _KEY_PATTERN.match(key):
        raise StorageError("The storage key is not valid")
    return key


class FileStorage(ABC):
    """Bytes addressed by keys the server generates."""

    @staticmethod
    def new_key(tenant_id: uuid.UUID, namespace: str, extension: str) -> str:
        """A fresh key: `<tenant>/<namespace>/<random><ext>`."""
        if not _NAMESPACE_PATTERN.match(namespace):
            raise StorageError(f"'{namespace}' is not a valid storage namespace")
        if not _EXTENSION_PATTERN.match(extension):
            raise StorageError(f"'{extension}' is not a valid file extension")
        return f"{tenant_id}/{namespace}/{secrets.token_hex(16)}{extension}"

    @abstractmethod
    def describe(self) -> str:
        """Where files go, for the startup log. Never includes a credential."""

    @abstractmethod
    async def put(self, key: str, data: bytes) -> None:
        """Write bytes at a key. Overwrites nothing silently: the caller owns the
        key and decides whether rewriting it is legitimate."""

    @abstractmethod
    async def get(self, key: str) -> bytes | None:
        """The bytes at a key, or None when nothing is stored there."""

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Remove a stored file. True when something was removed."""
