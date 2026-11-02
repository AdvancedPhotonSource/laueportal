"""A seeded database for the run and job grid lists, and helpers for their tests.

``seed_run_lists`` adds three scans (101 with a catalog, aperture ``wire``; 102 with one, ``mask``; 103 without
one), four wire reconstructions, three LaueGo indexing runs, a LaueMatching run (left out of the indexations
grid), a bare job without a run, and a LaueGo run without its parameters row (cancelled long ago). Every job moves
through ``jobs.state``'s transitions at a fixed time, ``T0`` minus some minutes (``at``), so its ``updated_at``,
start and finish are known; two jobs run remotely (recorded as submitted to DM, with DM's state; the queued
``crux`` one with a remote observation, phase ``uplink``). Job and run IDs
follow the order of ``SEEDED`` (job 1 is ``r_finished``, ..., job 10 is ``i_no_parameters``, I5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.core.errors import PROBLEM_JSON
from laueportal.indexing.models import IndexingRun
from laueportal.jobs import state
from laueportal.jobs.models import Job
from laueportal.jobs.queue import run_queue_id
from laueportal.jobs.records import new_job
from laueportal.jobs.state import JobStatus
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.remote.submission import queue_identity
from tests.conftest import (
    create_test_catalog,
    create_test_lauego_parameters,
    create_test_metadata,
    create_test_wire_reconstruction_parameters,
)

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

SEEDED = (
    "r_finished",
    "r_running",
    "r_queued",
    "r_cancelled",
    "i_finished",
    "i_failed",
    "i_queued",
    "i_matching",
    "bare",
    "i_no_parameters",
)
"""The seeded jobs in creation order: ``r_`` reconstructions, ``i_`` indexing runs, ``bare`` a job without a run."""

LIFECYCLE = ("queued", "running", "finished", "failed", "cancelled")
"""The status order, by code (0 to 4): every run list sorts ``status`` so, and ``-status`` the reverse."""

BAD_CHANGED_SINCE = [
    ("0001-01-01T00:00:00Z", "value_error"),  # datetime.min: the 2 s overlap before it is no time
    ("0001-01-01T00:00:01.999999Z", "value_error"),
    ("0001-01-01T00:30:00+01:00", "value_error"),  # before year 1 in UTC
    ("9999-12-31T23:59:59-01:00", "value_error"),  # after year 9999 in UTC
    ("2026-10-18T12:00:00", "value_error"),  # no offset: not a server time
    ("yesterday", "datetime_from_date_parsing"),
    ("2026-02-30T00:00:00Z", "datetime_from_date_parsing"),
]
"""``changed_since`` values every live list refuses with a 422 (never a 500), and the error code."""


def at(minutes_before_t0: float) -> datetime:
    return T0 - timedelta(minutes=minutes_before_t0)


@dataclass(frozen=True)
class Seeded:
    job_id: int
    run_id: int | None
    """The reconstruction or indexing run's ID; None for the bare job."""


def _job(session: Session, *, submitted: datetime, n_inputs: int, backend: str = "local") -> Job:
    job = new_job(computer_name="test-host", priority=0, submitted_at=submitted, n_inputs=n_inputs, backend=backend)
    session.add(job)
    session.flush()
    return job


def _queued(
    session: Session, *, submitted: datetime, n_inputs: int, backend: str = "local", dm_state: str | None = None
) -> Job:
    """A published run handed to the queue; a remote one recorded as submitted to DM (as ``submit_run`` does)."""
    job = _job(session, submitted=submitted, n_inputs=n_inputs, backend=backend)
    directory = f"/runs/job_{job.job_id}"
    state.mark_published(
        job,
        run_directory=directory,
        manifest_path=f"{directory}/inputs.jsonl",
        manifest_digest="digest",
        request_path=f"{directory}/request.json",
        n_inputs=n_inputs,
        now=submitted,
    )
    if backend == "local":
        state.mark_queued(job, run_queue_id(job.job_id), now=submitted)
    else:
        job.submission_id = str(uuid.UUID(int=job.job_id))
        job.external_state = dm_state
        state.mark_queued(job, queue_identity(job.submission_id), now=submitted)
    return job


def _ran(
    job: Job,
    *,
    started: datetime,
    succeeded: int,
    failed: int = 0,
    progress_at: datetime | None = None,
    end: JobStatus | None = None,
    finished: datetime | None = None,
    message: str | None = None,
) -> Job:
    state.mark_running(job, now=started)
    if succeeded or failed:
        state.record_progress(job, succeeded=succeeded, failed=failed, now=progress_at or started)
    if end is not None:
        state.finalize(job, status=end, message=message, now=finished)
    return job


def _reconstruction(
    session: Session, job: Job, *, scan: int | None, author: str | None, notes: str | None, parameters: bool = True
) -> Seeded:
    run = ReconstructionRun(
        job_id=job.job_id,
        scan_number=scan,
        method="wire",
        input_path="/data/in",
        author=author,
        notes=notes,
        created_at=job.submit_time,
    )
    session.add(run)
    session.flush()
    if parameters:
        session.add(create_test_wire_reconstruction_parameters(run.id))
    return Seeded(job.job_id, run.id)


def _indexing(
    session: Session,
    job: Job,
    *,
    scan: int | None,
    author: str | None,
    notes: str | None = None,
    parent: Seeded | None = None,
    method: str = "lauego",
    parameters: dict[str, Any] | None = None,
) -> Seeded:
    run = IndexingRun(
        job_id=job.job_id,
        scan_number=scan,
        reconstruction_id=None if parent is None else parent.run_id,
        method=method,
        input_path="/data/in",
        author=author,
        notes=notes,
        created_at=job.submit_time,
    )
    session.add(run)
    session.flush()
    if parameters is not None:
        values = create_test_lauego_parameters(run.id)
        for key, value in parameters.items():
            setattr(values, key, value)
        session.add(values)
    return Seeded(job.job_id, run.id)


def seed_run_lists(engine: Engine) -> dict[str, Seeded]:
    """The scans, runs and jobs of the module docstring; returns each seeded job's and run's IDs by name."""
    seeded: dict[str, Seeded] = {}
    with Session(engine) as session, session.begin():
        for scan_number, aperture, sample in ((101, "wire", "Si"), (102, "mask", "Ni"), (103, None, None)):
            session.add(create_test_metadata(scan_number))
            session.flush()
            if aperture is not None:
                catalog = create_test_catalog(scan_number)
                catalog.aperture, catalog.sample_name = aperture, sample
                session.add(catalog)

        job = _ran(
            _queued(session, submitted=at(70), n_inputs=10),
            started=at(60),
            succeeded=10,
            progress_at=at(31),
            end=JobStatus.FINISHED,
            finished=at(30),
            message="Finished: 10 succeeded\n",
        )
        seeded["r_finished"] = _reconstruction(session, job, scan=101, author="alice", notes="first pass")

        job = _ran(
            _queued(session, submitted=at(8), n_inputs=10), started=at(5), succeeded=3, failed=1, progress_at=at(1)
        )
        seeded["r_running"] = _reconstruction(session, job, scan=101, author="bob", notes=None)

        job = _queued(session, submitted=at(1), n_inputs=4, backend="crux", dm_state="pending")
        job.remote_observation = {  # where the crux run is (remote.observation)
            "phase": "uplink",
            "detail": "Transferring inputs",
            "observed_at": at(1).isoformat(timespec="seconds"),
            "submission_id": job.submission_id,
        }
        seeded["r_queued"] = _reconstruction(session, job, scan=None, author=None, notes=None)

        job = _queued(session, submitted=at(45), n_inputs=6)
        state.request_cancellation(job, reason="user", now=at(40))
        state.finalize(job, status=JobStatus.CANCELLED, message="Cancelled before the run started", now=at(40))
        seeded["r_cancelled"] = _reconstruction(
            session, job, scan=102, author="carol", notes="stopped early", parameters=False
        )

        job = _ran(
            _queued(session, submitted=at(25), n_inputs=2),
            started=at(20),
            succeeded=2,
            end=JobStatus.FINISHED,
            finished=at(10),
            message="Finished: 2 succeeded",
        )
        seeded["i_finished"] = _indexing(
            session, job, scan=101, author="alice", parent=seeded["r_finished"], parameters={}
        )

        job = _ran(
            _queued(session, submitted=at(55), n_inputs=3, backend="aps", dm_state="failed"),
            started=at(50),
            succeeded=1,
            failed=1,
            end=JobStatus.FAILED,
            finished=at(45),
            message="DM job failed\nPBS exit status 1",
        )
        seeded["i_failed"] = _indexing(
            session,
            job,
            scan=102,
            author="bob",
            notes="bad geometry",
            parameters={"box_size": 12, "depth_range": "-5 5", "depth_range_len": 5, "crystal_file": "/x/Si.xtal"},
        )

        job = _queued(session, submitted=at(2), n_inputs=5)
        seeded["i_queued"] = _indexing(session, job, scan=None, author="dave", parameters={"scan_points_len": 5})

        job = _ran(
            _queued(session, submitted=at(17), n_inputs=1),
            started=at(16),
            succeeded=1,
            end=JobStatus.FINISHED,
            finished=at(15),
        )
        seeded["i_matching"] = _indexing(session, job, scan=103, author="erin", method="laue_matching")

        seeded["bare"] = Seeded(_job(session, submitted=at(0.5), n_inputs=0).job_id, None)

        job = _queued(session, submitted=at(100), n_inputs=4)
        state.request_cancellation(job, reason="user", now=at(95))
        state.finalize(job, status=JobStatus.CANCELLED, message="Cancelled before the run started", now=at(95))
        seeded["i_no_parameters"] = _indexing(session, job, scan=103, author="frank")  # no parameters row
    assert list(seeded) == list(SEEDED)
    return seeded


# ---------------------------------------------------------------------------
# Test helpers


def get_rows(client: TestClient, path: str, **params: Any) -> list[dict[str, Any]]:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()["rows"]


def problem_items(response: Any, status: int = 422, title: str = "Validation failed") -> list[tuple[list[Any], str]]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert (body["status"], body["title"]) == (status, title)
    return [(item["loc"], item["code"]) for item in body["errors"]]


def in_lifecycle_order(rows: list[dict[str, Any]], *, descending: bool = False) -> bool:
    """Whether the rows' statuses follow ``LIFECYCLE`` (reversed when ``descending``)."""
    ranks = [LIFECYCLE.index(row["status"]) for row in rows]
    return ranks == sorted(ranks, reverse=descending)


def parse_z(text: str) -> datetime:
    """An API timestamp (``...Z``) as an aware datetime."""
    assert text.endswith("Z"), text
    return datetime.fromisoformat(text)
