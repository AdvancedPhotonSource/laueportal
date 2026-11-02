"""The worker's dispatcher: the job table is the queue.

A committed Queued local job (phase ``queued``) is its queue entry, and a ``conversion`` row ``queued`` is a
conversion's (no job row). The worker's dispatcher thread (``Dispatcher``) runs a cycle every
``execution.dispatch_seconds`` and at once when one of its job processes exits (``wake``):

1. **Reap** its job processes that exited, and kill what is left in their process groups
   (``lifecycle.sweep_process_group``; logged only when something was killed). A row a dead job process left
   Running is the reconciler's to finalize, never the dispatcher's (but for a batch item it killed for a live run,
   below, and a live run its Stop now killed).
   **A live run's Stop now** (``_stop_now``): a Running live run whose stop was requested and
   whose messages hold ``state.STOP_NOW_REQUESTED`` (``POST /jobs/{id}/stop-now``, the Live page's Stop now while its
   session is Stopping: the user says its drain is stuck) has its job process's group killed (this dispatcher's
   child, or the recorded orphan while it is the recorded process alive on this host), and once it is gone the run
   ends Failed, ``interrupted`` (``end_stopped_now``, ``STOPPED_NOW``: what it published stays), only while still
   Running with that process; until then it is this dispatcher's (``running_items``), so the reconciler never
   finalizes it while it lives, and a worker that dies meanwhile leaves it to the reconciler's stale rule, which ends
   it the same way. Nothing else stops a live run: it has no time limit.
2. **Count the slots**: one **batch slot** (local batch runs and conversions, one at a time) and two **live slots**
   (``jobs.order.LIVE_SLOTS``; live runs, ``jobs.live``); neither kind takes the other's. A slot is busy while one of
   its own job processes of that kind is not reaped (one that exited is reaped, and its group swept, by the next cycle's
   first step, so a slot's job processes never overlap), or while a Running local job or a running conversion of that
   kind records a job process that is alive on this host (``recorded_job_processes``, ``recorded_process_alive``: its
   pid with the recorded start since boot), e.g. one an earlier worker started before it was killed: the dispatcher
   starts nothing in its slot and never finalizes it.
3. **With no live run open** (Queued or Running, ``jobs.live.open_live_runs``), take the next batch item
   (``next_item``, on ``jobs.order``'s queries, which the status page shares) while the batch slot is free: the
   queued conversions, oldest request first; then the Queued local batch jobs moved to the front
   (``moved_to_front_at``), the newest move first; then the rest by ``job_id``. Remote runs are never the
   dispatcher's (the remote poller's).
4. **With a live run open**, no batch item is claimed (they wait, in order; the claim checks it in its own transaction,
   so a live run queued while a batch item is being claimed holds it or finds it claimed). The batch slot's item is
   stopped (``_hold_batch``): a run gets the stop flag with the reason "stopped for a live run" (``STOPPED_FOR_LIVE``,
   ``state.request_cancellation``) and drains, ending Cancelled (re-run by Clone); a conversion, which
   cannot stop part way, is killed at once. A run still alive ``live.batch_stop_seconds`` after its stop flag is killed
   too. The dispatcher kills a job process's group only while it is the recorded or its own one, waits for it to be gone
   and only then ends its item (``end_stopped_for_live``: the run Cancelled, the conversion ``failed``, each with that
   message, only while still as claimed); until then the item counts as this worker's running one (``running_items``),
   so the reconciler leaves it alone, and a write that fails is tried again at the next cycle. Once the batch slot's
   process has exited and been reaped (a cycle that killed it reaps it at once), the Queued live runs are claimed into
   the free live slots, by ``job_id``, each with its **cores** (``live_cores``: ``floor(cpu_count × live.cpu_share)``,
   at least 1, shared; with another live run open the wire run takes ``round(share × live.wire_share)`` and the LaueGo
   run the rest, each at least 1; a run alone takes the whole share), passed to its job process (``--cores``; the live
   compute sizes its pools from ``RunRequest.cores``). The batch path's cores are unchanged (``execution.workers``,
   ``reconstruction_workers``).
5. **Claim** an item atomically (``claim_run``, moved from the executor's start: one ``begin_write`` transaction,
   Queued → Running only while it is still Queued, or Cancelled when its stop flag is set; ``claim_conversion``:
   ``queued`` → ``running``), start its **job process** (``start_job_process``: ``python -m laueportal.jobs.process
   run|convert <id> [--cores <n>]``, a new session, ``close_fds``; no fork) and record it on the row
   (``process_host``, ``process_pid``, ``process_since_boot``). Only then is the job process told to go (a line on
   its stdin): it checks that the row names its pid before it runs anything (``jobs.process``). A start that fails
   ends the run Failed (the conversion ``failed``) with the reason.

A database error in a cycle (``OperationalError``: the database write-locked past its busy timeout, a disk error)
ends that cycle, logged; the next cycle tries again. A claim that failed changed nothing, so its item
is still queued; a job process whose record failed waits at its go line, holding the batch slot, and the next cycle
records it and lets it go (``Child.unrecorded``): nothing is claimed twice or lost. The worker's reconciler never
finalizes a row whose job process this dispatcher started and still runs (``running_items``). At the service stop,
a job process still unrecorded gets no go line (``let_go_unrecorded``: it reads the end of its stdin and exits
unrun), and the worker's last write ends its item (``stop_unstarted``): the run Failed/interrupted "Stopped by
service shutdown before the run started", the conversion ``failed``.

A job process arms its own timeout (``execution.job_timeout_seconds``, a conversion's
``indexing.conversion.CONVERSION_TIMEOUT_SECONDS``) once told to go; the dispatcher kills the group of one still
alive ``execution.shutdown_grace_seconds`` after it, counted from the go line too (``Child.started``). A live run (a
live slot's) has no time limit, so no kill either. The service stop (``stop``,
``let_go_unrecorded``, ``signal_job_processes``, ``kill_job_processes``) is driven by ``jobs.worker``.
"""

from __future__ import annotations

import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, exists, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal.core.db import DatabaseErrors, begin_write
from laueportal.core.timeutil import utcnow
from laueportal.indexing.models import Conversion
from laueportal.jobs import state
from laueportal.jobs.backends import is_remote
from laueportal.jobs.lifecycle import SHUTDOWN_SIGNAL, sweep_process_group
from laueportal.jobs.live import has_live_marker, live_marker, open_live_runs
from laueportal.jobs.liveness import ProcessRecord, process_record, recorded_process_alive
from laueportal.jobs.liveness import since_boot as since_boot
from laueportal.jobs.models import Job
from laueportal.jobs.order import (
    CONVERSION_QUEUED,
    CONVERSION_RUNNING,
    CONVERT,
    LIVE_SLOTS,
    RUN,
    Kind,
    local_jobs,
    queued_batch_runs,
    queued_conversions,
    queued_live_runs,
)
from laueportal.jobs.order import queued_in_order as queued_in_order
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.reconstruction.models import ReconstructionRun

logger = logging.getLogger(__name__)

CONVERSION_FINISHED = "finished"
CONVERSION_FAILED = "failed"

JOB_COMMAND: list[str] = [sys.executable, "-m", "laueportal.jobs.process"]
"""The job process's command, before its kind and ID. The live worker tests replace it (in the worker process) with
one that registers their test computes first (``tests.worker_support``)."""

GO = b"go\n"
"""What the dispatcher writes on a job process's stdin once the row records it."""

STOPPED_UNSTARTED = "Stopped by service shutdown before the run started"
STOPPED_UNSTARTED_CONVERSION = "Stopped by service shutdown before the conversion started; convert the results again"
"""The messages of a claimed item whose job process the service stop let go unrecorded (``stop_unstarted``)."""

STOPPED_FOR_LIVE = "stopped for a live run"
"""The reason of the stop flag a running local batch run gets while a live run is open:
"Cancellation requested: stopped for a live run"; it drains and ends Cancelled (re-run by Clone)."""
STOPPED_FOR_LIVE_KILLED = "Stopped for a live run: its job process was killed after {seconds:g} s"
"""The message of a batch run whose job process was killed ``live.batch_stop_seconds`` after its stop flag
(Cancelled)."""
STOPPED_FOR_LIVE_CONVERSION = "Stopped for a live run; convert the results again"
"""The message of a running conversion stopped (killed) for a live run (``failed``)."""
STOPPED_NOW = "Stopped now: its job process was killed; what it published is kept"
"""The message of a live run ended by its Stop now (``Dispatcher._stop_now``)."""
KILL_WAIT_SECONDS = 5.0
"""How long a cycle waits for a job process it killed for a live run to be gone before it ends its item (the next
cycle looks again)."""


# ---------------------------------------------------------------------------
# Job processes: what a row records, and whether it is alive (``jobs.liveness``)


def _alive_recorded(row: Job | Conversion) -> bool:
    return recorded_process_alive(row.process_host, row.process_pid, row.process_since_boot) is True


@dataclass(frozen=True)
class RecordedProcess:
    """A job process a row records that is alive on this host, with its item and its slot's kind."""

    kind: Kind
    key: int
    live: bool
    """A live run's (a live slot); else the batch slot's (a batch run or a conversion)."""
    record: ProcessRecord

    @property
    def pid(self) -> int:
        return self.record.pid


def _record_of(row: Job | Conversion) -> ProcessRecord:
    return ProcessRecord(row.process_host or "", row.process_pid or 0, row.process_since_boot or 0.0)


def recorded_job_processes(session: Session) -> list[RecordedProcess]:
    """The live job processes the rows record on this host, each with its slot's kind: a Running local job's (a live
    run's by its queue identity, ``jobs.live.live_marker``) and a running conversion's."""
    jobs = session.execute(
        select(Job, live_marker()).where(
            Job.status == int(JobStatus.RUNNING), Job.process_pid.is_not(None), local_jobs()
        )
    ).all()
    conversions = session.scalars(
        select(Conversion).where(Conversion.status == CONVERSION_RUNNING, Conversion.process_pid.is_not(None))
    ).all()
    recorded = [
        RecordedProcess(RUN, job.job_id, bool(live), _record_of(job)) for job, live in jobs if _alive_recorded(job)
    ]
    recorded += [
        RecordedProcess(CONVERT, row.indexing_id, False, _record_of(row)) for row in conversions if _alive_recorded(row)
    ]
    return recorded


def live_recorded_processes(session: Session) -> list[ProcessRecord]:
    """The live job processes the rows record on this host: a Running local job's and a running conversion's (the
    dispatcher's slots, and the seed's check before it empties a database)."""
    return [process.record for process in recorded_job_processes(session)]


# ---------------------------------------------------------------------------
# The order, the claim and the record


def next_item(session: Session) -> tuple[Kind, int] | None:
    """The batch slot's next item (``jobs.order``), or None: a queued conversion (``("convert", indexing ID)``), else
    a Queued local batch job (``("run", job ID)``)."""

    conversion = session.scalar(queued_conversions().limit(1))
    if conversion is not None:
        return CONVERT, conversion
    job_id = session.scalar(queued_batch_runs().limit(1))
    return None if job_id is None else (RUN, job_id)


def next_live(session: Session) -> int | None:
    """The next Queued live run (its job ID), or None."""
    return session.scalar(queued_live_runs().limit(1))


def _live_open(session: Session) -> bool:
    return session.execute(open_live_runs().limit(1)).first() is not None


def claim_run(engine: Engine, job_id: int) -> bool:
    """Claim a Queued local run atomically: one ``begin_write`` transaction that marks it Running only while it is
    still Queued (``state.mark_running``), or ends it Cancelled when its stop flag is set. True when it was claimed;
    False when it was not Queued any more (another claim, a stop, a reconciler), was cancelled here, or is a batch run
    held while a live run is Queued or Running (read in the same transaction, so a live run queued
    meanwhile either holds it or finds it claimed)."""

    with Session(engine) as session, session.begin():
        begin_write(session)
        job = session.get(Job, job_id)
        if job is None or job.status != JobStatus.QUEUED or job.phase != RunPhase.QUEUED or is_remote(job.backend):
            return False
        if job.cancel_requested_at is not None:
            state.finalize(job, status=JobStatus.CANCELLED, message="Cancelled before the run started")
            logger.info("Job %s was cancelled before it started", job_id)
            return False
        if not has_live_marker(job) and _live_open(session):
            logger.info("Job %s waits: a live run is open", job_id)
            return False
        state.mark_running(job)
        job.process_host = job.process_pid = job.process_since_boot = None
    return True


def claim_conversion(engine: Engine, indexing_id: int) -> bool:
    """Claim a queued conversion atomically (``queued`` → ``running``); False when it is not queued any more, or while
    a live run is Queued or Running (held; read in the same transaction)."""

    with Session(engine) as session, session.begin():
        begin_write(session)
        row = session.get(Conversion, indexing_id)
        if row is None or row.status != CONVERSION_QUEUED:
            return False
        if _live_open(session):
            logger.info("The conversion of I%s waits: a live run is open", indexing_id)
            return False
        row.status = CONVERSION_RUNNING
        row.started_at = utcnow()
        row.ended_at = None
        row.message = None
        row.process_host = row.process_pid = row.process_since_boot = None
    return True


def _row(session: Session, kind: Kind, key: int) -> Job | Conversion | None:
    return session.get(Job, key) if kind == RUN else session.get(Conversion, key)


def record_process(engine: Engine, kind: Kind, key: int, record: ProcessRecord) -> bool:
    """Record the job process on its claimed row; False when the row is not the claimed one any more (ended)."""

    with Session(engine) as session, session.begin():
        begin_write(session)
        row = _row(session, kind, key)
        if row is None:
            return False
        if isinstance(row, Job):
            if row.status != JobStatus.RUNNING or row.phase != RunPhase.RUNNING:
                return False
        elif row.status != CONVERSION_RUNNING:
            return False
        row.process_host, row.process_pid, row.process_since_boot = record.host, record.pid, record.since_boot
    return True


def fail_start(engine: Engine, kind: Kind, key: int, reason: str) -> None:
    """End a claimed item whose job process could not start: the run Failed, the conversion ``failed``."""

    message = f"The job process could not start: {reason}"
    with Session(engine) as session, session.begin():
        begin_write(session)
        row = _row(session, kind, key)
        if isinstance(row, Job):
            if not state.is_terminal(row):
                state.record_failure(row, message)
        elif row is not None and row.status == CONVERSION_RUNNING:
            row.status = CONVERSION_FAILED
            row.message = message
            row.ended_at = utcnow()
    logger.error("%s: %s", f"Job {key}" if kind == RUN else f"The conversion of I{key}", message)


def stop_unstarted(session: Session, kind: Kind, key: int) -> bool:
    """End a claimed item whose job process the service stop let go unrecorded (``Dispatcher.let_go_unrecorded``:
    it ran nothing), in the caller's write transaction (the worker's last, ``jobs.worker.stop_row``): the run
    Failed/interrupted (``STOPPED_UNSTARTED``), the conversion ``failed`` (``STOPPED_UNSTARTED_CONVERSION``), each
    only while still claimed with no process recorded. True when it ended it."""

    row = _row(session, kind, key)
    if row is None or row.process_pid is not None:
        return False
    if isinstance(row, Job):
        if row.status != JobStatus.RUNNING or row.phase != RunPhase.RUNNING:
            return False
        state.finalize(row, status=JobStatus.FAILED, message=STOPPED_UNSTARTED, phase=RunPhase.INTERRUPTED)
        logger.warning("Job %s: %s", key, STOPPED_UNSTARTED)
        return True
    if row.status != CONVERSION_RUNNING:
        return False
    row.status, row.message, row.ended_at = CONVERSION_FAILED, STOPPED_UNSTARTED_CONVERSION, utcnow()
    logger.warning("The conversion of I%s: %s", key, STOPPED_UNSTARTED_CONVERSION)
    return True


def ask_to_stop_for_live(engine: Engine, job_id: int) -> bool:
    """Give a running local batch run the stop flag for a live run (``state.request_cancellation`` with the reason
    ``STOPPED_FOR_LIVE``; only the first request is recorded, so a user's Stop keeps its own), in one ``begin_write``
    transaction; False when it is not Running any more."""
    with Session(engine) as session, session.begin():
        begin_write(session)
        job = session.get(Job, job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return False
        return state.request_cancellation(job, reason=STOPPED_FOR_LIVE)


def stop_now_requests(session: Session) -> list[tuple[int, ProcessRecord | None]]:
    """The Running live runs whose Stop now is recorded (``state.STOP_NOW_REQUESTED``), each with
    the job process its row records (None: none recorded yet)."""
    rows = session.execute(
        select(Job.job_id, Job.messages, Job.process_host, Job.process_pid, Job.process_since_boot).where(
            Job.status == int(JobStatus.RUNNING), Job.cancel_requested_at.is_not(None), live_marker()
        )
    ).all()
    return [
        (key, None if pid is None else ProcessRecord(host or "", pid, since or 0.0))
        for key, messages, host, pid, since in rows
        if state.stop_now_requested(messages)
    ]


def end_stopped_now(engine: Engine, key: int, pid: int) -> bool:
    """End a live run whose job process ``pid`` its Stop now killed, once that process is gone (the caller's check):
    Failed, ``interrupted`` (``STOPPED_NOW``; what it published stays), only while it is still Running with that
    process (or none) recorded. False when it had ended already (its drain finished before the kill)."""
    with Session(engine) as session, session.begin():
        begin_write(session)
        job = session.get(Job, key)
        if job is None or job.process_pid not in (None, pid):
            return False
        if state.is_terminal(job) or job.status != JobStatus.RUNNING:
            return False
        state.finalize(job, status=JobStatus.FAILED, message=STOPPED_NOW, phase=RunPhase.INTERRUPTED)
    logger.warning("Job %s: %s", key, STOPPED_NOW)
    return True


def end_stopped_for_live(engine: Engine, kind: Kind, key: int, pid: int, message: str) -> bool:
    """End a batch item whose job process ``pid`` the dispatcher killed for a live run, once that process is gone
    (the caller's check): the run Cancelled, the conversion ``failed``, with ``message``, each only while it is still
    claimed with that process (or none yet) recorded. True when it ended it; False when it had ended already (its
    run drained, or its conversion finished, before the kill)."""
    with Session(engine) as session, session.begin():
        begin_write(session)
        row = _row(session, kind, key)
        if row is None or row.process_pid not in (None, pid):
            return False
        if isinstance(row, Job):
            if state.is_terminal(row) or row.status != JobStatus.RUNNING:
                return False
            state.finalize(row, status=JobStatus.CANCELLED, message=message)
        else:
            if row.status != CONVERSION_RUNNING:
                return False
            row.status, row.message, row.ended_at = CONVERSION_FAILED, message, utcnow()
    logger.warning("%s: %s", f"Job {key}" if kind == RUN else f"The conversion of I{key}", message)
    return True


def start_job_process(
    kind: Kind, key: int, cores: int | None = None, *, output: int | None = None
) -> subprocess.Popen[bytes]:
    """The job process for one claimed item: a fresh interpreter in a session of its own (so it outlives a worker
    crash, and its group is its pid), no inherited descriptors but its stdin (the go line) and its output: ``output``
    (a descriptor, ``JobLog``'s file) for both its stdout and stderr, else the worker's; ``cores``: a live run's
    (``--cores``), none for a batch item."""
    return subprocess.Popen(
        [*JOB_COMMAND, kind, str(key), *(() if cores is None else ("--cores", str(cores)))],
        stdin=subprocess.PIPE,
        stdout=output,
        stderr=output,
        start_new_session=True,
        close_fds=True,
    )


JOB_LOG_MAX_BYTES = 20 * 1024 * 1024
JOB_LOG_BACKUPS = 10
"""The job log's rotation (``JobLog``): the worker's own log's in the production template (20 MB, 10 kept)."""


class JobLog:
    """The job processes' own log (``execution.job_log``): the file each job process writes its
    stdout and stderr to, appended, so its lines outlive its worker. supervisor closes a dead program's pipe, so a job
    process left writing to the worker's output after its worker was killed (an OOM kill, ``kill -9``) would lose
    every later line (a live run's for hours). ``start`` is the dispatcher's: the file is opened for each job process
    (0600), rotated first once it reached ``JOB_LOG_MAX_BYTES`` (``<name>.1`` the newest of ``JOB_LOG_BACKUPS``, as
    supervisor's), and a job process still writing into a rotated file keeps it until it ends. A file that cannot be
    opened leaves that job process on the worker's output, logged once per reason; no path: the worker's output."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._reported: str | None = None

    def start(self, kind: Kind, key: int, cores: int | None = None) -> subprocess.Popen[bytes]:
        output = self._open()
        try:
            return start_job_process(kind, key, cores, output=output)
        finally:
            if output is not None:
                os.close(output)  # the job process has its own copy

    def _open(self) -> int | None:
        path = self.path
        if path is None:
            return None
        try:
            try:
                if path.stat().st_size >= JOB_LOG_MAX_BYTES:
                    for number in range(JOB_LOG_BACKUPS - 1, 0, -1):
                        older = path.with_name(f"{path.name}.{number}")
                        if older.exists():
                            older.replace(path.with_name(f"{path.name}.{number + 1}"))
                    path.replace(path.with_name(f"{path.name}.1"))
            except FileNotFoundError:
                pass
            return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError as error:
            reason = f"{type(error).__name__}: {error}"
            if reason != self._reported:
                self._reported = reason
                logger.warning(
                    "The job log %s cannot be written (%s): job processes write to the worker's", path, reason
                )
            return None


# ---------------------------------------------------------------------------
# The live runs' cores


@dataclass(frozen=True)
class LivePolicy:
    """The dispatcher's live rules, from the ``[live]`` settings (``jobs.worker.dispatch_policy``)."""

    batch_stop_seconds: float = 60.0
    cpu_share: float = 0.75
    wire_share: float = 0.67
    cpu_count: int = field(default_factory=lambda: os.cpu_count() or 1)


def live_cores(policy: LivePolicy, *, wire: bool, paired: bool) -> int:
    """A live run's cores: the live share ``floor(cpu_count × cpu_share)`` (at least 1) whole for a run alone; with
    another live run open (``paired``), ``round(share × wire_share)`` for the wire run and the rest for the LaueGo
    run, each at least 1."""
    share = max(1, math.floor(policy.cpu_count * policy.cpu_share))
    if not paired:
        return share
    wire_part = max(1, round(share * policy.wire_share))
    return wire_part if wire else max(1, share - wire_part)


# ---------------------------------------------------------------------------
# The dispatcher


@dataclass
class Child:
    """A job process this dispatcher started."""

    kind: Kind
    key: int
    process: subprocess.Popen[bytes]
    started: float
    """``time.monotonic()`` at its go line (at its launch until then): its own timeout counts from the go line, so
    the kill's deadline does too (a record that waited on a write lock never shortens its run)."""
    timeout: float
    """Its own timeout (seconds; ``math.inf`` for a live run); the dispatcher kills its group ``grace`` after it."""
    unrecorded: bool = True
    """Not yet recorded on its row (so not yet told to go)."""
    delayed: bool = False
    """Its record failed on a database error at least once (logged once); each cycle tries again."""
    live: bool = False
    """A live run's (a live slot); else the batch slot's."""
    cores: int | None = None
    """A live run's cores (``live_cores``); None for a batch item."""

    @property
    def label(self) -> str:
        return f"job {self.key}" if self.kind == RUN else f"the conversion of I{self.key}"


@dataclass(frozen=True)
class DispatchPolicy:
    dispatch_seconds: float = 1.0
    job_timeout_seconds: float = 24 * 3600
    conversion_timeout_seconds: float = 3600
    shutdown_grace_seconds: float = 600
    heartbeat_seconds: float = 15.0
    live: LivePolicy = field(default_factory=LivePolicy)


@dataclass
class _Occupant:
    """The batch slot's item while a live run is open: this dispatcher's child, or a recorded orphan."""

    kind: Kind
    key: int
    pid: int
    child: Child | None = None
    record: ProcessRecord | None = None

    def gone(self) -> bool:
        if self.child is not None:
            return self.child.process.poll() is not None
        assert self.record is not None
        return recorded_process_alive(self.record.host, self.record.pid, self.record.since_boot) is not True

    @property
    def label(self) -> str:
        return f"job {self.key}" if self.kind == RUN else f"the conversion of I{self.key}"


@dataclass
class _Held:
    """A batch item the dispatcher stopped for a live run, until its job process is gone and its end recorded."""

    occupant: _Occupant
    asked: float
    """``time.monotonic()`` when it was stopped: ``live.batch_stop_seconds`` count from it."""
    killed: bool = False
    """Its job process's group was killed: its end is the dispatcher's to record (``end_stopped_for_live``)."""


class Dispatcher:
    """The dispatcher's state and cycle (the module's steps). ``run_forever`` is the worker's thread body; the
    worker calls ``wake`` when a child exits (SIGCHLD) and drives the service stop."""

    def __init__(
        self,
        engine: Engine,
        policy: DispatchPolicy,
        *,
        mark: Callable[[datetime], None] | None = None,
        start: Callable[..., subprocess.Popen[bytes]] = start_job_process,
    ) -> None:
        self.engine = engine
        self.policy = policy
        self._mark = mark
        self._start = start
        """``start_job_process`` or a test's stand-in: called ``(kind, key)`` for a batch item, ``(kind, key,
        cores=...)`` for a live run."""
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self.claiming = True
        self.children: dict[int, Child] = {}
        """This dispatcher's running job processes, by pid."""
        self.held: dict[tuple[Kind, int], _Held] = {}
        """The batch items stopped for a live run whose job process is not gone yet, or whose end is not recorded."""
        self.stopping_now: dict[int, _Held] = {}
        """The live runs whose Stop now killed their job process, until it is gone and their end recorded."""
        self._unknown_host: set[int] = set()
        """The Stop now requests whose job process is recorded on another host (logged once)."""
        self._last_mark = float("-inf")
        self._database = DatabaseErrors(logger, "dispatcher")

    # -- the thread

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        """End ``run_forever`` (and claim nothing more)."""
        self.claiming = False
        self._stopped.set()
        self._wake.set()

    def run_forever(self) -> None:
        while not self._stopped.is_set():
            self._wake.clear()  # before the cycle: a wake from then on ends the wait after it, so none is lost
            self.cycle()
            self._wake.wait(self.policy.dispatch_seconds)

    def cycle(self) -> Kind | None:
        """One cycle; returns the kind of what it started, if anything. A database error ends the cycle, logged
        (once until a cycle succeeds again); the next cycle tries again (the module's docstring)."""
        started: Kind | None = None
        with self._lock:
            try:
                self.reap()
                self._kill_overdue()
                self._record_unrecorded()
                self._end_held()
                self._stop_now()
                if self.claiming:
                    started = self._dispatch()
                self._beat()
            except OperationalError as error:
                self._database.failed(error)
                return started
        self._database.succeeded()
        return started

    def _beat(self) -> None:
        now = time.monotonic()
        if self._mark is not None and now - self._last_mark >= self.policy.heartbeat_seconds:
            self._last_mark = now
            self._mark(utcnow())

    # -- step 1: reap

    def reap(self) -> list[Child]:
        """Collect the children that exited; kill what is left in their groups."""
        ended = []
        for pid, child in list(self.children.items()):
            code = child.process.poll()
            if code is None:
                continue
            del self.children[pid]
            ended.append(child)
            if code != 0:
                logger.warning("The job process %s of %s exited with status %s", pid, child.label, code)
            if sweep_process_group(pid):
                logger.warning("Killed processes left behind by the job process %s of %s", pid, child.label)
        return ended

    def _kill_overdue(self) -> None:
        now = time.monotonic()
        for pid, child in self.children.items():
            if now - child.started > child.timeout + self.policy.shutdown_grace_seconds:
                logger.error(
                    "The job process %s of %s is alive %g s after its timeout; killing its group",
                    pid,
                    child.label,
                    self.policy.shutdown_grace_seconds,
                )
                _kill_group(pid)

    # -- step 2: the slots

    def orphans(self) -> list[RecordedProcess]:
        """The live job processes the rows record on this host that are not this dispatcher's children: a Running
        local job's or a running conversion's (an earlier worker's), each with its slot's kind."""
        with Session(self.engine) as session:
            return [process for process in recorded_job_processes(session) if process.pid not in self.children]

    def _batch_occupants(self, orphans: list[RecordedProcess]) -> list[_Occupant]:
        """The batch slot's job processes: this dispatcher's batch children until they are reaped (one that exited is
        reaped, and its group swept, by the next cycle's first step, before the slot takes anything: a slot's job
        processes never overlap), and the rows' batch orphans."""
        own = [
            _Occupant(child.kind, child.key, pid, child=child) for pid, child in self.children.items() if not child.live
        ]
        return own + [
            _Occupant(orphan.kind, orphan.key, orphan.pid, record=orphan.record)
            for orphan in orphans
            if not orphan.live
        ]

    def _live_in_use(self, orphans: list[RecordedProcess]) -> int:
        """The live slots in use: this dispatcher's live children until they are reaped, and the rows' live orphans."""
        own = sum(1 for child in self.children.values() if child.live)
        return own + sum(1 for orphan in orphans if orphan.live)

    def batch_slot_free(self) -> bool:
        return not self._batch_occupants(self.orphans())

    def live_slots_free(self) -> int:
        return max(0, LIVE_SLOTS - self._live_in_use(self.orphans()))

    def idle(self) -> bool:
        """Nothing queued and no job process runs (a burst worker's end)."""
        with self._lock:
            self.reap()
            if self.children or self.held or self.orphans():
                return False
            with Session(self.engine) as session:
                return next_item(session) is None and next_live(session) is None

    # -- steps 3 to 5

    def _dispatch(self) -> Kind | None:
        """The slots' rules (the module's steps 3 and 4); the kind of the first item started, if any."""
        orphans = self.orphans()
        with Session(self.engine) as session:
            live_open = _live_open(session)
        batch = self._batch_occupants(orphans)
        if not live_open:
            return None if batch else self._dispatch_one()
        if batch:
            self._hold_batch(batch)
            self.reap()  # what its kills ended is reaped (its group swept) now, as the next cycle's first step would
            orphans = self.orphans()
            if self._batch_occupants(orphans):
                return None  # the live runs start once the batch slot's process has exited and been reaped
        return self._dispatch_live(LIVE_SLOTS - self._live_in_use(orphans))

    def _dispatch_one(self) -> Kind | None:
        with Session(self.engine) as session:
            item = next_item(session)
        while item is not None:
            kind, key = item
            claimed = claim_conversion(self.engine, key) if kind == CONVERT else claim_run(self.engine, key)
            if claimed:
                self._launch(kind, key)
                return kind
            with Session(self.engine) as session:  # lost it (a stop, another claim) or held: the next one
                following = next_item(session)
            if following == item:
                return None
            item = following
        return None

    def _dispatch_live(self, free: int) -> Kind | None:
        """Claim the Queued live runs into the ``free`` live slots, by ``job_id``, each with its cores."""
        started: Kind | None = None
        tried: set[int] = set()
        while free > 0:
            with Session(self.engine) as session:
                key = next_live(session)
                if key is None or key in tried:
                    return started
                wire = bool(session.scalar(select(exists().where(ReconstructionRun.job_id == key))))
                paired = session.scalar(open_live_runs().where(Job.job_id != key).limit(1)) is not None
            tried.add(key)
            if not claim_run(self.engine, key):  # lost it (a Stop while Queued): the next one
                continue
            cores = live_cores(self.policy.live, wire=wire, paired=paired)
            logger.info(
                "Live run job %s (%s): %d core(s) of %d",
                key,
                "wire" if wire else "LaueGo",
                cores,
                self.policy.live.cpu_count,
            )
            self._launch(RUN, key, live=True, cores=cores)
            started = started or RUN
            free -= 1
        return started

    # -- step 4: the batch slot's item stopped for a live run

    def _hold_batch(self, batch: list[_Occupant]) -> None:
        """Stop the batch slot's item for an open live run: a run's stop flag ("stopped for a live run"), its job
        process killed ``live.batch_stop_seconds`` later; a conversion killed at once."""
        now = time.monotonic()
        for occupant in batch:
            item = (occupant.kind, occupant.key)
            held = self.held.get(item)
            if held is None and occupant.gone():
                continue  # it ended by itself and is reaped below
            if held is None and occupant.kind == CONVERT:
                logger.warning("The conversion of I%s is stopped for a live run", occupant.key)
                self._kill_held(self.held.setdefault(item, _Held(occupant, asked=now)))
            elif held is None:
                ask_to_stop_for_live(self.engine, occupant.key)  # a database error: asked again at the next cycle
                self.held[item] = _Held(occupant, asked=now)
                logger.warning(
                    "Job %s is stopped for a live run: it drains; its job process is killed after %g s",
                    occupant.key,
                    self.policy.live.batch_stop_seconds,
                )
            elif not held.killed and now - held.asked > self.policy.live.batch_stop_seconds:
                logger.error(
                    "Job %s, stopped for a live run, is still running %g s later; killing its job process's group",
                    occupant.key,
                    self.policy.live.batch_stop_seconds,
                )
                self._kill_held(held)

    def _kill_held(self, held: _Held) -> None:
        """Kill a held item's job process's group, wait for it to be gone (``KILL_WAIT_SECONDS`` at most, in real
        time), then end its item (``_end_held``; a process not gone yet is killed and looked at again by the next
        cycle)."""
        occupant = held.occupant
        if not occupant.gone():  # an orphan's pid is killed only while it is the recorded process
            held.killed = True
            if occupant.child is not None:
                occupant.child.unrecorded = False  # never recorded or told to go now: it is killed
            _kill_and_wait(occupant)
        self._end_held()

    def _end_held(self) -> None:
        """Forget the held items whose job process is gone: a drained run ended by itself; a killed item's end is
        recorded here (``end_stopped_for_live``), only once its process is gone, never while it lives (one still
        alive after its kill is killed again)."""
        for item, held in list(self.held.items()):
            occupant = held.occupant
            if held.killed and not occupant.gone():
                _kill_and_wait(occupant)
            if not occupant.gone():
                continue
            if held.killed:
                if occupant.kind == RUN:
                    message = STOPPED_FOR_LIVE_KILLED.format(seconds=self.policy.live.batch_stop_seconds)
                else:
                    message = STOPPED_FOR_LIVE_CONVERSION
                end_stopped_for_live(self.engine, occupant.kind, occupant.key, occupant.pid, message)
            del self.held[item]

    # -- step 1b: a live run's Stop now

    def _stop_now(self) -> None:
        """Kill the job process of each live run whose Stop now is recorded (``stop_now_requests``): this dispatcher's
        child, or the recorded orphan while it is the recorded process alive on this host; wait for it to be gone
        (``KILL_WAIT_SECONDS`` at most), then end the run (``end_stopped_now``: Failed, ``interrupted``), never while
        it lives (one still alive is killed again at the next cycle). Until its end is recorded it is this
        dispatcher's (``running_items``), so the reconciler leaves it alone. One whose recorded process is already
        gone is ended at once; one with no process known (none recorded and not this dispatcher's child, or another
        host's) is left as any Running row (the reconciler's stale rule)."""
        with Session(self.engine) as session:
            requests = stop_now_requests(session)
        for key in set(self.stopping_now) - {key for key, _ in requests}:
            del self.stopping_now[key]  # it ended meanwhile
        for key, record in requests:
            held = self.stopping_now.get(key)
            if held is None:
                occupant = self._live_occupant(key, record)
                if occupant is None:
                    continue
                held = self.stopping_now[key] = _Held(occupant, asked=time.monotonic(), killed=True)
                if occupant.child is not None:
                    occupant.child.unrecorded = False  # never recorded or told to go now: it is killed
                logger.warning("Job %s: Stop now: its job process %s is killed", key, occupant.pid)
            if not held.occupant.gone():
                _kill_and_wait(held.occupant)
            if held.occupant.gone():
                end_stopped_now(self.engine, key, held.occupant.pid)
                del self.stopping_now[key]

    def _live_occupant(self, key: int, record: ProcessRecord | None) -> _Occupant | None:
        """The job process of live run ``key`` a Stop now kills: this dispatcher's child, else the recorded one on
        this host (alive or gone); None when none is known here."""
        for child in self.children.values():
            if child.kind == RUN and child.key == key:
                return _Occupant(RUN, key, child.process.pid, child=child)
        if record is None:
            return None
        if recorded_process_alive(record.host, record.pid, record.since_boot) is None:
            if key not in self._unknown_host:
                self._unknown_host.add(key)
                logger.error("Job %s: Stop now: its job process is recorded on %s, not here", key, record.host)
            return None
        return _Occupant(RUN, key, record.pid, record=record)

    def _launch(self, kind: Kind, key: int, *, live: bool = False, cores: int | None = None) -> None:
        timeout = self.policy.job_timeout_seconds if kind == RUN else self.policy.conversion_timeout_seconds
        if live:
            timeout = math.inf  # a live run has no time limit
        try:
            process = self._start(kind, key) if cores is None else self._start(kind, key, cores=cores)
        except Exception as error:
            fail_start(self.engine, kind, key, f"{type(error).__name__}: {error}")
            return
        child = Child(kind, key, process, time.monotonic(), timeout, live=live, cores=cores)
        self.children[process.pid] = child
        self._record(child)

    def _record(self, child: Child) -> None:
        """Record ``child`` on its row, then tell it to go. A database error leaves it unrecorded, waiting at its go
        line (the next cycle tries again); any other error kills it and ends its item as a start that failed."""
        pid = child.process.pid
        try:
            recorded = record_process(self.engine, child.kind, child.key, process_record(pid))
        except OperationalError as error:
            if not child.delayed:
                logger.warning(
                    "Could not record the job process %s of %s (%s); it waits, and the next cycle records it",
                    pid,
                    child.label,
                    error.orig,
                )
            child.delayed = True
            return
        except Exception as error:
            child.unrecorded = False
            logger.exception("Could not record the job process %s of %s", pid, child.label)
            _kill_group(pid)
            reason = f"its process could not be recorded ({type(error).__name__}: {error})"
            fail_start(self.engine, child.kind, child.key, reason)
            return
        child.unrecorded = False
        if not recorded:  # ended meanwhile (a stop of a just-claimed run cannot; a reconciler could)
            logger.warning("%s ended before its job process %s was recorded; the process exits", child.label, pid)
        child.started = time.monotonic()
        _release(child.process)
        logger.info("Started the job process %s of %s", pid, child.label)

    def _record_unrecorded(self) -> None:
        for child in list(self.children.values()):
            if child.unrecorded:
                self._record(child)

    def let_go_unrecorded(self) -> list[tuple[Kind, int]]:
        """At the service stop (``jobs.worker``, after ``stop``; whether or not the dispatcher thread runs, e.g. in
        its restart wait): a job process never recorded gets no go line, and no later cycle records it; it reads the
        end of its stdin, finds its row does not name it and exits unrun (``jobs.process``). Returns their items,
        which the worker's last write ends (``stop_unstarted``)."""
        items: list[tuple[Kind, int]] = []
        with self._lock:
            for pid, child in self.children.items():
                if child.unrecorded:
                    child.unrecorded = False
                    logger.warning("The job process %s of %s was never recorded; it exits unrun", pid, child.label)
                    _close_stdin(child.process)
                    items.append((child.kind, child.key))
        return items

    def running_items(self) -> set[tuple[Kind, int]]:
        """The items whose job process this dispatcher started and that has not exited, recorded on their rows or
        not yet (the reconciler leaves them alone, as the slots count them busy), the batch items it stopped for a
        live run until their end is recorded (a killed one's is the dispatcher's: ``end_stopped_for_live``), and the
        live runs its Stop now killed until theirs is (``end_stopped_now``)."""
        with self._lock:
            running: set[tuple[Kind, int]] = {
                (child.kind, child.key) for child in self.children.values() if child.process.poll() is None
            }
            stopping: set[tuple[Kind, int]] = {(RUN, key) for key in self.stopping_now}
            return running | set(self.held) | stopping

    # -- the service stop (jobs.worker)

    def stop_claiming(self) -> None:
        self.claiming = False

    def job_processes(self) -> list[int]:
        """Every job process this dispatcher knows on this host: its children and the rows' live ones."""
        with self._lock:
            self.reap()
            return sorted({*self.children, *(record.pid for record in self.orphans())})

    def signal_job_processes(self, signum: int = SHUTDOWN_SIGNAL) -> list[int]:
        pids = self.job_processes()
        for pid in pids:
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass
        return pids

    def kill_job_processes(self, *, wait: float = 5.0) -> list[int]:
        """Kill the job processes' groups; wait up to ``wait`` seconds in all for this dispatcher's children."""
        pids = self.job_processes()
        for pid in pids:
            _kill_group(pid)
        deadline = time.monotonic() + wait
        with self._lock:
            for child in self.children.values():
                try:
                    child.process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            self.reap()
        return pids


def _release(process: subprocess.Popen[bytes]) -> None:
    """Tell the job process to go, and close its stdin (an exited one reads nothing)."""
    stdin = process.stdin
    if stdin is None:
        return
    try:
        stdin.write(GO)
        stdin.flush()
    except OSError:
        pass
    finally:
        _close_stdin(process)


def _close_stdin(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass


_real_clock = time.monotonic
_real_sleep = time.sleep
"""The kill's wait for a real process to be gone, in real time (a test's stand-in clock is ``dispatch.time``)."""


def _kill_and_wait(occupant: _Occupant) -> None:
    """Kill the group of a batch item's job process, then wait for it to be gone, ``KILL_WAIT_SECONDS`` at most."""
    _kill_group(occupant.pid)
    deadline = _real_clock() + KILL_WAIT_SECONDS
    while not occupant.gone() and _real_clock() < deadline:
        _real_sleep(0.02)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
