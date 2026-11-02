"""API schemas of the system domain. The names are part of the contract (Orval type names)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from laueportal.core.timeutil import UTCTimestamp
from laueportal.jobs.schemas import RunReference, RunStatus

ProbeStatus = Literal["ok", "error"]

WorkerState = Literal["running", "late", "not_running"]
"""The worker: ``running`` while its newest mark (or its start, before its first) is at most
``system.status.WORKER_LATE_SECONDS`` (45) old, ``late`` after that, ``not_running`` with no row, its stop
recorded (``stopped_at``), or its process gone on the API's own host (``jobs.liveness.worker_process_alive``: no
process with its pid; once its marks are late, also a pid another process took since)."""

ThreadState = Literal["ok", "late", "off"]
"""A worker thread: ``ok`` while its mark (or the worker's start, before its first) is at most its ``late_after_seconds``
old, ``late`` after that, ``off`` when it does not run: the poller without remote execution, and every thread of a
worker that is not running."""


class _ApiModel(BaseModel):
    """API schemas: attribute docstrings become the OpenAPI descriptions (and Orval's comments)."""

    model_config = ConfigDict(use_attribute_docstrings=True)


class Health(_ApiModel):
    db: ProbeStatus
    worker: WorkerState | None
    """The worker as its row records it; null when the database cannot be read."""
    version: str


# ---------------------------------------------------------------------------
# GET /api/v1/system/status


class DatabaseStatus(_ApiModel):
    status: ProbeStatus
    """``ok`` when the database file exists and answers a read within about a second."""
    path: str
    """The database file (shown to the user)."""


class QueueCounts(_ApiModel):
    """The worker's queue (the job table's local runs and the conversion table) and its live slots."""

    queued_runs: int
    """Queued local runs."""
    running: int
    """Running local runs (live runs too) and running conversions."""
    queued_conversions: int
    live_slots_in_use: int
    """The worker's live slots in use: its Running live runs."""
    live_slots: int
    """The worker's live slots (two, beside its one batch slot)."""


class QueuedItem(_ApiModel):
    """A queued local run or conversion, as the dispatcher will take it (``jobs.order.queued_in_order``)."""

    kind: Literal["run", "conversion"]
    job_id: int | None
    """The run's job; null for a conversion (no job row)."""
    run: RunReference | None
    """The run (``I12``, ``R3``), or the indexing run a conversion converts; null for a job without a run row yet."""
    moved_to_front: bool


class WorkerThread(_ApiModel):
    """One of the worker's threads and its last mark (``jobs.worker``: written at most every
    ``execution.heartbeat_seconds``)."""

    name: Literal["dispatcher", "reconciler", "poller"]
    state: ThreadState
    last_mark_at: UTCTimestamp | None
    """Null before its first mark since the worker started."""
    last_mark_age_seconds: float | None
    """Seconds since its last mark (the server's clock); null before its first."""
    late_after_seconds: float | None
    """Late once its mark is older: three of its cycles (its interval, at least ``execution.heartbeat_seconds``) or
    45 s, whichever is longer; null for a thread that is off (it cannot be late)."""


class WorkerStatus(_ApiModel):
    """The worker as its row records it (``jobs.worker``)."""

    state: WorkerState
    host: str | None
    """Null when no worker ever started on this database."""
    pid: int | None
    started_at: UTCTimestamp | None
    stopped_at: UTCTimestamp | None
    """Set by the worker's last write at its stop."""
    threads: list[WorkerThread]
    """The dispatcher, the reconciler and the remote poller, in that order."""


class RemoteRunState(_ApiModel):
    """Where a remote run executes."""

    target: str
    """The execution target (``aps``, ``crux``)."""
    dm_state: str | None
    """DM's state for the run, as the submission and the remote poller last recorded it; null before submission."""


class ActiveRun(_ApiModel):
    """A queued or running run (its job), newest first."""

    job_id: int
    run: RunReference | None
    """The indexing or reconstruction run the job executes (``I12``, ``R3``)."""
    status: RunStatus
    """``queued`` or ``running``."""
    phase: str | None
    """The finer execution phase (``created``, ``published``, ``queued``, ``running``, ``finalizing``)."""
    n_inputs: int
    n_succeeded: int
    n_failed: int
    n_not_run: int
    heartbeat_age_seconds: float | None
    """Seconds since the run's last heartbeat; null until it starts. A remote run's heartbeat is the poller's."""
    stale: bool
    """A running local run whose heartbeat is missing or older than ``execution.stale_heartbeat_seconds``: the
    worker's reconciler (every ``execution.reconcile_seconds``) marks it interrupted once it finds no live job
    process. Always false for queued and remote runs."""
    stop_requested: bool
    """A stop was requested and the run has not ended yet."""
    remote: RemoteRunState | None
    """The target and DM's state of a remote run; null for a local one."""


class SystemStatus(_ApiModel):
    """The status page: database, API, the worker and its threads, the queue and the active runs. Never fails
    because the database is down: it reports ``error`` and what needs it is null."""

    version: str
    """The API's version."""
    database: DatabaseStatus
    worker: WorkerStatus | None
    """Null when the database cannot be read."""
    queue: QueueCounts | None
    """Null when the database cannot be read."""
    next_up: list[QueuedItem] | None
    """The next five items the dispatcher takes, in its order: queued conversions first (oldest request first),
    then the Queued local runs moved to the front (the newest move first), then the rest by creation. Null when the
    database cannot be read."""
    active_runs: list[ActiveRun] | None
    """Queued and running runs, newest first; null when the database cannot be read."""
