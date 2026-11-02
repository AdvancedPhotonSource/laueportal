"""The results viewer's peaks and patterns tables (``GET …/peaks`` and ``…/patterns``): lauelab's
``indexed_peak_table`` and ``pattern_table``, as Arrow tables for the grid protocol's pyarrow translator
(``core.arrow_paging``).

Each is made once per dataset over every pattern (``ALL_PATTERNS``; the app keeps it with the cached dataset,
``DatasetCache.derived``), and a scope selects its rows (``ResultsTable.scope_rows``): lauelab's table for a scope is
exactly the rows of this one whose pattern the scope selects, in order, since both tables follow the dataset's
pattern (and assignment) order. A number lauelab leaves NaN is a blank (null) value; ``id`` is a row's position in
the whole table, unique and the same in every scope (the grid's row ID).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa
from lauelab.visualization import DataScope, VisualizationDataset, indexed_peak_table, pattern_table

ALL_PATTERNS = DataScope(patterns="all", min_indexed=0)
"""Every pattern: the scope the cached tables are made with."""

PEAK_TEXT_FIELDS = ("frame_id", "input_image")
PEAK_NUMBER_FIELDS = (
    "id",
    "step",
    "pattern_index",
    "h",
    "k",
    "l",
    "peak_index",
    "x_pixel",
    "y_pixel",
    "intensity",
    "integral",
    "qx",
    "qy",
    "qz",
    "rms_error_deg",
    "goodness",
    "n_peaks",
    "energy_kev",
    "error_deg",
    "predicted_intensity",
    "hwhm_x",
    "hwhm_y",
    "tilt",
    "chisq",
    "background",
    "pattern_n_indexed",
    "pattern_indexed_fraction",
)
PATTERN_TEXT_FIELDS = ("frame_id", "astar", "bstar", "cstar", "structure", "input_image")
PATTERN_NUMBER_FIELDS = (
    "id",
    "step",
    "scan_number",
    "pattern_index",
    "n_indexed",
    "n_peaks",
    "indexed_fraction",
    "rms_error_deg",
    "goodness",
    "n_patterns",
    "space_group",
    "x_um",
    "y_um",
    "z_um",
    "depth_um",
    "frame_energy_kev",
)


@dataclass(frozen=True)
class ResultsTable:
    table: pa.Table
    pattern_rows: np.ndarray
    """Each row's pattern: its row in the dataset's patterns."""

    def scope_rows(self, dataset: VisualizationDataset, scope: DataScope) -> pa.Array:
        """The rows whose pattern ``scope`` selects, in order (``core.arrow_paging``'s ``rows``)."""

        return pa.array(np.flatnonzero(scope.pattern_mask(dataset)[self.pattern_rows]), type=pa.int64())


def _numbers(values: Any) -> pa.Array:
    """A number column; NaN is null."""

    return pa.array(np.asarray(values), from_pandas=True)


def _per_frame_text(values: Sequence[Any], steps: np.ndarray) -> pa.Array:
    """Text that belongs to a frame (its ID, its input image), dictionary-encoded by step."""

    texts = pa.array([None if value is None else str(value) for value in values], type=pa.string())
    return pa.DictionaryArray.from_arrays(pa.array(steps, type=pa.int32()), texts)


def _column(table: Any, name: str, count: int) -> pa.Array:
    return _numbers(table[name]) if name in table.columns else pa.nulls(count, pa.float64())


def peak_table(dataset: VisualizationDataset) -> ResultsTable:
    """One row per indexed peak (a pattern's assignment, with its detected peak and its pattern)."""

    table = indexed_peak_table(dataset, scope=ALL_PATTERNS)
    pattern_rows = np.asarray(dataset.assignment_pattern_rows, dtype=np.int64)
    if len(table) != len(pattern_rows):  # lauelab's table is no longer every assignment in order
        raise ValueError(f"indexed_peak_table has {len(table)} rows for {len(pattern_rows)} assignments")
    count = len(table)
    steps = np.asarray(dataset.pattern_frame_indices, dtype=np.int64)[pattern_rows]
    n_peaks = np.asarray(dataset.frame_n_peaks, dtype=np.int64)[steps]
    pattern_n_indexed = np.asarray(table["pattern_n_indexed"], dtype=np.int64)
    fraction = np.divide(pattern_n_indexed, n_peaks, out=np.full(count, np.nan), where=n_peaks > 0, casting="unsafe")
    columns = {
        "id": pa.array(np.arange(count, dtype=np.int64)),
        "step": pa.array(steps),
        "frame_id": _per_frame_text(dataset.frame_ids, steps),
        "pattern_index": _numbers(table["pattern_index"]),
        "h": _numbers(table["h"]),
        "k": _numbers(table["k"]),
        "l": _numbers(table["l"]),
        "peak_index": _numbers(table["peak_index"]),
        "x_pixel": _column(table, "fit_x", count),
        "y_pixel": _column(table, "fit_y", count),
        "intensity": _column(table, "intens", count),
        "integral": _column(table, "integral", count),
        "qx": _column(table, "qhat_x", count),
        "qy": _column(table, "qhat_y", count),
        "qz": _column(table, "qhat_z", count),
        "rms_error_deg": _numbers(table["rms_error_deg"]),
        "goodness": _numbers(table["goodness"]),
        "n_peaks": pa.array(n_peaks),
        "energy_kev": _numbers(table["energy_kev"]),
        "error_deg": _numbers(table["error_deg"]),
        "predicted_intensity": _numbers(table["predicted_intensity"]),
        "input_image": _per_frame_text(dataset.input_images, steps),
        "hwhm_x": _column(table, "hwhm_x", count),
        "hwhm_y": _column(table, "hwhm_y", count),
        "tilt": _column(table, "tilt", count),
        "chisq": _column(table, "chisq", count),
        "background": _column(table, "background", count),
        "pattern_n_indexed": pa.array(pattern_n_indexed),
        "pattern_indexed_fraction": _numbers(fraction),
    }
    return ResultsTable(pa.table(columns), pattern_rows)


def _vector_text(rows: np.ndarray) -> pa.Array:
    """The text of a reciprocal vector: its three components with six significant digits."""

    return pa.array([" ".join(f"{value:.6g}" for value in row) for row in rows], type=pa.string())


def pattern_table_of(dataset: VisualizationDataset) -> ResultsTable:
    """One row per pattern."""

    table = pattern_table(dataset, scope=ALL_PATTERNS)
    count = len(dataset.pattern_indices)
    if len(table) != count:
        raise ValueError(f"pattern_table has {len(table)} rows for {count} patterns")
    pattern_rows = np.arange(count, dtype=np.int64)
    steps = np.asarray(dataset.pattern_frame_indices, dtype=np.int64)
    n_patterns = np.bincount(steps, minlength=dataset.n_frames)[steps]
    reciprocal = np.stack(
        [np.stack([np.asarray(table[f"reciprocal_{row}{column}"]) for column in range(3)], axis=1) for row in range(3)],
        axis=1,
    )
    crystal = dataset.crystal
    structure = (crystal.name or None) if crystal is not None else None
    space_group = int(crystal.space_group) if crystal is not None and crystal.space_group is not None else None
    columns = {
        "id": pa.array(pattern_rows),
        "step": pa.array(steps),
        "frame_id": _per_frame_text(dataset.frame_ids, steps),
        "scan_number": _numbers(table["scan_number"]),
        "pattern_index": _numbers(table["pattern_index"]),
        "n_indexed": _numbers(table["n_indexed"]),
        "n_peaks": pa.array(np.asarray(dataset.frame_n_peaks, dtype=np.int64)[steps]),
        "indexed_fraction": _numbers(table["indexed_fraction"]),
        "rms_error_deg": _numbers(table["rms_error_deg"]),
        "goodness": _numbers(table["goodness"]),
        "n_patterns": pa.array(n_patterns.astype(np.int64)),
        "astar": _vector_text(reciprocal[:, 0]),
        "bstar": _vector_text(reciprocal[:, 1]),
        "cstar": _vector_text(reciprocal[:, 2]),
        "structure": pa.array([structure] * count, type=pa.string()),
        "space_group": pa.array([space_group] * count, type=pa.int64()),
        "x_um": _numbers(table["x_um"]),
        "y_um": _numbers(table["y_um"]),
        "z_um": _numbers(table["z_um"]),
        "depth_um": _numbers(table["depth_um"]),
        "frame_energy_kev": _numbers(table["frame_energy_kev"]),
        "input_image": _per_frame_text(dataset.input_images, steps),
    }
    return ResultsTable(pa.table(columns), pattern_rows)
