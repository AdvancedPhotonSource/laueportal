"""Live ``laueportal worker`` processes for the worker tests: a settings file, the command, waiting.

The worker runs as it does under supervisor: a fresh interpreter, settings from ``$LAUEPORTAL_CONFIG``
(the job table is the queue: ``jobs.dispatch``). Its working directory is ``backend/``, so the job processes it
starts can import the test computes (``tests.jobs.job_computes``) when the test asks for them (``test_computes``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from laueportal.indexing.models import Conversion
from laueportal.jobs.liveness import recorded_process_alive
from laueportal.jobs.models import Job, Worker
from tests.settings_support import fresh_interpreter_env, make_database

BACKEND = Path(__file__).resolve().parents[1]

FAST_EXECUTION = {
    "heartbeat_seconds": 0.05,
    "progress_seconds": 0.05,
    "cancel_poll_seconds": 0.02,
    "shutdown_grace_seconds": 5,
    "dispatch_seconds": 0.1,
}
"""``[execution]`` values that make the test runs quick (``run_support.FAST_POLICY``'s timings)."""

JOB_WITH_TEST_COMPUTES = (
    "import sys\n"
    "from tests.jobs import job_computes\n"
    "job_computes.register_test_computes()\n"
    "from laueportal.jobs import process\n"
    "sys.exit(process.main(sys.argv[1:]))\n"
)
"""A job process (``python -m laueportal.jobs.process``) whose wire runs are computed by ``tests.jobs.job_computes``."""

HOLD_SECONDS = 120
HELD_JOB = (
    "import os, sys, time\n"
    "from pathlib import Path\n"
    "hold, worker = Path(sys.argv.pop(1)), int(sys.argv.pop(1))\n"
    f"deadline = time.monotonic() + {HOLD_SECONDS}\n"
    "partial = hold / f'holding.{os.getpid()}'\n"
    "partial.write_text(str(os.getpid()))\n"
    "partial.replace(hold / 'holding')\n"
    "while not (hold / 'release').exists():\n"
    "    if os.getppid() != worker or time.monotonic() > deadline:\n"
    "        sys.exit(9)\n"
    "    time.sleep(0.02)\n" + JOB_WITH_TEST_COMPUTES
)
"""``JOB_WITH_TEST_COMPUTES`` held at its start, before anything of ``jobs.process`` runs: it writes ``holding`` (its
pid, whole) in the directory its first argument names, then waits until the test creates ``release`` there. It runs
in a session of its own, which a test's cleanup of the worker does not reach, so it exits (status 9) by itself once
its parent is no longer the worker (its second argument: the worker's pid, so a worker gone before the job process
started counts too) or after ``HOLD_SECONDS``: it never outlives a failed test."""


def _worker_script(job_command: str | None, prelude: str = "") -> str:
    """``laueportal worker`` whose job processes are ``job_command`` (Python source for ``jobs.dispatch.JOB_COMMAND``,
    the seam; None: the real one), after ``prelude`` (Python source the worker runs first, with ``os``, ``sys`` and
    ``jobs.dispatch`` imported: a test's fault)."""
    return (
        "import os, sys\n"
        "from laueportal.jobs import dispatch\n"
        + (f"dispatch.JOB_COMMAND = {job_command}\n" if job_command is not None else "")
        + prelude
        + "from laueportal import cli\n"
        "sys.exit(cli.main(['worker', *sys.argv[1:]]))\n"
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)  # strings and numbers: JSON's syntax is TOML's here


def write_config(
    path: Path,
    *,
    database: Path,
    runs_root: Path,
    execution: Mapping[str, Any] | None = None,
    remote: Mapping[str, Any] | None = None,
    live: Mapping[str, Any] | None = None,
) -> Path:
    """A TOML settings file for a worker on ``database`` (``remote``: its ``[remote]`` section, else none; ``live``:
    its ``[live]`` section, else none)."""
    sections: dict[str, Mapping[str, Any]] = {
        "database": {"path": database},
        "files": {"runs_root": runs_root},
        "execution": {**FAST_EXECUTION, **(execution or {})},
    }
    if remote is not None:
        sections["remote"] = remote
    if live is not None:
        sections["live"] = live
    lines = []
    for section, values in sections.items():
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in values.items())
    path.write_text("\n".join(lines) + "\n")
    return path


def worker_env(config: Path) -> dict[str, str]:
    return fresh_interpreter_env(config)


@dataclass
class WorkerProcess:
    process: subprocess.Popen[bytes]
    log: Path
    extra_pids: list[int] = field(default_factory=list)
    """Processes the test started through the worker (job processes, compute children), killed by ``cleanup``."""

    @property
    def pid(self) -> int:
        return self.process.pid

    def wait(self, timeout: float = 120) -> int:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.cleanup()
            raise AssertionError(f"the worker did not exit within {timeout} s:\n{self.log_text()}") from None

    def log_text(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def cleanup(self) -> None:
        """Kill whatever is left: the worker's process group and the recorded processes' groups."""
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=10)
        kill_quietly(self.extra_pids)


def start_worker(
    config: Path,
    *,
    log: Path,
    burst: bool = True,
    test_computes: bool = False,
    hold: Path | None = None,
    prelude: str = "",
    pipe: bool = False,
) -> WorkerProcess:
    """``laueportal worker`` (optionally with ``tests.jobs.job_computes``' computes) in its own session; with ``hold``
    (a directory), its job processes are ``HELD_JOB``s, with the test computes; ``prelude``: Python source the worker
    runs before it starts (``_worker_script``); ``pipe``: its output a pipe, as under supervisor (``process.stdout``,
    which the test reads or closes; ``log`` then stays empty), else the file ``log``."""
    options = ["--burst"] if burst else []
    job_command = None
    if hold is not None:
        job_command = f"[sys.executable, '-c', {HELD_JOB!r}, {str(hold)!r}, str(os.getpid())]"
    elif test_computes:
        job_command = f"[sys.executable, '-c', {JOB_WITH_TEST_COMPUTES!r}]"
    if job_command is None and not prelude:
        command = [sys.executable, "-m", "laueportal", "worker", *options]
    else:
        command = [sys.executable, "-c", _worker_script(job_command, prelude), *options]
    with log.open("wb") as output:
        process = subprocess.Popen(
            command,
            cwd=BACKEND,
            env=worker_env(config),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if pipe else output,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # as under supervisor: signals to the worker reach only the worker
        )
    return WorkerProcess(process=process, log=log)


def run_burst_worker(config: Path, *, log: Path, timeout: float = 120, **options: Any) -> WorkerProcess:
    """Start a burst worker and wait until nothing is queued or running and it has exited."""
    worker = start_worker(config, log=log, burst=True, **options)
    worker.wait(timeout)
    return worker


def wait_for(condition: Callable[[], bool], timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def handles_signal(pid: int, signum: int, *, mask: str = "SigCgt") -> bool:
    """Whether process ``pid`` has installed a handler for ``signum``: bit ``signum - 1`` of ``SigCgt`` in
    ``/proc/<pid>/status`` (Linux); with ``mask="SigIgn"``, whether it ignores it. False for a process that is
    gone."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    for line in status.splitlines():
        if line.startswith(f"{mask}:"):
            return bool(int(line.split()[1], 16) >> (signum - 1) & 1)
    return False


def worker_row(engine: Engine) -> Worker | None:
    """The database's worker row (``jobs.worker``), detached."""
    with Session(engine, expire_on_commit=False) as session:
        row = session.get(Worker, "worker")
        if row is not None:
            session.expunge(row)
        return row


def listening(engine: Engine, worker: WorkerProcess) -> bool:
    """The worker has its row and its stop handler: a signal now reaches its service stop."""
    row = worker_row(engine)
    return row is not None and row.pid == worker.pid and handles_signal(worker.pid, signal.SIGTERM)


def alive(pid: int) -> bool:
    """True for a running process (a zombie is dead: it only waits to be reaped)."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def kill_quietly(pids: Iterable[int]) -> None:
    for pid in pids:
        for kill in (os.killpg, os.kill):
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@dataclass
class Live:
    """A test database and a worker settings file for it."""

    engine: Engine
    config: Path
    """The worker's settings file (``write_config`` writes variants)."""
    database: Path

    def write_config(self, path: Path, *, live: Mapping[str, Any] | None = None, **execution: Any) -> Path:
        return write_config(
            path, database=self.database, runs_root=path.parent / "runs", execution=execution, live=live
        )


def kill_recorded_job_processes(engine: Engine) -> list[int]:
    """Kill the groups of the job processes the database's rows record that still run on this host (a Running job's or
    conversion's), and return their pids. A job process has a session of its own, and a live run's has no time limit, so
    one a failed test left would otherwise run on (the ``live`` fixture's teardown)."""
    with Session(engine) as session:
        rows = [
            *session.scalars(select(Job).where(Job.process_pid.is_not(None))),
            *session.scalars(select(Conversion).where(Conversion.process_pid.is_not(None))),
        ]
        records = [(row.process_host, row.process_pid, row.process_since_boot) for row in rows]
    pids = [pid for host, pid, since in records if pid is not None and recorded_process_alive(host, pid, since)]
    kill_quietly(pids)
    return pids


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    """A database with every table (``tmp_path/laueportal.db``) and ``tmp_path/worker.toml`` for workers on it. At
    its end, the job processes its rows record that still run are killed (``kill_recorded_job_processes``: a test
    that failed before its run ended)."""
    settings, engine = make_database(tmp_path)
    setup = Live(engine, tmp_path / "worker.toml", settings.database.path)
    setup.write_config(setup.config)
    try:
        yield setup
    finally:
        kill_recorded_job_processes(engine)
        engine.dispose()
