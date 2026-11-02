"""The remote poller as a thread of the worker (``remote.poller.RemotePoller``).

DM is ``tests.remote_support.FakeDMService``: the real DM client (``remote.dm.DMClient``) runs, and every process it
starts is counted, a DM call running DM's real API script on a stand-in API in a fresh interpreter. One call per pass
(for 1 and for 20 active remote runs, none with none active), a test per activity that runs only while needed (the DM
call, the cancel workflow, the inbox listing), the back-offs (a failed call; the poller lock held elsewhere), the lock
held for the thread's lifetime, and the worker's stop. The passes' reconciliation and publication: ``test_poller.py``;
the thread raising inside a live worker: ``tests/jobs/test_faults.py``'s fault 12.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from sqlalchemy.orm import Session

from laueportal.core import db
from laueportal.jobs import controls, worker
from laueportal.jobs.models import Job, Worker
from laueportal.jobs.state import JobStatus
from laueportal.remote import poller, submission
from laueportal.remote.dm import DMClient
from laueportal.remote.poller import CLEANUP_SECONDS, MAX_WAIT_SECONDS, POLLER_LOCK_NAME, PollerLock, RemotePoller
from tests.remote.test_poller import FakeDM, _return
from tests.remote_support import FakeDMService, Layout, bdp_settings, create_wire_run, remote_settings
from tests.run_support import load_job, make_engine
from tests.settings_support import make_database
from tests.worker_support import Live, alive, kill_quietly, start_worker, wait_for, write_config

POLL_SECONDS = 10.0
"""``bdp_settings``' ``poll_seconds``."""


@pytest.fixture
def remote(tmp_path):
    layout = Layout(tmp_path)
    engine = make_engine(tmp_path / "portal")
    values = bdp_settings(layout)
    yield SimpleNamespace(engine=engine, layout=layout, values=values, settings=remote_settings(values))
    engine.dispose()


@pytest.fixture
def dm(tmp_path, monkeypatch) -> FakeDMService:
    return FakeDMService(tmp_path / "dm", monkeypatch)


def _submitted(remote, count: int, *, name: str = "run") -> list[int]:
    """``count`` crux runs submitted through the real DM client (one ``submit`` call each): Queued, their DM jobs
    ``dm-001``, ``dm-002``, ... pending."""
    client = DMClient(remote.settings.dm)
    jobs = []
    for number in range(count):
        run = create_wire_run(remote.engine, remote.layout, count=1, backend="crux", name=f"{name}{number}")
        submission.submit_run(run.job_id, engine=remote.engine, settings=remote.settings, client=client)
        jobs.append(run.job_id)
    return jobs


def _poller(remote, *, stopped=None, marks=None, clock=None, settings=None) -> RemotePoller:
    return RemotePoller(
        settings or remote.settings,
        engine=remote.engine,
        stopped=stopped or threading.Event(),
        mark=None if marks is None else marks.append,
        heartbeat_seconds=0,
        clock=clock or time.monotonic,
    )


def _lock_path(remote) -> Path:
    return Path(remote.settings.submission_directory) / POLLER_LOCK_NAME


def _listings(monkeypatch) -> list[dict[str, Any]]:
    """The inbox listings (``remote.cleanup.clean_up``) the poller makes."""
    listings: list[dict[str, Any]] = []
    monkeypatch.setattr(poller, "clean_up", lambda **kwargs: listings.append(kwargs) or [])
    return listings


@pytest.mark.parametrize("active", [0, 1, 20])
def test_one_dm_call_per_pass_for_1_and_for_20_active_remote_runs_and_none_with_none_active(remote, dm, active):
    """Each pass starts one process for every active remote run, whatever their number (one shell running one Python
    process, bounded by ``dm.timeout_seconds``), with every record and the look-up of a lost reply in it; none while no
    remote run is active."""
    jobs = _submitted(remote, active)
    if active:  # the last one's reply was lost: found by its submission ID in the same call
        with Session(remote.engine) as session, session.begin():
            lost = session.get(Job, jobs[-1])
            assert lost is not None
            lost.external_id, lost.external_state = None, "submission_uncertain"
            lost_submission = lost.submission_id
    for number in range(1, active + 1, 2):
        dm.jobs[f"dm-{number:03d}"]["status"] = "running"
    before = len(dm.runs)
    passes = _poller(remote)

    for _ in range(3):
        assert passes.cycle() == POLL_SECONDS

    runs = dm.runs[before:]
    assert len(runs) == (3 if active else 0)
    assert all(run["request"]["operation"] == "fetch" for run in runs)
    assert all(run["timeout"] == remote.settings.dm.timeout_seconds for run in runs)
    if active:
        first, later = runs[0]["request"]["args"], runs[1]["request"]["args"]
        assert (len(first["ids"]), first["submission_ids"]) == (active - 1, [lost_submission])
        assert (len(later["ids"]), later["submission_ids"]) == (active, [])
        states = {
            job_id: (load_job(remote.engine, job_id).status, load_job(remote.engine, job_id).external_id)
            for job_id in jobs
        }
        assert states == {
            job_id: (JobStatus.RUNNING if number % 2 else JobStatus.QUEUED, f"dm-{number:03d}")
            for number, job_id in enumerate(jobs, start=1)
        }


def test_no_dm_call_while_no_remote_run_is_queued_or_running_or_has_a_publication_pending(remote, dm, monkeypatch):
    """Only while needed, the DM call: a local run and a remote one never submitted (no DM job) make no DM call; the
    submitted run makes one per pass while it is Queued, then while its publication is pending (DM done, its return
    refused once by a transient error), and none once it is finalized."""
    create_wire_run(remote.engine, remote.layout, count=1, backend="local", name="local")
    create_wire_run(remote.engine, remote.layout, count=1, backend="crux", name="unsubmitted")
    passes = _poller(remote)
    for _ in range(3):
        passes.cycle()
    assert dm.runs == []

    (job,) = _submitted(remote, 1)
    plan = dm.runs[-1]["request"]["args"]["arguments"]
    before = len(dm.runs)
    passes.cycle()
    assert dm.operations(before) == ["fetch"]

    _return(poller._read(Path(plan["filePath"]).parent / "bdp.json"), status="Cancelled")
    dm.jobs["dm-001"]["status"] = "done"
    publish = poller.publish_returned
    attempts = []

    def held_once(plan):
        attempts.append(plan["job_id"])
        if len(attempts) == 1:
            raise BlockingIOError(11, "Resource temporarily unavailable")  # another process's lock: retried
        return publish(plan)

    monkeypatch.setattr(poller, "publish_returned", held_once)
    before = len(dm.runs)
    passes.cycle()
    assert load_job(remote.engine, job).status == JobStatus.QUEUED  # its publication pending
    passes.cycle()
    assert (load_job(remote.engine, job).status, len(attempts)) == (JobStatus.CANCELLED, 2)
    for _ in range(3):
        passes.cycle()
    assert dm.operations(before) == ["fetch", "fetch"]


def test_the_cancel_workflow_is_called_only_while_a_stop_is_pending(remote, dm):
    """Only while needed, the cancel workflow: none without a stop; a stop submits it once (its own call), its job's
    state comes in the passes' one call until it is delivered, and is asked for no more."""
    (job,) = _submitted(remote, 1)
    dm.jobs["dm-001"]["status"] = "running"
    passes = _poller(remote)
    before = len(dm.runs)
    passes.cycle()
    passes.cycle()
    assert dm.operations(before) == ["fetch", "fetch"]

    controls.cancel_run(job, engine=remote.engine)
    before = len(dm.runs)
    passes.cycle()  # submits the cancel workflow's job, dm-002
    passes.cycle()
    assert dm.operations(before) == ["fetch", "submit", "fetch"]
    cancel = dm.runs[before + 1]["request"]["args"]
    assert (cancel["workflow"], cancel["arguments"]["cancelSubmissionId"]) == (
        remote.settings.dm.cancel_workflow,
        load_job(remote.engine, job).submission_id,
    )
    assert dm.runs[-1]["request"]["args"]["ids"] == ["dm-001", "dm-002"]

    dm.jobs["dm-002"]["status"] = "done"
    before = len(dm.runs)
    passes.cycle()  # delivered
    passes.cycle()
    assert dm.operations(before) == ["fetch", "fetch"]
    assert [run["request"]["args"]["ids"] for run in dm.runs[before:]] == [["dm-001", "dm-002"], ["dm-001"]]
    run_directory = Path(str(load_job(remote.engine, job).run_directory))
    assert (run_directory / ".cancel-sent").exists()


def test_the_inbox_listing_comes_at_the_start_then_only_while_remote_runs_are_active_or_one_ended(
    remote, dm, monkeypatch
):
    """Only while needed, the listing of the DM inbox and the return inboxes (``remote.cleanup``) at the thread's first
    pass, then at most every ``CLEANUP_SECONDS``, and only while a remote run is active or one ended since the last
    listing."""
    listings = _listings(monkeypatch)
    now = [0.0]
    passes = _poller(remote, clock=lambda: now[0])

    def listed_at(moment: float) -> int:
        now[0] = moment
        before = len(listings)
        passes.cycle()
        return len(listings) - before

    assert listed_at(0) == 1  # the start, with nothing active
    assert listed_at(CLEANUP_SECONDS) == 0  # nothing active, nothing ended
    (job,) = _submitted(remote, 1)
    assert listed_at(CLEANUP_SECONDS) == 1  # active
    assert listed_at(1.5 * CLEANUP_SECONDS) == 0  # not yet due
    assert listed_at(2 * CLEANUP_SECONDS) == 1
    dm.jobs["dm-001"]["status"] = "aborted"
    assert listed_at(2.5 * CLEANUP_SECONDS) == 0  # the pass ends the run; the listing is not yet due
    assert load_job(remote.engine, job).status == JobStatus.FAILED
    assert listed_at(3 * CLEANUP_SECONDS) == 1  # it ended since the last listing
    assert listed_at(4 * CLEANUP_SECONDS) == 0
    assert listed_at(5 * CLEANUP_SECONDS) == 0
    assert len(listings) == 4 and all(listing["settings"] is remote.settings for listing in listings)


def test_a_failed_dm_call_doubles_the_wait_up_to_60_s_logged_once_per_change(remote, dm, caplog):
    """Each failed call (as a whole) doubles the wait from ``poll_seconds`` up to 60 s, changes nothing and writes no
    mark; logged when the calls start failing, when the failure changes, and when DM answers again (then every
    ``poll_seconds``)."""
    (job,) = _submitted(remote, 1)
    marks: list[Any] = []
    passes = _poller(remote, marks=marks)
    timeout = subprocess.TimeoutExpired(["/bin/bash"], 30)
    dm.failing = [timeout] * 5
    with caplog.at_level(logging.INFO, logger=poller.__name__):
        waits = [passes.cycle() for _ in range(5)]
        dm.failing = [subprocess.CalledProcessError(255, ["ssh"], "", "ssh: connect to host: Connection refused\n")]
        waits.append(passes.cycle())
        assert (load_job(remote.engine, job).external_state, marks) == ("pending", [])  # as submitted
        waits.append(passes.cycle())
    assert waits == [20.0, 40.0, MAX_WAIT_SECONDS, MAX_WAIT_SECONDS, MAX_WAIT_SECONDS, MAX_WAIT_SECONDS, POLL_SECONDS]
    assert len(marks) == 1
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "The remote poller's DM call failed (no answer within 30 s); it calls DM again in 20 s, twice as long after "
        "each failure up to 60 s",
        "The remote poller's DM call failed (exit status 255: ssh: connect to host: Connection refused); it calls DM "
        "again in 60 s, twice as long after each failure up to 60 s",
        "DM answers the remote poller again; it polls every 10 s",
    ]


def test_a_record_dm_cannot_give_fails_its_job_alone_in_the_one_call(remote, dm, caplog):
    """A record that fails is reported per record; the call's other records are reconciled, and there is no back-off
    (the call answered)."""
    jobs = _submitted(remote, 3)
    for number in (1, 2, 3):
        dm.jobs[f"dm-{number:03d}"]["status"] = "running"
    dm.failing_records = {"dm-002"}
    before = len(dm.runs)
    with caplog.at_level(logging.ERROR, logger=poller.__name__):
        assert _poller(remote).cycle() == POLL_SECONDS
    assert len(dm.runs) == before + 1
    assert [load_job(remote.engine, job_id).status for job_id in jobs] == [
        JobStatus.RUNNING,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
    ]
    (failed,) = [record for record in caplog.records if record.exc_info]
    assert failed.getMessage() == f"Could not reconcile remote job {jobs[1]}"
    assert str(failed.exc_info[1]) == "LookupError: no processing job record dm-002"


def test_a_record_that_is_not_an_object_fails_its_job_alone_and_the_wait_stays_poll_seconds(remote, dm, caplog):
    """A record DM gives that is not a JSON object (here a DM object without a mapping, which the API script sends as
    its text) fails its run's reconciliation alone, at every pass; the call's other runs are reconciled, each pass
    marks, and there is no back-off (the call answered)."""
    jobs = _submitted(remote, 3)
    for number in (1, 2, 3):
        dm.jobs[f"dm-{number:03d}"]["status"] = "running"
    dm.jobs["dm-002"] = ["not", "a", "mapping"]
    marks: list[Any] = []
    passes = _poller(remote, marks=marks)
    before = len(dm.runs)
    with caplog.at_level(logging.ERROR, logger=poller.__name__):
        assert [passes.cycle(), passes.cycle()] == [POLL_SECONDS, POLL_SECONDS]
    assert len(dm.runs) == before + 2 and len(marks) == 2
    assert [load_job(remote.engine, job_id).status for job_id in jobs] == [
        JobStatus.RUNNING,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
    ]
    failed = [record for record in caplog.records if record.exc_info]
    assert [record.getMessage() for record in failed] == [f"Could not reconcile remote job {jobs[1]}"] * 2
    assert {str(record.exc_info[1]) for record in failed} == {"DM's record of job dm-002 is not an object"}


def test_a_pass_through_a_write_locked_database_ends_at_its_first_write_logged_once_without_a_mark(
    remote, dm, monkeypatch, caplog
):
    """Another connection's write lock past the busy timeout ends the pass at its first write (not a traceback per run
    and per pass, each waiting the busy timeout), logged once for the streak, with no mark; once the lock goes, the next
    pass reconciles every run, marks, and says so once."""
    jobs = _submitted(remote, 2)
    for number in (1, 2):
        dm.jobs[f"dm-{number:03d}"]["status"] = "running"
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 100)
    remote.engine.dispose()  # its next connections wait 0.1 s for a lock
    _listings(monkeypatch)
    writes: list[int] = []
    begin_write = poller.begin_write
    monkeypatch.setattr(poller, "begin_write", lambda session: writes.append(1) or begin_write(session))
    marks: list[Any] = []
    passes = _poller(remote, marks=marks)
    locker = sqlite3.connect(str(remote.engine.url.database), timeout=5, isolation_level=None)
    try:
        with caplog.at_level(logging.INFO, logger=poller.__name__):
            locker.execute("BEGIN IMMEDIATE")
            assert [passes.cycle(), passes.cycle()] == [POLL_SECONDS, POLL_SECONDS]
            assert (len(writes), marks) == (2, [])  # each pass ended at its first write
            locker.execute("ROLLBACK")
            assert passes.cycle() == POLL_SECONDS
    finally:
        locker.close()
    assert [load_job(remote.engine, job_id).status for job_id in jobs] == [JobStatus.RUNNING] * 2
    assert len(marks) == 1
    messages = [record.getMessage() for record in caplog.records]
    locked = "The remote poller could not use the database (database is locked); its next cycle tries again"
    assert messages.count(locked) == 1 and messages.count("The remote poller can use the database again") == 1
    assert not [record for record in caplog.records if record.exc_info]


def test_while_another_process_holds_the_lock_the_thread_waits_without_passes_or_marks(remote, dm, caplog):
    """Another poller (``remote poll --once``) holds ``.poller.lock``: the thread does not end; it tries again after
    ``poll_seconds``, twice as long each time up to 60 s, logged once when it finds the lock held and once when it takes
    it; it makes no pass and writes no mark meanwhile, so its mark goes late."""
    _submitted(remote, 1)
    other = PollerLock(_lock_path(remote))
    assert other.take()
    marks: list[Any] = []
    passes = _poller(remote, marks=marks)
    before = len(dm.runs)
    with caplog.at_level(logging.INFO, logger=poller.__name__):
        tries = []
        for _ in range(5):
            tries.append((passes.take_lock(), passes.lock_wait))
        other.close()
        taken = passes.take_lock()
        passes.cycle()
    try:
        assert tries == [(False, 10.0), (False, 20.0), (False, 40.0), (False, 60.0), (False, 60.0)]
        assert taken and len(marks) == 1 and dm.operations(before) == ["fetch"]
        messages = [record.getMessage() for record in caplog.records]
        assert len(messages) == 2
        assert messages[0].startswith(f"Another process holds {_lock_path(remote)} (`laueportal remote poll --once`")
        assert messages[1] == f"The remote poller holds {_lock_path(remote)}; it polls every 10 s"
        assert not PollerLock(_lock_path(remote)).take()
    finally:
        passes.close()
    assert PollerLock(_lock_path(remote)).take()


def test_the_thread_holds_the_lock_for_its_lifetime_across_a_restart_and_ends_at_the_stop(remote, dm, monkeypatch):
    """The thread under the worker's ``Supervised`` restarts: the lock taken before its first pass stays held while
    it waits to start again after raising (no other poller can slip in), and it goes only at ``close``; the stop
    ends ``run_forever``."""
    monkeypatch.setattr(worker, "RESTART_FIRST_SECONDS", 0.2)
    settings = remote.settings.model_copy(update={"poll_seconds": 0.02})
    raised = threading.Event()
    passes_made = []
    real_poll = poller.poll

    def raising_once(**kwargs):
        passes_made.append(1)
        if not raised.is_set():
            raised.set()
            raise RuntimeError("an injected poller fault")
        return real_poll(**kwargs)

    monkeypatch.setattr(poller, "poll", raising_once)
    stopped = threading.Event()
    thread_poller = _poller(remote, stopped=stopped, settings=settings)
    thread = worker.Supervised("poller", thread_poller.run_forever, stopped, interval=0.02)
    thread.start()
    try:
        assert raised.wait(10)
        held_while_restarting = not PollerLock(_lock_path(remote)).take()
        deadline = time.monotonic() + 10
        while len(passes_made) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stopped.set()
        thread.join(10)
    assert held_while_restarting and thread.restarts == 1 and len(passes_made) >= 4
    assert not thread.is_alive()
    assert not PollerLock(_lock_path(remote)).take()  # held until the worker's end
    thread_poller.close()
    assert PollerLock(_lock_path(remote)).take()


def test_the_worker_s_stop_ends_a_pass_after_the_job_it_is_on_and_lists_nothing(remote, dm, monkeypatch):
    """The service stop ends the thread after its current pass; the pass stops after the job it was on, and nothing is
    listed; ``run_forever`` then returns at once."""
    jobs = _submitted(remote, 3)
    listings = _listings(monkeypatch)
    stopped = threading.Event()
    reconciled = []
    real = poller._reconcile

    def reconcile_then_stop(engine, job, *arguments):
        reconciled.append(job.job_id)
        stopped.set()  # SIGTERM to the worker, during this job
        return real(engine, job, *arguments)

    monkeypatch.setattr(poller, "_reconcile", reconcile_then_stop)
    passes = _poller(remote, stopped=stopped)
    passes.cycle()
    started = time.monotonic()
    passes.run_forever()
    assert time.monotonic() - started < 1
    assert reconciled == [jobs[0]] and listings == []
    passes.close()


@pytest.mark.slow
def test_a_dm_call_running_at_the_worker_s_stop_ends_with_everything_it_started_before_the_worker_exits(
    tmp_path: Path, live: Live
) -> None:
    """A live worker (remote execution on, one submitted ``crux`` run) whose poller's DM call (the real ``DMClient``;
    ``dm.setup_file`` a script whose shell starts a child and waits, so no DM, SSH or network) still runs when SIGTERM
    comes, past the threads' join and inside ``dm.timeout_seconds``: the worker exits 0 within its bound, and by then
    the call's shell and its child are gone (``RemotePoller.close``: ``DMClient.abort``), not left to run on without a
    time limit."""
    layout = Layout(tmp_path / "remote")
    layout.experiment.mkdir(parents=True)
    pids = tmp_path / "dm-call.pids"
    setup = tmp_path / "dm.setup.sh"
    quoted = shlex.quote(str(pids))
    setup.write_text(f"echo $$ >> {quoted}\nsleep 41.25 &\necho $! >> {quoted}\nwait\n")
    values = bdp_settings(layout)
    values["dm"] = {**values["dm"], "setup_file": str(setup), "timeout_seconds": 20}
    values["poll_seconds"] = 1
    bdp = tmp_path / "bdp.yaml"
    bdp.write_text(yaml.safe_dump(values))
    run = create_wire_run(live.engine, layout, count=1, backend="crux", name="in-flight")
    submission.submit_run(run.job_id, engine=live.engine, settings=remote_settings(values), client=FakeDM())
    config = write_config(
        tmp_path / "remote.toml",
        database=live.database,
        runs_root=layout.experiment,
        remote={"enabled": True, "settings_file": bdp},
    )
    home = tmp_path / "home"  # an empty HOME: the call reads no profile of the user's
    home.mkdir()
    prelude = f"os.environ['HOME'] = {str(home)!r}\n"
    worker_process = start_worker(config, log=tmp_path / "worker.log", burst=False, prelude=prelude)

    def started() -> list[int]:
        return [int(pid) for pid in pids.read_text().split()] if pids.exists() else []

    try:
        assert wait_for(lambda: len(started()) == 2, timeout=60), worker_process.log_text()
        os.kill(worker_process.pid, signal.SIGTERM)
        began = time.monotonic()
        assert worker_process.wait(60) == 0, worker_process.log_text()
        stopped_after = time.monotonic() - began
        left = [pid for pid in started() if alive(pid)]
    finally:
        worker_process.cleanup()
        kill_quietly(started())
    assert left == [], worker_process.log_text()
    assert worker.THREAD_JOIN_SECONDS <= stopped_after < worker.THREAD_JOIN_SECONDS + 10
    assert "The worker stopped" in worker_process.log_text()


def test_the_worker_runs_the_poller_only_with_remote_execution_enabled_and_usable(tmp_path, caplog):
    """The worker's ``remote_poller``: none when remote execution is disabled, or when its settings cannot be used
    (logged, naming the key; the worker's other threads run); else the thread's state with an engine of its own and
    ``bdp.yaml``'s ``poll_seconds``."""
    layout = Layout(tmp_path)
    values = bdp_settings(layout)
    bdp = tmp_path / "bdp.yaml"
    bdp.write_text(yaml.safe_dump({**values, "poll_seconds": 7}))
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump({**values, "dm": {**values["dm"], "owner": None}}))
    stopped = threading.Event()

    disabled, _ = make_database(tmp_path / "off", remote={"enabled": False, "settings_file": bdp})
    assert worker.remote_poller(disabled, stopped) is None
    settings, engine = make_database(
        tmp_path / "on", files={"runs_root": layout.experiment}, remote={"enabled": True, "settings_file": bdp}
    )
    engine.dispose()
    enabled = worker.remote_poller(settings, stopped)
    assert enabled is not None
    try:
        assert enabled.poll_seconds == 7 and enabled.lock.path == layout.submissions / POLLER_LOCK_NAME
        assert str(settings.database.path) in str(enabled.engine.url) and not enabled.lock.held
    finally:
        enabled.engine.dispose()
    unusable = settings.model_copy(update={"remote": settings.remote.model_copy(update={"settings_file": broken})})
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        assert worker.remote_poller(unusable, stopped) is None
    assert "Remote execution is enabled, but its settings cannot be used (" in caplog.text
    assert "dm.owner" in caplog.text


def test_a_burst_worker_runs_no_poller(tmp_path):
    """The seed's burst worker runs the queue only: with remote execution enabled it takes no poller lock, makes no
    pass and leaves ``poller_at`` unset."""
    layout = Layout(tmp_path)
    bdp = tmp_path / "bdp.yaml"
    bdp.write_text(yaml.safe_dump(bdp_settings(layout)))
    settings, engine = make_database(
        tmp_path, files={"runs_root": layout.experiment}, remote={"enabled": True, "settings_file": bdp}
    )
    try:
        assert worker.run_worker(settings, burst=True) == 0
        with Session(engine) as session:
            row = session.get(Worker, worker.WORKER_ROW)
            assert row is not None and row.dispatcher_at is not None and row.poller_at is None
        assert not (layout.submissions / POLLER_LOCK_NAME).exists()
    finally:
        engine.dispose()
