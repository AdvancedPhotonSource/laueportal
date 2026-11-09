"""The Live page's API (``jobs.live_session``): ``GET /programs``' live programs, ``POST /live-runs/validate`` and
``POST /live-runs`` (one or two runs, the follower linked to the wire run, under one lock, queued together),
``GET /live/daqs`` (the shared DAQ list), the routes' gate (``[live] enabled``), and Stop through the job API.

The place: one allowed root holding the live folder (empty: its files arrive later), the lauelab fixtures' geometry
and crystal, and the runs root; scan 1 in the database. DM is a stand-in: ``tests.remote_support.FakeDMService``
(the real client and script, every process counted) or a patched ``DMClient`` call where a test holds one in
flight. The fast tests run the API with ``TestClient`` (real concurrent requests through one app) and the dispatcher
in this process with stand-in job processes; the slow one a real worker and its job processes.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from laueportal.app import create_app, openapi_document
from laueportal.core.errors import PROBLEM_JSON
from laueportal.core.settings import Settings
from laueportal.core.text import quoted
from laueportal.files.inputs import WorkflowValidationError
from laueportal.indexing.models import IndexingRun
from laueportal.jobs import creation, dispatch, live_session, programs, state
from laueportal.jobs.arrivals import NO_TEMPLATE, ArrivalRefused, notify
from laueportal.jobs.dispatch import Dispatcher, DispatchPolicy, LivePolicy
from laueportal.jobs.live import live_queue_id, read_live_document
from laueportal.jobs.live_session import SCAN_SETS
from laueportal.jobs.models import IdempotencyKey, Job
from laueportal.jobs.rundir import REQUEST_FILENAME
from laueportal.jobs.state import JobStatus, RunPhase
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.remote.dm import DaqRecord, DMClient
from laueportal.science.calibration import MAX_CALIBRATION_BYTES
from tests.api_support import PORTAL_HEADERS
from tests.conftest import create_test_metadata
from tests.jobs.test_dispatch import FakeJobs
from tests.jobs.test_live_dispatch import RUNS_ON
from tests.remote_support import FakeDMService
from tests.run_support import dispatch_queue, load_job
from tests.settings_support import make_database, make_settings
from tests.wire_support import GEOMETRY, OPTIONS, write_wire_scan
from tests.worker_support import Live, start_worker, wait_for

FIXTURES = Path(__file__).parents[1] / "fixtures" / "lauelab"
VALIDATE, CREATE, DAQS = "/api/v1/live-runs/validate", "/api/v1/live-runs", "/api/v1/live/daqs"
SESSION = "/api/v1/live/session"
DAQ_ID = "1a2b"


@dataclass
class Place:
    settings: Settings
    engine: Engine
    root: Path
    folder: Path
    """The live folder (empty)."""
    output: Path
    geometry: Path
    crystal: Path

    def scan(self, **values: Any) -> dict[str, Any]:
        return {
            "source": "notify",
            "daq_id": None,
            "folder": os.fspath(self.folder),
            "filename_template": "wire_%d.h5",
            "output_folder": os.fspath(self.output),
            "scan_number": 1,
            "author": "tester",
            **values,
        }

    def part(self, kind: programs.LiveProgramKind, **values: Any) -> dict[str, Any]:
        """A program card's values: the configured defaults (but the Scan card's fields), this place's files."""
        defaults = {name: value for name, value in programs.form_defaults(kind, self.settings).items()}
        files: dict[str, Any] = {"geometry_file": os.fspath(self.geometry)}
        if kind == "lauego-live":
            files["crystal_file"] = os.fspath(self.crystal)
        return {name: value for name, value in {**defaults, **files}.items() if name not in SCAN_SETS} | values

    def form(
        self,
        *,
        reconstruction: bool = True,
        indexing: bool = True,
        scan: dict[str, Any] | None = None,
        wire: dict[str, Any] | None = None,
        lauego: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A Both session (or one run alone) that passes every check."""
        return {
            "scan": self.scan(**(scan or {})),
            "reconstruction": self.part("wire-live", **(wire or {})) if reconstruction else None,
            "indexing": self.part("lauego-live", **(lauego or {})) if indexing else None,
        }


def make_live_place(tmp_path: Path, **live: Any) -> Place:
    root = tmp_path / "root"
    folder = root / "dm" / "34ide" / "data" / "scan_1"  # inside the stand-in DAQ's destination
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "geoN_2022-03-29_14-15-05.xml", root / "geometry.xml")
    shutil.copy(FIXTURES / "Ni.xml", root / "Ni.xml")
    settings, engine = make_database(
        tmp_path,
        files={"roots": {"data": root}, "runs_root": root / "runs"},
        live={"enabled": True, **live},
    )
    with Session(engine) as session, session.begin():
        session.add(create_test_metadata(1))
    return Place(settings, engine, root, folder, root / "out", root / "geometry.xml", root / "Ni.xml")


@pytest.fixture
def place(tmp_path) -> Iterator[Place]:
    yield (place := make_live_place(tmp_path))
    place.engine.dispose()


def portal(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), headers=PORTAL_HEADERS)


@pytest.fixture
def client(place: Place) -> Iterator[TestClient]:
    with portal(place.settings) as client:
        yield client


def start(client: TestClient, form: Any, key: str = "key-1") -> httpx.Response:
    return client.post(CREATE, json=form, headers={"Idempotency-Key": key})


def listed(response: httpx.Response) -> list[tuple[tuple[str | int, ...], str, str]]:
    """A validation's issues as ``(loc, severity, code)``, in order, each as often as it was reported."""
    assert response.status_code == 200, response.text
    return [(tuple(issue["loc"]), issue["severity"], issue["code"]) for issue in response.json()["issues"]]


def found(response: httpx.Response) -> set[tuple[tuple[str | int, ...], str, str]]:
    """A validation's issues as ``(loc, severity, code)``."""
    return set(listed(response))


def problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["status"] == status
    return body


def jobs(engine: Engine) -> list[Job]:
    with Session(engine) as session:
        return list(session.scalars(select(Job).order_by(Job.job_id)))


def daq(storage: Path, **values: Any) -> dict[str, Any]:
    """DM's record of the stand-in DAQ: running, writing to ``<storage>/34ide/data``."""
    return {
        "id": DAQ_ID,
        "experimentName": "e1",
        "status": "Running",
        "dataDirectory": "/detector/e1",
        "storageDirectory": os.fspath(storage),
        "destDirectory": "34ide/data",
        **values,
    }


# ---------------------------------------------------------------------------
# GET /programs and the gate


def test_programs_lists_the_live_programs_with_their_configured_defaults_only_while_live_is_enabled(place, tmp_path):
    """``wire-live`` and ``lauego-live`` as the batch programs are listed (fields, defaults from config, layout), the
    target the portal's (``local``, no choice), after the batch ones; with live processing off, the batch ones only."""
    with portal(place.settings) as client:
        listed = client.get("/api/v1/programs").json()["programs"]
    off = make_database(tmp_path / "off", live={"enabled": False})
    off[1].dispose()
    with portal(off[0]) as client:
        batch = client.get("/api/v1/programs").json()["programs"]

    assert [program["kind"] for program in batch] == ["lauego", "laue_matching", "wire"]
    assert [(program["kind"], program["schema_id"]) for program in listed] == [
        ("lauego", "lauego/v1"),
        ("laue_matching", "laue-matching/v1"),
        ("wire", "wire/v1"),
        ("lauego-live", "lauego-live/v1"),
        ("wire-live", "wire-live/v1"),
    ]
    for program in listed[3:]:
        schema = program["json_schema"]
        assert schema == programs.parameters_schema(program["kind"], place.settings)
        model = programs.MODELS[program["kind"]]
        assert set(schema["required"]) <= set(model.fields_from("form"))
        assert "x-options" not in schema["properties"]["backend"]  # derived: local
        assert "scan_points" not in [name for group in schema["x-layout"] for name in group["fields"]]
    wire = listed[4]["json_schema"]["properties"]
    assert wire["depth_start"]["default"] == place.settings.reconstruction.defaults.depth_start


@pytest.mark.parametrize(("method", "path"), [("GET", DAQS), ("POST", VALIDATE), ("POST", CREATE), ("GET", SESSION)])
def test_the_live_routes_answer_404_while_live_processing_is_off(tmp_path, monkeypatch, method, path):
    """``[live] enabled`` off: each live route is a 404 problem, as if absent (the routes stay in the contract, which
    never depends on the configuration); nothing is asked of DM, no key is claimed and nothing is created."""
    place = make_live_place(tmp_path, enabled=False, dm_setup_file=tmp_path / "dm-setup.sh")
    dm = FakeDMService(tmp_path / "dm", monkeypatch, workflow=False)
    with portal(place.settings) as client:
        response = client.request(method, path, json=place.form(), headers={"Idempotency-Key": "key-1"})

    body = problem(response, 404)
    assert (body["title"], body["detail"]) == ("Not Found", "Live processing is not enabled on this portal")
    assert dm.runs == [] and jobs(place.engine) == []
    with Session(place.engine) as session:
        assert session.scalars(select(IdempotencyKey)).all() == []
    place.engine.dispose()


# ---------------------------------------------------------------------------
# POST /live-runs: the session's runs


def test_a_both_session_is_a_live_wire_run_and_its_follower_queued_together(place, client):
    """One Start, two runs and jobs (a Both session): the wire run on the folder (its real path, as ``live.json``'s),
    ``<output>/rec_<id>``; the LaueGo run following it (its catalog as ``input_path``, its ID as ``reconstruction_id``,
    ``<output>/index_<id>``, ``live.json`` ``follows`` it), one scan number and author; ``request.json`` with exactly
    the batch keys; both queued as live runs."""
    response = start(client, place.form())

    assert response.status_code == 201, response.text
    wire, follower = response.json()["runs"]
    assert (wire["run"]["label"], follower["run"]["label"]) == ("R1", "I1")
    for answer in (wire, follower):
        assert (answer["status"], answer["target"], answer["failure"]) == ("queued", "local", None)
        assert answer["queue_id"] == live_queue_id(answer["job_id"])
    rows = {row.job_id: row for row in jobs(place.engine)}
    assert sorted(rows) == [wire["job_id"], follower["job_id"]]
    assert {(row.status, row.phase) for row in rows.values()} == {(JobStatus.QUEUED, RunPhase.QUEUED)}
    with Session(place.engine) as session:
        recon = session.get(ReconstructionRun, wire["run"]["id"])
        index = session.get(IndexingRun, follower["run"]["id"])
        assert recon is not None and index is not None
        wire_dir, follower_dir = Path(recon.output_path or ""), Path(index.output_path or "")
        assert (recon.scan_number, index.scan_number, recon.author, index.author) == (1, 1, "tester", "tester")
        assert recon.input_path == os.fspath(place.folder.resolve())
        assert index.reconstruction_id == recon.id
        assert index.input_path == os.fspath(wire_dir / "reconstruction" / "scan.h5")
    assert (wire_dir, follower_dir) == (place.output / f"rec_{recon.id}", place.output / f"index_{index.id}")
    wire_live, follower_live = read_live_document(wire_dir), read_live_document(follower_dir)
    assert (wire_live.source, wire_live.folder, wire_live.filename_template, wire_live.daq, wire_live.follows) == (
        "notify",
        os.fspath(place.folder.resolve()),
        "wire_%d.h5",
        None,
        None,
    )
    assert follower_live.source == "run" and follower_live.folder == wire_live.folder
    assert follower_live.follows is not None
    assert (follower_live.follows.job_id, follower_live.follows.directory) == (wire["job_id"], os.fspath(wire_dir))
    for directory, batch, schema in ((wire_dir, "wire", "wire-live/v1"), (follower_dir, "lauego", "lauego-live/v1")):
        request = json.loads((directory / REQUEST_FILENAME).read_text())["request"]
        assert set(request) == set(programs.PROGRAMS[batch].model_fields)  # exactly the batch keys
        assert (request["schema_id"], request["scan_points"], request["backend"]) == (schema, "", "local")


@pytest.mark.parametrize("run", ["reconstruction", "indexing"])
def test_a_session_of_one_run_creates_that_run_on_the_folder(place, client, run):
    """Reconstruction alone: one live wire run; Indexing alone: one live LaueGo run on the folder's raw frames (no
    reconstruction, ``live.json`` its own source)."""
    response = start(client, place.form(reconstruction=run == "reconstruction", indexing=run == "indexing"))

    assert response.status_code == 201, response.text
    (answer,) = response.json()["runs"]
    assert answer["run"]["kind"] == run and answer["status"] == "queued"
    (row,) = jobs(place.engine)
    document = read_live_document(row.run_directory or "")
    assert (document.source, document.folder, document.follows) == ("notify", os.fspath(place.folder.resolve()), None)
    if run == "indexing":
        with Session(place.engine) as session:
            index = session.get(IndexingRun, answer["run"]["id"])
            assert index is not None and index.reconstruction_id is None
            assert index.input_path == os.fspath(place.folder.resolve())


def test_a_daq_session_binds_its_daq_with_one_dm_call_and_live_json_names_it(place, tmp_path, monkeypatch):
    """A DAQ source: the bind check at Start (one DM DAQ-info call per live create, none for the list) gives
    ``live.json``'s ``daq`` (id, experiment, storage, destination)."""
    dm = FakeDMService(tmp_path / "dm", monkeypatch, workflow=False)
    dm.daqs = {DAQ_ID: daq(place.root / "dm")}
    settings = place.settings.model_copy(
        update={"live": place.settings.live.model_copy(update={"dm_setup_file": tmp_path / "dm-setup.sh"})}
    )
    with portal(settings) as client:
        response = start(client, place.form(scan={"source": "daq", "daq_id": DAQ_ID}))

    assert response.status_code == 201, response.text
    assert dm.operations() == ["daq"]
    for row in jobs(place.engine):
        document = read_live_document(row.run_directory or "")
        if document.source == "run":
            continue
        assert document.source == "daq" and document.daq is not None
        storage = os.fspath((place.root / "dm").resolve())
        assert (document.daq.id, document.daq.experiment, document.daq.storage) == (DAQ_ID, "e1", storage)
        assert document.daq.destination == os.path.join(storage, "34ide", "data")


def test_a_daq_session_on_a_folder_the_daq_has_not_made_yet_starts_and_live_json_names_it_normalized(
    place, tmp_path, monkeypatch
):
    """A DAQ makes its folder when it writes: a Folder that is not there yet is a warning for a DAQ source, and Start
    makes the bind check (one DM call: the folder, resolved through its nearest existing folder, inside the DAQ's
    destination). ``live.json``'s folder is that path normalized (the doubled and trailing slashes typed are gone),
    inside the destination, for both runs. Start does not make the folder: the DAQ does."""
    dm = FakeDMService(tmp_path / "dm", monkeypatch, workflow=False)
    dm.daqs = {DAQ_ID: daq(place.root / "dm")}
    settings = place.settings.model_copy(
        update={"live": place.settings.live.model_copy(update={"dm_setup_file": tmp_path / "dm-setup.sh"})}
    )
    data = place.root / "dm" / "34ide" / "data"
    form = place.form(scan={"source": "daq", "daq_id": DAQ_ID, "folder": f"{data}//scan_2/"})
    with portal(settings) as client:
        assert found(client.post(VALIDATE, json=form)) == {
            (("scan", "folder"), "warning", "not_found"),
            (("scan", "daq_id"), "warning", "checked_at_start"),
        }
        response = start(client, form)

    assert response.status_code == 201, response.text
    assert dm.operations() == ["daq"]
    destination = os.path.join(os.fspath((place.root / "dm").resolve()), "34ide", "data")
    rows = jobs(place.engine)
    assert len(rows) == 2
    for row in rows:
        document = read_live_document(row.run_directory or "")
        assert document.folder == os.path.join(destination, "scan_2")
        if document.source == "daq":
            assert document.daq is not None and document.daq.destination == destination
    assert not (data / "scan_2").exists()


@pytest.mark.parametrize(
    "typed",
    ["{root}/exp/analysis/scan_12", "{root}/exp/analysis/scan_12/", "{root}/exp//analysis/scan_12"],
    ids=["as mirrored", "a trailing slash", "a doubled slash"],
)
def test_a_session_on_a_mirrored_output_folder_makes_its_runs_in_it_and_gives_it_normalized(place, client, typed):
    """The Live page mirrors a Folder ``<root>/exp/data/scan_12`` into the Output Folder ``<root>/exp/analysis/scan_12``
    (``frontend/src/features/live/live-form.ts``, ``mirroredOutput``), which does not exist yet; typed with a trailing
    or a doubled slash too. Start makes it with the runs' folders in it, ``<output>/rec_<ID>`` and ``<output>/index_<ID>``,
    and the run pages (each run's ``output_path``), the job page's run directory and the session's Scan card (the next
    session's prefill) give it normalized. The clones keep the configured output template (``jobs.prefill``)."""
    (place.root / "exp" / "data" / "scan_12").mkdir(parents=True)
    output = typed.format(root=place.root)
    normalized = os.fspath(place.root / "exp" / "analysis" / "scan_12")
    form = place.form(scan={"folder": f"{place.root}/exp/data/scan_12", "output_folder": output})
    assert found(client.post(VALIDATE, json=form)) == set()
    response = start(client, form)

    assert response.status_code == 201, response.text
    wire, follower = (answer["run"]["id"] for answer in response.json()["runs"])
    recon = client.get(f"/api/v1/reconstruction-runs/{wire}").json()
    index = client.get(f"/api/v1/indexing-runs/{follower}").json()
    assert (recon["output_path"], index["output_path"]) == (
        f"{normalized}/rec_{wire}",
        f"{normalized}/index_{follower}",
    )
    assert sorted(os.listdir(normalized)) == [f"index_{follower}", f"rec_{wire}"]
    job = client.get(f"/api/v1/jobs/{recon['job_id']}").json()
    assert job["configuration"]["run_directory"]["path"] == f"{normalized}/rec_{wire}"
    assert client.get(SESSION).json()["session"]["scan"]["output_folder"] == normalized
    clone = client.get("/api/v1/programs/wire/prefill", params={"reconstruction": wire}).json()["values"]
    assert clone["output_path_template"] == programs.output_path_template("wire", place.settings, scan_number=1)


def test_an_output_folder_with_a_dot_dot_component_is_refused_and_start_creates_nothing(place, client):
    """``..`` (a mirror of ``…/data/../data/scan_12``) is refused on the Output Folder, as on any path from a request
    (``files.roots.resolve_inside_roots``): Validate's error, and Start's 422 with nothing created."""
    form = place.form(scan={"output_folder": f"{place.root}/exp/data/../analysis/scan_12"})
    assert found(client.post(VALIDATE, json=form)) == {(("scan", "output_folder"), "error", "path_not_allowed")}
    problem(start(client, form), 422)
    assert jobs(place.engine) == [] and not (place.root / "exp").exists()


def test_a_session_with_no_filename_template_takes_every_h5_file_its_runs_recording_none(place, client):
    """Filename Template is optional: blank, white space or null is no issue. Start gives each run ``filename_prefixes``
    ``[]`` in its ``request.json`` (the key kept, as every other) and ``live.json``'s ``filename_template`` null (the
    follower's too); the session's Scan card and the job page say null, and notify admits a numbered ``.h5`` file."""
    for blank in ("", "  ", None):
        assert found(client.post(VALIDATE, json=place.form(scan={"filename_template": blank}))) == set()
    response = start(client, place.form(scan={"filename_template": ""}))

    assert response.status_code == 201, response.text
    wire, follower = (answer["job_id"] for answer in response.json()["runs"])
    for row in jobs(place.engine):
        directory = Path(row.run_directory or "")
        request = json.loads((directory / REQUEST_FILENAME).read_text())["request"]
        assert "filename_prefixes" in request and request["filename_prefixes"] == []
        stored = json.loads((directory / "live.json").read_text())
        assert "filename_template" in stored and stored["filename_template"] is None
    assert client.get(SESSION).json()["session"]["scan"]["filename_template"] is None
    assert client.get(f"/api/v1/jobs/{wire}").json()["live"]["filename_template"] is None
    assert client.get(f"/api/v1/jobs/{follower}").json()["live"]["filename_template"] is None
    (place.folder / "Si_00012.h5").write_bytes(b"x")
    assert notify(place.engine, wire, place.folder / "Si_00012.h5").arrival.scan_point == 12


def test_a_missing_daq_folder_named_through_a_link_is_recorded_by_its_real_path(place, tmp_path, monkeypatch):
    """A DAQ Folder not there yet, named through a link inside the roots (``dm-link`` to ``dm``): its real path is
    resolved through its nearest existing folder, and ``live.json`` names that path, inside the DAQ's destination."""
    dm = FakeDMService(tmp_path / "dm", monkeypatch, workflow=False)
    dm.daqs = {DAQ_ID: daq(place.root / "dm")}
    settings = place.settings.model_copy(
        update={"live": place.settings.live.model_copy(update={"dm_setup_file": tmp_path / "dm-setup.sh"})}
    )
    (place.root / "dm-link").symlink_to(place.root / "dm")
    form = place.form(scan={"source": "daq", "daq_id": DAQ_ID, "folder": f"{place.root}/dm-link/34ide/data/scan_2"})
    with portal(settings) as client:
        response = start(client, form)

    assert response.status_code == 201, response.text
    destination = os.fspath((place.root / "dm" / "34ide" / "data").resolve())
    for row in jobs(place.engine):
        document = read_live_document(row.run_directory or "")
        assert document.folder == os.path.join(destination, "scan_2")
        if document.source == "daq":
            assert document.daq is not None and document.daq.destination == destination


def test_a_second_session_on_the_same_output_folder_starts_with_no_not_empty_warning(place, client):
    """The next session of an experiment takes the same Output Folder (the mirror of the same Folder, or the form
    filled from the last session): the earlier session's run folders give no warning, so the page's Start asks no
    confirmation; anything else in the folder still warns."""
    form = place.form()
    first = start(client, form)
    assert first.status_code == 201, first.text
    for answer in first.json()["runs"]:
        _end(place.engine, answer["job_id"], JobStatus.FINISHED)
    assert sorted(os.listdir(place.output)) == ["index_1", "rec_1"]

    assert found(client.post(VALIDATE, json=form)) == set()
    second = start(client, form, key="key-2")
    assert second.status_code == 201, second.text
    (place.output / "notes.txt").write_text("")
    assert found(client.post(VALIDATE, json=form)) == {(("scan", "output_folder"), "warning", "not_empty")}


def test_the_batch_forms_cloned_from_a_live_run_with_no_template_get_the_template_that_takes_its_files(place, client):
    """A live run with no template (``filename_prefixes`` ``[]``), cloned on a batch form or its reconstruction indexed
    from the LaueGo form, gets ``*%d.h5`` (``jobs.arrivals.NO_TEMPLATE``), never a blank Filename: the same files with
    the same numbers as the live run's rule (``tests/jobs/test_arrivals.py``). The wire clone validates with no issue
    on its Filename or its inputs."""
    response = start(client, place.form(scan={"filename_template": ""}))
    assert response.status_code == 201, response.text
    wire, follower = (answer["run"]["id"] for answer in response.json()["runs"])
    prefill = "/api/v1/programs/{}/prefill"
    clone = client.get(prefill.format("wire"), params={"reconstruction": wire}).json()["values"]
    indexed = client.get(prefill.format("lauego"), params={"reconstruction": wire}).json()["values"]
    follower_clone = client.get(prefill.format("lauego"), params={"indexing": follower}).json()["values"]
    assert clone["filename_prefixes"] == indexed["filename_prefixes"] == [NO_TEMPLATE]
    assert follower_clone["filename_prefixes"] == [NO_TEMPLATE]

    (place.folder / "Si_00012.h5").write_bytes(b"x")
    report = client.post("/api/v1/reconstruction-runs/validate", json={**clone, "scan_points": "12"})
    assert report.status_code == 200, report.text
    names = {issue["loc"][0] for issue in report.json()["issues"] if issue["loc"] and issue["severity"] == "error"}
    assert names.isdisjoint({"filename_prefixes", "input_path"}), report.json()["issues"]


def test_a_notify_runs_file_without_a_number_is_its_line_on_the_live_page_once(place, client):
    """With no template a ``.h5`` file without a number is not taken, and ``notify`` answers why (its one line). The
    run's arrival status (``arrival-status.json``) gets that line too, so the Live page and the job page show it: once,
    unchanged by the same file notified again or a numbered file admitted, replaced by another such file, and none
    once the run was stopped."""
    response = start(client, place.form(indexing=False, scan={"filename_template": ""}))
    (job_id,) = (answer["job_id"] for answer in response.json()["runs"])
    for name in ("Si_dark.h5", "Si_00012.h5", "Si_flat.h5", "Si_bright.h5"):
        (place.folder / name).write_bytes(b"x")

    def arrival() -> Any:
        return client.get(SESSION).json()["session"]["runs"][0]["arrival"]

    assert arrival() is None
    with pytest.raises(ArrivalRefused, match="'Si_dark.h5' has no number in its name"):
        notify(place.engine, job_id, place.folder / "Si_dark.h5")
    first = arrival()
    assert first["error"] == "Not taken: 'Si_dark.h5' has no number in its name"
    assert (first["state"], first["checked_at"], first["rows_seen"]) == ("available", None, None)
    assert first["error_at"] is not None
    assert client.get(f"/api/v1/jobs/{job_id}").json()["live"]["arrival"] == first
    status_file = Path(jobs(place.engine)[0].run_directory or "") / "arrival-status.json"
    written = os.stat(status_file)
    with pytest.raises(ArrivalRefused):
        notify(place.engine, job_id, place.folder / "Si_dark.h5")
    assert notify(place.engine, job_id, place.folder / "Si_00012.h5").admitted
    assert arrival() == first
    unchanged = os.stat(status_file)  # not replaced (a replace is a new inode), so error_at stays the first time
    assert (unchanged.st_ino, unchanged.st_mtime_ns) == (written.st_ino, written.st_mtime_ns)
    with pytest.raises(ArrivalRefused):
        notify(place.engine, job_id, place.folder / "Si_flat.h5")
    assert arrival()["error"] == "Not taken: 'Si_flat.h5' has no number in its name"
    assert dispatch.claim_run(place.engine, job_id)
    client.post(f"/api/v1/jobs/{job_id}/cancel")
    with pytest.raises(ArrivalRefused):
        notify(place.engine, job_id, place.folder / "Si_bright.h5")
    assert arrival()["error"] == "Not taken: 'Si_flat.h5' has no number in its name"


def test_a_notify_runs_file_without_a_number_after_the_runs_end_gives_no_line(place, client):
    """A run that ended without a Stop (here Failed) stays shown with its arrival status; a file without a number
    notified afterwards is refused (its one line) but never becomes the ended run's line."""
    response = start(client, place.form(indexing=False, scan={"filename_template": ""}))
    (job_id,) = (answer["job_id"] for answer in response.json()["runs"])
    assert dispatch.claim_run(place.engine, job_id)
    _fail(place.engine, job_id)
    (place.folder / "Si_dark.h5").write_bytes(b"x")
    with pytest.raises(ArrivalRefused, match="has no number in its name"):
        notify(place.engine, job_id, place.folder / "Si_dark.h5")
    session = client.get(SESSION).json()["session"]
    assert (session["open"], session["stopped"], session["runs"][0]["arrival"]) == (False, False, None)
    assert not (Path(jobs(place.engine)[0].run_directory or "") / "arrival-status.json").exists()


def test_start_makes_a_missing_output_folder_with_its_parents_and_the_runs_go_in_it(place, client):
    """A missing Output Folder is no issue: Start makes it, its parents too, inside the roots, with each run's
    folder in it."""
    output = place.root / "a" / "b" / "c"
    response = start(client, place.form(scan={"output_folder": os.fspath(output)}))

    assert response.status_code == 201, response.text
    directories = sorted(row.run_directory or "" for row in jobs(place.engine))
    assert directories == [os.fspath(output / "index_1"), os.fspath(output / "rec_1")]
    assert all(os.path.isdir(directory) for directory in directories)


# ---------------------------------------------------------------------------
# The checks


def _not_a_folder(place: Place) -> str:
    path = place.root / "a-file"
    path.write_text("")
    return os.fspath(path)


def _too_large(place: Place) -> str:
    """A file one byte over the calibration files' bound (sparse: its size alone refuses it)."""
    path = place.root / "big.xml"
    with path.open("wb") as handle:
        handle.truncate(MAX_CALIBRATION_BYTES + 1)
    return os.fspath(path)


def _not_empty(place: Place) -> str:
    """An earlier session's output folder that also holds a file of something else."""
    path = place.root / "full"
    path.mkdir()
    (path / "rec_1").mkdir()
    (path / "notes.txt").write_text("")
    return os.fspath(path)


def _earlier_runs(place: Place) -> str:
    """An output folder holding only earlier sessions' run folders (unique by ID: they cannot mix)."""
    path = place.root / "runs-only"
    for name in ("rec_1", "index_2", "rec_10"):
        (path / name).mkdir(parents=True)
    return os.fspath(path)


def _file_named_as_a_run_folder(place: Place) -> str:
    """An output folder holding a file named as a run folder (not a run's folder)."""
    path = place.root / "run-name-file"
    path.mkdir()
    (path / "rec_1").write_text("")
    return os.fspath(path)


def _folder_named_almost_as_a_run_folder(place: Place) -> str:
    """An output folder holding a folder named almost as a run folder (no ID)."""
    path = place.root / "run-name-folder"
    (path / "index_x").mkdir(parents=True)
    return os.fspath(path)


def _link_out(place: Place) -> str:
    """A link inside the root to a folder outside every root."""
    outside = place.root.parent / "outside"
    outside.mkdir()
    link = place.root / "out-link"
    link.symlink_to(outside)
    return os.fspath(link)


CASES: list[tuple[str, Any, set[tuple[tuple[str | int, ...], str, str]]]] = [
    ("the author left blank", {"scan": {"author": " "}}, {(("scan", "author"), "error", "missing")}),
    ("an unknown source", {"scan": {"source": "ftp"}}, {(("scan", "source"), "error", "literal_error")}),
    ("a DAQ source without its DAQ", {"scan": {"source": "daq"}}, {(("scan", "daq_id"), "error", "missing")}),
    ("a DAQ with notifications", {"scan": {"daq_id": DAQ_ID}}, {(("scan", "daq_id"), "error", "not_for_source")}),
    (
        "a DAQ ID too long",
        {"scan": {"source": "daq", "daq_id": "z" * 201}},
        {(("scan", "daq_id"), "error", "string_too_long")},
    ),
    (
        "a folder that is missing, for notifications",
        {"scan": {"folder": "{root}/missing"}},
        {(("scan", "folder"), "error", "not_found")},
    ),
    (
        "a folder that is missing, for a DAQ: the DAQ makes it",
        {"scan": {"source": "daq", "daq_id": DAQ_ID, "folder": "{root}/dm/34ide/data/scan_2"}},
        {(("scan", "folder"), "warning", "not_found"), (("scan", "daq_id"), "warning", "checked_at_start")},
    ),
    (
        "a missing folder, for a DAQ, through a link that leads out of the roots",
        {"scan": {"source": "daq", "daq_id": DAQ_ID, "folder": "{link}/scan_2"}},
        {(("scan", "folder"), "error", "path_not_allowed")},
    ),
    (
        "a folder that is a file, for a DAQ",
        {"scan": {"source": "daq", "daq_id": DAQ_ID, "folder": "{file}"}},
        {(("scan", "folder"), "error", "not_a_folder")},
    ),
    ("a folder that is a file", {"scan": {"folder": "{file}"}}, {(("scan", "folder"), "error", "not_a_folder")}),
    (
        "a folder outside the roots",
        {"scan": {"folder": "/etc"}},
        {(("scan", "folder"), "error", "path_not_allowed")},
    ),
    (
        "a template that is a path",
        {"scan": {"filename_template": "sub/wire_%d.h5"}},
        {(("scan", "filename_template"), "error", "not_a_file_name")},
    ),
    (
        "a template with two %d",
        {"scan": {"filename_template": "wire_%d_%d.h5"}},
        {(("scan", "filename_template"), "error", "placeholders")},
    ),
    (
        "an output folder that is a file",
        {"scan": {"output_folder": "{file}"}},
        {(("scan", "output_folder"), "error", "not_a_folder")},
    ),
    (
        "an output folder under a file",
        {"scan": {"output_folder": "{file}/out"}},
        {(("scan", "output_folder"), "error", "not_a_folder")},
    ),
    (
        "an output folder outside the roots",
        {"scan": {"output_folder": "/elsewhere"}},
        {(("scan", "output_folder"), "error", "path_not_allowed")},
    ),
    ("a missing output folder and its parents: Start makes them", {"scan": {"output_folder": "{root}/a/b/c"}}, set()),
    (
        "a missing output folder through a link that leads out of the roots",
        {"scan": {"output_folder": "{link}/out"}},
        {(("scan", "output_folder"), "error", "path_not_allowed")},
    ),
    (
        "an output folder that is not empty",
        {"scan": {"output_folder": "{full}"}},
        {(("scan", "output_folder"), "warning", "not_empty")},
    ),
    ("an output folder holding only earlier sessions' run folders", {"scan": {"output_folder": "{runs}"}}, set()),
    (
        "an output folder holding a file named as a run folder",
        {"scan": {"output_folder": "{named_file}"}},
        {(("scan", "output_folder"), "warning", "not_empty")},
    ),
    (
        "an output folder holding a folder named almost as a run folder",
        {"scan": {"output_folder": "{named_folder}"}},
        {(("scan", "output_folder"), "warning", "not_empty")},
    ),
    ("an output folder that is empty", {"scan": {"output_folder": "{empty}"}}, set()),
    ("a scan not found", {"scan": {"scan_number": 99}}, {(("scan", "scan_number"), "error", "source_not_found")}),
    ("a scan number not whole", {"scan": {"scan_number": "abc"}}, {(("scan", "scan_number"), "error", "int_parsing")}),
    ("no scan: unlinked", {"scan": {"scan_number": None}}, {(("scan", "scan_number"), "warning", "unlinked")}),
    (
        "an author too long, refused once for the pair",
        {"scan": {"author": "a" * 201}},
        {(("scan", "author"), "error", "string_too_long")},
    ),
    ("an unknown Scan field", {"scan": {"colour": "red"}}, {(("scan", "colour"), "error", "extra_forbidden")}),
    (
        "a program part sending a Scan card field",
        {"wire": {"input_path": "/x"}, "lauego": {"reconstruction_id": 1}},
        {
            (("reconstruction", "input_path"), "error", "set_by_scan"),
            (("indexing", "reconstruction_id"), "error", "set_by_scan"),
        },
    ),
    ("no run", {"reconstruction": False, "indexing": False}, {((), "error", "no_runs")}),
    (
        "depth indices for indexing alone",
        {"reconstruction": False, "lauego": {"depth_range": "0-2"}},
        {(("indexing", "depth_range"), "error", "depth_without_reconstruction")},
    ),
    ("depth indices for a follower", {"lauego": {"depth_range": "0-2"}}, set()),
    (
        "a wire geometry that does not load",
        {"wire": {"geometry_file": "{crystal}"}},
        {(("reconstruction", "geometry_file"), "error", "does_not_load")},
    ),
    (
        "an indexing crystal that does not load",
        {"lauego": {"crystal_file": "{geometry}"}},
        {(("indexing", "crystal_file"), "error", "does_not_load")},
    ),
    (
        "a wire geometry too large to read",
        {"wire": {"geometry_file": "{big}"}},
        {(("reconstruction", "geometry_file"), "error", "too_large")},
    ),
    (
        "an indexing crystal too large to read",
        {"lauego": {"crystal_file": "{big}"}},
        {(("indexing", "crystal_file"), "error", "too_large")},
    ),
    (
        "an indexing geometry not found",
        {"lauego": {"geometry_file": "{root}/missing.xml"}},
        {(("indexing", "geometry_file"), "error", "not_found")},
    ),
    (
        "wire depths out of order",
        {"wire": {"depth_start": 10.0, "depth_end": -10.0}},
        {
            (("reconstruction", "depth_start"), "error", "depth_order"),
            (("reconstruction", "depth_end"), "error", "depth_order"),
        },
    ),
    ("a field's own rule", {"lauego": {"box_size": 0}}, {(("indexing", "box_size"), "error", "greater_than")}),
]


@pytest.mark.parametrize(("change", "issues"), [case[1:] for case in CASES], ids=[case[0] for case in CASES])
def test_each_check_of_a_live_session_is_an_issue_on_its_field(place, client, change, issues):
    """The Live page's create checks and the batch form's that apply, each reported once on the field it is about, with
    its part (a run's issue on a field the Scan card sets on the Scan card's); the base form has none."""
    names = {"root": os.fspath(place.root), "file": _not_a_folder(place), "big": _too_large(place)}
    names |= {"geometry": os.fspath(place.geometry), "crystal": os.fspath(place.crystal)}
    (place.root / "empty").mkdir()
    names |= {"full": _not_empty(place), "link": _link_out(place), "empty": os.fspath(place.root / "empty")}
    names |= {"runs": _earlier_runs(place), "named_file": _file_named_as_a_run_folder(place)}
    names |= {"named_folder": _folder_named_almost_as_a_run_folder(place)}

    def filled(values: dict[str, Any]) -> dict[str, Any]:
        return {key: value.format(**names) if isinstance(value, str) else value for key, value in values.items()}

    form = place.form(
        reconstruction=change.get("reconstruction", True),
        indexing=change.get("indexing", True),
        scan=filled(change.get("scan", {})),
        wire=filled(change.get("wire", {})),
        lauego=filled(change.get("lauego", {})),
    )

    assert found(client.post(VALIDATE, json=place.form())) == set()
    reported = listed(client.post(VALIDATE, json=form))
    assert sorted(reported) == sorted(issues)  # each once: a Both session's run issue on a Scan field is not repeated
    assert jobs(place.engine) == []  # a validation creates nothing


def test_a_calibration_file_that_grows_after_its_stat_is_refused_unread(place, client, monkeypatch):
    """The 1 MiB bound holds on the read, not only on the stat: a geometry and a crystal rewritten past the bound right
    after ``check_file`` stat'ed them are ``too_large`` by their descriptor's stat, and, when that stat understates them
    too (a pseudo file, NFS's attribute cache), by the read itself; lauelab never sees them."""
    from laueportal.science import calibration

    geometry, crystal = place.root / "grows-geometry.xml", place.root / "grows-crystal.xml"
    stat_file = live_session.check_file

    def grown_after_the_stat(issues, settings, checked, name):
        status = stat_file(issues, settings, checked, name)
        if status is not None and name in ("geometry_file", "crystal_file"):
            path = Path(getattr(checked.values, name))
            path.write_bytes(path.read_bytes() + b" " * (MAX_CALIBRATION_BYTES + 1))
        return status

    loaded: list[str] = []
    for name in ("Geometry", "load_crystal"):
        real = getattr(calibration, name)
        monkeypatch.setattr(calibration, name, lambda path, *args, real=real: loaded.append(path) or real(path, *args))
    monkeypatch.setattr(live_session, "check_file", grown_after_the_stat)
    form = place.form(reconstruction=False, lauego={"geometry_file": str(geometry), "crystal_file": str(crystal)})

    def reported() -> dict[tuple[tuple[str | int, ...], str], str]:
        shutil.copy(place.geometry, geometry)
        shutil.copy(place.crystal, crystal)
        return {(tuple(issue["loc"]), issue["code"]): issue["message"] for issue in post_issues(client, form)}

    def refused(message: str) -> dict[tuple[tuple[str | int, ...], str], str]:
        return {(("indexing", name), "too_large"): message for name in ("geometry_file", "crystal_file")}

    assert reported() == refused("Is 1.1 MiB: a calibration file is at most 1 MiB")

    class UnderstatingOs:
        """``os`` for ``science.calibration`` alone, whose descriptor's stat says each file is still 2.3 KB."""

        def __getattr__(self, name: str) -> Any:
            return getattr(os, name)

        @staticmethod
        def fstat(descriptor: int) -> os.stat_result:
            values = list(os.fstat(descriptor))
            values[6] = 2300  # st_size
            return os.stat_result(values)

    monkeypatch.setattr(calibration, "os", UnderstatingOs())
    assert reported() == refused("Is over 1 MiB: a calibration file is at most 1 MiB")
    assert loaded == []


def post_issues(client: TestClient, form: Any) -> list[dict[str, Any]]:
    response = client.post(VALIDATE, json=form)
    assert response.status_code == 200, response.text
    return response.json()["issues"]


def test_an_invalid_session_is_a_422_problem_with_its_issues_and_its_key_can_be_sent_again(place, client):
    """An error stops Start: a 422 problem with every issue, errors first, nothing created; the refused request keeps
    nothing with its key, so the corrected form sends it again."""
    refused = start(client, place.form(scan={"author": "", "scan_number": None}))

    body = problem(refused, 422)
    assert (body["type"], body["title"], body["detail"]) == ("about:blank", "Validation failed", "2 problems")
    assert [(tuple(item["loc"]), item["severity"], item["code"]) for item in body["errors"]] == [
        (("scan", "author"), "error", "missing"),
        (("scan", "scan_number"), "warning", "unlinked"),
    ]
    assert all(item["message"] for item in body["errors"])
    assert jobs(place.engine) == []
    assert start(client, place.form()).status_code == 201


# ---------------------------------------------------------------------------
# The Idempotency-Key, one session at a time, the lock


def test_the_same_key_answers_the_same_session_and_another_body_is_a_409(place, client):
    """The ``Idempotency-Key`` as the batch creates use it: the same key with the same body answers the same runs again
    (nothing new); with another body after the session was started, a 409 ``key-created`` whose ``created`` is the kept
    answer."""
    first = start(client, place.form())
    again = start(client, place.form())
    other = start(client, place.form(lauego={"notes": "edited"}))

    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and len(jobs(place.engine)) == 2
    body = problem(other, 409)
    assert body["type"] == "urn:laueportal:problem:key-created"
    assert body["created"] == first.json()
    assert "R1 (job 1), I1 (job 2)" in body["detail"]


def test_a_start_while_a_live_run_is_open_is_a_409_naming_it_and_creates_nothing(place, client):
    """One live session at a time: while a live run is Queued or Running, Start is a 409 ``live-session-open`` naming
    the oldest open run, nothing created and its key released; once the session is stopped (Stop while Queued cancels at
    once) the same key starts one."""
    first = start(client, place.form()).json()
    refused = start(client, place.form(indexing=False), key="key-2")

    body = problem(refused, 409)
    assert body["type"] == "urn:laueportal:problem:live-session-open"
    assert body["open_run"] == {
        "job_id": 1,
        "run": {"kind": "reconstruction", "id": 1, "label": "R1"},
        "status": "queued",
    }
    assert body["detail"].startswith("R1 (job 1) is Queued: one live session at a time")
    assert len(jobs(place.engine)) == 2
    for run in first["runs"]:
        assert client.post(f"/api/v1/jobs/{run['job_id']}/cancel").json()["state"] == "cancelled"
    assert start(client, place.form(indexing=False), key="key-2").status_code == 201


@pytest.mark.parametrize("error", [WorkflowValidationError("refused"), RuntimeError("lost")], ids=["refused", "lost"])
def test_a_session_whose_follower_is_not_created_queues_nothing_and_its_wire_run_is_failed(place, monkeypatch, error):
    """All or nothing: the wire run exists when its follower's creation fails, so none is queued and the wire run is
    Failed with the reason, never left to run alone. A refusal answers the 201 with the run's ``failure`` (kept with
    the key); an unexpected error is a 500 that keeps the key claimed (a retry is its 409, never a second session)."""

    def fails(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(creation, "create_live_indexing", fails)
    with TestClient(create_app(place.settings), headers=PORTAL_HEADERS, raise_server_exceptions=False) as client:
        first = start(client, place.form())
        again = start(client, place.form())

    (wire,) = jobs(place.engine)
    assert (wire.status, wire.phase) == (JobStatus.FAILED, RunPhase.FAILED)
    assert dispatch_queue(place.engine) == []
    if isinstance(error, WorkflowValidationError):
        assert first.status_code == 201 and again.json() == first.json()
        (answer,) = first.json()["runs"]
        assert answer["status"] == "failed" and "the session's other run was not created: refused" in answer["failure"]
    else:
        assert (first.status_code, problem(again, 409)["type"]) == (500, "about:blank")


def test_two_starts_racing_make_one_session_and_one_409(place, client, monkeypatch):
    """Real concurrent requests: two Starts at once, each creation slowed so both would pass the one-session check
    without Start's lock (it sees only queued runs); with it, one session is created and the other Start is the 409
    naming its wire run. Never two sessions."""
    slow = creation.create_live_reconstruction

    def slowly(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.5)
        return slow(*args, **kwargs)

    monkeypatch.setattr(creation, "create_live_reconstruction", slowly)
    barrier = threading.Barrier(2)
    responses: dict[str, httpx.Response] = {}

    def post(key: str) -> None:
        barrier.wait()
        responses[key] = start(client, place.form(), key=key)

    threads = [threading.Thread(target=post, args=(key,)) for key in ("key-a", "key-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(response.status_code for response in responses.values()) == [201, 409]
    created = next(response.json() for response in responses.values() if response.status_code == 201)
    refused = next(response.json() for response in responses.values() if response.status_code == 409)
    assert refused["open_run"]["job_id"] == created["runs"][0]["job_id"]
    assert len(jobs(place.engine)) == 2  # one Both session


def test_two_starts_in_two_api_processes_never_queue_two_sessions(place, monkeypatch):
    """One live session at a time, held by the database: Start's lock is one api process's, so two processes (here two
    apps, each with its own engine and lock) can both pass the one-session check (held here until both have); the
    queueing checks again in its write transaction (``jobs.queue.enqueue_live_runs``), so one session is queued and the
    other's runs end Failed, as any queueing refusal leaves them."""
    check = live_session._refuse_an_open_session
    both_checked = threading.Barrier(2)

    def checked(engine: Engine) -> None:
        check(engine)
        both_checked.wait(10)

    monkeypatch.setattr(live_session, "_refuse_an_open_session", checked)
    responses: list[httpx.Response] = []
    with portal(place.settings) as one, portal(place.settings) as two:
        threads = [
            threading.Thread(target=lambda client=client, key=key: responses.append(start(client, place.form(), key)))
            for client, key in ((one, "key-a"), (two, "key-b"))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

    assert [response.status_code for response in responses] == [201, 201]
    sessions = [response.json()["runs"] for response in responses]
    queued = [runs for runs in sessions if all(run["failure"] is None for run in runs)]
    refused = [runs for runs in sessions if all("one live session at a time" in (run["failure"] or "") for run in runs)]
    assert (len(queued), len(refused)) == (1, 1)
    statuses = sorted(int(job.status) for job in jobs(place.engine))
    assert statuses == sorted([int(JobStatus.QUEUED)] * 2 + [int(JobStatus.FAILED)] * 2)


def test_a_both_sessions_runs_are_queued_together_so_the_dispatcher_never_claims_the_wire_run_alone(
    place, client, monkeypatch, tmp_path
):
    """The real dispatcher cycling while Start creates the session, the follower's creation slowed: the wire run is
    claimed only once its follower is queued, with the pair's cores (8 cores: 4 + 2), never alone with the whole
    live share (6; ``dispatch.live_cores``)."""
    slow = creation.create_live_indexing

    def slowly(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.5)
        return slow(*args, **kwargs)

    monkeypatch.setattr(creation, "create_live_indexing", slowly)
    fake = FakeJobs(tmp_path, RUNS_ON)
    policy = DispatchPolicy(dispatch_seconds=0.02, heartbeat_seconds=0.05, live=LivePolicy(cpu_count=8))
    dispatcher = Dispatcher(place.engine, policy, start=fake)
    stop = threading.Event()

    def cycle() -> None:
        while not stop.is_set():
            dispatcher.cycle()
            time.sleep(0.01)

    cycling = threading.Thread(target=cycle)
    cycling.start()
    try:
        response = start(client, place.form())
        assert response.status_code == 201, response.text
        assert wait_for(lambda: len(fake.started) == 2, 10)
    finally:
        stop.set()
        cycling.join(10)
        fake.cleanup()

    wire, follower = (run["job_id"] for run in response.json()["runs"])
    assert [key for _, key in fake.started] == [wire, follower]
    assert fake.cores == {wire: 4, follower: 2}
    assert dispatch.live_cores(policy.live, wire=True, paired=False) == 6  # what alone would have taken


# ---------------------------------------------------------------------------
# DM: the DAQ list and the bind check


def test_the_daq_list_is_one_answer_at_most_one_dm_call_per_daq_list_seconds_and_none_unless_asked(
    place, tmp_path, monkeypatch
):
    """No Live page form asking, no DM call (the app's start, other routes, a validation of a notification session);
    asked, one call per ``daq_list_seconds`` whatever the requests, each DAQ with where it writes (the folder a DAQ
    source fills in)."""
    dm = FakeDMService(tmp_path / "dm", monkeypatch, workflow=False)
    dm.daqs = {DAQ_ID: daq(place.root / "dm")}
    live = place.settings.live.model_copy(update={"dm_setup_file": tmp_path / "dm-setup.sh", "daq_list_seconds": 1.0})
    with portal(place.settings.model_copy(update={"live": live})) as client:
        client.get("/api/v1/programs")
        assert found(client.post(VALIDATE, json=place.form())) == set()
        time.sleep(0.2)
        unasked = list(dm.runs)
        answers = [client.get(DAQS) for _ in range(3)]
        time.sleep(1.1)
        later = client.get(DAQS)

    assert unasked == []
    assert dm.operations() == ["daqs", "daqs"]
    assert [answer.status_code for answer in answers] == [200] * 3
    # One answer (its DAQs, failures and time) for every request; the time it is still kept counts down.
    shared = [{key: value for key, value in answer.json().items() if key != "kept_seconds"} for answer in answers]
    assert shared[1] == shared[0] == shared[2]
    kept = [answer.json()["kept_seconds"] for answer in answers]
    assert 1.0 >= kept[0] >= kept[1] >= kept[2] >= 0
    assert later.json()["checked_at"] != answers[0].json()["checked_at"]
    (entry,) = answers[0].json()["daqs"]
    assert entry == {
        "id": DAQ_ID,
        "experiment": "e1",
        "status": "Running",
        "data_directory": "/detector/e1",
        "storage_directory": os.fspath(place.root / "dm"),
        "dest_directory": "34ide/data",
        "destination": os.fspath(place.root / "dm" / "34ide" / "data"),
    }
    assert answers[0].json()["failed"] == [] and answers[0].headers["cache-control"] == "no-store"


def test_the_daq_list_leaves_out_the_daqs_daq_name_exclude_names_and_validate_and_start_still_take_one(
    place, tmp_path, monkeypatch
):
    """``[live] daq_name_exclude`` (``logs``, ``current``) leaves out of ``GET /live/daqs`` a DAQ whose option on the
    Live page shows one, ignoring case: its experiment name, or its data directory's last two names; a name further up
    the data directory never counts (the option does not show it). ``[]`` lists them all. The kept answer is
    unchanged: Validate checks an excluded DAQ against it as any other (no warning that Start checks it), and Start
    binds it."""
    dm = FakeDMService(tmp_path / "dm", monkeypatch, workflow=False)
    storage = place.root / "dm"
    dm.daqs = {
        DAQ_ID: daq(storage),
        "l1": daq(storage, id="l1", experimentName="Beamline-LOGS"),
        "c1": daq(storage, id="c1", dataDirectory="/detector/Current/e1"),
        "k1": daq(storage, id="k1", experimentName="e2", dataDirectory="/detector/e2/raw"),
        "k2": daq(storage, id="k2", experimentName="e3", dataDirectory="/net/current/logs-archive/e3/raw"),
    }
    live = place.settings.live.model_copy(update={"dm_setup_file": tmp_path / "dm-setup.sh"})
    assert live.daq_name_exclude == ["logs", "current"]
    with portal(place.settings.model_copy(update={"live": live})) as client:
        listed_ids = [entry["id"] for entry in client.get(DAQS).json()["daqs"]]
        form = place.form(scan={"source": "daq", "daq_id": "l1"})
        validated = found(client.post(VALIDATE, json=form))
        started = start(client, form)
    every = live.model_copy(update={"daq_name_exclude": []})
    with portal(place.settings.model_copy(update={"live": every})) as client:
        every_ids = [entry["id"] for entry in client.get(DAQS).json()["daqs"]]

    assert sorted(listed_ids) == sorted([DAQ_ID, "k1", "k2"])
    assert sorted(every_ids) == sorted([DAQ_ID, "l1", "c1", "k1", "k2"])
    assert validated == set()
    assert started.status_code == 201, started.text
    assert dm.operations() == ["daqs", "daq", "daqs"]


def test_a_daq_list_dm_cannot_give_is_a_503_problem_with_its_reason(place, client, tmp_path, monkeypatch):
    """DM not configured (no ``dm_setup_file``: no call at all): a 503 ``dm-unavailable`` with the reason and
    ``Retry-After`` (the next call's earliest time)."""
    dm = FakeDMService(tmp_path / "dm", monkeypatch, workflow=False)
    response = client.get(DAQS)

    body = problem(response, 503)
    assert (body["type"], body["detail"]) == (
        "urn:laueportal:problem:dm-unavailable",
        "DM is not configured for live runs ([live] dm_setup_file)",
    )
    assert response.headers["retry-after"] == "30" and dm.runs == []


def test_a_failed_daq_lists_retry_after_is_the_time_its_failure_is_kept(place, tmp_path, monkeypatch):
    """A failing DM is asked less often (each failure in a row kept twice as long), so ``Retry-After`` says how long the
    failure is kept: a client that obeys it gets a new call, not the same kept failure."""
    live = place.settings.live.model_copy(update={"daq_list_seconds": 1.0})
    with portal(place.settings.model_copy(update={"live": live})) as client:
        first = client.get(DAQS)
        time.sleep(1.1)
        second = client.get(DAQS)

    assert (problem(first, 503)["type"], problem(second, 503)["type"]) == ("urn:laueportal:problem:dm-unavailable",) * 2
    assert [first.headers["retry-after"], second.headers["retry-after"]] == ["1", "2"]


def test_the_bind_checks_refusal_is_a_422_and_dms_silence_a_503_each_creating_nothing(place, tmp_path, monkeypatch):
    """The bind check at Start (``jobs.daq.bind_daq``): a DAQ that is not running (or a folder outside where it
    writes, or a DAQ DM does not know, named shortened) is refused with its reason, a 422 on ``["scan", "daq_id"]``;
    DM not answering is a 503 ``dm-unavailable``
    with ``Retry-After`` (Validate makes no call: its note that Start checks the DAQ); either way nothing is created
    and the key is released."""
    dm = FakeDMService(tmp_path / "dm", monkeypatch, workflow=False)
    dm.daqs = {DAQ_ID: daq(place.root / "dm", status="Stopped")}
    settings = place.settings.model_copy(
        update={"live": place.settings.live.model_copy(update={"dm_setup_file": tmp_path / "dm-setup.sh"})}
    )
    form = place.form(scan={"source": "daq", "daq_id": DAQ_ID})
    with portal(settings) as client:
        refused = start(client, form)
        dm.daqs[DAQ_ID]["status"] = "Running"
        outside = start(client, place.form(scan={"source": "daq", "daq_id": DAQ_ID, "folder": os.fspath(place.root)}))
        unknown = start(client, place.form(scan={"source": "daq", "daq_id": "z" * 200}))
        dm.failing = [OSError("connection refused")]
        silent = start(client, form)
        validated = found(client.post(VALIDATE, json=form))
        created = start(client, form)

    body = problem(refused, 422)
    assert [(item["loc"], item["code"]) for item in body["errors"]] == [(["scan", "daq_id"], "daq_refused")]
    assert "is not running (Stopped)" in body["errors"][0]["message"]
    outside_issue = problem(outside, 422)["errors"]
    assert [item["code"] for item in outside_issue] == ["daq_refused"]
    assert outside_issue[0]["message"].startswith(f"The folder must lie inside DAQ {DAQ_ID}'s destination")
    (unknown_issue,) = problem(unknown, 422)["errors"]
    assert unknown_issue["code"] == "daq_refused"
    assert unknown_issue["message"] == f"DM has no DAQ {quoted('z' * 200)}: select a running DAQ"
    assert "z" * 100 not in unknown_issue["message"]
    silence = problem(silent, 503)
    assert silence["type"] == "urn:laueportal:problem:dm-unavailable" and silent.headers["retry-after"] == "10"
    assert validated == {(("scan", "daq_id"), "warning", "checked_at_start")}
    assert created.status_code == 201, created.text  # the same key, released each time
    assert dm.operations() == ["daq"] * 5  # one call per Start, none for the Validate


def test_any_number_of_validates_make_no_dm_call_and_one_start_makes_one(place, tmp_path, monkeypatch):
    """DM's DAQ info is asked once per live create: the form validates as it is edited, so Validate never asks DM.
    Before the Live page's DAQ list was asked, a DAQ session's Validate notes that Start checks the DAQ; once the list
    is kept, it checks the folder against the DAQ's destination as the list saw it (outside: the bind check's refusal,
    still no call). Start makes the one call."""
    dm = FakeDMService(tmp_path / "dm", monkeypatch, workflow=False)
    dm.daqs = {DAQ_ID: daq(place.root / "dm")}
    settings = place.settings.model_copy(
        update={"live": place.settings.live.model_copy(update={"dm_setup_file": tmp_path / "dm-setup.sh"})}
    )
    form = place.form(scan={"source": "daq", "daq_id": DAQ_ID})
    outside = place.form(scan={"source": "daq", "daq_id": DAQ_ID, "folder": os.fspath(place.root)})
    with portal(settings) as client:
        unlisted = [found(client.post(VALIDATE, json=form)) for _ in range(5)]
        unasked = dm.operations()
        assert client.get(DAQS).status_code == 200
        listed = found(client.post(VALIDATE, json=form))
        refused = client.post(VALIDATE, json=outside).json()["issues"]
        before_start = dm.operations()
        created = start(client, form)

    assert unasked == []
    assert unlisted == [{(("scan", "daq_id"), "warning", "checked_at_start")}] * 5
    assert listed == set()
    assert [(issue["loc"], issue["severity"], issue["code"]) for issue in refused] == [
        (["scan", "daq_id"], "error", "daq_refused")
    ]
    assert refused[0]["message"].startswith(f"The folder must lie inside DAQ {DAQ_ID}'s destination")
    assert before_start == ["daqs"]
    assert created.status_code == 201, created.text
    assert dm.operations() == ["daqs", "daq"]


def test_the_bind_check_runs_off_the_event_loop(place, tmp_path, monkeypatch):
    """Start's DM call (the bind check, up to ``dm_timeout_seconds``) runs in a worker thread: while it waits, the API
    answers other requests."""
    entered, release = threading.Event(), threading.Event()
    record = DaqRecord(DAQ_ID, "e1", "Running", "/detector/e1", os.fspath(place.root / "dm"), "34ide/data")

    def held(client: DMClient, daq_id: str) -> DaqRecord:
        entered.set()
        assert release.wait(30)
        return record

    monkeypatch.setattr(DMClient, "daq", held)
    settings = place.settings.model_copy(
        update={"live": place.settings.live.model_copy(update={"dm_setup_file": tmp_path / "dm-setup.sh"})}
    )
    created: list[httpx.Response] = []
    health: list[httpx.Response] = []
    with portal(settings) as client:
        starting = threading.Thread(
            target=lambda: created.append(start(client, place.form(scan={"source": "daq", "daq_id": DAQ_ID})))
        )
        starting.start()
        try:
            assert entered.wait(10)
            asking = threading.Thread(target=lambda: health.append(client.get("/api/v1/health")))
            asking.start()
            asking.join(10)
            answered_while_held = [response.status_code for response in health]
        finally:
            release.set()
            starting.join(30)

    assert answered_while_held == [200]
    assert [response.status_code for response in created] == [201]


# ---------------------------------------------------------------------------
# Stop: the job API's cancel on the session's runs


def test_stop_is_the_job_apis_cancel_on_the_source_run_and_a_follower_can_be_stopped_alone(place, client):
    """No new route: ``POST /jobs/{id}/cancel``. On a Running live run it is ``requested`` (the stop flag: the executor
    seals it and drains it, ending Finished); on a Queued one it cancels at once, as on a follower stopped alone; the
    session's other run is untouched."""
    wire, follower = (run["job_id"] for run in start(client, place.form()).json()["runs"])
    assert dispatch.claim_run(place.engine, wire)  # the dispatcher took the wire run

    stopped = client.post(f"/api/v1/jobs/{wire}/cancel").json()
    wire_row = load_job(place.engine, wire)
    alone = client.post(f"/api/v1/jobs/{follower}/cancel").json()

    assert (stopped["state"], wire_row.status, wire_row.cancel_requested_at is not None) == (
        "requested",
        JobStatus.RUNNING,
        True,
    )
    assert alone["state"] == "cancelled" and load_job(place.engine, follower).status == JobStatus.CANCELLED
    assert load_job(place.engine, wire).status == JobStatus.RUNNING


# ---------------------------------------------------------------------------
# GET /live/session: the Live page's session


def test_the_live_session_is_the_latest_starts_runs_with_their_counters_arrival_status_and_scan_card(place, client):
    """None before the first Start; then the latest session's runs, the wire run first (Both: its follower after it),
    each with its job row's status, counters and stop, and its ``arrival-status.json`` once there is one (a
    ``notify`` run has none); ``scan`` its Scan card as recorded (``live.json``: source, folder's real path,
    template; its parameters: the output folder, scan, author); ``open`` until every run has ended."""
    assert client.get(SESSION).json() == {"session": None}
    wire, follower = (run["job_id"] for run in start(client, place.form()).json()["runs"])
    first = client.get(SESSION)

    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    session = first.json()["session"]
    assert session["open"] is True
    assert [(run["job_id"], run["run"]["label"], run["status"]) for run in session["runs"]] == [
        (wire, "R1", "queued"),
        (follower, "I1", "queued"),
    ]
    assert {
        (run["n_inputs"], run["n_succeeded"], run["n_failed"], run["stop_requested"]) for run in session["runs"]
    } == {(0, 0, 0, False)}
    assert [run["arrival"] for run in session["runs"]] == [None, None]
    assert session["scan"] == place.scan(
        folder=os.fspath(place.folder.resolve()), output_folder=os.fspath(place.output)
    )

    with Session(place.engine) as session_rows:
        wire_job = session_rows.get(Job, wire)
        assert wire_job is not None
        wire_dir = Path(wire_job.run_directory or "")
        session_rows.execute(update(Job).where(Job.job_id == wire).values(n_inputs=3, n_succeeded=2, n_failed=1))
        session_rows.commit()
    status = {
        "checked_at": "2026-10-27T10:00:00Z",
        "rows_seen": 4,
        "error": "1 catalog row(s) not used: x",
        "error_at": "2026-10-27T09:59:00Z",
    }
    (wire_dir / "arrival-status.json").write_text(json.dumps(status))
    assert dispatch.claim_run(place.engine, wire)
    client.post(f"/api/v1/jobs/{wire}/cancel")
    running = client.get(SESSION).json()["session"]["runs"][0]

    assert (running["status"], running["n_inputs"], running["n_succeeded"], running["n_failed"]) == ("running", 3, 2, 1)
    assert running["stop_requested"] is True
    assert running["arrival"] == {"state": "available", **status}

    for job_id in (wire, follower):
        client.post(f"/api/v1/jobs/{job_id}/cancel")
    with Session(place.engine) as session_rows:
        session_rows.execute(update(Job).where(Job.job_id == wire).values(status=int(JobStatus.FINISHED)))
        session_rows.commit()
    ended = client.get(SESSION).json()["session"]
    assert ended["open"] is False
    assert [run["status"] for run in ended["runs"]] == ["finished", "cancelled"]
    assert ended["runs"][0]["stop_requested"] is False


def _end(engine: Engine, job_id: int, status: JobStatus) -> None:
    with Session(engine) as session_rows:
        session_rows.execute(update(Job).where(Job.job_id == job_id).values(status=int(status)))
        session_rows.commit()


def test_a_session_a_user_stopped_says_so_once_ended_and_one_that_ended_without_a_stop_does_not(place, client):
    """``stopped``: the session's first run's stop flag, set by a user's Stop (or Stop now) and kept once it has
    ended, so the Live page shows no session then; the answer still has its runs and Scan card (the page fills the
    next form from them). A session whose run failed without a Stop, or whose follower alone was stopped, is not
    ``stopped``: it stays shown."""
    wire, follower = (run["job_id"] for run in start(client, place.form()).json()["runs"])
    assert client.get(SESSION).json()["session"]["stopped"] is False
    assert dispatch.claim_run(place.engine, wire)
    client.post(f"/api/v1/jobs/{wire}/cancel")
    stopping = client.get(SESSION).json()["session"]
    assert (stopping["open"], stopping["stopped"]) == (True, True)
    _end(place.engine, wire, JobStatus.FINISHED)
    _end(place.engine, follower, JobStatus.FINISHED)
    ended = client.get(SESSION).json()["session"]
    assert (ended["open"], ended["stopped"]) == (False, True)
    assert [run["job_id"] for run in ended["runs"]] == [wire, follower] and ended["scan"] is not None

    # Stop now on a Running run not stopped yet stops it as Stop does; it then ends Failed: still a Stop.
    (now,) = (run["job_id"] for run in start(client, place.form(indexing=False), key="key-2").json()["runs"])
    assert dispatch.claim_run(place.engine, now)
    assert client.post(f"/api/v1/jobs/{now}/stop-now").json()["state"] == "requested"
    _end(place.engine, now, JobStatus.FAILED)
    assert client.get(SESSION).json()["session"]["stopped"] is True

    # A run that failed by itself, its follower stopped alone: not a stopped session.
    wire, follower = (run["job_id"] for run in start(client, place.form(), key="key-3").json()["runs"])
    client.post(f"/api/v1/jobs/{follower}/cancel")
    _end(place.engine, wire, JobStatus.FAILED)
    failed = client.get(SESSION).json()["session"]
    assert (failed["open"], failed["stopped"]) == (False, False)
    assert [run["status"] for run in failed["runs"]] == ["failed", "cancelled"]


def _fail(engine: Engine, job_id: int, *, phase: RunPhase | None = None, n_failed: int = 0) -> None:
    """End the Running ``job_id`` Failed, as the executor or the reconciler does (``phase``: ``interrupted`` for a
    worker stop), with ``n_failed`` failed files."""
    with Session(engine) as session_rows, session_rows.begin():
        job = session_rows.get(Job, job_id)
        assert job is not None
        job.n_inputs, job.n_failed = max(job.n_inputs, n_failed), n_failed
        state.finalize(job, status=JobStatus.FAILED, phase=phase)


def test_a_run_ended_by_its_stop_now_is_stopped_now_and_no_failure_of_its_own_is(place, client):
    """``stopped_now``: a run ended by its Stop now (the second press, on a run whose stop was asked: the job's
    messages hold "Stop now requested", and the dispatcher ends it Failed, ``interrupted``). Not: a file failed in the
    drain, the worker stopped during it (``interrupted``, no Stop now), a Stop now the drain's own failure overtook, a
    follower's run-level failure. The Live page hides the first session once it has ended and keeps the others shown."""

    def session_runs() -> list[tuple[str, bool]]:
        return [(run["status"], run["stopped_now"]) for run in client.get(SESSION).json()["session"]["runs"]]

    def stopped_wire_run(key: str) -> int:
        (job_id,) = (run["job_id"] for run in start(client, place.form(indexing=False), key=key).json()["runs"])
        assert dispatch.claim_run(place.engine, job_id)
        assert client.post(f"/api/v1/jobs/{job_id}/cancel").json()["state"] == "requested"
        assert session_runs() == [("running", False)]
        return job_id

    killed = stopped_wire_run("key-1")
    assert client.post(f"/api/v1/jobs/{killed}/stop-now").json()["state"] == "stopping_now"
    assert dispatch.end_stopped_now(place.engine, killed, pid=12345)
    assert session_runs() == [("failed", True)]
    assert client.get(SESSION).json()["session"]["stopped"] is True

    drain_failed = stopped_wire_run("key-2")
    _fail(place.engine, drain_failed, n_failed=1)
    assert session_runs() == [("failed", False)]

    worker_stopped = stopped_wire_run("key-3")
    _fail(place.engine, worker_stopped, phase=RunPhase.INTERRUPTED)
    assert session_runs() == [("failed", False)]

    overtaken = stopped_wire_run("key-4")
    assert client.post(f"/api/v1/jobs/{overtaken}/stop-now").json()["state"] == "stopping_now"
    _fail(place.engine, overtaken, n_failed=1)
    assert not dispatch.end_stopped_now(place.engine, overtaken, pid=12345)
    assert session_runs() == [("failed", False)]

    wire, follower = (run["job_id"] for run in start(client, place.form(), key="key-5").json()["runs"])
    assert dispatch.claim_run(place.engine, follower)
    client.post(f"/api/v1/jobs/{wire}/cancel")
    _fail(place.engine, follower)  # a run-level error: no failed file
    _end(place.engine, wire, JobStatus.CANCELLED)
    assert session_runs() == [("cancelled", False), ("failed", False)]


def test_an_ended_row_with_the_live_queue_identity_but_no_live_json_is_no_session(place, client):
    """``jobs.live``'s rule for ended rows (``is_live_run``): the queue identity and ``live.json`` (a v1 ``cb1159c``
    row's identity may match). Without it, the newest row is no session, and a follower's wire run is left out of the
    follower's; an open row needs the identity alone."""
    wire, follower = (run["job_id"] for run in start(client, place.form()).json()["runs"])
    with Session(place.engine) as session_rows:
        directories = {
            job_id: Path(job.run_directory or "")
            for job_id in (wire, follower)
            if (job := session_rows.get(Job, job_id)) is not None
        }
    for directory in directories.values():
        (directory / "live.json").unlink()
    assert [run["job_id"] for run in client.get(SESSION).json()["session"]["runs"]] == [wire, follower]

    with Session(place.engine) as session_rows:
        session_rows.execute(update(Job).where(Job.job_id == wire).values(status=int(JobStatus.FINISHED)))
        session_rows.commit()
    alone = client.get(SESSION).json()["session"]
    assert ([run["job_id"] for run in alone["runs"]], alone["open"], alone["scan"]) == ([follower], True, None)

    with Session(place.engine) as session_rows:
        session_rows.execute(update(Job).where(Job.job_id == follower).values(status=int(JobStatus.FINISHED)))
        session_rows.commit()
    assert client.get(SESSION).json() == {"session": None}


# ---------------------------------------------------------------------------
# GET /jobs/{id}: a live run's block on the job page


def test_the_job_page_of_a_live_run_shows_its_source_arrivals_and_arrival_status(place, client):
    """``live``: the source from ``live.json`` (``notify`` with its folder and template; a follower's ``run``, the wire
    run it follows and that run's job), the arrivals (the whole lines of ``arrivals.jsonl``: a last line still being
    written is not counted) and ``arrival-status.json`` (a follower's waiting reason); null for a batch run and for an
    ended row whose run directory has no ``live.json`` (``is_live_run``)."""
    wire, follower = (run["job_id"] for run in start(client, place.form()).json()["runs"])
    with Session(place.engine) as rows:
        directories = {
            job_id: Path(job.run_directory or "") for job_id in (wire, follower) if (job := rows.get(Job, job_id))
        }

    first = client.get(f"/api/v1/jobs/{wire}").json()["live"]
    assert first == {
        "source": "notify",
        "source_state": "available",
        "daq_id": None,
        "folder": os.fspath(place.folder.resolve()),
        "filename_template": "wire_%d.h5",
        "follows": None,
        "follows_job_id": None,
        "arrivals": 0,
        "arrivals_state": "available",
        "arrival": None,
    }

    with (directories[wire] / "arrivals.jsonl").open("ab") as ledger:
        ledger.write(b'{"n":0}\n{"n":1}\n{"n":2')  # two whole lines, the third still being written
    waiting = {
        "checked_at": "2026-10-27T10:00:00+00:00",
        "rows_seen": 3,
        "error": "point 'wire_2' (line 2) not admitted yet: not there",
        "error_at": "2026-10-27T09:59:00+00:00",
    }
    (directories[follower] / "arrival-status.json").write_text(json.dumps(waiting))
    wire_live = client.get(f"/api/v1/jobs/{wire}").json()["live"]
    follower_live = client.get(f"/api/v1/jobs/{follower}").json()["live"]

    assert wire_live["arrivals"] == 2
    assert (follower_live["source"], follower_live["follows"], follower_live["follows_job_id"]) == (
        "run",
        {"kind": "reconstruction", "id": 1, "label": "R1"},
        wire,
    )
    assert follower_live["arrival"] == {"state": "available", **waiting}

    for job_id in (wire, follower):
        client.post(f"/api/v1/jobs/{job_id}/cancel")  # Queued: cancelled at once
    assert client.get(f"/api/v1/jobs/{wire}").json()["live"]["source"] == "notify"  # ended, live.json there
    (directories[wire] / "live.json").unlink()
    assert client.get(f"/api/v1/jobs/{wire}").json()["live"] is None
    with Session(place.engine) as rows:  # a job of no live run: running, its queue identity another's
        rows.add(Job(job_id=99, computer_name="h", status=int(JobStatus.RUNNING), priority=0, queue_job_id="live_1"))
        rows.commit()
    assert client.get("/api/v1/jobs/99").json()["live"] is None


# ---------------------------------------------------------------------------
# The contract


def test_the_contract_documents_the_live_routes_errors_and_key():
    """The live routes are in the contract whatever the configuration (camelCase operation IDs, response models),
    with the problems they answer: 404 (live off), Start's 409 (``LiveRunsConflict``: ``open_run``, ``created``),
    its 503, and its required ``Idempotency-Key``."""
    paths = openapi_document()["paths"]
    daqs, validate, create = paths[DAQS]["get"], paths[VALIDATE]["post"], paths[CREATE]["post"]
    session = paths[SESSION]["get"]

    assert (daqs["operationId"], validate["operationId"], create["operationId"], session["operationId"]) == (
        "listLiveDaqs",
        "validateLiveRuns",
        "createLiveRuns",
        "getLiveSession",
    )
    assert {"200", "404", "503"} <= set(daqs["responses"]) and {"200", "404"} <= set(validate["responses"])
    assert {"200", "404"} <= set(session["responses"])
    assert {"201", "404", "409", "422", "503"} <= set(create["responses"])
    conflict = create["responses"]["409"]["content"][PROBLEM_JSON]["schema"]["$ref"]
    assert conflict.endswith("/LiveRunsConflict")
    (key,) = [parameter for parameter in create["parameters"] if parameter["in"] == "header"]
    assert (key["name"], key["required"]) == ("Idempotency-Key", True)


# ---------------------------------------------------------------------------
# End to end (slow): a session started through the API, run by a real worker


@pytest.mark.slow
def test_a_live_session_started_through_the_api_runs_through_the_worker_and_stops_finished(
    tmp_path, live: Live, monkeypatch
):
    """A Both session started through the API on demo stacks (``tests.wire_support``): a real worker runs the live
    wire run and its follower (the real computes), stacks admitted with ``notify`` are reconstructed and their points
    indexed, and Stop through the job API on the wire run ends both Finished (the follower drains once its wire run
    has ended)."""
    root = tmp_path / "root"
    folder = root / "live"
    folder.mkdir(parents=True)
    shutil.copy(GEOMETRY, root / "geometry.xml")
    shutil.copy(FIXTURES / "Ni.xml", root / "Ni.xml")
    with Session(live.engine) as session, session.begin():
        session.add(create_test_metadata(1))
    settings = make_settings(
        tmp_path,
        database={"path": live.database},
        files={"roots": {"data": root}, "runs_root": tmp_path / "runs"},
        execution={"num_threads": 1, "memory_limit_mb": 256},  # wire_support's reconstruction (OPTIONS)
        live={"enabled": True},
    )
    place = Place(settings, live.engine, root, folder, root / "out", root / "geometry.xml", root / "Ni.xml")
    wire_values = {name: OPTIONS[name] for name in OPTIONS if name in place.part("wire-live")}
    form = place.form(
        wire={**wire_values, "wire_edges": "leading"},
        lauego={"box_size": 5, "threshold_ratio": None, "depth_range": "0-2"},
    )
    config = live.write_config(
        tmp_path / "live-worker.toml",
        live={"publish_seconds": 0.5, "arrivals_seconds": 0.05, "follow_seconds": 0.3, "cpu_share": _share(2)},
        workers=1,
    )
    with portal(settings) as client:
        response = start(client, form)
        assert response.status_code == 201, response.text
        wire, follower = (run["job_id"] for run in response.json()["runs"])
        worker = start_worker(config, log=tmp_path / "worker.log")
        try:
            running = lambda: all(load_job(live.engine, job).process_pid for job in (wire, follower))  # noqa: E731
            assert wait_for(running, 60), worker.log_text()
            for number in (1, 2):
                notify(live.engine, wire, write_wire_scan(folder / f"wire_{number}.h5", seed=number))
            indexed = lambda: load_job(live.engine, follower).n_succeeded == 6  # noqa: E731  2 points x depths 0-2
            assert wait_for(indexed, 120), worker.log_text()
            assert client.post(f"/api/v1/jobs/{wire}/cancel").json()["state"] == "requested"
            assert worker.wait(120) == 0, worker.log_text()
        finally:
            worker.cleanup()

    for job_id, inputs in ((wire, 2), (follower, 6)):
        row = load_job(live.engine, job_id)
        assert (row.status, row.phase) == (JobStatus.FINISHED, RunPhase.FINISHED), row.messages
        assert (row.n_inputs, row.n_succeeded, row.n_failed) == (inputs, inputs, 0)


def _share(cores: int) -> float:
    """``live.cpu_share`` that gives the live runs ``cores`` cores in all on the test's host (``dispatch.live_cores``)."""
    count = os.cpu_count() or 1
    assert count >= cores
    return min(1.0, (cores + 0.5) / count)
