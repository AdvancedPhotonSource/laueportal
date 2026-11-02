"""Shared run persistence: job creation and manifest publication.

Both run services follow the same sequence: create the job and run rows
in one transaction (the run ID names the run directory), then publish the
manifest and frozen request (``jobs.rundir``), then record the publication on
the job. A run whose publication fails is marked Failed with the reason; it is
never runnable because only ``RunPhase.PUBLISHED`` jobs may be enqueued.

Once the rows are saved, whatever fails (the publication, its recording, the
lookup after it) is a ``RunPublicationError`` naming the run's job, which is
marked Failed (``publishing``): a caller can always tell a run that was saved
from a refusal that saved nothing, and no run is left in phase ``created``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.files.inputs import ResolvedInput
from laueportal.jobs import rundir, state
from laueportal.jobs.arrivals import create_ledger
from laueportal.jobs.backends import BACKENDS
from laueportal.jobs.live import LiveDocument, write_live_document
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import MANIFEST_FILENAME, REQUEST_FILENAME, ManifestSummary

logger = logging.getLogger(__name__)


class RunPublicationError(RuntimeError):
    """Raised when a run's manifest or request could not be published; the job is marked Failed. ``job_id`` names
    the run's job (its rows exist)."""

    def __init__(self, message: str, *, job_id: int) -> None:
        super().__init__(message)
        self.job_id = job_id


def new_job(*, computer_name: str, priority: int, submitted_at: datetime, n_inputs: int, backend: str = "local") -> Job:
    if backend not in BACKENDS:
        raise ValueError(f"Unknown execution backend: {backend}")
    job = Job(
        backend=backend,
        computer_name=computer_name,
        status=int(state.JobStatus.QUEUED),
        priority=priority,
        submit_time=submitted_at,
        start_time=None,
        finish_time=None,
    )
    state.initialize(job, n_inputs=n_inputs, now=submitted_at)
    return job


def publish_run_inputs(
    engine: Engine,
    *,
    job_id: int,
    display_id: str,
    run_directory: str,
    inputs: Sequence[ResolvedInput],
    request_payload: Mapping[str, Any],
    depths: Mapping[int, float] | None = None,
    now: datetime | None = None,
) -> ManifestSummary:
    """Write ``inputs.jsonl`` and ``request.json`` into ``run_directory`` and record them."""

    try:
        summary = rundir.write_run_inputs(run_directory, inputs=inputs, request_payload=request_payload, depths=depths)
    except Exception as error:
        raise _publication_failed(engine, job_id=job_id, display_id=display_id, error=error, now=now) from error

    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        if job is None:
            raise RunPublicationError(
                f"{display_id}: job {job_id} vanished before its manifest was recorded", job_id=job_id
            )
        state.mark_published(
            job,
            run_directory=run_directory,
            manifest_path=os.path.join(run_directory, MANIFEST_FILENAME),
            manifest_digest=summary.sha256,
            request_path=os.path.join(run_directory, REQUEST_FILENAME),
            n_inputs=summary.n_inputs,
            now=now,
        )
    return summary


def publish_live_run(
    engine: Engine,
    *,
    job_id: int,
    display_id: str,
    run_directory: str,
    request_payload: Mapping[str, Any],
    live: LiveDocument,
    now: datetime | None = None,
) -> ManifestSummary:
    """Write a live run's files into ``run_directory`` and record them: an empty
    ``inputs.jsonl`` and ``request.json`` (``rundir.write_live_run_inputs``), then ``live.json``, then an empty
    ``arrivals.lock`` and ``arrivals.jsonl`` (``jobs.arrivals.create_ledger``); the job is published with 0
    inputs."""

    try:
        summary = rundir.write_live_run_inputs(run_directory, request_payload=request_payload)
        write_live_document(run_directory, live)
        create_ledger(run_directory)
    except Exception as error:
        raise _publication_failed(engine, job_id=job_id, display_id=display_id, error=error, now=now) from error

    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        if job is None:
            raise RunPublicationError(
                f"{display_id}: job {job_id} vanished before its manifest was recorded", job_id=job_id
            )
        state.mark_published(
            job,
            run_directory=run_directory,
            manifest_path=os.path.join(run_directory, MANIFEST_FILENAME),
            manifest_digest=summary.sha256,
            request_path=os.path.join(run_directory, REQUEST_FILENAME),
            n_inputs=summary.n_inputs,
            now=now,
        )
    return summary


@contextmanager
def publishing(engine: Engine, *, job_id: int, display_id: str, now: datetime | None = None) -> Iterator[None]:
    """Around what run creation does once its run and job rows are saved (the publication, its recording, the
    lookup after it): any failure becomes a ``RunPublicationError`` naming ``job_id``, whose job is marked Failed
    with the reason (best effort: the database may be what failed)."""

    try:
        yield
    except RunPublicationError:
        raise
    except Exception as error:
        raise _publication_failed(engine, job_id=job_id, display_id=display_id, error=error, now=now) from error


def _publication_failed(
    engine: Engine, *, job_id: int, display_id: str, error: BaseException, now: datetime | None
) -> RunPublicationError:
    """Mark the saved run's job Failed (unless it is terminal already), and the error to raise. A failure to record
    it is logged: the caller still reports the run as created and failed."""

    message = f"Run publication failed: {error}"
    try:
        with Session(engine) as session, session.begin():
            job = session.get(Job, job_id)
            if job is not None and not state.is_terminal(job):
                state.record_failure(job, message, now=now)
    except Exception:
        logger.exception("%s: its publication failure could not be recorded", display_id)
    return RunPublicationError(f"{display_id}: {message}", job_id=job_id)
