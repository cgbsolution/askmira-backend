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


@pytest.fixture(autouse=True)
def _nothing_leaves_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never send real mail or call a real AI provider.

    A developer's .env may hold the company's SMTP relay and AI keys. Without
    this a test run tries to email made-up addresses through the real relay,
    and a test that forgot `use_backend` would spend real tokens. Email falls
    back to the console adapter; a test that needs a model swaps one in.
    """
    monkeypatch.setattr(settings, "SMTP_HOST", None)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OLLAMA_BASE_URL"):
        monkeypatch.setattr(settings, name, None)
