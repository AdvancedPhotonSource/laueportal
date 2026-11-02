"""A live LaueGo run following a live wire run (``jobs.follower``, ``indexing.lauego.live_compute``).

The slow tests drive a linked pair as the portal does: a live wire run whose source is ``notify`` and its follower,
created and queued (``create_live_run``), a real worker whose two job processes run the real computes (the wire run
one core, the follower one), ``tests.wire_support``'s small stacks admitted with ``notify``, Stop through
``controls.cancel_run``. The job processes run under ``tests.jobs.follower_probe``, which logs each look of a job
process at the wire run's ``completed.jsonl`` (a stat, a read), anything it writes in the wire run's directory, and
its seal. The fast tests run the follower's looks in this process (``jobs.follower``: its reader, its admissions, its
end) and the compute's follower shards with the pool's workers as threads.
"""

from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import pytest
from sqlalchemy.orm import Session

from laueportal.jobs import controls
from laueportal.jobs.arrivals import notify
from laueportal.jobs.models import Job
from laueportal.jobs.state import JobStatus, RunPhase
from tests.jobs.follower_probe import probe_log, probe_prelude
from tests.jobs.live_support import LiveRun, create_live_follower, create_live_wire_run, live_settings
from tests.run_support import load_job
from tests.wire_support import GEOMETRY, N_DEPTHS, OPTIONS, write_wire_scan
from tests.worker_support import Live, WorkerProcess, start_worker, wait_for

FIXTURES = Path(__file__).parents[1] / "fixtures" / "lauelab"
CRYSTAL = FIXTURES / "Ni.xml"
WIRE = {**OPTIONS, "geometry_file": os.fspath(GEOMETRY), "wire_edges": "leading"}
"""The live wire run's reconstruction: ``tests.wire_support``'s 32 x 32 stacks, 11 depths each."""
INDEXING = {
    "geometry_file": os.fspath(GEOMETRY),
    "crystal_file": os.fspath(CRYSTAL),
    "box_size": 5,
    "threshold_ratio": None,
}
"""The follower's indexing of those depth frames (``test_scan_indexing``'s values)."""
FOLLOW_SECONDS = 0.3


@dataclass
class Pair:
    """A linked pair: the live wire run, its follower's run directory and job ID, and the probe's log folder."""

    wire: LiveRun
    follower: int
    directory: Path
    probes: Path

    def stack(self, number: int) -> Path:
        """Write the wire run's stack ``number`` (``wire_<number>.h5``, ``write_wire_scan`` seeded by it)."""
        return write_wire_scan(self.wire.folder / f"wire_{number}.h5", seed=number)

    def admit(self, live: Live, *numbers: int) -> None:
        for number in numbers:
            notify(live.engine, self.wire.job_id, self.stack(number))

    def probe(self, live: Live, job_id: int) -> list[dict[str, Any]]:
        pid = load_job(live.engine, job_id).process_pid
        assert pid is not None
        return probe_log(self.probes, pid)


def _pair(live: Live, tmp_path: Path, **params: Any) -> Pair:
    settings = live_settings(tmp_path)
    wire = create_live_wire_run(live.engine, tmp_path, name="wire", settings=settings, params=WIRE)
    follower = create_live_follower(live.engine, tmp_path, wire, settings=settings, params={**INDEXING, **params})
    probes = tmp_path / "probes"
    probes.mkdir()
    return Pair(wire, follower, Path(load_job(live.engine, follower).run_directory), probes)


def _share(cores: int) -> float:
    """``live.cpu_share`` that gives the live runs ``cores`` cores in all on the test's host (``dispatch.live_cores``)."""
    count = os.cpu_count() or 1
    assert count >= cores
    return min(1.0, (cores + 0.5) / count)


def _start(
    live: Live, tmp_path: Path, pair: Pair, *, publish_seconds: float = 0.5, stitch_seconds: float = 0.0
) -> WorkerProcess:
    """A worker whose ``[live]`` settings give the pair two cores (one each: ``wire_share`` 0.67 of 2), the follower
    ``FOLLOW_SECONDS`` and both ``publish_seconds``, its job processes under the probe, once both run."""
    config = live.write_config(
        tmp_path / "live-worker.toml",
        live={
            "publish_seconds": publish_seconds,
            "arrivals_seconds": 0.05,
            "follow_seconds": FOLLOW_SECONDS,
            "cpu_share": _share(2),
        },
        workers=1,
    )
    prelude = probe_prelude(pair.probes, pair.wire.run_directory, stitch_seconds=stitch_seconds)
    worker = start_worker(config, log=tmp_path / "worker.log", prelude=prelude)
    running = lambda: all(  # noqa: E731
        load_job(live.engine, job_id).process_pid is not None for job_id in (pair.wire.job_id, pair.follower)
    )
    assert wait_for(running, 60), worker.log_text()
    return worker


def _counters(live: Live, job_id: int) -> tuple[int, int, int, int]:
    job = load_job(live.engine, job_id)
    return job.n_inputs, job.n_succeeded, job.n_failed, job.n_not_run


def _ended(live: Live, job_id: int) -> bool:
    return load_job(live.engine, job_id).status in (JobStatus.FINISHED, JobStatus.FAILED, JobStatus.CANCELLED)


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _frame_ids(path: Path) -> list[str]:
    with h5py.File(path, "r") as file:
        return [value.decode() for value in file["frames/frame_ids"][()]]


def _preview(directory: Path) -> list[str]:
    try:
        return _frame_ids(directory / "output.h5")
    except FileNotFoundError:
        return []


def _shards(directory: Path) -> dict[str, list[str]]:
    """Each shard's frames, by its folder's name."""
    folder = directory / "shards"
    names = sorted(os.listdir(folder)) if folder.exists() else []
    return {name: _frame_ids(folder / name / "output.h5") for name in names}


def _frames(points: list[str], depths: range | list[int] = range(3)) -> list[str]:
    return [f"{point}_{depth}" for point in points for depth in depths]


def _snapshot(directory: Path) -> dict[str, tuple[int, int, int]]:
    """Every file under ``directory``: its size, modification time and inode."""
    found: dict[str, tuple[int, int, int]] = {}
    for root, _, names in os.walk(directory):
        for name in names:
            status = os.stat(os.path.join(root, name))
            found[os.path.relpath(os.path.join(root, name), directory)] = (
                status.st_size,
                status.st_mtime_ns,
                status.st_ino,
            )
    return found


def _scan_points(live: Live, job_id: int) -> tuple[str, int]:
    with Session(live.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None and job.indexing_run is not None
        parameters = job.indexing_run.lauego_parameters
        return parameters.scan_points, parameters.scan_points_len


def _stop(live: Live, job_id: int) -> None:
    controls.cancel_run(job_id, engine=live.engine)


def _wait(worker: WorkerProcess, condition: Callable[[], bool], timeout: float = 60) -> None:
    assert wait_for(condition, timeout), worker.log_text()


@pytest.mark.slow
def test_a_linked_pair_indexes_each_completed_point_and_both_runs_finish_when_the_wire_run_is_stopped(tmp_path, live):
    """Points arrive over time; each completed point is admitted (its line names the point), counted as its selected
    depths, indexed into its own shard and published; Stop on the wire run ends it, and the follower, once it has
    admitted every point the wire run completed, seals itself and drains. The follower never writes the wire run's
    directory."""
    pair = _pair(live, tmp_path)
    worker = _start(live, tmp_path, pair)
    try:
        for numbers, points in (((2,), ["wire_2"]), ((1,), ["wire_2", "wire_1"])):  # over time
            pair.admit(live, *numbers)
            _wait(worker, lambda points=points: _counters(live, pair.follower)[:2] == (3 * len(points),) * 2)
            _wait(worker, lambda points=points: _preview(pair.directory) == _frames(points))
        assert not (pair.directory / "output.xml").exists()  # the Output XML at the seal
        pair.admit(live, 3)
        _wait(worker, lambda: len(_lines(pair.wire.run_directory / "completed.jsonl")) == 3)
        _stop(live, pair.wire.job_id)  # the session's Stop: on the wire run
        _wait(worker, lambda: _ended(live, pair.wire.job_id))
        wire_files = _snapshot(pair.wire.run_directory)
        assert worker.wait(120) == 0, worker.log_text()
    finally:
        worker.cleanup()

    wire_job, job = load_job(live.engine, pair.wire.job_id), load_job(live.engine, pair.follower)
    assert (wire_job.status, wire_job.phase) == (JobStatus.FINISHED, RunPhase.FINISHED), wire_job.messages
    assert (job.status, job.phase) == (JobStatus.FINISHED, RunPhase.FINISHED), job.messages
    assert job.messages.startswith("Finished: 9 succeeded, 0 failed, 0 not run of 9; 9 frames"), job.messages
    assert _counters(live, pair.follower) == (9, 9, 0, 0)
    completed = _lines(pair.wire.run_directory / "completed.jsonl")
    arrivals = _lines(pair.directory / "arrivals.jsonl")
    assert [(line["point_id"], line["scan_point"], line["path"], line["n_depths"]) for line in arrivals] == [
        (line["point_id"], line["scan_point"], line["path"], line["n_depths"]) for line in completed
    ]  # in the wire run's publication order
    assert [line["point_id"] for line in arrivals] == ["wire_2", "wire_1", "wire_3"]
    assert {line["source"] for line in arrivals} == {"run"} and {line["n_depths"] for line in arrivals} == {N_DEPTHS}
    order = ["wire_2", "wire_1", "wire_3"]
    assert _shards(pair.directory) == {point: _frames([point]) for point in order}
    assert _preview(pair.directory) == _frames(order)  # arrival order
    summary = json.loads((pair.directory / "run.json").read_text())
    assert summary["artifacts"] == {
        "results": os.fspath(pair.directory / "output.h5"),
        "xml": os.fspath(pair.directory / "output.xml"),
    }
    assert len(ET.parse(pair.directory / "output.xml").getroot().findall("step")) == 9
    assert "its wire run has ended and every point it completed is admitted" in summary["log"]
    assert _scan_points(live, pair.follower) == ("1-3", 3)  # the arrivals' numbers
    probe = pair.probe(live, pair.follower)
    assert [line for line in probe if line["op"] == "write"] == []  # never the wire run's directory
    assert _snapshot(pair.wire.run_directory) == wire_files  # nothing changed there once the wire run had ended


def _looks(probe: list[dict[str, Any]], op: str) -> list[float]:
    return [line["t"] for line in probe if line["op"] == op]


@pytest.mark.slow
def test_the_follower_looks_every_follow_seconds_reads_only_what_grew_and_never_after_its_seal(tmp_path, live):
    """A stat of the wire run's ``completed.jsonl`` every ``follow_seconds``, a read only when it grew (none while it is
    missing or unchanged), none after the follower's seal (its drain lasts the probe's 1.5 s stitch)."""
    pair = _pair(live, tmp_path)
    worker = _start(live, tmp_path, pair, stitch_seconds=1.5)
    try:
        _wait(worker, lambda: len(_looks(pair.probe(live, pair.follower), "stat")) >= 3)
        assert _looks(pair.probe(live, pair.follower), "read") == []  # missing: empty, nothing to read
        pair.admit(live, 1)
        _wait(worker, lambda: _counters(live, pair.follower)[1] == 3)
        quiet = (time.time(), time.time() + 8 * FOLLOW_SECONDS)
        time.sleep(quiet[1] - quiet[0])  # nothing grows
        pair.admit(live, 2)
        _wait(worker, lambda: _counters(live, pair.follower)[1] == 6)
        _stop(live, pair.wire.job_id)
        assert worker.wait(120) == 0, worker.log_text()
    finally:
        worker.cleanup()

    assert load_job(live.engine, pair.follower).status == JobStatus.FINISHED
    probe = pair.probe(live, pair.follower)
    stats, reads, (sealed,) = _looks(probe, "stat"), _looks(probe, "read"), _looks(probe, "seal")
    gaps = [later - earlier for earlier, later in zip(stats, stats[1:], strict=False)]
    assert min(gaps) >= 0.8 * FOLLOW_SECONDS, gaps  # one look per follow_seconds
    assert len([at for at in stats if quiet[0] < at < quiet[1]]) >= 6
    assert [at for at in reads if quiet[0] < at < quiet[1]] == []  # nothing grew: no read
    assert 2 <= len(reads) <= 3  # a point's line each, and the last at the wire run's end
    assert [line for line in probe if line["op"] in ("stat", "read") and line["t"] > sealed] == []
    assert {line["thread"] for line in probe if line["op"] in ("stat", "read")} == {"live-follower"}


@pytest.mark.slow
@pytest.mark.parametrize("wire_end", ["cancelled", "failed"])
def test_the_follower_seals_with_what_it_has_when_its_wire_run_ends_cancelled_or_failed(tmp_path, live, wire_end):
    """Whatever the wire run's end: stopped before anything arrived, it ends Cancelled and so does the follower; ended
    Failed (a stack that is not one), the follower indexes the points it completed and ends Finished."""
    pair = _pair(live, tmp_path)
    worker = _start(live, tmp_path, pair)
    try:
        if wire_end == "failed":
            pair.admit(live, 1)
            notify(live.engine, pair.wire.job_id, pair.wire.file(2, b"not a wire scan"))
            _wait(worker, lambda: _counters(live, pair.wire.job_id)[1:3] == (1, 1))
        _stop(live, pair.wire.job_id)
        assert worker.wait(120) == 0, worker.log_text()
    finally:
        worker.cleanup()

    wire_job, job = load_job(live.engine, pair.wire.job_id), load_job(live.engine, pair.follower)
    if wire_end == "cancelled":
        assert (wire_job.status, job.status) == (JobStatus.CANCELLED, JobStatus.CANCELLED), job.messages
        assert job.messages.endswith("Stopped before any input arrived")
        assert _lines(pair.directory / "arrivals.jsonl") == [] and not (pair.directory / "output.h5").exists()
    else:
        assert (wire_job.status, job.status) == (JobStatus.FAILED, JobStatus.FINISHED), job.messages
        assert wire_job.messages.endswith("Incomplete: 1 succeeded, 1 failed, 0 not run of 2")
        assert [line["point_id"] for line in _lines(pair.directory / "arrivals.jsonl")] == ["wire_1"]
        assert _counters(live, pair.follower) == (3, 3, 0, 0) and _preview(pair.directory) == _frames(["wire_1"])


@pytest.mark.slow
def test_a_stop_of_the_follower_alone_seals_only_it_and_the_wire_run_goes_on(tmp_path, live):
    """The follower's own Stop seals it (no look after its seal, no point admitted after it) and drains it; the wire
    run takes and completes further stacks, then ends at its own Stop."""
    pair = _pair(live, tmp_path)
    worker = _start(live, tmp_path, pair, stitch_seconds=1.5)
    try:
        pair.admit(live, 1)
        _wait(worker, lambda: _counters(live, pair.follower)[1] == 3)
        _stop(live, pair.follower)
        _wait(worker, lambda: _ended(live, pair.follower))
        assert load_job(live.engine, pair.wire.job_id).status == JobStatus.RUNNING
        pair.admit(live, 2, 3)
        _wait(worker, lambda: len(_lines(pair.wire.run_directory / "completed.jsonl")) == 3)
        _stop(live, pair.wire.job_id)
        assert worker.wait(120) == 0, worker.log_text()
    finally:
        worker.cleanup()

    wire_job, job = load_job(live.engine, pair.wire.job_id), load_job(live.engine, pair.follower)
    assert (wire_job.status, wire_job.n_inputs, wire_job.n_succeeded) == (JobStatus.FINISHED, 3, 3)
    assert (job.status, job.phase) == (JobStatus.FINISHED, RunPhase.FINISHED), job.messages
    assert _counters(live, pair.follower) == (3, 3, 0, 0)
    assert [line["point_id"] for line in _lines(pair.directory / "arrivals.jsonl")] == ["wire_1"]
    assert _shards(pair.directory) == {"wire_1": _frames(["wire_1"])}
    probe = pair.probe(live, pair.follower)
    (sealed,) = _looks(probe, "seal")
    assert [line for line in probe if line["op"] in ("stat", "read") and line["t"] > sealed] == []


@pytest.mark.slow
def test_a_selected_depth_a_point_lacks_is_one_failed_input_and_its_other_depths_are_indexed(tmp_path, live):
    """Depths 0, 1 and 11 of points with 11 depths (0-10): each point is three inputs, depth 11 a failed input
    (lauelab's read refuses it); the run ends Failed with the others indexed and published."""
    pair = _pair(live, tmp_path, depth_range="0-1,11")
    worker = _start(live, tmp_path, pair)
    try:
        pair.admit(live, 1, 2)
        _wait(worker, lambda: len(_lines(pair.wire.run_directory / "completed.jsonl")) == 2)
        _stop(live, pair.wire.job_id)
        assert worker.wait(120) == 0, worker.log_text()
    finally:
        worker.cleanup()

    job = load_job(live.engine, pair.follower)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert job.messages == "Incomplete: 4 succeeded, 2 failed, 0 not run of 6", job.messages
    failures = _lines(pair.directory / "failures.jsonl")
    points = [line["point_id"] for line in _lines(pair.directory / "arrivals.jsonl")]
    assert [(failure["input_id"], failure["category"]) for failure in failures] == [
        (f"{point}_11", "input") for point in points
    ]
    assert all(failure["message"].endswith("depth_index 11 is outside 0 to 10") for failure in failures), failures
    assert _shards(pair.directory) == {point: _frames([point], [0, 1]) for point in points}
    assert _preview(pair.directory) == _frames(points, [0, 1])


@pytest.mark.slow
def test_a_line_of_completed_jsonl_that_is_not_a_point_fails_the_follower_at_once_and_keeps_what_it_published(
    tmp_path, live
):
    """The follower cannot know its input: a run-level error (Failed at once, no Stop), what it published kept, no
    Output XML; the wire run goes on."""
    pair = _pair(live, tmp_path)
    worker = _start(live, tmp_path, pair)
    completed = pair.wire.run_directory / "completed.jsonl"
    try:
        pair.admit(live, 1)
        _wait(worker, lambda: _preview(pair.directory) == _frames(["wire_1"]))
        with open(completed, "a") as ledger:
            ledger.write("not a point\n")  # a whole line
        _wait(worker, lambda: _ended(live, pair.follower))
        pair.admit(live, 2)
        _wait(worker, lambda: len(completed.read_text().splitlines()) == 3)
        _stop(live, pair.wire.job_id)
        assert worker.wait(120) == 0, worker.log_text()
    finally:
        worker.cleanup()

    job = load_job(live.engine, pair.follower)
    assert (job.status, job.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert job.messages.startswith(
        "Run failed: CompletedUnreadable: line 2 of the followed run's completed.jsonl is not a point (JSONDecodeError"
    ), job.messages
    assert _counters(live, pair.follower) == (3, 3, 0, 0)
    assert _preview(pair.directory) == _frames(["wire_1"]) and not (pair.directory / "output.xml").exists()
    assert load_job(live.engine, pair.wire.job_id).status == JobStatus.FINISHED
