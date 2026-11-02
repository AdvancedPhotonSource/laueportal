"""The results viewer's products (``indexing.products``, ``science.products``): the colour map in every colour mode, the
pole figure, the colour keys, a point's details, the selection statistics, a failing product's problem and the entity
tags, on a run made from ``tests/fixtures/test_indexing.xml`` (cubic): steps 0 to 3 with 12, 8, 4 and 3 peaks, two
patterns at step 0, none at step 3. The expected values are lauelab's, computed here. I1 and I2: ``test_viewer_demo.py``
(slow)."""

from __future__ import annotations

import json
import shutil
import struct
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, get_args, get_type_hints

import numpy as np
import pyarrow as pa
import pytest
from lauelab.analysis import cubic_ipf_key, hsv_key, lattice_params_to_reciprocal, pairwise_misorientation
from lauelab.analysis import symmetry_operations as lauelab_symmetry_operations
from lauelab.visualization import COLOR_MODES, VisualizationDataset, prepare_map, prepare_pole_figure

from laueportal.indexing.products import PRODUCT_FAILED
from laueportal.indexing.schemas import (
    MAX_SELECTION_ENTRIES,
    LegendColor,
    MapColor,
    OrientationSymmetry,
    PoleScheme,
)
from laueportal.jobs.rundir import RESULTS_FILENAME
from laueportal.science import products as science_products
from laueportal.science.cache import DatasetCache, load_dataset
from laueportal.science.viewer import to_data_scope
from tests.indexing.test_viewer import GEOMETRY, Viewer, converted, open_viewer, with_peaks

ARROW = "application/vnd.apache.arrow.stream"
EVERY_STEP = {"pattern0_only": "false", "min_peaks": 0}
"""The scope that keeps every pattern and every step."""


def results_xml(target: Path) -> Path:
    """The fixture XML with 12, 8, 4 and 3 peaks at steps 0 to 3 and no pattern at step 3."""
    tree = ET.parse(with_peaks(target, {2: 4, 3: 3}))
    indexing = tree.getroot().findall("step")[3].find("indexing")
    assert indexing is not None
    for pattern in indexing.findall("pattern"):
        indexing.remove(pattern)
    indexing.set("Npatterns", "0")
    indexing.set("Nindexed", "0")
    tree.write(target)
    return target


@dataclass
class Run:
    viewer: Viewer
    dataset: VisualizationDataset
    """The run's results as lauelab loads them, for the expected values."""

    def get(self, product: str, **params: Any) -> Any:
        return self.viewer.get(1, product, **params)

    def post(self, action: str, body: Any) -> Any:
        return self.viewer.post(1, action, body)


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    for viewer in open_viewer(tmp_path):
        directory = viewer.run_directory(1)
        results = converted(directory, source=results_xml(tmp_path / "steps.xml"))
        viewer.add_run(1, directory)
        yield Run(viewer, load_dataset(results))


def arrow(response: Any) -> tuple[pa.Table, dict[str, Any]]:
    """The Arrow table of a product (one record batch) and its metadata's ``product``."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == ARROW
    reader = pa.ipc.open_stream(response.content)
    batches = list(reader)
    assert len(batches) == 1
    table = pa.Table.from_batches(batches)
    assert table.schema.metadata is not None
    return table, json.loads(table.schema.metadata[b"product"])


def rgb8(colors: np.ndarray) -> list[list[int]]:
    """lauelab's colours as uint8, gray without one (``plot_map``)."""
    return [
        [int(np.clip(np.rint(value * 255), 0, 255)) for value in row] if np.isfinite(row).all() else [150, 150, 150]
        for row in np.asarray(colors, dtype=float).reshape(-1, 3)
    ]


def rgb_of(table: pa.Table) -> list[list[int]]:
    return [list(row) for row in zip(*(table[name].to_pylist() for name in "rgb"), strict=True)]


def g_ref(dataset: VisualizationDataset) -> np.ndarray:
    """The crystal's own reciprocal basis (rows a*, b*, c* in 1/nm with 2π): the identity orientation."""
    assert dataset.crystal is not None
    cell = dataset.crystal.cell.in_angstrom
    return lattice_params_to_reciprocal(
        cell.a / 10,
        cell.b / 10,
        cell.c / 10,
        cell.alpha,
        cell.beta,
        cell.gamma,
        space_group=dataset.crystal.space_group,
    )


def numbers(values: np.ndarray) -> str:
    return ",".join(repr(float(value)) for value in np.ravel(values))


# ---------------------------------------------------------------------------
# The colour map


@pytest.mark.parametrize(
    ("params", "columns", "steps", "patterns", "indexed"),
    [
        pytest.param(
            {},
            ["x", "y", "r", "g", "b", "frame_id", "pattern_index", "step", "indexed"],
            [0, 1, 2],
            [0, 0, 0],
            [True, True, True],
            id="default-scope",
        ),
        pytest.param(
            {**EVERY_STEP, "axes": "X,Y,Z"},
            ["x", "y", "z", "r", "g", "b", "frame_id", "pattern_index", "step", "indexed"],
            [0, 0, 1, 2, 3],
            [0, 1, 0, 0, -1],
            [True, True, True, True, False],
            id="every-step-3d",
        ),
        pytest.param(
            {"min_peaks": 5, "color": "goodness"},
            ["x", "y", "value", "frame_id", "pattern_index", "step", "indexed"],
            [0, 1],
            [0, 0],
            [True, True],
            id="min-peaks-scalar",
        ),
    ],
)
def test_the_map_has_a_record_per_selected_pattern_then_per_step_left_without_one(
    run: Run, params: dict[str, Any], columns: list[str], steps: list[int], patterns: list[int], indexed: list[bool]
) -> None:
    table, metadata = arrow(run.get("map", **params))

    assert table.schema.names == columns
    assert (table["step"].to_pylist(), table["pattern_index"].to_pylist()) == (steps, patterns)
    assert table["indexed"].to_pylist() == indexed
    assert table["frame_id"].to_pylist() == steps  # a converted XML's frames are its steps
    positions = run.dataset.sample_positions[steps]
    assert table["x"].to_pylist() == positions[:, 0].tolist()
    if "z" in columns:
        assert table["z"].to_pylist() == positions[:, 2].tolist()
        assert rgb_of(table)[-1] == [150, 150, 150]  # the frame-only record has no colour
    assert metadata["axes"] == params.get("axes", "X,H").split(",")


def _rodrigues_custom(dataset: VisualizationDataset) -> tuple[dict[str, Any], dict[str, Any]]:
    matrix = g_ref(dataset)
    return {"color": "rodrigues", "rodrigues_ref": numbers(matrix)}, {
        "color": "rodrigues",
        "rodrigues_reference_reciprocal": matrix,
    }


COLOUR_CASES: list[Any] = [
    pytest.param(lambda _: ({}, {"color": "cubic_ipf"}), id="cubic_ipf"),
    pytest.param(
        # The Z preset's tilt, roll and normal as a custom surface.
        lambda _: ({"surface": "1,0,0,0,1,0,0,0,1"}, {"color": "cubic_ipf", "surface": "Z"}),
        id="cubic_ipf-custom-surface",
    ),
    pytest.param(
        lambda _: (
            {"color": "rodrigues", "rodrigues_symmetry": "none"},
            {"color": "rodrigues", "orientation_symmetry": "none"},
        ),
        id="rodrigues-lab",
    ),
    pytest.param(
        lambda _: ({"color": "rodrigues", "rodrigues_ref": "1"}, {"color": "rodrigues", "rodrigues_reference": (1, 0)}),
        id="rodrigues-step",
    ),
    pytest.param(_rodrigues_custom, id="rodrigues-custom-g_ref"),
    pytest.param(
        lambda _: (
            {"color": "misorientation", "ref_pattern": "0,1"},
            {"color": "misorientation", "misorientation_reference": (0, 1)},
        ),
        id="misorientation",
    ),
    pytest.param(
        lambda _: (
            {"color": "pole_hsv", "pole_hkl": "1,1,0", "pole_center": "0.1,-0.2", "pole_radius": 30, "surface": "Y"},
            {
                "color": "pole_hsv",
                "pole_hkl": (1, 1, 0),
                "pole_center": (0.1, -0.2),
                "pole_color_radius_deg": 30.0,
                "surface": "Y",
            },
        ),
        id="pole_hsv",
    ),
    *(
        pytest.param(lambda _, mode=mode: ({"color": mode}, {"color": mode}), id=mode)
        for mode in ("n_indexed", "goodness", "rms_error", "n_patterns")
    ),
]


@pytest.mark.parametrize(
    ("choices", "offered"),
    [
        pytest.param(MapColor, {choice.value for choice in COLOR_MODES}, id="map-colours"),  # the summary's
        pytest.param(
            OrientationSymmetry, set(get_args(get_type_hints(prepare_map)["orientation_symmetry"])), id="symmetries"
        ),
        pytest.param(PoleScheme, set(science_products.POLE_SCHEMES), id="pole-schemes"),  # mapped to lauelab's
        pytest.param(LegendColor, set(science_products.LEGENDS), id="legends"),
    ],
)
def test_the_products_accept_exactly_lauelabs_choices(choices: type[StrEnum], offered: set[str]) -> None:
    assert {choice.value for choice in choices} == offered


@pytest.mark.parametrize("case", COLOUR_CASES)
def test_each_colour_mode_gives_lauelabs_colours(
    run: Run, case: Callable[[VisualizationDataset], tuple[dict[str, Any], dict[str, Any]]]
) -> None:
    params, arguments = case(run.dataset)
    expected = prepare_map(
        run.dataset, axes=("X", "H"), scope=to_data_scope(pattern0_only=False, min_peaks=0), **arguments
    )

    table, metadata = arrow(run.get("map", **EVERY_STEP, **params))

    assert (metadata["color"], metadata["color_kind"]) == (arguments["color"], expected.color_kind)
    assert (metadata["color_label"], metadata["symmetry"]) == (expected.color_label, expected.symmetry)
    if expected.color_kind == "rgb":
        assert rgb_of(table) == rgb8(expected.colors)
    else:
        np.testing.assert_array_equal(table["value"].to_numpy(), expected.colors)


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        pytest.param({"min": 135, "max": 145}, [135.0, 145.0], id="set"),
        pytest.param({"min": 145, "max": 135}, [135.0, 145.0], id="reversed"),
        pytest.param({"min": 135}, [130.0, 150.0], id="incomplete"),
        pytest.param({"min": 140, "max": 140}, [130.0, 150.0], id="empty"),
    ],
)
def test_a_scalar_modes_range_is_the_users_when_complete_else_the_datas(
    run: Run, limits: dict[str, float], expected: list[float]
) -> None:
    _, metadata = arrow(run.get("map", color="goodness", palette="terrain", reverse="true", **limits))

    assert (metadata["palette"], metadata["reverse"]) == ("terrain", True)
    assert (metadata["range"], metadata["auto_range"]) == (expected, [130.0, 150.0])  # steps 0 to 2's goodness


# ---------------------------------------------------------------------------
# The pole figure


@pytest.mark.parametrize(
    ("params", "arguments"),
    [
        pytest.param({}, {"color": "hsv_position"}, id="hsv"),
        pytest.param(
            {"scheme": "hsv", "radius": 30, "center": "0.1,0.2", "hkl": "1,1,1", "surface": "X", **EVERY_STEP},
            {"color": "hsv_position", "pole_color_radius_deg": 30.0, "pole_center": (0.1, 0.2), "hkl": (1, 1, 1)},
            id="hsv-every-pattern",
        ),
        pytest.param({"scheme": "ipf"}, {"color": "ipf"}, id="ipf"),
        pytest.param({"scheme": "uniform"}, {"color": "uniform"}, id="uniform"),
    ],
)
def test_the_pole_figure_is_lauelabs_within_the_scope(
    run: Run, params: dict[str, Any], arguments: dict[str, Any]
) -> None:
    scope = to_data_scope(pattern0_only=params.get("pattern0_only") != "false", min_peaks=params.get("min_peaks", 4))
    expected = prepare_pole_figure(run.dataset, scope=scope, surface=params.get("surface", "normal"), **arguments)

    table, metadata = arrow(run.get("pole-figure", **params))

    assert table.schema.names == ["x", "y", "r", "g", "b", "frame_id", "pattern_index", "step"]
    assert table["x"].to_pylist() == expected.points[:, 0].tolist()
    assert table["y"].to_pylist() == expected.points[:, 1].tolist()
    assert rgb_of(table) == rgb8(np.broadcast_to(expected.colors, (len(expected.points), 3)))
    assert table["pattern_index"].to_pylist() == expected.pattern_indices.tolist()
    assert table["step"].to_pylist() == list(expected.frame_ids)
    assert metadata == {
        "hkl": list(expected.hkl),
        "scheme": params.get("scheme", "hsv"),
        "color_kind": expected.color_kind,
        "color_radius": expected.color_radius,
        "center": list(expected.center),
    }


# ---------------------------------------------------------------------------
# The colour keys


def png_pixels(data: bytes) -> np.ndarray:
    """An 8-bit RGBA PNG without filters (what ``science.products.png`` writes) as an ``(h, w, 4)`` array; every
    chunk's CRC checked."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks: dict[bytes, bytes] = {}
    offset = 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
        assert zlib.crc32(kind + body) == crc, kind  # a browser rejects a chunk with a wrong CRC
        chunks[kind] = chunks.get(kind, b"") + body
        offset += 12 + length
    width, height, depth, colour, *_ = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, colour) == (8, 6)
    rows = np.frombuffer(zlib.decompress(chunks[b"IDAT"]), dtype=np.uint8).reshape(height, 1 + width * 4)
    assert not rows[:, 0].any()  # filter type 0 on every row
    return rows[:, 1:].reshape(height, width, 4)


@pytest.mark.parametrize(("color", "key"), [("cubic_ipf", cubic_ipf_key), ("pole_hsv", hsv_key)])
def test_the_legends_are_lauelabs_colour_keys_as_png(run: Run, color: str, key: Callable[..., np.ndarray]) -> None:
    response = run.get("legend", color=color)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    np.testing.assert_array_equal(png_pixels(response.content), key(resolution=256))


# ---------------------------------------------------------------------------
# A point's details


def test_a_steps_details_hold_its_patterns_and_its_coordinates_on_the_axes(run: Run) -> None:
    body = run.get("points/0", axes="X,H").json()
    table, _ = arrow(run.get("map", axes="X,H"))

    assert {key: body[key] for key in ("step", "frame_id", "sample_position", "depth", "n_peaks", "in_scope")} == {
        "step": 0,
        "frame_id": 0,
        "sample_position": run.dataset.sample_positions[0].tolist(),
        "depth": None,
        "n_peaks": 12,
        "in_scope": True,
    }
    # The default scope keeps pattern 0 only.
    assert [(item["pattern_index"], item["n_indexed"], item["in_scope"]) for item in body["patterns"]] == [
        (0, 9, True),
        (1, 3, False),
    ]
    assert body["patterns"][0]["goodness"] == 150.0
    assert [(item["axis"], item["value"]) for item in body["coordinates"]] == [
        ("X", table["x"][0].as_py()),
        ("H", table["y"][0].as_py()),
    ]


def test_a_steps_coordinates_need_only_its_own_depth(tmp_path: Path) -> None:
    source = results_xml(tmp_path / "steps.xml")
    tree = ET.parse(source)
    for step, depth in zip(tree.getroot().findall("step")[:3], ("10", "20", "30"), strict=True):
        element = step.find("depth")
        assert element is not None
        element.text = depth  # step 3 keeps no depth
    tree.write(source)
    for viewer in open_viewer(tmp_path):
        directory = viewer.run_directory(1)
        converted(directory, source=source)
        viewer.add_run(1, directory)
        placed, _ = arrow(viewer.get(1, "map", axes="X,Zlab"))  # the default scope leaves out step 3
        every_step, _ = arrow(viewer.get(1, "map", axes="X,H", **EVERY_STEP))

        with_depth = viewer.get(1, "points/0", axes="X,Zlab")
        without = viewer.get(1, "points/3", axes="X,Zlab")

        assert with_depth.status_code == 200, with_depth.text
        row = placed["step"].to_pylist().index(0)
        assert [(item["axis"], item["value"]) for item in with_depth.json()["coordinates"]] == [
            ("X", placed["x"][row].as_py()),
            ("Zlab", placed["y"][row].as_py()),
        ]
        row = every_step["step"].to_pylist().index(3)
        assert [(item["axis"], item["value"]) for item in without.json()["coordinates"]] == [
            ("X", every_step["x"][row].as_py()),
            ("Zlab", None),
        ]


def test_a_step_the_results_do_not_have_is_a_404(run: Run) -> None:
    response = run.get("points/4")

    assert response.status_code == 404
    assert response.json()["detail"] == "I1's results have steps 0 to 3, not 4"


# ---------------------------------------------------------------------------
# A selection's misorientation


def test_a_selections_statistics_are_lauelabs_pairwise_misorientation(run: Run) -> None:
    rotations = run.dataset.pattern_rotations[[0, 1, 2, 3]]  # (0, 0), (0, 1), (1, 0), (2, 0)
    _, angles = pairwise_misorientation(rotations, operations=lauelab_symmetry_operations("cubic"))

    response = run.post("selection-stats", {"patterns": [[2, 0], [0, 0], [0, 1], [1, 0], [0, 0]]})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "n_patterns": 4,  # (0, 0) is named twice
        "n_oriented": 4,
        "n_pairs": 6,
        "mean": float(np.mean(angles)),  # identical: the same lauelab calls, batched per reference
        "min": float(np.min(angles)),
        "max": float(np.max(angles)),
        "symmetry": "cubic",
    }
    one = run.post("selection-stats", {"patterns": [[1, 0]]}).json()
    assert (one["n_pairs"], one["mean"], one["min"], one["max"]) == (0, None, None, None)


@pytest.mark.parametrize(
    ("patterns", "status", "loc", "code"),
    [
        pytest.param([[0, 0], [0, 1], [1, 0]] * 700, 200, None, None, id="3-distinct-named-2100-times"),
        pytest.param([[0, 0], [0, 1], [1, 0], [2, 0]], 422, ["patterns"], "too_many_patterns", id="4-distinct"),
        pytest.param([[0, 0]] * (MAX_SELECTION_ENTRIES + 1), 422, ["patterns"], "too_long", id="too-many-entries"),
        pytest.param([[0, 0], [3, 0]], 422, ["patterns", 1], "unknown_pattern", id="unknown-pattern"),
    ],
)
def test_a_selection_has_at_most_700_distinct_oriented_patterns_of_the_results(
    run: Run,
    monkeypatch: pytest.MonkeyPatch,
    patterns: list[list[int]],
    status: int,
    loc: list[str | int] | None,
    code: str | None,
) -> None:
    assert science_products.MAX_SELECTION_PATTERNS == 700
    monkeypatch.setattr(science_products, "MAX_SELECTION_PATTERNS", 3)  # the run has four oriented patterns

    response = run.post("selection-stats", {"patterns": patterns})

    assert response.status_code == status, response.text
    if loc is not None:
        assert [(error["loc"], error["code"]) for error in response.json()["errors"]] == [(loc, code)]


# ---------------------------------------------------------------------------
# Errors and caching


@pytest.mark.parametrize(
    ("product", "params", "detail"),
    [
        pytest.param(
            "map",
            {"color": "misorientation"},
            "No map: Misorientation coloring needs a reference pattern: click one in the pole figure",
            id="no-reference",
        ),
        pytest.param(
            "map",
            {"color": "rodrigues", "rodrigues_ref": "0,0,0,0,0,0,0,0,0"},
            "No map: rodrigues_reference_reciprocal is singular",
            id="singular-g_ref",
        ),
        pytest.param(
            "map",
            {"color": "misorientation", "ref_pattern": "3,0", **EVERY_STEP},
            "No map: misorientation_reference must identify a pattern as (frame_id, pattern_index)",
            id="no-pattern",
        ),
        pytest.param("pole-figure", {"surface": "1,0,0,1,0,0,1,0,0"}, None, id="degenerate-surface"),
    ],
)
def test_a_product_lauelab_cannot_make_is_a_problem_of_its_own_type(
    run: Run, product: str, params: dict[str, Any], detail: str | None
) -> None:
    response = run.get(product, **params)

    assert response.status_code == 422, response.text
    body = response.json()
    assert (body["type"], body["title"]) == (PRODUCT_FAILED, "Product failed")
    if detail is not None:
        assert body["detail"] == detail


@pytest.mark.parametrize(
    ("product", "name", "value"),
    [
        ("map", "axes", "X"),
        ("map", "axes", "X,Q"),
        ("map", "surface", "tilted"),
        ("map", "pole_hkl", "0,0,0"),
        ("map", "pole_center", "1,nan"),
        ("map", "pole_radius", "0"),
        ("map", "rodrigues_ref", "1,2,3"),
        ("map", "ref_pattern", "1"),
        ("map", "min", "inf"),
        ("pole-figure", "hkl", "1,0"),
        ("points/0", "axes", "X,Y,Z,H"),
    ],
)
def test_a_malformed_parameter_is_a_422_that_locates_it(run: Run, product: str, name: str, value: str) -> None:
    response = run.get(product, **{name: value})

    assert response.status_code == 422, response.text
    assert [error["loc"] for error in response.json()["errors"]] == [["query", name]]


@pytest.mark.parametrize("product", ["summary", "map", "pole-figure", "points/1", "peaks", "patterns"])
def test_the_summary_and_the_products_revalidate_until_a_conversion_changes_the_results(
    tmp_path: Path, product: str
) -> None:
    for viewer in open_viewer(tmp_path):
        directory = viewer.run_directory(1, "out.xml")  # xml_only: read as it is
        viewer.add_run(1, directory)
        url = f"/api/v1/indexing-runs/1/{product}"

        first = viewer.client.get(url)
        tag = first.headers["etag"]
        datasets: DatasetCache = viewer.client.app.state.datasets
        datasets.clear()
        unchanged = viewer.client.get(url, headers={"If-None-Match": tag})
        assert (len(datasets), datasets.misses) == (0, 1)  # the 304 loaded nothing
        converted(directory)  # what the conversion writes: the artifact is now output.h5
        after = viewer.client.get(url, headers={"If-None-Match": tag})

        assert (first.status_code, first.headers["cache-control"]) == (200, "private, no-cache")
        assert (unchanged.status_code, unchanged.content, unchanged.headers["etag"]) == (304, b"", tag)
        assert after.status_code == 200
        assert after.headers["etag"] != tag
        assert (directory / RESULTS_FILENAME).is_file()


@pytest.mark.parametrize("change", ["scope", "parameter", "geometry-edited-in-place"])
def test_a_tag_changes_with_the_scope_the_parameters_and_the_geometry_file(tmp_path: Path, change: str) -> None:
    for viewer in open_viewer(tmp_path):
        directory = viewer.run_directory(1)
        geometry = shutil.copy(GEOMETRY, directory / "geometry.xml")
        converted(directory, geometry_path=str(geometry))  # loaded with this geometry file
        viewer.add_run(1, directory)
        params: dict[str, Any] = {"color": "cubic_ipf"}
        tag = viewer.get(1, "map", **params).headers["etag"]

        if change == "scope":
            params["min_peaks"] = 0
        elif change == "parameter":
            params["color"] = "n_indexed"
        else:
            with open(geometry, "a") as file:
                file.write("\n")
        after = viewer.client.get("/api/v1/indexing-runs/1/map", params=params, headers={"If-None-Match": tag})

        assert viewer.get(1, "summary").json()["geometry"]["source"] == "file"
        assert after.status_code == 200, after.text
        assert after.headers["etag"] != tag
