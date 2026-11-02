"""The live LaueGo compute on raw frames (``indexing.lauego.live_compute``).

The slow tests drive it as the portal does: a live LaueGo run created and queued (``create_live_run``), a real worker
whose job process runs the real compute, frames admitted with ``notify`` (copies of ``tests/fixtures/lauelab``'s
synthetic Ni frames), Stop through ``controls.cancel_run``; a service stop, a run-level error with a frame running and
one held, a changed frame, the depth override and a worker's indexer run in this process. A ``Watcher`` samples the
preview ``output.h5`` every 10 ms. The fast tests run the loop in this process without lauelab's native code: the
pool's workers as threads, stand-ins for the worker's task and the session's indexer (the merges' spacing, a worker's
own error, the outcomes kept at a run-level error, the shards' batches, the geometry override).
"""

from __future__ import annotations

import inspect
import itertools
import json
import os
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import h5py
import numpy as np
import psutil
import pytest
from lauelab.indexing import (
    FrameResult,
    Indexer,
    IndexParams,
    InputError,
    PeakParams,
    ResultsWriter,
    ScanFrame,
    WorkerError,
    load_crystal,
    validate_results_file,
)
from lauelab.indexing.indexer import PEAK_DTYPE  # a stand-in result's peaks (no public name in lauelab d5de751)

from laueportal.indexing.lauego import live_compute
from laueportal.indexing.lauego.compute import settings_from_request
from laueportal.indexing.lauego.live_compute import Calibration, live_workers
from laueportal.jobs import contract, controls
from laueportal.jobs.arrivals import notify
from laueportal.jobs.contract import RunOutcome, RunRequest
from laueportal.jobs.rundir import ManifestEntry, file_identity
from laueportal.jobs.state import JobStatus, RunPhase
from tests.jobs.live_support import LiveRun, create_live_lauego_run
from tests.results_support import FRAME_TIMINGS, assert_equal_values, results_by_frame
from tests.run_support import load_job
from tests.worker_support import Live, WorkerProcess, start_worker, wait_for

FIXTURES = Path(__file__).parents[1] / "fixtures" / "lauelab"
GEOMETRY = FIXTURES / "geoN_2022-03-29_14-15-05.xml"
CRYSTAL = FIXTURES / "Ni.xml"
GRAIN = "synthetic_ni_grain_a.h5"  # 24 peaks, 1 pattern, about 0.25 s
TWO_GRAINS = "synthetic_ni_two_grains.h5"  # 48 peaks, 2 patterns, about 0.4 s
EMPTY = "synthetic_ni_empty.h5"  # 1 peak, no pattern, about 0.02 s
PARAMETERS = {
    "geometry_file": os.fspath(GEOMETRY),
    "crystal_file": os.fspath(CRYSTAL),
    "mask_file": None,
    "box_size": 18,
    "max_rfactor": 0.5,
    "min_size": 3.0,
    "min_separation": 20,
    "threshold": None,
    "threshold_ratio": None,
    "peak_shape": "Lorentzian",
    "max_peaks": 200,
    "smooth": False,
    "index_kev_max_calc": 17.2,
    "index_kev_max_test": 35.0,
    "index_angle_tolerance": 0.1,
    "index_cone": 72.0,
    "index_h": 0,
    "index_k": 0,
    "index_l": 1,
    "max_number": 300,
    "depth": None,
}
"""The batch compute's tests' values (``test_lauego_compute._parameters``), for the runs in this process."""


def test_the_pool_has_a_worker_per_core_of_the_dispatchers_share():
    assert [live_workers(cores, 4) for cores in (1, 2, 3)] == [1, 2, 3]
    assert live_workers(None, 4) == 4  # no cores (outside the dispatcher): the policy's workers


def test_the_registry_resolves_the_live_lauego_compute():
    assert contract.get_live_compute_function("lauego_indexing") is live_compute.compute_live_lauego_indexing


# ---------------------------------------------------------------------------
# Through a real worker and job process


def _share(cores: int) -> float:
    """``live.cpu_share`` that gives a live run alone ``cores`` cores on the test's host (``dispatch.live_cores``)."""
    count = os.cpu_count() or 1
    assert count >= cores
    return min(1.0, (cores + 0.5) / count)


def _create(live: Live, tmp_path: Path, *, geometry: Path = GEOMETRY, crystal: Path = CRYSTAL) -> LiveRun:
    params = {"geometry_file": os.fspath(geometry), "crystal_file": os.fspath(crystal)}
    return create_live_lauego_run(live.engine, tmp_path, params=params)


def _start(live: Live, tmp_path: Path, run: LiveRun, *, publish_seconds: float = 1.0, cores: int = 1) -> WorkerProcess:
    """A worker whose ``[live]`` settings give the run ``cores`` and ``publish_seconds`` (the policy's ``workers`` 1,
    so a pool that ignored the cores would show it), once its job process runs the run."""
    config = live.write_config(
        tmp_path / "live-worker.toml",
        live={"publish_seconds": publish_seconds, "arrivals_seconds": 0.05, "cpu_share": _share(cores)},
        workers=1,
    )
    worker = start_worker(config, log=tmp_path / "worker.log")
    running = lambda: load_job(live.engine, run.job_id).process_pid is not None  # noqa: E731
    assert wait_for(running, 60), worker.log_text()
    return worker


def _frame(run: LiveRun, number: int, fixture: str = GRAIN) -> Path:
    """The run's folder's frame ``number`` (``<name>_<number>.h5``), a copy of ``fixture``."""
    return Path(shutil.copyfile(FIXTURES / fixture, run.folder / f"{run.name}_{number}.h5"))


def _corrupt(run: LiveRun, number: int) -> Path:
    return run.file(number, b"not an HDF5 frame")


def _piped(path: Path) -> Path:
    """A frame whose pixels are in an external raw file that is a named pipe nobody writes: its worker waits for them
    until it is ended (a frame that runs as long as the test needs)."""
    shutil.copyfile(FIXTURES / GRAIN, path)
    with h5py.File(path, "r") as file:
        data = file["entry1/data/data"][()]
    raw = path.with_suffix(".raw")
    with h5py.File(path, "a") as file:
        del file["entry1/data/data"]
        file["entry1/data"].create_dataset(
            "data", shape=data.shape, dtype=data.dtype, external=[(os.fspath(raw), 0, data.nbytes)]
        )
    raw.unlink(missing_ok=True)
    os.mkfifo(raw)
    return path


def _counters(live: Live, job_id: int) -> tuple[int, int, int, int]:
    job = load_job(live.engine, job_id)
    return job.n_inputs, job.n_succeeded, job.n_failed, job.n_not_run


def _results(run: LiveRun) -> Path:
    return run.run_directory / "output.h5"


def _xml(run: LiveRun) -> Path:
    return run.run_directory / "output.xml"


def _frame_ids(path: Path) -> list[str]:
    with h5py.File(path, "r") as file:
        return [value.decode() for value in file["frames/frame_ids"][()]]


def _preview(run: LiveRun) -> list[str]:
    """The preview's frames ([] while there is none)."""
    try:
        return _frame_ids(_results(run))
    except FileNotFoundError:
        return []


def _shards(run: LiveRun) -> list[Path]:
    """The shards' folders, ``batch_0``, ``batch_1``, … in their order."""
    folder = run.run_directory / "shards"
    names = sorted(os.listdir(folder), key=lambda name: int(name.removeprefix("batch_"))) if folder.exists() else []
    assert all(name.startswith("batch_") for name in names), names
    return [folder / name for name in names]


def _xml_images(path: Path) -> list[str]:
    return [step.findtext("detector/inputImage") or "" for step in ET.parse(path).getroot().findall("step")]


def _failures(run: LiveRun) -> list[dict[str, Any]]:
    path = run.run_directory / "failures.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _stop(live: Live, run: LiveRun, worker: WorkerProcess) -> None:
    controls.cancel_run(run.job_id, engine=live.engine)
    assert worker.wait(120) == 0, worker.log_text()


@dataclass
class Watcher:
    """Samples a live LaueGo run's preview ``output.h5`` every 10 ms in a thread: each publication (a new identity)
    as ``(seconds, frames)``, its seconds the file's modification time (when lauelab wrote it, however late the
    sampler saw it)."""

    run: LiveRun
    publications: list[tuple[float, list[str]]] = field(default_factory=list)
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

    def frames(self) -> list[int]:
        """Each publication's number of frames."""
        return [len(frames) for _, frames in self.publications]

    def _sample(self) -> None:
        identity: tuple[int, int] | None = None
        while not self._stop.wait(0.01):
            try:
                identity = self._look(identity)
            except FileNotFoundError:
                pass
            except Exception as error:  # the test reports it
                self.errors.append(f"{type(error).__name__}: {error}")

    def _look(self, identity: tuple[int, int] | None) -> tuple[int, int] | None:
        before = _identity(_results(self.run))
        if before == identity:
            return identity
        frames = _frame_ids(_results(self.run))
        if _identity(_results(self.run)) != before:  # republished while it was read: look again
            return identity
        self.publications.append((before[1] / 1e9, frames))
        return before


def _identity(path: Path) -> tuple[int, int]:
    status = os.stat(path)
    return status.st_ino, status.st_mtime_ns


@pytest.mark.slow
def test_each_frame_is_indexed_into_shards_in_arrival_order_and_a_stop_drains_every_admitted_frame(tmp_path, live):
    run = _create(live, tmp_path)
    worker = _start(live, tmp_path, run)
    try:
        for number, fixture, preview in ((3, EMPTY, ["frame_3"]), (1, GRAIN, ["frame_3", "frame_1"])):  # over time
            notify(live.engine, run.job_id, _frame(run, number, fixture))
            succeeded = lambda preview=preview: _counters(live, run.job_id)[1] == len(preview)  # noqa: E731
            assert wait_for(succeeded, 60), worker.log_text()  # progress per input, during the run
            assert wait_for(lambda preview=preview: _preview(run) == preview, 30), worker.log_text()
        assert not _xml(run).exists()  # the Output XML is stitched at the seal
        for number, fixture in ((2, TWO_GRAINS), (4, GRAIN)):  # admitted together, then Stop at once: both still run
            notify(live.engine, run.job_id, _frame(run, number, fixture))
        _stop(live, run, worker)
    finally:
        worker.cleanup()

    job = load_job(live.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FINISHED, RunPhase.FINISHED), job.messages
    assert job.messages.endswith(
        "Finished: 4 succeeded, 0 failed, 0 not run of 4; 4 frames, 4 patterns, 97 peaks in output.h5; 3 frames indexed"
    )
    assert _counters(live, run.job_id) == (4, 4, 0, 0)
    order = ["frame_3", "frame_1", "frame_2", "frame_4"]  # arrival order
    assert validate_results_file(_results(run), frame_ids=order).n_frames == 4
    shards = _shards(run)
    assert [frame_id for shard in shards for frame_id in _frame_ids(shard / "output.h5")] == order
    for shard in shards:
        assert sorted(os.listdir(shard)) == ["output.h5", "output.xml"]
        validate_results_file(shard / "output.h5", frame_ids=_frame_ids(shard / "output.h5"))
    assert _xml_images(_xml(run)) == [os.fspath(run.folder / f"{frame_id}.h5") for frame_id in order]
    summary = json.loads((run.run_directory / "run.json").read_text())
    assert summary["artifacts"] == {"results": os.fspath(_results(run)), "xml": os.fspath(_xml(run))}
    assert summary["provenance"]["n_shards"] == len(shards) and summary["counters"]["n_indexed"] == 3
    assert summary["warnings"] == [] and _failures(run) == []


@pytest.mark.slow
def test_the_preview_is_republished_at_most_every_publish_seconds_and_only_after_new_results(tmp_path, live):
    publish_seconds = 1.5
    run = _create(live, tmp_path)
    watcher = Watcher(run).start()
    worker = _start(live, tmp_path, run, publish_seconds=publish_seconds)
    try:
        time.sleep(publish_seconds + 0.5)  # a publication would be due, with nothing to publish
        assert watcher.publications == []
        notify(live.engine, run.job_id, _frame(run, 1))
        assert wait_for(lambda: watcher.frames() == [1], 30), (watcher.frames(), worker.log_text())
        time.sleep(2 * publish_seconds)  # nothing arrives: nothing is published
        assert watcher.frames() == [1]
        for number, fixture in ((2, GRAIN), (3, TWO_GRAINS), (4, EMPTY)):  # a burst
            notify(live.engine, run.job_id, _frame(run, number, fixture))
        assert wait_for(lambda: watcher.frames()[-1:] == [4], 60), (watcher.frames(), worker.log_text())
        published = len(watcher.publications)
        notify(live.engine, run.job_id, _corrupt(run, 5))  # a failed frame: no result, so no publication
        assert wait_for(lambda: _counters(live, run.job_id)[2] == 1, 30), worker.log_text()
        time.sleep(publish_seconds + 0.5)
        assert len(watcher.publications) == published
        _stop(live, run, worker)
    finally:
        worker.cleanup()
        watcher.stop()

    assert watcher.errors == []
    # A preview is written before the compute's clock restarts, the next one publish_seconds later at the earliest: so
    # their modification times are at least publish_seconds apart, less the file clock's tick at most. The seal had
    # nothing new to publish.
    times = [when for when, _ in watcher.publications]
    assert all(later - earlier >= publish_seconds - 0.02 for earlier, later in itertools.pairwise(times)), times
    counts = watcher.frames()
    assert all(later > earlier for earlier, later in itertools.pairwise(counts)), counts  # new results each time
    assert watcher.publications[-1][1] == ["frame_1", "frame_2", "frame_3", "frame_4"] == _preview(run)
    assert load_job(live.engine, run.job_id).status == JobStatus.FAILED  # the failed frame


@pytest.mark.slow
def test_a_frame_that_fails_is_recorded_the_others_merged_and_the_run_ends_failed(tmp_path, live):
    run = _create(live, tmp_path)
    worker = _start(live, tmp_path, run)
    try:
        notify(live.engine, run.job_id, _frame(run, 1))
        notify(live.engine, run.job_id, _corrupt(run, 2))
        notify(live.engine, run.job_id, _frame(run, 3, EMPTY))
        processed = lambda: _counters(live, run.job_id)[1:3] == (2, 1) and _preview(run) == ["frame_1", "frame_3"]  # noqa: E731
        assert wait_for(processed, 60), worker.log_text()
        assert load_job(live.engine, run.job_id).status == JobStatus.RUNNING  # it goes on
        _stop(live, run, worker)
    finally:
        worker.cleanup()

    job = load_job(live.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert job.messages.endswith("Incomplete: 2 succeeded, 1 failed, 0 not run of 3")
    assert [(f["input_id"], f["error_type"], f["category"]) for f in _failures(run)] == [
        ("frame_2", "InputError", "input")
    ]
    assert _preview(run) == ["frame_1", "frame_3"]
    assert _xml_images(_xml(run)) == [os.fspath(run.folder / f"frame_{n}.h5") for n in (1, 3)]


@pytest.mark.slow
def test_a_live_lauego_run_stopped_before_anything_arrived_ends_cancelled_with_nothing_published(tmp_path, live):
    run = _create(live, tmp_path)
    worker = _start(live, tmp_path, run)
    try:
        time.sleep(1.0)  # its compute has started
        _stop(live, run, worker)
    finally:
        worker.cleanup()

    job = load_job(live.engine, run.job_id)
    assert job.status == JobStatus.CANCELLED and job.messages.endswith("Stopped before any input arrived")
    assert not _results(run).exists() and not _xml(run).exists() and _shards(run) == []
    summary = json.loads((run.run_directory / "run.json").read_text())
    assert summary["artifacts"] == {} and summary["provenance"]["n_shards"] == 0


@pytest.mark.slow
def test_a_run_level_error_marks_the_running_frame_publishes_the_frames_indexed_and_keeps_what_was_published(
    tmp_path, live
):
    """Frame 2 runs until it is ended (its pixels come from a pipe); frame 3, indexed in the pool's second worker,
    waits for it (arrival order); then a line of the ledger that is not an arrival fails the run."""
    run = _create(live, tmp_path)
    worker = _start(live, tmp_path, run, cores=2)
    try:
        notify(live.engine, run.job_id, _frame(run, 1))
        assert wait_for(lambda: _preview(run) == ["frame_1"], 60), worker.log_text()
        notify(live.engine, run.job_id, _piped(run.folder / "frame_2.h5"))
        notify(live.engine, run.job_id, _frame(run, 3, EMPTY))
        assert wait_for(lambda: _counters(live, run.job_id)[:2] == (3, 2), 60), worker.log_text()
        assert _preview(run) == ["frame_1"]  # frame 3 is held behind frame 2
        with open(run.run_directory / "arrivals.jsonl", "a") as ledger:
            ledger.write("not an arrival\n")  # a whole line that is not the next arrival
        assert worker.wait(60) == 0, worker.log_text()  # no Stop: the error ends the run
    finally:
        worker.cleanup()

    job = load_job(live.engine, run.job_id)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert "Run failed: ArrivalsUnreadable: line 4 of arrivals.jsonl is not an arrival" in job.messages
    assert [(f["input_id"], f["category"]) for f in _failures(run)] == [("frame_2", "interrupted")]
    assert _preview(run) == ["frame_1", "frame_3"]  # what was published kept, the frame indexed published
    assert [_frame_ids(shard / "output.h5") for shard in _shards(run)] == [["frame_1"], ["frame_3"]]
    assert not _xml(run).exists()  # no XML at a run-level error, as a batch run's
    summary = json.loads((run.run_directory / "run.json").read_text())
    assert summary["artifacts"] == {"results": os.fspath(_results(run))}


def _changed(geometry: Path, crystal: Path) -> None:
    """The detector 10 mm further away, a larger lattice: each changes the frames' results."""
    text = geometry.read_text()
    assert '<P unit="mm">28.720 3.010 513.097</P>' in text
    geometry.write_text(text.replace("28.720 3.010 513.097", "28.720 3.010 523.097"))
    text = crystal.read_text()
    assert text.count("0.35238") == 3
    crystal.write_text(text.replace("0.35238", "0.36"))


def _expected(
    path: Path, frame: Path, frame_id: str, geometry: Path, crystal: Path, depth: float | None = None
) -> dict[str, Any]:
    """``frame``'s results with ``geometry`` and ``crystal`` (at ``depth``), as a results file holds them."""
    settings = settings_from_request(
        {**PARAMETERS, "geometry_file": os.fspath(geometry), "crystal_file": os.fspath(crystal)}
    )
    indexer = Indexer(
        geometry, crystal, peak_params=settings.peak_params, index_params=settings.index_params, detector_index=0
    )
    indexer.write_results([indexer.index(frame, depth=depth, keep_image=False)], path, frame_ids=[frame_id])
    return results_by_frame(path)[frame_id]


def _same_frame(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Every column but the timings and the frame's own name and path."""
    try:
        for name, values in expected.items():
            if name not in (*FRAME_TIMINGS, "frames/frame_ids", "frames/input_images"):
                assert_equal_values(actual[name], values, name)
    except AssertionError:
        return False
    return True


@pytest.mark.slow
def test_the_calibrations_read_when_the_job_starts_serve_the_whole_session(tmp_path, live):
    """The geometry and the crystal change on disk once the job has read them (its first arrival counted: its loop runs)
    and before its pool's worker builds its indexer (at its first frame): every frame is indexed with the calibration
    read at the start, and the results file embeds it."""
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    geometry, crystal = (Path(shutil.copy(source, calibration)) for source in (GEOMETRY, CRYSTAL))
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    shutil.copy(geometry, pristine)
    shutil.copy(crystal, pristine)
    run = _create(live, tmp_path, geometry=geometry, crystal=crystal)
    worker = _start(live, tmp_path, run)
    try:
        notify(live.engine, run.job_id, _frame(run, 1))
        assert wait_for(lambda: _counters(live, run.job_id)[0] == 1, 60, interval=0.005), worker.log_text()
        _changed(geometry, crystal)  # not read again
        assert wait_for(lambda: _counters(live, run.job_id)[1] == 1, 60), worker.log_text()
        notify(live.engine, run.job_id, _frame(run, 2))  # the same pixels as frame 1
        assert wait_for(lambda: _counters(live, run.job_id)[1] == 2, 60), worker.log_text()
        _stop(live, run, worker)
    finally:
        worker.cleanup()

    assert load_job(live.engine, run.job_id).status == JobStatus.FINISHED
    frame = run.folder / "frame_1.h5"
    original = _expected(tmp_path / "original.h5", frame, "frame_1", pristine / GEOMETRY.name, pristine / CRYSTAL.name)
    changed = _expected(tmp_path / "changed.h5", frame, "frame_1", geometry, crystal)
    assert not _same_frame(changed, original)  # the change matters
    found = results_by_frame(_results(run))
    assert _same_frame(found["frame_1"], original) and _same_frame(found["frame_2"], original)
    with h5py.File(_results(run), "r") as results:
        assert results["geometry/xml"][()].decode() == (pristine / GEOMETRY.name).read_text()
        assert results["geometry"].attrs["path"] == os.fspath(geometry)
        assert results["crystal"].attrs["source"] == os.fspath(crystal)
    steps = ET.parse(_xml(run)).getroot().findall("step")
    assert [step.findtext("detector/geoFile") for step in steps] == [os.fspath(geometry)] * 2


@pytest.mark.slow
def test_the_pool_is_sized_by_the_cores_and_the_frames_keep_their_arrival_order(tmp_path, live):
    """Two frames at once start both workers; then frame 3 (about 0.4 s) and frame 4 (about 0.02 s) go to the two idle
    workers together: frame 4 ends first and waits for frame 3."""
    run = _create(live, tmp_path)
    worker = _start(live, tmp_path, run, cores=2)
    job_process = psutil.Process(load_job(live.engine, run.job_id).process_pid)
    most = 0

    def count() -> None:
        nonlocal most
        workers = [child for child in job_process.children() if "spawn_main" in " ".join(child.cmdline())]
        most = max(most, len(workers))

    def processed(number: int) -> bool:
        count()
        return _counters(live, run.job_id)[1] == number

    try:
        for number in (1, 2):
            notify(live.engine, run.job_id, _frame(run, number))
        assert wait_for(lambda: processed(2), 60, interval=0.01), worker.log_text()
        notify(live.engine, run.job_id, _frame(run, 3, TWO_GRAINS))
        notify(live.engine, run.job_id, _frame(run, 4, EMPTY))
        assert wait_for(lambda: processed(4), 60, interval=0.01), worker.log_text()
        _stop(live, run, worker)
    finally:
        worker.cleanup()

    assert most == 2  # one worker per core
    provenance = json.loads((run.run_directory / "run.json").read_text())["provenance"]
    assert (provenance["cores"], provenance["workers"]) == (2, 2)
    order = ["frame_1", "frame_2", "frame_3", "frame_4"]
    assert _preview(run) == order
    assert [frame_id for shard in _shards(run) for frame_id in _frame_ids(shard / "output.h5")] == order
    assert load_job(live.engine, run.job_id).status == JobStatus.FINISHED


# ---------------------------------------------------------------------------
# In this process: a service stop, a run-level error with a frame running and one held, a worker's indexer


class _Hooks:
    """Hands out ``entries`` at the first look; ``looked`` runs at each later look (it may raise: a run-level error);
    ``shutdown_after`` looks at ``shutting_down``, the service stops (``should_stop`` True from then on); ``sealed``:
    the run is sealed once the entries are handed out (``should_stop`` True from then on: drain). A look past
    ``deadline_seconds`` raises, so a test whose end never comes fails, never hangs."""

    def __init__(
        self,
        entries: list[ManifestEntry],
        *,
        looked: Callable[[], None] | None = None,
        shutdown_after: int | None = None,
        sealed: bool = False,
        deadline_seconds: float = 120.0,
    ) -> None:
        self.entries = entries
        self.looked = looked
        self.shutdown_after = shutdown_after
        self.sealed = sealed
        self.shutdown_calls = 0
        self.deadline = time.monotonic() + deadline_seconds
        self.failures: list[Any] = []
        self.progress: list[tuple[int, int]] = []
        self.outcome: RunOutcome | None = None
        self.messages: list[str] = []

    def _stopping(self) -> bool:
        return self.shutdown_after is not None and self.shutdown_calls > self.shutdown_after

    def should_stop(self) -> bool:
        return self._stopping() or (self.sealed and not self.entries)

    def shutting_down(self) -> bool:
        self.shutdown_calls += 1
        return self._stopping()

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


def _request(
    run_directory: Path,
    *,
    cores: int,
    publish_seconds: float,
    source: str = "notify",
    parameters: dict[str, Any] = PARAMETERS,
) -> RunRequest:
    return RunRequest(
        job_id=1,
        kind="lauego_indexing",
        display_id="Indexing I1",
        run_directory=os.fspath(run_directory),
        manifest_path="",
        document={"request": parameters, "output": {"xml": "output.xml"}},
        n_inputs=0,
        workers=1,
        max_in_flight=None,
        live={"source": source},
        cores=cores,
        publish_seconds=publish_seconds,
    )


def _entries(*paths: Path) -> list[ManifestEntry]:
    """``paths``' inputs, as the live hooks hand them out: with their identities when they were admitted (now)."""
    return [
        ManifestEntry(n, path.stem, os.fspath(path), 0, n + 1, None, identity=file_identity(path))
        for n, path in enumerate(paths)
    ]


def _copies(folder: Path, *fixtures: str) -> list[Path]:
    folder.mkdir(exist_ok=True)
    return [
        Path(shutil.copyfile(FIXTURES / fixture, folder / f"frame_{n}.h5")) for n, fixture in enumerate(fixtures, 1)
    ]


def _pool_workers() -> list[psutil.Process]:
    return [child for child in psutil.Process().children() if "spawn_main" in " ".join(child.cmdline())]


def _running(process: psutil.Process) -> bool:
    try:
        return process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.slow
def test_a_service_stop_starts_no_further_frame_and_the_waiting_ones_are_not_run(tmp_path):
    """One worker: frame 1 starts, then the service stops: frames 2 and 3 are not run; frame 1 ends and is published,
    its XML stitched."""
    frames = _copies(tmp_path / "input", GRAIN, GRAIN, GRAIN)
    hooks = _Hooks(_entries(*frames), shutdown_after=1)
    run = tmp_path / "run"
    outcome = live_compute.compute_live_lauego_indexing(_request(run, cores=1, publish_seconds=1.0), hooks)

    assert (outcome.n_succeeded, outcome.n_failed, outcome.stopped) == (1, 0, True)
    assert [(record.input_id, record.category, record.message) for record in hooks.failures] == [
        (f"frame_{n}", "interrupted", "service stopped before this input") for n in (2, 3)
    ]
    assert _frame_ids(run / "output.h5") == ["frame_1"]
    assert _xml_images(run / "output.xml") == [os.fspath(frames[0])]
    assert "the service stopped the run: the frames not started were not run" in hooks.messages


@pytest.mark.slow
def test_a_run_level_error_ends_the_workers_first_marks_the_running_frame_and_publishes_the_frame_held(
    tmp_path, monkeypatch
):
    """Two workers: frame 1 runs until it is ended (a pipe), frame 2 is indexed and held behind it (arrival order);
    then the arrivals fail. When the compute raises its workers are gone, frame 1 is interrupted and frame 2
    published."""
    monkeypatch.setattr(live_compute, "POLL_SECONDS", 0.01)
    folder = tmp_path / "input"
    folder.mkdir()
    frames = [_piped(folder / "frame_1.h5"), Path(shutil.copyfile(FIXTURES / EMPTY, folder / "frame_2.h5"))]
    seen: dict[str, Any] = {}

    def fail_once_frame_2_is_in() -> None:
        if hooks.progress == [(1, 0)]:
            seen["workers"] = _pool_workers()
            raise OSError("the ledger cannot be read")

    hooks = _Hooks(_entries(*frames), looked=fail_once_frame_2_is_in)
    run = tmp_path / "run"
    try:
        with pytest.raises(OSError, match="the ledger cannot be read"):
            live_compute.compute_live_lauego_indexing(_request(run, cores=2, publish_seconds=60.0), hooks)
        left = [worker for worker in seen.get("workers", []) if _running(worker)]
    finally:
        for worker in _pool_workers():
            worker.kill()

    assert len(seen["workers"]) == 2 and left == []  # ended first
    assert [(record.input_id, record.category) for record in hooks.failures] == [("frame_1", "interrupted")]
    assert _frame_ids(run / "output.h5") == ["frame_2"]  # nothing was published before the error
    assert hooks.outcome is not None and hooks.outcome.artifacts == {"results": os.fspath(run / "output.h5")}
    assert not (run / "output.xml").exists()


@pytest.mark.slow
def test_a_worker_builds_its_indexer_once_from_the_calibration_read_at_the_start(tmp_path, monkeypatch):
    settings = settings_from_request(PARAMETERS)
    calibration = Calibration.read(settings)
    built: list[Indexer] = []
    build = Calibration.indexer

    def counted(self: Calibration, settings: Any) -> Indexer:
        built.append(build(self, settings))
        return built[-1]

    monkeypatch.setattr(Calibration, "indexer", counted)
    monkeypatch.setattr(live_compute, "_session", None)
    monkeypatch.setattr(live_compute, "_indexer", None)
    frames = _copies(tmp_path / "input", GRAIN, EMPTY)
    live_compute._start_worker(calibration, settings)
    outcomes = [live_compute._index_frame(os.fspath(frame), None, None) for frame in frames]

    assert [(result is not None, error) for result, error, _ in outcomes] == [(True, None), (True, None)]
    assert len(built) == 1  # at the first frame, then kept
    assert [result.image for result, _, _ in outcomes] == [None, None]  # keep_image=False: no frame held


@pytest.mark.slow
def test_a_frame_whose_file_changed_after_its_admission_is_a_failed_input_and_the_run_goes_on(tmp_path):
    """Frame 2 is rewritten (another fixture) once it was admitted: its worker refuses it; the others are indexed and
    published."""
    frames = _copies(tmp_path / "input", GRAIN, EMPTY, GRAIN)
    entries = _entries(*frames)
    shutil.copyfile(FIXTURES / TWO_GRAINS, frames[1])
    hooks = _Hooks(entries, sealed=True)
    run = tmp_path / "run"
    outcome = live_compute.compute_live_lauego_indexing(_request(run, cores=1, publish_seconds=1.0), hooks)

    assert (outcome.n_succeeded, outcome.n_failed) == (2, 1)
    assert [(record.input_id, record.error_type, record.category, record.message) for record in hooks.failures] == [
        ("frame_2", "InputError", "input", "Input changed after it was admitted")
    ]
    assert _frame_ids(run / "output.h5") == ["frame_1", "frame_3"]


def test_a_worker_refuses_a_frame_whose_file_changed_after_its_admission_or_while_it_was_indexed(tmp_path, monkeypatch):
    """The worker's checks (``_index_frame``), with a stand-in for its indexer: the file's identity against the one
    admitted, before the indexing (the frame is then not indexed) and after it."""
    frame = tmp_path / "frame_1.h5"
    frame.write_bytes(b"a frame as admitted")
    indexed: list[str] = []

    class Rewriting:
        rewrite = False

        def index(self, source: str, **options: Any) -> str:
            indexed.append(source)
            if self.rewrite:
                Path(source).write_bytes(b"a frame rewritten while it was indexed")
            return "result"

    indexer = Rewriting()
    monkeypatch.setattr(live_compute, "_session", (SimpleNamespace(mask=None), None))
    monkeypatch.setattr(live_compute, "_indexer", indexer)
    admitted = file_identity(frame)
    assert live_compute._index_frame(os.fspath(frame), None, admitted)[:2] == ("result", None)
    frame.write_bytes(b"a frame rewritten after its admission")
    result, error, _ = live_compute._index_frame(os.fspath(frame), None, admitted)
    assert (result, type(error), str(error), len(indexed)) == (
        None,
        InputError,
        "Input changed after it was admitted",
        1,  # not indexed
    )
    indexer.rewrite = True
    result, error, _ = live_compute._index_frame(os.fspath(frame), None, file_identity(frame))
    assert (result, type(error), str(error), len(indexed)) == (
        None,
        InputError,
        "Input changed while it was indexed",
        2,
    )
    indexer.rewrite = False
    admitted = file_identity(frame)
    frame.write_bytes(frame.read_bytes().upper())  # rewritten in place, the same size ...
    os.utime(frame, ns=(admitted[1] + 10**9, admitted[1] + 10**9))  # ... at another time (any clock)
    rewritten = live_compute._index_frame(os.fspath(frame), None, admitted)
    admitted = file_identity(frame)
    replacement = tmp_path / "replacement.h5"
    replacement.write_bytes(frame.read_bytes().lower())  # the same size ...
    os.utime(replacement, ns=(admitted[1], admitted[1]))  # ... and time: only the inode tells
    os.replace(replacement, frame)
    replaced = live_compute._index_frame(os.fspath(frame), None, admitted)
    assert [(str(error), len(indexed)) for _, error, _ in (rewritten, replaced)] == [
        ("Input changed after it was admitted", 2),
        ("Input changed after it was admitted", 2),
    ]


# ---------------------------------------------------------------------------
# In this process, without lauelab's native code: the pool's workers as threads, each frame given to a stand-in for
# the worker's task; the session's indexer a stand-in too (what the shards' writer and XML take from it). The shards,
# the preview's merges and the XML's stitch are lauelab's.


class _StandInIndexer:
    def __init__(self, geometry_file: str) -> None:
        self.crystal = load_crystal(CRYSTAL)
        self.geometry = SimpleNamespace(path=Path(geometry_file))
        self.peak_params = PeakParams()
        self.index_params = IndexParams()
        self.detector_index = 0
        self.detector_id = "stand-in"
        self.cosmic_filter = False

    def write_many_xml(self, results: list[FrameResult], path: Path) -> None:
        steps = "".join(
            f"<step><detector><inputImage>{result.input_image}</inputImage><geoFile>{self.geometry.path}</geoFile>"
            "</detector></step>"
            for result in results
        )
        Path(path).write_text(f'<?xml version="1.0" encoding="utf-8"?>\n<AllSteps>{steps}</AllSteps>\n')


def _stand_in_result(source: str) -> FrameResult:
    """A frame's result with no peak and no pattern, as lauelab's writer and merges take it."""
    return FrameResult(
        peaks=np.empty(0, dtype=PEAK_DTYPE),
        patterns=(),
        threshold_used=1.0,
        total_sum=0.0,
        sum_above_threshold=0.0,
        num_above_threshold=0,
        peaksearch_seconds=0.0,
        indexing_seconds=0.0,
        input_image=source,
        image_shape=(8, 8),
    )


@dataclass
class _Threads:
    """The stand-in task: ``held`` frames (by input ID) wait for their event; ``raised`` ones raise their error; the
    others are indexed at once. ``release()`` lets every held frame go (a test's ``finally``: no thread is left)."""

    held: dict[str, threading.Event] = field(default_factory=dict)
    raised: dict[str, BaseException] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    futures: dict[str, Future[Any]] = field(default_factory=dict)
    """Each frame's task, as the compute submitted it."""

    failed: dict[str, BaseException] = field(default_factory=dict)
    """Frames whose task returns their input error."""
    frames: list[Any] = field(default_factory=list)
    """What each task was given to index, in the order they started."""

    def task(self, frame: str | ScanFrame, depth: float | None, identity: Any) -> Any:
        name = _name(frame)
        self.started.append(name)
        self.frames.append(frame)
        if name in self.held:
            assert self.held[name].wait(60), f"{name} was never released"
        if name in self.raised:
            raise self.raised[name]
        if name in self.failed:
            return None, self.failed[name], 0.01
        return _stand_in_result(frame.path if isinstance(frame, ScanFrame) else frame), None, 0.01

    def release(self) -> None:
        for event in self.held.values():
            event.set()


def _name(frame: str | ScanFrame) -> str:
    """A task's frame by its input ID: a raw frame's stem, a followed point's ``<point_id>_<depth index>``."""
    return f"{frame.point_id}_{frame.depth_index}" if isinstance(frame, ScanFrame) else Path(frame).stem


def _in_threads(monkeypatch: pytest.MonkeyPatch, threads: _Threads) -> None:
    class Pool(ThreadPoolExecutor):
        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
            future = super().submit(fn, *args, **kwargs)
            threads.futures[_name(args[0])] = future
            return future

    monkeypatch.setattr(live_compute, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(live_compute, "ProcessPoolExecutor", lambda max_workers, **_: Pool(max_workers))
    monkeypatch.setattr(live_compute, "_index_frame", threads.task)
    monkeypatch.setattr(Calibration, "indexer", lambda self, settings: _StandInIndexer(self.geometry_file))


def _inputs(folder: Path, count: int) -> list[Path]:
    folder.mkdir(exist_ok=True)
    paths = [folder / f"frame_{n}.h5" for n in range(1, count + 1)]
    for path in paths:
        path.write_bytes(b"a frame")
    return paths


@pytest.mark.parametrize(
    ("raised", "message", "records"),
    [
        (
            RuntimeError("the worker's own error"),
            "indexing worker failed on input frame_2: RuntimeError: the worker's own error",
            [("frame_2", "error", "the worker's own error"), ("frame_1", "interrupted", None)],
        ),
        (
            live_compute.IndexerNotBuilt("ValueError: no geometry"),
            "indexing worker initialization failed: ValueError: no geometry",
            [("frame_1", "interrupted", None), ("frame_2", "interrupted", None)],
        ),
    ],
    ids=["the-frames-own", "no-indexer"],
)
def test_a_workers_own_error_names_its_frame_which_failed_and_the_other_running_frames_are_interrupted(
    tmp_path, monkeypatch, raised, message, records
):
    """Two workers: frame 1 runs on, frame 2's worker raises, frame 3 waits. The run's error names frame 2 and frame 2
    failed, as lauelab's batch ``WorkerError`` ("indexing worker failed on input ..."); frame 1 is interrupted. An
    indexer a worker could not build is no frame's: both are interrupted."""
    threads = _Threads(held={"frame_1": threading.Event()}, raised={"frame_2": raised})
    _in_threads(monkeypatch, threads)
    hooks = _Hooks(_entries(*_inputs(tmp_path / "input", 3)))
    try:
        with pytest.raises(WorkerError) as caught:
            live_compute.compute_live_lauego_indexing(_request(tmp_path / "run", cores=2, publish_seconds=60.0), hooks)
    finally:
        threads.release()

    assert str(caught.value) == message and caught.value.__cause__ is raised
    interrupted = "the run failed (a run-level error) while this input was being indexed"
    assert [
        (record.input_id, record.category, None if record.message == interrupted else record.message)
        for record in hooks.failures
    ] == records
    assert threads.started == ["frame_1", "frame_2"]  # frame 3 waited: not run


def test_a_worker_that_cannot_build_its_indexer_raises_an_error_that_is_no_frames(tmp_path, monkeypatch):
    def cannot(self: Calibration, settings: Any) -> Indexer:
        raise ValueError("no geometry")

    monkeypatch.setattr(Calibration, "indexer", cannot)
    monkeypatch.setattr(live_compute, "_session", (Calibration("geometry.xml", b"", load_crystal(CRYSTAL), None), None))
    monkeypatch.setattr(live_compute, "_indexer", None)
    with pytest.raises(live_compute.IndexerNotBuilt, match="^ValueError: no geometry$"):
        live_compute._index_frame(os.fspath(tmp_path / "frame_1.h5"), None, None)


def test_at_a_run_level_error_a_frame_indexed_before_the_workers_were_ended_is_published_not_interrupted(
    tmp_path, monkeypatch
):
    """Two workers: frames 1 and 2 run; at the next look at the arrivals frame 2 ends, then the arrivals fail. Its
    outcome came in before the workers were ended: frame 2 is published, frame 1 interrupted."""
    threads = _Threads(held={"frame_1": threading.Event(), "frame_2": threading.Event()})
    _in_threads(monkeypatch, threads)

    def frame_2_ends_then_the_ledger_fails() -> None:
        threads.held["frame_2"].set()
        threads.futures["frame_2"].result(timeout=60)
        raise OSError("the ledger cannot be read")

    hooks = _Hooks(_entries(*_inputs(tmp_path / "input", 2)), looked=frame_2_ends_then_the_ledger_fails)
    run = tmp_path / "run"
    try:
        with pytest.raises(OSError, match="the ledger cannot be read"):
            live_compute.compute_live_lauego_indexing(_request(run, cores=2, publish_seconds=60.0), hooks)
    finally:
        threads.release()

    assert [(record.input_id, record.category) for record in hooks.failures] == [("frame_1", "interrupted")]
    assert _frame_ids(run / "output.h5") == ["frame_2"]
    assert hooks.outcome is not None and (hooks.outcome.n_succeeded, hooks.outcome.n_failed) == (1, 0)


def _live_indexing(
    tmp_path: Path,
    *,
    publish_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    geometry: Path = GEOMETRY,
    workers: int = 1,
) -> Any:
    """The compute's loop object with ``workers``, its calibration ``geometry`` read now, its session indexer a
    stand-in, its clock ``clock``."""
    return live_compute._LiveIndexing(
        _request(tmp_path / "run", cores=workers, publish_seconds=publish_seconds),
        _Hooks([]),
        RunOutcome(n_succeeded=0, n_failed=0),
        settings=settings_from_request(PARAMETERS),
        calibration=Calibration(os.fspath(geometry), geometry.read_bytes(), load_crystal(CRYSTAL), None),
        indexer=_StandInIndexer(os.fspath(geometry)),  # pyright: ignore[reportArgumentType]
        workers=workers,
        publish_seconds=publish_seconds,
        clock=clock,
    )


def _shard_one(live: Any, name: str) -> None:
    """A shard of the indexed frame ``name``, as the loop writes one."""
    source = f"/input/{name}.h5"
    entry = ManifestEntry(0, name, source, 0, 1, None)
    live.released = [live_compute._Frame(entry, result=_stand_in_result(source), done=True)]
    live._shard()


def test_the_next_preview_waits_nine_times_the_last_merges_seconds_once_that_is_longer_than_publish_seconds(
    tmp_path, monkeypatch
):
    """The rule, by the run's clock (each merge takes the seconds the test says): the next merge is due
    ``max(publish_seconds, 9 × the last merge's seconds)`` after the last ended, so merges take at most about a tenth of
    the run's time as the preview grows."""
    now = [0.0]
    merge_seconds = [0.0625]
    merge = live_compute.merge_results_files

    def timed(*args: Any, **kwargs: Any) -> Any:
        now[0] += merge_seconds[0]
        return merge(*args, **kwargs)

    monkeypatch.setattr(live_compute, "merge_results_files", timed)
    live = _live_indexing(tmp_path, publish_seconds=1.0, clock=lambda: now[0])

    def published(at: float, name: str) -> bool:
        _shard_one(live, name)
        now[0] = at
        before = list(live.published)
        live._publish_if_due()
        live._merged(wait=True)
        return live.published != before

    try:
        assert published(0.0, "frame_1")  # the first at once; its merge ends at 0.0625 s
        merge_seconds[0] = 2.0  # the preview has grown
        assert not published(1.0, "frame_2")  # publish_seconds (9 × 0.0625 s is shorter) not passed yet
        assert published(1.0625, "frame_3")  # its merge ends at 3.0625 s
        assert not published(3.0625 + 17.5, "frame_4")  # 9 × 2 s
        assert published(3.0625 + 18.0, "frame_5")
    finally:
        live._close_merger()
    assert _frame_ids(tmp_path / "run" / "output.h5") == [f"frame_{n}" for n in range(1, 6)]


def test_the_pool_goes_on_indexing_while_the_preview_is_merged(tmp_path, monkeypatch):
    """One worker, three frames: the first preview's merge waits until frame 3 has started, which needs the loop to
    collect frame 2 and dispatch frame 3 meanwhile (it waits 5 s at most)."""
    threads = _Threads()
    _in_threads(monkeypatch, threads)
    merge = live_compute.merge_results_files
    seen: list[bool] = []

    def slow(*args: Any, **kwargs: Any) -> Any:
        if not seen:
            seen.append(wait_for(lambda: "frame_3" in threads.started, 5, interval=0.01))
        return merge(*args, **kwargs)

    monkeypatch.setattr(live_compute, "merge_results_files", slow)
    hooks = _Hooks(_entries(*_inputs(tmp_path / "input", 3)), sealed=True)
    run = tmp_path / "run"
    outcome = live_compute.compute_live_lauego_indexing(_request(run, cores=1, publish_seconds=0.01), hooks)

    assert seen == [True]
    assert outcome.n_succeeded == 3 and _frame_ids(run / "output.h5") == ["frame_1", "frame_2", "frame_3"]


def _geometry_group(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    with h5py.File(path, "r") as file:
        group = file["geometry"]
        assert isinstance(group, h5py.Group)
        return dict(group.attrs), {name: group[name][()] for name in group}  # pyright: ignore[reportIndexIssue]


def test_the_geometry_text_is_written_with_lauelabs_own_private_dataset_writer():
    """``_SessionResultsWriter._write_geometry`` writes the geometry's text with
    ``lauelab._results_layout.write_dataset(target, path, values)``, a private module (docs/contributing/backend.md,
    "Private lauelab names"): the function lauelab d5de751's ``ResultsWriter`` writes it with. Its signature and
    lauelab's own use of it are pinned here, what it writes by the next test (the geometry group equals lauelab's own
    writer's), so a lauelab that moves, renames or changes it fails ``make check``."""
    import sys

    from lauelab import _results_layout

    assert list(inspect.signature(_results_layout.write_dataset).parameters) == ["target", "path", "values"]
    assert getattr(sys.modules[ResultsWriter.__module__], "write_dataset", None) is _results_layout.write_dataset
    assert live_compute.write_dataset is _results_layout.write_dataset


def test_the_shards_and_the_preview_embed_the_geometry_read_at_the_start_through_lauelabs_writer(tmp_path):
    """Reading the calibration once rests on a private lauelab method: ``ResultsWriter._write_geometry(self, target)``,
    which lauelab d5de751 calls once per file it starts and ``_SessionResultsWriter`` overrides. The geometry file
    changes once the session has read it; then two shards are written, merged into the preview and the XML stitched, by
    the loop's own code. Each results file's geometry group is the one lauelab's own writer writes for the file as it
    was read (no member or attribute more or less), and the stitched XML is the shards' steps as they wrote them. So a
    lauelab that renames that method, changes its signature, stops calling it or writes more into the group fails here
    (``make check``)."""
    method = vars(ResultsWriter).get("_write_geometry")
    assert method is not None and list(inspect.signature(method).parameters) == ["self", "target"]
    geometry = Path(shutil.copy(GEOMETRY, tmp_path / GEOMETRY.name))
    indexer = _StandInIndexer(os.fspath(geometry))
    reference = tmp_path / "reference.h5"
    with ResultsWriter(
        reference,
        crystal=indexer.crystal,
        geometry=indexer.geometry,
        peak_params=indexer.peak_params,
        index_params=indexer.index_params,
        detector_index=indexer.detector_index,
        detector_id=indexer.detector_id,
        cosmic_filter=indexer.cosmic_filter,
    ):
        pass
    expected = _geometry_group(reference)
    assert expected[0] == {"path": os.fspath(geometry)} and expected[1]["xml"].decode() == GEOMETRY.read_text()
    live = _live_indexing(tmp_path, publish_seconds=1.0, geometry=geometry)
    geometry.write_text(GEOMETRY.read_text().replace("28.720 3.010 513.097", "28.720 3.010 523.097"))
    try:
        _shard_one(live, "frame_1")
        _shard_one(live, "frame_2")
        live._publish()
    finally:
        live._close_merger()
    live.stitch()

    run = tmp_path / "run"
    for path in (*(run / "shards" / f"batch_{n}" / "output.h5" for n in (0, 1)), run / "output.h5"):
        assert _geometry_group(path) == expected, path

    def steps(path: Path) -> list[tuple[str | None, str | None]]:
        return [
            (step.findtext("detector/inputImage"), step.findtext("detector/geoFile"))
            for step in ET.parse(path).getroot().findall("step")
        ]

    shards = [step for n in (0, 1) for step in steps(run / "shards" / f"batch_{n}" / "output.xml")]
    assert steps(run / "output.xml") == shards == [(f"/input/frame_{n}.h5", os.fspath(geometry)) for n in (1, 2)]


def test_a_shard_holds_a_frame_per_worker_or_its_first_frame_waits_shard_seconds(tmp_path):
    """Two workers, by the run's clock: frame 1 indexed waits for frame 2 (a frame per worker makes a shard), frame 3
    alone waits ``SHARD_SECONDS`` (1 s), then is a shard of its own (batches, not a shard per frame)."""
    now = [0.0]
    live = _live_indexing(tmp_path, publish_seconds=10.0, clock=lambda: now[0], workers=2)
    frames = []
    for n in (1, 2, 3):
        source = f"/input/frame_{n}.h5"
        frames.append(live_compute._Frame(ManifestEntry(n - 1, f"frame_{n}", source, 0, n, None)))
    live.arrived.extend(frames)

    def indexed(at: float, n: int) -> list[list[str]]:
        now[0] = at
        frames[n - 1].result, frames[n - 1].done = _stand_in_result(frames[n - 1].entry.source), True
        live._shard_if_due()
        return [shard.frame_ids for shard in live.shards]

    assert indexed(0.0, 1) == []  # one frame of two
    assert indexed(0.5, 2) == [["frame_1", "frame_2"]]
    assert indexed(1.0, 3) == [["frame_1", "frame_2"]]
    now[0] = 1.9375
    live._shard_if_due()
    assert [shard.frame_ids for shard in live.shards] == [["frame_1", "frame_2"]]
    now[0] = 2.0
    live._shard_if_due()
    assert [shard.frame_ids for shard in live.shards] == [["frame_1", "frame_2"], ["frame_3"]]


@pytest.mark.slow
def test_each_frame_is_indexed_at_the_requests_depth_override(tmp_path):
    """The request's depth (a frame without one of its own: the synthetic frames have none) is the depth each frame is
    indexed at, as a batch run's (``Indexer.index(..., depth=...)``)."""
    frames = _copies(tmp_path / "input", GRAIN)
    hooks = _Hooks(_entries(*frames), sealed=True)
    run = tmp_path / "run"
    parameters = {**PARAMETERS, "depth": 100.0}
    live_compute.compute_live_lauego_indexing(_request(run, cores=1, publish_seconds=1.0, parameters=parameters), hooks)

    at_depth = _expected(tmp_path / "at_depth.h5", frames[0], "frame_1", GEOMETRY, CRYSTAL, depth=100.0)
    without = _expected(tmp_path / "without.h5", frames[0], "frame_1", GEOMETRY, CRYSTAL)
    assert not _same_frame(without, at_depth)  # the depth matters
    assert _same_frame(results_by_frame(run / "output.h5")["frame_1"], at_depth)


def test_of_two_frames_whose_workers_raise_in_one_wait_the_run_names_the_first_and_both_failed(tmp_path, monkeypatch):
    """Two workers' own errors come in one wait (both released, and ended, while the loop looks at the arrivals); every
    outcome is taken and the run's error is the first's, in dispatch order."""
    threads = _Threads(
        held={"frame_1": threading.Event(), "frame_2": threading.Event()},
        raised={"frame_1": RuntimeError("the first"), "frame_2": RuntimeError("the second")},
    )
    _in_threads(monkeypatch, threads)

    def both_end() -> None:
        threads.release()
        assert wait_for(lambda: all(threads.futures[name].done() for name in ("frame_1", "frame_2")), 60)

    hooks = _Hooks(_entries(*_inputs(tmp_path / "input", 2)), looked=both_end)
    with pytest.raises(WorkerError) as caught:
        live_compute.compute_live_lauego_indexing(_request(tmp_path / "run", cores=2, publish_seconds=60.0), hooks)

    assert str(caught.value) == "indexing worker failed on input frame_1: RuntimeError: the first"
    assert [(record.input_id, record.message) for record in hooks.failures] == [
        ("frame_1", "the first"),
        ("frame_2", "the second"),
    ]


def test_a_failed_merge_gives_its_shards_back_so_the_preview_left_at_the_run_level_error_holds_every_frame(
    tmp_path, monkeypatch
):
    """The preview's second merge fails (a run-level error); the last publication at that error merges the shards it had
    taken too, so the preview holds every frame indexed, in order."""
    threads = _Threads(held={"frame_3": threading.Event()})
    _in_threads(monkeypatch, threads)
    merge = live_compute.merge_results_files
    calls: list[list[str]] = []

    def second_fails(sources: list[Path], *args: Any, **kwargs: Any) -> Any:
        calls.append([Path(source).parent.name for source in sources])
        if len(calls) == 2:
            raise OSError("the preview cannot be written")
        return merge(sources, *args, **kwargs)

    monkeypatch.setattr(live_compute, "merge_results_files", second_fails)

    def frame_3_once_two_merged() -> None:
        if len(calls) >= 2:
            threads.release()

    hooks = _Hooks(_entries(*_inputs(tmp_path / "input", 3)), looked=frame_3_once_two_merged)
    run = tmp_path / "run"
    try:
        with pytest.raises(OSError, match="the preview cannot be written"):
            live_compute.compute_live_lauego_indexing(_request(run, cores=1, publish_seconds=0.01), hooks)
    finally:
        threads.release()

    assert calls[:2] == [["batch_0"], ["run", "batch_1"]]  # the second: the preview and frame 2's shard
    assert _frame_ids(run / "output.h5") == ["frame_1", "frame_2"]  # frame 2 given back to abandon's publication


def test_shards_a_merge_could_not_start_with_go_to_the_next_merge(tmp_path, monkeypatch):
    """The merge's thread cannot start (its ``submit`` raises): a run-level error; the shards stay unpublished, so the
    publication at that error merges them."""

    class Merger(ThreadPoolExecutor):
        refused = False

        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
            if not Merger.refused:
                Merger.refused = True
                raise RuntimeError("can't start new thread")
            return super().submit(fn, *args, **kwargs)

    threads = _Threads()
    _in_threads(monkeypatch, threads)
    monkeypatch.setattr(live_compute, "ThreadPoolExecutor", Merger)
    run = tmp_path / "run"
    with pytest.raises(RuntimeError, match="can't start new thread"):
        live_compute.compute_live_lauego_indexing(
            _request(run, cores=1, publish_seconds=60.0), _Hooks(_entries(*_inputs(tmp_path / "input", 1)))
        )
    assert _frame_ids(run / "output.h5") == ["frame_1"]


# ---------------------------------------------------------------------------
# A follower: its inputs are the depth frames of a live wire run's completed points


def _point_entries(points: dict[str, Path], depths: dict[str, list[int]]) -> list[ManifestEntry]:
    """The inputs the live hooks hand out for the followed ``points`` (point ID → point file), one per selected
    depth, in that order (``LiveExecutorHooks._entries``)."""
    entries: list[ManifestEntry] = []
    for scan_point, (point, path) in enumerate(points.items(), start=1):
        for depth in depths[point]:
            entries.append(
                ManifestEntry(
                    len(entries),
                    f"{point}_{depth}",
                    os.fspath(path),
                    0,
                    scan_point,
                    depth,
                    point_id=point,
                    depth_index=depth,
                    identity=file_identity(path),
                )
            )
    return entries


def test_a_followers_depth_frames_go_to_the_pool_as_scan_frames_and_each_point_has_its_own_shard(tmp_path, monkeypatch):
    """Each input is a ``ScanFrame`` of its point file, as a batch run on a reconstruction gives lauelab (the depth: the
    request's override, else the point's); a selected depth a point lacks is a failed input (lauelab's read refuses it,
    here the stand-in); the results go point by point into ``shards/<point_id>/``, merged into the preview in arrival
    order, and the Output XML is stitched at the seal."""
    folder = tmp_path / "reconstruction" / "points"
    folder.mkdir(parents=True)
    points = {name: folder / f"{name}.h5" for name in ("wire_3", "wire_1")}
    for path in points.values():
        path.write_bytes(b"a point file")
    threads = _Threads(failed={"wire_1_11": InputError("depth_index 11 is outside 0 to 10")})
    _in_threads(monkeypatch, threads)
    hooks = _Hooks(_point_entries(points, {"wire_3": [0, 1, 11], "wire_1": [0, 1, 11]}), sealed=True)
    threads.failed["wire_3_11"] = threads.failed["wire_1_11"]
    run = tmp_path / "run"
    outcome = live_compute.compute_live_lauego_indexing(
        _request(run, cores=2, publish_seconds=0.01, source="run"), hooks
    )

    assert threads.frames == [
        ScanFrame(os.fspath(points[point]), point, depth) for point in ("wire_3", "wire_1") for depth in (0, 1, 11)
    ]
    assert (outcome.n_succeeded, outcome.n_failed) == (4, 2)
    assert [(record.input_id, record.category) for record in hooks.failures] == [
        ("wire_3_11", "input"),
        ("wire_1_11", "input"),
    ]
    assert sorted(os.listdir(run / "shards")) == ["wire_1", "wire_3"]
    assert _frame_ids(run / "shards" / "wire_3" / "output.h5") == ["wire_3_0", "wire_3_1"]
    assert _frame_ids(run / "output.h5") == ["wire_3_0", "wire_3_1", "wire_1_0", "wire_1_1"]  # arrival order
    assert len(_xml_images(run / "output.xml")) == 4
    assert outcome.provenance["output_layout"] == "shards/<point_id>/output.h5 merged into output.h5, in arrival order"


def _follower_frame(point: str, depth: int, *, indexed: bool, done: bool = True) -> Any:
    source = f"/rec/points/{point}.h5"
    entry = ManifestEntry(0, f"{point}_{depth}", source, 0, 1, depth, point_id=point, depth_index=depth)
    return live_compute._Frame(entry, result=_stand_in_result(source) if indexed else None, done=done)


def test_a_followers_point_is_written_once_the_outcome_of_its_every_input_is_in_and_a_point_with_none_indexed_has_no_shard(
    tmp_path,
):
    """The shard rule for a follower (``_shard_points``): a point's shard waits for its last input; a point whose
    every input failed has none; at the run's end (``_shard``) a point a run-level error cut short is written with
    the frames it has."""
    live = live_compute._LiveIndexing(
        _request(tmp_path / "run", cores=1, publish_seconds=1.0, source="run"),
        _Hooks([]),
        RunOutcome(n_succeeded=0, n_failed=0),
        settings=settings_from_request(PARAMETERS),
        calibration=Calibration(os.fspath(GEOMETRY), GEOMETRY.read_bytes(), load_crystal(CRYSTAL), None),
        indexer=_StandInIndexer(os.fspath(GEOMETRY)),  # pyright: ignore[reportArgumentType]
        workers=1,
        publish_seconds=1.0,
    )
    last = _follower_frame("p1", 2, indexed=False, done=False)
    frames = [
        _follower_frame("p1", 0, indexed=True),
        _follower_frame("p1", 1, indexed=True),
        last,
        _follower_frame("p2", 0, indexed=False),
        _follower_frame("p3", 0, indexed=True),
        _follower_frame("p4", 0, indexed=True),
        _follower_frame("p4", 1, indexed=True, done=False),
    ]
    live.arrived.extend(frames)
    live.point_inputs.update(p1=3, p2=1, p3=1, p4=2)
    shards = lambda: sorted(os.listdir(live.run_directory / "shards"))  # noqa: E731
    live._shard_if_due()
    assert not (live.run_directory / "shards").exists()  # p1 waits for its depth 2
    last.done = True  # failed
    live._shard_if_due()
    assert shards() == ["p1", "p3"]  # p2 had no frame indexed; p4 waits
    assert _frame_ids(live.run_directory / "shards" / "p1" / "output.h5") == ["p1_0", "p1_1"]
    live._shard()  # the run's end (a run-level error: p4's depth 1 never came)
    assert shards() == ["p1", "p3", "p4"]
    assert _frame_ids(live.run_directory / "shards" / "p4" / "output.h5") == ["p4_0"]
