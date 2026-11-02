from __future__ import annotations

import logging
from pathlib import Path

import pytest

from laueportal.core.logging import configure_logging
from tests.settings_support import make_settings


def test_configure_logging_sets_level_and_one_stderr_handler(
    tmp_path: Path, restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings(tmp_path, app={"log_level": "WARNING"}))
    configure_logging(make_settings(tmp_path, app={"log_level": "debug"}))  # again: replaces, never stacks

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1 and isinstance(root.handlers[0], logging.StreamHandler)
    assert logging.getLogger("uvicorn.access").handlers == []  # uvicorn propagates to the root handler

    logging.getLogger("laueportal.test").info("hello from the test")
    assert " INFO [" in (err := capsys.readouterr().err) and "laueportal.test: hello from the test" in err
