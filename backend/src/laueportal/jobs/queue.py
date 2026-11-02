"""Run queueing: the job table is the queue, and the execution policy.

A local run is queued by committing its phase ``queued`` (``enqueue_run``): that row is its queue entry, which the
worker's dispatcher claims (``jobs.dispatch``). A remote run goes to DM.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from laueportal.core.db import begin_write
from laueportal.core.settings import ExecutionSettings
from laueportal.core.timeutil import utcnow
from laueportal.indexing.models import IndexingRun
from laueportal.jobs import state
from laueportal.jobs.backends import is_remote
from laueportal.jobs.live import LiveSessionOpen, live_queue_id, open_live_runs
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import verify_manifest
from laueportal.reconstruction.models import ReconstructionRun

if TYPE_CHECKING:
    from laueportal.remote.settings import RemoteSettings

logger = logging.getLogger(__name__)

RUN_JOB_TYPE = "run"
CONVERT_JOB_TYPE = "convert_results"
"""A results conversion's kind and its id's prefix (``indexing.conversion``: ``convert_results_<indexing id>``),
as ``RUN_JOB_TYPE`` is a local run's id prefix (``run_queue_id``)."""


def run_queue_id(job_id: int) -> str:
    """A local run's queue identity (``job.queue_job_id``): ``run_<id>``, kept on the row; nothing reads it."""

    return f"{RUN_JOB_TYPE}_{int(job_id)}"


@dataclass(frozen=True)
class RunPolicy:
    """Whole-run execution limits, from the ``execution`` settings.

    ``job_timeout_seconds`` is the wall-clock limit for one run; the job process's own timeout raises inside
    the run when it is exceeded (``jobs.process``), and the executor records a failure.
    ``stale_heartbeat_seconds`` decides
    when a Running job whose heartbeat stopped is reconciled as interrupted.
    ``workers`` bounds an indexing run's processes; ``reconstruction_workers``
    is the number of points a wire reconstruction computes at once, each with
    ``execution.num_threads`` threads and its own stripe-buffer budget.
    """

    job_timeout_seconds: int = 24 * 3600
    heartbeat_seconds: float = 15.0
    progress_seconds: float = 5.0
    cancel_poll_seconds: float = 2.0
    stale_heartbeat_seconds: int = 300
    shutdown_grace_seconds: int = 600
    workers: int = 4
    max_in_flight: int | None = None
    reconstruction_workers: int = 1

    def __post_init__(self) -> None:
        if self.job_timeout_seconds <= 0:
            raise ValueError("job_timeout_seconds must be positive")
        for name in ("heartbeat_seconds", "progress_seconds", "cancel_poll_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.stale_heartbeat_seconds <= self.heartbeat_seconds:
            raise ValueError("stale_heartbeat_seconds must exceed heartbeat_seconds")
        if self.shutdown_grace_seconds < 0:
            raise ValueError("shutdown_grace_seconds must not be negative")
        if self.workers <= 0:
            raise ValueError("workers must be positive")
        if self.reconstruction_workers <= 0:
            raise ValueError("reconstruction_workers must be positive")
        if self.max_in_flight is not None and self.max_in_flight < self.workers:
            raise ValueError("max_in_flight must be at least workers")

    @classmethod
    def from_settings(cls, execution: ExecutionSettings) -> RunPolicy:
        """The policy part of ``settings.execution`` (the compute knobs stay in the settings)."""

        values: dict[str, Any] = {field.name: getattr(execution, field.name) for field in fields(cls)}
        return cls(**values)


# --- enqueueing ------------------------------------------------------------------


def enqueue_run(
    job_id: int,
    *,
    engine: Engine,
    at_front: bool = False,
    remote: RemoteSettings | None = None,
    dm_client: Any = None,
) -> str:
    """Hand a published run to its target exactly once; returns its queue identity.

    A local run is queued by one transaction that moves it to phase ``queued`` (``state.mark_queued``, after its
    manifest is verified): the committed row is its queue entry (``run_<job_id>``), which the worker's dispatcher
    claims; a second call raises. ``at_front`` queues it moved to the front (``moved_to_front_at``, as Move to Front),
    which a remote run refuses (its scheduler orders it). A job whose ``backend`` is remote (``aps``, ``crux``) goes to
    DM through ``remote.submission.submit_run`` with the ``remote`` settings (``remote.settings.portal_settings``;
    ``None`` when remote execution is disabled, which refuses it) and returns DM's job ID.
    """

    with Session(engine) as session:
        job = session.get(Job, job_id)
        backend = job.backend if job is not None else None
    if is_remote(backend):
        if at_front:
            raise ValueError("DM jobs cannot be moved ahead of scheduler jobs")
        from laueportal.remote.submission import submit_run  # the DM client is never loaded for local runs

        return submit_run(job_id, engine=engine, settings=remote, client=dm_client)

    queue_id = run_queue_id(job_id)
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError(f"Job {job_id} does not exist")
        state.assert_enqueueable(job)
        verify_manifest(job.manifest_path or "", expected_digest=job.manifest_digest or "", expected_count=job.n_inputs)
        state.mark_queued(job, queue_id)
        if at_front:
            job.moved_to_front_at = utcnow()
    logger.info("Queued run %s", job_id)
    return queue_id


def enqueue_live_run(job_id: int, *, engine: Engine) -> str:
    """Queue a published live run: as ``enqueue_run`` queues a local run, under its queue
    identity ``live_<job id>``, which marks it live (``jobs.live``). A live run is always local (``backend``
    ``local``); returns the identity."""

    queue_id = live_queue_id(job_id)
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError(f"Job {job_id} does not exist")
        if is_remote(job.backend):
            raise ValueError(f"Job {job_id} runs on {job.backend}; a live run is local")
        state.assert_enqueueable(job)
        verify_manifest(job.manifest_path or "", expected_digest=job.manifest_digest or "", expected_count=job.n_inputs)
        state.mark_queued(job, queue_id)
    logger.info("Queued live run %s", job_id)
    return queue_id


def enqueue_live_runs(job_ids: Sequence[int], *, engine: Engine) -> list[str]:
    """Queue a live session's published runs together (``enqueue_live_run``'s checks for each, in one transaction
    holding the write lock): the dispatcher, whose claim reads in a transaction of its own, sees every one queued or
    none, so a wire run is never claimed before its follower is queued (and so never alone with the whole live share,
    ``dispatch.live_cores``; the Live page's Start). Any refusal queues none; so does another live run Queued or Running
    (``LiveSessionOpen``, checked in the same transaction): one live session at a time holds in the database, whichever
    process starts a session (Start's lock is one api process's). Returns their queue identities, in order."""

    queue_ids = [live_queue_id(job_id) for job_id in job_ids]
    with Session(engine) as session:  # the manifests first, read outside the write lock (the runs root may be NFS)
        manifests = [
            session.execute(
                select(Job.manifest_path, Job.manifest_digest, Job.n_inputs).where(Job.job_id == job_id)
            ).first()
            for job_id in job_ids
        ]
    for job_id, manifest in zip(job_ids, manifests, strict=True):
        if manifest is None:
            raise ValueError(f"Job {job_id} does not exist")
        verify_manifest(manifest[0] or "", expected_digest=manifest[1] or "", expected_count=manifest[2])
    with Session(engine) as session, session.begin():
        begin_write(session)
        other = session.execute(open_live_runs().where(Job.job_id.not_in(job_ids)).limit(1)).first()
        if other is not None:
            raise LiveSessionOpen(other.job_id, other.status)
        for job_id, queue_id, manifest in zip(job_ids, queue_ids, manifests, strict=True):
            job = session.get(Job, job_id)
            if job is None:  # nothing deletes jobs
                raise ValueError(f"Job {job_id} does not exist")
            if is_remote(job.backend):
                raise ValueError(f"Job {job_id} runs on {job.backend}; a live run is local")
            if (job.manifest_path, job.manifest_digest, job.n_inputs) != tuple(manifest or ()):
                raise ValueError(f"Job {job_id}'s manifest changed while it was queued")
            state.assert_enqueueable(job)
            state.mark_queued(job, queue_id)
    logger.info("Queued live runs %s together", ", ".join(str(job_id) for job_id in job_ids))
    return queue_ids


def _job_id_for(engine: Engine, model: type[IndexingRun] | type[ReconstructionRun], run_id: int, label: str) -> int:
    with Session(engine) as session:
        job_id = session.scalar(select(model.job_id).where(model.id == run_id))
    if job_id is None:
        raise ValueError(f"{label}{run_id} does not exist")
    return job_id


def enqueue_indexing(
    indexing_id: int,
    *,
    engine: Engine,
    at_front: bool = False,
    remote: RemoteSettings | None = None,
    dm_client: Any = None,
) -> str:
    """Queue a persisted indexing run by its ``I{id}`` database ID (``enqueue_run``: a remote run goes to DM with
    ``remote`` and ``dm_client``)."""

    return enqueue_run(
        _job_id_for(engine, IndexingRun, int(indexing_id), "Indexing I"),
        engine=engine,
        at_front=at_front,
        remote=remote,
        dm_client=dm_client,
    )


def enqueue_reconstruction(
    reconstruction_id: int,
    *,
    engine: Engine,
    at_front: bool = False,
    remote: RemoteSettings | None = None,
    dm_client: Any = None,
) -> str:
    """Queue a persisted reconstruction by its ``R{id}`` database ID (``enqueue_run``: a remote run goes to DM
    with ``remote`` and ``dm_client``)."""

    return enqueue_run(
        _job_id_for(engine, ReconstructionRun, int(reconstruction_id), "Reconstruction R"),
        engine=engine,
        at_front=at_front,
        remote=remote,
        dm_client=dm_client,
    )
