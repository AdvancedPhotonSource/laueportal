"""The worker: ``laueportal worker``, one process whose threads run the portal's work.

``run_worker(settings)`` is the entry point (``laueportal worker [--burst]`` configures logging, then calls it):

1. **The lock and the row.** It takes an exclusive ``flock`` on ``<database>.worker.lock`` (``lock_path``: beside
   the file the database path resolves to, so a symlink to the database names the same lock; local disk) for
   its lifetime; a second worker on the same database exits 1 and names the holder from the worker row. It then
   replaces the one ``worker`` row (host, pid, ``started_at``; ``stopped_at`` and the threads' marks cleared). A
   missing database is refused (exit 1), never created.
2. **The threads.** The dispatcher (``jobs.dispatch.Dispatcher``: the job table is the queue; it marks
   ``dispatcher_at``), the reconciler (``jobs.reconciler.reconcile_once`` at once, then every
   ``execution.reconcile_seconds``, never finalizing a row whose job process the dispatcher started and still runs;
   it marks ``reconciler_at``) and, only when remote execution is enabled (``[remote] enabled``; never in a burst),
   the remote poller (``remote.poller.RemotePoller``, with an engine of its own: a pass every ``poll_seconds`` of
   ``bdp.yaml`` under ``<submission_directory>/.poller.lock``, which it holds for its lifetime and waits for while
   another process holds it, with one DM call per pass for every active remote run and none without one; it marks
   ``poller_at``). Remote settings that cannot be used leave the poller off, logged, and the other threads run.
   Each writes its mark at most every ``execution.heartbeat_seconds``. A database error in a cycle
   (``OperationalError``) is logged once until a cycle gets through, and the next cycle tries again
   (``core.db.DatabaseErrors``). A thread that raises is logged and started again after ``RESTART_FIRST_SECONDS``
   (10 s), twice as long each time up to ``RESTART_MAX_SECONDS`` (5 min), and after ``RESTART_FIRST_SECONDS`` again
   when it had run healthily (``HEALTHY_INTERVALS`` of its own interval) before it raised; the other threads carry
   on. SIGCHLD wakes the dispatcher, so it reaps an exited job process and takes the next item at once. The worker
   ignores SIGUSR1 itself, so that its job processes start ignoring it (``jobs.process``); the DM calls' processes
   inherit that too, which is intended (``remote.dm``).
3. **The service stop.** supervisor's SIGTERM (or SIGINT): the dispatcher claims nothing more, every job process on
   this host that a row records gets SIGUSR1 (``lifecycle.SHUTDOWN_SIGNAL``: a run drains and ends
   Failed/interrupted; a conversion finishes), again on each poll (a job process still starting gets it once its
   handling is in place), until ``execution.shutdown_grace_seconds`` after the SIGTERM; then the worker kills their
   process groups. A second SIGTERM kills them at once. A job process the dispatcher never recorded (its record
   waited on a write lock) gets no go line and exits unrun, whether or not the dispatcher thread runs. The worker
   then lets its threads end (the remote poller after its current pass, within the same bounded join) and, in its
   last write (``stop_row``), ends the items of those job processes (the run Failed/interrupted "Stopped by service
   shutdown before the run started", the conversion ``failed``; ``dispatch.stop_unstarted``) and sets the row's
   ``stopped_at``. A DM call or launcher command the poller's pass still runs then is killed with everything it
   started (``RemotePoller.close``: ``remote.dm.DMClient.abort``) before the worker lets the poller lock go and
   exits 0; a publication still running ends with the process, and the next poller finishes it. The whole stop
   takes at most the grace plus about 46 s (``_service_stop``), within supervisor's ``stopwaitsecs`` (the grace plus
   60 s).

A burst worker (``--burst``, the seed's) stops the same way once nothing is queued and none of its job processes
runs. Nothing happens at import time.
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Collection
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import IO, TYPE_CHECKING, Any

from sqlalchemy import Engine, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal.core.db import DatabaseErrors, begin_write, make_engine
from laueportal.core.settings import Settings
from laueportal.core.timeutil import utcnow
from laueportal.jobs.dispatch import Dispatcher, DispatchPolicy, JobLog, Kind, LivePolicy, stop_unstarted
from laueportal.jobs.lifecycle import SHUTDOWN_SIGNAL
from laueportal.jobs.models import WORKER_ROW, Worker
from laueportal.jobs.reconciler import Own, ReconcilePolicy, reconcile_once
from laueportal.jobs.reconciler import logger as reconciler_logger

if TYPE_CHECKING:
    from laueportal.remote.poller import RemotePoller

logger = logging.getLogger(__name__)

LOCK_SUFFIX = ".worker.lock"
RESTART_FIRST_SECONDS = 10.0
RESTART_MAX_SECONDS = 300.0
HEALTHY_INTERVALS = 10
"""A thread whose body ran for this many of its own intervals (the dispatcher's ``execution.dispatch_seconds``, the
reconciler's ``execution.reconcile_seconds``) before it raised was healthy: its restart waits
``RESTART_FIRST_SECONDS`` again, so isolated errors never build up to ``RESTART_MAX_SECONDS``."""
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
STOP_POLL_SECONDS = 0.2
KILL_WAIT_SECONDS = 5.0
"""How long the service stop waits, in all, for the job processes it killed to exit."""
THREAD_JOIN_SECONDS = 10.0
"""How long the service stop waits, in all, for the worker's threads to end once its job processes are gone."""


def lock_path(database: Path) -> Path:
    """The worker lock of ``database``: beside the file the path resolves to, so every path to one database (a
    symlink to it, a relative one) names one lock. The worker and the seed use it."""
    resolved = database.expanduser().resolve()
    return resolved.with_name(resolved.name + LOCK_SUFFIX)


def take_lock(path: Path) -> IO[bytes] | None:
    """The worker lock (an exclusive ``flock``, held while the returned file stays open), or None when another
    process holds it."""
    handle = path.open("ab")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def holder(engine: Engine) -> str:
    """Who the worker row says the worker is (for the refusal of a second one)."""
    with Session(engine) as session:
        row = session.get(Worker, WORKER_ROW)
        if row is None:
            return "no worker row"
        return f"pid {row.pid} on {row.host}, started {row.started_at.isoformat(timespec='seconds')}"


def start_row(engine: Engine) -> None:
    """Replace the worker row: this process, started now, its marks cleared."""
    with Session(engine) as session, session.begin():
        begin_write(session)
        row = session.get(Worker, WORKER_ROW)
        if row is None:
            row = Worker(name=WORKER_ROW)
            session.add(row)
        row.host, row.pid, row.started_at = socket.gethostname(), os.getpid(), utcnow()
        row.stopped_at = row.dispatcher_at = row.reconciler_at = row.poller_at = None


def mark(engine: Engine, column: str, at: datetime) -> None:
    """Write a thread's mark (``dispatcher_at``, ``reconciler_at``, ``poller_at``) on this worker's row."""
    with Session(engine) as session, session.begin():
        session.execute(update(Worker).where(Worker.name == WORKER_ROW, Worker.pid == os.getpid()).values({column: at}))


def stop_row(engine: Engine, unstarted: Collection[tuple[Kind, int]] = ()) -> None:
    """The worker's last write, one transaction: the items whose job process its stop let go unrecorded end
    (``dispatch.stop_unstarted``), and the row's ``stopped_at`` is set. Should the database stay write-locked past
    its busy timeout, it raises, and such an item is left Running with no process recorded, which the next worker's
    reconciler fails once its claim's heartbeat is stale."""
    with Session(engine) as session, session.begin():
        begin_write(session)
        for kind, key in unstarted:
            stop_unstarted(session, kind, key)
        session.execute(
            update(Worker).where(Worker.name == WORKER_ROW, Worker.pid == os.getpid()).values(stopped_at=utcnow())
        )


class Supervised(threading.Thread):
    """A worker thread: ``body`` runs until it returns or ``stopped`` is set; when it raises, the error is logged
    and it starts again after ``RESTART_FIRST_SECONDS``, twice as long each time up to ``RESTART_MAX_SECONDS``; a
    body that ran for ``HEALTHY_INTERVALS`` times its ``interval`` (seconds) before it raised starts again after
    ``RESTART_FIRST_SECONDS``."""

    def __init__(self, name: str, body: Callable[[], None], stopped: threading.Event, *, interval: float) -> None:
        super().__init__(name=name, daemon=True)
        self._body = body
        self._stopped = stopped
        self.healthy_seconds = HEALTHY_INTERVALS * interval
        self.restarts = 0

    def run(self) -> None:
        delay = RESTART_FIRST_SECONDS
        while not self._stopped.is_set():
            began = time.monotonic()
            try:
                self._body()
                return
            except Exception:
                if time.monotonic() - began >= self.healthy_seconds:
                    delay = RESTART_FIRST_SECONDS
                logger.exception("The worker's %s thread raised; it starts again in %g s", self.name, delay)
            if self._stopped.wait(delay):
                return
            self.restarts += 1
            delay = min(2 * delay, RESTART_MAX_SECONDS)


def reconciler_body(
    engine: Engine, policy: ReconcilePolicy, stopped: threading.Event, *, own: Own | None = None
) -> Callable[[], None]:
    """The reconciler thread's body: a cycle at once, then every ``reconcile_seconds``; ``own``: the dispatcher's
    running job processes, which the cycle never finalizes (``jobs.reconciler``). A cycle that hits a database error
    ends logged (once until a cycle gets through); the next cycle tries again."""
    database = DatabaseErrors(reconciler_logger, "reconciler")

    def body() -> None:
        while not stopped.is_set():
            try:
                reconcile_once(engine, policy, own=own)
                mark(engine, "reconciler_at", utcnow())
            except OperationalError as error:
                database.failed(error)
            else:
                database.succeeded()
            if stopped.wait(policy.reconcile_seconds):
                return

    return body


def remote_poller(settings: Settings, stopped: threading.Event) -> RemotePoller | None:
    """The remote poller thread's state, with an engine of its own, when remote execution is enabled; None when it is
    disabled, or when its settings cannot be used (logged: the other threads run without it)."""
    if not settings.remote.enabled:
        return None
    from laueportal.remote.poller import RemotePoller
    from laueportal.remote.settings import RemoteSettingsError, portal_settings

    try:
        remote = portal_settings(settings.remote, runs_root=settings.files.runs_root)
        if remote is None:
            return None
        remote.submissions()
    except RemoteSettingsError as error:
        logger.error(
            "Remote execution is enabled, but its settings cannot be used (%s): the remote poller does not run until "
            "the worker starts again (`laueportal remote poll --once` says why)",
            error,
        )
        return None
    engine = make_engine(settings, create=False)
    return RemotePoller(
        remote,
        engine=engine,
        stopped=stopped,
        mark=lambda at: mark(engine, "poller_at", at),
        heartbeat_seconds=settings.execution.heartbeat_seconds,
    )


class _Stops:
    """The stop signals as they come: the first asks for the service stop, the second for an immediate one."""

    def __init__(self) -> None:
        self.requested = threading.Event()
        self.forced = threading.Event()
        self.at: float | None = None
        """``time.monotonic()`` at the first stop signal: the grace counts from it."""

    def handle(self, signum: int, frame: FrameType | None) -> None:
        if self.requested.is_set():
            self.forced.set()
        else:
            self.at = time.monotonic()
        self.requested.set()


def dispatch_policy(settings: Settings) -> DispatchPolicy:
    from laueportal.indexing.conversion import CONVERSION_TIMEOUT_SECONDS

    execution, live = settings.execution, settings.live
    return DispatchPolicy(
        dispatch_seconds=execution.dispatch_seconds,
        job_timeout_seconds=execution.job_timeout_seconds,
        conversion_timeout_seconds=CONVERSION_TIMEOUT_SECONDS,
        shutdown_grace_seconds=execution.shutdown_grace_seconds,
        heartbeat_seconds=execution.heartbeat_seconds,
        live=LivePolicy(
            batch_stop_seconds=live.batch_stop_seconds,
            cpu_share=live.cpu_share,
            wire_share=live.wire_share,
            cpu_count=os.cpu_count() or 1,
        ),
    )


def run_worker(settings: Settings, *, burst: bool = False) -> int:
    """Run the worker until stopped (the module's steps); returns an exit status: 0 after a stop (or, in burst mode,
    once the queue is empty), 1 for a missing database or a lock another worker holds."""

    database = settings.database.path
    if not database.is_file():  # SQLite would create an empty file on connect
        logger.error("Database %s does not exist (create it with `laueportal db upgrade`)", database)
        return 1
    engine = make_engine(settings, create=False)
    lock = take_lock(lock_path(database))
    if lock is None:
        logger.error(
            "Another worker holds %s (%s); one worker per database: exiting", lock_path(database), holder(engine)
        )
        engine.dispose()
        return 1
    stops = _Stops()
    stopped = threading.Event()
    handled = (*STOP_SIGNALS, signal.SIGCHLD, SHUTDOWN_SIGNAL)
    handlers: dict[int, Any] = {signum: signal.getsignal(signum) for signum in handled}
    dispatcher = Dispatcher(
        engine,
        dispatch_policy(settings),
        mark=lambda at: mark(engine, "dispatcher_at", at),
        start=JobLog(settings.execution.job_log).start,
    )
    threads: list[Supervised] = []
    poller: RemotePoller | None = None
    try:
        start_row(engine)
        # Inherited across exec: a job process ignores the stop's signal until its own handling is in place, so a
        # stop never kills one that is still starting (jobs.process); the stop sends it again on each poll.
        signal.signal(SHUTDOWN_SIGNAL, signal.SIG_IGN)
        for signum in STOP_SIGNALS:
            signal.signal(signum, stops.handle)
        signal.signal(signal.SIGCHLD, lambda signum, frame: dispatcher.wake())
        logger.info("Starting the worker on %s (pid %s): %s", database, os.getpid(), dispatcher.policy)
        reconcile = ReconcilePolicy.from_settings(settings.execution)
        threads = [
            Supervised("dispatcher", dispatcher.run_forever, stopped, interval=dispatcher.policy.dispatch_seconds),
            Supervised(
                "reconciler",
                reconciler_body(engine, reconcile, stopped, own=dispatcher.running_items),
                stopped,
                interval=reconcile.reconcile_seconds,
            ),
        ]
        for thread in threads:
            thread.start()
        # After the others: bdp.yaml's checks look at the experiment directory, a slow mount must not hold them.
        poller = None if burst else remote_poller(settings, stopped)  # a burst runs the queue only
        if poller is not None:
            threads.append(Supervised("poller", poller.run_forever, stopped, interval=poller.poll_seconds))
            threads[-1].start()
        while not stops.requested.wait(STOP_POLL_SECONDS):
            if burst and dispatcher.idle():
                logger.info("Nothing is queued or running: the burst ends")
                break
        began = stops.at if stops.at is not None else time.monotonic()  # a burst's end: now
        grace = settings.execution.shutdown_grace_seconds
        unstarted = _service_stop(dispatcher, threads, stopped, stops, grace, began=began)
        stop_row(engine, unstarted)
        logger.info("The worker stopped")
        return 0
    finally:
        stopped.set()
        dispatcher.stop()
        for signum, handler in handlers.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)
        if poller is not None:
            poller.close()
            poller.engine.dispose()
        lock.close()
        engine.dispose()


def _service_stop(
    dispatcher: Dispatcher,
    threads: list[Supervised],
    stopped: threading.Event,
    stops: _Stops,
    grace: float,
    *,
    began: float,
) -> list[tuple[Kind, int]]:
    """Claim nothing more; let go unrecorded the job processes never recorded (``Dispatcher.let_go_unrecorded``,
    here and not in the dispatcher thread, which may be in its restart wait); ask the job processes to drain, again
    on each poll, until ``grace`` after the stop signal (``began``, ``time.monotonic()``); kill what is left; then let
    the threads end (the remote poller ends after its current pass; the join waits for it within the same bound,
    and the worker's end kills a DM call the pass still runs: ``RemotePoller.close``).
    Returns the items let go unrecorded (``stop_row`` ends them).

    A job process still starting ignores the signal (inherited from the worker) until its own handling is in place
    (``jobs.process``); the next poll's signal then reaches it. The worst case from the stop signal is the grace, one
    poll, the kill's wait (``KILL_WAIT_SECONDS``), the threads' (``THREAD_JOIN_SECONDS``), the worker row's write
    (the database's busy timeout, 30 s) and the end of the poller's DM call (``remote.dm.ABORT_WAIT_SECONDS``, 1 s):
    the grace plus about 46 s, inside supervisor's ``stopwaitsecs`` (the grace plus 60 s in every template)."""

    dispatcher.stop()
    stopped.set()
    unstarted = dispatcher.let_go_unrecorded()
    deadline = began + grace
    asked = False
    while pids := dispatcher.signal_job_processes(SHUTDOWN_SIGNAL):
        if not asked:
            asked = True
            logger.warning("Asked job process(es) %s to stop; waiting up to %g s for them to drain", pids, grace)
        if stops.forced.is_set() or time.monotonic() >= deadline:
            killed = dispatcher.kill_job_processes(wait=KILL_WAIT_SECONDS)
            why = "a second stop signal" if stops.forced.is_set() else f"{grace:g} s"
            logger.warning("Killed the job process group(s) %s after %s", killed, why)
            break
        stops.forced.wait(STOP_POLL_SECONDS)
    ends = time.monotonic() + THREAD_JOIN_SECONDS
    for thread in threads:
        thread.join(max(0.0, ends - time.monotonic()))
    return unstarted
