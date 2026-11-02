"""Remote jobs in the job system: the execution target on requests and jobs, enqueueing that hands remote jobs to DM
submission (never to the worker's dispatcher), stop and move-to-front, and the worker's startup reconciliation.

DM submission itself is ``tests/remote/``'s; here ``remote.submission.submit_run`` is replaced where only the
dispatch matters. No status value or phase transition is new: a remote job uses the existing ones.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from laueportal.core.timeutil import utcnow
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.jobs import controls, reconciler, records, state
from laueportal.jobs import queue as core
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import read_request
from laueportal.jobs.schemas import CancelResult, CancelState, MoveResult, MoveState
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.reconstruction.wire.params import WireParamsV1
from laueportal.remote import submission
from tests.remote_support import Layout, create_lauego_run, create_wire_run
from tests.run_support import dispatch_queue, load_job, make_engine, publish_wire_run

SUBMISSION = "6f1c2a4e-3b5d-4e7f-8a9b-0c1d2e3f4a5b"


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


def _set(engine, job_id, **values):
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        for key, value in values.items():
            setattr(job, key, value)


def _submitted(engine, tmp_path, backend="crux", *, running=False):
    """A remote run as ``submit_run`` leaves it (queued with its submission identity), optionally running."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    run = publish_wire_run(engine, tmp_path, count=2)
    with Session(engine) as session, session.begin():
        job = session.get(Job, run.job_id)
        assert job is not None
        job.backend, job.submission_id, job.external_state = backend, SUBMISSION, "pending"
        state.mark_queued(job, submission.queue_identity(SUBMISSION))
        if running:
            state.mark_running(job)
    return run


# --- the execution target --------------------------------------------------------------------------------


def test_requests_run_locally_unless_told_otherwise():
    for request_type in (LaueGoParamsV1, WireParamsV1):
        assert request_type.model_fields["backend"].default == "local"


@pytest.mark.parametrize("backend", ["local", "aps", "crux"])
def test_the_target_is_written_to_request_json_and_the_job(engine, tmp_path, backend):
    from laueportal.reconstruction import service

    inputs = tmp_path / "wire-input"
    inputs.mkdir()
    for index in (1, 2):
        (inputs / f"wire_{index}.h5").write_text("")
    run = publish_wire_run(engine, tmp_path, count=2, name="base")  # also creates the scan's metadata
    request = WireParamsV1(
        scan_number=1,
        input_path=os.fspath(inputs),
        output_path_template=os.fspath(tmp_path / "target-run_%d"),
        filename_prefixes=["wire_%d.h5"],
        geometry_file="/config/geometry.xml",
        percent_brightest=5.0,
        wire_edges="leading",
        depth_start=-10.0,
        depth_end=10.0,
        depth_resolution=0.5,
        num_threads=1,
        memory_limit_mb=256,
        scan_points="1-2",
        verbose=0,
        backend=backend,
    )
    created = service.create_reconstruction(request, engine=engine)
    assert read_request(created.job.request_path)["request"]["backend"] == backend
    assert load_job(engine, created.job_id).backend == backend
    assert read_request(run.job.request_path)["request"]["backend"] == "local"


def test_new_job_refuses_an_unknown_target():
    with pytest.raises(ValueError, match="Unknown execution backend: polaris"):
        records.new_job(computer_name="host", priority=0, submitted_at=utcnow(), n_inputs=1, backend="polaris")
    assert records.new_job(computer_name="host", priority=0, submitted_at=utcnow(), n_inputs=1).backend == "local"


# --- enqueueing ------------------------------------------------------------------------------------------


def test_enqueue_hands_a_remote_job_to_dm_submission_and_never_to_the_worker(engine, tmp_path, monkeypatch):
    run = publish_wire_run(engine, tmp_path, count=2)
    _set(engine, run.job_id, backend="aps")
    calls = []
    monkeypatch.setattr(submission, "submit_run", lambda job_id, **kwargs: calls.append((job_id, kwargs)) or "dm-7")
    remote, client = object(), object()

    result = core.enqueue_run(run.job_id, engine=engine, remote=remote, dm_client=client)  # type: ignore[arg-type]

    assert result == "dm-7" and dispatch_queue(engine) == []
    assert calls == [(run.job_id, {"engine": engine, "settings": remote, "client": client})]
    with pytest.raises(ValueError, match="cannot be moved ahead of scheduler jobs"):
        core.enqueue_run(run.job_id, engine=engine, at_front=True)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["indexing", "reconstruction"])
def test_the_run_id_wrappers_hand_remote_runs_to_dm_submission(engine, tmp_path, monkeypatch, kind):
    """The pages submit by run ID (``enqueue_indexing``, ``enqueue_reconstruction``): a remote run goes to DM submission
    with the settings and client given, never to the worker, and never at the front."""
    layout = Layout(tmp_path)
    if kind == "indexing":
        run, enqueue = create_lauego_run(engine, layout, count=1, backend="crux"), core.enqueue_indexing
    else:
        run, enqueue = create_wire_run(engine, layout, count=2, backend="aps"), core.enqueue_reconstruction
    calls = []
    monkeypatch.setattr(submission, "submit_run", lambda job_id, **kwargs: calls.append((job_id, kwargs)) or "dm-9")
    remote, client = object(), object()

    result = enqueue(run.id, engine=engine, remote=remote, dm_client=client)  # type: ignore[arg-type]

    assert result == "dm-9" and dispatch_queue(engine) == []
    assert calls == [(run.job_id, {"engine": engine, "settings": remote, "client": client})]
    with pytest.raises(ValueError, match="cannot be moved ahead of scheduler jobs"):
        enqueue(run.id, engine=engine, at_front=True, remote=remote)  # type: ignore[arg-type]
    assert len(calls) == 1 and dispatch_queue(engine) == []


def test_a_remote_job_is_refused_while_remote_execution_is_disabled(engine, tmp_path):
    run = publish_wire_run(engine, tmp_path, count=2)
    _set(engine, run.job_id, backend="crux")
    with pytest.raises(ValueError, match="Remote execution is not enabled"):
        core.enqueue_run(run.job_id, engine=engine, remote=None)
    job = load_job(engine, run.job_id)
    assert (job.phase, job.submission_id, dispatch_queue(engine)) == (RunPhase.PUBLISHED, None, [])
    assert not (Path(run.output_path) / "bdp.json").exists()


@pytest.mark.parametrize("backend", ["local", None])
def test_local_and_older_jobs_use_the_queue(engine, tmp_path, backend):
    """``None``: a job created before v1 3ad1557 added the column."""
    run = publish_wire_run(engine, tmp_path, count=2)
    _set(engine, run.job_id, backend=backend)
    assert core.enqueue_run(run.job_id, engine=engine) == f"run_{run.job_id}"
    assert dispatch_queue(engine) == [("run", run.job_id)]


# --- controls ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("running", [False, True])
def test_stopping_a_submitted_remote_run_records_the_request_for_the_poller(engine, tmp_path, running):
    run = _submitted(engine, tmp_path, running=running)
    result = controls.cancel_run(run.job_id, engine=engine)
    assert result == CancelResult(job_id=run.job_id, state=CancelState.REQUESTED, n_pending=2)
    job = load_job(engine, run.job_id)
    assert job.cancel_requested_at is not None and not state.is_terminal(job)
    assert (job.status, job.phase) == (
        (JobStatus.RUNNING, RunPhase.RUNNING) if running else (JobStatus.QUEUED, RunPhase.QUEUED)
    )


def test_a_remote_run_never_submitted_is_cancelled_at_once(engine, tmp_path):
    run = publish_wire_run(engine, tmp_path, count=2)
    _set(engine, run.job_id, backend="crux")
    result = controls.cancel_run(run.job_id, engine=engine)
    assert (result.state, result.n_pending) == (CancelState.CANCELLED, 2)
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.n_not_run) == (JobStatus.CANCELLED, RunPhase.CANCELLED, 2)


def test_move_to_front_refuses_remote_runs(engine, tmp_path):
    queued = _submitted(engine, tmp_path / "queued")
    assert controls.move_run_to_front(queued.job_id, engine=engine) == MoveResult(
        job_id=queued.job_id, state=MoveState.REMOTE, phase=RunPhase.QUEUED
    )
    running = _submitted(engine, tmp_path / "running", running=True)
    result = controls.move_run_to_front(running.job_id, engine=engine)
    assert result.state == MoveState.NOT_QUEUED


# --- the worker's startup reconciliation -------------------------------------------------------------


def test_startup_reconciliation_leaves_remote_runs_to_the_poller(engine, tmp_path):
    running = _submitted(engine, tmp_path / "running", running=True)
    queued = _submitted(engine, tmp_path / "queued", backend="aps")
    stale = utcnow() - timedelta(hours=1)
    _set(engine, running.job_id, heartbeat_at=stale, updated_at=stale)
    reconciled = reconciler.reconcile_runs(engine, stale_after_seconds=1, creation_timeout_seconds=1)
    assert not reconciled
    assert load_job(engine, running.job_id).status == JobStatus.RUNNING
    assert load_job(engine, queued.job_id).phase == RunPhase.QUEUED
