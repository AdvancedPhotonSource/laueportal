"""A ``pioneer`` run continued over sequential PBS allocations: the runner's allocations (the compute's checked
checkpoint, its hand-off and its end), the launcher's chain of allocations (``remote.stage``: recorded and reconciled
submissions, one allocation at a time, a stop, and the one failure path for anything unexpected), with a PBS that runs
each allocation's runner in this process, the publication of the run, and the operator's salvage of a failed run.
Tiny allocations (``max_frames_per_allocation``, a clock past the soft deadline) make one run continue over several,
with the GPU stream daemon's stand-in."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest

pytest.importorskip("laue_index")

from laueportal.indexing.laue_matching import compute as laue_compute  # noqa: E402
from laueportal.indexing.laue_matching import output as laue_output  # noqa: E402
from laueportal.jobs import runner  # noqa: E402
from laueportal.jobs.rundir import FailureRecord  # noqa: E402
from laueportal.remote import plan, poller, stage  # noqa: E402
from tests.indexing.test_laue_matching import calls, stand_in, write_frame  # noqa: E402
from tests.laue_matching_stream_support import fake_daemon, on_gpu  # noqa: E402
from tests.remote.test_pioneer import SUBMISSION, _execution, check, setup  # noqa: E402
from tests.remote_support import job_view  # noqa: E402


@pytest.fixture(autouse=True)
def _no_visibility_wait(monkeypatch):
    """The launcher's wait for an ended allocation's files (``VISIBILITY_GRACE_SECONDS``): none here, unless a test
    sets it."""
    monkeypatch.setattr(stage, "VISIBILITY_GRACE_SECONDS", 0.0)


def continued(tmp_path: Path, monkeypatch, *, count: int = 5, frames: int = 2, script: str = "", **execution: Any):
    """A published ``pioneer`` run of ``count`` frames whose allocations take at most ``frames`` each, its plan saved
    (the launcher's view: ``built``), the remote settings, and the daemon's stand-in in place (``script``: Python run
    by the CPU stand-in it calls, before it writes a frame's tables)."""
    target = {"execution": {**_execution(), "max_frames_per_allocation": frames, **execution}}
    tmp_path.mkdir(parents=True, exist_ok=True)
    layout, engine, settings, run = setup(tmp_path, count=count, target=target)
    built = plan.save_plan(job_view(engine, run.job_id), settings, SUBMISSION, source_check=check(layout))
    monkeypatch.delenv("LAUEMATCHING_BIN", raising=False)
    daemon = fake_daemon(tmp_path, stand_in(tmp_path, script))
    monkeypatch.setattr(laue_compute, "stream_binary", lambda require: daemon)
    monkeypatch.setattr(laue_compute, "gpu_evidence", on_gpu)
    return layout, engine, settings, run, built


def allocate(built: dict[str, Any], sequence: int, *, finalize: bool = False) -> dict[str, Any]:
    """Run allocation ``sequence`` as its PBS job would, with the launcher's record of it written first."""
    folder = Path(built["output_directory"]) / "allocations" / f"{sequence:06d}"
    folder.mkdir(parents=True, exist_ok=True)
    record = {"submission_id": SUBMISSION, "sequence": sequence, "finalize": finalize}
    (folder / "allocation.json").write_text(json.dumps(record))
    return runner.run(built["control_directory"], allocation=sequence)


def read_failure_records(path: Path) -> list[FailureRecord]:
    return [FailureRecord.from_dict(json.loads(line)) for line in path.read_text().splitlines() if line]


def frame_ids(path: Path) -> list[str]:
    with h5py.File(path) as results:
        return [value.decode() for value in results["frames/frame_ids"][...]]


def manifest_ids(built: dict[str, Any]) -> list[str]:
    lines = (Path(built["control_directory"]) / "inputs.jsonl").read_text().splitlines()
    return [json.loads(line)["input_id"] for line in lines if '"input_id"' in line]


# --- the runner's allocations ----------------------------------------------------------------------------------------


def test_one_run_continues_over_three_allocations_each_frame_once_and_published_once(tmp_path, monkeypatch):
    layout, engine, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    output = Path(built["output_directory"])

    first = allocate(built, 1)

    assert (first["status"], first["reason"]) == ("handoff", "frame_limit")
    assert first["committed"] == {"n_attempted": 2, "n_succeeded": 2, "n_failed": 0}
    # A hand-off publishes nothing and fails nothing: no summary, no failure report, no merged files.
    assert not any((output / name).exists() for name in ("run.json", "failures.jsonl", "output.h5", "laue_matching.h5"))
    assert json.loads((output / "progress.json").read_text())["n_succeeded"] == 2

    second = allocate(built, 2)
    assert second["committed"]["n_attempted"] == 4
    assert json.loads((output / "progress.json").read_text())["n_succeeded"] == 4  # the whole run's, never less
    summary = allocate(built, 3)

    assert summary["status"] == "Finished", summary["message"]
    assert summary["counters"] == {"n_inputs": 5, "n_succeeded": 5, "n_failed": 0, "n_not_run": 0, "n_indexed": 5}
    assert calls(tmp_path) == 5  # each frame matched once: no committed frame computed again
    ids = manifest_ids(built)
    assert frame_ids(output / "output.h5") == ids
    with h5py.File(output / "laue_matching.h5") as native:
        assert [value.decode() for value in native["frame_ids"][...]] == ids
    assert [s["allocation"] for s in summary["provenance"]["segments"]] == [1, 2, 3]
    assert [a["status"] for a in summary["provenance"]["allocations"]] == ["handoff", "handoff", "Finished"]
    assert summary["started_at"] == json.loads((output / "allocations/000001/runner.lock").read_text())["started_at"]
    assert not (output / "segments").exists()

    poller.publish_returned(built)  # the portal publishes the run once, as any other
    directory = Path(built["run_directory"])
    assert json.loads((directory / "run.json").read_text())["status"] == "Finished"
    assert frame_ids(directory / "output.h5") == ids


def _datasets(path: Path) -> dict[str, Any]:
    """Every dataset of an HDF5 file but the timings, with paths by file name (two runs' folders differ)."""
    found: dict[str, Any] = {}

    def visit(name: str, item: Any) -> None:
        if isinstance(item, h5py.Dataset) and "seconds" not in name:
            value = item[...]
            if value.dtype.kind in "OS":
                value = np.array(
                    [os.path.basename(v.decode() if isinstance(v, bytes) else str(v)) for v in value.ravel()]
                )
            found[name] = value

    with h5py.File(path) as handle:
        handle.visititems(visit)
    return found


def test_the_merged_files_of_a_continued_run_are_those_of_one_allocation(tmp_path, monkeypatch):
    """Frame IDs, offsets, counters and every table: three allocations merge into exactly what one writes."""
    *_, built = continued(tmp_path / "three", monkeypatch, count=5, frames=2)
    for sequence in (1, 2, 3):
        allocate(built, sequence)
    *_, single = continued(tmp_path / "one", monkeypatch, count=5, frames=65535)
    assert allocate(single, 1)["status"] == "Finished"

    for name in ("output.h5", "laue_matching.h5"):
        merged = _datasets(Path(built["output_directory"]) / name)
        whole = _datasets(Path(single["output_directory"]) / name)
        assert merged.keys() == whole.keys()
        for key in merged:
            np.testing.assert_array_equal(merged[key], whole[key], err_msg=f"{name}:{key}")
    with h5py.File(Path(built["output_directory"]) / "laue_matching.h5") as native:
        offsets = native["solution_offsets"][...]
        assert offsets[0] == 0 and offsets[-1] == native["solutions"].shape[0] and np.all(np.diff(offsets) >= 0)


def test_a_raw_frames_background_is_computed_once_and_kept_across_allocations(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    computed: list[str] = []
    original = laue_compute.compute_background
    monkeypatch.setattr(laue_compute, "compute_background", lambda raw, **k: computed.append("x") or original(raw, **k))
    output = Path(built["output_directory"])

    allocate(built, 1)
    record = json.loads((output / "background.json").read_text())
    first = (output / "background.bin").read_bytes()
    summary = allocate(built, 2)

    assert computed == ["x"]  # the second allocation read the run's background back, not its own first frame's
    assert (output / "background.bin").read_bytes() == first
    assert record["input_id"] == manifest_ids(built)[0] and record["source"].endswith("frame_1.h5")
    assert summary["provenance"]["background"] == record["source"]
    assert summary["provenance"]["background_sha256"] == record["sha256"]


def test_a_background_changed_between_allocations_stops_the_run(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    background = Path(built["output_directory"]) / "background.bin"
    data = bytearray(background.read_bytes())
    data[0] ^= 1
    background.write_bytes(bytes(data))

    summary = allocate(built, 2)

    assert summary["status"] == "Failed" and "background" in summary["message"]
    assert (Path(built["output_directory"]) / "segments/000000").is_dir()  # the committed work is kept


def test_failures_of_every_allocation_are_reported_once_in_order(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    folder = next(Path(built["run_directory"]).parents[1].glob("data/lm_frames"))
    for index in (2, 5):  # one failure in the first allocation, one in the last
        write_frame(folder / f"frame_{index}.h5", shape=(100, 100))
    output = Path(built["output_directory"])

    allocate(built, 1)
    assert not (output / "failures.jsonl").exists()
    allocate(built, 2)
    summary = allocate(built, 3)

    assert summary["status"] == "Failed" and summary["message"] == "Not all inputs completed successfully"
    assert summary["counters"] == {"n_inputs": 5, "n_succeeded": 3, "n_failed": 2, "n_not_run": 0, "n_indexed": 3}
    failures = read_failure_records(output / "failures.jsonl")
    ids = manifest_ids(built)
    assert [f.input_id for f in failures] == [ids[1], ids[4]]
    assert all(f.category == "input" and "full detector" in f.message for f in failures)
    assert frame_ids(output / "output.h5") == [ids[0], ids[2], ids[3]]


def after_frames(monkeypatch, count: int, action) -> list[int]:
    """Call ``action`` once ``count`` frames of this process have finished (each frame's processing returned), so a
    limit or a stop comes between two frames, never during one."""
    done = [0]
    original = laue_compute._process_frame

    def process(*args, **kwargs):
        result = original(*args, **kwargs)
        done[0] += 1
        if done[0] == count:
            action()
        return result

    monkeypatch.setattr(laue_compute, "_process_frame", process)
    return done


def test_a_time_limit_hands_off_and_the_next_allocation_goes_on(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=3, frames=65535)
    late = [0.0]
    monkeypatch.setattr(laue_compute, "_clock", lambda: time.monotonic() + late[0])
    after_frames(monkeypatch, 1, lambda: late.__setitem__(0, 1e9))  # past the soft deadline after one frame

    first = allocate(built, 1)
    assert (first["status"], first["reason"], first["committed"]["n_attempted"]) == ("handoff", "time_limit", 1)

    late[0] = 0.0
    summary = allocate(built, 2)
    assert summary["status"] == "Finished" and calls(tmp_path) == 3


def test_an_allocation_that_commits_nothing_hands_off_its_unchanged_counts(tmp_path, monkeypatch):
    """The allocation does not decide whether the run can go on: its hand-off says what is committed, and the launcher
    fails a run that an allocation did not advance (:func:`test_a_hand_off_that_committed_nothing_fails_the_run`)."""
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    allocate(built, 1)
    monkeypatch.setattr(laue_compute, "_clock", lambda: time.monotonic() + 1e9)  # past the deadline at once

    second = allocate(built, 2)

    assert (second["status"], second["reason"]) == ("handoff", "time_limit")
    assert second["committed"]["n_attempted"] == 2 and calls(tmp_path) == 2
    assert not (Path(built["output_directory"]) / "run.json").exists()


def test_a_tampered_segment_stops_the_run_and_keeps_its_files(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    segment = Path(built["output_directory"]) / "segments/000000/output.h5"
    with open(segment, "r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        last = handle.read(1)
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes([last[0] ^ 1]))

    summary = allocate(built, 2)

    assert (
        summary["status"] == "Failed"
        and "segment 0: its output.h5 changed after it was committed" in summary["message"]
    )
    assert segment.exists() and calls(tmp_path) == 2  # nothing more was computed


def test_a_changed_input_between_allocations_stops_the_run(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    checkpoint = json.loads((Path(built["output_directory"]) / "checkpoint.json").read_text())
    crystal = Path(checkpoint["files"]["crystal_file"]["path"])
    crystal.write_text(crystal.read_text() + "\n")

    summary = allocate(built, 2)

    assert (
        summary["status"] == "Failed" and "checkpoint.json disagrees with this allocation (files)" in summary["message"]
    )


def test_segments_of_another_run_or_a_changed_manifest_are_refused(tmp_path, monkeypatch):
    from laueportal.jobs.rundir import ManifestError

    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    sink = laue_output.Segments(output, lambda path: None, None, seconds=1, identity="another")  # type: ignore[arg-type]
    with pytest.raises(laue_output.CheckpointError, match="segment 0: it was committed by another run"):
        sink.resume()
    manifest = Path(built["control_directory"]) / "inputs.jsonl"
    lines = manifest.read_text().splitlines(keepends=True)
    entries = [i for i, line in enumerate(lines) if '"input_id"' in line]
    lines[entries[0]], lines[entries[1]] = lines[entries[1]], lines[entries[0]]
    manifest.write_text("".join(lines))
    with pytest.raises(ManifestError, match="does not match"):  # the plan's checksum comes first
        allocate(built, 2)


def test_skip_committed_refuses_a_prefix_that_is_not_the_manifests():
    from laueportal.jobs.rundir import ManifestEntry

    entries = [
        ManifestEntry(index=i, input_id=f"f{i}", source=f"/x/{i}", template_index=0, scan_point=i, depth_point=None)
        for i in range(3)
    ]
    assert [e.input_id for e in laue_output.skip_committed(entries, ["f0", "f1"])] == ["f2"]
    with pytest.raises(laue_output.CheckpointError, match="not the start of the manifest"):
        laue_output.skip_committed(entries, ["f1"])
    with pytest.raises(laue_output.CheckpointError, match="not the start of the manifest"):
        laue_output.skip_committed(entries, ["f0", "f1", "f2", "f3"])


def test_a_stop_before_an_allocation_starts_ends_the_run_cancelled_with_its_committed_work(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    allocate(built, 1)
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")

    summary = allocate(built, 2)

    assert summary["status"] == "Cancelled" and calls(tmp_path) == 2  # no daemon, no frame
    assert summary["counters"]["n_succeeded"] == 2 and summary["counters"]["n_not_run"] == 3
    output = Path(built["output_directory"])
    assert len(frame_ids(output / "output.h5")) == 2
    assert {f.category for f in read_failure_records(output / "failures.jsonl")} == {"cancelled"}


def test_a_stop_while_an_allocation_runs_drains_and_publishes_cancelled(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    allocate(built, 1)
    request = Path(built["control_directory"]) / "cancel.request"
    after_frames(monkeypatch, 1, lambda: request.write_text("{}"))  # the stop comes after the allocation's first frame

    summary = allocate(built, 2)

    assert summary["status"] == "Cancelled" and calls(tmp_path) == 3  # nothing started after the stop
    assert summary["counters"]["n_succeeded"] == 3 and summary["counters"]["n_not_run"] == 2
    output = Path(built["output_directory"])
    assert frame_ids(output / "output.h5") == manifest_ids(built)[:3]
    assert {f.category for f in read_failure_records(output / "failures.jsonl")} == {"cancelled"}


def test_a_signal_commits_without_a_summary_and_after_a_stop_the_finalizing_allocation_ends_the_run(
    tmp_path, monkeypatch
):
    """A signal (``qdel`` of a running allocation, after a stop's grace) keeps the committed frames and ends the
    allocation, never the run; the stop's finalizing allocation then ends it Cancelled with them."""
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    output = Path(built["output_directory"])
    allocate(built, 1)
    after_frames(monkeypatch, 1, lambda: os.kill(os.getpid(), signal.SIGTERM))  # the runner's handler keeps it

    interrupted = allocate(built, 2)

    assert (interrupted["status"], interrupted["reason"]) == ("interrupted", f"signal {signal.SIGTERM.value}")
    assert not (output / "run.json").exists() and interrupted["committed"]["n_attempted"] == 3
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")
    summary = allocate(built, 3, finalize=True)
    assert summary["status"] == "Cancelled" and calls(tmp_path) == 3
    assert summary["counters"]["n_succeeded"] == 3 and summary["counters"]["n_not_run"] == 2
    assert {f.category for f in read_failure_records(output / "failures.jsonl")} == {"cancelled"}
    assert frame_ids(output / "output.h5") == manifest_ids(built)[:3]


def test_a_finalizing_allocation_after_every_input_was_committed_finishes_the_run(tmp_path, monkeypatch):
    """A stop that came after every input was committed, the allocation killed while it merged: the finalizing one
    merges the segments and the run is Finished, every input having been processed."""
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    output = Path(built["output_directory"])
    allocate(built, 1)

    class Killed(BaseException):
        pass

    def killed(*args, **kwargs):
        raise Killed

    with monkeypatch.context() as patch:
        patch.setattr(laue_output.Segments, "merge", killed)
        allocate(built, 2)
    (output / "run.json").unlink()  # what SIGKILL during the merge leaves: no summary, every segment committed
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")

    summary = allocate(built, 3, finalize=True)

    assert summary["status"] == "Finished" and summary["warnings"] == [] and calls(tmp_path) == 4
    assert frame_ids(output / "output.h5") == manifest_ids(built)


def test_an_allocation_refuses_to_run_after_the_run_ended_or_after_a_later_one_started(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    allocate(built, 1)
    allocate(built, 2)
    with pytest.raises(RuntimeError, match="has ended"):
        allocate(built, 3)
    *_, other = continued(tmp_path / "other", monkeypatch, count=3, frames=1)
    allocate(other, 1)
    allocate(other, 2)
    lock = Path(other["output_directory"]) / "allocations/000001/runner.lock"
    lock.unlink()  # as if PBS ran allocation 1's job again
    with pytest.raises(RuntimeError, match="Allocation 2 of this run has started after this one"):
        runner.run(other["control_directory"], allocation=1)
    with pytest.raises(RuntimeError, match="no launcher record"):
        runner.run(other["control_directory"], allocation=4)


def test_an_allocation_refuses_to_run_once_the_launcher_ended_the_run(tmp_path, monkeypatch):
    """A job PBS accepted after the launcher failed the run (a lost ``qsub`` reply it stopped waiting for) starts,
    finds the ledger's ended status, and writes nothing."""
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    ledger = {"status": "FAILED", "message": "PBS never confirmed allocation 2's submission", "allocations": []}
    (output / "allocations/ledger.json").write_text(json.dumps(ledger))

    with pytest.raises(RuntimeError, match="The launcher ended this run \\(FAILED\\): PBS never confirmed"):
        allocate(built, 2)

    assert not (output / "allocations/000002/runner.lock").exists() and calls(tmp_path) == 2
    assert not (output / "run.json").exists()


# --- planning ---------------------------------------------------------------------------------------------------------


def test_a_plan_of_more_than_one_daemons_frames_continues_with_a_cap_per_allocation(tmp_path):
    """70,000 inputs: one run, planned with nothing computed; each allocation attempts at most 65,535."""
    from types import SimpleNamespace

    from laueportal.jobs.rundir import manifest_digest

    layout, engine, settings, run = setup(tmp_path, count=1)
    view = job_view(engine, run.job_id)
    manifest = Path(view.manifest_path)
    lines = manifest.read_text().splitlines()
    header = [line for line in lines if '"input_id"' not in line]
    entry = json.loads(next(line for line in lines if '"input_id"' in line))
    big = tmp_path / "big.jsonl"
    with open(big, "w") as stream:
        for line in header:
            stream.write(line + "\n")
        for index in range(70000):
            stream.write(json.dumps({**entry, "index": index, "input_id": f"i{index}", "scan_point": index}) + "\n")
    job = SimpleNamespace(
        **{**vars(view), "manifest_path": str(big), "manifest_digest": manifest_digest(big), "n_inputs": 70000}
    )

    built = plan.build_plan(job, settings, SUBMISSION, source_check=check(layout))

    assert built["n_inputs"] == 70000
    assert built["continuation"] == {"version": 1, "max_frames_per_allocation": 65535}


def test_a_cap_above_one_daemon_is_refused(tmp_path):
    from laueportal.remote.settings import RemoteSettingsError

    layout, engine, settings, run = setup(
        tmp_path, target={"execution": {**_execution(), "max_frames_per_allocation": 65536}}
    )
    with pytest.raises(RemoteSettingsError, match=re.escape("max_frames_per_allocation (1 to 65535)")):
        plan.build_plan(job_view(engine, run.job_id), settings, SUBMISSION, source_check=check(layout))


def test_one_daemon_matches_at_most_its_image_numbers(tmp_path):
    from laueportal.indexing.laue_matching.stream import MAX_IMAGES, DaemonFailed, StreamMatcher

    matcher = StreamMatcher(
        tmp_path / "LaueMatchingGPUStream",
        params=tmp_path / "p",
        database=tmp_path / "d",
        hkls=tmp_path / "h",
        threads=1,
        folder=tmp_path,
        solution_columns=1,
        spot_columns=1,
        should_stop=lambda: False,
    )
    matcher.socket = object()  # type: ignore[assignment]
    matcher.images = MAX_IMAGES
    with pytest.raises(DaemonFailed, match="at most 65535 frames"):
        matcher.match(np.zeros((1, 1)))


def test_each_allocations_script_is_named_and_runs_its_allocation(tmp_path, monkeypatch):
    from laueportal.remote.pbs import allocation_name, build_script

    *_, built = continued(tmp_path, monkeypatch, count=2, frames=1)
    script = build_script(built, allocation=3)
    name = allocation_name(built, 3)
    assert name == "L" + SUBMISSION.replace("-", "") + "-3"
    assert f"#PBS -N {name}\n" in script and "#PBS -r n\n" in script
    assert script.rstrip().endswith(f"-m laueportal.jobs.runner {built['control_directory']} --allocation 3")


# --- the launcher's chain ---------------------------------------------------------------------------------------------


class ChainPBS:
    """PBS for the launcher's commands (``stage._pbs_run``): ``qsub`` queues a job (``self.replies`` says how the
    next replies: ``ok``, ``lost`` (accepted, the reply lost), ``timeout`` (not accepted, no reply), ``ssh``
    (ssh's own failure), ``refuse``); ``qselect -x -N`` lists jobs by name; ``qstat`` moves a queued job to running
    (running its allocation's runner here, unless ``hold``), then to finished; ``qdel`` ends a job. ``fail_qstat`` and
    ``fail_qselect`` make that many calls fail as an unreachable scheduler does."""

    def __init__(self, built: dict[str, Any], *, run: bool = True) -> None:
        self.built = built
        self.run = run
        self.hold = False
        self.replies: list[str] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.commands: list[list[str]] = []
        self.fail_qstat = 0
        self.fail_qselect = 0
        self.finish = True
        self.number = 100

    def qsubs(self) -> int:
        return sum(1 for command in self.commands if command[0].endswith("qsub"))

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        program, args = command[0].rsplit("/", 1)[-1], command[1:]
        if program == "qsub":
            reply = self.replies.pop(0) if self.replies else "ok"
            if reply in ("timeout", "ssh", "refuse"):
                if reply == "timeout":
                    raise subprocess.TimeoutExpired(command, 60)
                code = 255 if reply == "ssh" else 38
                return subprocess.CompletedProcess(command, code, "", "qsub: would exceed the queue's limit")
            text = Path(args[-1]).read_text()
            self.number += 1
            pbs_id = f"{self.number}.server"
            self.jobs[pbs_id] = {
                "name": re.search(r"#PBS -N (\S+)", text)[1],  # type: ignore[index]
                "sequence": int(re.search(r"--allocation (\d+)", text)[1]),  # type: ignore[index]
                "queue": re.search(r"#PBS -q (\S+)", text)[1],  # type: ignore[index]
                "ngpus": int(re.search(r"ngpus=(\d+)", text)[1]),  # type: ignore[index]
                "state": "Q",
                "exit": None,
            }
            if reply == "lost":
                raise subprocess.TimeoutExpired(command, 60)
            return subprocess.CompletedProcess(command, 0, pbs_id + "\n", "")
        if program == "qselect":
            if self.fail_qselect:
                self.fail_qselect -= 1
                raise subprocess.CalledProcessError(255, command, "", "ssh: connect: timed out")
            name = args[args.index("-N") + 1]
            ids = [pbs_id for pbs_id, job in self.jobs.items() if job["name"] == name]
            return subprocess.CompletedProcess(command, 0, "".join(f"{i}\n" for i in ids), "")
        if program == "qstat":
            if self.fail_qstat:
                self.fail_qstat -= 1
                raise subprocess.CalledProcessError(255, command, "", "ssh: connect: timed out")
            pbs_id = args[-1]
            job = self.jobs[pbs_id]
            if job["state"] == "R" and self.finish:
                job["state"] = "F"
            elif job["state"] == "Q" and not self.hold:
                job["state"] = "R"
                if self.run:
                    job["exit"] = self._run(pbs_id, job)
            record = {"Jobs": {pbs_id: {"job_state": job["state"], "Exit_status": job["exit"]}}}
            return subprocess.CompletedProcess(command, 0, json.dumps(record), "")
        if program == "qdel":
            job = self.jobs[args[-1]]
            job["deleted"] = True
            if job["state"] == "Q":
                job["state"], job["exit"] = "F", -3
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    def _run(self, pbs_id: str, job: dict[str, Any]) -> int:
        os.environ["PBS_JOBID"] = pbs_id
        try:
            summary = runner.run(self.built["control_directory"], allocation=job["sequence"])
        except Exception:
            return 1
        finally:
            del os.environ["PBS_JOBID"]
        return 0 if summary.get("status") in ("handoff", "Finished") else 1


def chain(built, settings, pbs, monkeypatch, *, limit: int = 40) -> dict[str, Any]:
    """The DM workflow's submit, then its monitor until the run ends (or ``limit`` monitor runs)."""
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    data = stage.submit(built, settings)
    for _ in range(limit):
        if data["status"] in stage.TERMINAL_STATUSES:
            break
        data = stage.monitor(built, settings)
    return data


def state_of(built, settings) -> dict[str, Any]:
    return json.loads((Path(settings.state_directory) / f"{SUBMISSION}.json").read_text())


def test_the_launcher_chains_allocations_one_at_a_time_until_the_run_ends(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    observations: list[dict[str, Any]] = []
    original = stage.continued_observation
    monkeypatch.setattr(
        stage, "continued_observation", lambda p, d: observations.append(original(p, d)) or observations[-1]
    )

    data = chain(built, settings, pbs, monkeypatch)

    assert data["status"] == "SUCCEEDED"
    allocations = data["allocations"]
    assert [(a["sequence"], a["mode"], a["result"]) for a in allocations] == [
        (1, "process", "handoff"),
        (2, "process", "handoff"),
        (3, "process", "final"),
    ]
    assert [a["pbs_id"] for a in allocations] == ["101.server", "102.server", "103.server"] and pbs.qsubs() == 3
    # One allocation at a time: each was submitted after the one before it had finished.
    for before, after in zip(allocations, allocations[1:], strict=False):
        assert before["ended_at"] <= after["created_at"]
    assert [a["committed"] for a in allocations[:2]] == [2, 4]
    ledger = json.loads((Path(built["output_directory"]) / "allocations/ledger.json").read_text())
    assert ledger["status"] == "SUCCEEDED" and [a["pbs_id"] for a in ledger["allocations"]] == [
        "101.server",
        "102.server",
        "103.server",
    ]
    done = [o["frames_done"] for o in observations]
    assert done == sorted(done) and done[-1] == 5  # cumulative, never back
    assert "Waiting for next PBS allocation" in {o["detail"] for o in observations}
    assert json.loads((Path(built["output_directory"]) / "run.json").read_text())["status"] == "Finished"


def chain_rest(built, settings, limit: int = 20) -> dict[str, Any]:
    data = state_of(built, settings)
    for _ in range(limit):
        if data["status"] in stage.TERMINAL_STATUSES:
            break
        data = stage.monitor(built, settings)
    return data


def ledger_of(built) -> dict[str, Any]:
    return json.loads((Path(built["output_directory"]) / "allocations/ledger.json").read_text())


def assert_failed_and_kept(data: dict[str, Any], built: dict[str, Any], pbs: ChainPBS, *, qsubs: int) -> None:
    """The one failure path: FAILED with its reason in the state file, the ledger and the portal's job message; no
    allocation submitted after it (never a rescue); the committed segments and checkpoint kept, unmerged."""
    output = Path(built["output_directory"])
    assert data["status"] == "FAILED" and pbs.qsubs() == qsubs
    assert all(a["mode"] == "process" for a in data["allocations"])  # no finalizing (rescue) allocation
    assert f"the run's files are kept in {output}" in data["message"]
    assert ledger_of(built)["status"] == "FAILED" and ledger_of(built)["message"] == data["message"]
    reason = poller.launcher_reason(built)  # what the portal's job then says, rather than a bare DM failure
    assert reason is not None and reason.rstrip("…") == data["message"][: len(reason.rstrip("…"))]
    assert (output / "segments/000000/segment.json").exists() and (output / "checkpoint.json").exists()
    assert not (output / "run.json").exists() and not (output / "output.h5").exists()


# --- submissions: recorded, reconciled, never repeated ----------------------------------------------------------------


def test_a_lost_qsub_reply_is_reconciled_from_pbs_history_without_a_second_job(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built, run=False)
    pbs.replies = ["lost"]
    monkeypatch.setattr(stage, "_pbs_run", pbs)

    data = stage.submit(built, settings)
    entry = data["allocations"][0]
    assert (data["status"], entry["state"], entry["qsub"]) == ("RUNNING", "submitting", "no reply within 60 s")
    data = stage.monitor(built, settings)  # PBS's history has the job: adopted, nothing submitted again

    assert data["allocations"][0]["pbs_id"] == "101.server" and pbs.qsubs() == 1 and len(pbs.jobs) == 1


@pytest.mark.parametrize("reply", ["timeout", "ssh"])
def test_a_qsub_that_pbs_never_confirms_fails_the_run_and_is_never_submitted_again(tmp_path, monkeypatch, reply):
    """A lost reply whose job PBS's history never shows: the launcher waits ``RECONCILE_SECONDS``, then fails the run
    saying the submission is uncertain and naming the job to look for; it never submits again. A job that PBS shows
    later refuses to run."""
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)  # allocation 1 runs and hands off
    pbs.replies = [reply]
    data = stage.monitor(built, settings)  # allocation 2's qsub: no usable reply
    data = stage.monitor(built, settings)
    assert data["status"] == "RUNNING" and pbs.qsubs() == 2  # within RECONCILE_SECONDS: PBS may still show it

    monkeypatch.setattr(stage, "RECONCILE_SECONDS", 0.0)
    data = chain_rest(built, settings)

    assert_failed_and_kept(data, built, pbs, qsubs=2)
    name = data["allocations"][1]["name"]
    assert "PBS never confirmed allocation 2's submission" in data["message"]
    assert f"qselect -x -N {name}" in data["message"] and "failure ledger" in data["message"]
    # The job PBS accepted after all: its runner starts, sees the run ended, and writes nothing.
    with pytest.raises(RuntimeError, match="The launcher ended this run"):
        runner.run(built["control_directory"], allocation=2)
    assert calls(tmp_path) == 2


def test_a_stage_that_died_after_qsub_finds_the_job_and_one_that_died_before_submits_once(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built, run=False)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    state = state_of(built, settings)
    entry = state["allocations"][0]
    # Died after qsub, before the reply was saved: the qsub is on record as pending.
    entry.update(state="submitting", qsub="pending")
    entry.pop("pbs_id")
    (Path(settings.state_directory) / f"{SUBMISSION}.json").write_text(json.dumps(state))

    data = stage.monitor(built, settings)
    assert data["allocations"][0]["pbs_id"] == "101.server" and pbs.qsubs() == 1

    # Died after the allocation was recorded, before its qsub: PBS has no such job, so it is submitted once.
    state = state_of(built, settings)
    entry = state["allocations"][0]
    entry.update(state="submitting", name=entry["name"] + "x")
    for key in ("qsub", "qsub_at", "pbs_id"):
        entry.pop(key)
    (Path(settings.state_directory) / f"{SUBMISSION}.json").write_text(json.dumps(state))
    stage.monitor(built, settings)
    stage.monitor(built, settings)
    assert pbs.qsubs() == 2


def test_a_refused_next_allocation_fails_the_run_with_its_checkpoint_and_no_rescue(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)  # allocation 1 runs and hands off
    pbs.replies = ["refuse"]  # the queue is at its per-user limit

    data = chain_rest(built, settings)

    assert_failed_and_kept(data, built, pbs, qsubs=2)
    assert "PBS refused allocation 2 (qsub exit 38: qsub: would exceed the queue's limit)" in data["message"]
    assert data["allocations"][1]["state"] == "not_submitted" and calls(tmp_path) == 2


def test_a_refused_first_allocation_fails_the_submit_stage(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built)
    pbs.replies = ["refuse"]
    monkeypatch.setattr(stage, "_pbs_run", pbs)

    with pytest.raises(RuntimeError, match="PBS refused allocation 1"):
        stage.submit(built, settings)

    assert state_of(built, settings)["status"] == "FAILED" and ledger_of(built)["status"] == "FAILED"
    assert stage.monitor(built, settings)["status"] == "FAILED" and pbs.qsubs() == 1


def test_concurrent_monitors_and_a_stop_submit_the_next_allocation_once(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)  # allocation 1 runs (here) and hands off
    pbs.hold = True  # the next allocation waits in the queue
    slow = pbs.__call__

    def slower(command, **kwargs):
        if command[0].endswith("qsub"):
            time.sleep(0.3)
        return slow(command, **kwargs)

    monkeypatch.setattr(stage, "_pbs_run", slower)
    errors: list[BaseException] = []

    def run_stage(action):
        try:
            action(built, settings)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=run_stage, args=(stage.monitor,)) for _ in range(3)]
    threads.append(threading.Thread(target=run_stage, args=(stage.cancel,)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert pbs.qsubs() == 2 and len(state_of(built, settings)["allocations"]) == 2


def test_a_state_write_that_fails_before_qsub_never_leads_to_a_second_job(tmp_path, monkeypatch):
    """The allocation's record is saved before its qsub: a save that fails stops the pass before PBS is asked, and the
    allocation folder written for it is written again by the next pass, which submits it once."""
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)
    real = stage.write_json_file
    fail = {"n": 1}

    def flaky(path, payload, **kwargs):
        if fail["n"] and Path(path).name == f"{SUBMISSION}.json" and len(payload.get("allocations", [])) == 2:
            fail["n"] -= 1
            raise OSError(28, "No space left on device")
        return real(path, payload, **kwargs)

    monkeypatch.setattr(stage, "write_json_file", flaky)
    stage.monitor(built, settings)
    assert pbs.qsubs() == 1  # nothing was submitted after the failed save
    data = chain_rest(built, settings)
    assert data["status"] == "SUCCEEDED" and pbs.qsubs() == 3 and len(pbs.jobs) == 3


# --- a stop -----------------------------------------------------------------------------------------------------------


def test_a_stop_deletes_a_queued_first_allocation_and_submits_nothing(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built)
    pbs.hold = True
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")

    assert stage.cancel(built, settings)["status"] == "CANCEL_REQUESTED"
    assert pbs.jobs["101.server"]["deleted"]
    data = chain_rest(built, settings)

    assert data["status"] == "CANCELLED" and pbs.qsubs() == 1 and calls(tmp_path) == 0
    assert data["message"] == "Stopped before any input was committed"


def test_a_stop_before_the_first_qsub_submits_nothing(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built)
    pbs.fail_qselect = 1  # PBS does not answer the submit stage: nothing is submitted yet
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    assert stage.submit(built, settings)["allocations"][0]["state"] == "submitting"
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")

    data = stage.monitor(built, settings)

    assert data["status"] == "CANCELLED" and pbs.qsubs() == 0 and not pbs.jobs


def test_a_stop_between_allocations_deletes_the_queued_one_and_finalizes_on_the_cpu_queue(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)  # allocation 1 runs and hands off
    pbs.hold = True
    stage.monitor(built, settings)  # it finished: allocation 2 is submitted and waits
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")
    stage.cancel(built, settings)
    assert pbs.jobs["102.server"].get("deleted")  # a queued processing allocation is deleted at once

    data = stage.monitor(built, settings)  # PBS says F: one finalizing allocation, held in the queue
    assert [(a["mode"], a["state"]) for a in data["allocations"]][-1] == ("finalize", "submitted")
    assert data["observation"]["detail"] == "Stopping: waiting to merge"
    pbs.hold = False
    data = chain_rest(built, settings)

    assert data["status"] == "FAILED"  # the runner's Cancelled summary: DM's check then fails, as for any stop
    summary = json.loads((Path(built["output_directory"]) / "run.json").read_text())
    assert summary["status"] == "Cancelled" and summary["counters"]["n_succeeded"] == 2
    assert calls(tmp_path) == 2 and pbs.qsubs() == 3
    assert [pbs.jobs[a["pbs_id"]]["ngpus"] for a in data["allocations"]] == [1, 1, 0]
    assert frame_ids(Path(built["output_directory"]) / "output.h5") == manifest_ids(built)[:2]


def test_a_stop_racing_a_hand_off_submits_no_processing_allocation(tmp_path, monkeypatch):
    """The stop arrives after allocation 1 handed off but before the launcher saw it end."""
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)  # allocation 1 runs and hands off (PBS: R)
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")
    stage.cancel(built, settings)
    pbs.hold = True
    data = stage.monitor(built, settings)  # allocation 1 is F

    assert [(a["sequence"], a["mode"]) for a in data["allocations"]] == [(1, "process"), (2, "finalize")]
    assert pbs.qsubs() == 2 and pbs.jobs["102.server"]["ngpus"] == 0
    assert (data["observation"]["phase"], data["observation"]["detail"]) == ("queued", "Stopping: waiting to merge")
    assert data["observation"]["pbs"]["mode"] == "finalize"
    record = json.loads((Path(built["output_directory"]) / "allocations/000002/allocation.json").read_text())
    assert record["finalize"] is True
    assert [a["mode"] for a in ledger_of(built)["allocations"]] == ["process", "finalize"]
    pbs.hold = False
    data = chain_rest(built, settings)
    summary = json.loads((Path(built["output_directory"]) / "run.json").read_text())
    assert summary["status"] == "Cancelled" and calls(tmp_path) == 2 and pbs.qsubs() == 2


def test_a_stop_before_a_recorded_processing_allocation_is_submitted_never_submits_it(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)  # allocation 1 hands off
    pbs.fail_qselect = 1
    data = stage.monitor(built, settings)  # allocation 2 recorded; PBS did not answer before its qsub
    assert data["allocations"][1]["state"] == "submitting" and "qsub" not in data["allocations"][1]
    assert data["unavailable"].startswith("PBS: qselect exit 255") and data["observation"]["stale"]
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")

    data = chain_rest(built, settings)

    second, third = data["allocations"][1:]
    assert second["state"] == "not_submitted" and third["mode"] == "finalize" and pbs.qsubs() == 2
    assert [job["sequence"] for job in pbs.jobs.values()] == [1, 3]  # allocation 2 never reached PBS
    assert json.loads((Path(built["output_directory"]) / "run.json").read_text())["status"] == "Cancelled"


def test_a_running_allocation_drains_on_a_stop_and_is_deleted_only_after_the_grace(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built, run=False)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    pbs.finish = False
    stage.submit(built, settings)
    stage.monitor(built, settings)  # running
    request = Path(built["control_directory"]) / "cancel.request"
    request.write_text("{}")
    stage.cancel(built, settings)
    stage.monitor(built, settings)
    assert not pbs.jobs["101.server"].get("deleted")  # it drains

    old = time.time() - stage.STOP_GRACE_SECONDS - 1
    os.utime(request, (old, old))
    state = state_of(built, settings)
    state["allocations"][0]["running_since"] = "2000-01-01T00:00:00+00:00"
    (Path(settings.state_directory) / f"{SUBMISSION}.json").write_text(json.dumps(state))
    stage.monitor(built, settings)
    assert pbs.jobs["101.server"].get("deleted")
    assert [c[0].rsplit("/", 1)[-1] for c in pbs.commands].count("qdel") == 1
    stage.monitor(built, settings)
    assert [c[0].rsplit("/", 1)[-1] for c in pbs.commands].count("qdel") == 1  # once


def test_a_stopped_allocation_deleted_after_the_grace_is_finalized_once(tmp_path, monkeypatch):
    """The drain did not finish within the grace: the deleted allocation ends without the summary, and one finalizing
    allocation (the stop's, never a rescue) merges the committed work: Cancelled."""
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    original = pbs._run

    def second_killed(pbs_id, job):
        if job["sequence"] != 2:
            return original(pbs_id, job)
        (Path(built["control_directory"]) / "cancel.request").write_text("{}")
        return -15  # killed by qdel before it drained

    pbs._run = second_killed  # type: ignore[method-assign]
    data = chain(built, settings, pbs, monkeypatch)

    assert [(a["mode"], a["result"]) for a in data["allocations"]] == [
        ("process", "handoff"),
        ("process", "none"),
        ("finalize", "final"),
    ]
    summary = json.loads((Path(built["output_directory"]) / "run.json").read_text())
    assert summary["status"] == "Cancelled" and summary["counters"]["n_succeeded"] == 2 and calls(tmp_path) == 2
    assert pbs.jobs[data["allocations"][-1]["pbs_id"]]["ngpus"] == 0


def test_a_failing_finalizing_allocation_fails_the_stopped_run_and_keeps_its_checkpoint(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    original = pbs._run

    def finalizer_killed(pbs_id, job):
        if job["sequence"] == 1:
            code = original(pbs_id, job)
            (Path(built["control_directory"]) / "cancel.request").write_text("{}")
            return code
        return -29

    pbs._run = finalizer_killed  # type: ignore[method-assign]
    data = chain(built, settings, pbs, monkeypatch)

    assert data["status"] == "FAILED" and "The finalizing allocation 2" in data["message"] and pbs.qsubs() == 2
    assert "pbs-output.log" in data["message"] and ledger_of(built)["status"] == "FAILED"
    output = Path(built["output_directory"])
    assert (output / "segments/000000").is_dir() and not (output / "run.json").exists()


# --- anything unexpected: one failure path, files kept, no rescue -----------------------------------------------------


def test_an_allocation_that_ends_without_a_result_fails_the_run_and_keeps_its_checkpoint(tmp_path, monkeypatch):
    """A node failure, the walltime, a crash: the run fails with the allocation named, its PBS output and the
    committed work kept, and no allocation follows (not a finalizing one either)."""
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    original = pbs._run
    pbs._run = lambda pbs_id, job: -29 if job["sequence"] == 2 else original(pbs_id, job)  # type: ignore[method-assign]

    data = chain(built, settings, pbs, monkeypatch)

    assert_failed_and_kept(data, built, pbs, qsubs=2)
    assert "Allocation 2 (PBS 102.server, exit status -29) ended without the run's summary" in data["message"]
    assert "it wrote no result" in data["message"] and "allocations/000002/pbs-output.log" in data["message"]
    assert data["allocations"][1]["result"] == "none" and calls(tmp_path) == 2


def test_an_interrupted_allocation_without_a_stop_fails_the_run(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    original = pbs._run

    def second_interrupted(pbs_id, job):
        if job["sequence"] == 2:
            with monkeypatch.context() as patch:
                after_frames(patch, 1, lambda: os.kill(os.getpid(), signal.SIGTERM))
                return original(pbs_id, job)
        return original(pbs_id, job)

    pbs._run = second_interrupted  # type: ignore[method-assign]
    data = chain(built, settings, pbs, monkeypatch)

    assert_failed_and_kept(data, built, pbs, qsubs=2)
    assert f"it was interrupted (signal {signal.SIGTERM.value})" in data["message"]
    assert data["allocations"][1]["committed"] == 3


def test_a_first_allocation_that_commits_nothing_and_ends_fails_the_run(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built)
    pbs._run = lambda pbs_id, job: -29  # type: ignore[method-assign]
    data = chain(built, settings, pbs, monkeypatch)

    assert data["status"] == "FAILED" and pbs.qsubs() == 1 and "Allocation 1 (PBS 101.server" in data["message"]
    assert "allocations/000001/pbs-output.log" in data["message"]


def test_a_hand_off_that_committed_nothing_fails_the_run(tmp_path, monkeypatch):
    """No endless chain of allocations that cannot advance the run."""
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    original = pbs._run

    def second_too_late(pbs_id, job):
        if job["sequence"] == 2:
            with monkeypatch.context() as patch:
                patch.setattr(laue_compute, "_clock", lambda: time.monotonic() + 1e9)
                return original(pbs_id, job)
        return original(pbs_id, job)

    pbs._run = second_too_late  # type: ignore[method-assign]
    data = chain(built, settings, pbs, monkeypatch)

    assert_failed_and_kept(data, built, pbs, qsubs=2)
    assert "it committed 2 inputs (before it, 2; the run has 5)" in data["message"]


def test_a_result_that_does_not_check_fails_the_run_at_once(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    original = pbs._run

    def corrupt_result(pbs_id, job):
        code = original(pbs_id, job)
        (Path(built["output_directory"]) / "allocations/000001/result.json").write_text("{not json")
        return code

    pbs._run = corrupt_result  # type: ignore[method-assign]
    monkeypatch.setattr(stage, "VISIBILITY_GRACE_SECONDS", 3600.0)  # a file that is there is decided at once
    data = chain(built, settings, pbs, monkeypatch, limit=3)

    assert_failed_and_kept(data, built, pbs, qsubs=1)
    assert "its result does not check" in data["message"]


def test_an_error_in_an_allocation_fails_the_run_with_its_summary_and_checkpoint(tmp_path, monkeypatch):
    """The runner's own failure (here the storage failing as the cache is prepared): it writes the run's summary
    Failed, with the committed work last recorded kept unmerged; the launcher submits nothing after it."""
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    original = pbs._run

    def second_fails(pbs_id, job):
        if job["sequence"] != 2:
            return original(pbs_id, job)
        with monkeypatch.context() as patch:

            def unavailable(*args, **kwargs):
                raise OSError(5, "Input/output error")

            patch.setattr(laue_compute, "_prepare_cache", unavailable)
            return original(pbs_id, job)

    pbs._run = second_fails  # type: ignore[method-assign]
    data = chain(built, settings, pbs, monkeypatch)

    output = Path(built["output_directory"])
    summary = json.loads((output / "run.json").read_text())
    assert data["status"] == "FAILED" and pbs.qsubs() == 2 and data["allocations"][-1]["result"] == "final"
    assert summary["status"] == "Failed" and summary["message"].startswith("[Errno 5] Input/output error")
    assert f"kept unmerged in {output / 'segments'} with checkpoint.json" in summary["message"]
    assert summary["counters"]["n_succeeded"] == 2 and summary["artifacts"] == {}
    assert (output / "segments/000000").is_dir() and not (output / "output.h5").exists() and calls(tmp_path) == 2


def test_an_error_of_the_launcher_itself_fails_the_run_and_deletes_its_job(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built, run=False)
    pbs.finish = False
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)

    def broken(*args, **kwargs):
        raise KeyError("job_state")

    monkeypatch.setattr(stage, "_stop_continued", broken)
    data = stage.monitor(built, settings)

    assert data["status"] == "FAILED" and data["message"].startswith("The launcher failed: KeyError: 'job_state'")
    assert "Deletion requested for PBS job 101.server" in data["message"] and pbs.jobs["101.server"]["deleted"]
    assert ledger_of(built)["status"] == "FAILED"


def test_a_job_the_failure_cannot_delete_is_said_to_be_possibly_running(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built, run=False)
    pbs.finish = False
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)

    def commands(command, **kwargs):
        if command[0].endswith("qdel"):
            raise subprocess.TimeoutExpired(command, 30)
        return pbs(command, **kwargs)

    def broken(*args, **kwargs):
        raise ValueError("broken")

    monkeypatch.setattr(stage, "_pbs_run", commands)
    monkeypatch.setattr(stage, "_stop_continued", broken)
    data = stage.monitor(built, settings)

    assert data["status"] == "FAILED"
    assert (
        "PBS job 101.server could not be deleted (qdel did not answer within 30 s) and may still run"
        in (data["message"])
    )


# --- PBS and the shared storage that do not answer --------------------------------------------------------------------


def test_a_pbs_outage_keeps_the_run_and_its_job_until_pbs_answers(tmp_path, monkeypatch):
    """No surprise cut-off: while PBS does not answer, the run stays RUNNING with its observation stale and the
    concrete reason kept; the running allocation is never taken to have ended, and nothing is submitted."""
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built)
    pbs.finish = False
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)  # allocation 1 runs (and hands off; PBS still says R)
    pbs.fail_qstat = 10**6

    for _ in range(4):
        data = stage.monitor(built, settings)
        assert data["status"] == "RUNNING" and data["observation"]["stale"]
        assert data["unavailable"] == "PBS: qstat exit 255: ssh: connect: timed out"
    assert data["allocations"][0]["state"] == "submitted" and pbs.jobs["101.server"]["state"] == "R"
    assert pbs.qsubs() == 1 and not pbs.jobs["101.server"].get("deleted")

    pbs.fail_qstat, pbs.finish = 0, True  # PBS answers again
    data = chain_rest(built, settings)

    assert data["status"] == "SUCCEEDED" and pbs.qsubs() == 2 and "unavailable" not in data


def test_a_one_time_storage_error_at_an_allocations_end_is_waited_out_without_a_second_job(tmp_path, monkeypatch):
    """An I/O error while the next allocation is prepared leaves the decision to the next pass, which submits the next
    allocation once."""
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)  # allocation 1 runs
    real = stage._new_allocation
    fail = {"n": 1}

    def flaky(*args, **kwargs):
        if fail["n"]:
            fail["n"] -= 1
            raise OSError(5, "Input/output error")
        return real(*args, **kwargs)

    monkeypatch.setattr(stage, "_new_allocation", flaky)
    data = stage.monitor(built, settings)  # allocation 1 finished; preparing allocation 2 fails once

    assert data["status"] == "RUNNING" and data["observation"]["stale"]
    assert data["unavailable"] == "shared storage: OSError: Input/output error"
    assert [(a["sequence"], a["state"]) for a in data["allocations"]] == [(1, "ended")]
    data = stage.monitor(built, settings)  # the decision is made again: allocation 2, once
    assert [(a["sequence"], a["state"]) for a in data["allocations"]] == [(1, "ended"), (2, "submitted")]
    assert "unavailable" not in data
    data = chain_rest(built, settings)
    assert data["status"] == "SUCCEEDED" and pbs.qsubs() == 3 and calls(tmp_path) == 5


def test_a_ledger_that_cannot_be_written_never_stops_the_run(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    real = stage.write_json_file
    fail = {"n": 2}

    def flaky(path, payload, **kwargs):
        if fail["n"] and Path(path).name == "ledger.json":
            fail["n"] -= 1
            raise OSError(116, "Stale file handle")
        return real(path, payload, **kwargs)

    monkeypatch.setattr(stage, "write_json_file", flaky)
    assert stage.submit(built, settings)["status"] == "RUNNING"
    data = chain_rest(built, settings)
    assert data["status"] == "SUCCEEDED"
    assert ledger_of(built)["status"] == "SUCCEEDED" and len(ledger_of(built)["allocations"]) == 2


def test_files_not_yet_visible_after_pbs_ends_an_allocation_are_waited_for(tmp_path, monkeypatch):
    """The shared storage's negative cache: result.json not seen on the first passes after F is no lost allocation."""
    monkeypatch.setattr(stage, "VISIBILITY_GRACE_SECONDS", 60.0)
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)  # allocation 1 runs and hands off
    real = stage.read_result
    misses = {"n": 2}

    def stale_negative(*args, **kwargs):
        if misses["n"]:
            misses["n"] -= 1
            return None, stage.RESULT_MISSING
        return real(*args, **kwargs)

    monkeypatch.setattr(stage, "read_result", stale_negative)
    data = stage.monitor(built, settings)
    assert [(a["state"], a.get("result")) for a in data["allocations"]] == [("ended", None)]
    assert data["observation"]["detail"] == "Ending allocation 1"
    data = stage.monitor(built, settings)  # still unseen, within the grace
    assert len(data["allocations"]) == 1
    data = stage.monitor(built, settings)  # seen: a hand-off, not a lost allocation
    assert [(a["mode"], a.get("result")) for a in data["allocations"]] == [("process", "handoff"), ("process", None)]


def test_an_unreadable_result_is_a_storage_failure_not_a_lost_allocation(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)
    real = Path.read_text
    failing = {"n": 3}

    def eio(self, *args, **kwargs):
        if self.name == "result.json" and failing["n"]:
            failing["n"] -= 1
            raise OSError(5, "Input/output error")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", eio)
    for _ in range(3):
        data = stage.monitor(built, settings)
        assert data["status"] == "RUNNING" and data["unavailable"] == "shared storage: OSError: Input/output error"
        assert [(a["state"], a.get("result")) for a in data["allocations"]] == [("ended", None)]
    data = stage.monitor(built, settings)
    assert [a.get("result") for a in data["allocations"]] == ["handoff", None]


def test_the_check_stage_passes_only_for_a_finished_continued_run(tmp_path, monkeypatch, capsys):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    data = chain(built, settings, ChainPBS(built), monkeypatch)
    assert data["status"] == "SUCCEEDED"
    monkeypatch.setattr(stage, "load_settings", lambda path: settings)
    monkeypatch.setattr(stage, "submission_path", lambda s, i: Path(built["run_directory"]) / "bdp.json")
    import argparse

    assert stage._stage(argparse.Namespace(config="x", action="check", submission_id=SUBMISSION)) == 0


def _hold_lock_in_another_process(path: Path, kind: str) -> subprocess.Popen[str]:
    """A process that holds ``path``'s lock (``flock`` or ``lockf``) until its stdin closes; returns once it does."""
    code = (
        "import fcntl, os, sys\n"
        f"fd = os.open({str(path)!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
        f"fcntl.{'flock' if kind == 'flock' else 'lockf'}(fd, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "sys.stdin.read()\n"
    )
    process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None and process.stdout.readline().strip() == "held"
    return process


def test_another_process_holding_the_launchers_lock_makes_a_monitor_print_the_kept_observation(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built, run=False)
    pbs.hold = True
    monkeypatch.setattr(stage, "_pbs_run", pbs)
    stage.submit(built, settings)
    stage.monitor(built, settings)
    monkeypatch.setattr(stage, "LOCK_WAIT_SECONDS", 0.5)
    holder = _hold_lock_in_another_process(Path(settings.state_directory) / f"{SUBMISSION}.lock", "flock")
    try:
        commands = len(pbs.commands)
        data = stage.monitor(built, settings)
        assert data["observation"]["stale"] and len(pbs.commands) == commands  # no PBS call without the lock
        assert stage.cancel(built, settings) == {"status": "CANCEL_REQUESTED"}  # never fails DM's cancel workflow
        assert len(pbs.commands) == commands
    finally:
        holder.communicate("")
    data = stage.monitor(built, settings)
    assert not data["observation"].get("stale") and len(pbs.commands) > commands


# --- the runner's checkpoint, its end and its heartbeat ----------------------------------------------------------------


def test_a_runner_in_another_process_holding_the_run_lock_keeps_other_allocations_out(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    folder = output / "allocations/000002"
    folder.mkdir()
    (folder / "allocation.json").write_text(json.dumps({"submission_id": SUBMISSION, "sequence": 2}))
    holder = _hold_lock_in_another_process(output / "allocations/run.lock", "lockf")
    try:
        with pytest.raises(RuntimeError, match="Another allocation of this run is running"):
            runner.run(built["control_directory"], allocation=2)
        assert not (folder / "runner.lock").exists()  # nothing written
        assert (output / "segments/000000").is_dir()
    finally:
        holder.communicate("")
    assert runner.run(built["control_directory"], allocation=2)["status"] == "Finished"


def test_a_corrupt_checkpoint_keeps_the_committed_counts_and_evidence_never_zero(tmp_path, monkeypatch):
    """An error before the allocation could check the checkpoint (here a committed segment's file changed) fails
    the run with the counts last recorded and the reason, keeps every committed segment and its failure records,
    and publishes no merged file and no failure report that would claim the inputs were never run."""
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    folder = next(Path(built["run_directory"]).parents[1].glob("data/lm_frames"))
    write_frame(folder / "frame_2.h5", shape=(100, 100))  # the second input fails in allocation 1
    allocate(built, 1)
    output = Path(built["output_directory"])
    assert json.loads((output / "progress.json").read_text())["n_failed"] == 1
    segment = output / "segments/000000/output.h5"
    with open(segment, "r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        last = handle.read(1)
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes([last[0] ^ 1]))

    summary = allocate(built, 2)

    assert summary["status"] == "Failed"
    assert summary["counters"] == {"n_inputs": 5, "n_succeeded": 1, "n_failed": 1, "n_not_run": 3, "n_indexed": None}
    assert summary["message"].startswith("segment 0: its output.h5 changed after it was committed")
    assert "the 2 inputs committed before (1 succeeded, 1 failed) are kept unmerged in" in summary["message"]
    assert summary["provenance"]["checkpoint"] == {"kept": "segments", "n_succeeded": 1, "n_failed": 1, "merged": False}
    assert summary["failure_report"] is None and not (output / "failures.jsonl").exists()
    assert not (output / "output.h5").exists() and segment.exists()
    [record] = read_failure_records(output / "segments/000000/failures.jsonl")  # the failure's evidence, kept
    assert record.input_id == manifest_ids(built)[1] and "full detector" in record.message
    assert json.loads((output / "progress.json").read_text())["n_succeeded"] == 1  # never went back
    assert calls(tmp_path) == 1


@pytest.mark.parametrize("where", ["params", "background", "identity"])
def test_other_errors_before_the_checkpoint_is_checked_keep_the_recorded_counts(tmp_path, monkeypatch, where):
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    if where == "params":
        (output / "params.txt").write_text((output / "params.txt").read_text() + "\n")
    elif where == "background":
        (output / "background.json").write_text("{}")
    else:
        monkeypatch.setattr(laue_compute, "_software", lambda: {"lauelab": "another"})  # software replaced mid-run

    summary = allocate(built, 2)

    assert summary["status"] == "Failed" and summary["counters"]["n_succeeded"] == 2
    assert summary["counters"]["n_not_run"] == 2 and "kept unmerged in" in summary["message"]
    if where == "identity":
        assert "checkpoint.json disagrees with this allocation (software)" in summary["message"]
    assert (output / "segments/000000").is_dir() and calls(tmp_path) == 2


@pytest.mark.parametrize("where", ["segment_record", "segment_file", "background"])
def test_an_io_error_while_checking_the_checkpoint_fails_the_run_and_keeps_it(tmp_path, monkeypatch, where):
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    with monkeypatch.context() as patch:
        if where == "segment_file":
            original = laue_output.sha256

            def checksum(path):
                if Path(path) == output / "segments/000000/output.h5":
                    raise OSError(5, "Input/output error")
                return original(path)

            patch.setattr(laue_output, "sha256", checksum)
        else:
            target = {
                "segment_record": output / "segments/000000/segment.json",
                "background": output / "background.json",
            }[where]
            original_read = Path.read_text

            def read(path, *args, **kwargs):
                if path == target:
                    raise OSError(5, "Input/output error")
                return original_read(path, *args, **kwargs)

            patch.setattr(Path, "read_text", read)
        summary = allocate(built, 2)
    assert summary["status"] == "Failed" and "Input/output error" in summary["message"]
    assert summary["counters"]["n_succeeded"] == 2 and "kept unmerged in" in summary["message"]
    assert (output / "segments/000000").is_dir() and not (output / "output.h5").exists() and calls(tmp_path) == 2


def test_the_heartbeat_runs_while_an_allocation_checks_the_checkpoint(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    stale = {"heartbeat_at": "2000-01-01T00:00:00+00:00", "n_succeeded": 2, "n_failed": 0}
    (output / "progress.json").write_text(json.dumps(stale))
    seen: list[dict[str, Any]] = []
    real = laue_output.Segments.resume

    def resume(self):
        seen.append(json.loads((output / "progress.json").read_text()))
        return real(self)

    monkeypatch.setattr(laue_output.Segments, "resume", resume)
    allocate(built, 2)

    [progress] = seen
    assert progress["heartbeat_at"] > stale["heartbeat_at"] and progress["n_succeeded"] == 2


@pytest.mark.parametrize("reported", [4, 99])
def test_heartbeat_counts_do_not_override_verified_segments(tmp_path, monkeypatch, reported):
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    progress = json.loads((output / "progress.json").read_text())
    progress.update(heartbeat_at="2000-01-01T00:00:00+00:00", n_succeeded=reported)
    (output / "progress.json").write_text(json.dumps(progress))
    assert allocate(built, 2)["committed"]["n_succeeded"] == 4
    summary = allocate(built, 3)
    assert summary["status"] == "Finished" and summary["counters"]["n_succeeded"] == 5
    assert calls(tmp_path) == 5


class Killed(BaseException):
    """What a SIGKILL does to the runner, for these tests: it ends the process, with no summary written after it."""


def test_an_end_killed_after_publishing_is_not_trusted_by_the_stops_finalizer(tmp_path, monkeypatch):
    """An end killed once its merged files were published (no summary): nothing resumes it. The stop's finalizing
    allocation finds those files, trusts and replaces none of them, and fails the run with the segments kept."""
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    write = runner.write_json_file

    def no_summary(path, payload, **kwargs):
        if Path(path).name == "run.json":
            raise Killed
        return write(path, payload, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runner, "write_json_file", no_summary)
        with pytest.raises(Killed):
            allocate(built, 2)
    assert (output / "output.h5").exists() and not (output / "run.json").exists()
    published = {name: (output / name).read_bytes() for name in ("output.h5", "laue_matching.h5")}
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")

    summary = allocate(built, 3, finalize=True)

    assert (
        summary["status"] == "Failed"
        and "output.h5 is in the output folder before the run ended" in (summary["message"])
    )
    assert "kept unmerged in" in summary["message"] and (output / "segments/000001").is_dir()
    assert {name: (output / name).read_bytes() for name in published} == published and calls(tmp_path) == 4


def test_an_end_killed_after_its_summary_before_the_segments_were_removed_is_complete(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=3, frames=2)
    pbs = ChainPBS(built)
    real = runner.shutil.rmtree

    def killed_removal(path, *args, **kwargs):
        if Path(path).name == "segments":
            raise Killed
        return real(path, *args, **kwargs)

    original = pbs._run

    def run_allocation(pbs_id, job):
        if job["sequence"] == 2:
            monkeypatch.setattr(runner.shutil, "rmtree", killed_removal)
            try:
                original(pbs_id, job)
            except Killed:
                pass
            finally:
                monkeypatch.setattr(runner.shutil, "rmtree", real)
            return -9
        return original(pbs_id, job)

    pbs._run = run_allocation  # type: ignore[method-assign]
    data = chain(built, settings, pbs, monkeypatch)

    assert data["allocations"][-1]["result"] == "final" and pbs.qsubs() == 2
    summary = json.loads((Path(built["output_directory"]) / "run.json").read_text())
    assert summary["status"] == "Finished" and calls(tmp_path) == 3
    poller.publish_returned(built)  # the leftover segments are published with the run, never merged again
    assert frame_ids(Path(built["run_directory"]) / "output.h5") == manifest_ids(built)


def test_a_merge_error_in_a_stops_finalizer_fails_the_run_with_its_checkpoint(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    pbs = ChainPBS(built)
    original = pbs._run

    def run_allocation(pbs_id, job):
        if job["ngpus"]:
            code = original(pbs_id, job)
            (Path(built["control_directory"]) / "cancel.request").write_text("{}")
            return code
        with monkeypatch.context() as patch:

            def unavailable(*args, **kwargs):
                raise OSError(5, "Input/output error")

            patch.setattr(laue_compute, "merge_results_files", unavailable)
            return original(pbs_id, job)

    pbs._run = run_allocation  # type: ignore[method-assign]
    data = chain(built, settings, pbs, monkeypatch)

    output = Path(built["output_directory"])
    summary = json.loads((output / "run.json").read_text())
    assert [a["mode"] for a in data["allocations"]] == ["process", "finalize"] and data["status"] == "FAILED"
    assert summary["status"] == "Failed" and "Input/output error" in summary["message"]
    assert "kept unmerged in" in summary["message"] and summary["artifacts"] == {}
    assert (output / "segments/000000").is_dir() and not (output / "output.h5").exists() and calls(tmp_path) == 2


def test_a_signal_during_a_stops_completed_merge_keeps_its_published_outcome(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    original = laue_output.Segments.merge

    def merge(self, *args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(laue_output.Segments, "merge", merge)
    (Path(built["control_directory"]) / "cancel.request").write_text("{}")
    summary = allocate(built, 2, finalize=True)

    assert summary["status"] == "Cancelled"  # no claim that the two merged results were unmerged
    assert summary["counters"]["n_succeeded"] == 2 and "checkpoint" not in summary["provenance"]
    assert not (output / "segments").exists() and frame_ids(output / "output.h5") == manifest_ids(built)[:2]


def test_a_daemon_that_cannot_start_fails_the_run_with_its_checkpoint_kept(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=4, frames=2)
    allocate(built, 1)
    monkeypatch.setattr(laue_compute, "gpu_evidence", lambda pid: {"daemon_on_gpu": False})

    summary = allocate(built, 2)

    assert summary["status"] == "Failed" and summary["counters"]["n_succeeded"] == 2
    assert summary["message"].startswith("The GPU stream daemon runs on no GPU that nvidia-smi lists")
    assert "kept unmerged in" in summary["message"] and calls(tmp_path) == 2
    assert (Path(built["output_directory"]) / "segments/000000").is_dir()


@pytest.mark.parametrize("when", ["start", "processing", "close"])
def test_failed_heartbeat_write_never_cancels_or_truncates_a_continued_run(tmp_path, monkeypatch, when):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    output = Path(built["output_directory"])
    active = []
    failures = []
    armed = when == "start"
    original_write, original_start = runner.write_json_file, runner.FileHooks.start
    original_process, original_close = laue_compute._process_frame, runner.FileHooks.close

    def write(path, *args, **kwargs):
        nonlocal armed
        if Path(path).name == "progress.json" and armed:
            armed = False
            failures.append(path)
            raise OSError(5, "Input/output error")
        return original_write(path, *args, **kwargs)

    def start(self):
        active.append(self)
        original_start(self)

    def process(*args, **kwargs):
        nonlocal armed
        result = original_process(*args, **kwargs)
        if when == "processing" and not failures:
            armed = True
            active[-1].publish()  # the same write used by the heartbeat thread, between two inputs
        return result

    def close(self):
        nonlocal armed
        if when == "close" and not failures:
            armed = True
        return original_close(self)

    monkeypatch.setattr(runner, "write_json_file", write)
    monkeypatch.setattr(runner.FileHooks, "start", start)
    monkeypatch.setattr(runner.FileHooks, "close", close)
    monkeypatch.setattr(laue_compute, "_process_frame", process)
    data = chain(built, settings, ChainPBS(built), monkeypatch)
    summary = json.loads((output / "run.json").read_text())
    assert len(failures) == 1 and data["status"] == "SUCCEEDED"
    assert summary["status"] == "Finished" and summary["counters"]["n_succeeded"] == 5
    assert calls(tmp_path) == 5 and not (Path(built["control_directory"]) / "cancel.request").exists()


def test_heartbeat_thread_retries_after_a_write_error_instead_of_exiting(tmp_path, monkeypatch):
    real = runner.write_json_file
    attempts = []
    recovered = threading.Event()

    def write(path, *args, **kwargs):
        attempts.append(path)
        if len(attempts) == 2:  # start succeeded; first background heartbeat fails
            raise OSError(5, "Input/output error")
        result = real(path, *args, **kwargs)
        if len(attempts) >= 3:
            recovered.set()
        return result

    monkeypatch.setattr(runner, "write_json_file", write)
    hooks = runner.FileHooks(tmp_path, tmp_path / "cancel.request", interval=0.01, best_effort_progress=True)
    hooks.start()
    try:
        assert recovered.wait(2), "the heartbeat thread did not retry its failed write"
        assert not hooks.should_stop() and not hooks.shutting_down()
    finally:
        hooks.close()


# --- the finalizing allocation's request ------------------------------------------------------------------------------


def test_a_finalizing_allocations_script_asks_for_the_cpu_request_never_a_gpu(tmp_path, monkeypatch):
    from laueportal.remote.pbs import build_script

    *_, built = continued(tmp_path, monkeypatch, count=2, frames=1)
    script = build_script(built, allocation=4, finalize=True)
    assert "#PBS -q cpu\n" in script and "ngpus=0" in script and "#PBS -l walltime=02:00:00\n" in script
    assert script.rstrip().endswith("--allocation 4") and "#PBS -r n\n" in script
    gpu = build_script(built, allocation=4)
    assert "#PBS -q gpu\n" in gpu and "ngpus=1" in gpu
    without = {**built, "target": {k: v for k, v in built["target"].items() if k != "finalize_pbs"}}
    with pytest.raises(ValueError, match="never asks for the GPU queue"):
        build_script(without, allocation=4, finalize=True)
    with_gpu = {**built, "target": {**built["target"], "finalize_pbs": {**built["target"]["finalize_pbs"], "ngpus": 1}}}
    with pytest.raises(ValueError, match="asks for no GPU"):
        build_script(with_gpu, allocation=4, finalize=True)


@pytest.mark.parametrize(
    ("finalize_pbs", "missing"),
    [
        (None, "finalize_pbs (the CPU request of a run's finalizing allocation)"),
        ({"queue": "cpu", "walltime": "02:00:00", "ngpus": 1}, "finalize_pbs.ngpus (0 or absent"),
        ({"queue": "cpu"}, "finalize_pbs.walltime"),
    ],
)
def test_a_gpu_target_without_a_cpu_finalizing_request_is_not_configured(tmp_path, finalize_pbs, missing):
    from laueportal.remote.settings import RemoteSettingsError

    layout, engine, settings, run = setup(tmp_path, target={"finalize_pbs": finalize_pbs})
    with pytest.raises(RemoteSettingsError, match=re.escape(missing)):
        plan.build_plan(job_view(engine, run.job_id), settings, SUBMISSION, source_check=check(layout))


# --- the operator's salvage of a failed run ---------------------------------------------------------------------------


def test_salvage_merges_a_failed_runs_checked_segments_into_a_fresh_folder_only(tmp_path, monkeypatch):
    *_, settings, run, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    folder = next(Path(built["run_directory"]).parents[1].glob("data/lm_frames"))
    write_frame(folder / "frame_2.h5", shape=(100, 100))  # one failure, committed in allocation 1
    pbs = ChainPBS(built)
    original = pbs._run
    pbs._run = lambda pbs_id, job: -29 if job["sequence"] == 3 else original(pbs_id, job)  # type: ignore[method-assign]
    data = chain(built, settings, pbs, monkeypatch)
    assert data["status"] == "FAILED"
    output = Path(built["output_directory"])

    def snapshot() -> dict[str, bytes]:
        return {str(p.relative_to(output)): p.read_bytes() for p in output.rglob("*") if p.is_file()}

    before = snapshot()
    salvaged = laue_compute.salvage(output, tmp_path / "salvaged")

    assert salvaged == {"n_attempted": 4, "n_succeeded": 3, "n_frames": 3} and calls(tmp_path) == 3
    ids = manifest_ids(built)
    assert frame_ids(tmp_path / "salvaged/output.h5") == [ids[0], ids[2], ids[3]]
    with h5py.File(tmp_path / "salvaged/laue_matching.h5") as native:
        assert [value.decode() for value in native["frame_ids"][...]] == [ids[0], ids[2], ids[3]]
    assert [f.input_id for f in read_failure_records(tmp_path / "salvaged/failures.jsonl")] == [ids[1]]
    assert snapshot() == before  # nothing in the run's folder was written
    with pytest.raises(FileExistsError):
        laue_compute.salvage(output, tmp_path / "salvaged")  # never into an existing folder


def test_salvage_refuses_segments_that_do_not_check(tmp_path, monkeypatch):
    *_, built = continued(tmp_path, monkeypatch, count=5, frames=2)
    allocate(built, 1)
    output = Path(built["output_directory"])
    record = output / "segments/000000/segment.json"
    record.write_text(record.read_text().replace('"first": 0', '"first": 1'))

    with pytest.raises(laue_output.CheckpointError, match="segment 0: its first input is number 1, not 0"):
        laue_compute.salvage(output, tmp_path / "salvaged")
    assert not (tmp_path / "salvaged").exists()
