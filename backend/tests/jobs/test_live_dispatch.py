"""The dispatcher's live rules (``jobs.dispatch``): two live slots beside the batch slot, each kind's orphans counted in
its own; no batch run or conversion claimed while a live run is Queued or Running; the batch slot's run stopped
("stopped for a live run": Cancelled), its conversion stopped (``failed``), the live runs started once its process has
exited, a run that does not drain killed after ``live.batch_stop_seconds`` and only then ended; the live runs' cores;
one live session; Move to Front refused; remote runs unaffected.

The fast tests run the dispatcher in this process with stand-in job processes (``test_dispatch.FakeJobs``); the
``slow`` ones a real worker and its job processes with the test computes (``tests.jobs.job_computes``). The faults
with a live run in the mix (the worker killed, a batch run that will not drain, a live run created in the
claim/record/go window) are ``test_faults.py``'s.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal.app import create_app
from laueportal.indexing import conversion
from laueportal.jobs import controls, dispatch, reconciler, state
from laueportal.jobs.dispatch import CONVERT, RUN, Dispatcher, DispatchPolicy, LivePolicy
from laueportal.jobs.live import LiveSessionOpen, check_one_live_session
from laueportal.jobs.models import Job
from laueportal.jobs.schemas import MoveState
from laueportal.jobs.state import JobStatus, RunPhase
from tests.api_support import PORTAL_HEADERS
from tests.indexing.test_viewer import add_run
from tests.jobs import job_computes
from tests.jobs.live_support import create_live_follower, create_live_wire_run
from tests.jobs.test_dispatch import POLICY, FakeJobs, _drain
from tests.run_support import dispatch_queue, enqueue_with, load_job, publish_wire_run
from tests.settings_support import make_database
from tests.worker_support import Live, alive, start_worker, wait_for

RUNS_ON = ["sh", "-c", "read line; exec sleep 300"]
"""A stand-in job process that, told to go, runs (sleeps) until it is killed: deaf to its stop flag."""


@pytest.fixture
def setup(tmp_path):
    settings, engine = make_database(tmp_path)
    yield settings, engine
    engine.dispose()


@pytest.fixture
def engine(setup):
    return setup[1]


def _queued(engine, tmp_path: Path, name: str, **options) -> int:
    run = publish_wire_run(engine, tmp_path, name=name, **options)
    enqueue_with(engine, run.job_id)
    return run.job_id


def _end(engine, job_id: int, status: JobStatus = JobStatus.CANCELLED, message: str = "ended by the test") -> None:
    """End a run as its job process would have (the stand-ins never do)."""
    with Session(engine) as session, session.begin():
        state.finalize(session.get(Job, job_id), status=status, message=message)


def _orphan(engine, kind, key: int) -> subprocess.Popen[bytes]:
    """A job process an earlier worker started: alive, in a session of its own, and recorded on the claimed row."""
    process = subprocess.Popen(["sleep", "300"], start_new_session=True)
    assert dispatch.record_process(engine, kind, key, dispatch.process_record(process.pid))
    return process


def _kill(process: subprocess.Popen[bytes]) -> None:
    process.kill()
    process.wait(10)


# ---------------------------------------------------------------------------
# The slots


def test_two_live_slots_beside_the_batch_slot_each_kind_s_orphans_counted_in_its_own(engine, tmp_path):
    """An earlier worker left a batch run's and a live run's job processes running: each holds its own kind's slot.
    The batch orphan holds the batch slot (stopped for the live run; the live runs wait for it), the live orphan one
    live slot, so of two Queued live runs one starts, then the other once the live orphan is gone. A live run's job
    process, still exiting after its run ended, never holds the batch slot."""
    batch = _queued(engine, tmp_path, "batch")
    assert dispatch.claim_run(engine, batch)
    first = create_live_wire_run(engine, tmp_path, name="first").job_id
    assert dispatch.claim_run(engine, first)
    batch_orphan, live_orphan = _orphan(engine, RUN, batch), _orphan(engine, RUN, first)
    second = create_live_wire_run(engine, tmp_path, name="second").job_id
    third = create_live_wire_run(engine, tmp_path, name="third").job_id
    fake = FakeJobs(tmp_path, RUNS_ON)
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    try:
        held = dispatcher.cycle()  # the batch orphan still runs: no live run starts beside it
        orphans = sorted((orphan.key, orphan.live) for orphan in dispatcher.orphans())
        _end(engine, batch)  # it drained
        _kill(batch_orphan)
        one = dispatcher.cycle()
        full = dispatcher.cycle()
        free_before = dispatcher.live_slots_free()
        _end(engine, first)
        _kill(live_orphan)
        other = dispatcher.cycle()
        for job_id in (second, third):  # the session ends; their job processes still run
            _end(engine, job_id)
        after = _queued(engine, tmp_path, "after")
        batch_again = dispatcher.cycle()
    finally:
        fake.cleanup()
        for process in (batch_orphan, live_orphan):
            _kill(process)

    assert orphans == [(batch, False), (first, True)]
    assert (held, one, full, free_before, other) == (None, RUN, None, 0, RUN)
    assert batch_again == RUN
    assert fake.started == [(RUN, second), (RUN, third), (RUN, after)]


def test_while_a_live_run_is_open_no_batch_run_or_conversion_is_claimed_and_they_wait_in_order(engine, tmp_path):
    """Queued batch runs and a queued conversion wait while a live run is Queued or Running (the claims refuse them
    in their own transaction too); once the live run has ended, they run in the dispatcher's order."""
    first, second = _queued(engine, tmp_path, "first"), _queued(engine, tmp_path, "second")
    add_run(engine, 101, None)
    conversion.enqueue_conversion(101, sessions=engine)
    live = create_live_wire_run(engine, tmp_path).job_id
    fake = FakeJobs(tmp_path)
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    try:
        while_queued = (dispatch.claim_run(engine, first), dispatch.claim_conversion(engine, 101))
        started = dispatcher.cycle()
        next(iter(dispatcher.children.values())).process.wait(10)  # it exited; its run is still Running
        held = [dispatcher.cycle() for _ in range(3)]
        while_running = (dispatch.claim_run(engine, first), dispatch.claim_conversion(engine, 101))
        waiting = dispatch_queue(engine)
        _end(engine, live)
        _drain(dispatcher)
    finally:
        fake.cleanup()

    assert while_queued == (False, False) and while_running == (False, False)
    assert (started, held) == (RUN, [None, None, None])
    assert waiting == [(CONVERT, 101), (RUN, first), (RUN, second)]
    assert fake.started == [(RUN, live), (CONVERT, 101), (RUN, first), (RUN, second)]


def test_a_queued_batch_run_without_a_queue_identity_is_still_the_batch_slot_s(engine, tmp_path):
    """The live runs are told apart by their queue identity; a Queued local run with none (no code path leaves one, but
    SQL's NOT of a NULL comparison is NULL) is still taken by the batch slot, never lost."""
    batch = _queued(engine, tmp_path, "batch")
    with Session(engine) as session, session.begin():
        session.get(Job, batch).queue_job_id = None
    fake = FakeJobs(tmp_path)
    try:
        started = Dispatcher(engine, POLICY, start=fake).cycle()
    finally:
        fake.cleanup()
    assert (started, fake.started) == (RUN, [(RUN, batch)])


# ---------------------------------------------------------------------------
# The batch slot's item, stopped for a live run


def test_a_running_batch_run_gets_the_stop_flag_for_a_live_run_which_starts_once_its_job_process_has_exited(
    engine, tmp_path
):
    """Stopped, not paused. The running batch run gets the stop flag, once, with the reason "stopped for a live run"
    (its executor then drains it to Cancelled: the slow test below); the live run waits while the batch run's job
    process lives and starts at the first cycle after it has exited."""
    batch, waiting = _queued(engine, tmp_path, "batch"), _queued(engine, tmp_path, "waiting")
    fake = FakeJobs(tmp_path, RUNS_ON)
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    try:
        assert dispatcher.cycle() == RUN
        live = create_live_wire_run(engine, tmp_path).job_id
        held = [dispatcher.cycle() for _ in range(3)]
        flagged = load_job(engine, batch)
        live_held = load_job(engine, live).status
        _end(engine, batch, message="Cancelled by request; completed results kept")  # its executor's end
        _kill(fake.processes[0])
        started = dispatcher.cycle()
    finally:
        fake.cleanup()

    assert held == [None, None, None] and live_held == JobStatus.QUEUED
    assert (flagged.status, flagged.cancel_requested_at is not None) == (JobStatus.RUNNING, True)
    assert (flagged.messages or "").count("Cancellation requested") == 1
    assert (flagged.messages or "").endswith("Cancellation requested: stopped for a live run")
    assert started == RUN and fake.started == [(RUN, batch), (RUN, live)]
    assert load_job(engine, waiting).status == JobStatus.QUEUED


def test_a_running_conversion_is_stopped_for_a_live_run_failed_and_can_be_converted_again(engine, tmp_path):
    """A conversion cannot stop part way: its job process's group is killed at once, the conversion ends ``failed``
    once the process is gone ("stopped for a live run; convert the results again"), and the live run starts in the
    same cycle. Converting again queues it."""
    add_run(engine, 101, None)
    conversion.enqueue_conversion(101, sessions=engine)
    fake = FakeJobs(tmp_path, RUNS_ON)
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    try:
        assert dispatcher.cycle() == CONVERT
        process = fake.processes[0]
        live = create_live_wire_run(engine, tmp_path).job_id
        started = dispatcher.cycle()
        stopped = conversion.find_conversion(101, sessions=engine)
        again = conversion.enqueue_conversion(101, sessions=engine)
    finally:
        fake.cleanup()

    assert process.returncode == -signal.SIGKILL
    assert stopped is not None and (stopped.status, stopped.message) == (
        "failed",
        "Stopped for a live run; convert the results again",
    )
    assert started == RUN and fake.started == [(CONVERT, 101), (RUN, live)]
    assert again.status == "queued"


def _clocked(monkeypatch) -> list[float]:
    """The dispatcher's clock, as the test sets it (seconds); the kill's own wait is in real time."""
    now = [0.0]
    monkeypatch.setattr(dispatch, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def test_a_batch_run_alive_past_batch_stop_seconds_has_its_group_killed_then_ends_cancelled(
    engine, tmp_path, monkeypatch
):
    """A batch run deaf to its stop flag: at ``live.batch_stop_seconds`` after the flag it still runs; past them the
    dispatcher kills its job process's group (the process and what it started), then ends it Cancelled with that
    message, and the live run starts."""
    batch = _queued(engine, tmp_path, "stubborn")
    left = tmp_path / "left.pid"
    fake = FakeJobs(tmp_path, ["sh", "-c", f"read line; sleep 300 & echo $! > {left}; wait"])
    policy = DispatchPolicy(dispatch_seconds=0.05, heartbeat_seconds=0.05, live=LivePolicy(batch_stop_seconds=60))
    dispatcher = Dispatcher(engine, policy, start=fake)
    now = _clocked(monkeypatch)
    try:
        assert dispatcher.cycle() == RUN
        assert wait_for(lambda: left.exists() and left.read_text().strip() != "", timeout=10)
        grandchild = int(left.read_text())
        live = create_live_wire_run(engine, tmp_path).job_id
        dispatcher.cycle()  # the stop flag, at 0
        now[0] = 60.0
        at_the_limit = (dispatcher.cycle(), fake.processes[0].poll(), load_job(engine, batch).status)
        now[0] = 60.5
        past_it = dispatcher.cycle()
        killed = (fake.processes[0].poll(), wait_for(lambda: not alive(grandchild), timeout=10))
    finally:
        fake.cleanup()

    assert at_the_limit == (None, None, JobStatus.RUNNING)
    assert killed == (-signal.SIGKILL, True)
    job = load_job(engine, batch)
    assert (job.status, job.phase, job.n_not_run) == (JobStatus.CANCELLED, RunPhase.CANCELLED, 3)
    assert (job.messages or "").splitlines()[-2:] == [
        "Cancellation requested: stopped for a live run",
        "Stopped for a live run: its job process was killed after 60 s",
    ]
    assert past_it == RUN and fake.started == [(RUN, batch), (RUN, live)]


def test_a_batch_run_stopped_for_a_live_run_is_never_ended_while_its_process_lives_nor_left_running_without_one(
    engine, tmp_path, monkeypatch
):
    """The kill's deadline and the reconciler agree. A kill that has not taken yet: the run stays Running, the live
    run waits, and the worker's reconciler leaves the run alone (its dispatcher holds it), stale or not. A kill
    that took but whose end could not be written (the database write-locked): the run, Running with no process, is
    still the dispatcher's, the reconciler leaves it alone, and the next cycle ends it."""
    batch = _queued(engine, tmp_path, "stubborn")
    fake = FakeJobs(tmp_path, RUNS_ON)
    policy = DispatchPolicy(dispatch_seconds=0.05, heartbeat_seconds=0.05, live=LivePolicy(batch_stop_seconds=1))
    dispatcher = Dispatcher(engine, policy, start=fake)
    now = _clocked(monkeypatch)
    monkeypatch.setattr(dispatch, "KILL_WAIT_SECONDS", 0.2)
    kill_group, end = dispatch._kill_group, dispatch.end_stopped_for_live
    try:
        assert dispatcher.cycle() == RUN
        process = fake.processes[0]
        live = create_live_wire_run(engine, tmp_path).job_id
        dispatcher.cycle()
        now[0] = 2.0
        monkeypatch.setattr(dispatch, "_kill_group", lambda pid: None)  # the kill has not taken
        not_taken = [dispatcher.cycle() for _ in range(2)]
        alive_then = (process.poll(), load_job(engine, batch).status, load_job(engine, live).status)
        left_alone = reconciler.reconcile_runs(engine, stale_after_seconds=0, own=dispatcher.running_items)

        monkeypatch.setattr(dispatch, "_kill_group", kill_group)

        def locked(*args, **kwargs):
            raise OperationalError("UPDATE job", {}, Exception("database is locked"))

        monkeypatch.setattr(dispatch, "end_stopped_for_live", locked)
        unwritten = dispatcher.cycle()  # killed and gone; its end raises: the cycle ends, logged
        gone_then = (process.poll(), load_job(engine, batch).status)
        still_left_alone = reconciler.reconcile_runs(engine, stale_after_seconds=0, own=dispatcher.running_items)
        monkeypatch.setattr(dispatch, "end_stopped_for_live", end)
        ended = dispatcher.cycle()
    finally:
        fake.cleanup()

    assert not_taken == [None, None]
    assert alive_then == (None, JobStatus.RUNNING, JobStatus.QUEUED) and not left_alone
    assert unwritten is None and gone_then == (-signal.SIGKILL, JobStatus.RUNNING) and not still_left_alone
    job = load_job(engine, batch)
    assert (job.status, (job.messages or "").splitlines()[-1]) == (
        JobStatus.CANCELLED,
        "Stopped for a live run: its job process was killed after 1 s",
    )
    assert ended == RUN and fake.started == [(RUN, batch), (RUN, live)]


def test_a_queued_batch_run_or_conversion_held_by_an_open_live_session_says_so_until_it_has_ended(setup, tmp_path):
    """The Run Monitor's and the job page's ``held``, and a conversion's (found, and in the answers that queue it),
    while a live run is open; not for a running batch run, a remote one or the live run itself; no longer once the
    session has ended."""
    settings, engine = setup
    running = _queued(engine, tmp_path, "running")
    assert dispatch.claim_run(engine, running)
    queued = _queued(engine, tmp_path, "queued")
    add_run(engine, 101, None)
    conversion.enqueue_conversion(101, sessions=engine)
    live = create_live_wire_run(engine, tmp_path).job_id

    def held() -> tuple[dict[int, bool], bool, bool, bool]:
        with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
            rows = client.get("/api/v1/jobs").json()["rows"]
            page = client.get(f"/api/v1/jobs/{queued}").json()
        found = conversion.find_conversion(101, sessions=engine)
        assert found is not None
        again = conversion.enqueue_conversion(101, sessions=engine)  # already queued: the answer is that one
        shown = {row["job_id"]: row["held"] for row in rows}
        return {key: shown[key] for key in (running, queued, live)}, page["held"], found.held, again.held

    while_open = held()
    add_run(engine, 501, None)
    queued_while_open = conversion.enqueue_conversion(501, sessions=engine).held
    _end(engine, live)
    after = held()
    assert while_open == ({running: False, queued: True, live: False}, True, True, True)
    assert queued_while_open
    assert after == ({running: False, queued: False, live: False}, False, False, False)


# ---------------------------------------------------------------------------
# Stop now


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("queued", "cancelled"),
        ("running", "requested"),
        ("stopping", "stopping_now"),
        ("batch", "not_live"),
        ("ended", "already_terminal"),
        ("unknown", "not_found"),
    ],
)
def test_stop_now_stops_a_live_run_not_stopped_yet_and_records_its_stop_now_once_it_is(
    engine, tmp_path, case, expected
):
    """``controls.stop_run_now``: a live run not stopped yet is stopped as Stop does (Queued: Cancelled; Running: its
    stop flag, it drains); one Running whose stop was requested gets its Stop now line (once), nothing else changed;
    a batch run is ``not_live``."""
    job_id = 999 if case == "unknown" else create_live_wire_run(engine, tmp_path).job_id
    if case == "batch":
        job_id = _queued(engine, tmp_path, "batch")
    if case in ("running", "stopping", "ended"):
        assert dispatch.claim_run(engine, job_id)
    if case in ("stopping", "ended"):
        controls.cancel_run(job_id, engine=engine)
    if case == "ended":
        _end(engine, job_id)

    results = [controls.stop_run_now(job_id, engine=engine).state for _ in range(2 if case == "stopping" else 1)]

    assert results[0] == expected
    if case == "unknown":
        return
    job = load_job(engine, job_id)
    lines = (job.messages or "").splitlines()
    assert lines.count(state.STOP_NOW_REQUESTED) == (1 if case == "stopping" else 0)
    status = {"queued": JobStatus.CANCELLED, "batch": JobStatus.QUEUED, "ended": JobStatus.CANCELLED}
    assert job.status == status.get(case, JobStatus.RUNNING)
    assert (job.cancel_requested_at is not None) == (case != "batch")


def test_stop_now_s_route_answers_the_state_404_and_409_for_a_batch_run(setup, tmp_path):
    settings, engine = setup
    live = create_live_wire_run(engine, tmp_path).job_id
    batch = _queued(engine, tmp_path, "batch")
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        answers = [client.post(f"/api/v1/jobs/{job_id}/stop-now") for job_id in (live, 999, batch)]
    assert [answer.status_code for answer in answers] == [200, 404, 409]
    assert answers[0].json() == {"job_id": live, "state": "cancelled", "n_pending": 0}
    assert answers[2].json()["title"] == "Not a live run"
    assert load_job(engine, batch).status == JobStatus.QUEUED


@pytest.mark.parametrize("whose", ["child", "orphan", "gone"])
def test_stop_now_kills_the_live_run_s_job_process_and_only_then_ends_it_failed_interrupted(
    engine, tmp_path, monkeypatch, whose
):
    """The dispatcher kills the job process of a live run whose Stop now is recorded (its child, or a recorded
    orphan; one already gone is ended at once), never ends it while it lives (the reconciler leaves it alone: the
    dispatcher holds it), and ends it Failed, ``interrupted``, once it is gone (a run whose drain ended by itself
    first: the next test)."""
    run = create_live_wire_run(engine, tmp_path)
    fake = FakeJobs(tmp_path, RUNS_ON)
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    monkeypatch.setattr(dispatch, "KILL_WAIT_SECONDS", 0.5)
    process = None
    try:
        if whose == "child":
            assert dispatcher.cycle() == RUN
            process = fake.processes[0]
        else:
            assert dispatch.claim_run(engine, run.job_id)
            process = _orphan(engine, RUN, run.job_id)
            if whose == "gone":
                _kill(process)
        controls.cancel_run(run.job_id, engine=engine)
        assert controls.stop_run_now(run.job_id, engine=engine).state.value == "stopping_now"
        kill_group = dispatch._kill_group
        monkeypatch.setattr(dispatch, "_kill_group", lambda pid: None)  # the kill has not taken yet
        dispatcher.cycle()
        not_taken = load_job(engine, run.job_id).status
        held = (RUN, run.job_id) in dispatcher.running_items()
        left_alone = reconciler.reconcile_runs(engine, stale_after_seconds=0, own=dispatcher.running_items)
        monkeypatch.setattr(dispatch, "_kill_group", kill_group)
        dispatcher.cycle()
    finally:
        fake.cleanup()
        if process is not None and process.poll() is None:
            _kill(process)
    job = load_job(engine, run.job_id)
    if whose == "gone":
        assert not_taken == JobStatus.FAILED  # its process known gone: ended at once
    else:
        assert (not_taken, held, bool(left_alone)) == (JobStatus.RUNNING, True, False)
        assert process.poll() is not None
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)
    assert job.messages.splitlines()[-1] == dispatch.STOPPED_NOW and dispatcher.stopping_now == {}


def test_a_live_run_whose_drain_ends_as_its_stop_now_is_acted_on_is_left_as_it_ended(engine, tmp_path, monkeypatch):
    """The job process commits its own end (Finished) after the dispatcher read the Stop now requests and before it ends
    the run (here: as the kill is sent). The run stays Finished with its own message, no ``STOPPED_NOW`` line is added,
    the cycle raises nothing, and nothing is left in ``stopping_now`` (``end_stopped_now``'s guard)."""
    run = create_live_wire_run(engine, tmp_path)
    fake = FakeJobs(tmp_path, RUNS_ON)
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    monkeypatch.setattr(dispatch, "KILL_WAIT_SECONDS", 0.5)
    kill_group = dispatch._kill_group

    def drained_then_killed(pid: int) -> None:
        _end(engine, run.job_id, JobStatus.FINISHED, "drained by itself")  # its own end, committed first
        kill_group(pid)

    try:
        assert dispatcher.cycle() == RUN
        controls.cancel_run(run.job_id, engine=engine)
        assert controls.stop_run_now(run.job_id, engine=engine).state.value == "stopping_now"
        monkeypatch.setattr(dispatch, "_kill_group", drained_then_killed)
        dispatcher.cycle()
        assert fake.processes[0].poll() is not None
        dispatcher.cycle()
    finally:
        fake.cleanup()
    job = load_job(engine, run.job_id)
    assert (job.status, job.messages.splitlines()[-1]) == (JobStatus.FINISHED, "drained by itself")
    assert dispatch.STOPPED_NOW not in job.messages and dispatcher.stopping_now == {}


# ---------------------------------------------------------------------------
# The cores


@pytest.mark.parametrize(
    ("cpu_count", "pair", "alone"),
    [(8, (4, 2), 6), (4, (2, 1), 3), (1, (1, 1), 1)],
    ids=["8 cores", "4 cores", "1 core"],
)
def test_the_live_runs_share_the_live_cores_the_wire_run_takes_its_part_each_at_least_one(
    setup, tmp_path, cpu_count, pair, alone
):
    """``floor(cpu_count × cpu_share)`` (0.75; at least 1) shared: with both runs, the wire run takes
    ``round(share × wire_share)`` (0.67) and its follower the rest, each at least 1; a run alone takes the whole
    share. The dispatcher passes each its count (``start(kind, key, cores=...)``); a batch item gets none."""
    _, engine = setup
    wire = create_live_wire_run(engine, tmp_path, name="wire")
    follower = create_live_follower(engine, tmp_path, wire)
    fake = FakeJobs(tmp_path, RUNS_ON)
    policy = DispatchPolicy(dispatch_seconds=0.05, heartbeat_seconds=0.05, live=LivePolicy(cpu_count=cpu_count))
    dispatcher = Dispatcher(engine, policy, start=fake)
    try:
        dispatcher.cycle()
        for job_id, process in zip((wire.job_id, follower), fake.processes, strict=True):
            _end(engine, job_id)
            _kill(process)
        single = create_live_wire_run(engine, tmp_path, name="single").job_id
        dispatcher.cycle()
        _end(engine, single)
        _kill(fake.processes[-1])
        batch = _queued(engine, tmp_path, "batch")
        dispatcher.cycle()
    finally:
        fake.cleanup()

    assert fake.started == [(RUN, wire.job_id), (RUN, follower), (RUN, single), (RUN, batch)]
    assert fake.cores == {wire.job_id: pair[0], follower: pair[1], single: alone, batch: None}


# ---------------------------------------------------------------------------
# One live session; Move to Front; remote runs


def test_a_live_create_is_refused_while_a_live_run_is_queued_or_running_naming_it(engine, tmp_path):
    """One live session at a time: the check Start calls refuses while a live run is Queued or Running and names the
    oldest; an ended live run or an open batch run does not count."""
    batch = _queued(engine, tmp_path, "batch")

    def refusal() -> LiveSessionOpen | None:
        with Session(engine) as session:
            try:
                check_one_live_session(session)
            except LiveSessionOpen as refused:
                return refused
        return None

    before = refusal()
    wire = create_live_wire_run(engine, tmp_path)
    follower = create_live_follower(engine, tmp_path, wire)
    queued = refusal()
    assert dispatch.claim_run(engine, wire.job_id)
    running = refusal()
    _end(engine, wire.job_id)
    following = refusal()
    controls.cancel_run(follower, engine=engine)
    after = refusal()

    assert before is None and after is None and load_job(engine, batch).status == JobStatus.QUEUED
    assert queued is not None and (queued.job_id, queued.status) == (wire.job_id, JobStatus.QUEUED)
    assert str(queued) == f"Live run job {wire.job_id} is Queued: one live session at a time"
    assert running is not None and (running.job_id, running.status) == (wire.job_id, JobStatus.RUNNING)
    assert following is not None and following.job_id == follower


def test_move_to_front_of_a_live_run_is_refused_with_a_409_and_nothing_moves(setup, tmp_path):
    settings, engine = setup
    live = create_live_wire_run(engine, tmp_path).job_id

    result = controls.move_run_to_front(live, engine=engine)
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        refused = client.post(f"/api/v1/jobs/{live}/move-to-front")

    assert result.state == MoveState.LIVE
    assert (refused.status_code, refused.json()["title"]) == (409, "Live run")
    assert f"Job {live} is a live run" in refused.json()["detail"]
    assert load_job(engine, live).moved_to_front_at is None


def test_remote_runs_are_neither_held_nor_stopped_by_an_open_live_run(engine, tmp_path):
    """A remote run is its target's: with a live run open, a running one gets no stop flag and a queued one stays
    as it is (the dispatcher never claims either); only the local batch run is stopped."""
    remote_running = publish_wire_run(engine, tmp_path, name="remote-running").job_id
    remote_queued = publish_wire_run(engine, tmp_path, name="remote-queued").job_id
    with Session(engine) as session, session.begin():
        for job_id, status, phase in (
            (remote_running, JobStatus.RUNNING, RunPhase.RUNNING),
            (remote_queued, JobStatus.QUEUED, RunPhase.QUEUED),
        ):
            job = session.get(Job, job_id)
            job.backend, job.submission_id, job.status, job.phase = "crux", f"uuid-{job_id}", int(status), phase
    batch = _queued(engine, tmp_path, "batch")
    fake = FakeJobs(tmp_path, RUNS_ON)
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    try:
        assert dispatcher.cycle() == RUN
        create_live_wire_run(engine, tmp_path)
        dispatcher.cycle()
    finally:
        fake.cleanup()

    assert load_job(engine, batch).cancel_requested_at is not None
    for job_id, status in ((remote_running, JobStatus.RUNNING), (remote_queued, JobStatus.QUEUED)):
        job = load_job(engine, job_id)
        assert (job.status, job.cancel_requested_at) == (status, None)


# ---------------------------------------------------------------------------
# Through a real worker and its job processes (slow)


def _config(live: Live, tmp_path: Path, **values) -> Path:
    return live.write_config(tmp_path / "live-rules.toml", live={"batch_stop_seconds": 30, **values})


class _Overlap:
    """Samples, every 10 ms in a thread, whether the live run ran while the batch run's job process lived."""

    def __init__(self, engine, live_job: int, batch_pid: int) -> None:
        self.engine, self.live_job, self.batch_pid = engine, live_job, batch_pid
        self.samples: list[tuple[bool, int]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()

    def _sample(self) -> None:
        while not self._stop.is_set():
            batch_alive = alive(self.batch_pid)
            self.samples.append((batch_alive, load_job(self.engine, self.live_job).status))
            self._stop.wait(0.01)

    def stop(self) -> list[tuple[bool, int]]:
        self._stop.set()
        self._thread.join(10)
        return self.samples


@pytest.mark.slow
def test_a_running_batch_run_stopped_for_a_live_run_drains_to_cancelled_and_the_live_run_starts_with_its_cores(
    tmp_path, live
):
    """Through the worker: the running batch run's executor sees the stop flag and drains it to Cancelled (the
    reason in its messages; re-run by Clone); the live run starts only once that job process has exited, its
    compute given its cores (a run alone: the whole live share); the queued batch run waits, and runs once the live
    run has been stopped."""
    engine = live.engine
    batch = _queued(engine, tmp_path, "batch", count=2, notes=job_computes.BLOCK)
    waiting = _queued(engine, tmp_path, "waiting")
    worker = start_worker(
        _config(live, tmp_path, cpu_share=0.5), log=tmp_path / "worker.log", burst=False, test_computes=True
    )
    overlap: _Overlap | None = None
    try:
        marker = Path(load_job(engine, batch).run_directory or "") / job_computes.COMPUTE_MARKER
        assert wait_for(marker.exists, timeout=60), worker.log_text()
        batch_process = json.loads(marker.read_text())["job_process"]
        run = create_live_wire_run(engine, tmp_path)
        overlap = _Overlap(engine, run.job_id, batch_process)
        started = run.run_directory / job_computes.LIVE_STARTED
        assert wait_for(started.exists, timeout=60), worker.log_text()
        cores = json.loads(started.read_text())["cores"]
        held = load_job(engine, waiting).status
        controls.cancel_run(run.job_id, engine=engine)
        assert wait_for(lambda: load_job(engine, waiting).status == JobStatus.FINISHED, timeout=60), worker.log_text()
    finally:
        samples = overlap.stop() if overlap is not None else []
        worker.cleanup()

    job = load_job(engine, batch)
    assert (job.status, job.phase) == (JobStatus.CANCELLED, RunPhase.CANCELLED), job.messages
    assert (job.messages or "").splitlines()[-2:] == [
        "Cancellation requested: stopped for a live run",
        "Cancelled by request; completed results kept; 0 succeeded, 0 failed, 2 not run of 2",
    ]
    assert cores == max(1, math.floor((os.cpu_count() or 1) * 0.5))
    assert held == JobStatus.QUEUED
    assert load_job(engine, run.job_id).status == JobStatus.CANCELLED  # stopped with nothing arrived
    assert samples and not [sample for sample in samples if sample == (True, JobStatus.RUNNING)]
    assert any(status == JobStatus.QUEUED for batch_alive, status in samples if batch_alive)  # it did wait
