"""A LaueMatching run on a GPU target: the stream daemon (a stand-in that matches with the CPU stand-in of
``test_laue_matching``), committed segments, the soft time limit, a stop and a daemon that ends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest

pytest.importorskip("laue_index")

from laueportal.indexing.laue_matching import compute as laue_compute  # noqa: E402
from laueportal.indexing.laue_matching import output, stream  # noqa: E402
from laueportal.jobs.contract import RunRequest  # noqa: E402
from laueportal.jobs.rundir import CATEGORY_CANCELLED, CATEGORY_NOT_RUN, ManifestEntry  # noqa: E402
from tests.indexing.test_laue_matching import (  # noqa: E402
    QUALITY,
    RECIP,
    Hooks,
    parameters,
    run,  # noqa: E402, F401 (the CPU path's fixture)
    stand_in,
    write_cache,
    write_frame,
)
from tests.laue_matching_stream_support import fake_daemon, on_gpu  # noqa: E402

STREAM = {"laue_matching_indexer": "stream", "segment_seconds": 0}
"""A GPU target's policy, a segment committed after every frame."""


@pytest.fixture
def gpu_run(tmp_path, monkeypatch):
    """Compute ``n`` frames with the GPU path and return the outcome, the run directory and the hooks."""
    data_root = tmp_path / "data"
    write_cache(data_root, parameters(data_root))
    cpu = stand_in(tmp_path)
    daemon = fake_daemon(tmp_path, cpu)
    monkeypatch.delenv("LAUEMATCHING_BIN", raising=False)
    monkeypatch.setattr(laue_compute, "stream_binary", lambda require: daemon)
    monkeypatch.setattr(laue_compute, "gpu_evidence", on_gpu)
    frame = write_frame(tmp_path / "frame.h5")

    def compute(n: int, *, execution=None, hooks=None, deadline=None, values=None, frames=None):
        hooks = hooks or Hooks()
        directory = tmp_path / "gpu"
        entries = [
            ManifestEntry(
                index=i, input_id=f"f{i}", source=str(frame), template_index=0, scan_point=i, depth_point=None
            )
            for i in range(n)
        ]
        if frames is not None:
            entries = [
                entry if path is None else _with_source(entry, path)
                for entry, path in zip(entries, frames, strict=True)
            ]
        request = RunRequest(
            job_id=1,
            kind=laue_compute.LAUE_MATCHING_KIND,
            display_id="I1",
            run_directory=str(directory),
            manifest_path=str(directory / "inputs.jsonl"),
            document={"request": values or parameters(data_root), "output": {}},
            n_inputs=n,
            workers=1,
            max_in_flight=None,
            execution=STREAM if execution is None else execution,
            deadline=deadline,
        )
        return laue_compute.compute_laue_matching(request, entries, hooks), directory, hooks

    return compute


def _with_source(entry: ManifestEntry, path: Path) -> ManifestEntry:
    import dataclasses

    return dataclasses.replace(entry, source=str(path))


def datasets(path: Path) -> dict[str, Any]:
    found: dict[str, Any] = {}
    with h5py.File(path) as handle:
        handle.visititems(lambda name, item: found.__setitem__(name, item[()]) if isinstance(item, h5py.Dataset) else 0)
    return found


def test_the_stream_path_gives_the_cpu_paths_results_and_removes_its_segments(gpu_run, run, tmp_path):  # noqa: F811
    outcome, directory, hooks = gpu_run(3)

    assert (outcome.n_succeeded, outcome.n_failed, outcome.n_indexed, outcome.stopped) == (3, 0, 3, False)
    assert outcome.validation_error is None and hooks.failures == []
    assert not (directory / output.SEGMENTS_DIRNAME).exists()
    assert [segment["n_frames"] for segment in outcome.provenance["segments"]] == [1, 1, 1]
    assert outcome.provenance["gpu"]["daemon_on_gpu"]
    assert outcome.provenance["binary"].endswith("LaueMatchingGPUStream")
    assert hooks.progress == [(1, 0), (2, 0), (3, 0), (3, 0)]
    with h5py.File(directory / "output.h5") as results:
        assert [name.decode() for name in results["frames/frame_ids"][()]] == ["f0", "f1", "f2"]
        np.testing.assert_array_equal(results["patterns/reciprocal"][2], RECIP.T)
        assert results["patterns/goodness"][()].tolist() == [QUALITY] * 3
    with h5py.File(directory / "laue_matching.h5") as native:
        assert native["solution_offsets"][()].tolist() == [0, 1, 2, 3]
        assert native["spot_offsets"][()].tolist() == [0, 2, 4, 6]
        assert list(native["solutions"].attrs["columns"]) == [f"c{i}" for i in range(laue_compute.SOLUTION_COLUMNS)]

    frame = write_frame(tmp_path / "frame.h5")
    cpu, cpu_directory, _ = run([frame, frame, frame])
    assert cpu.n_succeeded == 3
    on_cpu, on_gpu_side = datasets(cpu_directory / "output.h5"), datasets(directory / "output.h5")
    assert set(on_cpu) == set(on_gpu_side)
    for name, value in on_cpu.items():
        if name.endswith("_seconds"):
            continue
        np.testing.assert_array_equal(value, on_gpu_side[name], err_msg=name)


def test_a_frame_that_fails_is_reported_when_its_segment_is_committed(gpu_run, tmp_path):
    small = write_frame(tmp_path / "small.h5", shape=(256, 256))

    outcome, directory, hooks = gpu_run(3, frames=[None, small, None])

    assert (outcome.n_succeeded, outcome.n_failed) == (2, 1)
    assert [failure.input_id for failure in hooks.failures] == ["f1"]
    assert [segment["n_failed"] for segment in outcome.provenance["segments"]] == [0, 1, 0]
    with h5py.File(directory / "output.h5") as results:
        assert [name.decode() for name in results["frames/frame_ids"][()]] == ["f0", "f2"]


def test_the_time_limit_keeps_the_committed_frames_and_leaves_the_rest_not_run(gpu_run, monkeypatch):
    hooks = Hooks()
    # A clock that reads the committed frames: the limit comes once two are committed.
    monkeypatch.setattr(laue_compute, "_clock", lambda: len(hooks.progress))

    outcome, directory, hooks = gpu_run(4, deadline=2, hooks=hooks)

    assert (outcome.n_succeeded, outcome.n_failed, outcome.stopped) == (2, 0, False)
    assert [(failure.input_id, failure.category) for failure in hooks.failures] == [
        ("f2", CATEGORY_NOT_RUN),
        ("f3", CATEGORY_NOT_RUN),
    ]
    assert any("time limit after 2 of 4 frames" in warning for warning in outcome.warnings)
    assert outcome.artifacts["results"] == str(directory / "output.h5")


def test_a_stop_ends_the_daemon_keeps_finished_frames_and_cancels_the_rest(gpu_run):
    hooks = Hooks()
    original = hooks.report_progress

    def stop_after_first(*, succeeded, failed):
        original(succeeded=succeeded, failed=failed)
        hooks.stopping.set()

    hooks.report_progress = stop_after_first  # type: ignore[method-assign]
    outcome, directory, hooks = gpu_run(3, hooks=hooks)

    assert (outcome.n_succeeded, outcome.stopped) == (1, True)
    assert [(failure.input_id, failure.category) for failure in hooks.failures] == [
        ("f1", CATEGORY_CANCELLED),
        ("f2", CATEGORY_CANCELLED),
    ]
    assert (directory / "output.h5").exists()


def test_a_daemon_that_ends_fails_its_frame_and_leaves_the_rest_not_run(gpu_run, monkeypatch):
    monkeypatch.setenv("FAKE_STREAM_EXIT_AFTER", "1")

    outcome, directory, hooks = gpu_run(3)

    assert (outcome.n_succeeded, outcome.n_failed) == (1, 1)
    assert [failure.input_id for failure in hooks.failures] == ["f1", "f2"]
    assert hooks.failures[1].category == CATEGORY_NOT_RUN
    assert any("GPU stream daemon ended" in warning for warning in outcome.warnings)
    with h5py.File(directory / "output.h5") as results:
        assert [name.decode() for name in results["frames/frame_ids"][()]] == ["f0"]


def test_a_daemon_on_no_gpu_is_refused(gpu_run, monkeypatch):
    monkeypatch.setattr(laue_compute, "gpu_evidence", lambda pid: None)

    with pytest.raises(RuntimeError, match="runs on no GPU"):
        gpu_run(1)


def test_a_gpu_run_never_builds_a_forward_cache(gpu_run, tmp_path):
    with pytest.raises(RuntimeError, match="forward.bin is missing"):
        gpu_run(1, values=parameters(tmp_path / "data", create_forward_cache=True, forward_cache="new"))
    assert not (tmp_path / "data/new/forward.bin").exists()


def test_the_daemon_is_found_by_name_and_never_through_laue_matching_bin(tmp_path, monkeypatch):
    daemon = tmp_path / "LaueMatchingGPUStream"
    monkeypatch.delenv("LAUEMATCHING_BIN", raising=False)
    assert stream.stream_binary(lambda kind: daemon) == daemon
    with pytest.raises(RuntimeError, match="is not LaueMatchingGPUStream"):
        stream.stream_binary(lambda kind: tmp_path / "LaueMatchingCPU")
    monkeypatch.setenv("LAUEMATCHING_BIN", str(tmp_path / "LaueMatchingCPU"))
    with pytest.raises(RuntimeError, match="LAUEMATCHING_BIN is set"):
        stream.stream_binary(lambda kind: daemon)


def test_a_local_run_with_segments_matches_with_the_cpu_binary(gpu_run, tmp_path, monkeypatch):
    monkeypatch.setenv("LAUEMATCHING_BIN", str(tmp_path / "LaueMatchingCPU"))  # the CPU stand-in gpu_run wrote
    outcome, directory, _ = gpu_run(2, execution={"segment_seconds": 0})

    assert outcome.n_succeeded == 2
    assert outcome.provenance["binary"].endswith("LaueMatchingCPU")
    assert "gpu" not in outcome.provenance
    assert [segment["n_frames"] for segment in outcome.provenance["segments"]] == [1, 1]


def test_native_tables_merge_with_their_offsets_moved(tmp_path):
    class Frame:
        def __init__(self, solutions: int, spots: int) -> None:
            self.solutions = np.full((solutions, output.SOLUTION_COLUMNS), float(solutions))
            self.kept = np.ones(solutions, dtype=bool)
            self.spots = np.zeros((spots, output.SPOT_COLUMNS))
            self.unique_labels = np.zeros(solutions, dtype=np.int32)

    paths = []
    for number, frames in enumerate(([Frame(2, 3), Frame(0, 0)], [Frame(1, 4)])):
        path = tmp_path / f"{number}.h5"
        tables = output.NativeTables(path)
        tables.set_columns("%a b", "%c d")
        for index, frame in enumerate(frames):
            tables.append(f"{number}-{index}", frame)
        tables.close()
        paths.append(path)

    assert output.merge_native_tables(paths, tmp_path / "merged.h5") == 3
    with h5py.File(tmp_path / "merged.h5") as merged:
        assert merged["solution_offsets"][()].tolist() == [0, 2, 2, 3]
        assert merged["spot_offsets"][()].tolist() == [0, 3, 3, 7]
        assert merged["frame_ids"].asstr()[()].tolist() == ["0-0", "0-1", "1-0"]
        assert list(merged["solutions"].attrs["columns"]) == ["a", "b"]


def test_a_daemon_that_reads_nothing_cannot_hold_a_frame_past_a_stop(tmp_path):
    import socket
    import time

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    matcher = stream.StreamMatcher(
        tmp_path / "LaueMatchingGPUStream",
        params=tmp_path / "params.txt",
        database=tmp_path / "db.bin",
        hkls=tmp_path / "hkls.csv",
        threads=1,
        folder=tmp_path,
        solution_columns=output.SOLUTION_COLUMNS,
        spot_columns=output.SPOT_COLUMNS,
        should_stop=lambda: True,
    )
    matcher.socket = socket.create_connection(server.getsockname())
    matcher.socket.settimeout(stream.SEND_SECONDS)
    peer, _ = server.accept()  # connected, never read
    started = time.monotonic()
    try:
        with pytest.raises(stream.StopRequested):
            matcher.match(np.zeros((4096, 4096)))  # 64 MiB: more than the socket buffers hold
        assert time.monotonic() - started < 10 * stream.SEND_SECONDS
    finally:
        matcher.close()
        peer.close()
        server.close()
