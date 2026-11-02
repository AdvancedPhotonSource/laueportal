"""The reconciler's rules (``jobs.reconciler``) on a test database.

A cycle is the database's alone: a stale Running local run whose job process is not alive is Failed/interrupted, a
running conversion whose job process is gone is ``failed`` (and can be converted again), a run left in ``created`` or
``published`` is Failed with its run directory kept; Queued runs are their own queue entries (nothing is checked or
put back). A job process is alive as the dispatcher counts it: one the worker running the cycle started (``own``) or
the one the row records, alive on this host with its recorded start. The live faults (a killed job process or
worker, a conversion's process dying) are ``test_faults.py``'s; the purge of expired keys is
``test_idempotency.py``'s; remote runs are left to the poller (``test_remote_dispatch.py``).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from laueportal import cli, models
from laueportal.core.settings import get_settings
from laueportal.core.timeutil import utcnow
from laueportal.indexing import conversion
from laueportal.indexing.models import Conversion
from laueportal.jobs import dispatch, reconciler
from laueportal.jobs import state as execution
from laueportal.jobs.dispatch import CONVERT, RUN, ProcessRecord
from laueportal.jobs.state import JobStatus, RunPhase
from tests.indexing.test_viewer import add_run
from tests.run_support import claimed, enqueue_with, load_job, make_engine, publish_wire_run
from tests.worker_support import write_config

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


def _gone_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _stale_claimed(engine, tmp_path: Path, name: str, *, ago: timedelta = timedelta(hours=1)) -> int:
    """A run claimed by a dispatcher, no job process recorded, its last heartbeat ``ago``."""
    run = publish_wire_run(engine, tmp_path, name=name)
    claimed(engine, run.job_id)
    with Session(engine) as session, session.begin():
        job = session.get(models.Job, run.job_id)
        job.heartbeat_at = job.start_time = job.updated_at = utcnow() - ago
    return run.job_id


def _running_conversion(engine, indexing_id: int, *, started: datetime | None = None) -> None:
    """A claimed conversion (``running``), no job process recorded; started ``started`` (default: now)."""
    add_run(engine, indexing_id, None)
    conversion.enqueue_conversion(indexing_id, sessions=engine)
    assert dispatch.claim_conversion(engine, indexing_id)
    if started is not None:
        with Session(engine) as session, session.begin():
            session.get(Conversion, indexing_id).started_at = started


def _conversion(engine, indexing_id: int) -> Conversion:
    with Session(engine, expire_on_commit=False) as session:
        row = session.get(Conversion, indexing_id)
        assert row is not None
        session.expunge(row)
        return row


def test_a_stale_running_run_with_no_live_job_process_is_interrupted_and_nothing_else_changes(engine, tmp_path):
    """The two conditions: a stale heartbeat and no live job process (none recorded here). A Running run with a
    fresh heartbeat, a Queued run however old (its own queue entry: nothing checked, nothing put back) and a young
    published run are left as they are; a second pass changes nothing (a run is never resumed)."""
    stale = publish_wire_run(engine, tmp_path, name="stale")
    alive = publish_wire_run(engine, tmp_path, name="alive")
    queued = publish_wire_run(engine, tmp_path, name="queued")
    published = publish_wire_run(engine, tmp_path, name="published")
    for run in (stale, alive, queued):
        enqueue_with(engine, run.job_id)
    with Session(engine) as session, session.begin():
        stale_job = session.get(models.Job, stale.job_id)
        execution.mark_running(stale_job, now=NOW - timedelta(minutes=30))
        execution.record_progress(stale_job, succeeded=1, failed=1, now=NOW - timedelta(minutes=20))
        alive_job = session.get(models.Job, alive.job_id)
        execution.mark_running(alive_job, now=NOW - timedelta(minutes=30))
        execution.heartbeat(alive_job, now=NOW - timedelta(seconds=10))
        session.get(models.Job, queued.job_id).updated_at = NOW - timedelta(days=2)
        session.get(models.Job, published.job_id).updated_at = NOW - timedelta(minutes=1)
    before = load_job(engine, queued.job_id)

    result = reconciler.reconcile_runs(engine, stale_after_seconds=300, creation_timeout_seconds=600, now=NOW)

    assert (result.interrupted, result.abandoned, result.conversions_failed) == ([stale.job_id], [], [])
    stale_job = load_job(engine, stale.job_id)
    assert (stale_job.status, stale_job.phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)
    assert (stale_job.n_succeeded, stale_job.n_failed, stale_job.n_not_run) == (1, 1, 1)
    assert "Interrupted: the worker or job process died (no heartbeat since 2026-09-16T11:40:00" in stale_job.messages
    assert load_job(engine, alive.job_id).status == JobStatus.RUNNING
    after = load_job(engine, queued.job_id)
    assert (after.status, after.phase, after.updated_at, after.queue_job_id) == (
        JobStatus.QUEUED,
        RunPhase.QUEUED,
        before.updated_at,
        before.queue_job_id,
    )
    assert load_job(engine, published.job_id).phase == RunPhase.PUBLISHED
    assert not reconciler.reconcile_runs(engine, stale_after_seconds=300, creation_timeout_seconds=600, now=NOW)
    assert not hasattr(reconciler, "QueueView") and not hasattr(result, "requeued")


def test_a_stale_run_whose_recorded_job_process_lives_is_left_alone_until_it_dies(engine, tmp_path):
    """The job process the dispatcher recorded on the row (an orphan whose worker was killed, or a process that stopped)
    keeps its run, stale heartbeat or not; once it is gone, the cycle interrupts it."""
    job_id = _stale_claimed(engine, tmp_path, "recorded")
    process = subprocess.Popen(["sleep", "300"], start_new_session=True)
    policy = reconciler.ReconcilePolicy(stale_heartbeat_seconds=300)
    try:
        assert dispatch.record_process(engine, RUN, job_id, dispatch.process_record(process.pid))
        alive = reconciler.reconcile_once(engine, policy)
        status_while_alive = load_job(engine, job_id).status
    finally:
        process.kill()
        process.wait()
    gone = reconciler.reconcile_once(engine, policy)

    assert (alive.interrupted, status_while_alive) == ([], JobStatus.RUNNING)
    assert gone.interrupted == [job_id]
    job = load_job(engine, job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)


@pytest.mark.parametrize(
    "recorded",
    [
        pytest.param(lambda me: None, id="none-recorded"),
        pytest.param(lambda me: ProcessRecord(me.host, _gone_pid(), me.since_boot), id="exited"),
        pytest.param(lambda me: ProcessRecord(me.host, me.pid, me.since_boot - 60), id="pid-reused"),
        pytest.param(lambda me: ProcessRecord("another-host", me.pid, me.since_boot), id="another-host"),
    ],
)
def test_a_stale_run_without_a_live_recorded_job_process_is_interrupted(
    engine, tmp_path, recorded: Callable[[ProcessRecord], ProcessRecord | None]
):
    """None recorded counts as dead; so do a recorded pid that exited, a pid that is another process's now (this
    live process, with another start since boot) and a process on another host (this host cannot look at it)."""
    job_id = _stale_claimed(engine, tmp_path, "dead")
    record = recorded(dispatch.process_record(os.getpid()))
    if record is not None:
        assert dispatch.record_process(engine, RUN, job_id, record)

    result = reconciler.reconcile_once(engine, reconciler.ReconcilePolicy(stale_heartbeat_seconds=300))

    assert result.interrupted == [job_id]
    assert load_job(engine, job_id).phase == RunPhase.INTERRUPTED


def test_a_stale_run_whose_job_process_this_worker_started_is_left_alone_until_it_exits(engine, tmp_path):
    """The worker's own job processes count as its dispatcher counts them (``own``: its children, recorded on their
    rows or not yet, e.g. one whose record waits for a write-locked database). ``own`` is asked with no database
    connection held."""
    job_id = _stale_claimed(engine, tmp_path, "mine")
    held: list[int] = []

    def own() -> set[tuple[str, int]]:
        held.append(engine.pool.checkedout())
        return {(RUN, job_id), (CONVERT, 999)}

    policy = reconciler.ReconcilePolicy(stale_heartbeat_seconds=300)
    mine = reconciler.reconcile_once(engine, policy, own=own)
    status_while_mine = load_job(engine, job_id).status
    exited = reconciler.reconcile_once(engine, policy, own=set)

    assert (mine.interrupted, status_while_mine, held) == ([], JobStatus.RUNNING, [0])
    assert exited.interrupted == [job_id]


def test_a_running_conversion_whose_job_process_is_gone_is_failed_and_can_be_converted_again(engine):
    """A running conversion whose job process is gone (exited, a reused pid, or none recorded once its start is older
    than the stale limit) is ``failed`` with the interrupted message, and the run can be converted again. One whose
    recorded process lives, one just claimed (its record follows the claim), one this worker's dispatcher runs and a
    queued one are left alone."""
    me = dispatch.process_record(os.getpid())
    hour_ago = utcnow() - timedelta(hours=1)
    sleeper = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        for indexing_id in (101, 102, 104, 105, 107):
            _running_conversion(engine, indexing_id, started=hour_ago)
        _running_conversion(engine, 103)  # just claimed, its job process not recorded yet
        add_run(engine, 106, None)
        conversion.enqueue_conversion(106, sessions=engine)
        assert dispatch.record_process(engine, CONVERT, 101, ProcessRecord(me.host, _gone_pid(), me.since_boot))
        assert dispatch.record_process(engine, CONVERT, 102, dispatch.process_record(sleeper.pid))
        assert dispatch.record_process(engine, CONVERT, 105, ProcessRecord(me.host, me.pid, me.since_boot - 60))

        result = reconciler.reconcile_once(
            engine, reconciler.ReconcilePolicy(stale_heartbeat_seconds=300), own=lambda: {(CONVERT, 107)}
        )
        statuses = {indexing_id: _conversion(engine, indexing_id).status for indexing_id in range(101, 108)}
    finally:
        sleeper.kill()
        sleeper.wait()

    assert sorted(result.conversions_failed) == [101, 104, 105]
    assert (result.interrupted, result.abandoned) == ([], [])
    assert statuses == {
        101: "failed",
        102: "running",
        103: "running",
        104: "failed",
        105: "failed",
        106: "queued",
        107: "running",
    }
    failed = _conversion(engine, 104)
    assert failed.message == "Interrupted: the worker or job process died; convert the results again"
    assert failed.ended_at is not None and failed.ended_at >= hour_ago
    artifact = conversion.find_conversion(101, sessions=engine)
    assert artifact is not None and artifact.status == "failed"

    again = conversion.enqueue_conversion(101, sessions=engine)

    assert (again.status, again.message, again.started_at, again.ended_at) == ("queued", None, None, None)
    with Session(engine) as session:
        assert dispatch.queued_in_order(session) == [(CONVERT, 106), (CONVERT, 101)]  # queued again, after 106


def test_a_run_left_in_created_or_published_is_finalized_and_its_run_directory_kept(engine, tmp_path):
    """Past ``creation_timeout_seconds`` without a change, a run that never reached the queue is Failed ("creation did
    not complete"), a remote run that never reached its submission too (no one else would end it); a younger one, a
    queued one and a remote run being submitted (the poller's) are not."""
    published = publish_wire_run(engine, tmp_path, name="published")
    young = publish_wire_run(engine, tmp_path, name="young")
    queued = publish_wire_run(engine, tmp_path, name="queued")
    enqueue_with(engine, queued.job_id)
    created = publish_wire_run(engine, tmp_path, name="created")
    submitting = publish_wire_run(engine, tmp_path, name="submitting")
    unsubmitted = publish_wire_run(engine, tmp_path, name="unsubmitted")
    with Session(engine) as session, session.begin():
        for run in (published, queued, submitting, unsubmitted):
            session.get(models.Job, run.job_id).updated_at = NOW - timedelta(minutes=11)
        session.get(models.Job, unsubmitted.job_id).backend = "crux"  # its submission never started
        session.get(models.Job, young.job_id).updated_at = NOW - timedelta(minutes=9)
        creating = session.get(models.Job, created.job_id)
        creating.phase = RunPhase.CREATED  # the creation stopped before its manifest was published
        creating.updated_at = NOW - timedelta(hours=2)
        remote = session.get(models.Job, submitting.job_id)
        remote.backend, remote.submission_id = "crux", "6f1c2a4e-3b5d-4e7f-8a9b-0c1d2e3f4a5b"  # the poller's

    result = reconciler.reconcile_runs(engine, stale_after_seconds=300, creation_timeout_seconds=600, now=NOW)

    assert sorted(result.abandoned) == sorted([published.job_id, created.job_id, unsubmitted.job_id])
    for run, phase in ((published, "published"), (created, "created"), (unsubmitted, "published")):
        job = load_job(engine, run.job_id)
        assert (job.status, job.phase, job.n_not_run) == (JobStatus.FAILED, RunPhase.FAILED, 3)
        assert job.messages.splitlines()[-1].startswith(f"Creation did not complete: the run is still {phase} since")
    assert Path(load_job(engine, published.job_id).run_directory).is_dir()  # kept for diagnosis
    assert load_job(engine, young.job_id).phase == RunPhase.PUBLISHED
    assert load_job(engine, queued.job_id).phase == RunPhase.QUEUED
    assert load_job(engine, submitting.job_id).phase == RunPhase.PUBLISHED


def test_the_cycle_leaves_alone_the_rows_that_moved_on_between_its_read_and_its_change(engine, tmp_path):
    """The candidates are re-read in the final (write) transaction and changed only while still as read: a run
    that finished, showed a sign of life or got a live job process recorded meanwhile, a conversion that ended by
    itself and a stuck run queued meanwhile keep their new state; the unchanged candidates are finalized."""
    finished, revived, recorded, stale = (
        _stale_claimed(engine, tmp_path, name) for name in ("finished", "revived", "recorded", "stale")
    )
    stuck = publish_wire_run(engine, tmp_path, name="stuck").job_id
    queued_meanwhile = publish_wire_run(engine, tmp_path, name="queued").job_id
    with Session(engine) as session, session.begin():
        for job_id in (stuck, queued_meanwhile):
            session.get(models.Job, job_id).updated_at = utcnow() - timedelta(hours=1)
    hour_ago = utcnow() - timedelta(hours=1)
    _running_conversion(engine, 201, started=hour_ago)  # ends by itself meanwhile
    _running_conversion(engine, 202, started=hour_ago)
    sleeper = subprocess.Popen(["sleep", "300"], start_new_session=True)
    acted: list[bool] = []

    def meanwhile() -> set[tuple[str, int]]:
        """Other processes act while the cycle is between its read and its final transaction."""
        with Session(engine) as session, session.begin():
            finishing = session.get(models.Job, finished)
            execution.record_progress(finishing, succeeded=3, failed=0)
            execution.finalize(finishing, status=JobStatus.FINISHED)
            execution.heartbeat(session.get(models.Job, revived))
            execution.mark_queued(session.get(models.Job, queued_meanwhile), f"run_{queued_meanwhile}")
            ended = session.get(Conversion, 201)
            ended.status, ended.message, ended.ended_at = "finished", "converted: 4 frames, 5 patterns", utcnow()
        assert dispatch.record_process(engine, RUN, recorded, dispatch.process_record(sleeper.pid))
        acted.append(True)
        return set()

    try:
        result = reconciler.reconcile_runs(engine, stale_after_seconds=300, creation_timeout_seconds=600, own=meanwhile)
        states = {job_id: load_job(engine, job_id).phase for job_id in (finished, revived, recorded, stale, stuck)}
    finally:
        sleeper.kill()
        sleeper.wait()

    assert acted == [True]
    assert (result.interrupted, result.abandoned, result.conversions_failed) == ([stale], [stuck], [202])
    assert states == {
        finished: RunPhase.FINISHED,
        revived: RunPhase.RUNNING,
        recorded: RunPhase.RUNNING,
        stale: RunPhase.INTERRUPTED,
        stuck: RunPhase.FAILED,
    }
    assert load_job(engine, queued_meanwhile).phase == RunPhase.QUEUED
    assert "Interrupted" not in (load_job(engine, finished).messages or "")
    assert (_conversion(engine, 201).status, _conversion(engine, 202).status) == ("finished", "failed")
    assert _conversion(engine, 201).message == "converted: 4 frames, 5 patterns"


def test_laueportal_jobs_reconcile_once_runs_the_same_cycle(tmp_path, monkeypatch, capsys):
    """``laueportal jobs reconcile --once``: one cycle of the worker's reconciler on the configured database, which
    prints what it changed and exits 0."""
    engine = make_engine(tmp_path, "cli.db")
    try:
        job_id = _stale_claimed(engine, tmp_path, "stale")
        stuck = publish_wire_run(engine, tmp_path, name="stuck").job_id
        with Session(engine) as session, session.begin():
            session.get(models.Job, stuck).updated_at = utcnow() - timedelta(hours=1)
        _running_conversion(engine, 301, started=utcnow() - timedelta(hours=1))
        config = write_config(tmp_path / "cli.toml", database=tmp_path / "cli.db", runs_root=tmp_path / "runs")
        for name in [name for name in os.environ if name.upper().startswith("LAUEPORTAL_")]:
            monkeypatch.delenv(name)
        monkeypatch.setenv("LAUEPORTAL_CONFIG", str(config))
        monkeypatch.setattr("laueportal.core.logging.configure_logging", lambda settings: None)
        get_settings.cache_clear()

        status = cli.main(["jobs", "reconcile", "--once"])

        assert status == 0
        assert capsys.readouterr().out == (
            f"interrupted [{job_id}]; stuck in creation [{stuck}]; conversions failed [301]\n"
        )
        assert load_job(engine, job_id).phase == RunPhase.INTERRUPTED
        assert _conversion(engine, 301).status == "failed"
    finally:
        get_settings.cache_clear()
        engine.dispose()
