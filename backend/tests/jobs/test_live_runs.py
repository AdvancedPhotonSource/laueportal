"""A live run in the jobs core: created and queued as ``live_<job id>``, its inputs the files admitted into
``arrivals.jsonl`` (``jobs.arrivals``) while it is Queued or Running, its Stop a seal and a drain, and its end. The rows
with a real worker and job process (``slow``) run the stand-in live compute (``tests.jobs.job_computes.live_compute``);
the executor's live hooks also run here in-process.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal.core.settings import LiveSettings
from laueportal.jobs import arrivals, contract, controls, dispatch, executor, state
from laueportal.jobs.arrivals import ArrivalRefused, ArrivalWriter, notify
from laueportal.jobs.creation import create_live_run
from laueportal.jobs.dispatch import RUN, claim_run, process_record, record_process
from laueportal.jobs.lifecycle import STOP_REASON_CANCELLED, STOP_REASON_SHUTDOWN
from laueportal.jobs.live import (
    LiveFollows,
    is_live_job,
    is_live_run,
    live_marker,
    read_live_document,
)
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import (
    EMPTY_MANIFEST_SHA256,
    read_request,
    verify_manifest,
)
from laueportal.jobs.schemas import CancelState
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.reconstruction.models import ReconstructionRun
from tests.jobs.job_computes import (
    COMPUTE_MARKER,
    LIVE_FAIL,
    LIVE_HANG,
    LIVE_HOLD,
    LIVE_RAISE,
    LIVE_RETURN,
    RELEASE,
    live_compute,
)
from tests.jobs.live_support import (
    CounterSampler,
    LiveRun,
    create_live_wire_run,
    lauego_live_params,
    live_document,
    live_hooks,
    live_settings,
)
from tests.run_support import FAST_POLICY, enqueue_with, load_job, publish_wire_run
from tests.settings_support import make_database
from tests.worker_support import (
    FAST_EXECUTION,
    Live,
    alive,
    kill_quietly,
    kill_recorded_job_processes,
    listening,
    start_worker,
    wait_for,
)

RUN_SUMMARY_KEYS = {
    "format",
    "version",
    "job_id",
    "display_id",
    "kind",
    "status",
    "phase",
    "message",
    "started_at",
    "finished_at",
    "counters",
    "artifacts",
    "warnings",
    "validation_error",
    "provenance",
    "failure_report",
    "policy",
    "host",
    "log",
}
"""``run.json``'s keys: a live run's are the same."""


@pytest.fixture
def engine(tmp_path):
    _, engine = make_database(tmp_path)
    yield engine
    engine.dispose()


def _counters(engine, job_id: int) -> tuple[int, int, int, int]:
    job = load_job(engine, job_id)
    return job.n_inputs, job.n_succeeded, job.n_failed, job.n_not_run


def _run_summary(run: LiveRun) -> dict:
    return json.loads((run.run_directory / "run.json").read_text())


def _arrivals(run: LiveRun) -> list[dict]:
    return [json.loads(line) for line in (run.run_directory / "arrivals.jsonl").read_text().splitlines()]


# ---------------------------------------------------------------------------
# Created: n_inputs 0, Queued like any local run, its files


def test_a_live_run_is_created_with_its_files_and_queued_as_live_under_its_own_job_id(engine, tmp_path):
    run = create_live_wire_run(engine, tmp_path)
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, job.queue_job_id) == (JobStatus.QUEUED, RunPhase.QUEUED, f"live_{run.job_id}")
    assert (job.n_inputs, job.n_succeeded, job.n_failed, job.n_not_run, job.backend) == (0, 0, 0, 0, "local")
    files = {path.name: path for path in run.run_directory.iterdir()}
    assert set(files) == {"request.json", "inputs.jsonl", "live.json", "arrivals.lock", "arrivals.jsonl"}
    assert [files[name].stat().st_size for name in ("inputs.jsonl", "arrivals.lock", "arrivals.jsonl")] == [0, 0, 0]
    document = read_request(files["request.json"])
    assert document["manifest"] == {"path": "inputs.jsonl", "n_inputs": 0, "sha256": EMPTY_MANIFEST_SHA256}
    assert (document["kind"], document["request"]["schema_id"], document["request"]["scan_points"]) == (
        "wire_reconstruction",
        "wire-live/v1",
        "",
    )
    verify_manifest(job.manifest_path, expected_digest=job.manifest_digest, expected_count=0)  # the executor's check
    live = read_live_document(run.run_directory)
    assert (live.source, live.folder, live.filename_template) == ("notify", os.fspath(run.folder), "live_%d.h5")
    with Session(engine) as session:
        parameters = session.get(ReconstructionRun, run.run_id).wire_parameters
        assert (parameters.scan_points, parameters.scan_points_len) == ("", 0)


def test_a_row_is_live_by_its_queue_identity_while_active_and_by_live_json_too_once_ended(engine, tmp_path):
    """``queue_job_id`` exactly ``live_<own job id>``; in SQL for Queued and Running rows; ``live.json`` checked for an
    ended row (a folder that is gone reads as a batch run's)."""
    run = create_live_wire_run(engine, tmp_path)
    batch = publish_wire_run(engine, tmp_path, name="batch")
    enqueue_with(engine, batch.job_id)
    other = create_live_wire_run(engine, tmp_path, name="other")
    with Session(engine, expire_on_commit=False) as session, session.begin():
        stray = session.get(Job, other.job_id)
        stray.queue_job_id = f"live_{run.job_id}"  # another job's live identity is not its own
    with Session(engine) as session:
        live_ids = set(session.scalars(select(Job.job_id).where(live_marker())))
        assert live_ids == {run.job_id}
        assert (is_live_job(session, run.job_id), is_live_job(session, batch.job_id)) == (True, False)
    assert is_live_run(load_job(engine, run.job_id)) and not is_live_run(load_job(engine, batch.job_id))

    controls.cancel_run(run.job_id, engine=engine)
    assert is_live_run(load_job(engine, run.job_id))
    (run.run_directory / "live.json").unlink()
    assert not is_live_run(load_job(engine, run.job_id))


def test_a_live_lauego_run_on_raw_frames_or_following_a_live_wire_run_is_created_and_queued(engine, tmp_path):
    wire = create_live_wire_run(engine, tmp_path)
    settings = live_settings(tmp_path)
    frames = tmp_path / "frames"
    frames.mkdir()
    raw = lauego_live_params(
        settings,
        input_path=os.fspath(frames),
        output_path_template=os.fspath(tmp_path / "index_%d"),
        filename_prefixes=["frame_%d.h5"],
    )
    on_frames = create_live_run(raw, live_document(frames, "frame"), settings=settings, engine=engine)
    follower = lauego_live_params(
        settings,
        input_path=os.fspath(wire.run_directory / "reconstruction" / "scan.h5"),
        reconstruction_id=wire.run_id,
        output_path_template=os.fspath(tmp_path / "follow_%d"),
        filename_prefixes=["live_%d.h5"],
        depth_range="0-2",
    )
    follows = LiveFollows(job_id=wire.job_id, directory=os.fspath(wire.run_directory))
    following = create_live_run(
        follower, live_document(wire.folder, "live", "run", follows=follows), settings=settings, engine=engine
    )
    for created in (on_frames, following):
        assert created.failure is None
        job = load_job(engine, created.job_id)
        assert (job.phase, job.queue_job_id, job.n_inputs) == (RunPhase.QUEUED, f"live_{created.job_id}", 0)
        document = read_request(job.request_path)
        assert (document["kind"], document["request"]["schema_id"]) == ("lauego_indexing", "lauego-live/v1")
    assert read_request(load_job(engine, following.job_id).request_path)["run"]["reconstruction_id"] == wire.run_id

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(Exception, match="live.json's folder and file name"):
        create_live_run(raw, live_document(elsewhere, "frame"), settings=settings, engine=engine)


# ---------------------------------------------------------------------------
# The executor's live hooks, in this process


def _running_hooks(engine, tmp_path, **options) -> tuple[LiveRun, executor.LiveExecutorHooks, SimpleNamespace]:
    run = create_live_wire_run(engine, tmp_path)
    hooks, monitor = live_hooks(engine, run.job_id, **options)
    return run, hooks, monitor


def test_the_live_hook_hands_out_whole_lines_counted_first_and_seals_on_the_stop_flag(engine, tmp_path, caplog):
    run, hooks, monitor = _running_hooks(engine, tmp_path)
    for number in (3, 1):
        notify(engine, run.job_id, run.file(number))
    entries = hooks.arrivals()
    assert [(entry.index, entry.input_id, entry.scan_point, entry.template_index) for entry in entries] == [
        (0, "live_3", 3, 0),
        (1, "live_1", 1, 0),
    ]
    lines = (run.run_directory / "arrivals.jsonl").read_bytes().splitlines(keepends=True)
    admitted = [arrivals.Arrival.from_line(line, n).identity for n, line in enumerate(lines)]
    assert [entry.identity for entry in entries] == admitted  # each file's identity when admitted, for the compute
    assert _counters(engine, run.job_id)[0] == 2  # counted before the compute sees them
    assert hooks.arrivals() == [] and not hooks.should_stop()

    notify(engine, run.job_id, run.file(2))
    ledger = run.run_directory / "arrivals.jsonl"
    with ledger.open("ab") as handle:
        handle.write(b'{"n":3,"path":')  # a line still being written: not read yet
    assert [entry.input_id for entry in hooks.arrivals()] == ["live_2"]

    controls.cancel_run(run.job_id, engine=engine)  # the flag, as the monitor would see it
    monitor.stop_requested, monitor.stop_reason = True, STOP_REASON_CANCELLED
    with pytest.raises(ArrivalRefused, match="was stopped"):
        notify(engine, run.job_id, run.file(4))
    assert not hooks.should_stop()  # not sealed until the hook has handed out the rest
    with caplog.at_level(logging.WARNING, logger="laueportal.jobs.executor"):
        assert hooks.arrivals() == []
    assert hooks.sealed and hooks.should_stop() and hooks.arrivals() == []
    assert "ignored a torn last line of arrivals.jsonl at its seal" in caplog.text
    assert (hooks.n_inputs, hooks.scan_points, _counters(engine, run.job_id)[0]) == (3, [3, 1, 2], 3)
    assert "sealed after 3 arrivals (3 inputs)" in hooks.messages


def test_the_seal_waits_for_a_writer_that_read_the_run_open_so_its_line_is_part_of_the_run(
    engine, tmp_path, monkeypatch
):
    """The seal: a writer that checked the run open before the Stop holds the lock until its line is written; the seal
    takes the lock after it, so the executor reads that line before it calls the run sealed."""
    run, hooks, monitor = _running_hooks(engine, tmp_path)
    checking, go_on = threading.Event(), threading.Event()
    closed = ArrivalWriter._closed

    def slow_check(writer: ArrivalWriter) -> str | None:
        answer = closed(writer)  # open: read before the Stop
        checking.set()
        assert go_on.wait(10)
        return answer

    monkeypatch.setattr(ArrivalWriter, "_closed", slow_check)
    writer = threading.Thread(target=notify, args=(engine, run.job_id, run.file(1)))
    writer.start()
    assert checking.wait(10)
    controls.cancel_run(run.job_id, engine=engine)
    monitor.stop_requested, monitor.stop_reason = True, STOP_REASON_CANCELLED
    handed: list[list] = []
    executor_side = threading.Thread(target=lambda: handed.append(hooks.arrivals()))
    executor_side.start()
    executor_side.join(0.3)
    assert executor_side.is_alive() and not hooks.sealed  # waiting for the writer's lock
    go_on.set()
    writer.join(10)
    executor_side.join(10)
    assert [[entry.input_id for entry in entries] for entries in handed] == [["live_1"]]
    assert hooks.sealed and hooks.should_stop() and _counters(engine, run.job_id)[0] == 1


def test_a_service_stop_ends_the_live_hook_s_arrivals_at_once(engine, tmp_path):
    run, hooks, monitor = _running_hooks(engine, tmp_path)
    notify(engine, run.job_id, run.file(1))
    monitor.stop_requested, monitor.stop_reason = True, STOP_REASON_SHUTDOWN
    assert hooks.should_stop() and hooks.arrivals() == [] and not hooks.sealed
    assert _counters(engine, run.job_id)[0] == 0  # admitted, never read: not counted (no Resume)


def test_arrivals_that_cannot_be_counted_yet_are_handed_out_once_they_are(engine, tmp_path, monkeypatch):
    """A write-locked database (its busy timeout passed): nothing is handed out uncounted, and the next call reads
    the same lines again."""
    run, hooks, _ = _running_hooks(engine, tmp_path)
    notify(engine, run.job_id, run.file(1))
    record = hooks._record
    calls: list[int] = []

    def locked_once(count: int) -> None:
        calls.append(count)
        if len(calls) == 1:
            raise OperationalError("UPDATE job", {}, sqlite3.OperationalError("database is locked"))
        record(count)

    monkeypatch.setattr(hooks, "_record", locked_once)
    assert hooks.arrivals() == [] and _counters(engine, run.job_id)[0] == 0
    assert [entry.index for entry in hooks.arrivals()] == [0]
    assert (calls, hooks.n_inputs, _counters(engine, run.job_id)[0]) == ([1, 1], 1, 1)


def test_the_live_hook_looks_at_arrivals_jsonl_at_a_bounded_rate_however_fast_the_compute_asks(
    engine, tmp_path, monkeypatch
):
    """``arrivals.jsonl`` is on the runs root, so the hook looks at it at most every ``live.arrivals_seconds``, a stat,
    and reads it only when it changed; the seal reads it to the end at once."""
    now = [100.0]
    run, hooks, monitor = _running_hooks(engine, tmp_path, interval=1.0, clock=lambda: now[0])
    ledger = os.fspath(run.run_directory / "arrivals.jsonl")
    calls: list[str] = []
    real_stat, real_open = os.stat, open

    def stat(path, *args, **kwargs):
        if os.fspath(path) == ledger:
            calls.append("stat")
        return real_stat(path, *args, **kwargs)

    def opened(path, *args, **kwargs):
        if os.fspath(path) == ledger:
            calls.append("read")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(arrivals, "open", opened, raising=False)

    def call(at: float) -> tuple[list[str], list[str]]:
        now[0] = at
        calls.clear()
        handed = [entry.input_id for entry in hooks.arrivals()]
        return handed, list(calls)

    notify(engine, run.job_id, run.file(1))
    assert call(100.0) == (["live_1"], ["stat", "read"])
    notify(engine, run.job_id, run.file(2))
    assert call(100.5) == ([], [])  # within the interval: the file is not touched
    assert call(101.0) == (["live_2"], ["stat", "read"])
    assert call(102.0) == ([], ["stat"])  # unchanged: a stat only
    notify(engine, run.job_id, run.file(3))
    controls.cancel_run(run.job_id, engine=engine)
    monitor.stop_requested, monitor.stop_reason = True, STOP_REASON_CANCELLED
    assert call(102.1)[0] == ["live_3"] and hooks.sealed  # the seal: at once, to the end
    assert call(110.0) == ([], [])  # sealed: never looked at again
    assert _counters(engine, run.job_id)[0] == 3


def test_a_ledger_rewritten_to_its_size_is_read_again_by_its_modification_time(engine, tmp_path):
    """The ledger's one write that keeps its size (a dead writer's torn last line truncated, then the next admission's
    line of the same length appended) shows by its modification time alone; the next look reads the line."""
    now = [100.0]
    run, hooks, _ = _running_hooks(engine, tmp_path, interval=1.0, clock=lambda: now[0])
    ledger = run.run_directory / "arrivals.jsonl"
    notify(engine, run.job_id, run.file(1))
    notify(engine, run.job_id, run.file(2))
    first, second = ledger.read_bytes().splitlines(keepends=True)
    ledger.write_bytes(first + b"x" * len(second))  # before the second admission: a torn tail of that length
    stamp = ledger.stat().st_mtime_ns
    torn = [entry.input_id for entry in hooks.arrivals()]
    ledger.write_bytes(first + second)  # the next writer truncated the torn tail and appended its line
    os.utime(ledger, ns=(stamp + 10**9, stamp + 10**9))  # a second later, whatever the file system's tick
    now[0] = 101.0
    assert (torn, [entry.input_id for entry in hooks.arrivals()]) == (["live_1"], ["live_2"])
    assert _counters(engine, run.job_id)[0] == 2


def test_record_arrivals_only_raises_the_inputs_of_a_running_run(engine, tmp_path):
    run = create_live_wire_run(engine, tmp_path)
    with Session(engine) as session, session.begin():
        job = session.get(Job, run.job_id)
        with pytest.raises(state.ExecutionStateError, match="cannot take arrivals"):
            state.record_arrivals(job, 1)  # Queued
    assert claim_run(engine, run.job_id)
    with Session(engine) as session, session.begin():
        job = session.get(Job, run.job_id)
        state.record_arrivals(job, 2)
        state.record_arrivals(job, 0)
        with pytest.raises(state.ExecutionStateError, match="negative"):
            state.record_arrivals(job, -1)
        state.record_progress(job, succeeded=1, failed=1)
        state.mark_finalizing(job)
        with pytest.raises(state.ExecutionStateError, match="cannot take arrivals"):
            state.record_arrivals(job, 1)
        state.finalize(job, status=JobStatus.FINISHED)
        assert (job.n_inputs, job.n_succeeded, job.n_failed, job.n_not_run) == (2, 1, 1, 0)


def test_a_followed_point_is_one_input_per_selected_depth_counted_before_it_is_handed_out(engine, tmp_path):
    """A followed point gives one input per depth of the follower's ``depth_range``, else per depth it has; a selected
    depth it lacks is handed out too (its compute fails it, one failed input). Its line is written here (the follower's
    thread admits it: ``tests/jobs/test_follower.py``)."""
    wire = create_live_wire_run(engine, tmp_path)
    settings = live_settings(tmp_path)
    point = wire.run_directory / "reconstruction" / "points" / "p1.h5"
    follows = LiveFollows(job_id=wire.job_id, directory=os.fspath(wire.run_directory))
    handed: dict[str | None, list[tuple]] = {}
    for depth_range in (None, "1,5"):
        params = lauego_live_params(
            settings,
            input_path=os.fspath(wire.run_directory / "reconstruction" / "scan.h5"),
            reconstruction_id=wire.run_id,
            output_path_template=os.fspath(tmp_path / f"follow_{len(handed)}_%d"),
            filename_prefixes=["live_%d.h5"],
            depth_range=depth_range,
        )
        document = live_document(wire.folder, "live", "run", follows=follows)
        created = create_live_run(params, document, settings=settings, engine=engine)
        assert created.failure is None, created.failure
        job = load_job(engine, created.job_id)
        line = arrivals.Arrival(
            n=0,
            path=os.fspath(point),
            size=1,
            mtime_ns=1,
            inode=1,
            device=1,
            source="run",
            admitted_at="2026-10-26T10:00:00+00:00",
            scan_point=4,
            point_id="p1",
            n_depths=3,
        ).line()
        (Path(job.run_directory) / "arrivals.jsonl").write_bytes(line)
        hooks, _ = live_hooks(engine, created.job_id)
        entries = hooks.arrivals()
        handed[depth_range] = [
            (entry.index, entry.input_id, entry.point_id, entry.depth_index, entry.scan_point, entry.source)
            for entry in entries
        ]
        assert {entry.identity for entry in entries} == {(1, 1, 1, 1)}  # the point file's when admitted
        assert _counters(engine, created.job_id)[0] == len(entries) == hooks.n_inputs  # counted first
    assert handed == {
        None: [(depth, f"p1_{depth}", "p1", depth, 4, os.fspath(point)) for depth in range(3)],
        "1,5": [(0, "p1_1", "p1", 1, 4, os.fspath(point)), (1, "p1_5", "p1", 5, 4, os.fspath(point))],
    }


# ---------------------------------------------------------------------------
# A live run's setup, its ledger and its end, run by the executor in this process (the stand-in live compute)


def _execute(engine, job_id: int, monkeypatch) -> Job:
    """Claim the live run ``job_id`` and run it in this process with the stand-in live compute. A guard stops the
    service after 10 s, so a run that would wait for a Stop forever ends (interrupted) instead of hanging the test."""
    monkeypatch.setitem(contract._LIVE_REGISTRY, "wire_reconstruction", live_compute)
    assert claim_run(engine, job_id)
    shutdown = threading.Event()
    guard = threading.Timer(10, shutdown.set)
    guard.start()
    try:
        executor.execute_run(
            job_id,
            engine=engine,
            policy=FAST_POLICY,
            shutdown=shutdown,
            live_settings=LiveSettings(arrivals_seconds=0.02),
        )
    finally:
        guard.cancel()
    return load_job(engine, job_id)


def test_a_live_run_without_its_live_json_fails_at_setup(engine, tmp_path):
    """A run queued as live has its ``live.json`` as part of its setup: without it the run fails there, never run as a
    batch run."""
    run = create_live_wire_run(engine, tmp_path)
    (run.run_directory / "live.json").unlink()
    assert claim_run(engine, run.job_id)
    with pytest.raises(executor.RunSetupError, match="live.json"):
        executor._start(engine, run.job_id, executor.RunPolicy())
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert "Run setup failed: " in job.messages and "live.json" in job.messages


def test_a_live_compute_that_returns_before_the_seal_fails_the_run(engine, tmp_path, monkeypatch):
    """The end rule: only a sealed run (its Stop drained) ends Finished or Cancelled; a live compute that returns before
    it fails the run."""
    run = create_live_wire_run(engine, tmp_path)
    notify(engine, run.job_id, run.file(1, LIVE_RETURN))
    job = _execute(engine, run.job_id, monkeypatch)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert job.messages.endswith(
        "Run failed: the live compute returned before the run was stopped; 1 succeeded, 0 failed, 0 not run of 1"
    )
    assert run.processed() and "sealed after" not in " ".join(_run_summary(run)["log"])


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("not json", "line 2 of arrivals.jsonl is not an arrival (JSONDecodeError: "),
        ("out of order", "line 2 of arrivals.jsonl is arrival 2, not 1"),
    ],
)
def test_a_ledger_line_that_is_not_the_next_arrival_is_a_run_level_error_never_skipped(
    engine, tmp_path, monkeypatch, damage, reason
):
    """A whole line of ``arrivals.jsonl`` that is not JSON, or not the next arrival (a line lost), fails the run at
    once; it is never skipped, and nothing after it is handed out."""
    run = create_live_wire_run(engine, tmp_path)
    for number in (1, 2, 3):
        notify(engine, run.job_id, run.file(number))
    ledger = run.run_directory / "arrivals.jsonl"
    first, second, third = ledger.read_bytes().splitlines(keepends=True)
    ledger.write_bytes(first + (b"{not json\n" + third if damage == "not json" else third))
    with pytest.raises(arrivals.ArrivalsUnreadable, match=re.escape(reason)):  # the job process logs it
        _execute(engine, run.job_id, monkeypatch)
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert f"Run failed: ArrivalsUnreadable: {reason}" in job.messages
    assert (run.processed(), _counters(engine, run.job_id)) == ([], (0, 0, 0, 0))


# ---------------------------------------------------------------------------
# Stop while Queued: cancelled at once, whatever was admitted


def test_a_stop_while_queued_cancels_a_live_run_at_once_whatever_was_admitted(engine, tmp_path):
    run = create_live_wire_run(engine, tmp_path)
    assert notify(engine, run.job_id, run.file(1)).admitted  # a Queued live run admits
    result = controls.cancel_run(run.job_id, engine=engine)
    job = load_job(engine, run.job_id)
    assert (result.state, job.status, job.phase) == (CancelState.CANCELLED, JobStatus.CANCELLED, RunPhase.CANCELLED)
    assert job.n_inputs == 0 and len(_arrivals(run)) == 1
    with pytest.raises(ArrivalRefused, match="has ended"):
        notify(engine, run.job_id, run.file(2))


# ---------------------------------------------------------------------------
# Through a real worker and job process (slow): files admitted while Queued and Running, Stop, the end rule


def _start(live: Live, tmp_path: Path, *, burst: bool = True):
    return start_worker(live.config, log=tmp_path / "worker.log", burst=burst, test_computes=True)


def _running(engine, job_id: int) -> bool:
    return load_job(engine, job_id).status == JobStatus.RUNNING


def test_a_job_process_a_failed_test_left_running_is_killed_at_the_end_of_its_database(tmp_path, live):
    """A live run's job process has a session of its own and no time limit, so the ``live`` fixture's end kills the job
    processes its rows record that still run (a test that failed before it stopped its run)."""
    run = create_live_wire_run(live.engine, tmp_path)
    assert claim_run(live.engine, run.job_id)
    left = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        assert record_process(live.engine, RUN, run.job_id, process_record(left.pid))
        killed = kill_recorded_job_processes(live.engine)
        left.wait(10)
    finally:
        kill_quietly([left.pid])
    assert killed == [left.pid] and left.returncode == -signal.SIGKILL
    assert kill_recorded_job_processes(live.engine) == []  # gone: nothing more


@pytest.mark.slow
def test_a_live_run_counts_files_admitted_while_queued_and_running_and_its_stop_seals_and_drains_it_to_finished(
    tmp_path, live
):
    engine = live.engine
    run = create_live_wire_run(engine, tmp_path)
    for number in (1, 2):  # while Queued, counted once it runs
        notify(engine, run.job_id, run.file(number))
    sampler = CounterSampler(engine, run).start()
    worker = _start(live, tmp_path)
    try:
        assert wait_for(lambda: len(run.processed()) == 2), worker.log_text()
        assert load_job(engine, run.job_id).status == JobStatus.RUNNING and _counters(engine, run.job_id)[0] == 2
        notify(engine, run.job_id, run.file(3, LIVE_HOLD))  # while Running
        assert wait_for(lambda: (run.run_directory / COMPUTE_MARKER).exists()), worker.log_text()
        for number in (4, 5):  # admitted while the compute is busy: unread when the Stop comes
            notify(engine, run.job_id, run.file(number))
        assert controls.cancel_run(run.job_id, engine=engine).state == CancelState.REQUESTED
        with pytest.raises(ArrivalRefused, match="was stopped"):
            notify(engine, run.job_id, run.file(6))
        (run.run_directory / RELEASE).touch()
        assert worker.wait(60) == 0, worker.log_text()
    finally:
        worker.cleanup()
        sampler.stop()

    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FINISHED, RunPhase.FINISHED), job.messages
    assert _counters(engine, run.job_id) == (5, 5, 0, 0)
    assert job.messages.endswith("Finished: 5 succeeded, 0 failed, 0 not run of 5; test live compute")
    assert [json.loads(line)["index"] for line in run.processed()] == [0, 1, 2, 3, 4]
    assert [(line["n"], Path(line["path"]).name, line["source"]) for line in _arrivals(run)] == [
        (n, f"live_{n + 1}.h5", "notify") for n in range(5)
    ]
    summary = _run_summary(run)
    assert set(summary) == RUN_SUMMARY_KEYS  # no new key
    assert summary["policy"]["job_timeout_seconds"] is None  # no time limit
    assert summary["counters"]["n_inputs"] == 5 and "sealed after 5 arrivals (5 inputs)" in summary["log"]
    with Session(engine) as session:
        parameters = session.get(ReconstructionRun, run.run_id).wire_parameters
        assert (parameters.scan_points, parameters.scan_points_len) == ("1-5", 5)  # the arrivals' numbers
    sampler.assert_bounded_and_growing(worker.log_text())
    assert {sample[0] for sample in sampler.samples} >= {0, 2, 5}


@pytest.mark.slow
def test_a_live_run_stopped_with_nothing_admitted_ends_cancelled(tmp_path, live):
    engine = live.engine
    run = create_live_wire_run(engine, tmp_path)
    worker = _start(live, tmp_path)
    try:
        assert wait_for(lambda: _running(engine, run.job_id)), worker.log_text()
        assert controls.cancel_run(run.job_id, engine=engine).state == CancelState.REQUESTED
        assert worker.wait(60) == 0, worker.log_text()
    finally:
        worker.cleanup()
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase, _counters(engine, run.job_id)) == (
        JobStatus.CANCELLED,
        RunPhase.CANCELLED,
        (0, 0, 0, 0),
    )
    assert job.messages.endswith("Stopped before any input arrived")


@pytest.mark.slow
def test_a_failed_input_is_recorded_the_live_run_goes_on_and_it_ends_failed(tmp_path, live):
    engine = live.engine
    run = create_live_wire_run(engine, tmp_path)
    sampler = CounterSampler(engine, run).start()
    worker = _start(live, tmp_path)
    try:
        assert wait_for(lambda: _running(engine, run.job_id)), worker.log_text()
        for number, content in ((1, b""), (2, LIVE_FAIL), (3, b"")):
            notify(engine, run.job_id, run.file(number, content))
        assert wait_for(lambda: len(run.processed()) == 3), worker.log_text()  # the input after the failed one too
        assert load_job(engine, run.job_id).status == JobStatus.RUNNING
        controls.cancel_run(run.job_id, engine=engine)
        assert worker.wait(60) == 0, worker.log_text()
    finally:
        worker.cleanup()
        sampler.stop()
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert job.messages.endswith("Incomplete: 2 succeeded, 1 failed, 0 not run of 3")
    failures = (run.run_directory / "failures.jsonl").read_text().splitlines()
    assert [json.loads(line)["input_id"] for line in failures] == ["live_2"]
    assert job.failure_report_path == os.fspath(run.run_directory / "failures.jsonl")
    sampler.assert_bounded_and_growing(worker.log_text())


@pytest.mark.slow
def test_a_run_level_error_fails_a_live_run_at_once_and_keeps_what_it_published(tmp_path, live):
    engine = live.engine
    run = create_live_wire_run(engine, tmp_path)
    notify(engine, run.job_id, run.file(1))
    notify(engine, run.job_id, run.file(2, LIVE_RAISE))
    worker = _start(live, tmp_path)
    try:
        assert worker.wait(60) == 0, worker.log_text()  # no Stop: the error ends the run
    finally:
        worker.cleanup()
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert job.messages.endswith(
        "Run failed: RuntimeError: stand-in run-level error at live_2; 1 succeeded, 0 failed, 1 not run of 2"
    )
    assert [json.loads(line)["input_id"] for line in run.processed()] == ["live_1"]  # published, kept
    assert _run_summary(run)["status"] == "Failed"


@pytest.mark.slow
def test_stop_now_kills_a_live_run_whose_drain_is_stuck_and_it_ends_failed_interrupted_keeping_what_it_published(
    tmp_path, live
):
    """Through a real worker, an input that hangs keeps a stopped live run Running (no time limit); its Stop now kills
    its job process's group (the dispatcher) and the run ends Failed, interrupted, the input processed before it kept;
    the worker then has nothing open and ends."""
    engine = live.engine
    run = create_live_wire_run(engine, tmp_path)
    worker = _start(live, tmp_path)
    try:
        assert wait_for(lambda: _running(engine, run.job_id)), worker.log_text()
        notify(engine, run.job_id, run.file(1))
        notify(engine, run.job_id, run.file(2, LIVE_HANG))
        assert wait_for(lambda: (run.run_directory / COMPUTE_MARKER).exists()), worker.log_text()
        job_process = json.loads((run.run_directory / COMPUTE_MARKER).read_text())["job_process"]
        assert controls.cancel_run(run.job_id, engine=engine).state == CancelState.REQUESTED
        time.sleep(2 * FAST_EXECUTION["cancel_poll_seconds"] + 1)  # the stop is seen; the drain is stuck
        stuck = load_job(engine, run.job_id).status
        assert controls.stop_run_now(run.job_id, engine=engine).state == CancelState.STOPPING_NOW
        assert worker.wait(60) == 0, worker.log_text()
    finally:
        worker.cleanup()
    job = load_job(engine, run.job_id)
    assert stuck == JobStatus.RUNNING and not alive(job_process)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)
    assert job.messages.splitlines()[-2:] == [state.STOP_NOW_REQUESTED, dispatch.STOPPED_NOW]
    assert [json.loads(line)["input_id"] for line in run.processed()] == ["live_1"]  # published, kept
    assert "Stop now: its job process" in worker.log_text()


@pytest.mark.slow
def test_a_service_stop_interrupts_a_live_run_as_a_batch_run(tmp_path, live):
    engine = live.engine
    run = create_live_wire_run(engine, tmp_path)
    worker = _start(live, tmp_path, burst=False)
    try:
        assert wait_for(lambda: listening(engine, worker)), worker.log_text()
        notify(engine, run.job_id, run.file(1, LIVE_HOLD))
        assert wait_for(lambda: (run.run_directory / COMPUTE_MARKER).exists()), worker.log_text()
        notify(engine, run.job_id, run.file(2))  # admitted, never read: not counted
        started = time.monotonic()
        os.kill(worker.pid, signal.SIGTERM)
        assert worker.wait(60) == 0, worker.log_text()
        assert time.monotonic() - started < 30
    finally:
        worker.cleanup()
    job = load_job(engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)
    assert job.messages.endswith(
        "Stopped by service shutdown; completed results kept; 1 succeeded, 0 failed, 0 not run of 1"
    )
    assert _counters(engine, run.job_id) == (1, 1, 0, 0) and len(_arrivals(run)) == 2
