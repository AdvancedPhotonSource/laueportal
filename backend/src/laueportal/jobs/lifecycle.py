"""Run lifecycle support for the executor and the worker: heartbeat, progress persistence, process cleanup.

Every database write here goes through ``laueportal.jobs.state`` so
the counters stay consistent. The monitor thread owns its own sessions; the
progress recorder runs on the executor's thread. Reconciling runs that can no longer be alive is
``jobs.reconciler``'s.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable

import psutil
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from laueportal.jobs import state
from laueportal.jobs.models import Job
from laueportal.jobs.processes import is_helper_process
from laueportal.jobs.processes import terminate_descendants as terminate_descendants
from laueportal.jobs.queue import RunPolicy
from laueportal.jobs.state import RunPhase

logger = logging.getLogger(__name__)

STOP_REASON_CANCELLED = "cancelled"
STOP_REASON_SHUTDOWN = "shutdown"

# The worker sends this to its job processes when the service is asked to stop; the
# executor then stops admitting inputs, drains, and finalizes as interrupted.
SHUTDOWN_SIGNAL = signal.SIGUSR1


class RunTimeout(Exception):
    """A job process's own timeout expired (``jobs.process``: SIGALRM after ``execution.job_timeout_seconds``); the
    executor records the run Failed for exceeding the configured limit."""


class RunMonitor(threading.Thread):
    """Background heartbeat and cancellation polling for one running job.

    The heartbeat continues while the compute is inside a long native call, so
    a stale heartbeat always means the run process is gone. ``stop_requested``
    becomes True when the job's persisted cancellation flag is set or when the
    executor receives the shutdown signal (``shutdown``: the job process's own
    event, which its handler may have set before the run began).
    """

    def __init__(
        self,
        engine: Engine,
        job_id: int,
        policy: RunPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        shutdown: threading.Event | None = None,
    ):
        super().__init__(name=f"run-monitor-{job_id}", daemon=True)
        self._engine = engine
        self._job_id = job_id
        self._policy = policy
        self._clock = clock
        self._closed = threading.Event()
        self._cancel = threading.Event()
        self._shutdown = shutdown if shutdown is not None else threading.Event()
        self._shutdown_requested = False
        self.heartbeats = 0
        self.errors: list[str] = []

    @property
    def stop_requested(self) -> bool:
        return self._cancel.is_set() or self._shutdown.is_set()

    @property
    def stop_reason(self) -> str | None:
        if self._shutdown.is_set():
            return STOP_REASON_SHUTDOWN
        if self._cancel.is_set():
            return STOP_REASON_CANCELLED
        return None

    def request_shutdown(self) -> None:
        """Record the service stop. The executor's handler of the stop signal calls it, and the handler can run again
        inside itself (the signal repeats); only the first call sets the event, whose ``set()`` takes a lock that
        the inner call would wait for (``jobs.process._record_stop``)."""
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        self._shutdown.set()

    def poll(self) -> None:
        """One heartbeat write plus one cancellation check; called periodically and on demand."""

        try:
            with Session(self._engine) as session, session.begin():
                job = session.get(Job, self._job_id)
                if job is None:
                    return
                if job.cancel_requested_at is not None:
                    self._cancel.set()
                if not state.is_terminal(job):
                    state.heartbeat(job)
                    self.heartbeats += 1
        except Exception as error:  # the run must not die because a heartbeat write failed
            self.errors.append(str(error))
            logger.warning("Run %s heartbeat failed: %s", self._job_id, error)

    def run(self) -> None:
        interval = min(self._policy.heartbeat_seconds, self._policy.cancel_poll_seconds)
        next_heartbeat = self._clock()
        while not self._closed.is_set():
            now = self._clock()
            if now >= next_heartbeat:
                self.poll()
                next_heartbeat = now + self._policy.heartbeat_seconds
            elif not self._cancel.is_set():
                self._check_cancellation()
            self._closed.wait(interval)

    def _check_cancellation(self) -> None:
        try:
            with Session(self._engine) as session:
                flag = session.scalar(select(Job.cancel_requested_at).where(Job.job_id == self._job_id))
            if flag is not None:
                self._cancel.set()
        except Exception as error:
            self.errors.append(str(error))

    def close(self, timeout: float = 10.0) -> None:
        self._closed.set()
        if self.is_alive():
            self.join(timeout)


class ProgressRecorder:
    """Rate-limited persistence of absolute processed counts. A report within ``interval_seconds`` of the last write
    leaves its counts pending: the next report after the interval writes them, or ``flush_pending``, which a live
    run's hooks call at each look at its arrivals (so its row catches up after a burst)."""

    def __init__(
        self, engine: Engine, job_id: int, interval_seconds: float, *, clock: Callable[[], float] = time.monotonic
    ):
        self._engine = engine
        self._job_id = job_id
        self._interval = interval_seconds
        self._clock = clock
        self._last_flush = clock()
        self.succeeded = 0
        self.failed = 0
        self.flushes = 0
        self.errors: list[str] = []
        self._pending = False

    def report(self, *, succeeded: int, failed: int) -> None:
        if succeeded < self.succeeded or failed < self.failed:
            raise state.ExecutionStateError(
                f"progress went backwards: {succeeded}/{failed} after {self.succeeded}/{self.failed}"
            )
        self.succeeded = int(succeeded)
        self.failed = int(failed)
        self._pending = True
        if self._clock() - self._last_flush >= self._interval:
            self.flush()

    def flush_pending(self) -> None:
        """Write the counts a report left pending, once the interval has passed since the last write; nothing when
        none is pending."""
        if self._pending and self._clock() - self._last_flush >= self._interval:
            self.flush()

    def flush(self) -> None:
        """Write the counts now. A write that fails (the database write-locked past its busy timeout) leaves them
        pending, so the next ``report`` or ``flush_pending`` after the interval tries again."""
        self._last_flush = self._clock()
        try:
            with Session(self._engine) as session, session.begin():
                job = session.get(Job, self._job_id)
                if job is not None and job.phase in (RunPhase.RUNNING, RunPhase.FINALIZING):
                    state.record_progress(job, succeeded=self.succeeded, failed=self.failed)
                    self.flushes += 1
        except Exception as error:
            self.errors.append(str(error))
            logger.warning("Run %s progress write failed: %s", self._job_id, error)
            return
        self._pending = False


HELPER_GRACE_SECONDS = 2.0
"""How long the sweep lets multiprocessing's helpers (the resource tracker that lauelab's spawn-context pools
start) end by themselves after their run process is gone: they exit once their parent has (about 10 ms)."""


def _group_members(pgid: int) -> list[psutil.Process]:
    """The live processes of process group ``pgid`` (a zombie is dead: it only waits to be reaped)."""

    members = []
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) == pgid and process.status() != psutil.STATUS_ZOMBIE:
                members.append(process)
        except (OSError, psutil.Error):  # gone meanwhile, or not ours to look at
            continue
    return members


def sweep_process_group(pgid: int, *, helper_grace_seconds: float = HELPER_GRACE_SECONDS) -> bool:
    """Kill everything left in a finished job process's group; True when real work was left behind.

    A run that used a spawn-context process pool (lauelab's) leaves multiprocessing's resource tracker, which
    the executor does not terminate (``processes.is_helper_process``): it ends by itself just after the run
    process, and is given ``helper_grace_seconds`` to, so that its own clean-up runs. Anything else (a compute
    process that outlived its run), or a helper still there after the grace, is killed and reported."""

    if not pgid or pgid == os.getpid() or pgid == os.getpgid(0):
        return False
    try:
        os.killpg(pgid, 0)  # does the group still exist? (signal 0 only checks)
    except ProcessLookupError:
        return False
    except PermissionError:
        logger.warning("Could not sweep process group %s", pgid)
        return False
    members = _group_members(pgid)
    if members and all(is_helper_process(member) for member in members):
        _, members = psutil.wait_procs(members, timeout=helper_grace_seconds)
    try:
        os.killpg(pgid, signal.SIGKILL)  # whatever is left, including a process that appeared meanwhile
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.warning("Could not sweep process group %s", pgid)
        return False
    return bool(members)
