"""The results viewer's artifact, XML chooser, conversion and summary (``indexing.viewer``, ``indexing.conversion``) on
small runs made from ``tests/fixtures/test_indexing.xml``. I1 and I2 of the demo: ``test_viewer_demo.py`` (slow)."""

from __future__ import annotations

import json
import os
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py
import pytest
from fastapi.testclient import TestClient
from lauelab.visualization import convert_xml
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.app import create_app
from laueportal.core.settings import Settings
from laueportal.indexing import conversion
from laueportal.indexing import viewer as viewer_module
from laueportal.jobs import dispatch
from laueportal.jobs.rundir import RESULTS_FILENAME
from laueportal.jobs.state import JobStatus
from laueportal.jobs.worker import dispatch_policy
from laueportal.remote.paths import PLAN_NAME
from laueportal.science import results as science_results
from tests.api_support import PORTAL_HEADERS
from tests.conftest import create_test_lauego_parameters
from tests.run_support import dispatch_queue
from tests.settings_support import make_database
from tests.worker_support import Live, run_burst_worker

FIXTURES = Path(__file__).parents[1] / "fixtures"
FIXTURE_XML = FIXTURES / "test_indexing.xml"
GEOMETRY = FIXTURES / "lauelab" / "geoN_2022-03-29_14-15-05.xml"
RUNS = "/api/v1/indexing-runs"
REMOTE_RUN = "/compute/workspace/runs/run-1"
"""A remote run's directory as the remote side sees it (the plan maps the portal's run directory to it)."""


@dataclass
class Viewer:
    client: TestClient
    engine: Engine
    settings: Settings
    root: Path
    """The file root, holding the run directories."""

    def run_directory(self, indexing_id: int, *xml_names: str) -> Path:
        """``<root>/index_<id>`` with a copy of the fixture XML under each name."""
        directory = self.root / f"index_{indexing_id}"
        directory.mkdir()
        for name in xml_names:
            shutil.copy(FIXTURE_XML, directory / name)
        return directory

    def add_run(
        self,
        indexing_id: int,
        directory: Path | None,
        *,
        output_xml: str | None = None,
        geometry_file: str | None = None,
    ) -> int:
        add_run(self.engine, indexing_id, directory, output_xml=output_xml, geometry_file=geometry_file)
        return indexing_id

    def get(self, indexing_id: int, product: str, **params: Any) -> Any:
        return self.client.get(f"{RUNS}/{indexing_id}/{product}", params=params)

    def post(self, indexing_id: int, action: str, body: Any = None) -> Any:
        return self.client.post(f"{RUNS}/{indexing_id}/{action}", json=body)


def add_run(
    engine: Engine,
    indexing_id: int,
    directory: Path | None,
    *,
    output_xml: str | None = None,
    geometry_file: str | None = None,
) -> None:
    with Session(engine) as session:
        session.add(
            models.Job(
                job_id=indexing_id,
                computer_name="h",
                status=2,
                priority=0,
                submit_time=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        run = models.IndexingRun(
            id=indexing_id,
            job_id=indexing_id,
            method="lauego",
            input_path="/data",
            output_path=str(directory) if directory is not None else None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        parameters = create_test_lauego_parameters()
        parameters.output_xml = output_xml
        if geometry_file is not None:
            parameters.geometry_file = geometry_file
        run.lauego_parameters = parameters
        session.add(run)
        session.commit()


def converted(directory: Path, *, geometry_path: str | None = None, source: Path = FIXTURE_XML) -> Path:
    """``<directory>/output.h5`` converted from ``source`` (the fixture XML; no geometry), its ``/geometry@path``
    set to ``geometry_path``."""
    results = convert_xml(source, directory / RESULTS_FILENAME)
    if geometry_path is not None:
        with h5py.File(results, "r+") as file:
            file["geometry"].attrs["path"] = geometry_path
    return results


def with_peaks(target: Path, peaks: dict[int, int]) -> Path:
    """A copy of the fixture XML at ``target`` in which step ``i`` (one pattern) keeps only its first ``peaks[i]``
    peaks and the assignments to them."""
    tree = ET.parse(FIXTURE_XML)
    steps = tree.getroot().findall("step")
    for index, count in peaks.items():
        detected = steps[index].find("detector/peaksXY")
        indexing = steps[index].find("indexing")
        hkl = steps[index].find("indexing/pattern/hkl_s")
        assert detected is not None and indexing is not None and hkl is not None
        for column in detected:
            column.text = " ".join((column.text or "").split()[:count])
        assigned = [i for i, peak in enumerate((hkl.findtext("PkIndex") or "").split()) if int(peak) < count]
        for column in hkl:
            values = (column.text or "").split()
            column.text = " ".join(values[i] for i in assigned)
        detected.set("Npeaks", str(count))
        indexing.set("Npeaks", str(count))
        for element in (indexing, indexing.find("pattern")):
            assert element is not None
            element.set("Nindexed", str(len(assigned)))
    tree.write(target)
    return target


def selected_xml(engine: Engine, indexing_id: int) -> str | None:
    with Session(engine) as session:
        run = session.get(models.IndexingRun, indexing_id)
        assert run is not None
        return run.selected_xml


def results_path(engine: Engine, indexing_id: int) -> str | None:
    with Session(engine) as session:
        run = session.get(models.IndexingRun, indexing_id)
        assert run is not None
        return run.results_path


def open_viewer(tmp_path: Path, **sections: dict[str, Any]) -> Iterator[Viewer]:
    root = tmp_path / "data"
    root.mkdir()
    settings, engine = make_database(tmp_path, files={"roots": {"data": root}}, **sections)
    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        yield Viewer(client, engine, settings, root)
    engine.dispose()


@pytest.fixture
def viewer(tmp_path: Path) -> Iterator[Viewer]:
    """The API on a fresh database whose file root is ``tmp_path/data``."""
    yield from open_viewer(tmp_path)


# ---------------------------------------------------------------------------
# The artifact


@pytest.mark.parametrize(
    ("layout", "state"),
    [
        pytest.param("output.h5 and an XML", "ready", id="ready"),
        pytest.param("one XML", "xml_only", id="xml-only"),
        pytest.param("two XMLs", "ambiguous", id="ambiguous"),
        pytest.param("nothing", "empty", id="empty"),
    ],
)
def test_the_artifact_follows_its_resolution_order(viewer: Viewer, layout: str, state: str) -> None:
    names = {"output.h5 and an XML": ("a.xml",), "one XML": ("a.xml",), "two XMLs": ("b.xml", "a.xml")}.get(layout, ())
    directory = viewer.run_directory(1, *names)
    if layout.startswith("output.h5"):
        converted(directory)
    viewer.add_run(1, directory)

    response = viewer.get(1, "artifact")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == state
    assert body["results_path"] == (str(directory / RESULTS_FILENAME) if state == "ready" else None)
    assert body["xml_path"] == (str(directory / "a.xml") if state == "xml_only" else None)
    assert body["xml_source"] == ("only" if state == "xml_only" else None)
    assert body["xml_candidates"] == (["a.xml", "b.xml"] if state == "ambiguous" else [])
    assert (body["conversion_job_id"], body["conversion"]) == (None, None)  # xml_only: looked up, and none
    assert viewer.get(2, "artifact").status_code == 404


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (JobStatus.RUNNING, "writing"),  # active
        (JobStatus.FINISHED, "empty"),  # ended
    ],
)
def test_without_results_a_run_is_writing_or_empty_and_one_whose_directory_is_not_here_is_not_on_this_host(
    viewer: Viewer, status: JobStatus, state: str
) -> None:
    """A run directory that is there (or none recorded) without results is writing while the run is active, else
    empty; one that is absent or unreadable (an unmounted ``/net`` path, read as recorded) is not on this host,
    whatever the run's status, and its summary is a 404 that says so."""
    viewer.add_run(1, viewer.run_directory(1))
    viewer.add_run(2, None)
    viewer.add_run(3, viewer.run_directory(3))
    (viewer.root / "index_3").rmdir()  # recorded, then gone
    unreadable = viewer.run_directory(4)
    viewer.add_run(4, unreadable)
    with Session(viewer.engine) as session:
        session.execute(update(models.Job).values(status=int(status)))
        session.commit()
    unreadable.chmod(0)
    try:
        answers = {run: viewer.get(run, "artifact").json() for run in (1, 2, 3, 4)}
        summary = viewer.get(3, "summary")
    finally:
        unreadable.chmod(0o755)

    for run, body in answers.items():
        expected = state if run in (1, 2) else "not_on_host"
        assert (body["state"], body["results_path"], body["xml_path"]) == (expected, None, None)
    assert summary.status_code == 404
    assert (
        summary.json()["detail"]
        == f"I3 has no results to show: its run directory {viewer.root / 'index_3'} is not on this host"
    )


def test_the_artifact_is_revalidated_by_its_etag_and_a_replaced_results_file_is_another_version(
    viewer: Viewer,
) -> None:
    """The artifact is tagged by its content: asked again with its tag, a 304 while nothing changed; a results file
    replaced (a live run's preview ``output.h5``) is another ``results_version``, so another answer and tag.
    ``live_open`` says an open live run (its queue identity), which the page polls."""
    directory = viewer.run_directory(1)
    converted(directory)
    viewer.add_run(1, directory)

    first = viewer.get(1, "artifact")
    tag = first.headers["etag"]
    again = viewer.client.get(f"{RUNS}/1/artifact", headers={"If-None-Match": tag})

    assert (first.status_code, first.headers["cache-control"], again.status_code) == (200, "private, no-cache", 304)
    assert (first.json()["results_version"] is not None, first.json()["live_open"]) == (True, False)
    replacement = converted(viewer.run_directory(2))
    os.replace(replacement, directory / RESULTS_FILENAME)
    replaced = viewer.client.get(f"{RUNS}/1/artifact", headers={"If-None-Match": tag})
    assert replaced.status_code == 200 and replaced.headers["etag"] != tag
    assert replaced.json()["results_version"] not in (None, first.json()["results_version"])

    with Session(viewer.engine) as session:  # a running batch run: not open live
        session.execute(update(models.Job).values(status=int(JobStatus.RUNNING)))
        session.commit()
    assert viewer.get(1, "artifact").json()["live_open"] is False
    with Session(viewer.engine) as session:
        session.execute(update(models.Job).values(queue_job_id="live_1"))
        session.commit()
    assert viewer.get(1, "artifact").json()["live_open"] is True
    with Session(viewer.engine) as session:
        session.execute(update(models.Job).values(status=int(JobStatus.FINISHED)))
        session.commit()
    assert viewer.get(1, "artifact").json()["live_open"] is False


def test_the_xml_chooser_records_the_choice_can_change_it_and_the_viewer_reads_that_xml(viewer: Viewer) -> None:
    directory = viewer.run_directory(1, "a.xml", "b.xml")
    viewer.add_run(1, directory, output_xml="gone.xml")  # recorded, but not in the run directory
    assert viewer.get(1, "summary").status_code == 409

    chosen = viewer.post(1, "artifact/select-xml", {"name": "b.xml"})
    first_summary = viewer.get(1, "summary").json()
    again = viewer.post(1, "artifact/select-xml", {"name": "a.xml"})  # still offered, so the choice can change

    for response, name in ((chosen, "b.xml"), (again, "a.xml")):
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["state"], body["xml_path"]) == ("xml_only", str(directory / name))
        assert (body["xml_source"], body["xml_candidates"]) == ("chosen", ["a.xml", "b.xml"])
    assert (first_summary["source"], first_summary["path"]) == ("xml", str(directory / "b.xml"))
    assert selected_xml(viewer.engine, 1) == "a.xml"
    assert viewer.get(1, "summary").json()["path"] == str(directory / "a.xml")


def test_the_chooser_takes_only_an_offered_name_and_only_while_ambiguous(viewer: Viewer) -> None:
    viewer.add_run(1, viewer.run_directory(1, "a.xml", "b.xml"))
    viewer.add_run(2, viewer.run_directory(2, "a.xml"))

    unknown = viewer.post(1, "artifact/select-xml", {"name": "../index_2/a.xml"})
    settled = viewer.post(2, "artifact/select-xml", {"name": "a.xml"})

    assert unknown.status_code == 422, unknown.text
    assert [(item["loc"], item["code"]) for item in unknown.json()["errors"]] == [(["body", "name"], "unknown_xml")]
    assert settled.status_code == 409, settled.text
    assert "artifact is xml_only" in settled.json()["detail"]
    assert (selected_xml(viewer.engine, 1), selected_xml(viewer.engine, 2)) == (None, None)


def test_the_chooser_refuses_while_the_runs_conversion_is_queued_or_running(viewer: Viewer) -> None:
    """A queued conversion converts the choice it finds when it starts, and a converted run's output.h5 fixes the
    choice, so the choice cannot change under a conversion (its row, ``indexing.conversion``)."""

    viewer.add_run(1, viewer.run_directory(1, "a.xml", "b.xml"))
    conversion.enqueue_conversion(1, sessions=viewer.engine)
    refused = {"queued": viewer.post(1, "artifact/select-xml", {"name": "b.xml"})}
    assert dispatch.claim_conversion(viewer.engine, 1)
    refused["running"] = viewer.post(1, "artifact/select-xml", {"name": "b.xml"})
    unchanged = selected_xml(viewer.engine, 1)
    with Session(viewer.engine) as session, session.begin():
        session.execute(update(models.Conversion).values(status="failed"))
    after_a_failure = viewer.post(1, "artifact/select-xml", {"name": "b.xml"})

    for status, response in refused.items():
        assert response.status_code == 409, response.text
        assert response.json()["detail"].startswith(f"I1's conversion to output.h5 is {status}: the XML cannot change")
    assert (unchanged, after_a_failure.status_code) == (None, 200)
    assert selected_xml(viewer.engine, 1) == "b.xml"


# ---------------------------------------------------------------------------
# The summary and the scope


@pytest.mark.parametrize(
    ("scope", "steps"),
    [
        pytest.param({}, [0, 1, 2], id="default"),  # min_peaks 4 (the bar's N = 3): 4 peaks kept, 3 skipped
        pytest.param({"min_peaks": 5}, [0, 1], id="min-peaks"),
        pytest.param({"min_peaks": 12, "pattern0_only": "false"}, [0], id="all-patterns"),
        pytest.param({"min_peaks": 13}, [], id="none-kept"),
        pytest.param({"min_peaks": 0}, [0, 1, 2, 3], id="every-step"),
    ],
)
def test_the_summary_reports_the_results_within_the_scope(
    viewer: Viewer, tmp_path: Path, scope: dict[str, Any], steps: list[int]
) -> None:
    directory = viewer.run_directory(1)
    converted(directory, source=with_peaks(tmp_path / "steps.xml", {2: 4, 3: 3}))  # 12, 8, 4 and 3 peaks
    viewer.add_run(1, directory)

    response = viewer.get(1, "summary", **scope)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["steps"], body["default_step"]) == (steps, steps[0] if steps else None)
    assert (body["source"], body["path"]) == ("results", str(directory / RESULTS_FILENAME))
    assert body["counts"] == {"frames": 4, "patterns": 5, "peaks": 27}
    # The fixture's frames have no depth: no depth axis, and of the lab axes only those without Z.
    assert [axis["value"] for axis in body["axes"]] == ["X", "Y", "Z", "H", "F", "Xlab", "Ylab"]
    modes = {mode["value"]: (mode["scalar"], mode["available"]) for mode in body["color_modes"]}
    assert modes == {
        "cubic_ipf": (False, True),
        "rodrigues": (False, True),
        "misorientation": (False, True),
        "pole_hsv": (False, True),
        "n_indexed": (True, True),
        "goodness": (True, True),
        "rms_error": (True, True),
        "n_patterns": (True, True),
    }
    assert (body["structure"], body["space_group"]) == ("TestMaterial", 225)
    assert body["geometry"] == {"source": "none", "named": None, "recorded": None}


def test_the_scope_refuses_a_negative_minimum(viewer: Viewer) -> None:
    response = viewer.get(1, "summary", min_peaks=-1)
    assert response.status_code == 422, response.text
    assert [item["loc"] for item in response.json()["errors"]] == [["query", "min_peaks"]]


def test_missing_results_and_an_unknown_run_are_404s_with_the_reason(viewer: Viewer) -> None:
    viewer.add_run(1, viewer.run_directory(1))

    missing = viewer.get(1, "summary")

    assert missing.status_code == 404, missing.text
    assert missing.json()["title"] == "Results not found"
    assert missing.json()["detail"] == "I1 has no results to show: no results file and no XML document found"
    assert viewer.get(2, "summary").json()["detail"] == "No indexing run I2"


def test_results_lauelab_cannot_read_are_a_409_of_their_own_type(viewer: Viewer) -> None:
    directory = viewer.run_directory(1)
    (directory / "a.xml").write_text("<AllSteps><step>")  # not well-formed
    viewer.add_run(1, directory)

    response = viewer.get(1, "summary")

    assert response.status_code == 409, response.text
    body = response.json()
    assert (body["type"], body["title"]) == ("urn:laueportal:problem:results-unreadable", "Results unreadable")
    assert body["detail"].startswith(f"{directory / 'a.xml'} cannot be read: 'ParseError: ")


# ---------------------------------------------------------------------------
# Paths inside the outputs


@pytest.mark.parametrize(
    ("remote", "named", "state", "source"),
    [
        pytest.param(True, f"{REMOTE_RUN}/geometry.xml", "available", "file", id="remote-mapped"),
        pytest.param(True, "{elsewhere}/geometry.xml", "unavailable", "none", id="remote-unavailable"),
        pytest.param(False, "{run}/geometry.xml", "available", "file", id="local"),
        pytest.param(False, "{elsewhere}/geometry.xml", "outside_roots", "none", id="local-outside-the-roots"),
        pytest.param(False, "geometry.xml", "available", "file", id="local-relative"),
    ],
)
def test_the_geometry_an_output_names_is_mapped_and_an_unavailable_one_is_never_opened(
    viewer: Viewer, tmp_path: Path, remote: bool, named: str, state: str, source: str
) -> None:
    directory = viewer.run_directory(1)
    elsewhere = tmp_path / "elsewhere"  # outside the root and the run: a valid geometry lauelab would load
    elsewhere.mkdir()
    for folder in (directory, elsewhere):
        shutil.copy(GEOMETRY, folder / "geometry.xml")
    value = named.format(run=directory, elsewhere=elsewhere)
    converted(directory, geometry_path=value)
    if remote:
        plan = {"version": 1, "submission_id": "laue-1-0001", "path_map": {str(directory): REMOTE_RUN}}
        (directory / PLAN_NAME).write_text(json.dumps(plan))
    viewer.add_run(1, directory)

    response = viewer.get(1, "summary")

    assert response.status_code == 200, response.text
    mapped = str(directory / "geometry.xml") if state == "available" else None
    if state == "outside_roots":
        mapped = value
    assert response.json()["geometry"] == {
        "source": source,
        "named": {"value": value, "path": mapped, "state": state},
        "recorded": None,
    }


def test_a_geometry_under_a_folder_the_server_cannot_open_is_missing_and_converts_without_it(viewer: Viewer) -> None:
    """Not a 500: ``Path.is_file`` raises there (Python 3.12). The path is missing for this host, and lauelab would
    not find it either, so a conversion runs without a geometry."""
    directory = viewer.run_directory(1)
    locked = viewer.root / "locked"
    locked.mkdir()
    shutil.copy(GEOMETRY, locked / "geometry.xml")
    value = str(locked / "geometry.xml")
    text = FIXTURE_XML.read_text().replace("<detectorID>", f"<geoFile>{value}</geoFile><detectorID>")
    (directory / "a.xml").write_text(text)
    locked.chmod(0)
    try:
        mapped = viewer_module.map_output_path(
            value, run_directory=str(directory), base=directory, settings=viewer.settings
        )
        geometry = viewer_module.conversion_geometry(
            str(directory / "a.xml"), run_directory=str(directory), settings=viewer.settings
        )
    finally:
        locked.chmod(0o700)
    assert (mapped.path, mapped.state) == (value, "missing")
    assert geometry == (None, None)


def test_an_embedded_geometry_is_used_and_no_path_is_opened(viewer: Viewer) -> None:
    directory = viewer.run_directory(1)
    convert_xml(FIXTURE_XML, directory / RESULTS_FILENAME, geometry=GEOMETRY)  # embeds the geometry's text
    with h5py.File(directory / RESULTS_FILENAME, "r+") as file:
        file["geometry"].attrs["path"] = "/nowhere/geometry.xml"
    viewer.add_run(1, directory)

    geometry = viewer.get(1, "summary").json()["geometry"]

    assert geometry == {
        "source": "embedded",
        "named": {"value": "/nowhere/geometry.xml", "path": "/nowhere/geometry.xml", "state": "outside_roots"},
        "recorded": None,
    }


@pytest.mark.parametrize(
    ("recorded", "named", "source"),
    [
        pytest.param("{root}/geometry.xml", "/nowhere/geometry.xml", "recorded", id="recorded"),
        pytest.param("/test/geometry.xml", "{root}/geometry.xml", "file", id="recorded-unavailable"),
    ],
)
def test_an_xml_loads_with_the_runs_recorded_geometry_when_it_is_available(
    viewer: Viewer, recorded: str, named: str, source: str
) -> None:
    directory = viewer.run_directory(1)
    shutil.copy(GEOMETRY, viewer.root / "geometry.xml")
    recorded, named = recorded.format(root=viewer.root), named.format(root=viewer.root)
    text = FIXTURE_XML.read_text().replace("<detectorID>", f"<geoFile>{named}</geoFile><detectorID>")
    (directory / "a.xml").write_text(text)
    viewer.add_run(1, directory, geometry_file=recorded)

    geometry = viewer.get(1, "summary").json()["geometry"]

    def reported(value: str) -> dict[str, str]:
        return {
            "value": value,
            "path": value,
            "state": "available" if value.startswith(str(viewer.root)) else "outside_roots",
        }

    assert geometry == {"source": source, "named": reported(named), "recorded": reported(recorded)}


# ---------------------------------------------------------------------------
# Conversion on the worker


def test_converting_needs_an_xml_only_artifact_and_queues_one_conversion(viewer: Viewer) -> None:
    """The conversion table is its queue: a second request returns the conversion queued, and the dispatcher takes it
    before any run."""
    ready = viewer.run_directory(1)
    converted(ready)
    viewer.add_run(1, ready)
    viewer.add_run(2, viewer.run_directory(2, "a.xml"))

    refused = viewer.post(1, "convert")
    queued, again = viewer.post(2, "convert"), viewer.post(2, "convert")
    artifact = viewer.get(2, "artifact").json()

    assert refused.status_code == 409, refused.text
    assert "artifact is ready" in refused.json()["detail"]
    assert (queued.status_code, again.status_code) == (202, 202), queued.text
    assert (queued.json()["id"], queued.json()["status"]) == ("convert_results_2", "queued")
    assert again.json() == queued.json()
    assert (artifact["conversion_job_id"], artifact["conversion"]) == ("convert_results_2", queued.json())
    assert dispatch_queue(viewer.engine) == [("convert", 2)]


@pytest.mark.parametrize(
    ("named", "outcome", "recorded"),
    [
        pytest.param("{root}/geometry.xml", "converted", "{root}/geometry.xml", id="inside-the-roots"),
        pytest.param("{root}/link.xml", "converted", "{root}/geometry.xml", id="through-a-link"),  # as checked
        pytest.param("{elsewhere}/geometry.xml", "failed", None, id="outside-the-roots"),
        pytest.param("/nowhere/geometry.xml", "converted", "/nowhere/geometry.xml", id="not-on-this-host"),
    ],
)
def test_a_conversion_records_the_mapped_geometry_and_never_opens_one_outside_the_roots(
    viewer: Viewer, tmp_path: Path, named: str, outcome: str, recorded: str | None
) -> None:
    directory = viewer.run_directory(1)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for folder in (viewer.root, elsewhere):
        shutil.copy(GEOMETRY, folder / "geometry.xml")
    (viewer.root / "link.xml").symlink_to(viewer.root / "geometry.xml")
    value = named.format(root=viewer.root, elsewhere=elsewhere)
    text = FIXTURE_XML.read_text().replace("<detectorID>", f"<geoFile>{value}</geoFile><detectorID>")
    (directory / "a.xml").write_text(text)
    viewer.add_run(1, directory)

    result = conversion.convert_run(1, engine=viewer.engine, settings=viewer.settings)

    assert result.outcome == outcome, result.message
    if outcome == "failed":
        assert "outside roots for this run, and lauelab would open it as written" in result.message
        assert not (directory / RESULTS_FILENAME).exists() and results_path(viewer.engine, 1) is None
    else:
        assert results_path(viewer.engine, 1) == str(directory / RESULTS_FILENAME)
        assert recorded is not None
        geometry = science_results.named_geometry(directory / RESULTS_FILENAME)
        assert (geometry.embedded, geometry.paths) == (
            value.startswith(str(viewer.root)),
            (recorded.format(root=viewer.root),),
        )


def test_a_conversion_runs_on_the_worker_and_the_artifact_reports_how_it_ended(tmp_path: Path, live: Live) -> None:
    """End to end: the API queues two conversions on the table, a burst worker takes them (the oldest request first,
    before any run) in job processes of their own, each with the conversion's timeout, and the artifact reports how each
    ended."""
    root = tmp_path / "data"
    root.mkdir()
    good, bad = root / "index_1", root / "index_2"
    for directory in (good, bad):
        directory.mkdir()
    shutil.copy(FIXTURE_XML, good / "a.xml")
    (bad / "a.xml").write_text("<AllSteps><step>")  # not well-formed: "malformed"
    for indexing_id, directory in ((1, good), (2, bad)):
        add_run(live.engine, indexing_id, directory)
    settings, engine = make_database(tmp_path / "api", database={"path": live.database})
    engine.dispose()

    with TestClient(create_app(settings), headers=PORTAL_HEADERS) as client:
        first = client.post(f"{RUNS}/1/convert")
        again = client.post(f"{RUNS}/1/convert")
        assert client.post(f"{RUNS}/2/convert").status_code == 202
        queued = client.get(f"{RUNS}/1/artifact").json()
        order = dispatch_queue(live.engine)
        worker = run_burst_worker(live.config, log=tmp_path / "worker.log")
        done, failed = client.get(f"{RUNS}/1/artifact").json(), client.get(f"{RUNS}/2/artifact").json()

    assert worker.process.returncode == 0, worker.log_text()
    assert first.status_code == 202, first.text
    job = first.json()
    assert (job["id"], job["kind"], job["indexing_id"], job["status"]) == (
        "convert_results_1",
        "convert_results",
        1,
        "queued",
    )
    assert again.json()["id"] == job["id"] and dispatch_queue(live.engine) == []  # the worker took both, once each
    assert (queued["conversion_job_id"], queued["conversion"]["status"]) == ("convert_results_1", "queued")
    assert order == [("convert", 1), ("convert", 2)]
    assert dispatch_policy(settings).conversion_timeout_seconds == conversion.CONVERSION_TIMEOUT_SECONDS
    assert (done["state"], done["results_path"]) == ("ready", str(good / RESULTS_FILENAME))
    assert results_path(live.engine, 1) == str(good / RESULTS_FILENAME)
    assert science_results.validate_results(good / RESULTS_FILENAME)[0] is not None
    ended = conversion.find_conversion(1, sessions=live.engine)
    assert ended is not None and ended.status == "finished" and ended.message == "converted: 4 frames, 5 patterns"
    assert ended.started_at is not None and ended.ended_at is not None
    assert (failed["state"], failed["conversion"]["status"]) == ("xml_only", "failed")
    assert failed["conversion"]["message"].startswith("malformed: ParseError")
    assert not (bad / RESULTS_FILENAME).exists()
    log = worker.log_text()
    assert "Started the job process" in log and "of the conversion of I1" in log
