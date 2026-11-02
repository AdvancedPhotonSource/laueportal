"""The reconciler: the database is the source of truth.

It runs inside the worker (its reconciler thread, every ``execution.reconcile_seconds``, ``jobs.worker``) and as
``laueportal jobs reconcile --once``. One cycle (``reconcile_once``), on the database alone:

- a Running local run whose heartbeat is older than ``execution.stale_heartbeat_seconds`` and whose job process is
  not alive becomes Failed with the phase ``interrupted`` ("the worker or job process died");
- a running conversion whose job process is gone becomes ``failed`` ("Interrupted: the worker or job process died;
  convert the results again"), so the run can be converted again (``indexing.conversion.enqueue_conversion``
  replaces an ended conversion). With no job process recorded it counts as gone only once its start (the claim) is
  older than ``execution.stale_heartbeat_seconds``: a conversion is recorded just after its claim, and has no
  heartbeat;
- a run still ``created`` or ``published`` longer than ``execution.creation_timeout_seconds`` after its last
  change becomes Failed ("creation did not complete"); its run directory is kept for diagnosis (the rule is
  ``stuck_in_creation``; ``laueportal db check`` counts such runs with ``runs_stuck_in_creation``);
- the ``Idempotency-Key`` rows past their ``expires_at`` are deleted (``jobs.idempotency.purge_expired``, one
  indexed delete). Its failure is logged and never the cycle's, except a database error (``OperationalError``: a
  write lock past the busy timeout), which ends the cycle as any other (the worker's reconciler thread logs it once
  per streak).

A job process is alive as the worker's dispatcher counts its batch slot (``jobs.dispatch``): when the worker running
the cycle started it and it has not exited (``own``: the dispatcher's children, recorded on their rows or not yet),
or when the process the dispatcher recorded on the row exists on this host
(``jobs.dispatch.recorded_process_alive``: its pid with the recorded start since boot), e.g. an orphan whose worker
was killed. None recorded counts as dead, and so do a process on another host and a pid that is another process's
now. So a run or a conversion whose job process lives is never finalized here, stale heartbeat or not.

No queue checks, no lock, no re-enqueue: a Queued run is its own queue entry (the job table is the queue), and
the dispatcher takes it whenever a worker runs.

Remote runs are the remote poller's (``remote.poller``), except a remote run that never reached its submission
(``created`` or ``published`` with no submission ID): nothing else would ever end it. Status and phase changes go
through ``jobs.state``'s existing transitions only (``finalize`` with ``RunPhase.INTERRUPTED``,
``record_failure``). The candidates are read in one short session; the final transaction (``core.db.begin_write``)
re-reads each candidate and changes it only while it is still as read (its status, phase and recorded job process;
a run's heartbeat still stale).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal.core.db import begin_write
from laueportal.core.settings import ExecutionSettings
from laueportal.core.timeutil import utcnow
from laueportal.indexing.models import Conversion
from laueportal.jobs import state
from laueportal.jobs.backends import is_remote
from laueportal.jobs.dispatch import CONVERSION_FAILED, CONVERSION_RUNNING, CONVERT, RUN, recorded_process_alive
from laueportal.jobs.idempotency import purge_expired
from laueportal.jobs.models import Job
from laueportal.jobs.state import JobStatus, RunPhase

logger = logging.getLogger(__name__)

PROCESS_DIED = "the worker or job process died"
"""The message for a run (or a conversion) whose process is gone."""
CONVERSION_DIED = f"Interrupted: {PROCESS_DIED}; convert the results again"
"""A conversion's message when the reconciler fails it."""
CREATION_PHASES = (RunPhase.CREATED, RunPhase.PUBLISHED)
"""The phases of a run being created: its rows saved, its manifest not yet (or just) published, not queued yet."""

Own = Callable[[], Collection[tuple[str, int]]]
"""The items (``("run", job ID)``, ``("convert", indexing ID)``) whose job process the worker running the cycle
started and that has not exited (``jobs.dispatch.Dispatcher.running_items``)."""


@dataclass(frozen=True)
class ReconcilePolicy:
    """The reconciler's limits, from the ``execution`` settings."""

    stale_heartbeat_seconds: float = 300
    creation_timeout_seconds: float = 600
    reconcile_seconds: float = 60

    @classmethod
    def from_settings(cls, execution: ExecutionSettings) -> ReconcilePolicy:
        return cls(
            stale_heartbeat_seconds=execution.stale_heartbeat_seconds,
            creation_timeout_seconds=execution.creation_timeout_seconds,
            reconcile_seconds=execution.reconcile_seconds,
        )


@dataclass
class Reconciliation:
    """What one cycle changed."""

    interrupted: list[int] = field(default_factory=list)
    """Running runs finalized Failed/interrupted (job IDs)."""
    abandoned: list[int] = field(default_factory=list)
    """Runs stuck in ``created`` or ``published``, finalized Failed (job IDs)."""
    conversions_failed: list[int] = field(default_factory=list)
    """Running conversions whose job process is gone, now ``failed`` (indexing IDs)."""
    keys_purged: int = 0
    """Expired ``Idempotency-Key`` rows deleted (not a change to any run)."""

    def __bool__(self) -> bool:
        return bool(self.interrupted or self.abandoned or self.conversions_failed)


@dataclass(frozen=True)
class _Candidate:
    """A row as the reconciliation read it (plain values: no session stays open)."""

    kind: str
    key: int
    read: tuple[object, ...]
    """What must be unchanged when the final transaction re-reads it (``_as_read``)."""


def _as_read(row: Job | Conversion) -> tuple[object, ...]:
    """The values a candidate must still have to be changed: its state and its recorded job process."""
    if isinstance(row, Job):
        return (row.status, row.phase, row.process_host, row.process_pid, row.process_since_boot)
    return (row.status, row.started_at, row.process_host, row.process_pid, row.process_since_boot)


def _stale_reason(job: Job, stale_before: datetime) -> str | None:
    """Why a Running job's process must be gone (its last sign of life is older than ``stale_before``), or None."""

    last_sign = job.heartbeat_at or job.start_time or job.updated_at
    if last_sign is not None and last_sign >= stale_before:
        return None
    since = last_sign.isoformat(timespec="seconds") if last_sign else "its start"
    return f"Interrupted: {PROCESS_DIED} (no heartbeat since {since}); the run is not resumed"


def _conversion_gone(row: Conversion, stale_before: datetime) -> bool:
    """Whether a running conversion's job process is gone: the recorded one is not alive (or is another process
    now), or none is recorded (or one on another host) and the conversion started before ``stale_before``."""

    alive = recorded_process_alive(row.process_host, row.process_pid, row.process_since_boot)
    if alive is None:
        return row.started_at is None or row.started_at < stale_before
    return not alive


def _creation_reason(job: Job, created_before: datetime) -> str | None:
    """Why a run still ``created`` or ``published`` will never be queued (no change since ``created_before``)."""

    last_change = job.updated_at or job.submit_time
    if last_change is not None and last_change >= created_before:
        return None
    since = last_change.isoformat(timespec="seconds") if last_change else "an unknown time"
    return (
        f"Creation did not complete: the run is still {job.phase} since {since}; "
        f"its run directory is kept for diagnosis"
    )


def stuck_in_creation(job: Job, created_before: datetime) -> bool:
    """Whether ``job`` is a run stuck in creation, the rule the reconciler fails it by: unfinished (Queued or Running),
    still ``created`` or ``published`` with no change since ``created_before`` (``_creation_reason``), and no one
    else's to end (a local run, or a remote run that never reached its submission; one that did is the poller's)."""

    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING) or job.phase not in CREATION_PHASES:
        return False
    unclaimed = not is_remote(job.backend) or job.submission_id is None
    return unclaimed and _creation_reason(job, created_before) is not None


def runs_stuck_in_creation(
    session: Session, *, creation_timeout_seconds: float, now: datetime | None = None
) -> list[int]:
    """The IDs of the runs ``stuck_in_creation`` with ``execution.creation_timeout_seconds`` (``laueportal db check``
    counts them: the reconciler fails each at its next cycle). Only reads."""

    created_before = (now or utcnow()) - timedelta(seconds=creation_timeout_seconds)
    jobs = session.scalars(
        select(Job)
        .where(Job.status.in_((int(JobStatus.QUEUED), int(JobStatus.RUNNING))), Job.phase.in_(CREATION_PHASES))
        .order_by(Job.job_id)
    )
    return [job.job_id for job in jobs if stuck_in_creation(job, created_before)]


def _left_alone(candidate: _Candidate, why: str = "changed while it was being reconciled") -> None:
    what = f"Job {candidate.key}" if candidate.kind == RUN else f"The conversion of I{candidate.key}"
    logger.info("%s %s; left alone", what, why)


def _read_candidates(
    engine: Engine, stale_before: datetime, created_before: datetime | None
) -> tuple[list[_Candidate], list[_Candidate], list[_Candidate]]:
    """The cycle's candidates, in one short session: stale Running local runs and running conversions whose recorded
    job process is not alive, and runs stuck in creation."""

    dead: list[_Candidate] = []
    gone: list[_Candidate] = []
    creating: list[_Candidate] = []
    with Session(engine) as session:
        for job in session.scalars(
            select(Job).where(Job.status.in_((int(JobStatus.QUEUED), int(JobStatus.RUNNING))))
        ).all():
            candidate = _Candidate(RUN, job.job_id, _as_read(job))
            if job.phase in CREATION_PHASES:
                # A remote run that reached its submission is the poller's; one that never did is no one's.
                if created_before is not None and stuck_in_creation(job, created_before):
                    creating.append(candidate)
            elif is_remote(job.backend):
                continue  # The remote poller reconciles remote runs.
            elif job.status == JobStatus.RUNNING and _stale_reason(job, stale_before) is not None:
                if recorded_process_alive(job.process_host, job.process_pid, job.process_since_boot):
                    logger.warning(
                        "Job %s: no heartbeat since the stale limit, but its job process %s is alive; left alone",
                        job.job_id,
                        job.process_pid,
                    )
                    continue
                dead.append(candidate)
            # A Queued run is its own queue entry: nothing to check, nothing to put back.
        for row in session.scalars(select(Conversion).where(Conversion.status == CONVERSION_RUNNING)).all():
            if _conversion_gone(row, stale_before):
                gone.append(_Candidate(CONVERT, row.indexing_id, _as_read(row)))
    return dead, gone, creating


def reconcile_runs(
    engine: Engine,
    *,
    stale_after_seconds: float,
    creation_timeout_seconds: float | None = None,
    own: Own | None = None,
    now: datetime | None = None,
) -> Reconciliation:
    """One pass over the database's unfinished runs and running conversions (the module's rules); returns what it
    changed. Without ``creation_timeout_seconds``, runs in ``created`` or ``published`` are left alone. ``own`` (the
    worker's cycle gives its dispatcher's) is asked between the read and the final transaction, with no database
    connection held: a candidate it names is left alone."""

    moment = now or utcnow()
    stale_before = moment - timedelta(seconds=stale_after_seconds)
    created_before = None if creation_timeout_seconds is None else moment - timedelta(seconds=creation_timeout_seconds)
    result = Reconciliation()

    # 1. The candidates, in one short session.
    dead, gone, creating = _read_candidates(engine, stale_before, created_before)

    # 2. The worker's own job processes (its dispatcher's children), with no session open.
    if own is not None and (dead or gone):
        running = set(own())
        for candidate in [*dead, *gone]:
            if (candidate.kind, candidate.key) in running:
                _left_alone(candidate, "has a job process this worker started that has not exited")
        dead = [candidate for candidate in dead if (RUN, candidate.key) not in running]
        gone = [candidate for candidate in gone if (CONVERT, candidate.key) not in running]

    # 3. Change, in one write transaction, what is still as it was read.
    if dead or gone or creating:
        with Session(engine) as session, session.begin():
            begin_write(session)
            for candidate in [*dead, *creating]:
                job = session.get(Job, candidate.key)
                if job is None or _as_read(job) != candidate.read:
                    _left_alone(candidate)
                    continue
                if job.phase in CREATION_PHASES:
                    assert created_before is not None  # creating is empty without it
                    reason = _creation_reason(job, created_before)
                    if reason is None:
                        _left_alone(candidate)
                        continue
                    state.record_failure(job, reason, now=moment)
                    result.abandoned.append(job.job_id)
                else:
                    reason = _stale_reason(job, stale_before)
                    if reason is None:
                        _left_alone(candidate)
                        continue
                    state.finalize(job, status=JobStatus.FAILED, message=reason, phase=RunPhase.INTERRUPTED, now=moment)
                    result.interrupted.append(job.job_id)
                logger.warning("Job %s reconciled: %s", job.job_id, reason)
            for candidate in gone:
                row = session.get(Conversion, candidate.key)
                if row is None or _as_read(row) != candidate.read:
                    _left_alone(candidate)
                    continue
                row.status = CONVERSION_FAILED
                row.message = CONVERSION_DIED
                row.ended_at = moment
                result.conversions_failed.append(row.indexing_id)
                logger.warning("The conversion of I%s reconciled: %s", row.indexing_id, CONVERSION_DIED)
    return result


# ---------------------------------------------------------------------------
# One cycle


def reconcile_once(engine: Engine, policy: ReconcilePolicy, *, own: Own | None = None) -> Reconciliation:
    """One reconciliation cycle on the database (the module's rules); ``own``: the worker's dispatcher's job
    processes (``reconcile_runs``)."""

    result = reconcile_runs(
        engine,
        stale_after_seconds=policy.stale_heartbeat_seconds,
        creation_timeout_seconds=policy.creation_timeout_seconds,
        own=own,
    )
    if result:
        logger.warning(
            "Reconciled: %d interrupted %s, %d stuck in creation %s, %d conversions failed %s",
            len(result.interrupted),
            result.interrupted,
            len(result.abandoned),
            result.abandoned,
            len(result.conversions_failed),
            result.conversions_failed,
        )
    result.keys_purged = _purge_keys(engine)
    return result


def _purge_keys(engine: Engine) -> int:
    """Delete the expired ``Idempotency-Key`` rows; a failure is logged, never the cycle's, except a database error
    (``OperationalError``), which ends the cycle (``jobs.worker.reconciler_body`` logs it once per streak)."""
    try:
        purged = purge_expired(engine)
    except OperationalError:
        raise
    except Exception:
        logger.warning("Could not purge the expired Idempotency-Keys", exc_info=True)
        return 0
    if purged:
        logger.info("Purged %d expired Idempotency-Keys", purged)
    return purged
