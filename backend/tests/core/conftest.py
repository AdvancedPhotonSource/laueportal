from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import pytest

from laueportal.core.settings import get_settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No ``LAUEPORTAL_*`` variables from the developer's shell (scripts/env.sh sets some)."""
    for name in list(os.environ):
        if name.upper().startswith("LAUEPORTAL_"):
            monkeypatch.delenv(name)
    get_settings.cache_clear()
    return monkeypatch


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo configure_logging (it replaces the root handlers, including pytest's)."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved = {
        name: (logging.getLogger(name).level, list(logging.getLogger(name).handlers))
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, (level, handlers) in saved.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.handlers[:] = handlers
