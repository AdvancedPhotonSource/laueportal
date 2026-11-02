"""A probe of the job processes of a linked pair (a live wire run and its follower; the slow follower tests): what each
job process does to the wire run's directory, from Python (``install``), as JSON lines in ``<log dir>/<pid>.jsonl``.

``probe_prelude`` makes a worker's job processes run under it (``start_worker(prelude=...)``: the worker's
``jobs.dispatch.JOB_COMMAND``). Each job process then logs, with its wall-clock time:

- ``stat``: an ``os.stat`` of the wire run's ``completed.jsonl`` (a look, a missing file included);
- ``read``: an ``open`` of it for reading;
- ``write``: anything that would change the wire run's directory: an ``open`` or ``os.open`` of a path in it for
  writing (or creating), and ``os.replace``, ``os.rename``, ``os.link``, ``os.unlink``, ``os.remove``, ``os.mkdir``,
  ``os.truncate`` there (h5py's writes are C's, which a test checks by the directory's files instead);
- ``seal``: the executor's seal (``jobs.arrivals.seal``) of the job process's own run.

``stitch_seconds``: the live LaueGo compute's stitch of the Output XML waits that long first (in every job process;
only a LaueGo run stitches), so that a run's drain after its seal lasts long enough to show a look that should not
be there.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROBE_JOB = (
    "import sys\n"
    "from tests.jobs import follower_probe\n"
    "follower_probe.install(sys.argv.pop(1), sys.argv.pop(1), float(sys.argv.pop(1)))\n"
    "from laueportal.jobs import process\n"
    "sys.exit(process.main(sys.argv[1:]))\n"
)

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def probe_prelude(log_dir: Path, wire_directory: Path, *, stitch_seconds: float = 0.0) -> str:
    """Python source for ``start_worker(prelude=...)``: the worker's job processes run under the probe."""
    arguments = [str(log_dir), str(wire_directory), str(stitch_seconds)]
    return f"dispatch.JOB_COMMAND = [sys.executable, '-c', {PROBE_JOB!r}, *{arguments!r}]\n"


def install(log_dir: str, wire_directory: str, stitch_seconds: float) -> None:
    """Wrap this process's calls (the module's docstring); its log is ``<log_dir>/<pid>.jsonl``."""
    wire = os.path.normpath(wire_directory)
    completed = os.path.join(wire, "completed.jsonl")
    log = os.open(os.path.join(log_dir, f"{os.getpid()}.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    lock = threading.Lock()

    def note(op: str, path: Any, **values: Any) -> None:
        line = json.dumps({"t": time.time(), "op": op, "path": os.fspath(path), "thread": _thread(), **values})
        with lock:
            os.write(log, (line + "\n").encode())

    def target(path: Any) -> str | None:
        try:
            text = os.path.normpath(os.path.abspath(os.fspath(path)))
        except TypeError:  # a descriptor
            return None
        if not isinstance(text, str):  # a path given as bytes: none of the code under test gives one
            return None
        return text if text == wire or text.startswith(wire + os.sep) else None

    real_stat, real_open, real_os_open = os.stat, builtins.open, os.open

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if target(path) == completed:
            note("stat", path)
        return real_stat(path, *args, **kwargs)

    def open_(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        found = target(file)
        if found is not None:
            if any(flag in mode for flag in "wax+"):
                note("write", file, mode=mode)
            elif found == completed:
                note("read", file)
        return real_open(file, mode, *args, **kwargs)

    def os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> Any:
        if target(path) is not None and flags & WRITE_FLAGS:
            note("write", path, flags=flags)
        return real_os_open(path, flags, *args, **kwargs)

    def changing(name: str) -> Callable[..., Any]:
        real = getattr(os, name)

        def call(*paths: Any, **kwargs: Any) -> Any:
            for path in paths[:2]:
                if target(path) is not None:
                    note("write", path, call=name)
            return real(*paths, **kwargs)

        return call

    os.stat = stat  # type: ignore[assignment]
    builtins.open = io.open = open_  # type: ignore[assignment]
    os.open = os_open  # type: ignore[assignment]
    for name in ("replace", "rename", "link", "unlink", "remove", "mkdir", "truncate"):
        setattr(os, name, changing(name))

    from laueportal.jobs import arrivals

    real_seal = arrivals.seal

    def seal(run_directory: Any) -> None:
        note("seal", run_directory)
        real_seal(run_directory)

    arrivals.seal = seal  # before the executor imports it
    if stitch_seconds > 0:
        from laueportal.indexing.lauego import live_compute

        real_merge = live_compute.merge_xml_files

        def merge_xml_files(*args: Any, **kwargs: Any) -> Any:
            time.sleep(stitch_seconds)
            return real_merge(*args, **kwargs)

        live_compute.merge_xml_files = merge_xml_files  # type: ignore[assignment]
    sys.stdout.flush()


def _thread() -> str:
    return threading.current_thread().name


def probe_log(log_dir: Path, pid: int) -> list[dict[str, Any]]:
    """The probe's lines of the job process ``pid``, in order."""
    path = log_dir / f"{pid}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
