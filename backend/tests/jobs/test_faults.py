"""Fault injection: one test per fault, in the order below.

Each test injects its fault into a running system and checks that the database (the source of truth) ends right and
that the worker carries on: live ``laueportal worker`` processes and their job processes (``slow``), or, where no
real process is needed, the dispatcher in this process with stand-in job processes (``test_dispatch.FakeJobs``).
They wait on events (marker files, log lines, rows) with short configured intervals (``reconcile_seconds`` and
``stale_heartbeat_seconds`` of 1 s, the live tests' ``FAST_EXECUTION``), never on long sleeps.

1. a job process killed (SIGKILL) mid-run; 2. the worker killed mid-run; 3. the worker killed while runs are Queued;
4. the worker killed between a claim and the start (a hook in the worker holds it there); 5. a second worker; 6. a
run left in ``created`` or ``published``; 7. a conversion whose job process died; 8. Stop of a Queued run; 9. Move to
Front racing a claim; 10. SIGTERM during a run, and a second SIGTERM; 11. the database write-locked through a
dispatch cycle; 12. the poller thread raises (the worker's remote poller); 13. a job process that cannot start. Then,
with a live run in the mix, the same guarantees (no run twice, none lost, none ended while its job process runs): the
worker killed with a live run Queued and a batch run stopped for it in flight; a batch run that will not drain past
``live.batch_stop_seconds``; a live run created while a batch run is starting (the claim, the record and the go
line). The reconciler's rules on a database alone: ``test_reconciler.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from laueportal.app import create_app
from laueportal.core import db
from laueportal.core.timeutil import utcnow
from laueportal.indexing import conversion
from laueportal.indexing.models import Conversion
from laueportal.jobs import controls, dispatch, reconciler, worker
from laueportal.jobs.dispatch import RUN, Dispatcher, Kind
from laueportal.jobs.models import Job
from laueportal.jobs.schemas import CancelResult, CancelState, MoveResult, MoveState
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.remote.poller import POLLER_LOCK_NAME, PollerLock
from tests.api_support import PORTAL_HEADERS
from tests.indexing.test_viewer import FIXTURE_XML, RUNS, add_run
from tests.jobs import job_computes
from tests.jobs.live_support import create_live_wire_run
from tests.jobs.test_controls import _during_a_claim
from tests.jobs.test_dispatch import POLICY, FakeJobs, _drain
from tests.remote_support import Layout, bdp_settings
from tests.run_support import dispatch_queue, enqueue_with, load_job, publish_wire_run
from tests.settings_support import make_database
from tests.worker_support import (
    BACKEND,
    Live,
    WorkerProcess,
    alive,
    listening,
    run_burst_worker,
    start_worker,
    wait_for,
    worker_env,
    worker_row,
    write_config,
)

CYCLE_SECONDS = 1
"""The reconciler's period and the stale limit of the live tests."""

FINISHED = (JobStatus.FINISHED, RunPhase.FINISHED)
INTERRUPTED = (JobStatus.FAILED, RunPhase.INTERRUPTED)
QUEUED = (JobStatus.QUEUED, RunPhase.QUEUED)
RUNNING = (JobStatus.RUNNING, RunPhase.RUNNING)


# ---------------------------------------------------------------------------
# Helpers


@pytest.fixture
def setup(tmp_path):
    settings, engine = make_database(tmp_path)
    yield settings, engine
    engine.dispose()


@pytest.fixture
def engine(setup):
    return setup[1]


@pytest.fixture
def fake(tmp_path):
    jobs = FakeJobs(tmp_path)
    yield jobs
    jobs.cleanup()


def _state(engine, job_id: int) -> tuple[int, str | None]:
    job = load_job(engine, job_id)
    return job.status, job.phase


def _job(engine, job_id: int) -> tuple[int, str | None, str]:
    job = load_job(engine, job_id)
    return job.status, job.phase, job.messages or ""


def _queued(engine_or_live, tmp_path: Path, name: str, **options) -> int:
    engine = engine_or_live.engine if isinstance(engine_or_live, Live) else engine_or_live
    run = publish_wire_run(engine, tmp_path, name=name, **options)
    enqueue_with(engine, run.job_id)
    return run.job_id


def _blocked(live: Live, job_id: int, worker_process: WorkerProcess) -> dict[str, int]:
    """Wait until the run's test compute blocks; returns its pids (killed by the worker's cleanup)."""
    marker = Path(load_job(live.engine, job_id).run_directory or "") / job_computes.COMPUTE_MARKER
    assert wait_for(marker.exists, timeout=60), worker_process.log_text()
    compute = json.loads(marker.read_text())
    worker_process.extra_pids += [compute["job_process"], compute["grandchild"]]
    return compute


def _release(live: Live, job_id: int) -> None:
    """Let a ``job_computes.WAIT`` run's compute return (it then succeeds every input)."""
    (Path(load_job(live.engine, job_id).run_directory or "") / job_computes.RELEASE).write_text("")


def _fast_reconciler(live: Live, tmp_path: Path, **execution) -> Path:
    return live.write_config(
        tmp_path / "reconciling.toml",
        reconcile_seconds=CYCLE_SECONDS,
        stale_heartbeat_seconds=CYCLE_SECONDS,
        **execution,
    )


def _kill_main_process(worker_process: WorkerProcess) -> None:
    """SIGKILL the worker's main process alone (the OOM killer, ``kill -9``): its job processes, each a session of
    its own, live on."""
    os.kill(worker_process.pid, signal.SIGKILL)
    worker_process.process.wait(10)


def _cycled(live: Live, worker_process: WorkerProcess) -> bool:
    """The worker's dispatcher and reconciler have each run a cycle (their marks on its row)."""
    row = worker_row(live.engine)
    return row is not None and row.pid == worker_process.pid and None not in (row.dispatcher_at, row.reconciler_at)


def _started_jobs(log: str) -> list[int]:
    """The runs a worker started a job process for, in order (its log)."""
    return [int(job_id) for job_id in re.findall(r"Started the job process \d+ of job (\d+)", log)]


def _stop(live: Live, worker_process: WorkerProcess) -> None:
    """supervisor's SIGTERM; the worker exits 0."""
    assert wait_for(lambda: listening(live.engine, worker_process), timeout=30), worker_process.log_text()
    os.kill(worker_process.pid, signal.SIGTERM)
    assert worker_process.wait(60) == 0, worker_process.log_text()


# ---------------------------------------------------------------------------
# 1. A job process killed (SIGKILL) mid-run


@pytest.mark.slow
def test_a_job_process_killed_mid_run_frees_its_slot_at_once_and_its_run_is_interrupted_within_a_cycle(
    tmp_path: Path, live: Live
) -> None:
    """Its exit wakes the dispatcher (SIGCHLD; the dispatcher's period is 30 s here), which kills what the job
    process left in its group and starts the next run at once; within one reconcile cycle after its heartbeat goes
    stale, the killed run is Failed/interrupted."""
    killed = _queued(live, tmp_path, "killed", count=2, notes=job_computes.WAIT)
    following = _queued(live, tmp_path, "next")
    config = _fast_reconciler(live, tmp_path, dispatch_seconds=30)
    worker_process = start_worker(config, log=tmp_path / "worker.log", burst=False, test_computes=True)
    try:
        compute = _blocked(live, killed, worker_process)
        assert load_job(live.engine, killed).process_pid == compute["job_process"]  # the dispatcher's record
        assert _state(live.engine, following) == QUEUED

        os.kill(compute["job_process"], signal.SIGKILL)
        at = time.monotonic()

        assert wait_for(lambda: load_job(live.engine, following).start_time is not None, timeout=20), (
            worker_process.log_text()
        )
        started_after = time.monotonic() - at
        assert wait_for(lambda: _state(live.engine, killed) == INTERRUPTED, timeout=30), worker_process.log_text()
        interrupted_after = time.monotonic() - at
        assert started_after < 5, started_after  # the wake, not the 30 s period
        # The heartbeat is stale one second after the kill, and the next cycle (every second) finds it.
        assert interrupted_after < 2 * CYCLE_SECONDS + 5, interrupted_after
        job = load_job(live.engine, killed)
        assert "Interrupted: the worker or job process died (no heartbeat since" in (job.messages or "")
        assert job.n_not_run == 2
        assert wait_for(lambda: not alive(compute["grandchild"]), timeout=10)  # the sweep killed the leftover
        assert f"Killed processes left behind by the job process {compute['job_process']} of job {killed}" in (
            worker_process.log_text()
        )
        assert wait_for(lambda: _state(live.engine, following) == FINISHED, timeout=60), worker_process.log_text()
        _stop(live, worker_process)
    finally:
        worker_process.cleanup()


# ---------------------------------------------------------------------------
# 2. The worker killed (SIGKILL) mid-run


@pytest.mark.slow
def test_a_worker_killed_mid_run_leaves_its_job_process_to_finish_the_run_and_a_restarted_worker_waits_for_it(
    tmp_path: Path, live: Live
) -> None:
    """The job process carries on. A restarted worker counts its slot (the next run waits), starts nothing in it and
    does not finalize its run, even while its heartbeat is stale (the job process stopped: its recorded process
    still exists). Released, the job process finalizes its run Finished, and the restarted worker starts the next
    run."""
    orphaned = _queued(live, tmp_path, "orphan", count=2, notes=job_computes.WAIT)
    waiting = _queued(live, tmp_path, "waiting")
    first = start_worker(live.config, log=tmp_path / "w1.log", burst=False, test_computes=True)
    second: WorkerProcess | None = None
    try:
        compute = _blocked(live, orphaned, first)
        job_pid = compute["job_process"]
        _kill_main_process(first)
        second = start_worker(
            _fast_reconciler(live, tmp_path), log=tmp_path / "w2.log", burst=False, test_computes=True
        )
        assert wait_for(lambda: _cycled(live, second), timeout=60), second.log_text()
        assert (_state(live.engine, orphaned), _state(live.engine, waiting)) == (RUNNING, QUEUED)

        os.kill(job_pid, signal.SIGSTOP)  # its heartbeat goes stale; the process still exists
        try:
            left_alone = f"Job {orphaned}: no heartbeat since the stale limit, but its job process {job_pid} is alive"
            assert wait_for(lambda: left_alone in second.log_text(), timeout=30), second.log_text()
            assert (_state(live.engine, orphaned), _state(live.engine, waiting)) == (RUNNING, QUEUED)
        finally:
            os.kill(job_pid, signal.SIGCONT)
        _release(live, orphaned)

        assert wait_for(lambda: _state(live.engine, orphaned) == FINISHED, timeout=60), second.log_text()
        assert load_job(live.engine, orphaned).n_succeeded == 2  # by its own job process
        assert wait_for(lambda: _state(live.engine, waiting) == FINISHED, timeout=60), second.log_text()
        assert _started_jobs(second.log_text()) == [waiting]
        _stop(live, second)
    finally:
        first.cleanup()
        if second is not None:
            second.cleanup()


# ---------------------------------------------------------------------------
# 3. The worker killed while runs are Queued


@pytest.mark.slow
def test_a_worker_killed_while_runs_are_queued_loses_none_and_a_restarted_worker_runs_them_in_order(
    tmp_path: Path, live: Live
) -> None:
    """The worker is killed (SIGKILL) while three runs wait behind a running one, the last of them moved to the
    front. A Queued row is its own queue entry, so nothing is lost: once the killed worker's job process has
    finished its run, a restarted worker runs the three in order, the moved one first, each once."""
    running = _queued(live, tmp_path, "running", notes=job_computes.WAIT)
    first = start_worker(live.config, log=tmp_path / "w1.log", burst=False, test_computes=True)
    try:
        _blocked(live, running, first)
        a, b, c = (_queued(live, tmp_path, name) for name in ("a", "b", "c"))
        assert controls.move_run_to_front(c, engine=live.engine).state == MoveState.MOVED
        _kill_main_process(first)
        _release(live, running)
        assert wait_for(lambda: _state(live.engine, running) == FINISHED, timeout=60)
        assert dispatch_queue(live.engine) == [(RUN, c), (RUN, a), (RUN, b)]

        second = run_burst_worker(live.config, log=tmp_path / "w2.log", test_computes=True)

        assert second.process.returncode == 0, second.log_text()
        assert [_state(live.engine, job_id) for job_id in (c, a, b)] == [FINISHED] * 3
        assert _started_jobs(second.log_text()) == [c, a, b]
    finally:
        first.cleanup()


# ---------------------------------------------------------------------------
# 4. The worker killed between a claim and the start


def _claim_then_hang(marker: Path) -> str:
    """A worker prelude: the dispatcher's claim, then (once it claimed a run) write the run's ID to ``marker`` and
    hang, before the job process starts."""
    return (
        "import pathlib, threading\n"
        "_claim_run = dispatch.claim_run\n"
        "def _claim_then_hang(engine, job_id):\n"
        "    if not _claim_run(engine, job_id):\n"
        "        return False\n"
        f"    pathlib.Path({str(marker)!r}).write_text(str(job_id))\n"
        "    threading.Event().wait()\n"
        "dispatch.claim_run = _claim_then_hang\n"
    )


@pytest.mark.slow
def test_a_worker_killed_between_a_claim_and_the_start_leaves_a_running_run_the_reconciler_fails_once_stale(
    tmp_path: Path, live: Live
) -> None:
    """The worker is killed after its dispatcher claimed a run (Running) and before it started the run's job
    process. The run is Running with no process; a restarted worker's reconciler fails it Failed/interrupted once
    its heartbeat (the claim's) is stale, and the restarted dispatcher runs the next run meanwhile."""
    marker = tmp_path / "claimed"
    stranded = _queued(live, tmp_path, "stranded")
    following = _queued(live, tmp_path, "next")
    first = start_worker(live.config, log=tmp_path / "w1.log", burst=False, prelude=_claim_then_hang(marker))
    second: WorkerProcess | None = None
    try:
        assert wait_for(marker.exists, timeout=60), first.log_text()
        assert marker.read_text() == str(stranded)
        _kill_main_process(first)
        job = load_job(live.engine, stranded)
        assert (job.status, job.phase, job.process_pid) == (*RUNNING, None)
        assert "Started the job process" not in first.log_text()

        second = start_worker(
            _fast_reconciler(live, tmp_path), log=tmp_path / "w2.log", burst=False, test_computes=True
        )

        assert wait_for(lambda: _state(live.engine, stranded) == INTERRUPTED, timeout=30), second.log_text()
        job = load_job(live.engine, stranded)
        assert "Interrupted: the worker or job process died (no heartbeat since" in (job.messages or "")
        assert job.finish_time is not None and job.heartbeat_at is not None
        assert job.finish_time - job.heartbeat_at >= timedelta(seconds=CYCLE_SECONDS)  # only once stale
        assert wait_for(lambda: _state(live.engine, following) == FINISHED, timeout=60), second.log_text()
        assert _started_jobs(second.log_text()) == [following]
        _stop(live, second)
    finally:
        first.cleanup()
        if second is not None:
            second.cleanup()


# ---------------------------------------------------------------------------
# 5. A second worker on the same database


@pytest.mark.slow
def test_a_second_worker_on_the_same_database_exits_1_at_the_lock_and_no_run_starts_twice(
    tmp_path: Path, live: Live
) -> None:
    """One worker per database (the ``flock`` on ``<database>.worker.lock``): a second worker started while the first
    runs a run exits 1 at the lock and names the worker its row records; the first carries on, and each run starts once,
    in the first (the claim takes a run once: ``test_dispatch.py``'s concurrent claims)."""
    running = _queued(live, tmp_path, "running", notes=job_computes.WAIT)
    waiting = _queued(live, tmp_path, "waiting")
    first = start_worker(live.config, log=tmp_path / "first.log", burst=False, test_computes=True)
    try:
        compute = _blocked(live, running, first)
        assert wait_for(lambda: listening(live.engine, first), timeout=60), first.log_text()
        second = run_burst_worker(live.config, log=tmp_path / "second.log", timeout=60, test_computes=True)

        assert second.process.returncode == 1, second.log_text()
        lock = worker.lock_path(live.database)
        assert f"Another worker holds {lock} (pid {first.pid} on {socket.gethostname()}, started " in (
            second.log_text()
        )
        assert "Started the job process" not in second.log_text()
        row = worker_row(live.engine)
        assert row is not None and (row.pid, row.stopped_at) == (first.pid, None)
        assert first.process.poll() is None
        assert load_job(live.engine, running).process_pid == compute["job_process"]
        assert _state(live.engine, waiting) == QUEUED

        _release(live, running)
        assert wait_for(lambda: _state(live.engine, waiting) == FINISHED, timeout=60), first.log_text()
        assert _started_jobs(first.log_text()) == [running, waiting]
        _stop(live, first)
    finally:
        first.cleanup()


# ---------------------------------------------------------------------------
# 6. A run left in created or published


@pytest.mark.slow
def test_a_run_left_in_created_or_published_is_failed_by_a_reconcile_cycle_and_its_directory_kept(
    tmp_path: Path, live: Live
) -> None:
    """A creation that stopped (its process killed) after the run's rows were saved (``created``) or after its
    manifest was published (``published``), never queued. Past ``creation_timeout_seconds`` (their last change moved
    back: the setting's minimum is a minute), ``laueportal jobs reconcile --once``, the worker's cycle in a process
    of its own, fails each with today's message and keeps its run directory; a younger one is left."""
    published = publish_wire_run(live.engine, tmp_path, count=2, name="published").job_id
    created = publish_wire_run(live.engine, tmp_path, count=2, name="created").job_id
    young = publish_wire_run(live.engine, tmp_path, count=2, name="young").job_id
    with Session(live.engine) as session, session.begin():
        stopped = session.get(Job, created)
        assert stopped is not None
        stopped.phase = RunPhase.CREATED  # the creation stopped before its manifest was published
        for job_id, ago in ((published, 11), (created, 11), (young, 9)):
            job = session.get(Job, job_id)
            assert job is not None
            job.updated_at = utcnow() - timedelta(minutes=ago)

    done = subprocess.run(
        [sys.executable, "-m", "laueportal", "jobs", "reconcile", "--once"],
        cwd=BACKEND,
        env=worker_env(live.config),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr
    assert f"stuck in creation [{published}, {created}]" in done.stdout, done.stdout
    for job_id, phase in ((published, "published"), (created, "created")):
        job = load_job(live.engine, job_id)
        assert (job.status, job.phase, job.n_not_run) == (JobStatus.FAILED, RunPhase.FAILED, 2)
        assert (
            (job.messages or "")
            .splitlines()[-1]
            .startswith(f"Creation did not complete: the run is still {phase} since")
        )
        assert Path(job.run_directory or "").is_dir()  # kept for diagnosis
    assert _state(live.engine, young) == (JobStatus.QUEUED, RunPhase.PUBLISHED)


# ---------------------------------------------------------------------------
# 7. A conversion whose process died


def _conversion(live: Live) -> Conversion:
    with Session(live.engine, expire_on_commit=False) as session:
        row = session.get(Conversion, 1)
        assert row is not None
        session.expunge(row)
        return row


@pytest.mark.slow
def test_a_conversion_whose_job_process_died_is_failed_and_can_be_converted_again(tmp_path: Path, live: Live) -> None:
    """SIGKILL of a conversion's job process (held at its start here: it dies before it converts). The worker's
    reconciler fails the conversion ("Interrupted: the worker or job process died; convert the results again"), the
    artifact reports it, and converting again through the API queues a new conversion, which the worker runs to its
    end."""
    data = tmp_path / "data" / "index_1"
    data.mkdir(parents=True)
    shutil.copy(FIXTURE_XML, data / "a.xml")
    add_run(live.engine, 1, data)
    settings, api_engine = make_database(tmp_path / "api", database={"path": live.database})
    api_engine.dispose()
    hold = tmp_path / "hold"
    hold.mkdir()
    worker_process = start_worker(_fast_reconciler(live, tmp_path), log=tmp_path / "worker.log", burst=False, hold=hold)
    try:
        with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
            assert client.post(f"{RUNS}/1/convert").status_code == 202
            assert wait_for(lambda: (hold / "holding").exists() and _conversion(live).process_pid, timeout=60), (
                worker_process.log_text()
            )
            pid = _conversion(live).process_pid
            assert pid is not None and _conversion(live).status == "running"
            worker_process.extra_pids.append(pid)

            os.kill(pid, signal.SIGKILL)

            assert wait_for(lambda: _conversion(live).status == "failed", timeout=30), worker_process.log_text()
            failed = client.get(f"{RUNS}/1/artifact").json()
            again = client.post(f"{RUNS}/1/convert")
            (hold / "release").write_text("")
            assert wait_for(lambda: _conversion(live).status == "finished", timeout=120), worker_process.log_text()
            done = client.get(f"{RUNS}/1/artifact").json()

        assert (failed["state"], failed["conversion"]["status"]) == ("xml_only", "failed")
        assert failed["conversion"]["message"] == (
            "Interrupted: the worker or job process died; convert the results again"
        )
        assert (again.status_code, again.json()["status"]) == (202, "queued")
        assert done["state"] == "ready"
        assert _conversion(live).message == "converted: 4 frames, 5 patterns"
        assert worker_process.log_text().count("of the conversion of I1") >= 2  # started twice: once each
        _stop(live, worker_process)
    finally:
        worker_process.cleanup()


# ---------------------------------------------------------------------------
# 8. Stop of a Queued run


def test_stop_of_a_queued_run_cancels_it_at_once_and_the_dispatcher_never_starts_it(engine, tmp_path):
    stopped, other = _queued(engine, tmp_path, "stopped"), _queued(engine, tmp_path, "other")
    fake = FakeJobs(tmp_path)

    result = controls.cancel_run(stopped, engine=engine)
    cancelled = load_job(engine, stopped)
    try:
        _drain(Dispatcher(engine, POLICY, start=fake))
    finally:
        fake.cleanup()

    assert result == CancelResult(job_id=stopped, state=CancelState.CANCELLED, n_pending=3)
    assert (cancelled.status, cancelled.phase, cancelled.n_not_run) == (JobStatus.CANCELLED, RunPhase.CANCELLED, 3)
    assert fake.started == [(RUN, other)]
    job = load_job(engine, stopped)
    assert (job.status, job.start_time, job.process_pid) == (JobStatus.CANCELLED, None, None)
    assert dispatch.claim_run(engine, stopped) is False


# ---------------------------------------------------------------------------
# 9. Move to Front racing a claim


def test_move_to_front_racing_a_claim_is_never_undone_and_a_claimed_run_is_not_queued(setup, tmp_path, monkeypatch):
    """A move that commits first is the dispatcher's order (the claim takes that run, and the move stays recorded);
    a move that waits for a claim in progress finds the run Running: 409 not queued, through the API too, and
    nothing changes."""
    settings, engine = setup
    first, moved, raced = (_queued(engine, tmp_path, name) for name in ("first", "moved", "raced"))

    assert controls.move_run_to_front(moved, engine=engine).state == MoveState.MOVED
    with Session(engine) as session:
        taken = dispatch.next_item(session)
    assert taken == (RUN, moved) and dispatch.claim_run(engine, moved)
    claimed, result = _during_a_claim(
        monkeypatch, engine, raced, lambda: controls.move_run_to_front(raced, engine=engine)
    )
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        refused = client.post(f"/api/v1/jobs/{raced}/move-to-front")

    assert load_job(engine, moved).moved_to_front_at is not None  # never undone
    assert claimed is True
    assert result == MoveResult(job_id=raced, state=MoveState.NOT_QUEUED, phase=RunPhase.RUNNING)
    assert (refused.status_code, refused.json()["title"]) == (409, "Job not queued")
    assert load_job(engine, raced).moved_to_front_at is None
    assert dispatch_queue(engine) == [(RUN, first)]


# ---------------------------------------------------------------------------
# 10. SIGTERM to the worker during a run, and a second SIGTERM


@pytest.mark.slow
def test_stopping_the_worker_drains_the_running_run_then_exits(tmp_path, live):
    """supervisor's SIGTERM: no more claims, SIGUSR1 to the job process (the run drains and ends Failed/
    interrupted), then the worker records its stop and exits 0."""
    job_id = _queued(live, tmp_path, "running", count=2, notes=job_computes.BLOCK)
    waiting = _queued(live, tmp_path, "waiting")
    worker_process = start_worker(live.config, log=tmp_path / "worker.log", burst=False, test_computes=True)
    try:
        compute = _blocked(live, job_id, worker_process)
        assert _job(live.engine, job_id)[:2] == (JobStatus.RUNNING, RunPhase.RUNNING)
        assert wait_for(lambda: listening(live.engine, worker_process), timeout=30)

        os.kill(worker_process.pid, signal.SIGTERM)

        assert worker_process.wait(60) == 0, worker_process.log_text()
        status, phase, messages = _job(live.engine, job_id)
        assert (status, phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)
        assert messages.splitlines()[-1].startswith("Stopped by service shutdown")
        assert not alive(compute["grandchild"]) and not alive(compute["job_process"])
        assert f"Asked job process(es) [{compute['job_process']}] to stop" in worker_process.log_text()
        assert _job(live.engine, waiting)[:2] == (JobStatus.QUEUED, RunPhase.QUEUED)  # nothing claimed after
        row = worker_row(live.engine)
        assert row is not None and row.stopped_at is not None
    finally:
        worker_process.cleanup()


@pytest.mark.slow
def test_a_second_sigterm_kills_the_job_process_group_at_once(tmp_path, live):
    """A job process that does not drain (here, a compute deaf to the stop) holds the service stop up to
    ``shutdown_grace_seconds``; a second SIGTERM kills its group at once. Its run, left Running, is the
    reconciler's to finalize once its heartbeat is stale."""
    job_id = _queued(live, tmp_path, "stubborn", count=2, notes=job_computes.STUBBORN)
    config = live.write_config(tmp_path / "long-grace.toml", shutdown_grace_seconds=300)
    worker_process = start_worker(config, log=tmp_path / "worker.log", burst=False, test_computes=True)
    try:
        compute = _blocked(live, job_id, worker_process)
        assert wait_for(lambda: listening(live.engine, worker_process), timeout=30)
        os.kill(worker_process.pid, signal.SIGTERM)
        assert wait_for(lambda: "to stop; waiting up to 300" in worker_process.log_text(), timeout=30)
        time.sleep(0.5)
        assert alive(compute["job_process"]) and worker_process.process.poll() is None

        os.kill(worker_process.pid, signal.SIGTERM)
        stopping = time.monotonic()

        assert worker_process.wait(60) == 0, worker_process.log_text()
        assert time.monotonic() - stopping < 20
        assert wait_for(lambda: not alive(compute["job_process"]) and not alive(compute["grandchild"]), timeout=10)
        log = worker_process.log_text()
        assert f"Killed the job process group(s) [{compute['job_process']}] after a second stop signal" in log
        assert _job(live.engine, job_id)[:2] == (JobStatus.RUNNING, RunPhase.RUNNING)
    finally:
        worker_process.cleanup()


# ---------------------------------------------------------------------------
# 11. The database write-locked through a dispatch cycle


@pytest.fixture
def short_busy(tmp_path, monkeypatch):
    """A database whose connections wait 0.2 s for another's write lock (the busy timeout; 30 s otherwise)."""
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 200)
    settings, engine = make_database(tmp_path)
    yield settings, engine
    engine.dispose()


class _WriteLock:
    """Another connection's write lock on the database (``BEGIN IMMEDIATE``), held from ``take`` to ``release``."""

    def __init__(self, database: Path) -> None:
        self.connection = sqlite3.connect(database, timeout=5, isolation_level=None)

    def take(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def release(self) -> None:
        self.connection.execute("ROLLBACK")

    def close(self) -> None:
        self.connection.close()


def test_a_dispatch_cycle_through_a_write_locked_database_is_tried_again_and_nothing_is_claimed_twice_or_lost(
    short_busy, tmp_path, fake, monkeypatch, caplog
):
    """Another connection holds the database's write lock through a dispatch cycle (past the busy timeout): the
    cycle claims nothing and ends logged, and the next one claims the run. A lock taken between a claim and its
    record leaves the claimed run's job process waiting at its go line (it runs nothing, its slot is busy, and the
    worker's reconciler leaves the run alone: its dispatcher started the process); the first cycle after the lock
    records it and lets it go. Each run is claimed and started once."""
    settings, engine = short_busy
    first, second = _queued(engine, tmp_path, "first"), _queued(engine, tmp_path, "second")
    lock = _WriteLock(settings.database.path)
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    claim_run = dispatch.claim_run

    def claim_then_lock(engine_, job_id: int) -> bool:
        claimed = claim_run(engine_, job_id)
        if claimed:
            lock.take()
        return claimed

    try:
        with caplog.at_level(logging.INFO, logger=dispatch.__name__):
            lock.take()
            locked_cycle = dispatcher.cycle()
            unclaimed = (_state(engine, first), list(fake.started))
            lock.release()

            monkeypatch.setattr(dispatch, "claim_run", claim_then_lock)
            claiming_cycle = dispatcher.cycle()  # claims the first run; the lock comes before its record
            monkeypatch.setattr(dispatch, "claim_run", claim_run)
            (pid,) = dispatcher.children
            waiting_cycle = dispatcher.cycle()  # still locked: the record fails again, and nothing more is claimed
            unrecorded = load_job(engine, first)
            told_to_go = fake.marker(RUN, first).exists()
            lock.release()
            left_alone = reconciler.reconcile_runs(engine, stale_after_seconds=0, own=dispatcher.running_items)

            recording_cycle = dispatcher.cycle()
            recorded = load_job(engine, first)
            dispatcher.children[pid].process.wait(10)  # told to go, it wrote what it read and exited
            _drain(dispatcher)
    finally:
        lock.close()

    assert (locked_cycle, unclaimed) == (None, ((JobStatus.QUEUED, RunPhase.QUEUED), []))
    assert (claiming_cycle, waiting_cycle, recording_cycle) == (RUN, None, None)
    assert (unrecorded.status, unrecorded.process_pid, told_to_go) == (JobStatus.RUNNING, None, False)
    assert not left_alone
    assert (recorded.status, recorded.process_pid) == (JobStatus.RUNNING, pid)
    assert fake.marker(RUN, first).read_bytes() == dispatch.GO
    assert fake.started == [(RUN, first), (RUN, second)]
    assert dispatch_queue(engine) == []
    messages = [record.getMessage() for record in caplog.records]
    assert (
        messages.count("The dispatcher could not use the database (database is locked); its next cycle tries again")
        == 1
    )
    assert "The dispatcher can use the database again" in messages
    waits = f"Could not record the job process {pid} of job {first} (database is locked); it waits"
    assert sum(message.startswith(waits) for message in messages) == 1


# ---------------------------------------------------------------------------
# 12. The poller thread raises


def _poller_fault(fault: Path) -> str:
    """A worker prelude: its remote poller's pass (``remote.poller.poll``) raises while ``fault`` exists, and a
    thread that raised starts again after 0.3 s (``RESTART_FIRST_SECONDS``, then twice as long each time)."""
    return (
        "import pathlib\n"
        "from laueportal.jobs import worker as _worker\n"
        "from laueportal.remote import poller as _poller\n"
        "_worker.RESTART_FIRST_SECONDS = 0.3\n"
        "_poll = _poller.poll\n"
        "def _faulty(**kwargs):\n"
        f"    if pathlib.Path({str(fault)!r}).exists():\n"
        "        raise RuntimeError('an injected poller fault')\n"
        "    return _poll(**kwargs)\n"
        "_poller.poll = _faulty\n"
    )


def _poller_restarts(worker_process: WorkerProcess) -> int:
    return worker_process.log_text().count("The worker's poller thread raised; it starts again in")


@pytest.mark.slow
def test_a_poller_thread_that_raises_starts_again_while_the_dispatcher_and_the_reconciler_carry_on(
    tmp_path: Path, live: Live
) -> None:
    """The worker runs the remote poller (remote execution enabled; no remote run, so no DM call). While its pass
    raises, the dispatcher runs a queued run to the end and its mark and the reconciler's move on, but the poller's mark
    stays where it was: the status page shows it late. The poller thread starts again (twice here, the wait doubling),
    and once the fault is gone it passes and marks again. The worker then stops on SIGTERM with status 0 and lets the
    poller lock go."""
    layout = Layout(tmp_path / "remote")
    layout.experiment.mkdir(parents=True)
    bdp = tmp_path / "bdp.yaml"
    bdp.write_text(yaml.safe_dump({**bdp_settings(layout), "poll_seconds": 0.1}))
    config = write_config(
        tmp_path / "remote.toml",
        database=live.database,
        runs_root=layout.experiment,
        execution={"reconcile_seconds": CYCLE_SECONDS},
        remote={"enabled": True, "settings_file": bdp},
    )
    lock = layout.submissions / POLLER_LOCK_NAME
    fault = tmp_path / "fault"
    worker_process = start_worker(
        config, log=tmp_path / "worker.log", burst=False, test_computes=True, prelude=_poller_fault(fault)
    )
    try:
        assert wait_for(lambda: _marks(live, worker_process)[2] is not None, timeout=60), worker_process.log_text()
        assert not PollerLock(lock).take()  # the worker's thread holds it
        fault.write_text("")
        assert wait_for(lambda: _poller_restarts(worker_process) >= 1, timeout=30), worker_process.log_text()
        _, _, frozen = _marks(live, worker_process)
        assert frozen is not None
        run = _queued(live, tmp_path, "while-the-poller-raises")
        assert wait_for(lambda: _state(live.engine, run) == FINISHED, timeout=60), worker_process.log_text()
        assert wait_for(lambda: _poller_restarts(worker_process) >= 2, timeout=30), worker_process.log_text()
        moved_on = timedelta(seconds=CYCLE_SECONDS)
        assert wait_for(
            lambda: all(mark is not None and mark > frozen + moved_on for mark in _marks(live, worker_process)[:2]),
            timeout=30,
        ), worker_process.log_text()
        assert _marks(live, worker_process)[2] == frozen  # late meanwhile
        fault.unlink()
        assert wait_for(lambda: (_marks(live, worker_process)[2] or frozen) > frozen, timeout=30), (
            worker_process.log_text()
        )
        _stop(live, worker_process)
    finally:
        worker_process.cleanup()
    log = worker_process.log_text()
    assert "RuntimeError: an injected poller fault" in log
    assert f"The remote poller holds {lock}; it polls every 0.1 s" in log
    assert PollerLock(lock).take()  # let go at the worker's end


def _marks(live: Live, worker_process: WorkerProcess) -> tuple[datetime | None, datetime | None, datetime | None]:
    """The worker row's marks (dispatcher, reconciler, poller), once the row is this worker's."""
    row = worker_row(live.engine)
    if row is None or row.pid != worker_process.pid:
        return None, None, None
    return row.dispatcher_at, row.reconciler_at, row.poller_at


# ---------------------------------------------------------------------------
# 13. A job process that cannot start


def test_a_job_process_that_cannot_start_ends_its_item_with_the_reason_and_the_dispatcher_carries_on(
    engine, tmp_path, fake, caplog
):
    """Its interpreter is missing: the start (``subprocess.Popen``) raises, the run ends Failed and the conversion
    ``failed`` with the reason, and the dispatcher takes the next item."""
    broken, after = _queued(engine, tmp_path, "broken"), _queued(engine, tmp_path, "after")
    add_run(engine, 101, None)
    conversion.enqueue_conversion(101, sessions=engine)
    missing = tmp_path / "no-such-interpreter"

    def start(kind: Kind, key: int) -> subprocess.Popen[bytes]:
        if key in (broken, 101):
            return subprocess.Popen([str(missing), kind, str(key)], stdin=subprocess.PIPE, start_new_session=True)
        return fake(kind, key)

    dispatcher = Dispatcher(engine, POLICY, start=start)
    with caplog.at_level(logging.ERROR, logger="laueportal.jobs.dispatch"):
        _drain(dispatcher)

    reason = f"The job process could not start: FileNotFoundError: [Errno 2] No such file or directory: '{missing}'"
    job = load_job(engine, broken)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert reason in (job.messages or "")
    failed = conversion.find_conversion(101, sessions=engine)
    assert failed is not None and (failed.status, failed.message) == ("failed", reason)
    assert fake.started == [(RUN, after)]
    assert f"Job {broken}: The job process could not start" in caplog.text


# ---------------------------------------------------------------------------
# With a live run in the mix: no run twice, none lost, none ended while its process lives


class _EndedAlive:
    """Samples a run's state and its job process every 10 ms in a thread: ``ended_alive`` holds each sample in which
    the run had ended while the process still ran. It catches a gross defect only: an end at the stop flag, or the
    reconciler ending the live orphan, while the process lives. An end written right after the kill but before the
    process is gone leaves no window to sample (a SIGKILLed process dies within milliseconds); that rule is pinned by
    the fast test of ``test_live_dispatch.py``, whose kill does not take:
    ``test_a_batch_run_stopped_for_a_live_run_is_never_ended_while_its_process_lives_nor_left_running_without_one``."""

    def __init__(self, engine, job_id: int, pid: int) -> None:
        self.engine, self.job_id, self.pid = engine, job_id, pid
        self.samples = 0
        self.ended_alive: list[tuple[int, str | None]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()

    def _sample(self) -> None:
        while not self._stop.is_set():
            running = alive(self.pid)  # first: an end read after it while it lived is caught
            status, phase = _state(self.engine, self.job_id)
            if running and status in (JobStatus.FINISHED, JobStatus.FAILED, JobStatus.CANCELLED):
                self.ended_alive.append((status, phase))
            self.samples += 1
            self._stop.wait(0.01)

    def stop(self) -> _EndedAlive:
        self._stop.set()
        self._thread.join(10)
        return self


def _live_started(run, worker_process: WorkerProcess) -> dict:
    """Wait until the live run's stand-in compute runs; its marker (job process, cores)."""
    started = run.run_directory / job_computes.LIVE_STARTED
    assert wait_for(started.exists, timeout=60), worker_process.log_text()
    return json.loads(started.read_text())


def _stop_live(live: Live, run, worker_process: WorkerProcess) -> None:
    controls.cancel_run(run.job_id, engine=live.engine)
    assert wait_for(lambda: _state(live.engine, run.job_id) == CANCELLED, timeout=60), worker_process.log_text()


CANCELLED = (JobStatus.CANCELLED, RunPhase.CANCELLED)
STOPPED_FOR_LIVE = "Cancellation requested: stopped for a live run"


@pytest.mark.slow
def test_a_worker_killed_with_a_live_run_queued_and_a_batch_run_stopped_for_it_loses_neither_and_runs_neither_twice(
    tmp_path: Path, live: Live
) -> None:
    """The worker is killed while a batch run, stopped for a live run, has not drained (its compute is deaf to the
    stop) and the live run waits for it. The batch run's job process carries on; a restarted worker counts it in the
    batch slot, stops it again and, past ``live.batch_stop_seconds``, kills its group and only then ends it
    Cancelled; the live run then starts, once. The first worker started only the batch run, the second only the
    live run."""
    batch = _queued(live, tmp_path, "batch", count=2, notes=job_computes.STUBBORN)
    first = start_worker(
        live.write_config(tmp_path / "w1.toml", live={"batch_stop_seconds": 300}),
        log=tmp_path / "w1.log",
        burst=False,
        test_computes=True,
    )
    second: WorkerProcess | None = None
    sampler: _EndedAlive | None = None
    try:
        compute = _blocked(live, batch, first)
        run = create_live_wire_run(live.engine, tmp_path)
        assert wait_for(lambda: STOPPED_FOR_LIVE in _job(live.engine, batch)[2], timeout=30), first.log_text()
        _kill_main_process(first)
        in_flight = (_state(live.engine, batch), _state(live.engine, run.job_id), alive(compute["job_process"]))
        sampler = _EndedAlive(live.engine, batch, compute["job_process"])
        config = live.write_config(
            tmp_path / "w2.toml",
            live={"batch_stop_seconds": 1},
            reconcile_seconds=CYCLE_SECONDS,
            stale_heartbeat_seconds=CYCLE_SECONDS,
        )
        second = start_worker(config, log=tmp_path / "w2.log", burst=False, test_computes=True)

        _live_started(run, second)
        assert not alive(compute["job_process"]) and not alive(compute["grandchild"])
        _stop_live(live, run, second)
        _stop(live, second)
    finally:
        if sampler is not None:
            sampler.stop()
        first.cleanup()
        if second is not None:
            second.cleanup()

    assert in_flight == (RUNNING, QUEUED, True)
    status, phase, messages = _job(live.engine, batch)
    assert (status, phase) == CANCELLED
    assert messages.splitlines()[-2:] == [
        STOPPED_FOR_LIVE,
        "Stopped for a live run: its job process was killed after 1 s",
    ]
    assert sampler.samples > 0 and sampler.ended_alive == []
    assert (_started_jobs(first.log_text()), _started_jobs(second.log_text())) == ([batch], [run.job_id])


@pytest.mark.slow
def test_a_batch_run_that_will_not_drain_is_killed_past_batch_stop_seconds_and_ended_only_once_it_is_gone(
    tmp_path: Path, live: Live
) -> None:
    """A batch run whose compute is deaf to its stop flag: ``live.batch_stop_seconds`` after the flag the dispatcher
    kills its job process's group (the process and what it started), ends it Cancelled once they are gone, and
    starts the live run; the queued batch run waits for the live run's end."""
    batch = _queued(live, tmp_path, "batch", count=2, notes=job_computes.STUBBORN)
    waiting = _queued(live, tmp_path, "waiting")
    config = live.write_config(tmp_path / "stop.toml", live={"batch_stop_seconds": 1})
    worker_process = start_worker(config, log=tmp_path / "worker.log", burst=False, test_computes=True)
    sampler: _EndedAlive | None = None
    try:
        compute = _blocked(live, batch, worker_process)
        sampler = _EndedAlive(live.engine, batch, compute["job_process"])
        run = create_live_wire_run(live.engine, tmp_path)
        _live_started(run, worker_process)
        gone = (alive(compute["job_process"]), alive(compute["grandchild"]))
        held = _state(live.engine, waiting)
        _stop_live(live, run, worker_process)
        assert wait_for(lambda: _state(live.engine, waiting) == FINISHED, timeout=60), worker_process.log_text()
        _stop(live, worker_process)
    finally:
        if sampler is not None:
            sampler.stop()
        worker_process.cleanup()

    status, phase, messages = _job(live.engine, batch)
    assert (status, phase) == CANCELLED
    assert messages.splitlines()[-2:] == [
        STOPPED_FOR_LIVE,
        "Stopped for a live run: its job process was killed after 1 s",
    ]
    assert gone == (False, False) and held == QUEUED
    ended = load_job(live.engine, batch)
    assert ended.cancel_requested_at is not None and ended.finish_time is not None
    assert ended.finish_time - ended.cancel_requested_at >= timedelta(seconds=1)  # not before the limit
    assert sampler.samples > 0 and sampler.ended_alive == []
    log = worker_process.log_text()
    assert f"Job {batch}, stopped for a live run, is still running 1 s later; killing its job process's group" in log
    assert _started_jobs(log) == [batch, run.job_id, waiting]


def _claim_then_wait(marker: Path, go_on: Path) -> str:
    """A worker prelude: the dispatcher's first claim, then (once it claimed a run) write the run's ID to ``marker``
    and wait for ``go_on`` before the job process starts (the claim/record/go window)."""
    return (
        "import pathlib, time\n"
        "_claim_run = dispatch.claim_run\n"
        "def _claim_then_wait(engine, job_id):\n"
        "    if not _claim_run(engine, job_id):\n"
        "        return False\n"
        f"    marker = pathlib.Path({str(marker)!r})\n"
        "    if not marker.exists():\n"
        "        marker.write_text(str(job_id))\n"
        f"        while not pathlib.Path({str(go_on)!r}).exists():\n"
        "            time.sleep(0.02)\n"
        "    return True\n"
        "dispatch.claim_run = _claim_then_wait\n"
    )


@pytest.mark.slow
def test_a_live_run_created_while_a_batch_run_is_starting_waits_for_it_to_drain_and_neither_runs_twice(
    tmp_path: Path, live: Live
) -> None:
    """The live run is created after the dispatcher claimed a batch run and before it started the run's job process
    (the claim, the record and the go line). The batch run starts once, gets the stop flag at the next cycle and
    drains to Cancelled; the live run then starts, once; the other queued batch run waits for the live run's end."""
    marker, go_on = tmp_path / "claimed", tmp_path / "go-on"
    batch = _queued(live, tmp_path, "batch", count=2, notes=job_computes.BLOCK)
    waiting = _queued(live, tmp_path, "waiting")
    worker_process = start_worker(
        live.config,
        log=tmp_path / "worker.log",
        burst=False,
        test_computes=True,
        prelude=_claim_then_wait(marker, go_on),
    )
    try:
        assert wait_for(marker.exists, timeout=60), worker_process.log_text()
        claimed = load_job(live.engine, batch)
        run = create_live_wire_run(live.engine, tmp_path)
        go_on.touch()
        _live_started(run, worker_process)
        held = _state(live.engine, waiting)
        _stop_live(live, run, worker_process)
        assert wait_for(lambda: _state(live.engine, waiting) == FINISHED, timeout=60), worker_process.log_text()
        _stop(live, worker_process)
    finally:
        worker_process.cleanup()

    assert (claimed.status, claimed.process_pid) == (JobStatus.RUNNING, None)
    status, phase, messages = _job(live.engine, batch)
    assert (status, phase) == CANCELLED
    assert messages.splitlines()[-2:] == [
        STOPPED_FOR_LIVE,
        "Cancelled by request; completed results kept; 0 succeeded, 0 failed, 2 not run of 2",
    ]
    assert held == QUEUED
    assert _started_jobs(worker_process.log_text()) == [batch, run.job_id, waiting]
