"""Tests for whole-run queue admission (the job table is the queue), priority and cancellation. The reconciler's rules
are ``test_reconciler.py``'s."""

from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.core.settings import ExecutionSettings
from laueportal.jobs import controls, executor, lifecycle
from laueportal.jobs import queue as core
from laueportal.jobs import state as execution
from laueportal.jobs.schemas import CancelResult, CancelState, MoveResult, MoveState
from laueportal.jobs.state import ExecutionStateError, JobStatus, RunPhase
from tests.run_support import dispatch_queue, load_job, make_engine, publish_wire_run


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


def _enqueue_run(job_id, *, engine, at_front=False):
    return core.enqueue_run(job_id, engine=engine, at_front=at_front)


def test_the_batch_coordinator_machinery_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("laueportal.jobs.batch")
    for name in ("execute_indexing_chunk", "execute_reconstruction_subjob"):
        assert not hasattr(executor, name)
    for name in (
        "notify_subjobs_completed",
        "setup_batch_counter",
        "publish_job_update",
        "execute_with_status_updates",
    ):
        assert not hasattr(lifecycle, name)
    assert not hasattr(core, "PEAKINDEXING_QUEUE_BATCH_SIZE")
    assert not hasattr(core, "_chunked")
    assert core.run_queue_id(12) == "run_12"
    for name in (
        "redis_connection",
        "run_queue",
        "QueueUnavailableError",
        "CONTROL_TIMEOUT_SECONDS",
        "STATUS_MAPPING",
        "STATUS_REVERSE_MAPPING",
    ):
        assert not hasattr(core, name)  # no Redis or RQ


def test_run_policy_validates_its_settings():
    policy = core.RunPolicy.from_settings(ExecutionSettings(job_timeout_seconds=3600, workers=2))
    assert (policy.job_timeout_seconds, policy.workers, policy.heartbeat_seconds) == (3600, 2, 15.0)
    assert policy.reconstruction_workers == 1  # several points at once must be chosen explicitly
    assert core.RunPolicy.from_settings(ExecutionSettings(reconstruction_workers=3)).reconstruction_workers == 3
    with pytest.raises(ValueError, match="reconstruction_workers"):
        core.RunPolicy(reconstruction_workers=0)
    with pytest.raises(ValidationError, match="chunk_size"):  # unknown execution settings are rejected
        ExecutionSettings(chunk_size=50)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="job_timeout_seconds"):
        core.RunPolicy(job_timeout_seconds=0)
    with pytest.raises(ValueError, match="stale_heartbeat_seconds"):
        core.RunPolicy(heartbeat_seconds=30, stale_heartbeat_seconds=20)
    with pytest.raises(ValueError, match="max_in_flight"):
        core.RunPolicy(workers=4, max_in_flight=2)


def test_enqueue_run_is_deterministic_and_refuses_duplicates(engine, tmp_path):
    """The committed row is the queue entry: phase ``queued`` and the ``run_<id>`` queue ID, which nothing reads; the
    worker's dispatcher takes it from the table."""
    run = publish_wire_run(engine, tmp_path)

    queue_id = core.enqueue_reconstruction(run.id, engine=engine)

    assert queue_id == f"run_{run.job_id}"
    job = load_job(engine, run.job_id)
    assert (job.phase, job.status, job.queue_job_id) == (RunPhase.QUEUED, JobStatus.QUEUED, queue_id)
    assert (job.moved_to_front_at, dispatch_queue(engine)) == (None, [("run", run.job_id)])

    with pytest.raises(ExecutionStateError, match="only published runs"):
        _enqueue_run(run.job_id, engine=engine)
    assert dispatch_queue(engine) == [("run", run.job_id)]

    with pytest.raises(ValueError, match="Reconstruction R999 does not exist"):
        core.enqueue_reconstruction(999, engine=engine)
    with pytest.raises(ValueError, match="Indexing I999 does not exist"):
        core.enqueue_indexing(999, engine=engine)


def test_enqueue_priority_places_the_run_at_the_front(engine, tmp_path):
    first, second = publish_wire_run(engine, tmp_path, name="first"), publish_wire_run(engine, tmp_path, name="second")
    _enqueue_run(first.job_id, engine=engine)
    _enqueue_run(second.job_id, at_front=True, engine=engine)
    assert load_job(engine, second.job_id).moved_to_front_at is not None
    assert dispatch_queue(engine) == [("run", second.job_id), ("run", first.job_id)]


def test_enqueue_refuses_a_tampered_manifest(engine, tmp_path):
    run = publish_wire_run(engine, tmp_path)
    with open(run.job.manifest_path, "a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(Exception, match="digest"):
        _enqueue_run(run.job_id, engine=engine)
    assert dispatch_queue(engine) == []
    assert load_job(engine, run.job_id).phase == RunPhase.PUBLISHED


def test_cancel_a_queued_run_finalizes_it_and_takes_it_off_the_queue(engine, tmp_path):
    """One transaction: the row is its queue entry, so nothing else is taken off a queue."""
    run = publish_wire_run(engine, tmp_path)
    _enqueue_run(run.job_id, engine=engine)

    result = controls.cancel_run(run.job_id, engine=engine)

    assert result == CancelResult(job_id=run.job_id, state=CancelState.CANCELLED, n_pending=3)
    assert dispatch_queue(engine) == []
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.CANCELLED, RunPhase.CANCELLED)
    assert (job.n_succeeded, job.n_failed, job.n_not_run) == (0, 0, 3)
    assert job.cancel_requested_at is not None
    assert "Cancellation requested: user" in job.messages
    assert job.finish_time is not None

    again = controls.cancel_run(run.job_id, engine=engine)
    assert again == CancelResult(job_id=run.job_id, state=CancelState.ALREADY_TERMINAL, n_pending=0)
    assert controls.cancel_run(999, engine=engine).state == CancelState.NOT_FOUND
    assert not hasattr(controls, "cancel_batch_job")


def test_cancel_a_published_but_never_queued_run(engine, tmp_path):
    run = publish_wire_run(engine, tmp_path)
    result = controls.cancel_run(run.job_id, engine=engine)
    assert result == CancelResult(job_id=run.job_id, state=CancelState.CANCELLED, n_pending=3)
    assert load_job(engine, run.job_id).status == JobStatus.CANCELLED


def test_cancel_a_running_run_only_records_the_request(engine, tmp_path):
    run = publish_wire_run(engine, tmp_path)
    _enqueue_run(run.job_id, engine=engine)
    with Session(engine) as session, session.begin():
        job = session.get(models.Job, run.job_id)
        execution.mark_running(job)
        execution.record_progress(job, succeeded=1, failed=0)

    result = controls.cancel_run(run.job_id, engine=engine)

    assert result == CancelResult(job_id=run.job_id, state=CancelState.REQUESTED, n_pending=2)
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.RUNNING, RunPhase.RUNNING)  # the executor finalizes
    assert job.cancel_requested_at is not None
    first_request = job.cancel_requested_at
    controls.cancel_run(run.job_id, engine=engine)
    assert load_job(engine, run.job_id).cancel_requested_at == first_request


def test_move_run_to_front_moves_only_queued_runs(engine, tmp_path):
    """``moved_to_front_at`` on a Queued local run: the dispatcher takes it first, the newest move first."""
    first = publish_wire_run(engine, tmp_path, name="first")
    second = publish_wire_run(engine, tmp_path, name="second")
    third = publish_wire_run(engine, tmp_path, name="third")
    for run in (first, second, third):
        _enqueue_run(run.job_id, engine=engine)

    result = controls.move_run_to_front(second.job_id, engine=engine)
    assert result == MoveResult(job_id=second.job_id, state=MoveState.MOVED, phase="queued")
    assert controls.move_run_to_front(third.job_id, engine=engine).state == MoveState.MOVED
    assert dispatch_queue(engine) == [("run", run.job_id) for run in (third, second, first)]

    with Session(engine) as session, session.begin():
        execution.mark_running(session.get(models.Job, first.job_id))
    running = controls.move_run_to_front(first.job_id, engine=engine)
    assert running == MoveResult(job_id=first.job_id, state=MoveState.NOT_QUEUED, phase="running")
    assert load_job(engine, first.job_id).moved_to_front_at is None  # nothing changes unless moved
    published = publish_wire_run(engine, tmp_path, name="published")
    not_enqueued = controls.move_run_to_front(published.job_id, engine=engine)
    assert not_enqueued == MoveResult(job_id=published.job_id, state=MoveState.NOT_QUEUED, phase="published")
    assert controls.move_run_to_front(999, engine=engine) == MoveResult(job_id=999, state=MoveState.NOT_FOUND)
    assert not hasattr(controls, "move_batch_to_front")
