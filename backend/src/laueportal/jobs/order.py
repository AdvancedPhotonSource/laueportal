"""The dispatcher's order: which queued item the worker's dispatcher takes next.

The Queued live runs first (``jobs.live``: while one is Queued or Running, no batch item is claimed; they take the
two live slots, ``LIVE_SLOTS``, by ``job_id``); then the queued conversions (``conversion`` rows ``queued``, the oldest
request first); then the Queued local batch jobs (phase ``queued``) moved to the front (``moved_to_front_at``,
the newest move first), then the rest by ``job_id``. Remote runs are never the dispatcher's. ``jobs.dispatch`` takes
its next items with these queries, and the status page (``system.status``) lists the next items with
``queued_in_order`` and counts the live slots in use; this module imports neither the worker nor the run lifecycle, so
the API may use it (import contract 4).
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import ColumnElement, Select, not_, or_, select
from sqlalchemy.orm import Session

from laueportal.indexing.models import Conversion
from laueportal.jobs.backends import REMOTE_BACKENDS
from laueportal.jobs.live import live_marker
from laueportal.jobs.models import Job
from laueportal.jobs.state import JobStatus, RunPhase

Kind = Literal["run", "convert"]
RUN: Kind = "run"
CONVERT: Kind = "convert"

CONVERSION_QUEUED = "queued"
CONVERSION_RUNNING = "running"

LIVE_SLOTS = 2
"""The dispatcher's live slots, beside its one batch slot: a live session's wire run and its
follower, or one of them alone."""


def local_jobs() -> ColumnElement[bool]:
    """A job the dispatcher runs: no remote target."""
    return or_(Job.backend.is_(None), Job.backend.not_in(REMOTE_BACKENDS))


def queued_conversions() -> Select[int]:
    """The queued conversions' indexing IDs, the oldest request first."""
    return (
        select(Conversion.indexing_id)
        .where(Conversion.status == CONVERSION_QUEUED)
        .order_by(Conversion.requested_at, Conversion.indexing_id)
    )


def queued_runs() -> Select[int]:
    """The Queued local jobs' IDs, live and batch: those moved to the front first, the newest move first; then by
    ``job_id`` (the status page's count)."""
    return (
        select(Job.job_id)
        .where(Job.status == int(JobStatus.QUEUED), Job.phase == RunPhase.QUEUED, local_jobs())
        .order_by(Job.moved_to_front_at.desc().nulls_last(), Job.job_id)
    )


def batch_jobs() -> ColumnElement[bool]:
    """A job that is not a live run, one without a queue identity too (SQL's NOT of a NULL comparison is NULL)."""
    return or_(Job.queue_job_id.is_(None), not_(live_marker()))


def queued_batch_runs() -> Select[int]:
    """The Queued local batch jobs' IDs, in the batch slot's order (``queued_runs``' without the live runs)."""
    return queued_runs().where(batch_jobs())


def queued_live_runs() -> Select[int]:
    """The Queued live runs' IDs, by ``job_id`` (a live run is never moved to the front)."""
    return (
        select(Job.job_id)
        .where(Job.status == int(JobStatus.QUEUED), Job.phase == RunPhase.QUEUED, live_marker())
        .order_by(Job.job_id)
    )


def running_live_runs() -> Select[int]:
    """The Running live runs' IDs: the live slots in use, as the database records them (the status page's)."""
    return select(Job.job_id).where(Job.status == int(JobStatus.RUNNING), live_marker()).order_by(Job.job_id)


def queued_in_order(session: Session, *, limit: int | None = None) -> list[tuple[Kind, int]]:
    """Every queued item in dispatch order (the live runs, then the conversions, then the batch runs; ``limit`` of
    them at most)."""
    live = session.scalars(queued_live_runs().limit(limit)).all()
    conversions = session.scalars(queued_conversions().limit(limit)).all()
    jobs = session.scalars(queued_batch_runs().limit(limit)).all()
    items: list[tuple[Kind, int]] = [
        *((RUN, j) for j in live),
        *((CONVERT, i) for i in conversions),
        *((RUN, j) for j in jobs),
    ]
    return items if limit is None else items[:limit]
