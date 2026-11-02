"""Run progress and report queries backed by stored counters, never per-input rows.

``RunProgress`` holds raw counters and timestamps only, plus where the run executes (the
execution target and DM's state). Wording ("7/10", "3 failed", the last line of the messages, the status
label) is the frontend's job.

The run and job grids share the status and counter columns (``grid_progress_columns``,
declared by ``RUN_PROGRESS_FIELDS``) and their ``changed_since`` condition (``active_or_changed_since``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, case, or_, select
from sqlalchemy.orm import Session

from laueportal.core.paging import GridField
from laueportal.core.timeutil import utcnow
from laueportal.indexing.models import IndexingRun
from laueportal.jobs.backends import is_remote
from laueportal.jobs.models import Job
from laueportal.jobs.schemas import RunStatus
from laueportal.jobs.state import TERMINAL_STATUSES, JobStatus
from laueportal.reconstruction.models import ReconstructionRun


@dataclass(frozen=True)
class RunProgress:
    """Counters and execution state of one job, as stored (``status`` is a ``JobStatus`` value)."""

    job_id: int
    status: int
    phase: str | None
    n_inputs: int
    n_succeeded: int
    n_failed: int
    n_not_run: int
    updated_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    queue_job_id: str | None = None
    failure_report_path: str | None = None
    messages: str | None = None
    backend: str | None = None
    """The execution target (``jobs.backends``); None is local (a job from before the column existed)."""
    external_state: str | None = None
    """A remote run's DM state, as the submission and the poller record it."""
    external_url: str | None = None

    @property
    def n_processed(self) -> int:
        """Inputs whose processing finished, with or without error."""

        return self.n_succeeded + self.n_failed

    @property
    def n_pending(self) -> int:
        return max(self.n_inputs - self.n_processed - self.n_not_run, 0)

    @property
    def fraction(self) -> float | None:
        return None if self.n_inputs == 0 else self.n_processed / self.n_inputs

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @classmethod
    def from_job(cls, job: Job) -> RunProgress:
        return cls(
            job_id=job.job_id,
            status=job.status,
            phase=job.phase,
            n_inputs=job.n_inputs,
            n_succeeded=job.n_succeeded,
            n_failed=job.n_failed,
            n_not_run=job.n_not_run,
            updated_at=job.updated_at,
            heartbeat_at=job.heartbeat_at,
            cancel_requested_at=job.cancel_requested_at,
            queue_job_id=job.queue_job_id,
            failure_report_path=job.failure_report_path,
            messages=job.messages,
            backend=job.backend,
            external_state=job.external_state,
            external_url=job.external_url,
        )


def load_progress(session: Session, job_id: int) -> RunProgress | None:
    job = session.get(Job, job_id)
    return None if job is None else RunProgress.from_job(job)


ACTIVE_STATUSES = (int(JobStatus.QUEUED), int(JobStatus.RUNNING))


def derived_progress_columns():
    """Processed and pending counts derived in SQL, for queries that already select the whole Job."""

    processed = Job.n_succeeded + Job.n_failed
    return [
        processed.label("n_processed"),
        (Job.n_inputs - processed - Job.n_not_run).label("n_pending"),
    ]


def progress_columns():
    """Counter and liveness columns for table queries, plus the derived processed and pending counts."""

    return [
        Job.phase.label("phase"),
        Job.n_inputs.label("n_inputs"),
        Job.n_succeeded.label("n_succeeded"),
        Job.n_failed.label("n_failed"),
        Job.n_not_run.label("n_not_run"),
        *derived_progress_columns(),
        Job.heartbeat_at.label("heartbeat_at"),
        Job.cancel_requested_at.label("cancel_requested_at"),
    ]


def active_or_changed_since(since: datetime | None) -> ColumnElement[bool]:
    """Filter for jobs a live table must refresh: active runs plus anything finalized since ``since``.

    Terminal jobs never change again, so once a poll has seen a run finish it
    drops out of the next poll. Rows migrated from history have no ``updated_at``
    and are never active, so they are never re-sent.
    """

    active = Job.status.in_(ACTIVE_STATUSES)
    if since is None:
        return active
    return or_(active, Job.updated_at >= since)


def run_status_name() -> ColumnElement[str]:
    """``Job.status`` as the API names it (``jobs.schemas.RunStatus``: ``queued``, ``running``, ...), in SQL, so
    that a grid filters the name it shows. It sorts by the code (``status_code``, ``RUN_PROGRESS_FIELDS``)."""

    return case({int(status): RunStatus.of(status).value for status in JobStatus}, value=Job.status)


def grid_progress_columns() -> list[ColumnElement[Any]]:
    """A run's status (its API name, and its code to sort by) and counters, by the run and job grids' field names
    (``RUN_PROGRESS_FIELDS``). The row models ignore ``status_code``.

    ``n_processed`` (succeeded + failed) is the Run Progress column."""

    return [
        run_status_name().label("status"),
        Job.status.label("status_code"),
        Job.n_inputs.label("n_inputs"),
        Job.n_succeeded.label("n_succeeded"),
        Job.n_failed.label("n_failed"),
        Job.n_not_run.label("n_not_run"),
        (Job.n_succeeded + Job.n_failed).label("n_processed"),
    ]


RUN_PROGRESS_FIELDS: Mapping[str, GridField] = {
    "status": GridField("text", sort_by="status_code"),
    "n_inputs": GridField("number"),
    "n_succeeded": GridField("number"),
    "n_failed": GridField("number"),
    "n_not_run": GridField("number"),
    "n_processed": GridField("number"),
}
"""``grid_progress_columns``' grid fields: each sorts and filters (row-level, no join). ``status`` filters by its
name and sorts by its code, in the lifecycle order: queued, running, finished, failed, cancelled."""


def heartbeat_age_seconds(progress: RunProgress, *, now: datetime | None = None) -> float | None:
    """Seconds since the run's last sign of life, or None for a run that never started."""

    last_sign = progress.heartbeat_at
    if last_sign is None:
        return None
    return max(((now or utcnow()) - last_sign).total_seconds(), 0.0)


def is_stale(progress: RunProgress, *, stale_after_seconds: float, now: datetime | None = None) -> bool:
    """Whether a running local run has gone silent: no heartbeat, or none for ``stale_after_seconds``
    (``execution.stale_heartbeat_seconds``). The worker's startup reconciliation then marks it interrupted.

    Only local runs (a remote run's heartbeat comes from the poller), and only Running ones.
    """

    if is_remote(progress.backend) or progress.status != JobStatus.RUNNING:
        return False
    age = heartbeat_age_seconds(progress, now=now)
    return age is None or age > stale_after_seconds


@dataclass(frozen=True)
class ActiveJob:
    """A queued or running job and the run it executes (one of the two IDs, or neither for a bare job)."""

    progress: RunProgress
    indexing_id: int | None
    reconstruction_id: int | None


def load_active_runs(session: Session) -> list[ActiveJob]:
    """Every queued or running job with its run, newest first: one query."""

    statement = (
        select(Job, IndexingRun.id, ReconstructionRun.id)
        .outerjoin(IndexingRun, IndexingRun.job_id == Job.job_id)
        .outerjoin(ReconstructionRun, ReconstructionRun.job_id == Job.job_id)
        .where(Job.status.in_(ACTIVE_STATUSES))
        .order_by(Job.job_id.desc())
    )
    return [
        ActiveJob(RunProgress.from_job(job), indexing_id, reconstruction_id)
        for job, indexing_id, reconstruction_id in session.execute(statement)
    ]
