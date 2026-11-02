"""
Job table with info on compute jobs.

One job is one queued run. Per-input bookkeeping lives in the run's manifest
and failure report on disk (see ``laueportal.jobs.rundir``); the
database keeps run-level counters and execution state so progress and report
queries never join per-frame rows.

The table, column and attribute names are v1's (``dde1bde``, plus ``3ad1557``'s five remote-execution columns,
revision 0002). Timestamps are UTC (``UTCDateTime``) since revision 0005; get them from ``core.timeutil.utcnow()``.
Revision 0007 added ``job.moved_to_front_at``, the job process columns, and the tables
``idempotency_key`` (``IdempotencyKey``) and ``worker`` (``Worker``); revision 0008 v1 ``cb1159c``'s
``job.remote_observation``.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, CheckConstraint, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from laueportal.core.db import Base
from laueportal.core.timeutil import UTCDateTime

if TYPE_CHECKING:
    from laueportal.indexing.models import IndexingRun
    from laueportal.reconstruction.models import ReconstructionRun


class Job(Base):
    __tablename__ = "job"
    # Named ck_job_counters_non_negative and ck_job_counters_bounded (core.db.NAMING_CONVENTION).
    __table_args__ = (
        CheckConstraint(
            "n_inputs >= 0 AND n_succeeded >= 0 AND n_failed >= 0 AND n_not_run >= 0",
            name="counters_non_negative",
        ),
        CheckConstraint("n_succeeded + n_failed + n_not_run <= n_inputs", name="counters_bounded"),
    )

    job_id: Mapped[int] = mapped_column(primary_key=True)

    computer_name: Mapped[str] = mapped_column(String)
    status: Mapped[int] = mapped_column(Integer)  # Queued, Running, Finished, Failed, Cancelled
    priority: Mapped[int] = mapped_column(Integer)

    submit_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finish_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # Human-readable log; the last line is the terminal message of a finished run.
    messages: Mapped[str | None] = mapped_column(String, nullable=True)

    # Execution state (values in laueportal.jobs.state.RunPhase).
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    queue_job_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Remote execution (revision 0002) is optional; NULL means the local backend (the worker).
    backend: Mapped[str | None] = mapped_column(String, nullable=True)
    submission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    external_state: Mapped[str | None] = mapped_column(String, nullable=True)
    external_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Revision 0008: v1 cb1159c's column, with its name, type and place. A crux run's latest
    # remote observation (``remote.observation``: phase, detail, PBS ID, stale, times), as the remote poller accepted
    # it from DM's record; NULL until one is (always for an aps or local run).
    remote_observation: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)

    # Run counters. Processed work is n_succeeded + n_failed; n_not_run is work
    # that was never attempted (cancelled, interrupted, or skipped) and is fixed
    # at finalization so the three always sum to n_inputs for a finished run.
    n_inputs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    n_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    n_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    n_not_run: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Run-level artifact references (absolute paths).
    run_directory: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_path: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    request_path: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_report_path: Mapped[str | None] = mapped_column(String, nullable=True)

    # Revision 0007, for the worker that replaced RQ: when the run was moved to the
    # front of the queue (Move to Front, a Queued local run; ``jobs.controls``), and the job process the worker's
    # dispatcher started (``jobs.dispatch``): its host, pid and start in seconds since boot (a clock step does not
    # move it).
    moved_to_front_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    process_host: Mapped[str | None] = mapped_column(String, nullable=True)
    process_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_since_boot: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Parent of:
    reconstruction_run: Mapped["ReconstructionRun | None"] = relationship(back_populates="job", uselist=False)
    indexing_run: Mapped["IndexingRun | None"] = relationship(back_populates="job", uselist=False)


class IdempotencyKey(Base):
    """A create endpoint's ``Idempotency-Key`` and its kept answer (``jobs.idempotency``;
    revision 0007): one row per key and route, claimed ``pending`` before anything is checked or created, then
    ``kept`` with the created run's answer. A row past ``expires_at`` counts as absent: a claim replaces it, and the
    reconciler's cycle deletes it."""

    __tablename__ = "idempotency_key"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    """``<route>:<SHA-256 of the key>`` (the route: the create endpoint's operation ID)."""
    fingerprint: Mapped[str] = mapped_column(String)
    """The SHA-256 of the request body's JSON, its members sorted (``jobs.idempotency.fingerprint``)."""
    state: Mapped[str] = mapped_column(String)
    """``pending`` (claimed, not answered) or ``kept`` (``answer`` holds the created run's answer)."""
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    """The 201's ``CreatedRun`` as JSON; null while pending."""
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    """24 h after the claim, and again after the answer was kept (``jobs.idempotency.TTL_SECONDS``)."""


WORKER_ROW = "worker"
"""The worker row's name: one worker per database (``jobs.worker`` writes it, ``system.status`` reads it)."""


class Worker(Base):
    """The worker process that replaced RQ's (revision 0007): its host and pid,
    when it started and stopped, and each of its threads' last cycle (dispatcher, reconciler, remote poller), for
    the status page. One row, ``worker``, replaced by each worker at its start (``jobs.worker``)."""

    __tablename__ = "worker"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    host: Mapped[str] = mapped_column(String)
    pid: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    dispatcher_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reconciler_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    poller_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# The relationships above name other domains' models by string. Importing their modules here, at the
# end (the domain model modules import each other in a cycle), means that importing any one domain's
# models registers every mapper, so the first ORM use in any process can configure them.
# Nothing has to import laueportal.models first.
import laueportal.indexing.models  # noqa: E402, F401
import laueportal.reconstruction.models  # noqa: E402, F401
