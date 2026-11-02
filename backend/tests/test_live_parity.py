"""Live against batch: a live run fed a batch run's inputs in another order gives the batch run's outputs, matched by
scan point.

- a live wire run gives the batch run's reconstruction, point by point, with lauelab's reconstruction tolerance
  (``assert_same_reconstruction``), on ``tests.wire_support``'s small stacks;
- a live LaueGo run fed a batch run's raw frames gives its indexing results, frame by frame, exactly
  (``assert_same_indexing_results``), on ``tests/fixtures/lauelab``'s synthetic frames;
- a linked pair (a live wire run and its follower) fed a batch run's stacks gives the batch indexing of their
  reconstruction (indexable stacks: ``tests.wire_support.write_laue_wire_scan``).
"""

from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lauelab.indexing import validate_results_file
from sqlalchemy.orm import Session

from laueportal.indexing import service as indexing_service
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.jobs import controls
from laueportal.jobs.arrivals import notify
from laueportal.jobs.state import JobStatus
from laueportal.reconstruction import service as reconstruction_service
from laueportal.reconstruction.wire.params import WireParamsV1
from tests.conftest import create_test_metadata
from tests.jobs.live_support import (
    create_live_follower,
    create_live_lauego_run,
    create_live_wire_run,
    lauego_live_params,
    live_settings,
)
from tests.results_support import (
    PathMap,
    assert_same_indexing_results,
    assert_same_reconstruction,
    frame_order,
    read_jsonl,
    reconstruction_tolerance,
)
from tests.run_support import enqueue_with, load_job
from tests.wire_support import GEOMETRY, LAUE_OPTIONS, OPTIONS, write_laue_wire_scan, write_wire_scan
from tests.worker_support import Live, run_burst_worker, start_worker, wait_for

pytestmark = pytest.mark.slow


def test_a_live_wire_run_fed_a_batch_runs_files_in_another_order_gives_the_same_reconstruction(tmp_path, live: Live):
    """The batch run first (no batch run is claimed while a live run is open), then the live run on the same files,
    admitted 3, 1, 4, 2; each point's reconstruction compared by its scan point."""
    folder = tmp_path / "wire-input"
    folder.mkdir()
    for number in range(1, 5):
        write_wire_scan(folder / f"wire_{number}.h5", seed=number)
    wire = {**OPTIONS, "geometry_file": str(GEOMETRY), "wire_edges": "leading"}
    with Session(live.engine) as session:
        session.add(create_test_metadata(1))
        session.commit()
    batch_params = WireParamsV1(
        scan_number=1,
        input_path=str(folder),
        output_path_template=str(tmp_path / "batch-run_%d"),
        filename_prefixes=["wire_%d.h5"],
        scan_points="1-4",
        verbose=0,
        computer_name="test-host",
        submitted_at=datetime(2026, 10, 26, 10, 0, 0, tzinfo=UTC),
        **wire,
    )
    batch = reconstruction_service.create_reconstruction(batch_params, engine=live.engine)
    enqueue_with(live.engine, batch.job_id)
    run_burst_worker(live.config, log=tmp_path / "batch-worker.log", timeout=300)
    assert load_job(live.engine, batch.job_id).status == JobStatus.FINISHED

    run = create_live_wire_run(live.engine, tmp_path, name="wire", params=wire)
    assert run.folder == folder
    config = live.write_config(tmp_path / "live-worker.toml", live={"publish_seconds": 1.0, "arrivals_seconds": 0.05})
    worker = start_worker(config, log=tmp_path / "live-worker.log")
    try:
        for number in (3, 1, 4, 2):
            notify(live.engine, run.job_id, folder / f"wire_{number}.h5")
        completed = run.run_directory / "completed.jsonl"
        assert wait_for(lambda: completed.exists() and len(completed.read_text().splitlines()) == 4, 120)
        controls.cancel_run(run.job_id, engine=live.engine)
        assert worker.wait(120) == 0, worker.log_text()
    finally:
        worker.cleanup()
    assert load_job(live.engine, run.job_id).status == JobStatus.FINISHED

    batch_directory = Path(load_job(live.engine, batch.job_id).run_directory)
    by_scan_point = {entry["scan_point"]: entry["input_id"] for entry in read_jsonl(batch_directory / "inputs.jsonl")}
    live_points = {line["scan_point"]: line["point_id"] for line in read_jsonl(completed)}
    assert live_points == by_scan_point == {number: f"wire_{number}" for number in range(1, 5)}
    from lauelab.reconstruct import ScanReader

    with ScanReader(run.run_directory / "reconstruction" / "scan.h5") as scan:
        assert scan.point_ids == ("wire_3", "wire_1", "wire_4", "wire_2")  # arrival order
    rtol, atol = reconstruction_tolerance()
    assert_same_reconstruction(
        run.run_directory / "reconstruction", batch_directory / "reconstruction", PathMap(()), rtol=rtol, atol=atol
    )


LAUELAB_FIXTURES = Path(__file__).parent / "fixtures" / "lauelab"
LAUEGO_FRAMES = ("synthetic_ni_grain_a.h5", "synthetic_ni_two_grains.h5", "synthetic_ni_empty.h5")
LAUE_STACKS = ("synthetic_ni_grain_a.h5", "synthetic_ni_two_grains.h5", "synthetic_ni_grain_a.h5")
"""A linked pair's stacks (``write_laue_wire_scan``, each seeded by its number, so the two of grain A differ)."""


def _steps(path: Path) -> dict[str, bytes]:
    """An Output XML's steps by their frame (``inputImage``), without their timings (``executionTime``) and the white
    space after them (a document's layout)."""
    steps: dict[str, bytes] = {}
    for step in ET.parse(path).getroot().findall("step"):
        for element in step.iter():
            element.attrib.pop("executionTime", None)
        step.tail = None
        steps[step.findtext("detector/inputImage") or ""] = ET.tostring(step)
    return steps


def test_a_live_lauego_run_fed_a_batch_runs_frames_in_another_order_gives_the_same_results(tmp_path, live: Live):
    """The batch run first (no batch run is claimed while a live run is open), then the live run on the same frames
    with the same parameters, admitted 3, 1, 2 to a pool of two workers; each frame's results compared by its scan
    point."""
    folder = tmp_path / "frame-input"
    folder.mkdir()
    for number, fixture in enumerate(LAUEGO_FRAMES, start=1):
        shutil.copyfile(LAUELAB_FIXTURES / fixture, folder / f"frame_{number}.h5")
    settings = live_settings(tmp_path)
    calibration = {
        "geometry_file": os.fspath(LAUELAB_FIXTURES / "geoN_2022-03-29_14-15-05.xml"),
        "crystal_file": os.fspath(LAUELAB_FIXTURES / "Ni.xml"),
    }
    live_params = lauego_live_params(
        settings,
        input_path=os.fspath(folder),
        output_path_template=os.fspath(tmp_path / "frame-run_%d"),
        filename_prefixes=["frame_%d.h5"],
        **calibration,
    )
    batch_params = LaueGoParamsV1(
        **{
            **live_params.model_dump(),
            "schema_id": "lauego/v1",
            "scan_points": "1-3",
            "output_path_template": os.fspath(tmp_path / "batch-run_%d"),
        }
    )
    with Session(live.engine) as session:
        session.add(create_test_metadata(1))
        session.commit()
    batch = indexing_service.create_indexing(batch_params, engine=live.engine)
    enqueue_with(live.engine, batch.job_id)
    run_burst_worker(live.config, log=tmp_path / "batch-worker.log", timeout=300)
    assert load_job(live.engine, batch.job_id).status == JobStatus.FINISHED

    run = create_live_lauego_run(live.engine, tmp_path, settings=settings, params=calibration)
    assert run.folder == folder
    cpu_share = min(1.0, 2.5 / (os.cpu_count() or 1))  # two cores for a live run alone (dispatch.live_cores)
    config = live.write_config(
        tmp_path / "live-worker.toml", live={"publish_seconds": 1.0, "arrivals_seconds": 0.05, "cpu_share": cpu_share}
    )
    worker = start_worker(config, log=tmp_path / "live-worker.log")
    results = run.run_directory / "output.h5"
    try:
        for number in (3, 1, 2):
            notify(live.engine, run.job_id, folder / f"frame_{number}.h5")
        assert wait_for(lambda: results.exists() and len(frame_order(results)) == 3, 120), worker.log_text()
        controls.cancel_run(run.job_id, engine=live.engine)
        assert worker.wait(120) == 0, worker.log_text()
    finally:
        worker.cleanup()
    assert load_job(live.engine, run.job_id).status == JobStatus.FINISHED

    batch_directory = Path(load_job(live.engine, batch.job_id).run_directory)
    by_scan_point = {entry["scan_point"]: entry["input_id"] for entry in read_jsonl(batch_directory / "inputs.jsonl")}
    arrivals = read_jsonl(run.run_directory / "arrivals.jsonl")
    live_frames = {line["scan_point"]: Path(line["path"]).stem for line in arrivals}
    assert live_frames == by_scan_point == {number: f"frame_{number}" for number in range(1, 4)}
    reordered = assert_same_indexing_results(results, batch_directory / "output.h5", PathMap(()), merged=True)
    assert frame_order(results) == ["frame_3", "frame_1", "frame_2"]  # arrival order
    assert reordered == [("frame_3", 0, 2), ("frame_1", 1, 0), ("frame_2", 2, 1)]
    assert _steps(run.run_directory / "output.xml") == _steps(batch_directory / "output.xml")


def test_a_linked_pair_fed_a_batch_runs_stacks_in_another_order_gives_the_batch_indexing_of_their_reconstruction(
    tmp_path, live: Live
):
    """A linked pair: a batch wire run on three stacks whose depth images index (``write_laue_wire_scan``: the synthetic
    Ni frames) and a batch LaueGo run on its reconstruction (depths 0-2 of points 1-3) first; then a live wire run fed
    the same stacks, admitted 3, 1, 2, and its follower with the same indexing parameters. Each frame of the follower's
    results equals the batch run's, matched by scan point (its frames ``<point>_<depth>``, its points the stacks'
    stems)."""
    folder = tmp_path / "wire-input"
    folder.mkdir()
    for number, frame in enumerate(LAUE_STACKS, start=1):
        write_laue_wire_scan(folder / f"wire_{number}.h5", frame, seed=number)
    wire = {**LAUE_OPTIONS, "geometry_file": str(GEOMETRY), "wire_edges": "leading"}
    with Session(live.engine) as session:
        session.add(create_test_metadata(1))
        session.commit()
    batch_wire = reconstruction_service.create_reconstruction(
        WireParamsV1(
            scan_number=1,
            input_path=str(folder),
            output_path_template=str(tmp_path / "batch-rec_%d"),
            filename_prefixes=["wire_%d.h5"],
            scan_points="1-3",
            verbose=0,
            computer_name="test-host",
            submitted_at=datetime(2026, 10, 26, 10, 0, 0, tzinfo=UTC),
            **wire,
        ),
        engine=live.engine,
    )
    enqueue_with(live.engine, batch_wire.job_id)
    run_burst_worker(live.config, log=tmp_path / "batch-wire.log", timeout=300)
    assert load_job(live.engine, batch_wire.job_id).status == JobStatus.FINISHED
    batch_reconstruction = Path(load_job(live.engine, batch_wire.job_id).run_directory) / "reconstruction"

    settings = live_settings(tmp_path)
    indexing = {
        "geometry_file": os.fspath(GEOMETRY),
        "crystal_file": os.fspath(LAUELAB_FIXTURES / "Ni.xml"),
        "depth_range": "0-2",
    }
    batch_params = LaueGoParamsV1(
        **{
            **lauego_live_params(
                settings,
                input_path=os.fspath(batch_reconstruction / "scan.h5"),
                reconstruction_id=batch_wire.id,
                output_path_template=os.fspath(tmp_path / "batch-index_%d"),
                filename_prefixes=["wire_%d.h5"],
                **indexing,
            ).model_dump(),
            "schema_id": "lauego/v1",
            "scan_points": "1-3",
        }
    )
    batch = indexing_service.create_indexing(batch_params, engine=live.engine)
    enqueue_with(live.engine, batch.job_id)
    run_burst_worker(live.config, log=tmp_path / "batch-index.log", timeout=300)
    assert load_job(live.engine, batch.job_id).status == JobStatus.FINISHED

    run = create_live_wire_run(live.engine, tmp_path, name="wire", settings=settings, params=wire)
    assert run.folder == folder
    follower = create_live_follower(live.engine, tmp_path, run, settings=settings, params=indexing)
    cpu_share = min(1.0, 2.5 / (os.cpu_count() or 1))  # two cores for the pair: one each (wire_share 0.67)
    config = live.write_config(
        tmp_path / "live-worker.toml",
        live={"publish_seconds": 1.0, "arrivals_seconds": 0.05, "follow_seconds": 0.3, "cpu_share": cpu_share},
    )
    worker = start_worker(config, log=tmp_path / "live-worker.log")
    completed = run.run_directory / "completed.jsonl"
    try:
        assert wait_for(lambda: load_job(live.engine, follower).process_pid is not None, 120), worker.log_text()
        for number in (3, 1, 2):
            notify(live.engine, run.job_id, folder / f"wire_{number}.h5")
        assert wait_for(lambda: completed.exists() and len(completed.read_text().splitlines()) == 3, 120)
        controls.cancel_run(run.job_id, engine=live.engine)  # the session's Stop; the follower seals by itself
        assert worker.wait(120) == 0, worker.log_text()
    finally:
        worker.cleanup()
    assert load_job(live.engine, run.job_id).status == JobStatus.FINISHED
    assert load_job(live.engine, follower).status == JobStatus.FINISHED

    batch_directory = Path(load_job(live.engine, batch.job_id).run_directory)
    directory = Path(load_job(live.engine, follower).run_directory)
    by_scan_point = {entry["scan_point"]: entry["point_id"] for entry in read_jsonl(batch_directory / "inputs.jsonl")}
    arrivals = read_jsonl(directory / "arrivals.jsonl")
    live_points = {line["scan_point"]: line["point_id"] for line in arrivals}
    assert live_points == by_scan_point == {number: f"wire_{number}" for number in range(1, 4)}
    paths = PathMap(((os.fspath(batch_reconstruction), os.fspath(run.run_directory / "reconstruction")),))
    results = directory / "output.h5"
    assert_same_indexing_results(results, batch_directory / "output.h5", paths, merged=True)
    summary = validate_results_file(results)
    assert (summary.n_frames, summary.n_peaks, summary.n_patterns) == (9, 92, 4)  # 23 + 46 + 23 peaks at 0 µm
    order = [f"{line['point_id']}_{depth}" for line in arrivals for depth in range(3)]
    assert frame_order(results) == order  # the follower's arrival order: the wire run's publication order
    assert frame_order(batch_directory / "output.h5") == sorted(order)  # the batch run's: by scan point
    batch_points, live_points_folder = (text.encode() for text in paths.prefixes[0])
    batch_steps = {
        paths(image): step.replace(batch_points, live_points_folder)
        for image, step in _steps(batch_directory / "output.xml").items()
    }
    assert _steps(directory / "output.xml") == batch_steps  # but for their timings and the point files' folder
