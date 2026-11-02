"""``laueportal seed demo``: build the demo database and its runs root from scratch (``seed.demo`` has the
content: scan 990001, R1 and I1 run by a worker, the registered runs R2, I2 and R3, and with ``crux_run``
(``--crux-run``) R4, recorded as running on CRUX), all from synthetic data.

The steps, in order:

1. **Checks**, before anything changes: ``crux_run`` only while the settings do not enable remote execution (R4 has no
   DM job behind it, and the worker's remote poller would ask DM about it on every pass, for ever); the committed data
   the demo copies is there (``demo.DemoLayout.data``, this checkout's ``backend/tests``); no job process the database
   records is running; the database may be built (it does not exist, or ``force``); the runs root (``files.runs_root``)
   is empty or belongs to this database (``MARKER``) and does not hold the database.
2. **The database**: a new file upgraded to head, or, with ``force``, the existing file emptied in place and
   upgraded. In place, so that a running API and worker keep using the same file: their connections see the
   new schema, and the row of the worker serving the database is put back. The runs root is emptied and marked as this database's (a run directory is never reused, so
   the old database's run directories must go with it).
3. **The demo**: ``demo.create_demo_runs`` (staged inputs, scan 990001, R1 and I1 published),
   ``demo.register_runs`` (R2, I2 and R3, computed here), then R1 and I1 are queued (the job table is the queue) and run
   to Finished: by the worker that holds the database's worker lock (``jobs.worker``, the stack's), which must
   start them within ``WORKER_START_SECONDS`` (else the seed fails and names it), or, when no worker holds the
   lock, by a burst worker this module starts (``laueportal worker --burst`` in a subprocess, on the same
   database). Then, with ``crux_run``, ``demo.add_remote_run`` (R4, recorded as running on CRUX: no worker runs it,
   and no poller, since remote execution is off).

A database that this build created is removed again when a later step fails; a rebuilt one is left
incomplete (build it again). A database other than the configured one (``database``) has no worker of the
stack's (each worker serves one database), so a burst worker runs its R1 and I1.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from laueportal.core import migrations
from laueportal.core.db import BUSY_TIMEOUT_MS, database_engine
from laueportal.core.settings import DatabaseSettings, Settings
from laueportal.core.timestamps import utc_timestamp
from laueportal.jobs.dispatch import live_recorded_processes
from laueportal.jobs.models import Job, Worker
from laueportal.jobs.records import RunPublicationError
from laueportal.jobs.rundir import ManifestError
from laueportal.jobs.state import STATUS_NAMES, ExecutionStateError, JobStatus, RunPhase, is_terminal
from laueportal.jobs.worker import WORKER_ROW, holder, lock_path, take_lock
from laueportal.seed import demo

MARKER = ".laueportal-seed.json"
"""In the runs root: which database the runs root belongs to (written by ``build_demo``)."""
INPUTS = "inputs"
"""The staged demo inputs, under the runs root (``DemoLayout.inputs``)."""
SQLITE_COMPANIONS = ("", "-wal", "-shm")

WORKER_START_SECONDS = 60.0
"""How long the worker holding the database's lock may leave R1 and I1 queued before the seed fails and names it (a
burst worker would exit 1 at the lock); a burst worker runs them when no worker holds the lock (any more)."""
RUN_TIMEOUT_SECONDS = 3600.0
"""How long R1 and I1 may take together (about a minute on the development machine)."""
POLL_SECONDS = 0.5
LOG_TAIL_CHARACTERS = 4000

Report = Callable[[str], None]

_EXPECTED_ERRORS = (
    OSError,  # files: the committed data, the runs root, the staged copies
    ValueError,  # lauelab's errors (InvalidScanFile, InputError, ...), input resolution
    SQLAlchemyError,
    RunPublicationError,
    ManifestError,
    ExecutionStateError,
)
"""Errors of the demo's steps that ``build_demo`` reports as a ``SeedError`` (one line, no traceback)."""


class SeedError(RuntimeError):
    """The demo could not be built. The message says why and what to do."""


@dataclass(frozen=True)
class DemoResult:
    database: Path
    runs_root: Path
    revision: str | None
    rebuilt: bool
    """An existing database was emptied and built again (``force``)."""
    runs: demo.DemoRuns
    registered: demo.RegisteredRuns
    ran_by: str
    """Who ran R1 and I1: ``the worker (<who>)`` or ``a burst worker``."""


def build_demo(
    settings: Settings,
    *,
    force: bool = False,
    database: Path | None = None,
    crux_run: bool = False,
    data: Path | None = None,
    report: Report = print,
) -> DemoResult:
    """Build the demo database (``database``, default ``settings.database.path``) and its runs root, from the
    committed data in ``data`` (default ``demo.default_data_directory()``).

    Raises ``SeedError`` (nothing changed when a check fails). The burst worker, when one is needed, gets this
    process's environment plus the database path, so ``$LAUEPORTAL_CONFIG`` must describe ``settings`` (as it does
    when ``settings`` came from ``get_settings()``).
    """

    configured = settings.database.path.expanduser().resolve()
    target = (database if database is not None else configured).expanduser().resolve()
    database_settings = settings.database.model_copy(update={"path": target})
    runs_root = settings.files.runs_root.expanduser().resolve()
    layout = demo.DemoLayout(data=data or demo.default_data_directory(), inputs=runs_root / INPUTS, runs_root=runs_root)

    # 1. Checks (nothing changes yet).
    if crux_run and settings.remote.enabled:
        raise SeedError(
            "--crux-run is refused while remote execution is enabled (remote.enabled): R4 has no DM job behind it, "
            "and the remote poller would ask DM about it on every pass; seed with LAUEPORTAL_REMOTE__ENABLED=false, "
            "and never serve that database with remote execution enabled"
        )
    missing = layout.missing_data()
    if missing:
        raise SeedError(f"{len(missing)} file(s) of the demo's data are missing, e.g. {missing[0]}")
    if target.exists() and not force:
        raise SeedError(f"{target} exists; pass --force to build it again (its runs root {runs_root} is emptied too)")
    if not target.parent.is_dir():
        raise SeedError(f"directory {target.parent} does not exist")
    _check_runs_root(runs_root, target)
    if target.is_file():
        running = _running_job_processes(database_settings)
        if running:
            raise SeedError(
                f"{target} has job processes running ({running}); wait for them or stop them before seeding"
            )
    # 2. The database and the runs root.
    rebuilt = target.exists()
    try:
        revision = _build_database(database_settings, rebuild=rebuilt)
    except (OSError, sqlite3.Error, SQLAlchemyError, migrations.MigrationError) as error:
        if not rebuilt:
            _remove_database(target)
        raise SeedError(f"database {target}: {error}") from error
    report(f"{target}: {'emptied and rebuilt' if rebuilt else 'created'} at revision {revision}")
    engine = database_engine(database_settings)
    try:
        _reset_runs_root(runs_root, target)
        # 3. The demo.
        runs = demo.create_demo_runs(engine, layout, settings=settings)
        report(f"  scan {demo.SCAN_NUMBER}; created R{runs.reconstruction_id} (wire) and I{runs.indexing_id} (LaueGo)")
        registered = demo.register_runs(engine, layout)
        report(
            f"  registered R{registered.fine_reconstruction_id} (fine reconstruction), "
            f"I{registered.map_indexing_id} (map) and R{registered.multipoint_reconstruction_id} (multi-point "
            "reconstruction)"
        )
        demo.enqueue_demo_runs(runs, engine=engine)
        ran_by = _run_to_finish(runs, engine=engine, database=target, report=report)
        _require_finished(engine, runs)
        if crux_run:
            remote = demo.add_remote_run(engine, layout, settings=settings)
            report(f"  R{remote} recorded as running on CRUX, with the golden observation")
    except BaseException as error:
        engine.dispose()
        if not rebuilt:
            _remove_database(target)
        elif not isinstance(error, KeyboardInterrupt):
            report(f"{target} is incomplete; build it again (laueportal seed demo --force)")
        if isinstance(error, _EXPECTED_ERRORS):
            raise SeedError(f"{type(error).__name__}: {error}") from error
        raise
    engine.dispose()
    return DemoResult(target, runs_root, revision, rebuilt, runs, registered, ran_by)


# ---------------------------------------------------------------------------
# The database


def _companions(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in SQLITE_COMPANIONS]


def _remove_database(path: Path) -> None:
    for companion in _companions(path):
        companion.unlink(missing_ok=True)


def empty_database(path: Path) -> None:
    """Drop every table, view and trigger of the SQLite database ``path`` in one transaction, then VACUUM.

    The file stays (same inode): connections other processes hold keep working and see an empty schema.
    """

    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('view', 'trigger', 'table') "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        ).fetchall()
        order = {"view": 0, "trigger": 1, "table": 2}
        for kind, name in sorted(objects, key=lambda item: order[item[0]]):
            quoted = '"' + str(name).replace('"', '""') + '"'
            connection.execute(f"DROP {kind.upper()} IF EXISTS {quoted}")
        connection.execute("COMMIT")
        connection.execute("VACUUM")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _build_database(database: DatabaseSettings, *, rebuild: bool) -> str | None:
    serving: dict[str, object] | None = None
    if rebuild:
        serving = _serving_worker_row(database.path)
        empty_database(database.path)
    else:
        for companion in _companions(database.path)[1:]:  # a stale -wal must not be replayed into a new file
            companion.unlink(missing_ok=True)
    _before, after = migrations.upgrade(database)
    if serving is not None:  # the worker serving the database lives on: its row too (the status page, the seed)
        engine = database_engine(database, create=False)
        try:
            with Session(engine) as session, session.begin():
                session.add(Worker(**serving))
        finally:
            engine.dispose()
    return after


def _serving_worker_row(database: Path) -> dict[str, object] | None:
    """The row of the worker that serves ``database`` (it holds the worker lock), as columns; None without one."""
    lock = take_lock(lock_path(database))
    if lock is not None:
        lock.close()
        return None
    engine = database_engine(DatabaseSettings(path=database), create=False)
    try:
        with Session(engine) as session:
            row = session.get(Worker, WORKER_ROW)
            return None if row is None else {column.key: getattr(row, column.key) for column in Worker.__table__.c}
    except SQLAlchemyError:
        return None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The runs root


def _has_files(directory: Path) -> bool:
    return any(files for _root, _dirs, files in os.walk(directory))


def marker_database(runs_root: Path) -> str | None:
    """The database a runs root belongs to (its ``MARKER``, written by ``build_demo``); None without one."""
    try:
        return str(json.loads((runs_root / MARKER).read_text())["database"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _check_runs_root(runs_root: Path, database: Path) -> None:
    """Refuse a runs root the build must not empty."""

    if database == runs_root or database.is_relative_to(runs_root) or runs_root.is_relative_to(database):
        raise SeedError(f"the runs root {runs_root} and the database ({database}) overlap; give each its own directory")
    if runs_root == Path(runs_root.anchor) or runs_root == Path.home().resolve():
        raise SeedError(f"the runs root {runs_root} is not a directory the demo may empty")
    if not runs_root.exists():
        return
    if not runs_root.is_dir():
        raise SeedError(f"the runs root {runs_root} is not a directory")
    owner = marker_database(runs_root)
    if owner is not None and owner != str(database):
        raise SeedError(
            f"the runs root {runs_root} belongs to the demo database {owner}; give {database} a runs root of its "
            "own (files.runs_root, e.g. LAUEPORTAL_FILES__RUNS_ROOT)"
        )
    if owner is None and _has_files(runs_root):
        raise SeedError(
            f"the runs root {runs_root} holds files that `laueportal seed demo` did not make; empty it or choose "
            "another (files.runs_root)"
        )


def _reset_runs_root(runs_root: Path, database: Path) -> None:
    """Empty the runs root (checked by ``_check_runs_root``) and mark it as ``database``'s."""

    runs_root.mkdir(parents=True, exist_ok=True)
    for child in runs_root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    (runs_root / MARKER).write_text(
        json.dumps({"database": str(database), "seeded_at": utc_timestamp()}, indent=2) + "\n"
    )


# ---------------------------------------------------------------------------
# Running R1 and I1


def _labels(runs: demo.DemoRuns) -> dict[int, str]:
    return {runs.reconstruction_job_id: f"R{runs.reconstruction_id}", runs.indexing_job_id: f"I{runs.indexing_id}"}


def _jobs(engine: Engine, job_ids: Sequence[int]) -> dict[int, Job]:
    with Session(engine, expire_on_commit=False) as session:
        return {job_id: job for job_id in job_ids if (job := session.get(Job, job_id)) is not None}


def _running_job_processes(database: DatabaseSettings) -> list[int]:
    """The pids of the live job processes the database records on this host, its Running local runs' and running
    conversions' (``dispatch.live_recorded_processes``); none for a database that cannot be read (it is rebuilt)."""
    try:
        engine = database_engine(database, create=False)
    except Exception:
        return []
    try:
        with Session(engine) as session:
            return [record.pid for record in live_recorded_processes(session)]
    except SQLAlchemyError:
        return []
    finally:
        engine.dispose()


def _listening_worker(database: Path) -> str | None:
    """The worker serving ``database`` (it holds the database's worker lock), as its row names it; None without."""
    lock = take_lock(lock_path(database))
    if lock is not None:
        lock.close()
        return None
    engine = database_engine(DatabaseSettings(path=database), create=False)
    try:
        return holder(engine)
    finally:
        engine.dispose()


def _wait(
    engine: Engine,
    runs: demo.DemoRuns,
    *,
    report: Report,
    started_within: float | None = None,
    process: subprocess.Popen[bytes] | None = None,
) -> bool:
    """Wait until R1 and I1 are terminal (True). False when ``started_within`` passed with both still queued, or
    when ``process`` exited first. Raises ``SeedError`` after ``RUN_TIMEOUT_SECONDS``."""

    labels = _labels(runs)
    start = time.monotonic()
    reported: set[int] = set()
    while True:
        exited = process is not None and process.poll() is not None
        jobs = _jobs(engine, runs.job_ids)
        for job_id, job in jobs.items():
            if is_terminal(job) and job_id not in reported:
                reported.add(job_id)
                report(f"  {labels[job_id]}: {STATUS_NAMES[JobStatus(job.status)]}")
        if len(reported) == len(runs.job_ids):
            return True
        if exited:
            return False
        waited = time.monotonic() - start
        if started_within is not None and waited > started_within:
            if all(job.phase == RunPhase.QUEUED for job in jobs.values()):
                return False
            started_within = None  # it started them: wait for the end
        if waited > RUN_TIMEOUT_SECONDS:
            raise SeedError(f"R1 and I1 did not finish within {RUN_TIMEOUT_SECONDS:.0f} s")
        time.sleep(POLL_SECONDS)


def _tail(path: Path) -> str:
    try:
        return path.read_text(errors="replace")[-LOG_TAIL_CHARACTERS:]
    except OSError:
        return "(no log)"


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Stop a burst worker this module started (its own session: the worker and its job processes)."""
    if process.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _burst_worker(runs: demo.DemoRuns, *, engine: Engine, database: Path, report: Report) -> str:
    name = "laueportal worker --burst"
    environment: Mapping[str, str] = {**os.environ, "LAUEPORTAL_DATABASE__PATH": str(database)}
    with tempfile.NamedTemporaryFile(prefix="laueportal-seed-worker-", suffix=".log", delete=False) as log:
        report(f"  running R1 and I1 with burst worker {name} (log: {log.name})")
        process = subprocess.Popen(
            [sys.executable, "-m", "laueportal", "worker", "--burst"],
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    log_path = Path(log.name)
    try:
        _wait(engine, runs, report=report, process=process)
        returncode = process.wait(timeout=120)  # the queue is empty once both are terminal
    except BaseException:
        _stop(process)
        raise
    if returncode != 0:
        raise SeedError(
            f"burst worker {name} exited with status {returncode}; its log ({log_path}):\n{_tail(log_path)}"
        )
    log_path.unlink(missing_ok=True)
    return "a burst worker"


def _run_to_finish(runs: demo.DemoRuns, *, engine: Engine, database: Path, report: Report) -> str:
    worker = _listening_worker(database)
    if worker is not None:
        report(f"  waiting for the worker ({worker}) to run R1 and I1")
        if _wait(engine, runs, report=report, started_within=WORKER_START_SECONDS):
            return f"the worker ({worker})"
        holding = _listening_worker(database)
        if holding is not None:  # a burst worker would only exit 1 at the lock
            raise SeedError(
                f"the worker ({holding}) holds {lock_path(database)} but did not start R1 and I1 within "
                f"{WORKER_START_SECONDS:.0f} s; see its log, or stop it and seed again (a burst worker then runs them)"
            )
        report(f"  the worker stopped before it started them (within {WORKER_START_SECONDS:.0f} s)")
    return _burst_worker(runs, engine=engine, database=database, report=report)


def _require_finished(engine: Engine, runs: demo.DemoRuns) -> None:
    labels = _labels(runs)
    for job_id, job in _jobs(engine, runs.job_ids).items():
        if job.status != JobStatus.FINISHED:
            last = (job.messages or "").strip().splitlines()[-1:] or ["no message"]
            raise SeedError(f"{labels[job_id]} ended {STATUS_NAMES[JobStatus(job.status)]}: {last[0]}")
