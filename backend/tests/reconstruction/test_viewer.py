"""The wire viewer's run, artifact and points (``reconstruction.viewer``) on runs made from the synthetic reconstruction
and a generated legacy per-depth fixture (``tests/recon_support.py``)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from laueportal import models
from laueportal.app import create_app
from laueportal.jobs.state import JobStatus
from laueportal.reconstruction import viewer as viewer_module
from laueportal.remote.paths import PLAN_NAME
from laueportal.science.recon import DEFAULT_CACHE_BYTES, MIB
from tests.recon_support import (
    SYNTHETIC_DEPTHS,
    SYNTHETIC_FRAME,
    CatalogPoint,
    copy_synthetic,
    write_per_depth,
    write_snapshot,
)
from tests.reconstruction.conftest import REMOTE_RUN, Viewer
from tests.settings_support import make_settings


def artifact_counts(body: dict[str, Any]) -> tuple[Any, ...]:
    return body["state"], body["catalog_status"], body["points_complete"], body["points_total"]


# ---------------------------------------------------------------------------
# The run


def test_the_run_has_its_parameters_and_its_paths_relative_to_the_innermost_root(viewer: Viewer) -> None:
    run_directory = viewer.add_run(1)
    with Session(viewer.engine) as session:  # a geometry file outside every root
        session.execute(update(models.WireReconstructionParameters).values(geometry_file="/elsewhere/geo.xml"))
        session.commit()

    response = viewer.get(1)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["id"], body["method"], body["job_id"], body["output_path"]) == (1, "wire", 1, str(run_directory))
    assert body["parameters"]["scan_points"] == "1-10"
    assert body["parameters"]["geometry_file"] == "/elsewhere/geo.xml"
    assert body["relative_paths"] == {
        "input_path": {"root": "raw", "relative": ""},  # the root itself, named by the innermost root
        "output_path": {"root": "data", "relative": "rec_1"},
        "geometry_file": None,  # outside every root
    }


@pytest.mark.parametrize("product", ["", "artifact", "points"])
def test_an_unknown_run_is_a_404_problem(viewer: Viewer, product: str) -> None:
    response = viewer.get(7, product)

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "No reconstruction run R7"


# ---------------------------------------------------------------------------
# The artifact


@pytest.mark.parametrize("layout", ["scan", "per_depth"])
def test_the_artifact_is_the_scan_catalog_or_the_legacy_per_depth_files(viewer: Viewer, layout: str) -> None:
    run_directory = viewer.add_run(1)
    if layout == "scan":
        path = copy_synthetic(run_directory)  # the synthetic reconstruction: one point, finished
        expected = ("ready", "finished", 1, 1)
    else:
        path = run_directory
        write_per_depth(run_directory, "wire_1")
        write_per_depth(run_directory, "wire_2", frames=False)  # a summary without frames
        expected = ("ready", None, 1, 2)

    body = viewer.get(1, "artifact").json()

    assert (body["format"], body["path"], body["run_status"]) == (layout, str(path), "finished")
    assert artifact_counts(body) == expected


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (JobStatus.RUNNING, "writing"),  # active
        (JobStatus.FINISHED, "empty"),  # ended
    ],
)
def test_without_an_artifact_a_run_is_writing_or_empty_and_one_whose_directory_is_not_here_is_not_on_this_host(
    viewer: Viewer, status: JobStatus, state: str
) -> None:
    """A run directory that is there (or none recorded) without a reconstruction is writing while the run is active,
    else empty; one that is absent or unreadable (an unmounted ``/net`` path, read as recorded) is not on this
    host, whatever the run's status."""
    viewer.add_run(1, status=status)
    viewer.add_run(2, status=status, directory=False)
    viewer.add_run(3, status=status).rmdir()
    unreadable = viewer.add_run(4, status=status)
    unreadable.chmod(0)
    try:
        answers = {run: (viewer.get(run, "artifact").json(), viewer.get(run, "points").json()) for run in (1, 2, 3, 4)}
    finally:
        unreadable.chmod(0o755)

    for run, (artifact, points) in answers.items():
        expected = state if run in (1, 2) else "not_on_host"
        assert (artifact["state"], artifact["format"], artifact["path"]) == (expected, None, None)
        assert points == {"reconstruction_id": run, "state": expected, "rows": []}


def test_a_running_catalog_shows_the_points_complete_so_far(viewer: Viewer) -> None:
    run_directory = viewer.add_run(1, status=JobStatus.RUNNING)
    points = [CatalogPoint(f"wire_{i}", "pending", f"/raw/wire_{i}.h5") for i in (1, 2, 3)]
    progress = []
    for complete in (1, 2):
        points[complete - 1] = CatalogPoint(f"wire_{complete}", "complete", f"/raw/wire_{complete}.h5")
        points[complete] = CatalogPoint(f"wire_{complete + 1}", "writing", f"/raw/wire_{complete + 1}.h5")
        write_snapshot(run_directory, points)  # lauelab publishes a new snapshot as points complete
        rows = viewer.get(1, "points").json()["rows"]
        progress.append((artifact_counts(viewer.get(1, "artifact").json()), [row["status"] for row in rows]))

    assert progress == [
        (("ready", "running", 1, 3), ["complete", "writing", "pending"]),
        (("ready", "running", 2, 3), ["complete", "complete", "writing"]),
    ]


def test_the_artifact_and_the_points_revalidate_by_their_etag(viewer: Viewer) -> None:
    run_directory = viewer.add_run(1, status=JobStatus.RUNNING)
    points = [CatalogPoint("wire_1", "writing", "/raw/wire_1.h5"), CatalogPoint("wire_2", "pending", "/raw/b.h5")]
    write_snapshot(run_directory, points)
    first = {product: viewer.get(1, product) for product in ("artifact", "points")}

    for product, response in first.items():
        assert response.headers["cache-control"] == "private, no-cache"
        unchanged = viewer.get(1, product, **{"If-None-Match": response.headers["etag"]})
        assert (unchanged.status_code, unchanged.content) == (304, b"")

    points[0] = CatalogPoint("wire_1", "complete", "/raw/wire_1.h5")
    write_snapshot(run_directory, points)  # a new snapshot
    for product, response in first.items():
        changed = viewer.get(1, product, **{"If-None-Match": response.headers["etag"]})
        assert changed.status_code == 200
        assert changed.headers["etag"] != response.headers["etag"]

    tag = viewer.get(1, "artifact").headers["etag"]
    viewer.set_status(1, JobStatus.FINISHED)  # the run's status is part of the artifact
    assert viewer.get(1, "artifact", **{"If-None-Match": tag}).status_code == 200


def test_the_artifact_says_an_open_live_run_by_its_queue_identity(viewer: Viewer) -> None:
    """``live_open`` (the page's "Follow newest point"): a run whose queue identity is its live one (``live_<job id>``)
    while it is queued or running; not a batch run, nor a live run that has ended."""
    viewer.add_run(1, status=JobStatus.RUNNING)
    viewer.add_run(2, status=JobStatus.RUNNING)
    with Session(viewer.engine) as session:
        session.execute(update(models.Job).where(models.Job.job_id == 1).values(queue_job_id="live_1"))
        session.commit()
    tag = viewer.get(1, "artifact").headers["etag"]

    assert [viewer.get(run, "artifact").json()["live_open"] for run in (1, 2)] == [True, False]
    viewer.set_status(1, JobStatus.FINISHED)
    ended = viewer.get(1, "artifact", **{"If-None-Match": tag})
    assert (ended.status_code, ended.json()["live_open"]) == (200, False)
    # The live identity alone, its status kept (``mark_queued`` sets it on a Queued row): another tag, or a page that
    # read it before would keep its answer by a 304.
    batch_tag = viewer.get(2, "artifact").headers["etag"]
    with Session(viewer.engine) as session:
        session.execute(update(models.Job).where(models.Job.job_id == 2).values(queue_job_id="live_2"))
        session.commit()
    marked = viewer.get(2, "artifact", **{"If-None-Match": batch_tag})
    assert (marked.status_code, marked.json()["live_open"]) == (200, True)


def test_an_unreadable_catalog_is_a_409_problem(viewer: Viewer) -> None:
    run_directory = viewer.add_run(1)
    catalog = run_directory / "reconstruction" / "scan.h5"
    catalog.parent.mkdir()
    catalog.write_bytes(b"not HDF5")

    for product in ("artifact", "points"):
        response = viewer.get(1, product)
        assert response.status_code == 409
        assert response.json()["type"] == "urn:laueportal:problem:reconstruction-unreadable"
        assert response.json()["detail"].startswith(f"{catalog} cannot be read: ")


def test_the_artifact_and_the_points_hold_no_database_connection_while_they_read_files(
    viewer: Viewer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run directory may be on a slow or hung mount: they read the run, close the session, then look."""
    copy_synthetic(viewer.add_run(1))
    pool = viewer.client.app.state.engine.pool
    checked_out = []
    find = viewer_module.find_artifact

    def watching(run_directory: str | None) -> Any:
        checked_out.append(pool.checkedout())  # type: ignore[attr-defined]  # a QueuePool
        return find(run_directory)

    monkeypatch.setattr(viewer_module, "find_artifact", watching)
    for product in ("artifact", "points"):
        assert viewer.get(1, product).status_code == 200

    assert checked_out == [0, 0]


# ---------------------------------------------------------------------------
# The points


def test_the_points_have_every_column_and_an_explanation_without_images(viewer: Viewer) -> None:
    run_directory = viewer.add_run(1, status=JobStatus.RUNNING)
    statuses = ["complete", "pending", "writing", "unattempted", "interrupted", "failed"]
    errors = {5: "no wire images"}  # lauelab records an error with every failed point
    write_snapshot(
        run_directory,
        [CatalogPoint(f"w{i}", status, f"/raw/w{i}.h5", errors.get(i, "")) for i, status in enumerate(statuses)],
        run_status="running",
    )
    legacy = viewer.add_run(2)
    write_per_depth(legacy, "p1")
    write_per_depth(legacy, "p2", frames=False)

    rows = viewer.get(1, "points").json()["rows"]
    legacy_rows = viewer.get(2, "points").json()["rows"]

    assert rows[0] == {
        "point_id": "w0",
        "index": 0,
        "status": "complete",
        "explanation": None,
        "n_depths": SYNTHETIC_DEPTHS,
        "depth_first_um": -25.0,
        "depth_last_um": 25.0,
        "frame_shape": list(SYNTHETIC_FRAME),
        "dtype": "float64",
        "sample_x_um": 0.0,
        "sample_y_um": 0.0,
        "sample_z_um": -1.5,
        "source": "/raw/w0.h5",
        "source_path": "/raw/w0.h5",
        "source_state": "outside_roots",
        "error": None,
    }
    assert [(row["status"], row["n_depths"], row["frame_shape"], row["explanation"]) for row in rows[1:]] == [
        ("pending", None, None, "Point w1 has not been reconstructed yet; press Refresh after it completes. It has no images."),
        ("writing", None, None, "Point w2 is being reconstructed; press Refresh after it completes. It has no images."),
        ("unattempted", None, None, "Point w3 was not reconstructed because the run stopped first. It has no images."),
        ("interrupted", None, None, "Point w4 was interrupted before its file was recorded. It has no images."),
        ("failed", None, None, "Point w5 failed: no wire images. It has no images."),
    ]  # fmt: skip
    assert rows[5]["error"] == "no wire images"
    assert [(row["point_id"], row["status"], row["n_depths"], row["source"], row["error"], row["explanation"]) for row in legacy_rows] == [
        ("p1", "complete", SYNTHETIC_DEPTHS, None, None, None),
        ("p2", "no_frames", None, None, "no per-depth files next to the summary",
         "Point p2 has no per-depth files next to its summary. It has no images."),
    ]  # fmt: skip


def test_a_per_depth_point_whose_name_is_not_utf8_is_left_out(viewer: Viewer) -> None:
    """``os`` lists such a name with lone surrogates, which the API cannot send (it was a 500)."""
    run_directory = viewer.add_run(1)
    write_per_depth(run_directory, "good")
    (Path(os.fsdecode(bytes(run_directory) + b"/bad\xff_summary.txt"))).write_text("")

    points = viewer.get(1, "points")

    assert points.status_code == 200
    assert [row["point_id"] for row in points.json()["rows"]] == ["good"]
    assert artifact_counts(viewer.get(1, "artifact").json()) == ("ready", None, 1, 1)


@pytest.mark.parametrize("remote", [True, False], ids=["remote", "local"])
def test_a_remote_runs_sources_are_mapped_and_one_without_a_portal_path_is_unavailable(
    viewer: Viewer, tmp_path: Path, remote: bool
) -> None:
    run_directory = viewer.add_run(1)
    (run_directory / "inputs").mkdir()
    (run_directory / "inputs" / "wire_1.h5").write_bytes(b"")
    elsewhere = tmp_path / "elsewhere" / "wire_2.h5"  # outside the run and the roots: never opened
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"")
    in_run = f"{REMOTE_RUN}/inputs/wire_1.h5" if remote else str(run_directory / "inputs" / "wire_1.h5")
    sources = [in_run, str(elsewhere), "wire_3.h5"]
    write_snapshot(
        run_directory,
        [CatalogPoint(f"wire_{i}", "complete", source) for i, source in enumerate(sources, 1)],
        run_status="finished",
    )
    if remote:
        plan = {"version": 1, "submission_id": "laue-1-0001", "path_map": {str(run_directory): REMOTE_RUN}}
        (run_directory / PLAN_NAME).write_text(json.dumps(plan))

    rows = viewer.get(1, "points").json()["rows"]

    mapped = str(run_directory / "inputs" / "wire_1.h5")
    expected = [(in_run, mapped, "available")]
    if remote:  # neither the other host's path nor a relative name has a portal path
        expected += [(str(elsewhere), None, "unavailable"), ("wire_3.h5", None, "unavailable")]
    else:  # a local run's paths are the portal's: a relative name is the run directory's
        expected += [(str(elsewhere), str(elsewhere), "outside_roots"), ("wire_3.h5", str(run_directory / "wire_3.h5"), "missing")]  # fmt: skip
    assert [(row["source"], row["source_path"], row["source_state"]) for row in rows] == expected


# ---------------------------------------------------------------------------
# The product cache


def test_the_product_cache_budget_comes_from_the_settings(tmp_path: Path) -> None:
    configured = create_app(make_settings(tmp_path, reconstruction={"product_cache_mib": 512}))
    default = create_app(make_settings(tmp_path))

    assert configured.state.reconstruction_products.max_bytes == 512 * MIB
    assert default.state.reconstruction_products.max_bytes == DEFAULT_CACHE_BYTES == 1024 * MIB
