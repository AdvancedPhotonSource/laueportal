"""A hung mount for tests (``files.probe``): ``hang(monkeypatch, folder)`` makes every ``os.stat``, ``os.lstat``,
``os.scandir`` and ``os.open`` of a path under ``folder`` block on an event, as a stat on a hung NFS mount blocks in
the kernel, and
``install_probe`` gives the process a probe of its own with a short timeout, on a mount table where ``folder`` is a
mount of its own.

A test that expects a check to start within such a timeout starts the probe's threads first (``install_probe`` and
``start_threads`` do) and runs without garbage collection (the ``gc_disabled`` fixture): starting the threads, or a
full collection in a large test process, can take longer than the timeout."""

from __future__ import annotations

import gc
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from laueportal.files import probe

HELD_SECONDS = 30
"""The longest a hung stat blocks (a test releases it long before)."""


@dataclass
class Hung:
    folder: Path
    entered: threading.Event = field(default_factory=threading.Event)
    """A stat of a path under ``folder`` has started (and blocks)."""
    released: threading.Event = field(default_factory=threading.Event)
    calls: int = 0
    on_enter: Callable[[], None] | None = None
    """Called in the stat's thread as it starts blocking (e.g. to count the database connections held then)."""

    def holds(self, path: Any) -> bool:
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, (str, os.PathLike)):
            return False  # a descriptor
        text = os.path.abspath(os.fspath(path))
        return text == str(self.folder) or text.startswith(str(self.folder) + os.sep)


def hang(monkeypatch: pytest.MonkeyPatch, folder: Path) -> Hung:
    """Every stat, lstat, listing and open of a path under ``folder`` blocks until ``Hung.released`` is set."""

    hung = Hung(folder)
    for name in ("stat", "lstat", "scandir", "open"):
        real = getattr(os, name)

        def blocking(path: Any, *args: Any, _real: Any = real, **kwargs: Any) -> Any:
            if hung.holds(path):
                hung.calls += 1
                if hung.on_enter is not None:
                    hung.on_enter()
                hung.entered.set()
                hung.released.wait(HELD_SECONDS)
            return _real(path, *args, **kwargs)

        monkeypatch.setattr(os, name, blocking)
    return hung


def install_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mounts: tuple[Path, ...] = (),
    timeout: float = 0.3,
    hung_seconds: float = 60.0,
    threads: int = probe.PROBE_THREADS,
) -> probe.FileProbe:
    """The process's probe for this test: ``timeout`` seconds, ``mounts`` as mounts of their own on ``/``."""

    table = (("/", False), *((str(mount), False) for mount in mounts))
    installed = probe.FileProbe(threads=threads, timeout=timeout, hung_seconds=hung_seconds, mounts=lambda: table)
    monkeypatch.setattr(probe, "_default", installed)
    return start_threads(installed)


def start_threads(files: probe.FileProbe) -> probe.FileProbe:
    """``files``, its threads started (by a check that does nothing): a probe starts them in its first check,
    within that check's timeout."""
    files.run(os.sep, lambda: None)
    return files


@pytest.fixture
def gc_disabled() -> Iterator[None]:
    """No automatic garbage collection during the test: a full collection holds the GIL (about 0.3 s in a process
    that has run the whole suite), longer than a short probe timeout."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()
