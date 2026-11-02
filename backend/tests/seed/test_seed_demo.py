"""``laueportal seed demo`` end to end (slow: native compute; every input synthetic).

The command runs as it does from ``make dev-up`` and ``make e2e``: a fresh interpreter whose settings come from
``$LAUEPORTAL_CONFIG`` (the job table is the queue). With no worker serving the database it runs R1 and
I1 on a burst worker; ``--force`` then rebuilds the same database in place while a worker serves it, which runs
them.

The seeded runs are checked against their references: R1's reconstruction against lauelab's own reconstruction of
the same synthetic scan (R3's model), I1's results against their frames, the fine reconstruction against its point,
and the map against its results file. The committed data the demo copies is only read.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.core import migrations
from laueportal.core.db import database_engine
from laueportal.core.settings import DatabaseSettings
from laueportal.indexing.models import IndexingRun
from laueportal.indexing.results import STATUS_RESULTS, resolve_for_indexing_id
from laueportal.jobs.models import Job
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.reconstruction.views import locate_artifact
from laueportal.science import recon, synthetic
from laueportal.seed import build, demo
from tests.results_support import read_json, reconstruction_tolerance
from tests.settings_support import fresh_interpreter_env
from tests.worker_support import start_worker, wait_for

pytestmark = pytest.mark.slow


@dataclass(frozen=True)
class Seeded:
    root: Path
    config: Path
    database: Path
    runs_root: Path
    result: subprocess.CompletedProcess[str]
    data_before: dict[str, tuple[int, int]]

    @property
    def engine(self) -> Engine:
        return database_engine(DatabaseSettings(path=self.database))


def write_config(root: Path) -> Path:
    config = root / "seed.toml"
    config.write_text(
        f'[database]\npath = "{root / "demo.db"}"\n'
        f'[files]\nruns_root = "{root / "runs"}"\n'
        f'[files.roots]\nruns = "{root / "runs"}"\n'
    )
    return config


def seed(config: Path, *options: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "laueportal", "seed", "demo", *options],
        cwd=config.parent,
        env=fresh_interpreter_env(config),
        capture_output=True,
        text=True,
        timeout=900,
    )


def data_states() -> dict[str, tuple[int, int]]:
    data = demo.default_data_directory()
    return {name: ((data / name).stat().st_size, (data / name).stat().st_mtime_ns) for name in demo.DATA_FILES}


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seeded]:
    root = tmp_path_factory.mktemp("seed")
    config = write_config(root)
    before = data_states()
    result = seed(config)
    yield Seeded(root, config, root / "demo.db", root / "runs", result, before)


def test_the_seed_builds_the_database_with_a_burst_worker(seeded: Seeded) -> None:
    assert seeded.result.returncode == 0, seeded.result.stderr + seeded.result.stdout
    out = seeded.result.stdout
    assert f"{seeded.database}: created at revision {migrations.head_revision()}" in out
    assert "R1 and I1 ran on a burst worker" in out
    assert "R2, I2 and R3 are registered results" in out
    assert migrations.check(DatabaseSettings(path=seeded.database)) == []  # at head, matches the models
    marker = json.loads((seeded.runs_root / build.MARKER).read_text())
    assert marker["database"] == str(seeded.database)
    assert data_states() == seeded.data_before  # the committed data is only read


def test_the_runs_and_their_jobs(seeded: Seeded) -> None:
    engine = seeded.engine
    with Session(engine) as session:
        jobs = {job.job_id: job for job in session.query(Job)}
        reconstructions = {run.id: (run.job_id, run.scan_number) for run in session.query(ReconstructionRun)}
        indexing = {run.id: (run.job_id, run.scan_number) for run in session.query(IndexingRun)}
    engine.dispose()
    assert reconstructions == {1: (1, demo.SCAN_NUMBER), 2: (3, None), 3: (5, None)}
    assert indexing == {1: (2, demo.SCAN_NUMBER), 2: (4, None)}
    assert {job_id: (job.status, job.phase) for job_id, job in jobs.items()} == dict.fromkeys(
        (1, 2, 3, 4, 5), (JobStatus.FINISHED, RunPhase.FINISHED)
    )


def test_the_default_demo_has_no_crux_run(seeded: Seeded) -> None:
    """Without ``--crux-run`` the demo has no remote job, so no poller ever follows a run that no DM job stands
    behind."""
    engine = seeded.engine
    with Session(engine) as session:
        remote = session.query(Job).filter(Job.backend.in_(("aps", "crux"))).count()
        observed = session.query(Job).filter(Job.remote_observation.is_not(None)).count()
    engine.dispose()
    assert (remote, observed) == (0, 0)
    assert "R4" not in seeded.result.stdout


def test_r1_is_lauelabs_reconstruction_of_the_synthetic_scan_and_i1_indexes_its_frames(
    seeded: Seeded, tmp_path: Path
) -> None:
    """R1, run by the portal's wire compute, gives what lauelab's own reconstruction of its input with its settings
    gives (``science.synthetic.reconstruct_wire_scan``, the output type R1's point records), within lauelab's
    tolerance; I1's results hold its four frames and their four patterns."""
    from lauelab.indexing import validate_results_file
    from lauelab.reconstruct import PointReader

    runs = seeded.runs_root / f"scan_{demo.SCAN_NUMBER}"
    staged = seeded.runs_root / build.INPUTS / demo.WIRE_INPUT_DIR / demo.WIRE_INPUT_NAME
    rtol, atol = reconstruction_tolerance()
    with PointReader(runs / "rec_1" / "reconstruction" / "points" / "wire_1.h5") as r1:
        assert r1.shape == (51, 128, 128)
        reference = synthetic.reconstruct_wire_scan(
            staged,
            seeded.runs_root / build.INPUTS / demo.CALIBRATION_DIR / demo.GEOMETRY,
            tmp_path / "reference",
            output_pixel_type=int(r1.settings["output_pixel_type"]),
        )
        with PointReader(reference) as lauelab:
            np.testing.assert_array_equal(r1.depth_um, lauelab.depth_um)
            for index in range(lauelab.shape[0]):
                np.testing.assert_allclose(r1.frame(index), lauelab.frame(index), rtol=rtol, atol=atol)
    summary = validate_results_file(runs / "index_1" / "output.h5")
    assert (summary.n_frames, summary.n_patterns) == (4, 4)
    assert read_json(runs / "index_1" / "request.json")["request"]["geometry_file"] == str(
        seeded.runs_root / build.INPUTS / demo.CALIBRATION_DIR / demo.GEOMETRY
    )


def test_the_fine_reconstruction_is_a_finished_one_point_scan(seeded: Seeded) -> None:
    from lauelab.reconstruct import PointReader

    point = seeded.runs_root / build.INPUTS / demo.REGISTERED_DIR / "fine-reconstruction" / "points" / "fine_wire_1.h5"
    engine = seeded.engine
    with Session(engine) as session:
        run = session.get(ReconstructionRun, 2)
        assert run is not None and run.wire_parameters is not None
        job, parameters = run.job, run.wire_parameters
        assert (run.method, run.scan_number, run.notes) == ("wire", None, demo.FINE_NOTES)
        assert run.input_path == str(seeded.runs_root / build.INPUTS / demo.REGISTERED_DIR)
        assert run.output_path == job.run_directory == str(seeded.runs_root / "rec_2")
        assert (job.n_inputs, job.n_succeeded, job.computer_name) == (1, 1, demo.UNKNOWN_HOST)
        assert job.messages == demo.REGISTERED_MESSAGE.format(results=point)
        assert (parameters.filename_prefixes, parameters.scan_points, parameters.scan_points_len) == (
            ["fine_wire_%d.h5"], "1", 1,
        )  # fmt: skip
        assert (parameters.depth_start, parameters.depth_end, parameters.depth_resolution) == (-50.0, 50.0, 1.0)
        assert (parameters.num_threads, parameters.memory_limit_mb, parameters.wire_edges) == (1, 512, "leading")
        assert parameters.geometry_file == str(seeded.runs_root / build.INPUTS / demo.CALIBRATION_DIR / demo.GEOMETRY)
        run_directory = Path(run.output_path)
    engine.dispose()

    artifact = locate_artifact(str(run_directory))
    assert artifact == recon.Artifact(recon.KIND_SCAN, str(run_directory / "reconstruction" / "scan.h5"))
    [row] = recon.point_table(artifact).rows
    assert (row.point_id, row.status, row.n_depths, row.frame_shape) == ("fine_wire_1", "complete", 101, (256, 256))
    assert (row.depth_first_um, row.depth_last_um, row.dtype) == (-50.0, 50.0, "uint16")
    with PointReader(point) as source:
        frame = recon.stored_frame(artifact, "fine_wire_1", 50, cache=recon.ProductCache())
        np.testing.assert_array_equal(frame, source.frame(50))


def test_the_map_is_a_finished_lauego_run_with_its_results(seeded: Seeded) -> None:
    from lauelab.indexing import validate_results_file

    results = seeded.runs_root / build.INPUTS / demo.REGISTERED_DIR / "map" / "output.h5"
    summary = validate_results_file(results)
    engine = seeded.engine
    with Session(engine) as session:
        run = session.get(IndexingRun, 2)
        assert run is not None and run.lauego_parameters is not None
        job, parameters = run.job, run.lauego_parameters
        assert run.results_path == str(results)
        assert run.output_path == job.run_directory == str(seeded.runs_root / "index_2")
        assert (run.n_frames_indexed, job.n_inputs, job.n_succeeded) == (979, 1024, 1024)
        assert (run.scan_number, run.reconstruction_id, run.notes, run.author) == (None, None, demo.MAP_NOTES, "demo")
        assert run.input_path == str(seeded.runs_root / build.INPUTS / "synthetic-indexing")
        assert job.messages == demo.REGISTERED_MESSAGE.format(results=results)
        assert (parameters.filename_prefixes, parameters.scan_points, parameters.scan_points_len) == (
            ["frame_%d.h5"], "1-3", 1024,
        )  # fmt: skip
        assert (parameters.box_size, parameters.max_number, parameters.index_cone) == (18, 300, 72.0)
    resolution = resolve_for_indexing_id(2, engine=engine)
    engine.dispose()
    assert summary.n_frames == 1024
    assert resolution is not None and resolution.status == STATUS_RESULTS
    assert resolution.results_source == "recorded"
    assert list(Path(run.output_path).iterdir()) == []


def test_an_existing_database_is_refused_without_force(seeded: Seeded) -> None:
    before = seeded.database.stat().st_mtime_ns
    result = seed(seeded.config)
    assert result.returncode == 1
    assert result.stderr == (
        f"laueportal seed demo: {seeded.database} exists; pass --force to build it again "
        f"(its runs root {seeded.runs_root} is emptied too)\n"
    )
    assert seeded.database.stat().st_mtime_ns == before


def test_force_rebuilds_in_place_and_a_listening_worker_runs_r1_and_i1(seeded: Seeded) -> None:
    """As ``laueportal seed demo --force`` against the running dev stack: the database file stays the same (an
    open connection sees the new rows), the old runs are gone, and the stack's worker runs R1 and I1."""
    inode = seeded.database.stat().st_ino
    leftover = seeded.runs_root / "analysis" / "rec_9" / "request.json"  # a run of the old database
    leftover.parent.mkdir(parents=True)
    leftover.write_text("{}")
    held = sqlite3.connect(seeded.database)  # e.g. the API's pooled connection
    worker = start_worker(seeded.config, log=seeded.root / "worker.log", burst=False)
    try:
        assert wait_for(lambda: worker.log_text().count("Starting the worker on") == 1, timeout=60), worker.log_text()
        result = seed(seeded.config, "--force")
    finally:
        worker.process.terminate()
        worker.wait(60)
        worker.cleanup()
    assert result.returncode == 0, result.stderr + result.stdout
    assert f"{seeded.database}: emptied and rebuilt at revision" in result.stdout
    assert f"R1 and I1 ran on the worker (pid {worker.pid} on " in result.stdout
    assert seeded.database.stat().st_ino == inode
    jobs = held.execute("SELECT job_id, status FROM job ORDER BY job_id").fetchall()
    assert jobs == [(i, 2) for i in (1, 2, 3, 4, 5)]
    held.close()
    assert not leftover.exists()
    assert sorted(path.name for path in seeded.runs_root.iterdir()) == sorted(
        [build.MARKER, build.INPUTS, f"scan_{demo.SCAN_NUMBER}", "rec_2", "index_2", "rec_3"]
    )


def test_r4_runs_on_crux_with_the_golden_observation(seeded: Seeded) -> None:
    """R4 (a seeded observation; ``--crux-run`` only, as the e2e stack seeds it): R1's request on CRUX, recorded as
    submitted to DM and running there, with DM's job, its Globus run and the golden ``running`` as its stored
    observation. Last: it rebuilds the module's database (a burst worker runs R1 and I1)."""
    result = seed(seeded.config, "--force", "--crux-run")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "R4 recorded as running on CRUX, with the golden observation" in result.stdout
    goldens = json.loads((Path(__file__).parents[1] / "fixtures" / "observation_goldens" / "goldens.json").read_text())
    engine = seeded.engine
    with Session(engine) as session:
        jobs = {job.job_id: (job.status, job.phase) for job in session.query(Job)}
        run = session.get(ReconstructionRun, 4)
        assert run is not None
        job = run.job
        stored = (job.backend, job.submission_id, job.queue_job_id, job.external_id, job.external_state)
        url, observation, notes = job.external_url, job.remote_observation, run.notes
        linked = (run.job_id, run.scan_number)
    engine.dispose()
    assert jobs == {
        **dict.fromkeys((1, 2, 3, 4, 5), (JobStatus.FINISHED, RunPhase.FINISHED)),
        6: (JobStatus.RUNNING, RunPhase.RUNNING),
    }
    assert linked == (6, demo.SCAN_NUMBER)
    assert stored == ("crux", demo.REMOTE_SUBMISSION, f"dm:{demo.REMOTE_SUBMISSION}", "dm-1", "running")
    assert url == "https://app.globus.org/runs/0c5e7a12-1d3b-4f6a-8e2c-9b7d5f3a1e60"
    assert observation == json.loads(goldens["observations"]["running"])
    assert notes == demo.REMOTE_NOTES
    request = read_json(seeded.runs_root / f"scan_{demo.SCAN_NUMBER}" / "rec_4" / "request.json")
    assert request["request"]["backend"] == "crux"
