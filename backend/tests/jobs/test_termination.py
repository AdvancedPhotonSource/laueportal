"""Hard-termination tests with real processes.

A run process (standing in for a job process, ``jobs.process``) is started in its own
process group with a compute that spawns a grandchild and then blocks. The tests kill it
in the ways a worker or the OS would and check that compute descendants do not
survive and that the database never keeps a dead run Running.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
from sqlalchemy import create_engine

from laueportal.jobs import lifecycle, processes, reconciler
from laueportal.jobs.state import JobStatus, RunPhase
from tests.run_support import load_job

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # backend/

RUN_SCRIPT = textwrap.dedent(
    """
    import os, subprocess, sys, time, json
    from pathlib import Path

    db_file, run_dir_parent, marker = sys.argv[1], sys.argv[2], sys.argv[3]
    from laueportal.jobs import contract, executor
    from laueportal.jobs.contract import RunOutcome
    from laueportal.jobs.rundir import FailureRecord
    from tests.run_support import FAST_POLICY, claimed, publish_wire_run
    from tests.settings_support import make_database

    _, engine = make_database(Path(run_dir_parent), database={"path": Path(db_file)})

    def compute(request, entries, hooks):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        Path(marker).write_text(json.dumps({"grandchild": child.pid, "run": os.getpid()}))
        succeeded = 0
        for entry in entries:
            while not hooks.should_stop():
                time.sleep(0.02)  # block until cancelled, shut down, or killed
            hooks.record_failure(FailureRecord.not_run(entry, message="stopped"))
        return RunOutcome(n_succeeded=succeeded, n_failed=0, stopped=True)

    contract._REGISTRY["wire_reconstruction"] = compute
    run = publish_wire_run(engine, Path(run_dir_parent), count=2)
    claimed(engine, run.job_id)
    Path(marker + ".job").write_text(str(run.job_id))
    executor.execute_run(run.job_id, engine=engine, policy=FAST_POLICY)
    """
)


def _start_run(tmp_path):
    db_file = tmp_path / "termination.db"
    marker = tmp_path / "marker.json"
    # The child finds tests in its cwd and laueportal under src/ (as pytest does), not via an installed checkout.
    pythonpath = os.pathsep.join(filter(None, [str(PROJECT_ROOT / "src"), os.environ.get("PYTHONPATH")]))
    process = subprocess.Popen(
        [sys.executable, "-c", RUN_SCRIPT, str(db_file), str(tmp_path), str(marker)],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": pythonpath},
        start_new_session=True,  # like a job process: a session of its own
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not marker.exists():
        if process.poll() is not None:
            raise AssertionError(f"run process exited early: {process.communicate()[1]}")
        time.sleep(0.05)
    assert marker.exists(), "run process never started its compute"
    import json

    info = json.loads(marker.read_text())
    job_id = int((tmp_path / "marker.json.job").read_text())
    engine = create_engine(f"sqlite:///{db_file}")
    _wait_for(lambda: load_job(engine, job_id).status == JobStatus.RUNNING)
    return process, info["grandchild"], job_id, engine


def _wait_for(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _alive(pid):
    # The process can be reaped between any two psutil calls, so a vanished process is "not alive".
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _cleanup(process, grandchild):
    for pid in (grandchild, process.pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.communicate(timeout=10)
    except Exception:
        pass


def test_sigkill_of_the_run_process_is_reconciled_and_its_group_swept(tmp_path):
    process, grandchild, job_id, engine = _start_run(tmp_path)
    try:
        assert _alive(grandchild)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)

        job = load_job(engine, job_id)
        assert job.status == JobStatus.RUNNING  # nothing could finalize it: the process is gone
        assert job.heartbeat_at is not None
        assert _alive(grandchild)  # an orphaned compute process survives the kill itself...

        # ...until the worker sweeps the job process's group.
        assert lifecycle.sweep_process_group(process.pid) is True
        assert _wait_for(lambda: not _alive(grandchild))

        time.sleep(1.1)
        reconciled = reconciler.reconcile_runs(engine, stale_after_seconds=1)
        assert reconciled.interrupted == [job_id]
        job = load_job(engine, job_id)
        assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)
        assert (job.n_succeeded, job.n_failed, job.n_not_run) == (0, 0, 2)
        assert "no heartbeat since" in job.messages
        assert not reconciler.reconcile_runs(engine, stale_after_seconds=1)
    finally:
        _cleanup(process, grandchild)


def test_shutdown_signal_drains_finalizes_and_kills_descendants(tmp_path):
    process, grandchild, job_id, engine = _start_run(tmp_path)
    try:
        os.kill(process.pid, lifecycle.SHUTDOWN_SIGNAL)
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        assert not _alive(grandchild)  # terminate_descendants ran in the executor
        job = load_job(engine, job_id)
        assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.INTERRUPTED)
        assert job.n_not_run == 2
        assert job.messages.startswith("Stopped by service shutdown")
        assert os.path.exists(os.path.join(job.run_directory, "run.json"))
        assert os.path.exists(job.failure_report_path)
    finally:
        _cleanup(process, grandchild)


def test_sigterm_kills_the_run_process_without_leaving_it_running_forever(tmp_path):
    """The worker's kill of a job process (an overdue one, or at a second SIGTERM) takes its whole group; the DB is
    repaired by reconciliation."""

    process, grandchild, job_id, engine = _start_run(tmp_path)
    try:
        os.killpg(process.pid, signal.SIGKILL)  # what the worker's kill does
        process.wait(timeout=10)
        assert _wait_for(lambda: not _alive(grandchild))
        time.sleep(1.1)
        assert reconciler.reconcile_runs(engine, stale_after_seconds=1).interrupted == [job_id]
        assert load_job(engine, job_id).phase == RunPhase.INTERRUPTED
    finally:
        _cleanup(process, grandchild)


def test_sweep_never_targets_the_current_process_group():
    assert lifecycle.sweep_process_group(os.getpgid(0)) is False
    assert lifecycle.sweep_process_group(0) is False
    assert lifecycle.sweep_process_group(2**22 + 12345) is False


HELPER_LEFT_BEHIND = textwrap.dedent(
    """
    import subprocess, sys
    # A process whose command line marks it as multiprocessing's resource tracker (lauelab's spawn-context pools
    # start one; it outlives the run process for a moment): it writes its file after ``argv[2]`` seconds.
    helper = subprocess.Popen(
        [sys.executable, "-c", "import pathlib, sys, time; time.sleep(float(sys.argv[2])); "
         "pathlib.Path(sys.argv[1]).write_text('ended by itself')", sys.argv[1], sys.argv[2],
         "multiprocessing.resource_tracker"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(helper.pid, flush=True)
    """
)


def _leave_a_helper(tmp_path: Path, seconds: float) -> tuple[int, int, Path]:
    """A run process (a process group of its own) that exits at once, leaving a helper in its group."""
    ended = tmp_path / "helper-ended"
    process = subprocess.Popen(
        [sys.executable, "-c", HELPER_LEFT_BEHIND, str(ended), str(seconds)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout, _ = process.communicate(timeout=30)
    return process.pid, int(stdout.split()[0]), ended


def test_the_sweep_lets_a_multiprocessing_helper_end_by_itself_and_reports_nothing(tmp_path):
    """After a run that used lauelab's spawn-context pool, the worker would log "killed processes left behind": the
    sweep would kill the resource tracker, which ends by itself just after the run process. It is given time to end (and
    to do its own clean-up); nothing is reported."""
    group, helper, ended = _leave_a_helper(tmp_path, seconds=0.3)
    try:
        assert lifecycle.sweep_process_group(group) is False
        assert ended.read_text() == "ended by itself"
    finally:
        _cleanup_pid(helper)


def test_a_helper_that_outlives_the_grace_is_killed_and_reported(tmp_path):
    group, helper, ended = _leave_a_helper(tmp_path, seconds=60)
    try:
        assert lifecycle.sweep_process_group(group, helper_grace_seconds=0.2) is True
        assert _wait_for(lambda: not _alive(helper)) and not ended.exists()
    finally:
        _cleanup_pid(helper)


def _cleanup_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_ending_a_pools_workers_counts_one_that_outlives_sigkill():
    """A live compute's pool worker in uninterruptible I/O (its sentinel never ready) gets SIGTERM, then SIGKILL, and is
    counted, so the run's log can say so."""

    class Stuck:
        def __init__(self) -> None:
            self.sentinel, self.writer = os.pipe()
            self.signals: list[str] = []

        def terminate(self) -> None:
            self.signals.append("SIGTERM")

        def kill(self) -> None:
            self.signals.append("SIGKILL")

    class Pool:
        def __init__(self, process: Stuck) -> None:
            self._processes = {1: process}
            self.shut_down = False

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.shut_down = not wait and cancel_futures

    stuck = Stuck()
    pool = Pool(stuck)
    try:
        assert processes.end_pool_workers(pool, grace_seconds=0.05) == 1  # pyright: ignore[reportArgumentType]
    finally:
        os.close(stuck.sentinel)
        os.close(stuck.writer)
    assert stuck.signals == ["SIGTERM", "SIGKILL"] and pool.shut_down
