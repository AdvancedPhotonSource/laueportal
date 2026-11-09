"""A LaueMatching run with the real indexer: a small orientation database holding the grain's orientation, a forward
cache built by the run, and the same grain as LaueGo's in the results file. The released 7.2 GB database is not used."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest
from lauelab.indexing import Indexer, IndexParams, PeakParams
from scipy.spatial.transform import Rotation

pytest.importorskip("laue_index")

from laueportal.indexing.laue_matching import cache, compute  # noqa: E402
from laueportal.jobs.contract import RunRequest  # noqa: E402
from laueportal.jobs.rundir import ManifestEntry  # noqa: E402
from tests.indexing.test_laue_matching import CRYSTAL, GEOMETRY, Hooks, parameters  # noqa: E402

pytestmark = pytest.mark.slow

FRAME = Path(__file__).parents[1] / "fixtures" / "lauelab" / "synthetic_ni_grain_a.h5"


def lauego_orientation() -> np.ndarray:
    peaks = PeakParams(boxsize=18, max_rfactor=0.5, min_size=3, min_separation=20, threshold_ratio=4.0, max_peaks=200)
    index = IndexParams(kev_max_calc=17.2, kev_max_test=35.0, angle_tolerance_deg=0.1, cone_deg=72.0)
    result = Indexer(str(GEOMETRY), str(CRYSTAL), peak_params=peaks, index_params=index).index(str(FRAME))
    assert result.n_patterns == 1
    return unit_columns(result.patterns[0].reciprocal.T)


def unit_columns(matrix: np.ndarray) -> np.ndarray:
    columns = matrix / np.linalg.norm(matrix, axis=0, keepdims=True)
    left, _, right = np.linalg.svd(columns)
    return left @ right


def cubic_operators() -> np.ndarray:
    operators = []
    for permutation in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            operator = np.zeros((3, 3))
            for row, column in enumerate(permutation):
                operator[row, column] = signs[row]
            if np.linalg.det(operator) > 0:
                operators.append(operator)
    return np.array(operators)


def disorientation_deg(first: np.ndarray, second: np.ndarray) -> float:
    traces = np.trace(first.T @ second @ cubic_operators(), axis1=1, axis2=2)
    return float(np.degrees(np.arccos(np.clip((traces.max() - 1) / 2, -1, 1))))


def write_database(path: Path, orientation: np.ndarray) -> int:
    """A database of the grain's orientation, small turns of it, and random orientations (nine doubles each)."""
    near = (
        Rotation.from_rotvec(np.radians(0.2) * np.random.default_rng(1).normal(size=(200, 3))).as_matrix() @ orientation
    )
    far = Rotation.random(3000, random_state=2).as_matrix()
    rows = np.concatenate([orientation[None], near, far])
    rows.reshape(len(rows), 9).astype(np.float64).tofile(path)
    return len(rows)


def run(tmp_path: Path, name: str, values: dict[str, Any]):
    directory = tmp_path / name
    entries = [
        ManifestEntry(index=0, input_id="a", source=str(FRAME), template_index=0, scan_point=0, depth_point=None)
    ]
    request = RunRequest(
        job_id=1,
        kind=compute.LAUE_MATCHING_KIND,
        display_id="I1",
        run_directory=str(directory),
        manifest_path=str(directory / "inputs.jsonl"),
        document={"request": values, "output": {}},
        n_inputs=1,
        workers=1,
        max_in_flight=None,
    )
    hooks = Hooks()
    return compute.compute_laue_matching(request, entries, hooks), directory, hooks


def test_a_run_builds_a_cache_finds_the_grain_and_the_next_run_reuses_it(tmp_path):
    reference = lauego_orientation()
    data_root = tmp_path / "data"
    data_root.mkdir()
    n_orientations = write_database(data_root / cache.DATABASE_NAME, reference)
    values = parameters(
        data_root,
        n_orientations=n_orientations,
        forward_cache="grain",
        create_forward_cache=True,
        threshold_method="percentile",
        threshold_percentile=99.9,
        min_area=3,
        min_nr_spots=5,
        min_good_spots=2,
        num_threads=4,
    )

    built, directory, hooks = run(tmp_path, "first", values)

    assert (built.n_succeeded, built.n_failed, built.n_indexed) == (1, 0, 1), hooks.failures
    assert cache.reuse_problem(data_root / "grain", cache.key_from_parameters(values)) is None
    with h5py.File(directory / "output.h5") as results:
        assert results["run"].attrs["program"] == "laue_matching"
        found = unit_columns(results["patterns/reciprocal"][0].T)
        assert results["assignments/hkl"].shape[0] >= 5
    assert disorientation_deg(reference, found) < 0.1

    reused, again, hooks = run(tmp_path, "second", values | {"create_forward_cache": False})

    assert reused.n_indexed == 1, hooks.failures
    with h5py.File(again / "output.h5") as results:
        assert disorientation_deg(reference, unit_columns(results["patterns/reciprocal"][0].T)) < 0.1
