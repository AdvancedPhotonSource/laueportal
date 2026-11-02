"""The results viewer and its products on the demo's I1 and its map (slow: native indexing, and the map's 1,024 frames).

The runs are built as ``laueportal seed demo`` builds them (``seed.demo``): I1 is created and published (with R1),
queued, and computed here in the test's process (``execute_run``) rather than on a worker; the map is made from
three of I1's staged frames and registered (``register_indexing_map``). Both embed the staged geometry.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lauelab.visualization import indexed_peak_table, pattern_table

from laueportal.app import create_app
from laueportal.jobs import executor as executors
from laueportal.jobs.rundir import RESULTS_FILENAME
from laueportal.science.cache import load_dataset
from laueportal.science.viewer import to_data_scope
from laueportal.seed import demo
from tests.api_support import PORTAL_HEADERS
from tests.run_support import FAST_POLICY, claimed
from tests.settings_support import make_database

pytestmark = pytest.mark.slow

RUNS = "/api/v1/indexing-runs"


@dataclass(frozen=True)
class Demo:
    client: TestClient
    i1: int
    i1_directory: Path
    map: int
    layout: demo.DemoLayout


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Demo]:
    root = tmp_path_factory.mktemp("viewer-demo")
    runs_root = root / "runs"
    settings, engine = make_database(root, files={"runs_root": runs_root, "roots": {"runs": root}})
    layout = demo.DemoLayout(data=demo.default_data_directory(), inputs=root / "inputs", runs_root=runs_root)
    runs = demo.create_demo_runs(engine, layout)
    claimed(engine, runs.indexing_job_id)  # queued, as the seed leaves it, and claimed as the dispatcher does
    assert executors.execute_run(runs.indexing_job_id, engine=engine, policy=FAST_POLICY) is not None
    map_id = demo.register_indexing_map(engine, layout)
    directory = layout.scan_runs / f"index_{runs.indexing_id}"
    try:
        with TestClient(create_app(settings)) as client:
            yield Demo(client, runs.indexing_id, directory, map_id, layout)
    finally:
        engine.dispose()


def test_i1_and_the_map_are_ready_results(seeded: Demo) -> None:
    i1 = seeded.client.get(f"{RUNS}/{seeded.i1}/artifact").json()
    indexing_map = seeded.client.get(f"{RUNS}/{seeded.map}/artifact").json()

    # I1's results are the canonical output.h5 its run wrote; the map's the registered file.
    assert (i1["state"], i1["results_path"]) == ("ready", str(seeded.i1_directory / RESULTS_FILENAME))
    assert (indexing_map["state"], indexing_map["results_path"]) == ("ready", str(map_results(seeded)))


@pytest.mark.parametrize(
    ("scope", "steps"),
    [
        pytest.param({}, [0, 1, 3], id="default"),  # step 2 has fewer than 4 peaks
        pytest.param({"min_peaks": 0}, [0, 1, 2, 3], id="every-step"),
    ],
)
def test_i1s_summary_within_the_scope(seeded: Demo, scope: dict[str, int], steps: list[int]) -> None:
    body = seeded.client.get(f"{RUNS}/{seeded.i1}/summary", params=scope).json()

    assert body["counts"] == {"frames": 4, "patterns": 4, "peaks": 97}
    assert (body["steps"], body["default_step"]) == (steps, 0)
    assert [axis["value"] for axis in body["axes"]] == ["X", "Y", "Z", "H", "F", "Xlab", "Ylab"]  # raw frames
    assert all(mode["available"] for mode in body["color_modes"])
    assert (body["structure"], body["space_group"]) == ("Ni", 225)
    geometry = str(seeded.layout.geometry_file)
    assert body["geometry"] == {
        "source": "embedded",
        "named": {"value": geometry, "path": geometry, "state": "available"},
        "recorded": None,
    }


@pytest.mark.parametrize(
    ("scope", "n_steps", "default_step"),
    [
        pytest.param({}, 979, 1, id="default"),  # 45 positions have the empty frame
        pytest.param({"min_peaks": 0, "pattern0_only": "false"}, 1024, 1, id="every-step"),
    ],
)
def test_the_maps_summary_within_the_scope(
    seeded: Demo, scope: dict[str, str | int], n_steps: int, default_step: int
) -> None:
    body = seeded.client.get(f"{RUNS}/{seeded.map}/summary", params=scope).json()

    assert body["counts"] == {"frames": 1024, "patterns": 1558, "peaks": 37437}
    assert (len(body["steps"]), body["default_step"]) == (n_steps, default_step)
    assert [axis["value"] for axis in body["axes"]] == [
        "X", "Y", "Z", "H", "F", "depth", "Xlab", "Ylab", "Zlab", "Hlab", "Flab",
    ]  # fmt: skip
    assert all(mode["available"] for mode in body["color_modes"])
    assert (body["structure"], body["space_group"]) == ("Ni", 225)
    geometry = str(seeded.layout.geometry_file)
    assert body["geometry"] == {
        "source": "embedded",
        "named": {"value": geometry, "path": geometry, "state": "available"},
        "recorded": None,
    }


# ---------------------------------------------------------------------------
# The products

EVERY_STEP = {"min_peaks": 0, "pattern0_only": "false"}


def run_id(seeded: Demo, name: str) -> int:
    return seeded.i1 if name == "i1" else seeded.map


def map_results(seeded: Demo) -> Path:
    return seeded.layout.registered / "map" / RESULTS_FILENAME


def arrow(response: Any) -> pa.Table:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.apache.arrow.stream"
    return pa.ipc.open_stream(response.content).read_all()


@pytest.mark.parametrize("name", ["i1", "map"])
@pytest.mark.parametrize("scope", [pytest.param({}, id="default"), pytest.param(EVERY_STEP, id="every-step")])
def test_the_map_and_the_pole_figure_hold_the_scopes_steps(
    seeded: Demo, name: str, scope: dict[str, str | int]
) -> None:
    runs = f"{RUNS}/{run_id(seeded, name)}"
    summary = seeded.client.get(f"{runs}/summary", params=scope).json()

    rgb = arrow(seeded.client.get(f"{runs}/map", params=scope))
    scalar = arrow(seeded.client.get(f"{runs}/map", params={**scope, "color": "goodness", "axes": "X,Y,Z"}))
    poles = arrow(seeded.client.get(f"{runs}/pole-figure", params=scope))

    assert rgb.schema.names == ["x", "y", "r", "g", "b", "frame_id", "pattern_index", "step", "indexed"]
    assert scalar.schema.names == ["x", "y", "z", "value", "frame_id", "pattern_index", "step", "indexed"]
    assert poles.schema.names == ["x", "y", "r", "g", "b", "frame_id", "pattern_index", "step"]
    steps = rgb["step"].to_numpy()
    assert sorted(set(steps.tolist())) == summary["steps"]  # the map shows every step the scope keeps
    assert scalar["step"].to_pylist() == rgb["step"].to_pylist()
    patterns = int((rgb["pattern_index"].to_numpy() >= 0).sum())
    if scope:  # every pattern, then a record per step without one
        assert patterns == summary["counts"]["patterns"]
    else:  # pattern 0 or a frame-only record per step
        assert len(steps) == len(summary["steps"])
    assert poles.num_rows > 0
    assert set(poles["step"].to_pylist()) <= set(summary["steps"])


@pytest.mark.parametrize("name", ["i1", "map"])
def test_a_steps_details_a_selections_statistics_and_the_legend(seeded: Demo, name: str) -> None:
    runs = f"{RUNS}/{run_id(seeded, name)}"
    step = seeded.client.get(f"{runs}/summary").json()["default_step"]
    poles = arrow(seeded.client.get(f"{runs}/pole-figure"))
    selected = list(dict.fromkeys(zip(poles["step"].to_pylist(), poles["pattern_index"].to_pylist(), strict=True)))[:20]

    point = seeded.client.get(f"{runs}/points/{step}", params={"axes": "X,Y,Z"})
    stats = seeded.client.post(f"{runs}/selection-stats", json={"patterns": selected}, headers=PORTAL_HEADERS)
    legend = seeded.client.get(f"{runs}/legend", params={"color": "cubic_ipf"})

    assert point.status_code == 200, point.text
    details = point.json()
    assert (details["step"], details["in_scope"]) == (step, True)
    assert [item["axis"] for item in details["coordinates"]] == ["X", "Y", "Z"]
    assert details["patterns"] and details["patterns"][0]["in_scope"]
    assert stats.status_code == 200, stats.text
    count = len(selected)
    assert (stats.json()["n_patterns"], stats.json()["n_pairs"]) == (count, count * (count - 1) // 2)
    assert stats.json()["symmetry"] == "cubic"
    assert (legend.status_code, legend.headers["content-type"]) == (200, "image/png")


# ---------------------------------------------------------------------------
# The detector view and the tables


def results_of(seeded: Demo, name: str) -> Path:
    return seeded.i1_directory / RESULTS_FILENAME if name == "i1" else map_results(seeded)


@pytest.mark.parametrize("name", ["i1", "map"])
def test_a_steps_detector_image_and_overlays_with_a_simulation(seeded: Demo, name: str) -> None:
    """Both runs' frames are on this host (the staged inputs, inside the roots). A frame outside the roots is
    ``tests/indexing/test_viewer_detector.py``'s."""
    runs = f"{RUNS}/{run_id(seeded, name)}"
    step = seeded.client.get(f"{runs}/summary").json()["default_step"]

    image = seeded.client.get(f"{runs}/detector/{step}/image")
    overlays = seeded.client.get(f"{runs}/detector/{step}/overlays", params={"simulate": "true"})

    assert overlays.status_code == 200, overlays.text
    body = overlays.json()
    assert (body["summary"]["step"], body["summary"]["in_scope"], body["simulation_error"]) == (step, True, None)
    assert body["detected"] and body["patterns"] and body["summary"]["n_indexed"] > 0
    assert all(pattern["peaks"] and pattern["simulated"] is not None for pattern in body["patterns"])
    assert body["energy_range_kev"] == [6.0, 30.0]
    assert image.status_code == 200, image.text
    assert body["summary"]["image"]["available"] and image.headers["x-frame-dtype"] == "uint16"
    assert image.headers["x-frame-shape"] == f"{body['height']},{body['width']}"
    assert len(image.content) == body["height"] * body["width"] * 2


@pytest.mark.parametrize("name", ["i1", "map"])
@pytest.mark.parametrize("scope", [pytest.param({}, id="default"), pytest.param(EVERY_STEP, id="every-step")])
def test_the_peaks_and_patterns_grids_count_lauelabs_tables_within_the_scope(
    seeded: Demo, name: str, scope: dict[str, Any]
) -> None:
    runs = f"{RUNS}/{run_id(seeded, name)}"
    data_scope = to_data_scope(
        pattern0_only=scope.get("pattern0_only", "true") == "true", min_peaks=scope.get("min_peaks", 4)
    )
    dataset = load_dataset(results_of(seeded, name))
    peaks = indexed_peak_table(dataset, scope=data_scope)
    good = int((peaks["goodness"] > 10).sum())
    model = {"goodness": {"filterType": "number", "type": "greaterThan", "filter": 10}}

    patterns = seeded.client.get(f"{runs}/patterns", params={**scope, "limit": 1000})
    page = seeded.client.get(
        f"{runs}/peaks", params={**scope, "sort": "-intensity", "filter": json.dumps(model), "limit": 500}
    )

    assert patterns.status_code == 200, patterns.text
    assert patterns.json()["total"] == len(pattern_table(dataset, scope=data_scope))
    assert page.status_code == 200, page.text
    assert page.json()["total"] == good
    intensities = [row["intensity"] for row in page.json()["rows"]]
    assert len(intensities) == min(good, 500) and intensities == sorted(intensities, reverse=True)
