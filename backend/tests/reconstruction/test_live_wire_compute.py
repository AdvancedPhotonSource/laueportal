"""The live wire compute (``reconstruction.wire.live_compute``) on lauelab's ``PreparedScan``.

The slow tests drive it as the portal does: a live wire run created and queued (``create_live_run``), a real worker
whose job process runs the real compute, files admitted with ``notify`` (``tests.wire_support``'s small synthetic
stacks), Stop through ``controls.cancel_run``. A ``Watcher`` samples ``reconstruction/scan.h5`` and
``completed.jsonl`` every 10 ms meanwhile.
"""

from __future__ import annotations

import errno
import itertools
import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import h5py
import lauelab.reconstruct.scan as lauelab_scan
import numpy as np
import psutil
import pytest
from lauelab.reconstruct import ReconstructionError, ScanReader

from laueportal.jobs import contract, controls
from laueportal.jobs.arrivals import ArrivalWriter, notify
from laueportal.jobs.contract import RunOutcome, RunRequest
from laueportal.jobs.follower import CompletedReader, Follower
from laueportal.jobs.live import read_live_document
from laueportal.jobs.rundir import ManifestEntry, file_identity
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.reconstruction import viewer
from laueportal.reconstruction.wire import live_compute
from laueportal.reconstruction.wire.live_compute import live_pool
from laueportal.science import recon
from tests.jobs.live_support import LiveRun, create_live_follower, create_live_wire_run
from tests.run_support import load_job
from tests.settings_support import make_database
from tests.wire_support import GEOMETRY, N_DEPTHS, OPTIONS, write_wire_scan
from tests.worker_support import Live, WorkerProcess, start_worker, wait_for

LINE_KEYS = ["n", "point_id", "scan_point", "path", "n_depths", "completed_at"]
"""``completed.jsonl``'s keys, in order."""


def test_the_pool_takes_the_cores_in_workers_of_num_threads_each():
    assert [live_pool(cores, threads, 3) for cores, threads in ((4, 1), (4, 2), (5, 2), (2, 8), (1, None))] == [
        (4, 1),
        (2, 2),
        (2, 2),
        (1, 2),  # never more threads than cores
        (1, 1),
    ]
    assert live_pool(None, 4, 3) == (3, 4)  # no cores (outside the dispatcher): the policy's workers


def test_the_errors_that_fail_one_point_are_lauelabs_private_tuple_as_pinned():
    """The live wire compute fails one point, its input, for exactly the errors of
    ``lauelab.reconstruct.scan.EXPECTED_POINT_ERRORS`` (lauelab d5de751's own ``prepare_scan`` and point tasks catch
    that tuple for each point); any other error fails the run. The name is not in ``lauelab.reconstruct.__all__`` (a
    private name; docs/contributing/backend.md, "Private lauelab names"): a lauelab that changes the tuple, moves or
    renames it fails here (``make check``), and which errors fail a point is decided again."""
    from lauelab.indexing import LaueError

    assert lauelab_scan.EXPECTED_POINT_ERRORS == (LaueError, MemoryError, OSError)
    assert live_compute.EXPECTED_POINT_ERRORS is lauelab_scan.EXPECTED_POINT_ERRORS


def test_the_registry_resolves_the_live_wire_compute():
    assert contract.get_live_compute_function("wire_reconstruction") is live_compute.compute_live_wire_reconstruction


# ---------------------------------------------------------------------------
# Through a real worker and job process


def _share(cores: int) -> float:
    """``live.cpu_share`` that gives a live run alone ``cores`` cores on the test's host (``dispatch.live_cores``)."""
    count = os.cpu_count() or 1
    assert count >= cores
    return min(1.0, (cores + 0.5) / count)


def _create(live: Live, tmp_path: Path, *, geometry: Path = GEOMETRY) -> LiveRun:
    params = {**OPTIONS, "geometry_file": os.fspath(geometry), "wire_edges": "leading"}
    return create_live_wire_run(live.engine, tmp_path, params=params)


def _start(live: Live, tmp_path: Path, run: LiveRun, *, publish_seconds: float = 1.0, cores: int = 1) -> WorkerProcess:
    """A burst worker whose ``[live]`` settings give the run ``cores`` and ``publish_seconds``, once its job process
    runs the run."""
    config = live.write_config(
        tmp_path / "live-worker.toml",
        live={"publish_seconds": publish_seconds, "arrivals_seconds": 0.05, "cpu_share": _share(cores)},
    )
    worker = start_worker(config, log=tmp_path / "worker.log")
    running = lambda: load_job(live.engine, run.job_id).process_pid is not None  # noqa: E731
    assert wait_for(running, 60), worker.log_text()
    return worker


def _stack(run: LiveRun, number: int, *, seed: int | None = None) -> Path:
    """The run's folder's wire stack ``number`` (``<name>_<number>.h5``)."""
    return write_wire_scan(run.folder / f"{run.name}_{number}.h5", seed=number if seed is None else seed)


def _unreadable_stack(run: LiveRun, number: int) -> Path:
    """A stack whose metadata reads (so ``PreparedScan.add`` takes it) but whose pixels do not: they are in an
    external raw file that is gone, so its reconstruction fails (a failed point)."""
    path = _stack(run, number)
    with h5py.File(path, "r") as file:
        data = file["entry1/data/data"][()]
    raw = run.folder / f"{run.name}_{number}.raw"
    data.tofile(raw)
    with h5py.File(path, "a") as file:
        del file["entry1/data/data"]
        file["entry1/data"].create_dataset(
            "data", shape=data.shape, dtype=data.dtype, external=[(os.fspath(raw), 0, data.nbytes)]
        )
    raw.unlink()
    return path


def _catalog(run: LiveRun) -> Path:
    return run.run_directory / "reconstruction" / "scan.h5"


def _lines(run: LiveRun) -> list[dict[str, Any]]:
    """``completed.jsonl``'s whole lines."""
    path = run.run_directory / "completed.jsonl"
    return [json.loads(line) for line in path.read_text().split("\n")[:-1]] if path.exists() else []


def _points(run: LiveRun) -> tuple[str, list[tuple[str, str]]]:
    with ScanReader(_catalog(run)) as scan:
        return scan.run_status, [(point.point_id, point.status) for point in scan.points]


def _counters(live: Live, job_id: int) -> tuple[int, int, int, int]:
    job = load_job(live.engine, job_id)
    return job.n_inputs, job.n_succeeded, job.n_failed, job.n_not_run


@dataclass
class Watcher:
    """Samples a live wire run's ``scan.h5`` and ``completed.jsonl`` every 10 ms in a thread: each publication of the
    catalog (a new identity) as ``(seconds, run status, points recorded: complete or failed)``, its seconds the file's
    modification time (when lauelab wrote it, however late the sampler saw it), and each line of ``completed.jsonl``
    checked when first seen against the catalog published then (its point complete there)."""

    run: LiveRun
    publications: list[tuple[float, str, int]] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    """Lines whose point the catalog did not show complete when the line was first seen."""
    errors: list[str] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> Watcher:
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)

    def recorded(self) -> list[int]:
        """Each publication's points recorded."""
        return [count for _, _, count in self.publications]

    def snapshots(self) -> list[tuple[float, str, int]]:
        """The publications while the scan ran (``snapshot``): without ``prepare_scan``'s first and ``finish()``'s."""
        return [publication for publication in self.publications[1:] if publication[1] == "running"]

    def _sample(self) -> None:
        identity: tuple[int, int] | None = None
        seen = 0
        while not self._stop.wait(0.01):
            try:
                identity, seen = self._look(identity, seen)
            except FileNotFoundError:
                pass
            except Exception as error:  # the test reports it
                self.errors.append(f"{type(error).__name__}: {error}")

    def _look(self, identity: tuple[int, int] | None, seen: int) -> tuple[tuple[int, int] | None, int]:
        lines = _lines(self.run)  # first: a line follows its point's publication
        before = _identity(_catalog(self.run))
        if before == identity and len(lines) == seen:
            return identity, seen
        run_status, points = _points(self.run)
        if _identity(_catalog(self.run)) != before:  # republished while it was read: look again
            return identity, seen
        if before != identity:
            recorded = sum(state in ("complete", "failed") for _, state in points)
            self.publications.append((before[1] / 1e9, run_status, recorded))
        complete = {point_id for point_id, state in points if state == "complete"}
        self.unlisted.extend(line["point_id"] for line in lines[seen:] if line["point_id"] not in complete)
        return before, len(lines)


def _identity(path: Path) -> tuple[int, int]:
    status = os.stat(path)
    return status.st_ino, status.st_mtime_ns


def _stop(live: Live, run: LiveRun, worker: WorkerProcess) -> None:
    controls.cancel_run(run.job_id, engine=live.engine)
    assert worker.wait(120) == 0, worker.log_text()


@pytest.mark.slow
def test_each_arrival_is_reconstructed_its_published_point_lined_and_a_stop_drains_every_admitted_point(tmp_path, live):
    run = _create(live, tmp_path)
    watcher = Watcher(run).start()
    worker = _start(live, tmp_path, run)
    try:
        for number, lines in ((3, 1), (1, 2)):  # over time, out of the scan points' order
            notify(live.engine, run.job_id, _stack(run, number))
            assert wait_for(lambda lines=lines: len(_lines(run)) == lines, 60), worker.log_text()
            succeeded = lambda lines=lines: _counters(live, run.job_id)[1] == lines  # noqa: E731
            assert wait_for(succeeded, 30), _counters(live, run.job_id)  # progress per point, during the run
        for number in (2, 5, 4):  # admitted together, then Stop at once: all of them still run
            notify(live.engine, run.job_id, _stack(run, number))
        _stop(live, run, worker)
    finally:
        worker.cleanup()
        watcher.stop()

    job = load_job(live.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FINISHED, RunPhase.FINISHED), job.messages
    assert job.messages.endswith(
        "Finished: 5 succeeded, 0 failed, 0 not run of 5; 5 of 5 point(s) reconstructed into reconstruction/"
    )
    assert _counters(live, run.job_id) == (5, 5, 0, 0)
    order = ["live_3", "live_1", "live_2", "live_5", "live_4"]
    assert _points(run) == ("finished", [(point_id, "complete") for point_id in order])  # finish(), arrival order
    lines = _lines(run)
    assert [list(line) for line in lines] == [LINE_KEYS] * 5
    assert [line["n"] for line in lines] == [0, 1, 2, 3, 4]
    assert [line["point_id"] for line in lines[:2]] == ["live_3", "live_1"]
    assert sorted(line["point_id"] for line in lines[2:]) == ["live_2", "live_4", "live_5"]
    points = run.run_directory / "reconstruction" / "points"
    for line in lines:
        assert line["scan_point"] == int(line["point_id"].removeprefix("live_"))
        assert line["path"] == os.fspath(points / f"{line['point_id']}.h5") and Path(line["path"]).is_file()
        assert line["n_depths"] == N_DEPTHS
        assert datetime.fromisoformat(line["completed_at"]).utcoffset() == timedelta(0)
    assert watcher.unlisted == [] and watcher.errors == []  # each line came after its point's publication


@pytest.mark.slow
def test_the_catalog_is_republished_at_most_every_publish_seconds_and_only_after_a_point_was_recorded(tmp_path, live):
    publish_seconds = 1.5
    run = _create(live, tmp_path)
    watcher = Watcher(run).start()
    worker = _start(live, tmp_path, run, publish_seconds=publish_seconds)
    try:
        assert wait_for(lambda: len(watcher.publications) == 1, 60), worker.log_text()  # prepare_scan's
        time.sleep(publish_seconds + 0.5)  # a publication is due, with nothing to publish
        # Point 1 is added at once (the catalog changes) and its outcome comes once the pool's first worker has
        # started: no publication before it.
        notify(live.engine, run.job_id, _stack(run, 1))
        assert wait_for(lambda: watcher.recorded() == [0, 1], 30), (watcher.recorded(), worker.log_text())
        time.sleep(2 * publish_seconds)  # nothing arrives: nothing is published
        assert watcher.recorded() == [0, 1]
        for number in (2, 3, 4):  # a burst: their outcomes come within a publication's wait
            notify(live.engine, run.job_id, _stack(run, number))
        assert wait_for(lambda: watcher.recorded()[-1] == 4, 60), (watcher.recorded(), worker.log_text())
        published = len(watcher.publications)
        refused = run.folder / "live_9.h5"
        refused.write_text("not a wire stack")  # PreparedScan.add refuses it: a failed input, no catalog change
        notify(live.engine, run.job_id, refused)
        assert wait_for(lambda: _counters(live, run.job_id)[2] == 1, 30), worker.log_text()
        time.sleep(publish_seconds + 0.5)
        assert len(watcher.publications) == published
        _stop(live, run, worker)
    finally:
        worker.cleanup()
        watcher.stop()

    assert watcher.errors == []
    snapshots = watcher.snapshots()
    # A catalog is written before the compute's clock restarts, the next one publish_seconds later at the earliest: so
    # their modification times are at least publish_seconds apart, less the file clock's tick at most (the sampler's
    # own delay does not count).
    times = [watcher.publications[0][0], *(when for when, _, _ in snapshots)]
    assert all(later - earlier >= publish_seconds - 0.02 for earlier, later in itertools.pairwise(times)), times
    recorded = [0, *(count for _, _, count in snapshots)]
    assert all(later > earlier for earlier, later in itertools.pairwise(recorded)), recorded  # a new outcome each time
    assert recorded[-1] == 4 and watcher.publications[-1][1:] == ("finished", 4)
    assert load_job(live.engine, run.job_id).status == JobStatus.FAILED  # the refused input


@pytest.mark.slow
def test_a_point_that_fails_is_marked_in_the_finished_catalog_the_others_published_and_the_run_ends_failed(
    tmp_path, live
):
    run = _create(live, tmp_path)
    worker = _start(live, tmp_path, run)
    try:
        notify(live.engine, run.job_id, _stack(run, 1))
        notify(live.engine, run.job_id, _unreadable_stack(run, 2))
        notify(live.engine, run.job_id, _stack(run, 3))
        assert wait_for(lambda: len(_lines(run)) == 2 and _counters(live, run.job_id)[2] == 1, 60), worker.log_text()
        assert load_job(live.engine, run.job_id).status == JobStatus.RUNNING  # it goes on
        _stop(live, run, worker)
    finally:
        worker.cleanup()

    job = load_job(live.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert job.messages.endswith("Incomplete: 2 succeeded, 1 failed, 0 not run of 3")
    with ScanReader(_catalog(run)) as scan:  # finish(), the failed point marked
        assert scan.run_status == "finished"
        assert [(point.point_id, point.status) for point in scan.points] == [
            ("live_1", "complete"),
            ("live_2", "failed"),
            ("live_3", "complete"),
        ]
        assert "external raw data file" in scan.points[1].error
    assert [line["point_id"] for line in _lines(run)] == ["live_1", "live_3"]
    assert sorted(os.listdir(run.run_directory / "reconstruction" / "points")) == ["live_1.h5", "live_3.h5"]
    failures = [json.loads(line) for line in (run.run_directory / "failures.jsonl").read_text().splitlines()]
    assert [(failure["input_id"], failure["error_type"]) for failure in failures] == [("live_2", "ReconstructionError")]


@pytest.mark.slow
def test_a_live_wire_run_stopped_before_anything_arrived_ends_cancelled_with_its_catalog_cancelled(tmp_path, live):
    run = _create(live, tmp_path)
    worker = _start(live, tmp_path, run)
    try:
        assert wait_for(lambda: _catalog(run).exists(), 60), worker.log_text()
        _stop(live, run, worker)
    finally:
        worker.cleanup()

    job = load_job(live.engine, run.job_id)
    assert job.status == JobStatus.CANCELLED and job.messages.endswith("Stopped before any input arrived")
    assert _points(run) == ("cancelled", [])  # finish(cancelled=True)
    assert _lines(run) == [] and not (run.run_directory / "completed.jsonl").exists()


@pytest.mark.slow
def test_a_run_level_error_closes_the_catalog_failed_and_keeps_what_was_published(tmp_path, live):
    run = _create(live, tmp_path)
    worker = _start(live, tmp_path, run)
    try:
        notify(live.engine, run.job_id, _stack(run, 1))
        assert wait_for(lambda: len(_lines(run)) == 1, 60), worker.log_text()
        with open(run.run_directory / "arrivals.jsonl", "a") as ledger:
            ledger.write("not an arrival\n")  # a whole line that is not the next arrival
        assert worker.wait(60) == 0, worker.log_text()  # no Stop: the error ends the run
    finally:
        worker.cleanup()

    job = load_job(live.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert "Run failed: ArrivalsUnreadable: line 2 of arrivals.jsonl is not an arrival" in job.messages
    assert _points(run) == ("failed", [("live_1", "complete")])  # close()
    assert [line["point_id"] for line in _lines(run)] == ["live_1"]
    assert (run.run_directory / "reconstruction" / "points" / "live_1.h5").is_file()


@pytest.mark.slow
def test_the_geometry_read_when_the_job_starts_serves_the_whole_session(tmp_path, live):
    geometry = tmp_path / "geometry.xml"
    shutil.copyfile(GEOMETRY, geometry)
    original = geometry.read_text()
    run = _create(live, tmp_path, geometry=geometry)
    worker = _start(live, tmp_path, run)
    try:
        notify(live.engine, run.job_id, _stack(run, 1, seed=7))
        assert wait_for(lambda: len(_lines(run)) == 1, 60), worker.log_text()
        changed = original.replace('<Origin unit="micron">0 0 0</Origin>', '<Origin unit="micron">0 0 20</Origin>')
        assert changed != original
        geometry.write_text(changed)  # changed on disk mid-run (not read again)
        notify(live.engine, run.job_id, _stack(run, 2, seed=7))  # the same pixels as point 1
        assert wait_for(lambda: len(_lines(run)) == 2, 60), worker.log_text()
        _stop(live, run, worker)
    finally:
        worker.cleanup()

    assert load_job(live.engine, run.job_id).status == JobStatus.FINISHED
    with h5py.File(_catalog(run), "r") as catalog:
        assert catalog["geometry/xml"][()].decode() == original
    with ScanReader(_catalog(run)) as scan, scan.point("live_1") as first, scan.point("live_2") as second:
        np.testing.assert_array_equal(second.depth_um, first.depth_um)
        for depth in range(first.shape[0]):
            np.testing.assert_array_equal(second.frame(depth), first.frame(depth))


@pytest.mark.slow
def test_the_pool_is_sized_by_the_cores_the_dispatcher_passes(tmp_path, live):
    run = _create(live, tmp_path)
    worker = _start(live, tmp_path, run, cores=2)
    job_process = psutil.Process(load_job(live.engine, run.job_id).process_pid)
    most = 0
    try:
        for number in range(1, 7):
            notify(live.engine, run.job_id, _stack(run, number))
        deadline = time.monotonic() + 60
        while len(_lines(run)) < 6 and time.monotonic() < deadline:
            workers = [child for child in job_process.children() if "spawn_main" in " ".join(child.cmdline())]
            most = max(most, len(workers))
            time.sleep(0.01)
        _stop(live, run, worker)
    finally:
        worker.cleanup()

    assert most == 2  # two workers of one thread each in two cores
    provenance = json.loads((run.run_directory / "run.json").read_text())["provenance"]
    assert (provenance["cores"], provenance["workers"], provenance["threads_per_worker"]) == (2, 2, 1)
    assert load_job(live.engine, run.job_id).status == JobStatus.FINISHED


# ---------------------------------------------------------------------------
# A service stop (in this process: the hooks say when)


class _StoppingHooks:
    """Hands out ``entries`` at once; the service stops once the first point is reported."""

    def __init__(self, entries: list[ManifestEntry]) -> None:
        self.entries = entries
        self.stopping = False
        self.failures: list[Any] = []
        self.outcome: RunOutcome | None = None
        self.messages: list[str] = []

    def should_stop(self) -> bool:
        return self.stopping

    def shutting_down(self) -> bool:
        return self.stopping

    def arrivals(self) -> list[ManifestEntry]:
        entries, self.entries = self.entries, []
        return entries

    def report_progress(self, *, succeeded: int, failed: int) -> None:
        self.stopping = True

    def record_failure(self, record: Any) -> None:
        self.failures.append(record)

    def report_outcome(self, outcome: RunOutcome) -> None:
        self.outcome = outcome

    def log(self, message: str) -> None:
        self.messages.append(message)


@pytest.mark.slow
def test_a_service_stop_starts_no_further_point_and_the_waiting_ones_are_not_run(tmp_path):
    folder = tmp_path / "input"
    folder.mkdir()
    entries = [
        ManifestEntry(
            n, f"live_{n + 1}", os.fspath(write_wire_scan(folder / f"live_{n + 1}.h5", seed=n)), 0, n + 1, None
        )
        for n in range(3)
    ]
    request = RunRequest(
        job_id=1,
        kind="wire_reconstruction",
        display_id="Reconstruction R1",
        run_directory=os.fspath(tmp_path / "run"),
        manifest_path="",
        document={"request": {**OPTIONS, "geometry_file": os.fspath(GEOMETRY), "wire_edges": "leading"}},
        n_inputs=0,
        workers=1,
        max_in_flight=None,
        live={},
        cores=1,
        publish_seconds=10.0,
    )
    hooks = _StoppingHooks(entries)

    outcome = live_compute.compute_live_wire_reconstruction(request, hooks)

    assert (outcome.n_succeeded, outcome.n_failed, outcome.stopped) == (1, 0, True)
    assert [(failure.input_id, failure.category) for failure in hooks.failures] == [
        ("live_2", "interrupted"),
        ("live_3", "interrupted"),
    ]
    with ScanReader(tmp_path / "run" / "reconstruction" / "scan.h5") as scan:
        assert scan.run_status == "cancelled"
        assert [point.status for point in scan.points] == ["complete", "unattempted", "unattempted"]
    assert [
        json.loads(line)["point_id"] for line in (tmp_path / "run" / "completed.jsonl").read_text().splitlines()
    ] == ["live_1"]


# ---------------------------------------------------------------------------
# Run-level errors (in this process: the hooks say when, a pipe or a catalog write fails)


class _Hooks:
    """Hands out ``entries`` at the first look and seals the run after ``seal_seconds`` (never by default); ``added``
    runs once they are added (the compute asks ``should_stop`` next, before it dispatches them), ``looked`` at each later
    look at the arrivals. A look past ``deadline_seconds`` raises: a run-level error of its own, so a test whose error
    never comes fails, never hangs."""

    def __init__(
        self,
        entries: list[ManifestEntry],
        *,
        added: Callable[[], None] | None = None,
        looked: Callable[[], None] | None = None,
        seal_seconds: float | None = None,
        deadline_seconds: float = 120.0,
    ) -> None:
        self.entries = entries
        started = time.monotonic()
        self.sealed_at = None if seal_seconds is None else started + seal_seconds
        self.deadline = started + deadline_seconds
        self.added = added
        self.looked = looked
        self.failures: list[Any] = []
        self.progress: list[tuple[int, int]] = []
        self.outcome: RunOutcome | None = None
        self.messages: list[str] = []

    def should_stop(self) -> bool:
        if self.added is not None:
            self.added, added = None, self.added
            added()
        return self.sealed_at is not None and time.monotonic() >= self.sealed_at

    def shutting_down(self) -> bool:
        return False

    def arrivals(self) -> list[ManifestEntry]:
        if self.entries:
            entries, self.entries = self.entries, []
            return entries
        if time.monotonic() > self.deadline:
            raise AssertionError("the run went on past the test's deadline")
        if self.looked is not None:
            self.looked()
        return []

    def report_progress(self, *, succeeded: int, failed: int) -> None:
        self.progress.append((succeeded, failed))

    def record_failure(self, record: Any) -> None:
        self.failures.append(record)

    def report_outcome(self, outcome: RunOutcome) -> None:
        self.outcome = outcome

    def log(self, message: str) -> None:
        self.messages.append(message)


def _request(run_directory: Path, *, cores: int, publish_seconds: float) -> RunRequest:
    return RunRequest(
        job_id=1,
        kind="wire_reconstruction",
        display_id="Reconstruction R1",
        run_directory=os.fspath(run_directory),
        manifest_path="",
        document={"request": {**OPTIONS, "geometry_file": os.fspath(GEOMETRY), "wire_edges": "leading"}},
        n_inputs=0,
        workers=1,
        max_in_flight=None,
        live={},
        cores=cores,
        publish_seconds=publish_seconds,
    )


def _entry(path: Path, n: int) -> ManifestEntry:
    """``path``'s input, as the live hooks hand it out: with its identity when it was admitted (now)."""
    return ManifestEntry(n, path.stem, os.fspath(path), 0, n + 1, None, identity=file_identity(path))


def _piped_stack(path: Path) -> Path:
    """A stack ``PreparedScan.add`` takes whose pixels are in an external raw file that is a named pipe nobody writes:
    its worker waits for them until it is ended (a point that runs as long as the test needs)."""
    write_wire_scan(path, seed=1)
    with h5py.File(path, "r") as file:
        data = file["entry1/data/data"][()]
    raw = path.with_suffix(".raw")
    data.tofile(raw)
    with h5py.File(path, "a") as file:
        del file["entry1/data/data"]
        file["entry1/data"].create_dataset(
            "data", shape=data.shape, dtype=data.dtype, external=[(os.fspath(raw), 0, data.nbytes)]
        )
    raw.unlink()
    os.mkfifo(raw)
    return path


def _pool_workers() -> list[psutil.Process]:
    return [child for child in psutil.Process().children() if "spawn_main" in " ".join(child.cmdline())]


def _running(process: psutil.Process) -> bool:
    try:
        return process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _labels(catalog: Path) -> tuple[str | None, list[str | None]]:
    """The run status and each point's explanation, as the wire viewer reads them (None: complete)."""
    table = recon.point_table(recon.Artifact(recon.KIND_SCAN, os.fspath(catalog)))
    return table.run_status, [viewer.explanation(row) for row in table.rows]


def test_a_stack_whose_file_changed_after_its_admission_is_a_failed_input_and_not_added(tmp_path):
    """Before ``PreparedScan.add`` the file is checked against its identity when it was admitted."""

    class Scan:
        def __init__(self) -> None:
            self.added: list[str] = []

        def add(self, source: str, *, point_id: str) -> Any:
            self.added.append(point_id)
            return SimpleNamespace(index=len(self.added) - 1)

    folder = tmp_path / "input"
    folder.mkdir()
    stacks = [folder / f"live_{n}.h5" for n in (1, 2)]
    for stack in stacks:
        stack.write_bytes(b"a stack as admitted")
    entries = [_entry(stack, n) for n, stack in enumerate(stacks)]
    stacks[0].write_bytes(b"a stack rewritten after its admission")
    scan, hooks = Scan(), _Hooks([])
    live = live_compute._LiveScan(
        scan,  # pyright: ignore[reportArgumentType]
        _request(tmp_path / "run", cores=1, publish_seconds=1.0),
        hooks,
        workers=1,
        threads=1,
        publish_seconds=1.0,
    )
    for entry in entries:
        live._add(entry)

    assert scan.added == ["live_2"]
    assert [(record.input_id, record.message) for record in hooks.failures] == [
        ("live_1", "Input changed after it was admitted")
    ]
    assert (live.failed, hooks.progress) == (1, [(0, 1)])


@pytest.mark.slow
def test_a_run_level_error_ends_the_workers_first_keeps_the_outcomes_in_and_marks_the_running_points_interrupted(
    tmp_path, monkeypatch
):
    """Three workers: live_1 runs until it is ended, live_2's worker fails (its output exists: a run-level error that
    names it, and live_2 a failed point), live_3 completes in the same wait (held: nothing is published before 60 s),
    live_4 waits. The loop waits 10 ms for an outcome, so it reaches ``looked`` long before any spawned worker can
    start its point, on any host."""
    monkeypatch.setattr(live_compute, "POLL_SECONDS", 0.01)
    folder = tmp_path / "input"
    folder.mkdir()
    paths = [_piped_stack(folder / "live_1.h5"), *(write_wire_scan(folder / f"live_{n}.h5", seed=n) for n in (2, 3, 4))]
    run = tmp_path / "run"
    points = run / "reconstruction" / "points"
    seen: dict[str, Any] = {}

    def output_exists() -> None:  # after add (which refuses an existing output), before the dispatch
        points.mkdir(exist_ok=True)
        (points / "live_2.h5").write_bytes(b"")

    def wait_for_live_3() -> None:  # the loop waits here once: live_2's error and live_3's outcome come in one wait
        assert "workers" not in seen, "live_2's error did not end the run"
        seen["progress"] = list(hooks.progress)
        assert wait_for(lambda: (points / "live_3.h5").exists(), 60)
        time.sleep(1.0)  # its outcome comes back
        seen["workers"] = _pool_workers()

    hooks = _Hooks([_entry(path, n) for n, path in enumerate(paths)], added=output_exists, looked=wait_for_live_3)
    try:
        with pytest.raises(
            ReconstructionError, match="^reconstruction worker failed on point live_2: .*already exists"
        ):
            live_compute.compute_live_wire_reconstruction(_request(run, cores=3, publish_seconds=60.0), hooks)
        left = [worker for worker in seen.get("workers", []) if _running(worker)]
    finally:
        for worker in _pool_workers():
            worker.kill()

    assert seen["progress"] == [] and len(seen["workers"]) == 3  # nothing was taken before the loop waited
    assert left == []  # ended before close(): no point file can come after it
    assert sorted(os.listdir(points)) == ["live_2.h5", "live_3.h5"]  # live_2's: the test's
    exists = f"ReconstructionError: point 'live_2': {points / 'live_2.h5'} already exists"
    assert _labels(run / "reconstruction" / "scan.h5") == (
        "failed",
        [
            "Point live_1 was interrupted before its file was recorded. It has no images.",
            f"Point live_2 failed: {exists}. It has no images.",
            None,
            "Point live_4 was not reconstructed because the run stopped first. It has no images.",
        ],
    )
    assert [(record.input_id, record.category, record.message) for record in hooks.failures] == [
        ("live_2", "error", exists)
    ]
    lines = [json.loads(line) for line in (run / "completed.jsonl").read_text().splitlines()]
    assert [(line["n"], line["point_id"]) for line in lines] == [(0, "live_3")]  # each complete point lined
    assert hooks.outcome is not None and (hooks.outcome.n_succeeded, hooks.outcome.n_failed) == (1, 1)


@pytest.mark.slow
def test_at_a_run_level_error_a_point_reconstructed_before_the_workers_were_ended_is_recorded_not_interrupted(
    tmp_path, monkeypatch
):
    """Two workers: live_1 runs until it is ended, live_2 is reconstructed and its outcome comes back while the loop
    looks at the arrivals, which then fail. live_2's outcome came in before the workers were ended: it is recorded
    complete and lined, live_1 interrupted."""
    monkeypatch.setattr(live_compute, "POLL_SECONDS", 0.01)
    folder = tmp_path / "input"
    folder.mkdir()
    paths = [_piped_stack(folder / "live_1.h5"), write_wire_scan(folder / "live_2.h5", seed=2)]
    run = tmp_path / "run"
    points = run / "reconstruction" / "points"

    def live_2_ends_then_the_ledger_fails() -> None:
        assert wait_for(lambda: (points / "live_2.h5").exists(), 60)
        time.sleep(1.0)  # its outcome comes back
        raise OSError("the ledger cannot be read")

    hooks = _Hooks([_entry(path, n) for n, path in enumerate(paths)], looked=live_2_ends_then_the_ledger_fails)
    try:
        with pytest.raises(OSError, match="the ledger cannot be read"):
            live_compute.compute_live_wire_reconstruction(_request(run, cores=2, publish_seconds=60.0), hooks)
    finally:
        for worker in _pool_workers():
            worker.kill()

    assert _labels(run / "reconstruction" / "scan.h5") == (
        "failed",
        ["Point live_1 was interrupted before its file was recorded. It has no images.", None],
    )
    lines = [json.loads(line) for line in (run / "completed.jsonl").read_text().splitlines()]
    assert [(line["n"], line["point_id"]) for line in lines] == [(0, "live_2")]
    assert hooks.outcome is not None and (hooks.outcome.n_succeeded, hooks.outcome.n_failed) == (1, 0)


@pytest.mark.slow
def test_a_point_whose_catalog_write_failed_in_record_gets_its_line_from_the_catalog_close_publishes(
    tmp_path, monkeypatch
):
    """The first catalog with live_1 recorded fails to be written, inside lauelab's ``record`` (its own five seconds
    have passed): the point stays recorded complete, and ``close()``'s catalog shows it."""
    folder = tmp_path / "input"
    folder.mkdir()
    run = tmp_path / "run"
    written: list[str] = []
    write_catalog = lauelab_scan.write_catalog

    def failing_once(*args: Any, **kwargs: Any) -> None:
        written.append(kwargs["run_status"].name.lower())  # the run status published
        if len(written) == 2:
            raise OSError("the catalog could not be written (simulated)")
        write_catalog(*args, **kwargs)

    monkeypatch.setattr(lauelab_scan, "write_catalog", failing_once)
    hooks = _Hooks([_entry(write_wire_scan(folder / "live_1.h5", seed=1), 0)])

    with pytest.raises(OSError, match="simulated") as raised:
        live_compute.compute_live_wire_reconstruction(_request(run, cores=1, publish_seconds=5.5), hooks)

    assert "record" in [entry.name for entry in raised.traceback]
    # Its line is there when the compute raises: before the executor ends the run's row.
    lines = [json.loads(line) for line in (run / "completed.jsonl").read_text().splitlines()]
    assert [(line["n"], line["point_id"], line["scan_point"]) for line in lines] == [(0, "live_1", 1)]
    assert written == ["running", "running", "failed"]  # prepare_scan's, the one that failed, close()'s
    with ScanReader(run / "reconstruction" / "scan.h5") as scan:
        assert (scan.run_status, [(point.point_id, point.status) for point in scan.points]) == (
            "failed",
            [("live_1", "complete")],
        )


@pytest.mark.slow
def test_a_point_lauelab_published_complete_inside_record_is_lined_though_the_final_catalog_then_fails(
    tmp_path, monkeypatch
):
    """Sealed at 6 s with live_1's outcome held (``publish_seconds`` 60): recording it publishes on lauelab's own clock
    (its five seconds have passed), then ``finish()``'s catalog cannot be written. The catalog left on disk shows
    live_1 complete, so its line must be there."""
    folder = tmp_path / "input"
    folder.mkdir()
    run = tmp_path / "run"
    written: list[str] = []
    write_catalog = lauelab_scan.write_catalog

    def failing_at_finish(*args: Any, **kwargs: Any) -> None:
        written.append(kwargs["run_status"].name.lower())  # the run status published
        if written[-1] == "finished":
            raise OSError("the final catalog could not be written (simulated)")
        write_catalog(*args, **kwargs)

    monkeypatch.setattr(lauelab_scan, "write_catalog", failing_at_finish)
    hooks = _Hooks([_entry(write_wire_scan(folder / "live_1.h5", seed=1), 0)], seal_seconds=6.0)

    with pytest.raises(OSError, match="simulated"):
        live_compute.compute_live_wire_reconstruction(_request(run, cores=1, publish_seconds=60.0), hooks)

    assert written == ["running", "running", "finished"]  # prepare_scan's, lauelab's own inside record, finish()'s
    with ScanReader(run / "reconstruction" / "scan.h5") as scan:
        assert (scan.run_status, [(point.point_id, point.status) for point in scan.points]) == (
            "running",
            [("live_1", "complete")],
        )
    lines = [json.loads(line) for line in (run / "completed.jsonl").read_text().splitlines()]
    assert [(line["n"], line["point_id"], line["scan_point"]) for line in lines] == [(0, "live_1", 1)]


def test_of_two_points_whose_workers_raise_in_one_wait_the_run_names_the_first_and_both_are_held_failed():
    """As in the live LaueGo compute, ``_collect`` takes every outcome of a wait and raises the first run-level error
    among them, in dispatch order, not the last."""
    hooks = _Hooks([])
    loop = object.__new__(live_compute._LiveScan)  # the loop's state that ``_collect`` and ``_take`` use
    loop.hooks, loop.held, loop.succeeded, loop.failed, loop.running = hooks, [], 0, 0, {}
    for n, message in ((1, "the first"), (2, "the second")):
        future: Future[Any] = Future()
        future.set_exception(RuntimeError(message))
        entry = ManifestEntry(n - 1, f"live_{n}", f"/input/live_{n}.h5", 0, n, None)
        loop.running[future] = live_compute._Point(entry, SimpleNamespace(index=n - 1, point_id=f"live_{n}"))
    with pytest.raises(
        ReconstructionError, match="^reconstruction worker failed on point live_1: RuntimeError: the first$"
    ):
        loop._collect()
    assert [(outcome.point_id, outcome.status) for outcome in loop.held] == [("live_1", "failed"), ("live_2", "failed")]
    assert [record.input_id for record in hooks.failures] == ["live_1", "live_2"]


def test_a_failed_append_is_written_again_first_as_it_was_so_a_follower_that_read_it_misses_no_point(
    tmp_path, monkeypatch
):
    """Through ``_line`` and a real follower: a follower's look between a write of ``completed.jsonl`` and its
    truncation (the ``fsync`` failed) takes wire_12's line; wire_11, earlier in the catalog, completes before the retry.
    The retry writes the failed write's lines first, byte for byte, then wire_11's, so the follower, which keeps its
    offset, admits every point once (in catalog order the retry put wire_11 where wire_12 was read, and the follower
    then read wire_12 again and never admitted wire_11). The next publication lines only the point completed since,
    wire_13 (the retried lines are not written again)."""
    _, engine = make_database(tmp_path)
    try:
        wire = create_live_wire_run(engine, tmp_path, name="wire")
        job_id = create_live_follower(engine, tmp_path, wire)
        directory = Path(load_job(engine, job_id).run_directory)
        writer = ArrivalWriter(engine, job_id, directory, read_live_document(directory))
        reader = CompletedReader(wire.run_directory)
        follows = Follower(writer, reader, ended=lambda: False, interval=5.0, job_id=job_id)
        points = wire.run_directory / "reconstruction" / "points"
        points.mkdir(parents=True)
        completed = wire.run_directory / "completed.jsonl"
        loop = object.__new__(live_compute._LiveScan)  # the state ``_line`` uses, the scan's outcomes a stand-in
        loop.run_directory, loop.lined, loop.unlined, loop.points = os.fspath(wire.run_directory), set(), [], {}
        outcomes: list[SimpleNamespace] = []
        loop.scan = SimpleNamespace(outcomes=outcomes)

        def complete(index: int) -> None:
            name = f"wire_{10 + index}"
            output = points / f"{name}.h5"
            output.write_bytes(b"a point file")
            entry = ManifestEntry(index, name, f"/input/{name}.h5", 0, 10 + index, None)
            loop.points[index] = live_compute._Point(entry, SimpleNamespace(n_depths=11))
            outcomes.append(SimpleNamespace(status="complete", index=index, point_id=name, output=os.fspath(output)))
            outcomes.sort(key=lambda outcome: outcome.index)  # the scan's outcomes are in catalog order

        complete(0)
        loop._line()
        follows.step()
        complete(2)  # wire_11 (catalog index 1) still running
        real_fsync, failed_write = os.fsync, []

        def failing_fsync(descriptor: int) -> None:
            monkeypatch.setattr(os, "fsync", real_fsync)
            failed_write.append(completed.read_bytes())
            follows.step()  # the follower's look inside the window takes wire_12's line
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(os, "fsync", failing_fsync)
        with pytest.raises(OSError, match="Input/output error"):
            loop._line()
        assert len(completed.read_bytes()) < len(failed_write[0])  # truncated back
        complete(1)  # before the retry
        loop._line()
        follows.step()
        arrivals = [json.loads(line) for line in (directory / "arrivals.jsonl").read_text().splitlines()]
        assert [arrival["point_id"] for arrival in arrivals] == ["wire_10", "wire_12", "wire_11"]
        assert completed.read_bytes().startswith(failed_write[0])
        lines = [json.loads(line) for line in completed.read_text().splitlines()]
        assert [(line["n"], line["point_id"]) for line in lines] == [(0, "wire_10"), (1, "wire_12"), (2, "wire_11")]

        complete(3)  # wire_13, after the retry
        loop._line()
        follows.step()
        lines = [json.loads(line) for line in completed.read_text().splitlines()]
        expected = [(0, "wire_10"), (1, "wire_12"), (2, "wire_11"), (3, "wire_13")]
        assert [(line["n"], line["point_id"]) for line in lines] == expected
        arrivals = [json.loads(line) for line in (directory / "arrivals.jsonl").read_text().splitlines()]
        assert [arrival["point_id"] for arrival in arrivals] == ["wire_10", "wire_12", "wire_11", "wire_13"]
    finally:
        engine.dispose()
