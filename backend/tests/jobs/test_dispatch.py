"""The worker's dispatcher (``jobs.dispatch``) in this process, on a test database: the order, the claim, the batch slot
(with an orphan an earlier worker left running), the start of a job process, the sweep, the timeout's kill, and what a
job process checks before it runs anything.

The job processes here are small stand-ins (``Dispatcher(start=...)``: ``FakeJobs``); the real ones, started by a live
worker, are ``test_worker.py``'s. The fault-injection tests (a job process that cannot start, a write-locked database,
Stop and Move to Front against a claim, ...): ``test_faults.py``.
"""

from __future__ import annotations

import io
import logging
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from laueportal.indexing import conversion
from laueportal.indexing.models import Conversion
from laueportal.jobs import controls, dispatch, state
from laueportal.jobs import process as job_process
from laueportal.jobs.dispatch import CONVERT, RUN, Dispatcher, DispatchPolicy, Kind
from laueportal.jobs.lifecycle import RunMonitor
from laueportal.jobs.models import Job
from laueportal.jobs.queue import RunPolicy
from laueportal.jobs.schemas import MoveState
from laueportal.jobs.state import JobStatus, RunPhase
from tests.indexing.test_viewer import add_run
from tests.jobs.live_support import create_live_wire_run
from tests.run_support import enqueue_with, load_job, publish_wire_run
from tests.settings_support import make_database
from tests.worker_support import alive, kill_quietly, wait_for

POLICY = DispatchPolicy(dispatch_seconds=0.05, heartbeat_seconds=0.05, shutdown_grace_seconds=5)

GO_SCRIPT = "import sys\nline = sys.stdin.buffer.readline()\nopen(sys.argv[1], 'wb').write(line)\n"
"""A stand-in job process: it waits for the dispatcher's go line, writes what it read to ``argv[1]`` and exits 0."""


class FakeJobs:
    """A stand-in for ``dispatch.start_job_process``: one small process per item (``command``, by default
    ``GO_SCRIPT`` writing to ``<directory>/<kind>-<key>``) in a session of its own, as the real ones are."""

    def __init__(self, directory: Path, command: list[str] | None = None) -> None:
        self.directory = directory
        self.command = command
        self.started: list[tuple[Kind, int]] = []
        self.cores: dict[int, int | None] = {}
        """The cores each item's start was given, by key (a live run's; None for a batch item)."""
        self.processes: list[subprocess.Popen[bytes]] = []

    def marker(self, kind: Kind, key: int) -> Path:
        return self.directory / f"{kind}-{key}"

    def __call__(self, kind: Kind, key: int, cores: int | None = None) -> subprocess.Popen[bytes]:
        command = self.command or [sys.executable, "-S", "-c", GO_SCRIPT, str(self.marker(kind, key))]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, start_new_session=True)
        self.started.append((kind, key))
        self.cores[key] = cores
        self.processes.append(process)
        return process

    def cleanup(self) -> None:
        kill_quietly(process.pid for process in self.processes)
        for process in self.processes:
            process.wait(10)


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


def _queued(engine, tmp_path: Path, name: str, **options) -> int:
    run = publish_wire_run(engine, tmp_path, name=name, **options)
    enqueue_with(engine, run.job_id)
    return run.job_id


def _drain(dispatcher: Dispatcher, limit: int = 20) -> None:
    """Run cycles until one starts nothing; each job process is waited for before the next cycle."""
    for _ in range(limit):
        if dispatcher.cycle() is None:
            return
        for child in list(dispatcher.children.values()):
            child.process.wait(10)
    raise AssertionError("the dispatcher kept starting job processes")


def test_the_order_is_conversions_oldest_first_then_moves_newest_first_then_the_rest_by_job_id(engine, tmp_path, fake):
    runs = [_queued(engine, tmp_path, name) for name in ("first", "second", "third", "fourth")]
    for indexing_id in (101, 102):
        add_run(engine, indexing_id, None)
    conversion.enqueue_conversion(102, sessions=engine)
    conversion.enqueue_conversion(101, sessions=engine)
    for job_id in (runs[2], runs[3]):
        assert controls.move_run_to_front(job_id, engine=engine).state == MoveState.MOVED
    remote = _queued(engine, tmp_path, "remote")
    with Session(engine) as session, session.begin():
        session.execute(update(Job).where(Job.job_id == remote).values(backend="aps"))  # the remote poller's
    expected = [(CONVERT, 102), (CONVERT, 101), (RUN, runs[3]), (RUN, runs[2]), (RUN, runs[0]), (RUN, runs[1])]

    with Session(engine) as session:
        in_order = dispatch.queued_in_order(session)
    _drain(Dispatcher(engine, POLICY, start=fake))

    assert in_order == expected
    assert fake.started == expected  # one at a time: the batch slot
    assert load_job(engine, remote).status == JobStatus.QUEUED


def test_a_claim_takes_a_queued_item_once_and_ends_a_run_with_its_stop_flag_cancelled(engine, tmp_path):
    job_id, flagged = _queued(engine, tmp_path, "claimed"), _queued(engine, tmp_path, "flagged")
    with Session(engine) as session, session.begin():
        state.request_cancellation(session.get(Job, flagged), reason="user")
    add_run(engine, 101, None)
    conversion.enqueue_conversion(101, sessions=engine)

    claims = [dispatch.claim_run(engine, job_id), dispatch.claim_run(engine, job_id)]
    flagged_claim = dispatch.claim_run(engine, flagged)
    conversions = [dispatch.claim_conversion(engine, 101), dispatch.claim_conversion(engine, 101)]

    assert claims == [True, False] and conversions == [True, False]
    job = load_job(engine, job_id)
    assert (job.status, job.phase, job.process_pid) == (JobStatus.RUNNING, RunPhase.RUNNING, None)
    stopped = load_job(engine, flagged)
    assert (flagged_claim, stopped.status, stopped.n_not_run) == (False, JobStatus.CANCELLED, 3)
    assert "Cancelled before the run started" in (stopped.messages or "")
    found = conversion.find_conversion(101, sessions=engine)
    assert found is not None and found.status == "running" and found.started_at is not None
    assert dispatch.claim_run(engine, 999) is False


def test_concurrent_claims_of_one_run_claim_it_once(engine, tmp_path):
    """Two dispatchers (two workers on one database, which the lock refuses) or a claim racing itself: the claim's
    write transaction lets exactly one take the run."""
    job_id = _queued(engine, tmp_path, "contested")
    barrier = threading.Barrier(4)
    results: list[bool] = []

    def claim() -> None:
        barrier.wait(10)
        results.append(dispatch.claim_run(engine, job_id))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(results) == [False, False, False, True]
    assert load_job(engine, job_id).status == JobStatus.RUNNING


def test_a_job_process_is_recorded_on_its_row_then_told_to_go_and_its_ended_run_is_left_to_the_reconciler(
    engine, tmp_path, fake
):
    job_id = _queued(engine, tmp_path, "started")
    dispatcher = Dispatcher(engine, POLICY, start=fake)

    assert dispatcher.cycle() == RUN
    (pid,) = dispatcher.children
    row = load_job(engine, job_id)
    assert (row.process_host, row.process_pid) == (socket.gethostname(), pid)
    assert row.process_since_boot == pytest.approx(dispatch.since_boot(psutil.Process(pid)), abs=1.5)
    assert dispatch.recorded_process_alive(row.process_host, row.process_pid, row.process_since_boot) is True
    dispatcher.children[pid].process.wait(10)
    assert dispatcher.cycle() is None  # reaped: the slot is free, and nothing else is queued

    assert fake.marker(RUN, job_id).read_bytes() == dispatch.GO
    assert not dispatcher.children
    job = load_job(engine, job_id)  # the stand-in ended without finalizing: the reconciler's to do
    assert (job.status, job.phase) == (JobStatus.RUNNING, RunPhase.RUNNING)


def test_a_recorded_job_process_is_known_by_its_host_pid_and_start_since_boot_whatever_the_clock_does(monkeypatch):
    """psutil's start time of a process is the boot time (the clock less the uptime) plus its start since boot, so a
    step of the system clock moves it; the record keeps the start since boot. After a 1 s step the recorded process is
    still that process, and so is one recorded a whole second off (the boot time, whole seconds, turned over between its
    two reads), but a live process whose start differs more (a reused pid) is not."""
    me = dispatch.process_record(os.getpid())
    gone = subprocess.Popen(["true"])
    gone.wait()
    boot_time = psutil._psplatform.boot_time
    monkeypatch.setattr(psutil._psplatform, "boot_time", lambda: boot_time() + 1.0)  # the clock stepped 1 s

    assert dispatch.recorded_process_alive(me.host, me.pid, me.since_boot) is True
    assert dispatch.recorded_process_alive(me.host, me.pid, me.since_boot - 1.0) is True
    assert dispatch.recorded_process_alive(me.host, me.pid, me.since_boot - 60) is False  # a reused pid
    assert dispatch.recorded_process_alive(me.host, gone.pid, me.since_boot) is False
    assert dispatch.recorded_process_alive("another-host", me.pid, me.since_boot) is None  # cannot look
    assert dispatch.recorded_process_alive(None, None, None) is None


def test_a_live_job_process_an_earlier_worker_started_holds_the_slot_and_is_never_finalized(engine, tmp_path, fake):
    """A worker killed mid-run leaves its job process running; a restarted dispatcher counts its slot, starts nothing
    beside it, does not finalize its run, and starts the next run once it has ended."""
    orphaned, waiting = _queued(engine, tmp_path, "orphaned"), _queued(engine, tmp_path, "waiting")
    assert dispatch.claim_run(engine, orphaned)
    orphan = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        assert dispatch.record_process(engine, RUN, orphaned, dispatch.process_record(orphan.pid))
        dispatcher = Dispatcher(engine, POLICY, start=fake)

        idle_cycles = [dispatcher.cycle() for _ in range(3)]
        slot_free, orphans = dispatcher.batch_slot_free(), [record.pid for record in dispatcher.orphans()]
        states = (load_job(engine, orphaned).status, load_job(engine, waiting).status)
        orphan.kill()
        orphan.wait()
        after = dispatcher.cycle()
    finally:
        kill_quietly([orphan.pid])

    assert idle_cycles == [None, None, None] and (slot_free, orphans) == (False, [orphan.pid])
    assert states == (JobStatus.RUNNING, JobStatus.QUEUED)
    assert (after, fake.started) == (RUN, [(RUN, waiting)])
    assert load_job(engine, orphaned).status == JobStatus.RUNNING  # finalizing it is the reconciler's


def test_what_a_job_process_leaves_in_its_group_is_killed_when_it_exits(engine, tmp_path, caplog):
    job_id = _queued(engine, tmp_path, "leaver")
    pids = tmp_path / "left.pid"
    fake = FakeJobs(tmp_path, ["sh", "-c", f"sleep 300 & echo $! > {pids}; read line"])
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    try:
        assert dispatcher.cycle() == RUN
        assert wait_for(lambda: pids.exists() and pids.read_text().strip() != "", timeout=10)
        left = int(pids.read_text())
        fake.processes[0].wait(10)
        with caplog.at_level(logging.WARNING, logger="laueportal.jobs.dispatch"):
            dispatcher.cycle()
        assert wait_for(lambda: not alive(left), timeout=10)
    finally:
        fake.cleanup()
    process = fake.processes[0].pid
    assert f"Killed processes left behind by the job process {process} of job {job_id}" in caplog.text


def test_a_job_process_alive_past_its_timeout_and_the_grace_is_killed_with_its_group(engine, tmp_path, caplog):
    """The job process's own timeout (SIGALRM) ends its run; should it not exit, the dispatcher kills its group
    ``shutdown_grace_seconds`` later."""
    job_id = _queued(engine, tmp_path, "overdue")
    policy = DispatchPolicy(dispatch_seconds=0.05, job_timeout_seconds=0.2, shutdown_grace_seconds=0.2)
    fake = FakeJobs(tmp_path, ["sh", "-c", "sleep 300 & sleep 300"])
    dispatcher = Dispatcher(engine, policy, start=fake)
    try:
        assert dispatcher.cycle() == RUN
        process = fake.processes[0]
        members = lambda: [p.pid for p in psutil.process_iter() if _group(p.pid) == process.pid]  # noqa: E731
        assert wait_for(lambda: len(members()) >= 2, timeout=10)  # sh (or the sleep it became) and a sleep
        early = dispatcher.cycle()
        early_alive = process.poll() is None
        time.sleep(0.5)
        with caplog.at_level(logging.ERROR, logger="laueportal.jobs.dispatch"):
            dispatcher.cycle()
        assert wait_for(lambda: process.poll() is not None and members() == [], timeout=10)
        dispatcher.cycle()
    finally:
        fake.cleanup()
    assert (early, early_alive) == (None, True)
    assert process.returncode == -signal.SIGKILL
    assert f"The job process {process.pid} of job {job_id} is alive 0.2 s after its timeout" in caplog.text
    assert not dispatcher.children


def test_the_kill_s_deadline_counts_from_the_go_line_as_the_job_process_s_own_timeout_does(
    engine, tmp_path, monkeypatch
):
    """A job process whose record waited on a write lock is told to go late; its own timeout counts from its go line, so
    the dispatcher's kill (the timeout plus the grace) counts from there too, never from its launch. The dispatcher's
    clock is a stand-in here (seconds as the test sets them)."""
    job_id = _queued(engine, tmp_path, "late")
    policy = DispatchPolicy(dispatch_seconds=0.05, job_timeout_seconds=10, shutdown_grace_seconds=5)
    fake = FakeJobs(tmp_path, ["sh", "-c", "read line; exec sleep 300"])  # told to go, it runs (sleeps) on
    dispatcher = Dispatcher(engine, policy, start=fake)
    now = [0.0]
    monkeypatch.setattr(dispatch, "time", SimpleNamespace(monotonic=lambda: now[0]))
    record_process = dispatch.record_process
    locked = [True]

    def record_through_a_lock(*args, **kwargs) -> bool:
        if locked[0]:
            raise OperationalError("UPDATE job", {}, Exception("database is locked"))
        return record_process(*args, **kwargs)

    monkeypatch.setattr(dispatch, "record_process", record_through_a_lock)
    try:
        assert dispatcher.cycle() == RUN  # launched at 0; its record fails
        process = fake.processes[0]
        now[0], locked[0] = 8.0, False
        dispatcher.cycle()  # recorded and told to go at 8
        recorded = load_job(engine, job_id).process_pid
        now[0] = 16.0  # past the launch's 0 + 10 + 5, before the go line's 8 + 10 + 5
        dispatcher.cycle()
        time.sleep(0.2)
        before_the_deadline = process.poll()
        now[0] = 24.0
        dispatcher.cycle()
        assert wait_for(lambda: process.poll() is not None, timeout=10)
    finally:
        fake.cleanup()
    assert recorded == process.pid
    assert before_the_deadline is None
    assert process.returncode == -signal.SIGKILL


@pytest.mark.parametrize("slot", ["batch", "live"])
def test_a_job_process_that_exits_at_its_go_line_holds_its_slot_until_the_next_cycle_reaps_it(
    engine, tmp_path, monkeypatch, slot
):
    """A slot is busy while the dispatcher's child is not reaped, even one that exited within the cycle that let it go
    (its record had waited); only the next cycle, which reaps it and kills what it left in its group, takes the next
    item, so a slot's job processes never overlap. The batch slot with one such child; the two live slots with two."""
    if slot == "batch":
        first = [_queued(engine, tmp_path, "first")]
        following = _queued(engine, tmp_path, "following")
    else:
        first = [create_live_wire_run(engine, tmp_path, name=name).job_id for name in ("one", "two")]
        following = create_live_wire_run(engine, tmp_path, name="three").job_id
    fake = FakeJobs(tmp_path)  # GO_SCRIPT: it exits once told to go
    release, record_process = dispatch._release, dispatch.record_process
    failures = [len(first)]

    def exits_within_the_go_line(process) -> None:
        release(process)
        process.wait(10)

    def record_once_locked(*args, **kwargs) -> bool:
        if failures[0]:
            failures[0] -= 1
            raise OperationalError("UPDATE job", {}, Exception("database is locked"))
        return record_process(*args, **kwargs)

    monkeypatch.setattr(dispatch, "_release", exits_within_the_go_line)
    monkeypatch.setattr(dispatch, "record_process", record_once_locked)
    dispatcher = Dispatcher(engine, POLICY, start=fake)
    try:
        launching = dispatcher.cycle()  # claims, launches; each record fails
        recording = dispatcher.cycle()  # records them and lets them go; they exit at once, not yet reaped
        reaping = dispatcher.cycle()  # reaps them: the next item starts
    finally:
        fake.cleanup()

    assert (launching, recording, reaping) == (RUN, None, RUN)
    assert fake.started == [*((RUN, job_id) for job_id in first), (RUN, following)]


def test_the_worker_s_last_write_ends_an_item_whose_job_process_was_never_recorded_and_no_other(engine, tmp_path):
    """``stop_unstarted``, the worker's last write for the job processes its stop let go unrecorded (they ran nothing):
    a claimed run with no process recorded ends Failed/interrupted and a claimed conversion ``failed``, each with its
    message; a run whose process was recorded, and a run or a conversion that already ended (the conversion's status
    check), are left as they are. The live stop: ``test_worker.py``."""
    unrecorded, recorded = _queued(engine, tmp_path, "unrecorded"), _queued(engine, tmp_path, "recorded")
    add_run(engine, 101, None)
    conversion.enqueue_conversion(101, sessions=engine)
    assert dispatch.claim_run(engine, unrecorded) and dispatch.claim_run(engine, recorded)
    assert dispatch.claim_conversion(engine, 101)
    assert dispatch.record_process(engine, RUN, recorded, dispatch.process_record(os.getpid()))
    items: list[tuple[Kind, int]] = [(RUN, unrecorded), (CONVERT, 101), (RUN, recorded)]

    with Session(engine) as session, session.begin():
        ended = [dispatch.stop_unstarted(session, kind, key) for kind, key in items]
    with Session(engine) as session, session.begin():
        again = dispatch.stop_unstarted(session, RUN, unrecorded), dispatch.stop_unstarted(session, CONVERT, 101)

    assert (ended, again) == ([True, True, False], (False, False))
    job = load_job(engine, unrecorded)
    assert (job.status, job.phase, job.process_pid) == (JobStatus.FAILED, RunPhase.INTERRUPTED, None)
    assert (job.messages or "").splitlines()[-1] == dispatch.STOPPED_UNSTARTED
    assert (job.messages or "").count(dispatch.STOPPED_UNSTARTED) == 1
    with Session(engine) as session:
        row = session.get(Conversion, 101)
        assert row is not None and (row.status, row.message) == (
            dispatch.CONVERSION_FAILED,
            "Stopped by service shutdown before the conversion started; convert the results again",
        )
        assert row.ended_at is not None and row.message.count("Stopped by service shutdown") == 1
    left = load_job(engine, recorded)
    assert (left.status, left.phase, left.process_pid) == (JobStatus.RUNNING, RunPhase.RUNNING, os.getpid())


def _group(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None


def _stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))


def test_a_job_process_runs_nothing_unless_its_row_names_it(setup, tmp_path, monkeypatch):
    settings, engine = setup
    job_id = _queued(engine, tmp_path, "named")
    add_run(engine, 101, None)
    conversion.enqueue_conversion(101, sessions=engine)
    assert dispatch.claim_run(engine, job_id) and dispatch.claim_conversion(engine, 101)
    ran: list[tuple[str, int]] = []
    monkeypatch.setattr("laueportal.jobs.executor.execute_run", lambda key, **_: ran.append(("run", key)))
    monkeypatch.setattr("laueportal.indexing.conversion.run_conversion", lambda key, **_: ran.append(("convert", key)))
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGALRM, signal.SIGUSR1)}
    other = dispatch.ProcessRecord(socket.gethostname(), os.getpid() + 1, 0.0)
    mine = dispatch.process_record(os.getpid())
    try:
        exits = []
        for kind, key in (("run", job_id), ("convert", 101)):
            dispatch.record_process(engine, kind, key, other)  # another process's row: a duplicate start
            _stdin(monkeypatch, dispatch.GO)
            exits.append(job_process.run(kind, key, settings))
        for kind, key in (("run", job_id), ("convert", 101)):
            dispatch.record_process(engine, kind, key, mine)
            _stdin(monkeypatch, b"")  # the worker died before the go line: end of file, but the row names it
            exits.append(job_process.run(kind, key, settings))
        with Session(engine) as session, session.begin():
            session.execute(update(Conversion).values(status="failed"))  # ended meanwhile (the reconciler)
        _stdin(monkeypatch, dispatch.GO)
        exits.append(job_process.run("convert", 101, settings))
    finally:
        signal.alarm(0)
        for signum, handler in previous.items():  # a conversion's job process ignores SIGUSR1
            signal.signal(signum, handler)

    assert exits == [job_process.NOT_MINE, job_process.NOT_MINE, 0, 0, job_process.NOT_MINE]
    assert ran == [("run", job_id), ("convert", 101)]


def test_a_job_process_arms_its_own_timeout_the_settings_for_a_run_and_its_own_for_a_conversion(tmp_path, monkeypatch):
    """The alarm a job process arms is ``execution.job_timeout_seconds`` for a run and ``CONVERSION_TIMEOUT_SECONDS``
    for a conversion (the dispatcher's kill deadline is ``test_worker``'s)."""
    settings, engine = make_database(tmp_path, execution={"job_timeout_seconds": 4321})
    engine.dispose()
    armed: list[int] = []
    monkeypatch.setattr(job_process, "_timeout", armed.append)
    monkeypatch.setattr(job_process, "names_this_process", lambda *_: True)
    monkeypatch.setattr("laueportal.jobs.executor.execute_run", lambda key, **_: None)
    monkeypatch.setattr("laueportal.indexing.conversion.run_conversion", lambda key, **_: None)
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        for kind in ("run", "convert"):
            _stdin(monkeypatch, dispatch.GO)
            assert job_process.run(kind, 1, settings) == 0
    finally:
        signal.signal(signal.SIGUSR1, previous)

    assert armed == [4321, conversion.CONVERSION_TIMEOUT_SECONDS] and conversion.CONVERSION_TIMEOUT_SECONDS != 4321


def test_a_live_run_s_job_process_has_no_time_limit_so_the_dispatcher_never_kills_it_for_one(engine, tmp_path):
    """No job time limit for a live run, only its Stop or the stale-heartbeat rule; a batch run's job process past its
    timeout and the grace is killed (above)."""
    run = create_live_wire_run(engine, tmp_path)
    policy = DispatchPolicy(dispatch_seconds=0.05, job_timeout_seconds=0.2, shutdown_grace_seconds=0.2)
    fake = FakeJobs(tmp_path, ["sleep", "300"])
    dispatcher = Dispatcher(engine, policy, start=fake)
    try:
        assert dispatcher.cycle() == RUN and fake.started == [(RUN, run.job_id)]
        (child,) = dispatcher.children.values()
        time.sleep(0.6)
        dispatcher.cycle()
        assert child.timeout == math.inf and fake.processes[0].poll() is None
    finally:
        fake.cleanup()


def test_a_live_run_s_job_process_arms_no_timeout(tmp_path, monkeypatch):
    """The job process of a live run arms no alarm; a batch run's arms ``execution.job_timeout_seconds``."""
    settings, engine = make_database(tmp_path, execution={"job_timeout_seconds": 4321})
    batch = _queued(engine, tmp_path, "batch")
    assert dispatch.claim_run(engine, batch)  # before the live run exists: none while one is open
    live = create_live_wire_run(engine, tmp_path).job_id
    assert dispatch.claim_run(engine, live)
    engine.dispose()
    armed: list[int] = []
    monkeypatch.setattr(job_process, "_timeout", armed.append)
    monkeypatch.setattr(job_process, "names_this_process", lambda *_: True)
    monkeypatch.setattr("laueportal.jobs.executor.execute_run", lambda key, **_: None)
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        for job_id in (live, batch):
            _stdin(monkeypatch, dispatch.GO)
            assert job_process.run("run", job_id, settings) == 0
    finally:
        signal.signal(signal.SIGUSR1, previous)
    assert armed == [4321]


class _ReenteringEvent(threading.Event):
    """An event whose first ``set()`` raises the stop's signal again while it holds the event's lock: the handler then
    runs inside that ``set()``, as CPython runs it when the worker's next signal comes while the handler runs."""

    def __init__(self) -> None:
        super().__init__()
        self.inside = 0

    def set(self) -> None:
        with self._cond:
            if not self.inside:
                self.inside += 1
                signal.raise_signal(signal.SIGUSR1)  # the handler runs again here, before this returns
            self._flag = True
            self._cond.notify_all()


def _waited_for_the_lock(signum, frame) -> None:
    raise TimeoutError("a stop handler run again inside itself waited for the event's lock")


@pytest.mark.parametrize("handler", ["job process", "run monitor"])
def test_a_stop_handler_run_again_inside_itself_never_waits_for_the_event_s_lock(monkeypatch, handler):
    """The worker sends the stop's signal again on each poll, so a run's handler (the job process's ``_record_stop``,
    then the executor's ``RunMonitor.request_shutdown``) can run again inside its own ``Event.set()``. A second
    ``set()`` there would wait for good for the lock the first one holds; the inner call returns at once instead (an
    alarm fails the test rather than letting it hang)."""
    event = _ReenteringEvent()
    if handler == "job process":
        monkeypatch.setattr(job_process, "STOP", event)
        monkeypatch.setattr(job_process, "_stop_recorded", False)
        previous = {signum: signal.getsignal(signum) for signum in (signal.SIGUSR1, signal.SIGALRM)}
        job_process.handle_stop("run")
        stop = signal.getsignal(signal.SIGUSR1)
    else:
        previous = {signum: signal.getsignal(signum) for signum in (signal.SIGUSR1, signal.SIGALRM)}
        monitor = RunMonitor(None, 1, RunPolicy(), shutdown=event)  # never started: only its stop is used
        stop = lambda signum, frame: monitor.request_shutdown()  # noqa: E731 (the executor's handler)
    calls: list[int] = []

    def counted(signum, frame) -> None:
        calls.append(signum)
        stop(signum, frame)

    try:
        signal.signal(signal.SIGUSR1, counted)
        signal.signal(signal.SIGALRM, _waited_for_the_lock)
        signal.alarm(5)
        signal.raise_signal(signal.SIGUSR1)
    finally:
        signal.alarm(0)
        for signum, previous_handler in previous.items():
            signal.signal(signum, previous_handler)

    assert calls == [signal.SIGUSR1, signal.SIGUSR1] and event.inside == 1 and event.is_set()


# ---------------------------------------------------------------------------
# The job processes' own log (``JobLog``)

_PRINTING_JOB = [
    sys.executable,
    "-c",
    "import sys; print('out', *sys.argv[1:], flush=True); print('err', *sys.argv[1:], file=sys.stderr)",
]


def _started(job_log: dispatch.JobLog, key: int) -> None:
    process = job_log.start(RUN, key)
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(30) == 0


def test_the_job_log_takes_each_job_process_s_output_and_is_rotated_once_it_reached_its_size(tmp_path, monkeypatch):
    """Each job process's stdout and stderr are appended to ``execution.job_log`` (0600); a file that reached
    ``JOB_LOG_MAX_BYTES`` is rotated at the next job process's start, ``.1`` the newest of ``JOB_LOG_BACKUPS``."""
    monkeypatch.setattr(dispatch, "JOB_COMMAND", _PRINTING_JOB)
    path = tmp_path / "jobs.log"
    job_log = dispatch.JobLog(path)
    _started(job_log, 1)
    _started(job_log, 2)
    assert path.read_text().splitlines() == ["out run 1", "err run 1", "out run 2", "err run 2"]
    assert path.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(dispatch, "JOB_LOG_MAX_BYTES", 1)
    monkeypatch.setattr(dispatch, "JOB_LOG_BACKUPS", 3)
    for key in range(3, 7):
        _started(job_log, key)
    kept = {file.name: file.read_text().splitlines() for file in tmp_path.iterdir()}
    assert kept == {
        name: [f"out run {key}", f"err run {key}"]
        for name, key in [("jobs.log", 6), ("jobs.log.1", 5), ("jobs.log.2", 4), ("jobs.log.3", 3)]
    }


def test_a_job_log_that_cannot_be_written_leaves_job_processes_on_the_worker_s_output_logged_once(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(dispatch, "JOB_COMMAND", _PRINTING_JOB)
    path = tmp_path / "missing" / "jobs.log"
    job_log = dispatch.JobLog(path)
    caplog.set_level(logging.WARNING, logger=dispatch.__name__)
    _started(job_log, 1)
    _started(job_log, 2)
    assert not path.parent.exists()
    (warning,) = [record.getMessage() for record in caplog.records]
    assert warning.startswith(f"The job log {path} cannot be written (FileNotFoundError: ")
