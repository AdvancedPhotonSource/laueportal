"""Persist a submission identity before contacting DM; never automatically submit twice.

The job must be enqueueable and its target enabled; the plan is written (``remote.plan.save_plan``: ``bdp.json`` and the
DM inbox copy); then, in one transaction that holds the database's write lock (``core.db.begin_write``), the job, read
again, must still be enqueueable, and it records the submission ID, ``external_state`` ``submitting`` and the phase
``queued`` (queue identity ``dm:<submission>``; never the worker's dispatcher's). Only then is DM asked to start the
workflow. A stop that came while the plan was written (``jobs.controls.cancel_run``, which takes the same lock) wins:
the run is Cancelled, the submission is refused before DM is called, and the inbox copy is removed. The slow plan (every
input file is checked on ``crux``) is written outside the lock. While the plan is built, the job records its progress
(``updated_at``, at most every ``PLAN_PROGRESS_SECONDS``), so the reconciler's ``execution.creation_timeout_seconds``
counts from the last input checked, not from the publication: a large ``crux`` run on a slow mount is not failed while
its plan is being built, and a build stuck on a hung mount is (that build then stops at its next progress). A lost reply
(an error or a timeout) may mean DM accepted the job: the job is marked ``submission_uncertain`` and the poller finds
the DM job by submission ID (``remote.poller.poll_once``); nothing ever submits it again. DM's answer never overwrites
what the poller recorded meanwhile (DM's job, found by submission ID) nor a job that has ended (the poller cancels a
stopped submission DM has no job for, after a grace period): such a job keeps its state and gets a message naming the DM
job to cancel by hand, also logged. ``jobs.queue.enqueue_run`` calls this for a job whose ``backend`` is not ``local``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.core.db import begin_write
from laueportal.core.timeutil import utcnow
from laueportal.jobs import state
from laueportal.jobs.models import Job
from laueportal.jobs.state import ExecutionStateError
from laueportal.remote.dm import DMClient
from laueportal.remote.plan import save_plan, submission_path
from laueportal.remote.settings import RemoteSettings, require_backend

logger = logging.getLogger(__name__)

SUBMITTING = "submitting"
SUBMISSION_UNCERTAIN = "submission_uncertain"
PENDING = "pending"
PLAN_PROGRESS_SECONDS = 60.0
"""How often a plan being built records its progress on the job (well within ``creation_timeout_seconds``)."""


def plan_progress(job_id: int, *, engine: Engine) -> Callable[[], None]:
    """``remote.plan.build_plan``'s ``progress`` for ``job_id``: every ``PLAN_PROGRESS_SECONDS``, a short write
    transaction moves the job's last change (``updated_at``, which the reconciler's creation timeout counts from)
    to now. Raises ``ExecutionStateError`` once the job is no longer enqueueable (stopped, or failed by the
    reconciler), which ends the plan's build before anything is written."""
    last = time.monotonic()

    def progress() -> None:
        nonlocal last
        if time.monotonic() - last < PLAN_PROGRESS_SECONDS:
            return
        with Session(engine) as session, session.begin():
            begin_write(session)
            job = session.get(Job, job_id)
            if job is None:
                raise ExecutionStateError(f"job {job_id} vanished")
            state.assert_enqueueable(job)
            job.updated_at = utcnow()
        last = time.monotonic()

    return progress


def queue_identity(submission_id: str) -> str:
    """The job's ``queue_job_id`` while DM runs it."""
    return f"dm:{submission_id}"


def submit_run(job_id: int, *, engine: Engine, settings: RemoteSettings | None, client: Any = None) -> str:
    """Submit a published remote run to DM; returns DM's processing job ID.

    ``settings`` are the portal's remote settings (``remote.settings.portal_settings``), which are ``None`` while
    ``[remote]`` is disabled: a remote run is then refused.
    """
    if settings is None:
        raise ValueError(
            "Remote execution is not enabled: no remote settings were given (remote.settings.portal_settings "
            "returns None while [remote] enabled is false)"
        )
    if not settings.enabled:
        raise ValueError("Remote execution is not enabled: the remote settings given are disabled")
    client = client or DMClient(settings.dm)
    submission_id = str(uuid4())
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError(f"Job {job_id} does not exist")
        state.assert_enqueueable(job)
        require_backend(job.backend, settings)
        plan = save_plan(job, settings, submission_id, progress=plan_progress(job_id, engine=engine))
    try:
        with Session(engine) as session, session.begin():
            begin_write(session)  # a stop (jobs.controls.cancel_run) either came first or waits for this commit
            job = session.get(Job, job_id)
            if job is None:
                raise ExecutionStateError(f"job {job_id} vanished")
            state.assert_enqueueable(job)
            job.submission_id = submission_id
            job.external_state = SUBMITTING
            state.mark_queued(job, queue_identity(submission_id))
    except ExecutionStateError as error:
        # Nothing was recorded and DM was never asked: no DM job will ever read the inbox copy.
        submission_path(settings, submission_id).unlink(missing_ok=True)
        raise ExecutionStateError(
            f"job {job_id} changed while its DM submission was prepared ({error}); it was not submitted"
        ) from error
    try:
        record = client.submit(plan)
        external_id = record["id"] if isinstance(record, dict) else str(record)
        if not external_id:
            raise ValueError("DM returned no processing job ID")
    except Exception:
        # A timeout may mean DM accepted the job but its reply was lost.
        # The poller searches by submissionId; it must never blindly resubmit.
        with Session(engine) as session, session.begin():
            begin_write(session)
            job = session.get(Job, job_id)
            assert job is not None
            if state.is_terminal(job):
                state.append_message(job, _ended_meanwhile(submission_id, None))
            elif job.external_id is None:  # else the poller has found DM's job already: nothing is uncertain
                job.external_state = SUBMISSION_UNCERTAIN
                state.append_message(job, "DM submission response was lost; reconciliation required before retrying")
        logger.exception("DM submission %s needs reconciliation", submission_id)
        raise
    with Session(engine) as session, session.begin():
        begin_write(session)
        job = session.get(Job, job_id)
        assert job is not None
        ended = state.is_terminal(job)
        if ended:
            state.append_message(job, _ended_meanwhile(submission_id, external_id))
        elif job.external_id is None:  # else the poller has found DM's job already and follows it
            job.external_id = external_id
            job.external_state = PENDING
    if ended:
        logger.error("Remote job %s: %s", job_id, _ended_meanwhile(submission_id, external_id))
    return external_id


def _ended_meanwhile(submission_id: str, external_id: str | None) -> str:
    """The message for DM's reply (``external_id``, or ``None`` when it was lost) to a submission whose job ended while
    DM was asked: nothing follows a DM job of an ended run, so the operator cancels it in DM."""
    if external_id is None:
        return (
            f"DM's reply to submission {submission_id} was lost after this run ended; if DM has a processing job "
            "for it (look it up by submissionId), cancel it in DM"
        )
    return (
        f"DM accepted submission {submission_id} as processing job {external_id} after this run ended; cancel it in DM"
    )
