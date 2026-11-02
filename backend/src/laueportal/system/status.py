"""Health probes behind ``GET /api/v1/health`` and the system status behind ``GET /api/v1/system/status``.

Each probe catches its own errors and gives up after about a second, so ``collect_health`` and ``collect_status``
never raise and never hang on a locked database. A probe logs when its result changes.

The worker is its row's (``jobs.worker``): ``running`` while its newest mark (or its start, before
its first) is at most ``WORKER_LATE_SECONDS`` old, ``late`` after that, ``not_running`` with no row or its stop
recorded, and also when the row's process is gone on the API's own host (``jobs.liveness.worker_process_alive``), so
a worker killed outright (an OOM kill, a crash, a reboot) says not running, not late: no
process with its pid; once its marks are late, also a pid another process took since (started after the row's
``started_at``: the wall clock's, so while the marks are fresh the pid alone counts, and a clock stepped forward
never makes a live worker look gone). On another host the marks alone decide. Each thread (``THREADS``: the dispatcher, the reconciler, the remote poller) is ``ok`` while its mark (or
the worker's start, before its first) is at most ``late_after`` its cycle old, ``late`` after that, and ``off`` when
it does not run: the poller without ``[remote] enabled``, and every thread of a worker that is not running. With
remote execution enabled but settings the worker cannot use, the poller has no thread and no mark: it goes late,
never ``off``. A thread's cycle is its interval (the dispatcher's ``execution.dispatch_seconds``, the reconciler's
``reconcile_seconds``, the poller's ``poll_seconds`` of ``bdp.yaml``), at least ``execution.heartbeat_seconds``, the
most often a mark is written.

The system status adds the database's path, the queue's counts from the job and conversion tables (the job table is
the queue) with the live slots in use (the Running live runs), the next items in the dispatcher's
own order (``jobs.order.queued_in_order``: the Queued live runs first) and the active runs
(``jobs.progress.load_active_runs``): what needs an unreadable database is null.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Engine, Select, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from laueportal import __version__
from laueportal.core import db
from laueportal.core.settings import Settings
from laueportal.core.timeutil import utcnow
from laueportal.jobs.backends import is_remote
from laueportal.jobs.progress import ActiveJob, heartbeat_age_seconds, is_stale, load_active_runs
from laueportal.jobs.schemas import RunReference, RunStatus
from laueportal.system.schemas import (
    ActiveRun,
    DatabaseStatus,
    Health,
    ProbeStatus,
    QueueCounts,
    QueuedItem,
    RemoteRunState,
    SystemStatus,
    ThreadState,
    WorkerState,
    WorkerStatus,
    WorkerThread,
)

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 1.0

WORKER_LATE_SECONDS = 45.0
"""The worker is late once its newest mark is older; a thread's limit is never shorter."""

LATE_CYCLES = 3
"""A thread is late once its mark is older than this many of its cycles (or ``WORKER_LATE_SECONDS``)."""

NEXT_UP = 5
"""How many of the next queued items the status lists."""

ThreadName = Literal["dispatcher", "reconciler", "poller"]
THREADS: tuple[ThreadName, ...] = ("dispatcher", "reconciler", "poller")

_last_status: dict[str, ProbeStatus] = {}
_last_status_lock = threading.Lock()


def _record(probe: str, status: ProbeStatus, error: BaseException | None = None) -> ProbeStatus:
    with _last_status_lock:
        previous = _last_status.get(probe)
        _last_status[probe] = status
    if status == "error" and previous != "error":
        logger.warning("health: %s unavailable: %s: %s", probe, type(error).__name__, error)
    elif status == "ok" and previous == "error":
        logger.info("health: %s available again", probe)
    elif error is not None:
        logger.debug("health: %s still unavailable: %s", probe, error)
    return status


def database_status(engine: Engine) -> ProbeStatus:
    try:
        db.ping(engine, timeout_ms=int(PROBE_TIMEOUT_SECONDS * 1000))
    except Exception as exc:
        return _record("database", "error", exc)
    return _record("database", "ok")


# ---------------------------------------------------------------------------
# The worker and its threads


@dataclass(frozen=True)
class WorkerRow:
    """A snapshot of the worker row (``jobs.models.Worker``)."""

    host: str
    pid: int
    started_at: datetime
    stopped_at: datetime | None
    marks: dict[ThreadName, datetime | None]


def read_worker(engine: Engine) -> WorkerRow | None:
    """The worker row, or None when no worker ever started on this database. Raises the database's errors."""
    from laueportal.jobs.models import WORKER_ROW, Worker

    with Session(engine) as session:
        row = session.get(Worker, WORKER_ROW)
        if row is None:
            return None
        marks: dict[ThreadName, datetime | None] = {
            "dispatcher": row.dispatcher_at,
            "reconciler": row.reconciler_at,
            "poller": row.poller_at,
        }
        return WorkerRow(row.host, row.pid, row.started_at, row.stopped_at, marks)


def _age(at: datetime, now: datetime) -> float:
    return (now - at).total_seconds()


def worker_state(row: WorkerRow | None, *, now: datetime) -> WorkerState:
    """``not_running`` without a row, with its stop recorded, or with its process gone on this host (the pid alone
    while its marks are fresh, its start too once they are late); else ``running`` while its newest mark (its start
    before any) is at most ``WORKER_LATE_SECONDS`` old, ``late`` after that."""
    from laueportal.jobs.liveness import worker_process_alive

    if row is None or row.stopped_at is not None:
        return "not_running"
    newest = max([row.started_at, *(at for at in row.marks.values() if at is not None)])
    fresh = _age(newest, now) <= WORKER_LATE_SECONDS
    if worker_process_alive(row.host, row.pid, None if fresh else row.started_at) is False:
        return "not_running"
    return "running" if fresh else "late"


def _poll_seconds(settings: Settings) -> float:
    """The poller's interval (``bdp.yaml``'s ``poll_seconds``); its default when the file cannot be read (the
    worker then runs no poller, which the missing mark shows late)."""
    from laueportal.remote.settings import RemoteSettings, load_settings

    default = float(RemoteSettings.model_fields["poll_seconds"].default)
    file = settings.remote.settings_file
    if file is None:
        return default
    try:
        return float(load_settings(file).poll_seconds)
    except Exception:  # the worker refuses such settings too; its poller is then not running
        return default


def thread_cycles(settings: Settings) -> dict[ThreadName, float | None]:
    """Each thread's cycle in seconds (its interval, at least ``execution.heartbeat_seconds``); None for a thread
    that does not run (the poller without remote execution)."""
    execution = settings.execution
    floor = execution.heartbeat_seconds
    poller = max(_poll_seconds(settings), floor) if settings.remote.enabled else None
    return {
        "dispatcher": max(execution.dispatch_seconds, floor),
        "reconciler": max(float(execution.reconcile_seconds), floor),
        "poller": poller,
    }


def late_after(cycle: float | None) -> float:
    """Seconds after which a thread's mark is late: ``LATE_CYCLES`` cycles or ``WORKER_LATE_SECONDS``."""
    return max(LATE_CYCLES * (cycle or 0.0), WORKER_LATE_SECONDS)


def worker_threads(
    row: WorkerRow | None, state: WorkerState, cycles: dict[ThreadName, float | None], *, now: datetime
) -> list[WorkerThread]:
    threads = []
    for name in THREADS:
        cycle = cycles[name]
        limit = late_after(cycle)
        mark = row.marks[name] if row is not None else None
        thread_state: ThreadState
        if row is None or state == "not_running" or cycle is None:
            thread_state = "off"
        else:
            thread_state = "ok" if _age(mark or row.started_at, now) <= limit else "late"
        threads.append(
            WorkerThread(
                name=name,
                state=thread_state,
                last_mark_at=mark,
                last_mark_age_seconds=None if mark is None else max(_age(mark, now), 0.0),
                late_after_seconds=None if thread_state == "off" else limit,
            )
        )
    return threads


def worker_status(settings: Settings, row: WorkerRow | None, *, now: datetime) -> WorkerStatus:
    state = worker_state(row, now=now)
    return WorkerStatus(
        state=state,
        host=row.host if row else None,
        pid=row.pid if row else None,
        started_at=row.started_at if row else None,
        stopped_at=row.stopped_at if row else None,
        threads=worker_threads(row, state, thread_cycles(settings), now=now),
    )


def _read_worker_logged(engine: Engine) -> tuple[bool, WorkerRow | None]:
    """(read, row): read is False when the database refused (logged on change)."""
    try:
        row = read_worker(engine)
    except (SQLAlchemyError, db.DatabaseMissing) as exc:
        _record("worker row", "error", exc)
        return False, None
    _record("worker row", "ok")
    return True, row


def collect_health(settings: Settings, engine: Engine) -> Health:
    database = database_status(engine)
    worker: WorkerState | None = None
    if database == "ok":
        read, row = _read_worker_logged(engine)
        worker = worker_state(row, now=utcnow()) if read else None
    return Health(db=database, worker=worker, version=__version__)


# ---------------------------------------------------------------------------
# The queue


def queue_counts(session: Session) -> QueueCounts:
    """The queue's counts from the job and conversion tables, on the dispatcher's own predicates
    (``jobs.order``), and the live slots in use (its Running live runs)."""
    from laueportal.indexing.models import Conversion
    from laueportal.jobs.models import Job
    from laueportal.jobs.order import (
        CONVERSION_RUNNING,
        LIVE_SLOTS,
        local_jobs,
        queued_conversions,
        queued_runs,
        running_live_runs,
    )
    from laueportal.jobs.state import JobStatus

    def count(query: Select[Any]) -> int:
        return int(session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)

    running = count(select(Job.job_id).where(Job.status == int(JobStatus.RUNNING), local_jobs()))
    running += count(select(Conversion.indexing_id).where(Conversion.status == CONVERSION_RUNNING))
    return QueueCounts(
        queued_runs=count(queued_runs()),
        running=running,
        queued_conversions=count(queued_conversions()),
        live_slots_in_use=count(running_live_runs()),
        live_slots=LIVE_SLOTS,
    )


def next_up(session: Session, *, limit: int = NEXT_UP) -> list[QueuedItem]:
    """The next ``limit`` items the dispatcher takes, in its own order (``jobs.order.queued_in_order``)."""
    from laueportal.indexing.models import IndexingRun
    from laueportal.jobs.models import Job
    from laueportal.jobs.order import CONVERT, queued_in_order
    from laueportal.reconstruction.models import ReconstructionRun

    items = queued_in_order(session, limit=limit)
    job_ids = [key for kind, key in items if kind != CONVERT]
    runs: dict[int, RunReference | None] = {}
    moved: set[int] = set()
    if job_ids:
        statement = (
            select(Job.job_id, Job.moved_to_front_at, IndexingRun.id, ReconstructionRun.id)
            .outerjoin(IndexingRun, IndexingRun.job_id == Job.job_id)
            .outerjoin(ReconstructionRun, ReconstructionRun.job_id == Job.job_id)
            .where(Job.job_id.in_(job_ids))
        )
        for job_id, moved_at, indexing_id, reconstruction_id in session.execute(statement):
            runs[job_id] = RunReference.of(indexing_id=indexing_id, reconstruction_id=reconstruction_id)
            if moved_at is not None:
                moved.add(job_id)
    return [
        QueuedItem(
            kind="conversion",
            job_id=None,
            run=RunReference.of(indexing_id=key, reconstruction_id=None),
            moved_to_front=False,
        )
        if kind == CONVERT
        else QueuedItem(kind="run", job_id=key, run=runs.get(key), moved_to_front=key in moved)
        for kind, key in items
    ]


# ---------------------------------------------------------------------------
# System status


def _active_run(job: ActiveJob, *, stale_after_seconds: float, now: datetime) -> ActiveRun:
    progress = job.progress
    remote = None
    if progress.backend is not None and is_remote(progress.backend):
        remote = RemoteRunState(target=progress.backend, dm_state=progress.external_state)
    return ActiveRun(
        job_id=progress.job_id,
        run=RunReference.of(indexing_id=job.indexing_id, reconstruction_id=job.reconstruction_id),
        status=RunStatus.of(progress.status),
        phase=progress.phase,
        n_inputs=progress.n_inputs,
        n_succeeded=progress.n_succeeded,
        n_failed=progress.n_failed,
        n_not_run=progress.n_not_run,
        heartbeat_age_seconds=heartbeat_age_seconds(progress, now=now),
        stale=is_stale(progress, stale_after_seconds=stale_after_seconds, now=now),
        stop_requested=progress.cancel_requested_at is not None,
        remote=remote,
    )


@dataclass(frozen=True)
class _Tables:
    queue: QueueCounts
    next_up: list[QueuedItem]
    active_runs: list[ActiveRun]


def _read_tables(session: Session, settings: Settings, now: datetime) -> _Tables | None:
    """The queue, the next items and the active runs, in one session; None when the database cannot be read
    (logged on change)."""
    try:
        queue = queue_counts(session)
        upcoming = next_up(session)
        jobs = load_active_runs(session)
    except (SQLAlchemyError, db.DatabaseMissing) as exc:  # e.g. no tables yet, or the file removed after the probe
        session.rollback()
        _record("active runs", "error", exc)
        return None
    _record("active runs", "ok")
    stale_after = settings.execution.stale_heartbeat_seconds
    runs = [_active_run(job, stale_after_seconds=stale_after, now=now) for job in jobs]
    return _Tables(queue, upcoming, runs)


def collect_status(settings: Settings, engine: Engine, session: Session) -> SystemStatus:
    """``GET /api/v1/system/status``. Never raises because the database is down; the tables are read only once
    the database probe answered, so a missing database file is never created."""
    database = database_status(engine)
    now = utcnow()
    worker = None
    tables = None
    if database == "ok":
        read, row = _read_worker_logged(engine)
        worker = worker_status(settings, row, now=now) if read else None
        tables = _read_tables(session, settings, now)
    return SystemStatus(
        version=__version__,
        database=DatabaseStatus(status=database, path=str(settings.database.path)),
        worker=worker,
        queue=tables.queue if tables else None,
        next_up=tables.next_up if tables else None,
        active_runs=tables.active_runs if tables else None,
    )
