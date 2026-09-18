"""Suite-wide fixtures."""

from __future__ import annotations

import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def _files_stay_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests write files; never into a real bucket.

    A developer's .env may well say STORAGE_BACKEND=supabase. Tests that store
    files point FILE_STORAGE_ROOT at a temporary directory and read it back, and
    the Supabase backend's own tests use an in-process fake.
    """
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
