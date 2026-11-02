"""The demo scan 990001 and its runs R1 and I1, plus three runs registered with results computed elsewhere and, on
request, one recorded as running on CRUX. Every input is synthetic: copies of the committed fixtures
(``backend/tests``: ``fixtures/lauelab``'s synthetic Ni frames, geometry and crystal, ``scan_logs``) and files
``science.synthetic`` makes, all staged in ``<runs_root>/inputs`` (``DemoLayout.inputs``).

- **Scan 990001:** the seed rows (metadata, catalog, two scan dimensions on an artificial 2 x 2 grid).
- **R1:** a wire reconstruction of lauelab's synthetic wire scan (``science.synthetic.write_wire_scan``: 128 x 128,
  81 wire positions), staged as ``<inputs>/synthetic-reconstruction/wire_1.h5``.
- **I1:** a LaueGo indexing of the four synthetic Ni frames, staged as ``<inputs>/synthetic-indexing/frame_1.h5`` …
  ``frame_4.h5``. Only these working copies get artificial, labelled map positions (``entry1/sample/sample{X,Y,Z}``,
  in micrometres); the fixtures stay unchanged.
- **R2, the fine reconstruction** (``register_fine_reconstruction``): the same synthetic scan binned 8 (256 x 256),
  reconstructed by lauelab over 101 depths into uint16 (``<inputs>/registered/fine-reconstruction``), registered as
  a Finished wire run whose ``reconstruction/`` holds a copy of the point and a one-point catalog
  (``science.recon.publish_point_scan``).
- **I2, the map** (``register_indexing_map``): a LaueGo results file of 1,024 map positions in three dimensions
  (``science.synthetic.write_indexing_map``, from three of I1's frames, natively indexed; ``<inputs>/registered/
  map/output.h5``), registered as a Finished LaueGo run with I1's parameters and an empty run directory: the viewer's
  maps, 3D view and tables on more than a few frames.
- **R3, a multi-point reconstruction** (``register_multipoint_reconstruction``): three points made from the
  synthetic scan's reconstruction (51 depths of 128 x 128; ``<inputs>/registered/synthetic-reconstruction``),
  each with its own ID, sample position and depth profile (the frames rolled along the depth axis, so each point
  is brightest at another depth), catalogued by lauelab's own writer (``science.recon.publish_scan``) and
  registered as a Finished wire run: the wire viewer's point switching runs on it.
- **R4, a run on CRUX** (``add_remote_run``; only with ``laueportal seed demo --crux-run``, which the e2e stack
  passes): R1's request with ``backend = "crux"``, created and published as any run, then recorded as submitted to
  DM and running there (``state.mark_queued``, ``state.mark_running``, as the remote submission and poller do), with
  DM's job ID, the golden remote observation ``running`` (``REMOTE_OBSERVATION``) and its Globus run. No DM job
  stands behind it, so nothing may follow it: the option is refused while the settings enable remote execution
  (``seed.build.build_demo``), whose poller would ask DM about R4 on every pass, and such a database must never be
  served with remote execution enabled. The job page's Remote Run card and the run monitor's Status cell show it.

The staged inputs also hold the scan logs (``<inputs>/scan-logs``) and the calibration files
(``<inputs>/calibration``), so the e2e stack's samples root and scan-log folder are its runs root's ``inputs``.
``laueportal seed demo`` (``seed.build``) builds the demo database with these functions.

Nothing here reads the settings: the caller passes the engine, the layout and (to enqueue) the queue and the
policy, and may pass its settings for run creation's checks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.core.settings import Settings
from laueportal.core.timeutil import utcnow
from laueportal.indexing import service as indexing_service
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.indexing.models import IndexingRun
from laueportal.jobs import state
from laueportal.jobs.models import Job
from laueportal.jobs.queue import enqueue_run
from laueportal.jobs.rundir import RECONSTRUCTION_DIRECTORY
from laueportal.reconstruction import service as reconstruction_service
from laueportal.reconstruction.models import ReconstructionRun, WireReconstructionParameters
from laueportal.reconstruction.wire.params import WireParamsV1
from laueportal.remote import observation
from laueportal.remote.submission import queue_identity
from laueportal.scans.models import Catalog, Metadata, Scan
from laueportal.science import recon as science_recon
from laueportal.science import results as science_results
from laueportal.science import synthetic

SCAN_NUMBER = 990001

FIXTURES = "fixtures/lauelab"
"""The synthetic Ni frames, the geometry and the crystal (relative to the data directory)."""
GEOMETRY = "geoN_2022-03-29_14-15-05.xml"
CRYSTAL = "Ni.xml"
SCAN_LOGS = "scan_logs"
SCAN_LOG_FILES = ("test_log.xml", "test_log_2.xml")
WIRE_INPUT_DIR = "synthetic-reconstruction"
WIRE_INPUT_NAME = "wire_1.h5"
INDEXING_INPUT_DIR = "synthetic-indexing"
CALIBRATION_DIR = "calibration"
SCAN_LOG_DIR = "scan-logs"
REGISTERED_DIR = "registered"
FRAMES: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("synthetic_ni_grain_a.h5", (0, 0, 0)),
    ("synthetic_ni_two_grains.h5", (5, 0, 0)),
    ("synthetic_ni_empty.h5", (0, 5, 0)),
    ("synthetic_ni_grain_b.h5", (5, 5, 0)),
)
"""The source frames of ``frame_1.h5`` … ``frame_4.h5`` and their artificial map positions (micrometres)."""
FRAME_NOTE = "Synthetic portal fixture; artificial 2x2 map coordinates in micrometres"

ALGORITHM_VERSION = "lauelab 0.1.0"
"""The recorded algorithm version (a request value)."""
AUTHOR = "demo"
NOTE = (
    "Development fixtures: four synthetic Ni frames on an artificial 2x2 grid, "
    "plus an independent one-point synthetic wire-scan reconstruction. "
    "These are application test inputs, not a single experimental acquisition."
)
WIRE_NOTES = "Synthetic wire fixture; local CPU reconstruction."
INDEXING_NOTES = NOTE + " Raw-frame indexing; independent of the wire reconstruction."

FINE_SCAN = "fine_wire_1.h5"
"""R2's wire scan: the synthetic scan binned 8 (256 x 256), a 34-ID-E point name (``<prefix>_<n>``)."""
FINE_BINNING = 8
FINE_DEPTHS = (-50.0, 50.0)
FINE_PIXEL_TYPE = 3
"""lauelab's uint16 output."""
FINE_NOTES = (
    "Synthetic wire scan binned 8 (256 x 256), reconstructed over 101 depths into uint16 and registered by "
    "`laueportal seed demo` as a finished run."
)
MAP_FRAMES = (1, 2, 3)
"""The staged frames (``frame_<n>.h5``) the map is made from: grain A, two grains and the empty frame."""
MAP_NOTES = (
    "Synthetic map of 1,024 positions in three dimensions, made from three synthetic Ni frames indexed once "
    "(orientations turned along X, intensities and goodness scaled by seeded noise), registered by "
    "`laueportal seed demo` with results computed elsewhere. Not a measurement."
)
MULTIPOINT_SCAN = "synthetic_wire_scan.h5"
"""R3's model: the synthetic scan (R1's) under its own name, so its points are ``synthetic_wire_scan_<n>``."""
MULTIPOINT_SHIFTS: tuple[int, ...] = (0, -10, 10)
"""R3's points: each the model point with its frames (and depth profile) rolled by this many depths, so its
brightest depth is the model's (25) plus the shift."""
MULTIPOINT_POSITIONS: tuple[tuple[float, float, float], ...] = ((0.0, 0.0, 0.0), (5.0, 0.0, 0.0), (10.0, 0.0, 0.0))
"""Their sample positions (micrometres, artificial)."""
MULTIPOINT_NOTES = (
    "Synthetic three-point wire scan: copies of the synthetic scan's reconstructed point, each shifted in depth and "
    "placed apart, registered by `laueportal seed demo` as a finished run."
)
UNKNOWN_HOST = "unknown"
"""``job.computer_name`` of a registered run whose host was not recorded."""
REGISTERED_MESSAGE = "Registered by `laueportal seed demo` with results computed elsewhere: {results}"

DATA_FILES: tuple[str, ...] = (
    *(f"{FIXTURES}/{name}" for name, _position in FRAMES),
    f"{FIXTURES}/{GEOMETRY}",
    f"{FIXTURES}/{CRYSTAL}",
    *(f"{SCAN_LOGS}/{name}" for name in SCAN_LOG_FILES),
)
"""Every committed file the demo reads (relative to the data directory)."""


def default_data_directory() -> Path:
    """The committed test data of this checkout (``backend/tests``), which the demo copies from."""
    return Path(__file__).resolve().parents[3] / "tests"


@dataclass(frozen=True)
class DemoLayout:
    """Where the demo reads and writes."""

    data: Path
    """The committed test data (``backend/tests``, ``default_data_directory``); only read."""
    inputs: Path
    """Where the inputs are staged and made (``synthetic-reconstruction/``, ``synthetic-indexing/``,
    ``calibration/``, ``scan-logs/``, ``registered/``)."""
    runs_root: Path
    """Run directories: ``<runs_root>/scan_990001/rec_<R>`` and ``index_<I>``."""

    @property
    def wire_inputs(self) -> Path:
        return self.inputs / WIRE_INPUT_DIR

    @property
    def indexing_inputs(self) -> Path:
        return self.inputs / INDEXING_INPUT_DIR

    @property
    def geometry_file(self) -> Path:
        return self.inputs / CALIBRATION_DIR / GEOMETRY

    @property
    def crystal_file(self) -> Path:
        return self.inputs / CALIBRATION_DIR / CRYSTAL

    @property
    def registered(self) -> Path:
        """What the registered runs were computed from, and their results computed elsewhere."""
        return self.inputs / REGISTERED_DIR

    @property
    def scan_runs(self) -> Path:
        return self.runs_root / f"scan_{SCAN_NUMBER}"

    def reconstruction_run(self, reconstruction_id: int) -> Path:
        """The run directory of a registered reconstruction (no scan): ``<runs_root>/rec_<id>``."""
        return self.runs_root / f"rec_{reconstruction_id}"

    def indexing_run(self, indexing_id: int) -> Path:
        """The run directory of a registered indexing run (no scan): ``<runs_root>/index_<id>``."""
        return self.runs_root / f"index_{indexing_id}"

    def missing_data(self) -> list[Path]:
        """The committed files the demo needs that do not exist."""
        return [self.data / name for name in DATA_FILES if not (self.data / name).is_file()]


@dataclass(frozen=True)
class DemoRuns:
    reconstruction_id: int
    reconstruction_job_id: int
    indexing_id: int
    indexing_job_id: int

    @property
    def job_ids(self) -> tuple[int, int]:
        """R1 first, then I1."""
        return self.reconstruction_job_id, self.indexing_job_id


def _copy_once(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copyfile(source, destination)


def stage_inputs(layout: DemoLayout) -> None:
    """Make the demo's inputs in ``layout.inputs`` (once): the calibration files and scan logs copied, R1's wire
    scan written, and the frame copies with their map positions."""
    import h5py
    import numpy as np

    fixtures = layout.data / FIXTURES
    _copy_once(fixtures / GEOMETRY, layout.geometry_file)
    _copy_once(fixtures / CRYSTAL, layout.crystal_file)
    for name in SCAN_LOG_FILES:
        _copy_once(layout.data / SCAN_LOGS / name, layout.inputs / SCAN_LOG_DIR / name)

    layout.wire_inputs.mkdir(parents=True, exist_ok=True)
    wire_input = layout.wire_inputs / WIRE_INPUT_NAME
    if not wire_input.exists():
        partial = wire_input.with_name(wire_input.name + ".partial")
        synthetic.write_wire_scan(partial)
        partial.replace(wire_input)

    layout.indexing_inputs.mkdir(parents=True, exist_ok=True)
    for index, (name, position) in enumerate(FRAMES, start=1):
        destination = layout.indexing_inputs / f"frame_{index}.h5"
        if destination.exists():
            continue
        partial = destination.with_name(destination.name + ".partial")
        shutil.copy2(fixtures / name, partial)
        with h5py.File(partial, "r+") as file:
            sample = file.require_group("entry1/sample")
            for axis, coordinate in zip("XYZ", position, strict=True):
                key = f"sample{axis}"
                if key in sample:
                    dataset = sample[key]
                    assert isinstance(dataset, h5py.Dataset)
                    dataset[...] = coordinate
                else:
                    sample.create_dataset(key, data=np.array([coordinate], dtype=float))
                sample[key].attrs["unit"] = "micron"
            file.attrs["demo_note"] = FRAME_NOTE
        partial.replace(destination)


def add_demo_scan(engine: Engine, layout: DemoLayout, *, now: datetime | None = None) -> bool:
    """Insert the demo scan 990001; False when it already exists."""
    moment = now or utcnow()
    local = moment.astimezone()  # the scan's end time is the seeding machine's local wall-clock time
    epoch = int(moment.timestamp())
    with Session(engine) as session, session.begin():
        if session.get(Metadata, SCAN_NUMBER) is not None:
            return False
        session.add(
            Metadata(
                scan_number=SCAN_NUMBER, time=moment, time_epoch=epoch, user_name=AUTHOR,
                source_beam_bad="false", source_ccd_shutter="open", source_mono_trans_status="ok",
                source_energy_unit="keV", source_energy=20,
                sample_xyz_unit="micron", sample_xyz_desc="Synthetic fixture grid", sample_xyz="0,0,0",
                scan_end_abort="false", scan_end_time_epoch=epoch,
                scan_end_time=local.replace(tzinfo=None).isoformat(timespec="seconds"),
                scan_end_scan_duration_unit="s", scan_end_scan_duration=0,
                motor_group_sample_npts_total=4, motor_group_sample_cpt_total=4,
                motor_group_depth_npts_total=0, motor_group_depth_cpt_total=1,
                motor_group_energy_npts_total=0, motor_group_energy_cpt_total=1,
                motor_group_other_npts_total=0, motor_group_other_cpt_total=1,
            )
        )  # fmt: skip
        session.flush()
        session.add(
            Catalog(
                scan_number=SCAN_NUMBER,
                filefolder=str(layout.indexing_inputs),
                filename_prefix=["frame_%d.h5"],
                aperture="synthetic",
                sample_name="Synthetic portal demo",
                notes=NOTE,
            )
        )
        required = [
            column.name
            for column in Scan.__table__.columns
            if not column.nullable and column.name not in {"id", "scan_number", "scan_cpt"}
        ]
        for dimension, motor in ((1, "34ide:mxv:c0:m1"), (2, "34ide:mxv:c0:m3")):
            values: dict[str, object] = dict.fromkeys(required, "")
            values.update(
                scan_number=SCAN_NUMBER, scan_dim=dimension, scan_npts=2, scan_cpt=2, scan_after="true",
                scan_positioner1_pv=motor, scan_positioner1_ar="ABSOLUTE", scan_positioner1_mode="LINEAR",
                scan_positioner1="0,5",
            )  # fmt: skip
            session.add(Scan(**values))
    return True


def wire_request(
    layout: DemoLayout, *, computer_name: str | None = None, submitted_at: datetime | None = None
) -> WireParamsV1:
    """R1's request: the demo's values, with this layout's paths."""
    return WireParamsV1(
        scan_number=SCAN_NUMBER,
        input_path=str(layout.wire_inputs),
        output_path_template=str(layout.scan_runs / "rec_%d"),
        filename_prefixes=["wire_%d.h5"],
        geometry_file=str(layout.geometry_file),
        percent_brightest=100,
        wire_edges="leading",
        depth_start=-25,
        depth_end=25,
        depth_resolution=1,
        num_threads=1,
        memory_limit_mb=512,
        scan_points="1",
        verbose=1,
        author=AUTHOR,
        notes=WIRE_NOTES,
        algorithm_version=ALGORITHM_VERSION,
        computer_name=computer_name or socket.gethostname(),
        submitted_at=submitted_at or utcnow(),
    )


def indexing_request(
    layout: DemoLayout, *, computer_name: str | None = None, submitted_at: datetime | None = None
) -> LaueGoParamsV1:
    """I1's request: the demo's values, with this layout's paths."""
    return LaueGoParamsV1(
        scan_number=SCAN_NUMBER, reconstruction_id=None,
        input_path=str(layout.indexing_inputs),
        output_path_template=str(layout.scan_runs / "index_%d"),
        filename_prefixes=["frame_%d.h5"], threshold=None, threshold_ratio=4,
        max_rfactor=0.5, box_size=18, max_number=300, min_separation=20, peak_shape="Lorentzian",
        scan_points="1-4", depth_range=None,
        detector_crop_x1=0, detector_crop_x2=2047, detector_crop_y1=0, detector_crop_y2=2047,
        min_size=3, max_peaks=200, smooth=False, mask_file=None,
        index_kev_max_calc=17.2, index_kev_max_test=35, index_angle_tolerance=0.1,
        index_h=0, index_k=0, index_l=1, index_cone=72, energy_unit="keV", exposure_unit="sec",
        cosmic_filter=False, reciprocal_lattice_unit="1/nm", lattice_parameters_unit="nm",
        output_xml="output.xml", geometry_file=str(layout.geometry_file),
        crystal_file=str(layout.crystal_file), depth=None, beamline="34ID-E",
        author=AUTHOR, notes=INDEXING_NOTES,
        algorithm_version=ALGORITHM_VERSION, computer_name=computer_name or socket.gethostname(),
        submitted_at=submitted_at or utcnow(),
    )  # fmt: skip


def create_demo_runs(
    engine: Engine, layout: DemoLayout, *, computer_name: str | None = None, settings: Settings | None = None
) -> DemoRuns:
    """Stage the inputs, add scan 990001 if missing, then create (and publish) R1 and I1, in that order. With
    ``settings``, run creation also checks the run directories against the file roots."""
    stage_inputs(layout)
    add_demo_scan(engine, layout)
    reconstruction = reconstruction_service.create_reconstruction(
        wire_request(layout, computer_name=computer_name), engine=engine, settings=settings
    )
    indexing = indexing_service.create_indexing(
        indexing_request(layout, computer_name=computer_name), engine=engine, settings=settings
    )
    return DemoRuns(
        reconstruction_id=reconstruction.id,
        reconstruction_job_id=reconstruction.job_id,
        indexing_id=indexing.id,
        indexing_job_id=indexing.job_id,
    )


def enqueue_demo_runs(runs: DemoRuns, *, engine: Engine) -> Sequence[str]:
    """Queue R1, then I1 (the worker runs them in this order); returns their queue IDs."""
    return [enqueue_run(job_id, engine=engine) for job_id in runs.job_ids]


# ---------------------------------------------------------------------------
# A run on CRUX, as DM and the launcher would report it

REMOTE_NOTES = "R1's request on CRUX, recorded as running there; no remote run behind it."
REMOTE_SUBMISSION = "6f1c2a9e-4b7d-4c1a-9e3f-2d8b5a7c0e14"
"""The goldens' submission (``backend/tests/fixtures/observation_goldens/goldens.json``)."""
REMOTE_DM_JOB = "dm-1"
"""DM's job for the goldens' submission (``backend/tests/fixtures/observation_goldens/cases.py``)."""
REMOTE_DM_STATE = "running"
REMOTE_GLOBUS_RUN = "https://app.globus.org/runs/0c5e7a12-1d3b-4f6a-8e2c-9b7d5f3a1e60"
"""The goldens' Flow run (``flow_run_id``), as DM reports its URL."""
REMOTE_OBSERVATION = (
    '{"phase":"running","detail":"Processing","observed_at":"2026-10-25T12:00:00+00:00",'
    '"observer_observed_at":"2026-10-25T11:59:50+00:00","pbs":{"job_id":"123456.crux-pbs-01","status":"R"},'
    '"remote_progress":{"heartbeat_at":"2026-10-25T11:59:45+00:00","n_succeeded":3,"n_failed":0},'
    '"flow_state":"RunPbsJob","phase_since":"2026-10-25T12:00:00+00:00",'
    '"submission_id":"6f1c2a9e-4b7d-4c1a-9e3f-2d8b5a7c0e14","flow_run_id":"0c5e7a12-1d3b-4f6a-8e2c-9b7d5f3a1e60"}'
)
"""A ``running`` observation as the launcher prints it, verbatim (observed at a fixed time, so
the page shows it delayed)."""


def add_remote_run(engine: Engine, layout: DemoLayout, *, settings: Settings | None = None) -> int:
    """Create R4 (R1's request on CRUX) and record it as running there with ``REMOTE_OBSERVATION``; returns its
    reconstruction ID. With ``settings``, run creation also checks its folder against the file roots. No DM job
    stands behind it: never on a database whose settings enable remote execution (``build_demo`` refuses that)."""

    request = wire_request(layout).model_copy(update={"backend": observation.OBSERVED_TARGET, "notes": REMOTE_NOTES})
    run = reconstruction_service.create_reconstruction(request, engine=engine, settings=settings)
    with Session(engine) as session, session.begin():
        job = session.get(Job, run.job_id)
        if job is None:
            raise RuntimeError(f"R{run.id}'s job {run.job_id} is missing")
        # As remote.submission.submit_run records a submission, then the poller DM's first running state (DM's
        # job ID, its state and Globus run, and the observation, in the write that marks it running).
        job.submission_id = REMOTE_SUBMISSION
        state.mark_queued(job, queue_identity(REMOTE_SUBMISSION))
        job.external_id = REMOTE_DM_JOB
        job.external_state = REMOTE_DM_STATE
        job.external_url = REMOTE_GLOBUS_RUN
        job.remote_observation = json.loads(REMOTE_OBSERVATION)
        state.mark_running(job)
    return run.id


# ---------------------------------------------------------------------------
# Runs registered with results computed elsewhere


@dataclass(frozen=True)
class RegisteredRuns:
    fine_reconstruction_id: int
    map_indexing_id: int
    multipoint_reconstruction_id: int


def register_fine_reconstruction(engine: Engine, layout: DemoLayout) -> int:
    """Register R2: the synthetic scan binned 8, reconstructed in ``<inputs>/registered/fine-reconstruction``
    (``FINE_DEPTHS``, uint16), as a Finished one-point wire run; returns its reconstruction ID.

    The rows hold what the point file records: its settings, threads, memory budget, depth range and when it
    was written. The run directory is ``layout.reconstruction_run(<id>)``; its ``reconstruction/`` is a finished
    lauelab scan holding a copy of the point (``science.recon.publish_scan``)."""

    layout.registered.mkdir(parents=True, exist_ok=True)
    scan = synthetic.write_wire_scan(layout.registered / FINE_SCAN, binning=FINE_BINNING)
    point = synthetic.reconstruct_wire_scan(
        scan,
        layout.geometry_file,
        layout.registered / "fine-reconstruction",
        depth_range=FINE_DEPTHS,
        output_pixel_type=FINE_PIXEL_TYPE,
    )
    return _register_wire_scan(
        engine,
        layout,
        [point],
        notes=FINE_NOTES,
        geometry_file=layout.geometry_file,
        message=REGISTERED_MESSAGE.format(results=point),
    )


def map_request(layout: DemoLayout) -> LaueGoParamsV1:
    """The map's recorded request: I1's, on the frames the map was made from (``MAP_FRAMES``)."""
    return indexing_request(layout).with_values(scan_points=f"{MAP_FRAMES[0]}-{MAP_FRAMES[-1]}", notes=MAP_NOTES)


def register_indexing_map(engine: Engine, layout: DemoLayout) -> int:
    """Register the map (``science.synthetic.write_indexing_map`` of ``MAP_FRAMES``, with I1's peak search and
    indexing parameters) as a Finished LaueGo run; returns its indexing ID. Its ``results_path`` is
    ``<inputs>/registered/map/output.h5``; its run directory ``layout.indexing_run(<id>)`` is created empty. Every
    frame of the results file counts as an input."""

    request = indexing_request(layout)
    results = layout.registered / "map" / "output.h5"
    results.parent.mkdir(parents=True, exist_ok=True)
    started = utcnow()
    synthetic.write_indexing_map(
        results,
        [layout.indexing_inputs / f"frame_{number}.h5" for number in MAP_FRAMES],
        layout.geometry_file,
        layout.crystal_file,
        peaks={
            "boxsize": request.box_size,
            "max_rfactor": request.max_rfactor,
            "min_size": request.min_size,
            "min_separation": request.min_separation,
            "threshold": request.threshold,
            "threshold_ratio": request.threshold_ratio,
            "max_peaks": request.max_peaks,
        },
        indexing={
            "kev_max_calc": request.index_kev_max_calc,
            "kev_max_test": request.index_kev_max_test,
            "angle_tolerance_deg": request.index_angle_tolerance,
            "cone_deg": request.index_cone,
            "hkl_prefer": (request.index_h, request.index_k, request.index_l),
            "max_data": request.max_number,
        },
    )
    finished = utcnow()
    summary, reason = science_results.validate_results(results)
    if summary is None:
        raise ValueError(f"the map's results: {reason}")
    recorded = map_request(layout)
    with Session(engine) as session, session.begin():
        job = Job(computer_name=UNKNOWN_HOST, priority=0)
        state.initialize_registered(
            job,
            n_inputs=summary.n_frames,
            submitted_at=started,
            started_at=started,
            finished_at=finished,
            message=REGISTERED_MESSAGE.format(results=results),
        )
        session.add(job)
        session.flush()
        run = IndexingRun(
            scan_number=None,
            reconstruction_id=None,
            job=job,
            method="lauego",
            input_path=recorded.input_path,
            author=AUTHOR,
            notes=MAP_NOTES,
            algorithm_version=ALGORITHM_VERSION,
            created_at=started,
            n_frames_indexed=science_results.count_indexed_frames(results),
            results_path=str(results),
            lauego_parameters=indexing_service._lauego_parameters(recorded, scan_points_len=summary.n_frames),
        )
        session.add(run)
        session.flush()
        run_directory = layout.indexing_run(run.id)
        run.output_path = str(run_directory)
        job.run_directory = str(run_directory)
        run_directory.mkdir(parents=True, exist_ok=False)
        return run.id


def register_multipoint_reconstruction(engine: Engine, layout: DemoLayout, model: Path | None = None) -> int:
    """Register R3, a Finished wire run of three points made from ``model`` (by default the synthetic scan
    reconstructed in ``<inputs>/registered/synthetic-reconstruction``; ``MULTIPOINT_SHIFTS``,
    ``MULTIPOINT_POSITIONS``), catalogued by lauelab's writer; returns its reconstruction ID. The points are made
    in a staging folder beside the run directories, removed once the scan is published."""

    import h5py
    import numpy as np

    if model is None:
        layout.registered.mkdir(parents=True, exist_ok=True)
        scan = synthetic.write_wire_scan(layout.registered / MULTIPOINT_SCAN)
        model = synthetic.reconstruct_wire_scan(
            scan, layout.geometry_file, layout.registered / "synthetic-reconstruction"
        )
    stem = science_recon.read_point_record(model).point_id
    layout.runs_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".points-", dir=layout.runs_root) as staging:
        points = []
        for number, (shift, position) in enumerate(zip(MULTIPOINT_SHIFTS, MULTIPOINT_POSITIONS, strict=True), 1):
            point = Path(staging) / f"{stem}_{number}.h5"
            shutil.copyfile(model, point)
            with h5py.File(point, "r+") as file:
                for name in ("data/data", "reconstruction/depth_intensity", "reconstruction/computed_depth_intensity"):
                    dataset = file[f"entry1/{name}"]
                    assert isinstance(dataset, h5py.Dataset)
                    dataset[...] = np.roll(dataset[...], shift, axis=0)
                del file["entry1/reconstruction/point_id"]
                file.create_dataset("entry1/reconstruction/point_id", data=np.array([f"{stem}_{number}".encode()]))
                for axis, coordinate in zip("XYZ", position, strict=True):
                    dataset = file[f"entry1/sample/sample{axis}"]
                    assert isinstance(dataset, h5py.Dataset)
                    dataset[...] = coordinate
            points.append(point)
        return _register_wire_scan(
            engine,
            layout,
            points,
            notes=MULTIPOINT_NOTES,
            geometry_file=layout.geometry_file,
            message=REGISTERED_MESSAGE.format(results=model),
        )


def _register_wire_scan(
    engine: Engine, layout: DemoLayout, sources: Sequence[Path], *, notes: str, geometry_file: Path, message: str
) -> int:
    """Register the point files ``sources`` as a Finished wire run (``state.initialize_registered``) whose run
    directory's ``reconstruction/`` is a finished lauelab scan of copies of them (``science.recon.publish_scan``);
    returns its reconstruction ID. The rows hold what the first point records: its settings, threads, memory
    budget, depth range and when it was written (the run took ``elapsed_seconds`` before that); the raw input's
    directory is the one it records."""

    record = science_recon.read_point_record(sources[0])
    settings = record.settings
    # A 34-ID-E wire scan names its points <prefix>_<n>.h5 (scan point n).
    numbered = re.fullmatch(r"(.+)_(\d+)", record.point_id)
    prefix, first = (numbered.group(1) + "_%d", int(numbered.group(2))) if numbered else (record.point_id, None)
    if first is None:
        scan_points = ""
    else:
        last = first + len(sources) - 1
        scan_points = str(first) if last == first else f"{first}-{last}"
    finished = record.created or utcnow()
    started = finished - timedelta(seconds=max(record.elapsed_seconds, 0.0))
    depth_start, depth_end = (float(value) for value in settings["depth_range"])
    with Session(engine) as session, session.begin():
        job = Job(computer_name=UNKNOWN_HOST, priority=0)
        state.initialize_registered(
            job,
            n_inputs=len(sources),
            submitted_at=started,
            started_at=started,
            finished_at=finished,
            message=message,
        )
        session.add(job)
        session.flush()
        run = ReconstructionRun(
            scan_number=None,
            job=job,
            method="wire",
            input_path=os.path.dirname(record.source_path),
            author=None,
            notes=notes,
            algorithm_version=f"lauelab {record.lauelab_version}" if record.lauelab_version else None,
            created_at=started,
        )
        session.add(run)
        session.flush()
        run_directory = layout.reconstruction_run(run.id)
        run.output_path = str(run_directory)
        job.run_directory = str(run_directory)
        run.wire_parameters = WireReconstructionParameters(
            filename_prefixes=[f"{prefix}.h5"],
            geometry_file=str(geometry_file),
            percent_brightest=float(settings["percent_brightest"]),
            wire_edges=str(settings["wire_edge"]),
            depth_start=depth_start,
            depth_end=depth_end,
            depth_resolution=float(settings["resolution"]),
            frame_skip=int(settings["frame_skip"]),
            wire_skip=int(settings["wire_skip"]),
            num_threads=record.num_threads,
            memory_limit_mb=int(settings["memory_limit_mb"]),
            scan_points=scan_points,
            scan_points_len=len(sources),
            verbose=1,
        )
        science_recon.publish_scan(sources, run_directory / RECONSTRUCTION_DIRECTORY)
        return run.id


def register_runs(engine: Engine, layout: DemoLayout) -> RegisteredRuns:
    """R2 (the fine reconstruction), I2 (the map), then R3 (the multi-point reconstruction); the inputs must be
    staged (``stage_inputs``)."""
    fine = register_fine_reconstruction(engine, layout)
    indexing_map = register_indexing_map(engine, layout)
    return RegisteredRuns(fine, indexing_map, register_multipoint_reconstruction(engine, layout))
