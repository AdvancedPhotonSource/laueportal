"""Compare two run directories' outputs: reconstructions (``reconstruction/``: the scan catalog, every point's depth
images within lauelab's reconstruction tolerance) and LaueGo indexing results (``output.h5``: per frame, peaks,
patterns, assignments and frame records exactly). The live-against-batch parity (``tests/test_live_parity.py``),
the live LaueGo compute's tests and the demo seed's use them.

Every comparison is per input (keyed by input or frame ID, never by position): a live run takes its inputs in
arrival order. ``ordering_differences`` lists the inputs whose position changed; any other difference fails the
comparison.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

RECONSTRUCTION_TOLERANCE = (1e-12, 1e-9)
"""``(rtol, atol)`` of lauelab's reconstruction tests (``tests/test_reconstruct.py``, its CPU reference's
``comparison``)."""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def reconstruction_tolerance() -> tuple[float, float]:
    return RECONSTRUCTION_TOLERANCE


@dataclass(frozen=True)
class PathMap:
    """Moves absolute paths recorded under one prefix to another (longest prefix first)."""

    prefixes: tuple[tuple[str, str], ...]

    def __call__(self, value: Any) -> Any:
        if isinstance(value, bytes):
            return self(value.decode()).encode()
        if isinstance(value, str):
            for old, new in self.prefixes:
                if value == old or value.startswith(old + "/"):
                    return new + value[len(old) :]
            return value
        if isinstance(value, Mapping):
            return {key: self(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return type(value)(self(item) for item in value)
        return value


def ordering_differences(actual: Iterable[str], expected: Iterable[str]) -> list[tuple[str, int, int]]:
    """``(id, actual position, expected position)`` for every input whose position differs (same IDs on both sides)."""
    actual, expected = list(actual), list(expected)
    assert sorted(actual) == sorted(expected), "the inputs differ, not only their order"
    return [(key, actual.index(key), expected.index(key)) for key in actual if actual.index(key) != expected.index(key)]


# ---------------------------------------------------------------------------
# Wire reconstruction (``rec_<id>/reconstruction``)


def assert_same_reconstruction(actual: Path, expected: Path, paths: PathMap, *, rtol: float, atol: float) -> None:
    """Two ``reconstruction/`` directories: the scan catalog, then every point's depth images.

    Depths must match exactly; images, depth intensities and the computed depth intensity within
    ``rtol``/``atol`` (lauelab's reconstruction tolerance)."""
    from lauelab.reconstruct import ScanReader

    with h5py.File(actual / "scan.h5", "r") as a, h5py.File(expected / "scan.h5", "r") as e:
        assert_same_root_attributes(a, e, "scan.h5")
        assert set(a) == set(e), "scan.h5: top-level groups"
        assert set(a["parameters"]) == set(e["parameters"]), "scan.h5: parameters"
        for name, dataset in e["parameters"].items():
            assert_equal_values(a["parameters"][name][()], dataset[()], f"parameters/{name}")
        assert a["geometry/xml"][()] == e["geometry/xml"][()], "scan.h5: geometry/xml"
        assert set(a["geometry"].attrs) == set(e["geometry"].attrs) == {"path"}, "scan.h5: geometry attributes"
        assert a["geometry"].attrs["path"] == paths(e["geometry"].attrs["path"]), "scan.h5: geometry path"
    with ScanReader(actual / "scan.h5") as a, ScanReader(expected / "scan.h5") as e:
        assert a.run_status == e.run_status
        actual_points = {point.point_id: point for point in a.points}
        expected_points = {point.point_id: point for point in e.points}
        assert set(actual_points) == set(expected_points)
        for point_id, point in expected_points.items():
            mine = actual_points[point_id]
            for field in ("status", "error", "path", "scan_number", "sample_position", "energy_kev", "shape"):
                assert getattr(mine, field) == getattr(point, field), f"{point_id}.{field}"
            assert (mine.depth_bounds_um, mine.dtype) == (point.depth_bounds_um, point.dtype), point_id
            assert mine.source_path == paths(point.source_path), point_id
            with h5py.File(actual / point.path, "r") as a_point, h5py.File(expected / point.path, "r") as e_point:
                assert_same_root_attributes(a_point, e_point, point.path)
            with a.point(point_id) as mine_reader, e.point(point_id) as reader:
                np.testing.assert_array_equal(mine_reader.depth_um, reader.depth_um, err_msg=f"{point_id} depths")
                assert mine_reader.settings == paths(reader.settings), point_id
                images = np.stack([mine_reader.frame(i) for i in range(reader.shape[0])])
                reference = np.stack([reader.frame(i) for i in range(reader.shape[0])])
                assert images.dtype == reference.dtype, point_id
                np.testing.assert_allclose(images, reference, rtol=rtol, atol=atol, err_msg=f"{point_id} images")
                for name in ("depth_intensity", "computed_depth_intensity"):
                    np.testing.assert_allclose(
                        getattr(mine_reader, name)(), getattr(reader, name)(), rtol=rtol, atol=atol, err_msg=name
                    )


# ---------------------------------------------------------------------------
# LaueGo indexing (``index_<id>/output.h5``)

FRAME_TIMINGS = ("frames/indexing_seconds", "frames/peaksearch_seconds")
"""Wall-clock timings: they differ from run to run."""
FRAME_PATHS = ("frames/input_images",)
"""Paths of the inputs: compared after the ``PathMap``."""
OFFSETS = ("frames/peak_offsets", "frames/pattern_offsets", "patterns/assignment_offsets")
"""Positions in the flat tables: they change with the frame order; the per-frame slices are compared."""


def results_by_frame(path: Path) -> dict[str, dict[str, np.ndarray]]:
    """``output.h5`` cut per frame: ``{frame_id: {"frames/<name>": row, "peaks/<name>": rows,
    "patterns/<name>": rows, "assignments/<name>": rows}}`` (peak indices are per frame already)."""
    with h5py.File(path, "r") as file:
        frames = file["frames"]
        frame_ids = [value.decode() for value in frames["frame_ids"][()]]
        peak_offsets = frames["peak_offsets"][()]
        pattern_offsets = frames["pattern_offsets"][()]
        assignment_offsets = file["patterns/assignment_offsets"][()]
        tables = {
            group: {name: dataset[()] for name, dataset in file[group].items() if f"{group}/{name}" not in OFFSETS}
            for group in ("frames", "peaks", "patterns", "assignments")
        }
    by_frame: dict[str, dict[str, np.ndarray]] = {}
    for row, frame_id in enumerate(frame_ids):
        first_pattern, end_pattern = pattern_offsets[row], pattern_offsets[row + 1]
        rows = {
            "frames": slice(row, row + 1),
            "peaks": slice(peak_offsets[row], peak_offsets[row + 1]),
            "patterns": slice(first_pattern, end_pattern),
            "assignments": slice(assignment_offsets[first_pattern], assignment_offsets[end_pattern]),
        }
        by_frame[frame_id] = {
            f"{group}/{name}": values[rows[group]]
            for group, columns in tables.items()
            for name, values in columns.items()
        }
    return by_frame


def frame_order(path: Path) -> list[str]:
    with h5py.File(path, "r") as file:
        return [value.decode() for value in file["frames/frame_ids"][()]]


def assert_equal_values(actual: Any, expected: Any, what: str) -> None:
    """Exactly equal, NaN equal to NaN, and the same dtype for arrays."""
    if isinstance(expected, np.ndarray) or isinstance(actual, np.ndarray):
        actual, expected = np.asarray(actual), np.asarray(expected)
        assert actual.dtype == expected.dtype, f"{what}: dtype {actual.dtype} != {expected.dtype}"
        np.testing.assert_array_equal(actual, expected, err_msg=what)
    elif isinstance(expected, float) and np.isnan(expected):
        assert isinstance(actual, float) and np.isnan(actual), what
    else:
        assert actual == expected, f"{what}: {actual!r} != {expected!r}"


ROOT_VARIES = ("created",)
"""Root attributes that differ by design: when the file was written. Every other root attribute is compared
(``format``, ``version``, ``status``, ``lauelab_version``, ``native_version``, ``program``, a point file's
``file_name``/``file_time``)."""


def assert_same_root_attributes(
    actual: h5py.File, expected: h5py.File, what: str, *, added: tuple[str, ...] = ()
) -> None:
    """The same root attribute names (``actual`` with ``added`` besides), and the same values and types except
    ``ROOT_VARIES``."""
    assert set(added) <= set(actual.attrs), f"{what}: root attribute names"
    assert set(actual.attrs) - set(added) == set(expected.attrs), f"{what}: root attribute names"
    for name, value in expected.attrs.items():
        if name in ROOT_VARIES:
            continue
        mine = actual.attrs[name]
        assert type(mine) is type(value), f"{what}: root attribute {name}: {type(mine)} != {type(value)}"
        assert_equal_values(mine, value, f"{what}: root attribute {name}")


def assert_same_indexing_results(
    actual: Path, expected: Path, paths: PathMap, *, merged: bool = False
) -> list[tuple[str, int, int]]:
    """Two ``output.h5`` files, frame by frame: the frame records, peaks, patterns (orientations:
    ``patterns/reciprocal``) and assignments must be **exactly** equal, as must the crystal, the geometry XML
    and the run parameters. Returns the frames whose position differs (ordering only). ``merged``: ``actual`` is
    lauelab's merge of results files (a live run's preview), whose root attributes add ``merged_sources``."""
    mine, theirs = results_by_frame(actual), results_by_frame(expected)
    assert set(mine) == set(theirs)
    for frame_id, columns in theirs.items():
        assert set(mine[frame_id]) == set(columns), frame_id
        for name, values in columns.items():
            if name in FRAME_TIMINGS:
                continue
            if name in FRAME_PATHS:
                values = np.array([paths(value) for value in values], dtype=values.dtype)
            assert_equal_values(mine[frame_id][name], values, f"{frame_id} {name}")
    with h5py.File(actual, "r") as a, h5py.File(expected, "r") as e:
        assert_same_root_attributes(a, e, "output.h5", added=("merged_sources",) if merged else ())
        assert set(a) == set(e), "output.h5: top-level groups"
        assert set(a["crystal"]) == set(e["crystal"]), "output.h5: crystal"
        for name, dataset in e["crystal"].items():
            assert_equal_values(a["crystal"][name][()], dataset[()], f"crystal/{name}")
        assert {k: v for k, v in a["crystal"].attrs.items() if k != "source"} == {
            k: v for k, v in e["crystal"].attrs.items() if k != "source"
        }
        assert a["crystal"].attrs["source"] == paths(e["crystal"].attrs["source"])
        assert a["geometry/xml"][()] == e["geometry/xml"][()]
        assert a["geometry"].attrs["path"] == paths(e["geometry"].attrs["path"])
        assert set(a["run"].attrs) == set(e["run"].attrs)
        for name, value in e["run"].attrs.items():
            assert_equal_values(a["run"].attrs[name], value, f"run/{name}")
    return ordering_differences(frame_order(actual), frame_order(expected))
