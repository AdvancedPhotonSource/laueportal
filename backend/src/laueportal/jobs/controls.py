"""Run-level queue controls (the job table is the queue): Stop and Move to Front, with typed
results (``jobs.schemas``).

Each control reads and decides in one transaction that starts with ``core.db.begin_write``, so it never interleaves
with the dispatcher's claim (``jobs.dispatch.claim_run``) or a remote submission (``remote.submission.submit_run``):

- ``cancel_run``: a Queued run is Cancelled at once (a local one is its own queue entry: nothing to take off a
  queue); a Running one gets the stop flag, which its executor polls (it stops admitting inputs, drains, keeps the
  completed results and finalizes Cancelled). A submitted remote run (``backend`` ``aps`` or ``crux``) gets
  the flag too, which the remote poller delivers (``remote.poller``); one never submitted is
  cancelled at once, like a local one: either it is cancelled before its submission is recorded (which then refuses
  it, and DM is never asked), or the stop finds the submission and is recorded for the poller.
- ``stop_run_now``: a live run's Stop now: one not stopped yet is stopped as ``cancel_run`` does;
  one Running whose stop was requested gets ``state.STOP_NOW_REQUESTED``, and the worker's dispatcher kills its job
  process and ends it Failed, ``interrupted`` (``jobs.dispatch``).
- ``move_run_to_front`` sets ``job.moved_to_front_at`` on a Queued local run: the dispatcher takes the runs moved to
  the front first, the newest move first. A run the dispatcher claimed first is no longer queued (409); a move is
  never undone. Remote runs are refused: the remote scheduler orders them. So are live runs, which
  take the live slots, by ``job_id``, before any batch item.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.core.db import begin_write
from laueportal.core.timeutil import utcnow
from laueportal.jobs import state
from laueportal.jobs.backends import is_remote
from laueportal.jobs.live import has_live_marker
from laueportal.jobs.models import Job
from laueportal.jobs.schemas import CancelResult, CancelState, MoveResult, MoveState
from laueportal.jobs.state import JobStatus, RunPhase

logger = logging.getLogger(__name__)


def cancel_run(job_id: int, *, engine: Engine) -> CancelResult:
    """Cancel a run (one transaction; the module's rules).

    Returns the ``CancelState`` and ``n_pending`` (the inputs not processed when the request was made); database
    errors propagate."""

    with Session(engine) as session, session.begin():
        begin_write(session)  # read and decide under the write lock: no claim or submission interleaves
        job = session.get(Job, job_id)
        if job is None:
            return CancelResult(job_id=job_id, state=CancelState.NOT_FOUND)
        if state.is_terminal(job):
            return CancelResult(job_id=job_id, state=CancelState.ALREADY_TERMINAL)
        state.request_cancellation(job, reason="user")
        if is_remote(job.backend) and job.submission_id is not None:
            # A submitted remote run: the poller delivers the request to the remote side
            # (remote.poller.request_cancellation) and finalizes the run from what comes back.
            pending = job.n_inputs - job.n_succeeded - job.n_failed
            return CancelResult(job_id=job_id, state=CancelState.REQUESTED, n_pending=pending)
        if job.status == JobStatus.QUEUED:
            state.finalize(job, status=JobStatus.CANCELLED, message="Cancelled before the run started")
            return CancelResult(job_id=job_id, state=CancelState.CANCELLED, n_pending=job.n_inputs)
        pending = job.n_inputs - job.n_succeeded - job.n_failed
        return CancelResult(job_id=job_id, state=CancelState.REQUESTED, n_pending=pending)


def stop_run_now(job_id: int, *, engine: Engine) -> CancelResult:
    """Stop a live run now (the Live page's Stop now, while its session is Stopping), in one
    transaction under the write lock: a Queued one is Cancelled at once and a Running one without a stop gets its stop
    flag (it is sealed and drains), as ``cancel_run``; a Running one whose stop was requested gets its Stop now
    (``state.request_stop_now``: the dispatcher kills its job process and ends it Failed, ``interrupted``, with what
    it published kept; ``stopping_now``). A batch run is ``not_live`` and left as it is."""

    with Session(engine) as session, session.begin():
        begin_write(session)
        job = session.get(Job, job_id)
        if job is None:
            return CancelResult(job_id=job_id, state=CancelState.NOT_FOUND)
        if state.is_terminal(job):
            return CancelResult(job_id=job_id, state=CancelState.ALREADY_TERMINAL)
        if not has_live_marker(job):
            return CancelResult(job_id=job_id, state=CancelState.NOT_LIVE)
        pending = job.n_inputs - job.n_succeeded - job.n_failed
        if job.status == JobStatus.RUNNING and job.cancel_requested_at is not None:
            state.request_stop_now(job)
            return CancelResult(job_id=job_id, state=CancelState.STOPPING_NOW, n_pending=pending)
        state.request_cancellation(job, reason="user")
        if job.status == JobStatus.QUEUED:
            state.finalize(job, status=JobStatus.CANCELLED, message="Cancelled before the run started")
            return CancelResult(job_id=job_id, state=CancelState.CANCELLED, n_pending=job.n_inputs)
        return CancelResult(job_id=job_id, state=CancelState.REQUESTED, n_pending=pending)


def move_run_to_front(job_id: int, *, engine: Engine) -> MoveResult:
    """Move a Queued local run ahead of the other queued runs (``moved_to_front_at``, under the write lock). Active
    work is never preempted; nothing changes unless the result is ``moved``."""

    with Session(engine) as session, session.begin():
        begin_write(session)
        job = session.get(Job, job_id)
        if job is None:
            return MoveResult(job_id=job_id, state=MoveState.NOT_FOUND)
        phase = job.phase
        if job.status != JobStatus.QUEUED or phase != RunPhase.QUEUED:
            return MoveResult(job_id=job_id, state=MoveState.NOT_QUEUED, phase=phase)
        if is_remote(job.backend):  # the remote scheduler orders DM jobs
            return MoveResult(job_id=job_id, state=MoveState.REMOTE, phase=phase)
        if has_live_marker(job):  # a live run is never moved
            return MoveResult(job_id=job_id, state=MoveState.LIVE, phase=phase)
        job.moved_to_front_at = utcnow()
    logger.info("Job %s moved to the front of the queue", job_id)
    return MoveResult(job_id=job_id, state=MoveState.MOVED, phase=phase)
