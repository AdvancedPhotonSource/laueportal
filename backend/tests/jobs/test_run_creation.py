"""Tests for reconstruction and indexing creation services (run records and publication)."""

import json
import os
import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.app import create_app
from laueportal.files.inputs import FileResolutionError, ResolvedInput, WorkflowValidationError
from laueportal.indexing import service as indexing_workflow
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.jobs import rundir
from laueportal.jobs.records import RunPublicationError
from laueportal.jobs.rundir import (
    MANIFEST_FILENAME,
    REQUEST_FILENAME,
    RESULTS_FILENAME,
    manifest_digest,
    read_manifest,
    read_request,
    verify_manifest,
)
from laueportal.jobs.state import ExecutionStateError, JobStatus, RunPhase, assert_enqueueable
from laueportal.reconstruction import service as reconstruction_workflow
from laueportal.reconstruction.wire.params import WireParamsV1
from laueportal.util.srange import srange
from tests.conftest import create_test_metadata
from tests.settings_support import make_database, make_settings


@pytest.fixture
def workflow_engine(tmp_path):
    _, engine = make_database(tmp_path, database={"path": tmp_path / "services.db"})
    try:
        yield engine
    finally:
        engine.dispose()


def _wire_request(input_path, output_template, **overrides):
    values = {
        "scan_number": 1,
        "input_path": os.fspath(input_path),
        "output_path_template": os.fspath(output_template),
        "filename_prefixes": ["wire_%d.h5"],
        "geometry_file": "/config/geometry.xml",
        "percent_brightest": 5.0,
        "wire_edges": "both",
        "depth_start": -10.0,
        "depth_end": 10.0,
        "depth_resolution": 0.5,
        "num_threads": 4,
        "memory_limit_mb": 1024,
        "scan_points": "1-2",
        "verbose": 1,
        "author": "scientist",
        "notes": "wire test",
        "algorithm_version": "wire-v1",
        "computer_name": "worker-a",
        "priority": 3,
        "submitted_at": datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return WireParamsV1(**values)


def _indexing_request(input_path, output_template, **overrides):
    values = {
        "scan_number": 1,
        "reconstruction_id": None,
        "input_path": os.fspath(input_path),
        "output_path_template": os.fspath(output_template),
        "filename_prefixes": ["index_%d_%d.h5"],
        "threshold": 250,
        "threshold_ratio": 4.5,
        "max_rfactor": 0.5,
        "box_size": 18,
        "max_number": 50,
        "min_separation": 40,
        "peak_shape": "Lorentzian",
        "scan_points": "1-2",
        "depth_range": "0-1",
        "detector_crop_x1": 0,
        "detector_crop_x2": 2047,
        "detector_crop_y1": 0,
        "detector_crop_y2": 2047,
        "min_size": 1.13,
        "max_peaks": 50,
        "smooth": False,
        "mask_file": None,
        "index_kev_max_calc": 17.2,
        "index_kev_max_test": 30.0,
        "index_angle_tolerance": 0.1,
        "index_h": 1,
        "index_k": 1,
        "index_l": 1,
        "index_cone": 72.0,
        "energy_unit": "keV",
        "exposure_unit": "sec",
        "cosmic_filter": True,
        "reciprocal_lattice_unit": "1/nm",
        "lattice_parameters_unit": "nm",
        "output_xml": "merged.xml",
        "geometry_file": "/config/geometry.xml",
        "crystal_file": "/config/Al.xtal",
        "depth": "-49.0",  # a number ("3D" would fail every frame)
        "beamline": "34ID-E",
        "author": "scientist",
        "notes": "index test",
        "algorithm_version": "lauego-v1",
        "computer_name": "worker-b",
        "priority": 4,
        "submitted_at": datetime(2026, 8, 19, 13, 0, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return LaueGoParamsV1(**values)


def _add_metadata(engine, *scan_numbers):
    with Session(engine) as session:
        session.add_all(create_test_metadata(number) for number in scan_numbers)
        session.commit()


def _create_files(directory, filenames):
    directory.mkdir()
    for filename in filenames:
        (directory / filename).write_text("")


def _count(engine, model):
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def _row_total(engine):
    """Rows across every table; the constant-row check counts all of them."""

    with Session(engine) as session:
        return sum(
            session.execute(select(func.count()).select_from(table)).scalar_one()
            for table in models.Base.metadata.sorted_tables
        )


def _job(engine, job_id):
    with Session(engine) as session:
        return session.get(models.Job, job_id)


def _fake_inputs(tmp_path, count, stem="wire"):
    return tuple(ResolvedInput(os.fspath(tmp_path / f"{stem}_{index}.h5"), 0, (index,)) for index in range(count))


def test_create_reconstruction_persists_a_run_and_publishes_its_manifest(workflow_engine, tmp_path):
    _add_metadata(workflow_engine, 1)
    input_path = tmp_path / "wire-input"
    output_template = tmp_path / "analysis" / "rec_%d" / "data"
    _create_files(input_path, ["wire_1.h5", "wire_2.h5"])
    subjob_inserts = []

    @event.listens_for(workflow_engine, "before_cursor_execute")
    def observe_subjob_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO subjob"):
            subjob_inserts.append(executemany)

    run = reconstruction_workflow.create_reconstruction(
        _wire_request(input_path, output_template, frame_skip=0, wire_skip=2), engine=workflow_engine
    )

    expected_output = os.fspath(tmp_path / "analysis" / "rec_1" / "data")
    assert run.id == 1
    assert run.method == "wire"
    assert run.output_path == expected_output
    assert run.wire_parameters.scan_points_len == 2
    assert run.wire_parameters.filename_prefixes == ["wire_%d.h5"]
    assert (run.wire_parameters.frame_skip, run.wire_parameters.wire_skip) == (0, 2)
    assert run.job.status == JobStatus.QUEUED
    assert run.job.phase == RunPhase.PUBLISHED
    assert run.job.submit_time == datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    assert run.job.start_time is None
    assert run.job.finish_time is None
    assert (run.job.n_inputs, run.job.n_succeeded, run.job.n_failed, run.job.n_not_run) == (2, 0, 0, 0)
    assert run.job.run_directory == expected_output
    assert run.job.manifest_path == os.path.join(expected_output, MANIFEST_FILENAME)
    assert run.job.request_path == os.path.join(expected_output, REQUEST_FILENAME)
    assert run.job.manifest_digest == manifest_digest(run.job.manifest_path)
    assert subjob_inserts == []
    assert "subjob" not in inspect(workflow_engine).get_table_names()
    assert_enqueueable(run.job)

    entries = list(read_manifest(run.job.manifest_path))
    assert [(entry.index, entry.input_id, entry.scan_point, entry.depth_point) for entry in entries] == [
        (0, "wire_1", 1, None),
        (1, "wire_2", 2, None),
    ]
    assert [os.path.basename(entry.source) for entry in entries] == ["wire_1.h5", "wire_2.h5"]
    verify_manifest(run.job.manifest_path, expected_digest=run.job.manifest_digest, expected_count=2)

    request = read_request(run.job.request_path)
    assert request["kind"] == "wire_reconstruction"
    assert request["run"] == {"reconstruction_id": 1, "job_id": 1, "scan_number": 1, "method": "wire"}
    assert request["request"]["geometry_file"] == "/config/geometry.xml"
    assert request["request"]["submitted_at"] == "2026-08-19T12:00:00+00:00"  # UTC
    assert (request["request"]["frame_skip"], request["request"]["wire_skip"]) == (0, 2)
    assert request["manifest"] == {"path": MANIFEST_FILENAME, "n_inputs": 2, "sha256": run.job.manifest_digest}
    assert sorted(os.listdir(expected_output)) == [MANIFEST_FILENAME, REQUEST_FILENAME]

    loaded = reconstruction_workflow.get_reconstruction(run.id, engine=workflow_engine)
    assert loaded.wire_parameters.geometry_file == "/config/geometry.xml"
    assert loaded.job.job_id == run.job_id
    assert reconstruction_workflow.get_reconstruction(999, engine=workflow_engine) is None


def test_create_indexing_supports_direct_and_reconstruction_parent_runs(workflow_engine, tmp_path):
    _add_metadata(workflow_engine, 1)
    wire_input = tmp_path / "wire-input"
    index_input = tmp_path / "index-input"
    _create_files(wire_input, ["wire_1.h5", "wire_2.h5"])
    _create_files(
        index_input,
        ["index_1_0.h5", "index_1_1.h5", "index_2_0.h5", "index_2_1.h5"],
    )
    reconstruction = reconstruction_workflow.create_reconstruction(
        _wire_request(wire_input, tmp_path / "analysis" / "rec_%d"),
        engine=workflow_engine,
    )

    direct = indexing_workflow.create_indexing(
        _indexing_request(index_input, tmp_path / "analysis" / "index_%d"),
        engine=workflow_engine,
    )
    child = indexing_workflow.create_indexing(
        _indexing_request(
            index_input,
            tmp_path / "analysis" / "child-index_%d",
            scan_number=None,
            reconstruction_id=reconstruction.id,
        ),
        engine=workflow_engine,
    )

    assert direct.id == 1
    assert direct.reconstruction_id is None
    assert direct.scan_number == 1
    assert direct.output_path == os.fspath(tmp_path / "analysis" / "index_1")
    assert direct.job.n_inputs == 4
    assert direct.job.phase == RunPhase.PUBLISHED
    assert direct.n_frames_indexed is None
    entries = list(read_manifest(direct.job.manifest_path))
    assert [(entry.index, entry.input_id, entry.scan_point, entry.depth_point) for entry in entries] == [
        (0, "index_1_0", 1, 0),
        (1, "index_1_1", 1, 1),
        (2, "index_2_0", 2, 0),
        (3, "index_2_1", 2, 1),
    ]
    request = read_request(direct.job.request_path)
    assert request["kind"] == "lauego_indexing"
    assert request["output"] == {"directory": direct.output_path, "results": RESULTS_FILENAME, "xml": "merged.xml"}
    assert request["request"]["max_peaks"] == 50
    assert request["request"]["min_size"] == 1.13
    assert direct.lauego_parameters.depth_range_len == 2
    assert direct.lauego_parameters.output_xml == "merged.xml"

    assert child.id == 2
    assert child.reconstruction_id == reconstruction.id
    assert child.reconstruction.id == reconstruction.id
    assert child.scan_number == 1
    assert child.job.start_time is None
    assert child.job.finish_time is None
    assert read_request(child.job.request_path)["run"]["reconstruction_id"] == reconstruction.id
    assert read_request(child.job.request_path)["request"]["scan_number"] == 1  # the reconstruction's
    loaded = indexing_workflow.get_indexing(child.id, engine=workflow_engine)
    assert loaded.reconstruction.id == reconstruction.id
    assert loaded.lauego_parameters.crystal_file == "/config/Al.xtal"
    assert indexing_workflow.get_indexing(999, engine=workflow_engine) is None


def test_a_run_created_with_a_fractional_threshold_ratio_shows_its_parameters(tmp_path):
    """The model's ratio is a float (lauelab's); the column is INTEGER, which SQLite stores 4.5 in unchanged, and the
    results viewer reads it back (``GET /indexing-runs/{id}``)."""
    settings, engine = make_database(tmp_path, database={"path": tmp_path / "ratio.db"})
    try:
        _add_metadata(engine, 1)
        index_input = tmp_path / "index-input"
        _create_files(index_input, ["index_1_0.h5", "index_1_1.h5", "index_2_0.h5", "index_2_1.h5"])
        run = indexing_workflow.create_indexing(
            _indexing_request(index_input, tmp_path / "analysis" / "index_%d", threshold_ratio=4.5), engine=engine
        )
        response = TestClient(create_app(settings)).get(f"/api/v1/indexing-runs/{run.id}")
    finally:
        engine.dispose()

    assert response.status_code == 200
    assert response.json()["parameters"]["threshold_ratio"] == 4.5


def test_requests_require_an_id_placeholder_in_the_output_path(tmp_path):
    with pytest.raises(ValidationError, match="exactly one %d"):
        _wire_request(tmp_path, tmp_path / "fixed-output")


def test_indexing_request_rejects_reserved_support_file_names(tmp_path):
    with pytest.raises(ValidationError, match="reserved"):
        _indexing_request(tmp_path, tmp_path / "index_%d", output_xml="inputs.jsonl")
    with pytest.raises(ValidationError, match="reserved"):
        _indexing_request(tmp_path, tmp_path / "index_%d", output_xml="/elsewhere/output.h5")
    assert _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=" ").output_xml == "output.xml"


@pytest.mark.parametrize(
    "name",
    [
        "bdp.json",
        "incoming",
        "cancel.request",
        "progress.json",
        ".runner.lock",
        "ranks.json",
        "shards",
        "pbs.json",
        "pbs.json.tmp",
        "pbs-output.log",
        "job.pbs",
        "run.log",
    ],
)
def test_indexing_request_rejects_the_remote_execution_names(tmp_path, name):
    with pytest.raises(ValidationError, match="reserved"):
        _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name)


@pytest.mark.parametrize(
    "name",
    [
        ".bdp-publication.json",
        ".bdp-submission.lock",
        ".bdp-return-x1y2z3",
        ".cancel-sent",
        ".cancel-job.json",
        "/elsewhere/.bdp-anything.xml",
    ],
)
def test_indexing_request_rejects_the_portal_marker_prefixes(tmp_path, name):
    with pytest.raises(ValidationError, match="reserved for run support files"):
        _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name)


@pytest.mark.parametrize(
    "name",
    [
        "run.json.0123456789ab.partial",
        "progress.json.partial",
        "progress.json.0123456789ab.partial",
        "run.json.partial",
        "failures.jsonl.partial",
        "index.xml.partial",
        "/elsewhere/index.xml.partial",
    ],
)
def test_indexing_request_rejects_partial_file_names(tmp_path, name):
    with pytest.raises(ValidationError, match=r"reserved: names ending in '\.partial'"):
        _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name)


@pytest.mark.parametrize("name", ["../escape.xml", "sub/../../escape.xml", "sub/../inside.xml", "..", "a/.."])
def test_indexing_request_keeps_a_relative_output_xml_inside_the_run_directory(tmp_path, name):
    with pytest.raises(ValidationError, match=r"must stay inside the run directory.*'\.\.'"):
        _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name)


@pytest.mark.parametrize("name", ["output.h5/", "bdp.json/", "cancel.request/", "x.partial/", "sub/", "/elsewhere/"])
def test_indexing_request_rejects_an_output_xml_that_names_a_directory(tmp_path, name):
    """A trailing '/' would make lauego create a directory of that name."""

    with pytest.raises(ValidationError, match=r"must end in a file name, not '/'"):
        _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name)


@pytest.mark.parametrize("name", [".", "./", "sub/.", "/elsewhere/.", "/elsewhere/.."])
def test_indexing_request_rejects_an_output_xml_that_names_no_file(tmp_path, name):
    """'.' would put the XML partial next to the run directory, in its parent."""

    with pytest.raises(ValidationError, match="must end in a file name"):
        _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name)


@pytest.mark.parametrize(
    ("name", "match"),
    [
        ("output.h5/index.xml", r"'output\.h5' is reserved for run support files"),
        ("cancel.request/index.xml", r"'cancel\.request' is reserved for run support files"),
        ("sub/reconstruction/index.xml", r"'reconstruction' is reserved for run support files"),
        ("./progress.json", r"'progress\.json' is reserved for run support files"),
        ("incoming/index.xml", r"'incoming' is reserved for run support files"),
        ("sub/.cancel-sent/index.xml", r"'\.cancel-sent' is reserved for run support files"),
        ("x.partial/index.xml", r"'x\.partial' is reserved: names ending in '\.partial'"),
    ],
)
def test_indexing_request_rejects_reserved_directories_on_a_relative_output_xml(tmp_path, name, match):
    """lauego creates the directories of a relative name in the output directory."""

    with pytest.raises(ValidationError, match=match):
        _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name)


def test_indexing_request_accepts_other_output_xml_names(tmp_path):
    for name in ("index.xml", "sub/index.xml", "..index.xml", "a..b/x.xml", "/elsewhere/../index.xml"):
        assert _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name).output_xml == name
    for name in ("./index.xml", "sub//index.xml", "sub/./index.xml", "/output.h5/index.xml"):
        # Harmless spellings; an absolute name is used as given, so only its file name is checked.
        assert _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name).output_xml == name


def test_indexing_request_accepts_names_that_only_contain_a_marker_prefix(tmp_path):
    for name in ("index.bdp-1.xml", "x.cancel-sent.xml", "bdp.xml", "sub/incoming.xml", "/elsewhere/.bdp/x.xml"):
        assert _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name).output_xml == name


def test_indexing_request_accepts_names_that_only_contain_partial(tmp_path):
    for name in ("partial.xml", "index.partial.xml", "partial"):
        assert _indexing_request(tmp_path, tmp_path / "index_%d", output_xml=name).output_xml == name


def test_file_resolution_finishes_before_the_creation_transaction(workflow_engine, tmp_path, monkeypatch):
    _add_metadata(workflow_engine, 1)
    transaction_begins = 0

    @event.listens_for(workflow_engine, "begin")
    def count_transaction_begins(connection):
        nonlocal transaction_begins
        transaction_begins += 1

    def resolve_before_transaction(*args, **kwargs):
        assert transaction_begins == 0
        return _fake_inputs(tmp_path, 1)

    monkeypatch.setattr(reconstruction_workflow, "resolve_inputs", resolve_before_transaction)
    reconstruction_workflow.create_reconstruction(
        _wire_request(tmp_path / "unlisted", tmp_path / "rec_%d", scan_points="1"),
        engine=workflow_engine,
    )

    assert transaction_begins >= 1


def test_100k_input_manifest_creates_a_constant_number_of_rows(workflow_engine, tmp_path, monkeypatch):
    """A 100,000-input run adds three rows, not 100,000."""

    _add_metadata(workflow_engine, 1)
    inputs = _fake_inputs(tmp_path, 100_000)
    monkeypatch.setattr(reconstruction_workflow, "resolve_inputs", lambda *args, **kwargs: inputs)
    rows_before = _row_total(workflow_engine)

    started_at = time.monotonic()
    run = reconstruction_workflow.create_reconstruction(
        _wire_request(tmp_path / "unlisted", tmp_path / "rec_%d", scan_points="1"),
        engine=workflow_engine,
    )
    elapsed = time.monotonic() - started_at

    assert _row_total(workflow_engine) - rows_before == 3  # job, reconstruction_run, wire parameters
    assert run.job.n_inputs == 100_000
    assert elapsed < 20
    with open(run.job.manifest_path, "rb") as handle:
        line_count = sum(1 for _ in handle)
    assert line_count == 100_000
    last = None
    for entry in read_manifest(run.job.manifest_path):
        last = entry
    assert (last.index, last.input_id, last.scan_point) == (99_999, "wire_99999", 99_999)
    verify_manifest(run.job.manifest_path, expected_digest=run.job.manifest_digest, expected_count=100_000)


def test_manifest_publication_keeps_database_reads_responsive(workflow_engine, tmp_path, monkeypatch):
    _add_metadata(workflow_engine, 1)
    inputs = _fake_inputs(tmp_path, 20_000)
    monkeypatch.setattr(reconstruction_workflow, "resolve_inputs", lambda *args, **kwargs: inputs)
    publication_started = threading.Event()
    allow_publication = threading.Event()
    errors = []
    real_write_manifest = rundir.write_manifest

    def paused_write_manifest(*args, **kwargs):
        publication_started.set()
        if not allow_publication.wait(timeout=10):
            raise TimeoutError("test did not release the manifest write")
        return real_write_manifest(*args, **kwargs)

    monkeypatch.setattr(rundir, "write_manifest", paused_write_manifest)

    def create_large_run():
        try:
            reconstruction_workflow.create_reconstruction(
                _wire_request(tmp_path / "unlisted", tmp_path / "rec_%d", scan_points="1"),
                engine=workflow_engine,
            )
        except Exception as error:  # pragma: no cover - reported by the main thread
            errors.append(error)

    creator = threading.Thread(target=create_large_run, daemon=True)
    creator.start()
    assert publication_started.wait(timeout=10)

    read_started_at = time.monotonic()
    assert _count(workflow_engine, models.ReconstructionRun) == 1  # committed before publication
    assert _job(workflow_engine, 1).phase == RunPhase.CREATED
    read_duration = time.monotonic() - read_started_at

    allow_publication.set()
    creator.join(timeout=30)

    assert not creator.is_alive()
    assert errors == []
    assert read_duration < 2
    assert _job(workflow_engine, 1).phase == RunPhase.PUBLISHED


def test_multi_prefix_submission_records_template_identity(workflow_engine, tmp_path):
    _add_metadata(workflow_engine, 1)
    input_path = tmp_path / "wire-input"
    _create_files(input_path, ["left_1.h5", "right_1.h5"])

    run = reconstruction_workflow.create_reconstruction(
        _wire_request(
            input_path,
            tmp_path / "rec_%d",
            filename_prefixes=["left_%d.h5", "right_%d.h5"],
            scan_points="1",
        ),
        engine=workflow_engine,
    )

    entries = list(read_manifest(run.job.manifest_path))
    assert [(entry.input_id, entry.template_index, entry.scan_point) for entry in entries] == [
        ("left_1", 0, 1),
        ("right_1", 1, 1),
    ]


def test_resolution_failure_writes_no_database_rows_or_files(workflow_engine, tmp_path):
    _add_metadata(workflow_engine, 1)
    input_path = tmp_path / "wire-input"
    _create_files(input_path, ["wire_1.h5"])

    with pytest.raises(FileResolutionError, match="for 2"):
        reconstruction_workflow.create_reconstruction(
            _wire_request(input_path, tmp_path / "rec_%d"), engine=workflow_engine
        )

    assert _count(workflow_engine, models.Job) == 0
    assert _count(workflow_engine, models.ReconstructionRun) == 0
    assert not (tmp_path / "rec_1").exists()


def test_a_run_reading_more_inputs_than_allowed_is_refused_before_its_ranges_are_expanded(
    workflow_engine, tmp_path, monkeypatch
):
    """``execution.max_run_inputs`` (1,000,000 without settings), counted from the index ranges: a typed range
    cannot make run creation expand millions of indices."""
    input_path = tmp_path / "wire-input"
    _create_files(input_path, ["wire_1.h5", "wire_2.h5"])
    monkeypatch.setattr(srange, "list", lambda self: pytest.fail("a range was expanded"))

    with pytest.raises(WorkflowValidationError, match="read 2 inputs .* at most 1$"):
        reconstruction_workflow.create_reconstruction(
            _wire_request(input_path, tmp_path / "rec_%d"),
            engine=workflow_engine,
            settings=make_settings(tmp_path, execution={"max_run_inputs": 1}, files={"roots": {"data": tmp_path}}),
        )
    with pytest.raises(WorkflowValidationError, match="read 4,000,000 inputs .* at most 1,000,000$"):
        indexing_workflow.create_indexing(
            _indexing_request(input_path, tmp_path / "index_%d", scan_points="1-2000", depth_range="1-2000"),
            engine=workflow_engine,
        )

    assert _row_total(workflow_engine) == 0
    assert not [*tmp_path.glob("rec_*"), *tmp_path.glob("index_*")]


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"geometry_file": "/config/geometry.xml "}, "Geometry File has white space at its start or end"),
        ({"input_path": " {tmp}/wire-input"}, "Folder Path has white space"),
        ({"output_path_template": " {tmp}/rec_%d"}, "Output Path has white space"),
        ({"output_path_template": "rec_%d"}, "Output Path must be an absolute path"),
        ({"output_path_template": "{tmp}/../rec_%d"}, "Output Path: .* is outside the allowed roots"),
    ],
)
def test_run_creation_refuses_a_padded_path_and_an_output_folder_outside_the_roots(
    workflow_engine, tmp_path, values, match
):
    """Defence in depth: the seed and the runbook create runs without validating them first. A path with white space
    at its start or end would be read as another path than the roots check saw; the output folder must be absolute
    and, given the settings, inside the roots."""
    _add_metadata(workflow_engine, 1)
    input_path = tmp_path / "wire-input"
    _create_files(input_path, ["wire_1.h5", "wire_2.h5"])
    settings = make_settings(tmp_path, files={"roots": {"data": tmp_path}})
    request = _wire_request(input_path, tmp_path / "rec_%d")

    with pytest.raises(WorkflowValidationError, match=match):
        reconstruction_workflow.create_reconstruction(
            request.with_values(**{name: value.format(tmp=tmp_path) for name, value in values.items()}),
            engine=workflow_engine,
            settings=settings,
        )
    assert _row_total(workflow_engine) == 1  # the scan
    run = reconstruction_workflow.create_reconstruction(request, engine=workflow_engine, settings=settings)
    assert run.output_path == os.fspath(tmp_path / "rec_1")


def test_database_failure_rolls_back_job_run_and_parameters_without_files(workflow_engine, tmp_path):
    input_path = tmp_path / "wire-input"
    _create_files(input_path, ["wire_1.h5", "wire_2.h5"])

    with pytest.raises(IntegrityError):
        reconstruction_workflow.create_reconstruction(
            _wire_request(input_path, tmp_path / "rec_%d", scan_number=999),
            engine=workflow_engine,
        )

    assert _count(workflow_engine, models.Job) == 0
    assert _count(workflow_engine, models.ReconstructionRun) == 0
    assert _count(workflow_engine, models.WireReconstructionParameters) == 0
    assert not (tmp_path / "rec_1").exists()


def test_publication_failure_records_a_failed_non_runnable_run(workflow_engine, tmp_path):
    _add_metadata(workflow_engine, 1)
    input_path = tmp_path / "wire-input"
    _create_files(input_path, ["wire_1.h5", "wire_2.h5"])
    blocker = tmp_path / "rec_1"
    blocker.write_text("a file where the run directory should be")

    with pytest.raises(RunPublicationError, match=r"Reconstruction R1: Run publication failed"):
        reconstruction_workflow.create_reconstruction(
            _wire_request(input_path, tmp_path / "rec_%d"), engine=workflow_engine
        )

    job = _job(workflow_engine, 1)
    assert job.status == JobStatus.FAILED
    assert job.phase == RunPhase.FAILED
    assert job.finish_time is not None
    assert job.manifest_path is None
    assert (job.n_inputs, job.n_succeeded, job.n_failed, job.n_not_run) == (2, 0, 0, 2)
    assert job.messages.startswith("Run publication failed:")
    with pytest.raises(ExecutionStateError, match="only published runs"):
        assert_enqueueable(job)
    assert blocker.read_text() == "a file where the run directory should be"


def test_a_run_directory_holding_a_manifest_is_never_reused(workflow_engine, tmp_path):
    _add_metadata(workflow_engine, 1)
    input_path = tmp_path / "wire-input"
    _create_files(input_path, ["wire_1.h5", "wire_2.h5"])
    stale = tmp_path / "rec_1"
    stale.mkdir()
    (stale / MANIFEST_FILENAME).write_text(json.dumps({"index": 0}) + "\n")

    with pytest.raises(RunPublicationError, match="never reused"):
        reconstruction_workflow.create_reconstruction(
            _wire_request(input_path, tmp_path / "rec_%d"), engine=workflow_engine
        )

    assert (stale / MANIFEST_FILENAME).read_text() == json.dumps({"index": 0}) + "\n"
    assert _job(workflow_engine, 1).status == JobStatus.FAILED


def test_indexing_rejects_a_scan_that_disagrees_with_its_parent(workflow_engine, tmp_path):
    _add_metadata(workflow_engine, 1, 2)
    wire_input = tmp_path / "wire-input"
    index_input = tmp_path / "index-input"
    _create_files(wire_input, ["wire_1.h5", "wire_2.h5"])
    _create_files(
        index_input,
        ["index_1_0.h5", "index_1_1.h5", "index_2_0.h5", "index_2_1.h5"],
    )
    parent = reconstruction_workflow.create_reconstruction(
        _wire_request(wire_input, tmp_path / "rec_%d"), engine=workflow_engine
    )
    job_count = _count(workflow_engine, models.Job)

    with pytest.raises(WorkflowValidationError, match="does not match"):
        indexing_workflow.create_indexing(
            _indexing_request(
                index_input,
                tmp_path / "index_%d",
                scan_number=2,
                reconstruction_id=parent.id,
            ),
            engine=workflow_engine,
        )

    assert _count(workflow_engine, models.Job) == job_count
    assert _count(workflow_engine, models.IndexingRun) == 0
    assert not (tmp_path / "index_1").exists()
