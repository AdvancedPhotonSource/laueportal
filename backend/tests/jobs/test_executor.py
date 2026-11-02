"""Tests for the whole-run executor: progress, cancellation, errors, timeout, finalization."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime

import psutil
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.core.timestamps import local_timestamp
from laueportal.jobs import contract, lifecycle
from laueportal.jobs import executor as executors
from laueportal.jobs import state as execution
from laueportal.jobs.contract import RunOutcome
from laueportal.jobs.rundir import FailureRecord, read_failure_report
from laueportal.jobs.state import JobStatus, RunPhase
from tests.run_support import FAST_POLICY, claimed, enqueue_with, load_job, make_engine, publish_wire_run
from tests.settings_support import fresh_interpreter_env, make_database

WIRE_KIND = "wire_reconstruction"


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


@pytest.fixture
def compute(monkeypatch):
    """Register a compute function for the wire kind for one test."""

    registered = {}

    def install(function):
        monkeypatch.setitem(contract._REGISTRY, WIRE_KIND, function)
        registered["function"] = function
        return function

    return install


def _claimed_run(engine, tmp_path, monkeypatch, **kwargs):
    """A run the worker's dispatcher queued and claimed (Running), for ``execute_run`` in its job process."""
    run = publish_wire_run(engine, tmp_path, **kwargs)
    claimed(engine, run.job_id)
    return run


def _run_summary(run):
    with open(os.path.join(run.output_path, "run.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _per_entry(successes: set[int] | None = None, fail=None, stop_after=None, spawn=None, artifacts=None):
    """Build a compute function that succeeds for every entry except the listed indices."""

    def function(request, entries, hooks):
        succeeded = failed = 0
        stopped = False
        for entry in entries:
            if stopped or hooks.should_stop():
                stopped = True
                hooks.record_failure(FailureRecord.not_run(entry, category="cancelled", message="run cancelled"))
                continue
            if spawn is not None:
                spawn(entry)
            if fail is not None and entry.index in fail:
                failed += 1
                hooks.record_failure(FailureRecord.from_error(entry, ValueError(f"bad frame {entry.input_id}")))
            else:
                succeeded += 1
            hooks.report_progress(succeeded=succeeded, failed=failed)
            if stop_after is not None:
                stop_after(entry)
        return RunOutcome(
            n_succeeded=succeeded,
            n_failed=failed,
            stopped=stopped,
            artifacts=dict(artifacts or {}),
            provenance={"engine": "test"},
            summary="test compute",
        )

    return function


def test_successful_run_finishes_with_summary_and_counters(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch)
    compute(_per_entry(artifacts={"results": os.path.join(run.output_path, "output.h5")}))

    result = executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)

    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FINISHED, RunPhase.FINISHED)
    assert (job.n_inputs, job.n_succeeded, job.n_failed, job.n_not_run) == (3, 3, 0, 0)
    assert job.start_time is not None and job.finish_time is not None
    assert job.heartbeat_at is not None
    assert job.failure_report_path is None
    assert job.messages.startswith("Finished: 3 succeeded, 0 failed, 0 not run of 3")
    assert result == {
        "job_id": run.job_id,
        "status": "Finished",
        "message": job.messages,
        "n_succeeded": 3,
        "n_failed": 0,
    }
    summary = _run_summary(run)
    assert summary["format"] == "laue-portal-run-summary"
    assert summary["status"] == "Finished"
    assert summary["counters"] == {"n_inputs": 3, "n_succeeded": 3, "n_failed": 0, "n_not_run": 0, "n_indexed": None}
    assert summary["artifacts"] == {"results": os.path.join(run.output_path, "output.h5")}
    assert summary["provenance"] == {"engine": "test"}
    assert summary["policy"]["job_timeout_seconds"] == FAST_POLICY.job_timeout_seconds
    # the run directory's text: naive local time to the second; the database keeps aware UTC.
    assert summary["finished_at"] == local_timestamp(job.finish_time)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", summary["started_at"])
    assert local_timestamp(job.start_time) <= summary["started_at"] <= summary["finished_at"]
    assert not os.path.exists(os.path.join(run.output_path, "failures.jsonl"))
    assert sorted(os.listdir(run.output_path)) == ["inputs.jsonl", "request.json", "run.json"]


def test_input_failures_continue_and_finish_as_incomplete(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch)
    compute(_per_entry(fail={1}))

    executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)

    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert (job.n_succeeded, job.n_failed, job.n_not_run) == (2, 1, 0)
    assert job.messages.startswith("Incomplete: 2 succeeded, 1 failed, 0 not run of 3")
    assert job.failure_report_path == os.path.join(run.output_path, "failures.jsonl")
    records, _ = read_failure_report(job.failure_report_path)
    assert [(r.index, r.input_id, r.category, r.error_type) for r in records] == [(1, "wire_2", "input", "ValueError")]
    assert _run_summary(run)["failure_report"] == "failures.jsonl"


def test_all_inputs_failing_never_claims_success(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch)
    compute(_per_entry(fail={0, 1, 2}))
    executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)
    job = load_job(engine, run.job_id)
    assert job.status == JobStatus.FAILED
    assert job.messages.startswith("All inputs failed; 0 succeeded, 3 failed")
    assert len(read_failure_report(job.failure_report_path)[0]) == 3


def test_validation_failure_prevents_a_finished_state(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch)

    def function(request, entries, hooks):
        entries = list(entries)
        hooks.report_progress(succeeded=len(entries), failed=0)
        return RunOutcome(n_succeeded=len(entries), n_failed=0, validation_error="frame offsets do not partition")

    compute(function)
    executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)
    job = load_job(engine, run.job_id)
    assert (job.status, job.n_succeeded) == (JobStatus.FAILED, 3)
    assert job.messages.startswith("Authoritative output failed validation: frame offsets do not partition")
    assert _run_summary(run)["validation_error"] == "frame offsets do not partition"


def test_compute_exception_fails_the_run_and_propagates(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch)

    def function(request, entries, hooks):
        hooks.report_progress(succeeded=1, failed=0)
        raise RuntimeError("worker pool broke")

    compute(function)
    with pytest.raises(RuntimeError, match="worker pool broke"):
        executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert (job.n_succeeded, job.n_failed, job.n_not_run) == (1, 0, 2)
    assert job.messages.startswith("Run failed: RuntimeError: worker pool broke; 1 succeeded")
    assert _run_summary(run)["status"] == "Failed"


def test_timeout_is_an_explicit_policy_failure(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch)

    def function(request, entries, hooks):
        hooks.report_progress(succeeded=2, failed=0)
        raise lifecycle.RunTimeout("Task exceeded maximum timeout value (600 seconds)")

    compute(function)
    with pytest.raises(lifecycle.RunTimeout):
        executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)
    job = load_job(engine, run.job_id)
    assert job.status == JobStatus.FAILED
    assert job.messages.startswith("Run exceeded the configured limit of 600 s; 2 succeeded, 0 failed, 1 not run")


def test_unknown_kind_and_bad_counts_are_infrastructure_failures(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch)
    compute(lambda request, entries, hooks: RunOutcome(n_succeeded=7, n_failed=0))
    with pytest.raises(executors.RunSetupError, match="reported 7 processed inputs of 3"):
        executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)
    assert load_job(engine, run.job_id).status == JobStatus.FAILED

    other = _claimed_run(engine, tmp_path, monkeypatch, name="other")
    monkeypatch.delitem(contract._REGISTRY, WIRE_KIND, raising=False)
    monkeypatch.setattr(contract, "get_compute_function", executors.get_compute_function)

    def missing(kind):
        raise contract.UnknownRunKind(kind)

    monkeypatch.setattr(executors, "get_compute_function", missing)
    with pytest.raises(contract.UnknownRunKind):
        executors.execute_run(other.job_id, engine=engine, policy=FAST_POLICY)
    assert load_job(engine, other.job_id).messages.startswith("Run failed: UnknownRunKind")


def test_duplicate_delivery_does_not_run_again(engine, tmp_path, monkeypatch, compute):
    """A run that ended is not run again; a stop flag set before the claim is the claim's
    (``test_dispatch.py``)."""
    run = _claimed_run(engine, tmp_path, monkeypatch)
    calls = []
    compute(lambda request, entries, hooks: calls.append(1) or RunOutcome(n_succeeded=3, n_failed=0))

    executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)
    assert executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY) is None
    assert calls == [1]

    with pytest.raises(executors.RunSetupError, match="does not exist"):
        executors.execute_run(999, engine=engine, policy=FAST_POLICY)


def test_runs_the_dispatcher_has_not_claimed_are_skipped(engine, tmp_path, monkeypatch, compute):
    run = publish_wire_run(engine, tmp_path)
    queued = publish_wire_run(engine, tmp_path, name="queued")
    enqueue_with(engine, queued.job_id)
    compute(lambda request, entries, hooks: RunOutcome(n_succeeded=3, n_failed=0))
    assert executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY) is None
    assert executors.execute_run(queued.job_id, engine=engine, policy=FAST_POLICY) is None
    assert load_job(engine, run.job_id).phase == RunPhase.PUBLISHED
    assert load_job(engine, queued.job_id).phase == RunPhase.QUEUED


def _held_file_reads(monkeypatch, *names: str, error: Exception | None = None):
    """Hold ``_start``'s file reads (``verify_manifest``) in the threads called ``names`` until the returned event is
    set (then raise ``error`` there, if given); returns (held, release): ``held[name]`` is set once that thread
    holds."""
    import threading

    verify = executors.verify_manifest
    held = {name: threading.Event() for name in names}
    release = threading.Event()

    def held_verify(*args, **kwargs):
        name = threading.current_thread().name
        if name in held:
            held[name].set()
            assert release.wait(10)
            if error is not None:
                raise error
        return verify(*args, **kwargs)

    monkeypatch.setattr(executors, "verify_manifest", held_verify)
    return held, release


def test_a_job_process_whose_run_ended_while_it_read_its_files_records_no_setup_error(
    engine, tmp_path, monkeypatch, compute
):
    """The job process's reads fail (its mount answered late, with an error) after its run ended (the reconciler
    interrupted it meanwhile): it records nothing on a run that is no longer its own."""
    import threading

    run = _claimed_run(engine, tmp_path, monkeypatch)
    compute(lambda request, entries, hooks: RunOutcome(n_succeeded=3, n_failed=0))
    held, release = _held_file_reads(monkeypatch, "late", error=OSError("Stale file handle"))
    results: list[object] = []
    late = threading.Thread(
        target=lambda: results.append(executors._start(engine, run.job_id, FAST_POLICY)), name="late"
    )
    late.start()
    assert held["late"].wait(10)
    with Session(engine) as session, session.begin():
        execution.finalize(
            session.get(models.Job, run.job_id), status=JobStatus.FAILED, phase=RunPhase.INTERRUPTED, message="gone"
        )
    release.set()
    late.join(10)

    assert results == [(None, f"job {run.job_id} ended while its files were read")]
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)
    assert "Run setup failed" not in (job.messages or "")


def test_a_setup_error_fails_the_claimed_run_before_it_is_raised(engine, tmp_path, monkeypatch, compute):
    """A run whose manifest no longer verifies when its job process starts is Failed (committed), then
    ``RunSetupError`` is raised: the job process exits 1, and the run is never taken again."""
    run = _claimed_run(engine, tmp_path, monkeypatch)
    compute(lambda request, entries, hooks: RunOutcome(n_succeeded=3, n_failed=0))
    with open(load_job(engine, run.job_id).manifest_path, "a", encoding="utf-8") as manifest:
        manifest.write("\n")

    with pytest.raises(executors.RunSetupError):
        executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)

    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert job.messages.startswith("Run setup failed: ")


def test_cooperative_cancellation_keeps_completed_results(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch, count=6)

    def cancel_after_first(entry):
        if entry.index == 0:
            with Session(engine) as session, session.begin():
                execution.request_cancellation(session.get(models.Job, run.job_id), reason="user")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not hooks_seen["hooks"].should_stop():
                time.sleep(0.01)

    hooks_seen = {}
    base = _per_entry(stop_after=cancel_after_first)

    def function(request, entries, hooks):
        hooks_seen["hooks"] = hooks
        return base(request, entries, hooks)

    compute(function)
    result = executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)

    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.CANCELLED, RunPhase.CANCELLED)
    assert (job.n_succeeded, job.n_failed, job.n_not_run) == (1, 0, 5)
    assert job.messages.splitlines()[-1].startswith("Cancelled by request; completed results kept; 1 succeeded")
    assert result["status"] == "Cancelled"
    records, _ = read_failure_report(job.failure_report_path)
    assert [r.category for r in records] == ["cancelled"] * 5
    assert [r.index for r in records] == [1, 2, 3, 4, 5]


def test_shutdown_signal_stops_the_run_as_interrupted(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch, count=4)

    def signal_after_second(entry):
        if entry.index == 1:
            os.kill(os.getpid(), lifecycle.SHUTDOWN_SIGNAL)
            time.sleep(0.05)

    compute(_per_entry(stop_after=signal_after_second))
    executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)

    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)
    assert (job.n_succeeded, job.n_not_run) == (2, 2)
    assert job.messages.startswith("Stopped by service shutdown; completed results kept")
    assert signal.getsignal(lifecycle.SHUTDOWN_SIGNAL) in (signal.SIG_DFL, signal.SIG_IGN, None) or callable(
        signal.getsignal(lifecycle.SHUTDOWN_SIGNAL)
    )


def test_progress_is_persisted_periodically_and_heartbeats_continue(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch, count=4)
    observed = []

    def observe(entry):
        time.sleep(0.12)  # longer than the fast progress and heartbeat intervals
        job = load_job(engine, run.job_id)
        observed.append((job.n_succeeded, job.heartbeat_at))

    compute(_per_entry(stop_after=observe))
    executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)

    counts = [count for count, _ in observed]
    assert counts[0] <= counts[-1] and counts[-1] >= 2  # intermediate progress reached the database
    heartbeats = [beat for _, beat in observed if beat is not None]
    assert len(set(heartbeats)) >= 2  # the heartbeat advanced while the compute was busy
    assert load_job(engine, run.job_id).n_succeeded == 4


def test_progress_writes_are_rate_limited(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch, count=50)
    slow_policy = FAST_POLICY.__class__(**{**FAST_POLICY.__dict__, "progress_seconds": 3600})
    writes = []
    original = execution.record_progress

    def counting_record_progress(job, **kwargs):
        writes.append(kwargs["succeeded"])
        return original(job, **kwargs)

    monkeypatch.setattr(execution, "record_progress", counting_record_progress)
    compute(_per_entry())
    executors.execute_run(run.job_id, engine=engine, policy=slow_policy)
    assert writes == [50]  # one database write at finalization, none per input
    assert load_job(engine, run.job_id).n_succeeded == 50


def test_leftover_compute_processes_are_terminated(engine, tmp_path, monkeypatch, compute):
    run = _claimed_run(engine, tmp_path, monkeypatch, count=1)
    children = []

    def spawn(entry):
        children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"]))

    compute(_per_entry(spawn=spawn))
    executors.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)

    assert len(children) == 1
    child = children[0]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and child.poll() is None:
        time.sleep(0.05)
    assert child.poll() is not None
    try:
        child_alive = psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:  # reaped between checks: gone, which is what we assert
        child_alive = False
    assert not child_alive
    assert "terminated 1 leftover compute process(es)" in _run_summary(run)["log"]
    assert load_job(engine, run.job_id).status == JobStatus.FINISHED


def test_run_monitor_reports_cancellation_and_heartbeats(engine, tmp_path, monkeypatch):
    run = _claimed_run(engine, tmp_path, monkeypatch)  # Running
    monitor = lifecycle.RunMonitor(engine, run.job_id, FAST_POLICY)
    monitor.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and monitor.heartbeats < 2:
            time.sleep(0.01)
        assert monitor.heartbeats >= 2
        assert monitor.stop_requested is False
        with Session(engine) as session, session.begin():
            execution.request_cancellation(session.get(models.Job, run.job_id))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not monitor.stop_requested:
            time.sleep(0.01)
        assert monitor.stop_reason == lifecycle.STOP_REASON_CANCELLED
        monitor.request_shutdown()
        assert monitor.stop_reason == lifecycle.STOP_REASON_SHUTDOWN
    finally:
        monitor.close()
    assert monitor.errors == []
    assert not monitor.is_alive()


def test_progress_recorder_refuses_backward_counts(engine, tmp_path, monkeypatch):
    run = _claimed_run(engine, tmp_path, monkeypatch)
    recorder = lifecycle.ProgressRecorder(engine, run.job_id, 3600)
    recorder.report(succeeded=2, failed=0)
    with pytest.raises(execution.ExecutionStateError, match="backwards"):
        recorder.report(succeeded=1, failed=0)


def test_progress_a_report_left_pending_is_written_by_flush_pending_once_the_interval_has_passed(
    engine, tmp_path, monkeypatch
):
    """A live run's hooks call ``flush_pending`` at each look at the arrivals: the counts of a burst's last report,
    held back by the interval, reach the row without a later report; nothing is written when nothing is pending."""
    run = _claimed_run(engine, tmp_path, monkeypatch)
    now = [0.0]
    recorder = lifecycle.ProgressRecorder(engine, run.job_id, 5.0, clock=lambda: now[0])
    recorder.report(succeeded=1, failed=0)  # within the interval: pending
    recorder.flush_pending()
    assert (recorder.flushes, load_job(engine, run.job_id).n_succeeded) == (0, 0)
    now[0] = 5.0
    recorder.flush_pending()
    assert (recorder.flushes, load_job(engine, run.job_id).n_succeeded) == (1, 1)
    now[0] = 20.0
    recorder.flush_pending()  # nothing pending
    assert recorder.flushes == 1


def test_progress_a_write_that_failed_stays_pending_and_flush_pending_writes_it_after_the_interval(
    engine, tmp_path, monkeypatch
):
    """The database write-locked past its busy timeout fails a progress write: its counts stay pending, so the live
    hooks' next ``flush_pending`` after the interval writes them."""
    run = _claimed_run(engine, tmp_path, monkeypatch)
    now = [0.0]
    recorder = lifecycle.ProgressRecorder(engine, run.job_id, 5.0, clock=lambda: now[0])
    record_progress = execution.record_progress

    def locked(*args: object, **kwargs: object) -> None:
        raise OperationalError("UPDATE job", {}, Exception("database is locked"))

    monkeypatch.setattr(execution, "record_progress", locked)
    now[0] = 5.0
    recorder.report(succeeded=1, failed=0)  # due: written, and the write fails
    assert (recorder.flushes, len(recorder.errors), load_job(engine, run.job_id).n_succeeded) == (0, 1, 0)
    monkeypatch.setattr(execution, "record_progress", record_progress)
    recorder.flush_pending()  # within the interval of the failed write
    assert recorder.flushes == 0
    now[0] = 10.0
    recorder.flush_pending()
    assert (recorder.flushes, load_job(engine, run.job_id).n_succeeded) == (1, 1)


def test_a_queue_item_reads_the_settings_when_it_runs(tmp_path, monkeypatch, compute):
    """``execute_run(job_id)`` alone: the engine and the policy then come from $LAUEPORTAL_CONFIG, read at call
    time."""

    from laueportal.core.settings import get_settings

    database = tmp_path / "configured.db"
    engine = make_engine(tmp_path, name=database.name)
    run = _claimed_run(engine, tmp_path, monkeypatch)
    compute(_per_entry())
    config = tmp_path / "worker.toml"
    config.write_text(
        f'[database]\npath = "{database}"\n[files]\nruns_root = "{tmp_path}"\n'
        "[execution]\njob_timeout_seconds = 1234\nheartbeat_seconds = 0.05\nprogress_seconds = 0.05\n"
        "cancel_poll_seconds = 0.02\n"
    )
    for name in list(os.environ):
        if name.upper().startswith("LAUEPORTAL_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LAUEPORTAL_CONFIG", str(config))
    get_settings.cache_clear()
    try:
        result = executors.execute_run(run.job_id)
    finally:
        get_settings.cache_clear()
        engine.dispose()

    assert result is not None and result["status"] == "Finished"
    assert _run_summary(run)["policy"]["job_timeout_seconds"] == 1234


FRESH_PROCESS_SCRIPT = textwrap.dedent(
    """
    import json, sys

    from laueportal.jobs.contract import RunOutcome, register_compute_function
    from laueportal.jobs.executor import RunSetupError, execute_run

    def compute(request, entries, hooks):  # stands in for the wire program: the ORM use is what is tested
        entries = list(entries)
        hooks.report_progress(succeeded=len(entries), failed=0)
        return RunOutcome(n_succeeded=len(entries), n_failed=0, summary="fresh process")

    register_compute_function("wire_reconstruction", compute)
    try:
        execute_run(99)
        missing = None
    except RunSetupError as error:
        missing = str(error)
    finished = execute_run(int(sys.argv[1]))

    from laueportal.core.db import make_engine
    from laueportal.core.settings import get_settings
    from laueportal.jobs.reconciler import ReconcilePolicy, reconcile_once

    settings = get_settings()
    engine = make_engine(settings)
    reconciled = reconcile_once(engine, ReconcilePolicy.from_settings(settings.execution)).interrupted
    engine.dispose()
    print(json.dumps({
        "missing": missing,
        "status": finished and finished["status"],
        "reconciled": reconciled,
        "aggregate": "laueportal.models" in sys.modules,
    }))
    """
)


def test_a_fresh_process_runs_a_claimed_run_and_reconciles_dead_ones(tmp_path):
    """What a job process (``execute_run(job_id)`` of a claimed run) and the worker's reconciliation do in an
    interpreter of their own: no conftest and no ``laueportal.models``, settings from ``$LAUEPORTAL_CONFIG``."""

    settings, engine = make_database(tmp_path)
    try:
        queued = publish_wire_run(engine, tmp_path, name="queued")
        claimed(engine, queued.job_id)
        dead = publish_wire_run(engine, tmp_path, name="dead")
        enqueue_with(engine, dead.job_id)
        with Session(engine) as session, session.begin():
            execution.mark_running(session.get(models.Job, dead.job_id), now=datetime(2026, 1, 1, tzinfo=UTC))
        config = tmp_path / "worker.toml"
        config.write_text(
            f'[database]\npath = "{settings.database.path}"\n[files]\nruns_root = "{tmp_path}"\n'
            "[execution]\nheartbeat_seconds = 0.05\nprogress_seconds = 0.05\ncancel_poll_seconds = 0.02\n"
        )
        work = tmp_path / "cwd"
        work.mkdir()

        result = subprocess.run(
            [sys.executable, "-c", FRESH_PROCESS_SCRIPT, str(queued.job_id)],
            cwd=work,
            env=fresh_interpreter_env(config),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.splitlines()[-1]) == {
            "missing": "job 99 does not exist",
            "status": "Finished",
            "reconciled": [dead.job_id],
            "aggregate": False,
        }
        assert load_job(engine, queued.job_id).status == JobStatus.FINISHED
        assert (load_job(engine, dead.job_id).status, load_job(engine, dead.job_id).phase) == (
            JobStatus.FAILED,
            RunPhase.INTERRUPTED,
        )
    finally:
        engine.dispose()
