"""Compute functions for the live worker tests (``tests/jobs/test_worker.py`` and friends).

A worker started with ``test_computes`` (``tests.worker_support``) starts job processes that register them
(``register_test_computes``); the worker runs with ``backend/`` as its working directory, so its job processes
import this module. They write what they saw to JSON marker files.

``live_compute`` stands in for a live run's compute, registered for the tests only: it processes each input its hooks
hand out (``RunHooks.arrivals``) by the file's content (``LIVE_FAIL``: a failed input; ``LIVE_RAISE``: a run-level
error; ``LIVE_HOLD``: it waits for ``RELEASE``, or a service stop, first; ``LIVE_RETURN``: it returns once it has
processed it, before the seal) and "publishes" one line per processed input in ``LIVE_PROCESSED``, until ``should_stop``
says the run is sealed. It first writes ``LIVE_STARTED``: its job process and the cores its request gives it
(``RunRequest.cores``, the dispatcher's ``--cores``).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from laueportal.jobs.contract import (
    RunHooks,
    RunOutcome,
    RunRequest,
    register_compute_function,
    register_live_compute_function,
)
from laueportal.jobs.rundir import FailureRecord, ManifestEntry

BLOCK = "test:block"
"""A wire request whose ``notes`` say this runs ``blocking_compute``; any other runs ``quick_compute``."""
WAIT = "test:wait"
"""A wire request whose ``notes`` say this runs ``waiting_compute``."""
STUBBORN = "test:stubborn"
"""A wire request whose ``notes`` say this runs ``stubborn_compute``."""
COMPUTE_MARKER = "test-compute.json"
"""``blocking_compute``, ``waiting_compute`` and ``stubborn_compute`` write their pids here, in the run directory,
once they block."""
RELEASE = "test-release"
"""``waiting_compute`` and ``stubborn_compute`` return once the test creates this file in the run directory."""


def _write_json(path: str | os.PathLike[str], values: dict[str, Any]) -> None:
    partial = Path(f"{path}.partial")
    partial.write_text(json.dumps(values))
    partial.replace(path)  # readers never see a half-written marker


def _sleeper() -> subprocess.Popen[bytes]:
    """A child that would outlive its parent: it sleeps for five minutes."""
    return subprocess.Popen(
        ["sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def blocking_compute(request: RunRequest, entries: Iterable[ManifestEntry], hooks: RunHooks) -> RunOutcome:
    """Start a grandchild, then block until asked to stop (or interrupted by a timeout or a kill)."""
    child = _sleeper()
    _write_json(Path(request.run_directory) / COMPUTE_MARKER, {"grandchild": child.pid, "job_process": os.getpid()})
    while not hooks.should_stop():
        time.sleep(0.02)
    for entry in entries:
        hooks.record_failure(FailureRecord.not_run(entry, message="stopped"))
    return RunOutcome(n_succeeded=0, n_failed=0, stopped=True)


def waiting_compute(request: RunRequest, entries: Iterable[ManifestEntry], hooks: RunHooks) -> RunOutcome:
    """Start a grandchild, then wait until the test releases the run (``RELEASE``), and succeed every input."""
    child = _sleeper()
    directory = Path(request.run_directory)
    _write_json(directory / COMPUTE_MARKER, {"grandchild": child.pid, "job_process": os.getpid()})
    while not (directory / RELEASE).exists() and not hooks.should_stop():
        time.sleep(0.02)
    child.kill()
    child.wait()
    return quick_compute(request, entries, hooks)


def stubborn_compute(request: RunRequest, entries: Iterable[ManifestEntry], hooks: RunHooks) -> RunOutcome:
    """Start a grandchild, then wait until the test releases the run (``RELEASE``), deaf to a stop: only a kill
    ends it sooner."""
    child = _sleeper()
    directory = Path(request.run_directory)
    _write_json(directory / COMPUTE_MARKER, {"grandchild": child.pid, "job_process": os.getpid()})
    while not (directory / RELEASE).exists():
        time.sleep(0.02)
    child.kill()
    child.wait()
    return quick_compute(request, entries, hooks)


def quick_compute(request: RunRequest, entries: Iterable[ManifestEntry], hooks: RunHooks) -> RunOutcome:
    count = len(list(entries))
    hooks.report_progress(succeeded=count, failed=0)
    return RunOutcome(n_succeeded=count, n_failed=0, summary="test compute")


def dispatching_compute(request: RunRequest, entries: Iterable[ManifestEntry], hooks: RunHooks) -> RunOutcome:
    if request.parameters.get("notes") == BLOCK:
        return blocking_compute(request, entries, hooks)
    if request.parameters.get("notes") == WAIT:
        return waiting_compute(request, entries, hooks)
    if request.parameters.get("notes") == STUBBORN:
        return stubborn_compute(request, entries, hooks)
    return quick_compute(request, entries, hooks)


LIVE_FAIL = b"test:fail"
"""A file with this content is a failed input of ``live_compute``."""
LIVE_RAISE = b"test:raise"
"""A file with this content makes ``live_compute`` raise: a run-level error."""
LIVE_HOLD = b"test:hold"
"""A file with this content makes ``live_compute`` write ``COMPUTE_MARKER`` and wait for ``RELEASE`` (or the service
stop: ``should_stop``) before it counts it as succeeded."""
LIVE_HANG = b"test:hang"
"""A file with this content makes ``live_compute`` write ``COMPUTE_MARKER`` and wait for ``RELEASE`` alone, deaf to a
stop: an input whose read hangs (a drain that is stuck; Stop now)."""
LIVE_RETURN = b"test:return"
"""A file with this content makes ``live_compute`` return once it has processed it, without waiting for the seal (a
live compute that ends too soon)."""
LIVE_PROCESSED = "test-live-processed.jsonl"
"""``live_compute``'s output in the run directory: one line per processed input, written before it is reported."""
LIVE_STARTED = "test-live-started.json"
"""``live_compute``'s first act, in the run directory: ``{"job_process": pid, "cores": request.cores}``."""


def live_compute(request: RunRequest, hooks: RunHooks) -> RunOutcome:
    """The stand-in live compute (the module's docstring): arrivals until the seal, each processed whole."""
    directory = Path(request.run_directory)
    _write_json(directory / LIVE_STARTED, {"job_process": os.getpid(), "cores": request.cores})
    succeeded = failed = 0
    while True:
        entries = hooks.arrivals()
        for entry in entries:
            content = Path(entry.source).read_bytes()
            if content == LIVE_RAISE:
                raise RuntimeError(f"stand-in run-level error at {entry.input_id}")
            if content in (LIVE_HOLD, LIVE_HANG):
                _write_json(directory / COMPUTE_MARKER, {"job_process": os.getpid(), "holding": entry.input_id})
                while not (directory / RELEASE).exists() and (content == LIVE_HANG or not hooks.should_stop()):
                    time.sleep(0.02)
            ok = content != LIVE_FAIL
            with open(directory / LIVE_PROCESSED, "a", encoding="utf-8") as output:
                output.write(json.dumps({"index": entry.index, "input_id": entry.input_id, "ok": ok}) + "\n")
            if ok:
                succeeded += 1
            else:
                hooks.record_failure(FailureRecord.from_error(entry, ValueError("stand-in failed input")))
                failed += 1
            hooks.report_progress(succeeded=succeeded, failed=failed)
            if content == LIVE_RETURN:
                return RunOutcome(n_succeeded=succeeded, n_failed=failed, summary="test live compute")
        if hooks.should_stop():
            return RunOutcome(n_succeeded=succeeded, n_failed=failed, stopped=True, summary="test live compute")
        if not entries:
            time.sleep(0.02)


def register_test_computes() -> None:
    """Make wire runs use the test computes (before lauelab's compute module registers the real one), and live wire
    runs ``live_compute``."""
    register_compute_function("wire_reconstruction", dispatching_compute)
    register_live_compute_function("wire_reconstruction", live_compute)
