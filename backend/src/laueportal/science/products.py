"""The results viewer's products, computed by lauelab: the colour map (``prepare_map``), the pole figure
(``prepare_pole_figure``), the colour keys (``cubic_ipf_key``, ``hsv_key``), a point's details and a selection's
pairwise misorientation (through ``misorientation_from_reference``). ``indexing.products`` serves them; the browser only
draws.

- **Arrow products** are one record batch (``arrow_stream``). Their schema metadata has one key, ``product``: a
  JSON object of the scalars the renderer needs (labels, ranges, the symmetry applied, ...).
- **Steps** are frame positions in the dataset; a pattern is ``(step, pattern_index)``.
- **Errors:** a request these results cannot answer (lauelab's ``ValueError`` or ``KeyError``, or the portal's checks)
  raises ``ProductError`` with the message the page shows as its stale-plot warning.
"""

from __future__ import annotations

import dataclasses
import json
import struct
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal, TypeVar

import numpy as np
import pyarrow as pa
from lauelab.analysis import (
    SurfaceFrame,
    cubic_ipf_key,
    hsv_key,
    lattice_params_to_reciprocal,
    misorientation_from_reference,
    symmetry_operations,
)
from lauelab.visualization import (
    AXIS_OPTIONS,
    POLE_COLOR_MODES,
    SURFACE_PRESETS,
    DataScope,
    VisualizationDataset,
    prepare_map,
    prepare_pole_figure,
)

__all__ = [
    "AXES",
    "LEGENDS",
    "MAX_SELECTION_PATTERNS",
    "POLE_SCHEMES",
    "SURFACES",
    "MapRequest",
    "PoleFigureRequest",
    "ProductError",
    "SelectionTooLarge",
    "legend_png",
    "map_stream",
    "point_details",
    "pole_figure_stream",
    "selection_stats",
    "step_patterns",
]

METADATA_KEY = "product"
"""The Arrow schema metadata key of a product's JSON scalars."""

AXES = tuple(choice.value for choice in AXIS_OPTIONS)
AXIS_LABELS = {choice.value: choice.label for choice in AXIS_OPTIONS}
"""lauelab's label of each axis (``prepare_map``'s ``axis_labels``)."""
SURFACES = tuple(choice.value for choice in SURFACE_PRESETS)
"""The APS 34-ID-E surface presets: ``normal``, ``X``, ``H``, ``Y``, ``Z``, ``F``."""
POLE_SCHEMES = {"hsv": "hsv_position", "ipf": "ipf", "uniform": "uniform"}
"""The pole figure's colour schemes (the API's names) as lauelab's ``POLE_COLOR_MODES``."""
assert set(POLE_SCHEMES.values()) == {choice.value for choice in POLE_COLOR_MODES}
LEGENDS: dict[str, Callable[..., np.ndarray]] = {"cubic_ipf": cubic_ipf_key, "pole_hsv": hsv_key}
"""The colour modes with a key image: the cubic IPF triangle and the HSV wheel; the pole
figure's ``ipf`` and ``hsv`` schemes use the same two."""
LEGEND_RESOLUTION = 256
"""The key size in pixels."""
MAX_SELECTION_PATTERNS = 700
"""The bound on a selection's pairwise misorientation: the distinct selected patterns with an orientation (the work is
quadratic in their number)."""
MISSING_RGB = (150, 150, 150)
"""The colour of a record without one (lauelab's gray, ``plot_map``)."""

Surface = str | tuple[float, ...]
"""A preset (``SURFACES``), or nine numbers: the tilt, roll and normal vectors of a custom surface frame."""
RodriguesReference = Literal["lab"] | int | tuple[float, ...]
"""The laboratory frame, a step (its pattern 0), or nine numbers: a custom G_ref (rows a*, b*, c* in 1/nm
including 2π)."""


class ProductError(ValueError):
    """A product these results cannot give for the request; the message is for the user."""


@dataclass(frozen=True)
class MapRequest:
    """The colour map's settings."""

    axes: tuple[str, ...] = ("X", "H")
    color: str = "cubic_ipf"
    surface: Surface = "normal"
    """For ``pole_hsv``, the pole figure's surface."""
    palette: str = "viridis"
    """A scalar mode's palette, echoed for the browser (it maps the values)."""
    reverse: bool = False
    limits: tuple[float | None, float | None] = (None, None)
    """A scalar mode's range as the user set it; incomplete or empty: the data's range."""
    symmetry: str = "auto"
    """The symmetry reduction of ``rodrigues`` and ``misorientation`` colours."""
    rodrigues_reference: RodriguesReference = "lab"
    reference_pattern: tuple[int, int] | None = None
    """``misorientation``'s reference: ``(step, pattern_index)``, the pole clicked in the pole figure."""
    pole_hkl: tuple[int, int, int] = (1, 0, 0)
    pole_center: tuple[float, float] = (0.0, 0.0)
    pole_radius: float = 22.5
    """Degrees."""


@dataclass(frozen=True)
class PoleFigureRequest:
    """The pole figure's settings."""

    hkl: tuple[int, int, int] = (1, 0, 0)
    surface: Surface = "normal"
    scheme: str = "hsv"
    radius: float = 22.5
    """The HSV scheme's colour radius, in degrees."""
    center: tuple[float, float] = (0.0, 0.0)
    """The HSV scheme's colour centre (a clicked pole)."""


# ---------------------------------------------------------------------------
# Shared pieces

_Prepared = TypeVar("_Prepared")


def _prepared(prepare: Callable[..., _Prepared], dataset: VisualizationDataset, **kwargs: Any) -> _Prepared:
    try:
        return prepare(dataset, **kwargs)
    except (ValueError, KeyError) as error:
        raise ProductError(str(error).strip("'\"")) from error


def surface_frame(surface: Surface) -> str | SurfaceFrame:
    """A preset's name (lauelab resolves it), or the custom frame of nine numbers."""

    if isinstance(surface, str):
        return surface
    try:
        return SurfaceFrame.from_vectors(tilt=surface[0:3], roll=surface[3:6], normal=surface[6:9], name="custom")
    except ValueError as error:
        raise ProductError(f"Custom surface: {error}") from error


def frame_id_at(dataset: VisualizationDataset, step: int) -> Any:
    """The frame identity at a step."""

    if not 0 <= step < dataset.n_frames:
        raise ProductError(f"Step must be an integer between 0 and {dataset.n_frames - 1}")
    return dataset.frame_ids[step]


def _steps(dataset: VisualizationDataset, frame_ids: Sequence[Any]) -> np.ndarray:
    positions = {frame_id: step for step, frame_id in enumerate(dataset.frame_ids)}
    return np.fromiter((positions[frame_id] for frame_id in frame_ids), dtype=np.int32, count=len(frame_ids))


def _frame_id_column(frame_ids: Sequence[Any]) -> pa.Array:
    """int64, or text when the results name frames by text (lauelab's ``FrameId``)."""

    if all(isinstance(value, int | np.integer) for value in frame_ids):
        return pa.array(np.asarray(frame_ids, dtype=np.int64), type=pa.int64())
    return pa.array([str(value) for value in frame_ids], type=pa.string())


def _rgb8(colors: np.ndarray) -> np.ndarray:
    """``(n, 3)`` colours in [0, 1] as uint8 (lauelab's rounding); a row that is not finite gets ``MISSING_RGB``."""

    colors = np.asarray(colors, dtype=float).reshape(-1, 3)
    finite = np.isfinite(colors).all(axis=1)
    rgb = np.empty(colors.shape, dtype=np.uint8)
    rgb[:] = MISSING_RGB
    rgb[finite] = np.clip(np.rint(colors[finite] * 255), 0, 255).astype(np.uint8)
    return rgb


def _finite_or_none(value: float | None) -> float | None:
    return float(value) if value is not None and np.isfinite(value) else None


def arrow_stream(columns: dict[str, Any], metadata: dict[str, Any]) -> bytes:
    """One Arrow IPC stream holding one record batch of ``columns``, with ``metadata`` as the JSON of the schema
    metadata's ``product``."""

    batch = pa.record_batch(columns)
    schema = batch.schema.with_metadata({METADATA_KEY: json.dumps(metadata, allow_nan=False)})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema) as writer:
        writer.write_batch(batch.replace_schema_metadata(schema.metadata))
    return sink.getvalue().to_pybytes()


# ---------------------------------------------------------------------------
# The colour map


def _limits(limits: tuple[float | None, float | None]) -> tuple[float, float] | None:
    """The user's range when both ends are set, finite and different, in order; else None."""

    low, high = limits
    if low is None or high is None or not (np.isfinite(low) and np.isfinite(high)) or low == high:
        return None
    return (min(low, high), max(low, high))


def map_stream(dataset: VisualizationDataset, scope: DataScope, request: MapRequest) -> bytes:
    """The colour map as Arrow: one record per selected pattern, then one per frame left without one (the
    scope's frame-only records). Columns ``x``, ``y`` (and ``z`` with three axes, float64); ``r``, ``g``, ``b``
    (uint8) for the RGB modes or ``value`` (float64, NaN without one) for the scalar ones; ``frame_id``;
    ``pattern_index`` (int32, -1 for a frame-only record); ``step`` (int32); ``indexed`` (a pattern with a finite
    orientation)."""

    color = request.color
    kwargs: dict[str, Any] = {
        "axes": request.axes,
        "color": color,
        "scope": scope,
        "surface": surface_frame(request.surface),
    }
    if color == "pole_hsv":
        kwargs.update(
            pole_hkl=request.pole_hkl, pole_center=request.pole_center, pole_color_radius_deg=request.pole_radius
        )
    if color in ("rodrigues", "misorientation"):
        kwargs["orientation_symmetry"] = request.symmetry
    if color == "rodrigues":
        reference = request.rodrigues_reference
        if isinstance(reference, int):
            kwargs["rodrigues_reference"] = (frame_id_at(dataset, reference), 0)
        elif isinstance(reference, tuple):
            kwargs["rodrigues_reference_reciprocal"] = np.asarray(reference, dtype=float).reshape(3, 3)
    if color == "misorientation":
        if request.reference_pattern is None:
            raise ProductError("Misorientation coloring needs a reference pattern: click one in the pole figure")
        step, pattern_index = request.reference_pattern
        kwargs["misorientation_reference"] = (frame_id_at(dataset, step), pattern_index)
    data = _prepared(prepare_map, dataset, **kwargs)

    columns: dict[str, Any] = {
        name: data.coordinates[:, index] for index, name in enumerate("xyz"[: len(request.axes)])
    }
    metadata: dict[str, Any] = {
        "color": color,
        "color_kind": data.color_kind,
        "color_label": data.color_label,
        "axes": list(request.axes),
        "axis_labels": list(data.axis_labels),
        "spatial_axes": data.spatial_axes,
        "symmetry": data.symmetry,
    }
    if data.color_kind == "rgb":
        rgb = _rgb8(data.colors)
        columns.update(r=rgb[:, 0], g=rgb[:, 1], b=rgb[:, 2])
    else:
        values = np.asarray(data.colors, dtype=np.float64)
        finite = values[np.isfinite(values)]
        auto = [float(finite.min()), float(finite.max())] if len(finite) else None
        chosen = _limits(request.limits)
        columns["value"] = values
        metadata.update(
            palette=request.palette,
            reverse=request.reverse,
            range=list(chosen) if chosen is not None else auto,
            auto_range=auto,
        )
    columns.update(
        frame_id=_frame_id_column(data.frame_ids),
        pattern_index=np.asarray(data.pattern_indices, dtype=np.int32),
        step=_steps(dataset, data.frame_ids),
        indexed=np.asarray(data.indexed, dtype=bool),
    )
    return arrow_stream(columns, metadata)


# ---------------------------------------------------------------------------
# The pole figure


def pole_figure_stream(dataset: VisualizationDataset, scope: DataScope, request: PoleFigureRequest) -> bytes:
    """The pole figure as Arrow: one record per pole of the HKL family of each selected pattern (the scope's
    patterns; a cubic crystal). Columns ``x``, ``y`` (float64, stereographic), ``r``, ``g``, ``b`` (uint8),
    ``frame_id``, ``pattern_index`` (int32), ``step`` (int32)."""

    data = _prepared(
        prepare_pole_figure,
        dataset,
        hkl=request.hkl,
        scope=scope,
        surface=surface_frame(request.surface),
        color=POLE_SCHEMES[request.scheme],
        pole_center=request.center,
        pole_color_radius_deg=request.radius,
    )
    count = len(data.frame_ids)
    rgb = _rgb8(np.broadcast_to(data.colors, (count, 3)) if data.colors.ndim == 1 else data.colors)
    columns = {
        "x": data.points[:, 0],
        "y": data.points[:, 1],
        "r": rgb[:, 0],
        "g": rgb[:, 1],
        "b": rgb[:, 2],
        "frame_id": _frame_id_column(data.frame_ids),
        "pattern_index": np.asarray(data.pattern_indices, dtype=np.int32),
        "step": _steps(dataset, data.frame_ids),
    }
    metadata = {
        "hkl": list(data.hkl),
        "scheme": request.scheme,
        "color_kind": data.color_kind,
        "color_radius": _finite_or_none(data.color_radius),
        "center": list(data.center),
    }
    return arrow_stream(columns, metadata)


# ---------------------------------------------------------------------------
# The colour keys


def png(rgba: np.ndarray) -> bytes:
    """An ``(h, w, 4)`` uint8 RGBA image as PNG (8-bit RGBA, no filter)."""

    height, width, channels = rgba.shape
    if channels != 4 or rgba.dtype != np.uint8:
        raise ValueError("expected an (h, w, 4) uint8 image")
    rows = np.concatenate([np.zeros((height, 1), dtype=np.uint8), rgba.reshape(height, width * 4)], axis=1)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows.tobytes(), 9))
        + chunk(b"IEND", b"")
    )


@cache
def legend_png(color: str) -> bytes:
    """The colour key of ``color`` (``LEGENDS``) as a PNG, made once per process."""

    return png(LEGENDS[color](resolution=LEGEND_RESOLUTION))


# ---------------------------------------------------------------------------
# A point's details


@dataclass(frozen=True)
class PatternInfo:
    pattern_index: int
    n_indexed: int
    goodness: float | None
    rms_error_deg: float | None
    indexed: bool
    """A finite orientation."""
    in_scope: bool
    reciprocal: tuple[tuple[float, float, float], ...] | None = None
    """Rows a*, b*, c* in beamline coordinates (1/nm, with 2*pi); None without a finite orientation."""


@dataclass(frozen=True)
class CrystalCell:
    name: str | None
    space_group: int | None
    lengths_nm: tuple[float, float, float]
    angles_deg: tuple[float, float, float]
    reference_reciprocal: tuple[tuple[float, float, float], ...]
    """The unrotated cell's rows a*, b*, c* (1/nm, with 2*pi): lauelab's reference basis."""
    symmetry: Literal["cubic", "hexagonal", "none"]
    """The proper rotations ``equivalent_settings`` come from: lauelab's ``symmetry_operations``, which knows the
    cubic and hexagonal groups."""
    equivalent_settings: tuple[tuple[tuple[int, int, int], ...], ...]
    """Each proper rotation O of the crystal as the integer matrix P = R_ref O^T R_ref^-1, so that P R is an
    equivalent setting of a pattern's rows R (a, b, c relabelled, the same lattice); the identity alone for
    ``none``."""


@dataclass(frozen=True)
class AxisValue:
    axis: str
    label: str
    value: float | None


@dataclass(frozen=True)
class PointInfo:
    step: int
    frame_id: int | str
    sample_position: tuple[float | None, float | None, float | None]
    depth: float | None
    n_peaks: int
    in_scope: bool
    patterns: tuple[PatternInfo, ...]
    coordinates: tuple[AxisValue, ...]
    crystal: CrystalCell | None = None


def _frame_coordinates(dataset: VisualizationDataset, axes: tuple[str, ...]) -> np.ndarray:
    """The map coordinates of every frame, in step order: a scope that selects no pattern and keeps every frame
    as a frame-only record."""

    data = _prepared(
        prepare_map,
        dataset,
        axes=axes,
        color="n_patterns",
        scope=DataScope(patterns=(), min_indexed=0, unindexed_frames=True),
    )
    return data.coordinates


def step_coordinates(dataset: VisualizationDataset, step: int, axes: tuple[str, ...]) -> tuple[AxisValue, ...]:
    """``step``'s coordinates on ``axes``, by lauelab (``prepare_map``, every frame as a frame-only record).

    lauelab refuses a map with a frame it cannot place: one without a depth, on the depth-based axes. The other
    frames do not move this step, so their missing depths are filled (with 0) first; an axis this step itself
    cannot be placed on (it has no depth) is null, and the others keep their values."""

    others = ~np.isfinite(dataset.depths)
    others[step] = False
    if others.any():
        dataset = dataclasses.replace(dataset, depths=np.where(others, 0.0, dataset.depths))
    values: list[float | None]
    try:
        coordinates = _frame_coordinates(dataset, axes)
        values = [_finite_or_none(coordinates[step, index]) for index in range(len(axes))]
    except ProductError:
        if np.isfinite(dataset.depths[step]):
            raise
        values = []
        for axis in axes:  # one at a time: lauelab places this step on the axes that need no depth
            try:
                values.append(_finite_or_none(_frame_coordinates(dataset, (axis, axis))[step, 0]))
            except ProductError:
                values.append(None)
    return tuple(
        AxisValue(axis=axis, label=AXIS_LABELS[axis], value=value) for axis, value in zip(axes, values, strict=True)
    )


def step_patterns(dataset: VisualizationDataset, scope: DataScope, step: int) -> tuple[bool, tuple[PatternInfo, ...]]:
    """Whether the scope keeps ``step`` (a selected pattern, or a frame-only record), and each of its patterns in the
    results' order: indexed peaks, goodness, RMS error, a finite orientation, whether the scope keeps it."""

    rows = np.flatnonzero(dataset.pattern_frame_indices == step)
    selected = scope.pattern_mask(dataset)
    kept = bool(selected[rows].any()) or bool(
        scope.includes_unindexed_frames and scope.unindexed_frame_mask(dataset)[step]
    )
    rotations = dataset.pattern_rotations[rows]
    reciprocals = dataset.pattern_reciprocals[rows]
    patterns = tuple(
        PatternInfo(
            pattern_index=int(dataset.pattern_indices[row]),
            n_indexed=int(dataset.pattern_n_indexed[row]),
            goodness=_finite_or_none(dataset.pattern_goodness[row]),
            rms_error_deg=_finite_or_none(dataset.pattern_rms_error_deg[row]),
            indexed=bool(np.isfinite(rotation).all()),
            in_scope=bool(selected[row]),
            reciprocal=(_rows(reciprocal) if np.isfinite(rotation).all() and np.isfinite(reciprocal).all() else None),
        )
        for row, rotation, reciprocal in zip(rows, rotations, reciprocals, strict=True)
    )
    return kept, patterns


def point_details(dataset: VisualizationDataset, scope: DataScope, step: int, axes: tuple[str, ...]) -> PointInfo:
    """What the hover and click cards show of a step: its frame, motor position, depth and peaks, each of its patterns
    (indexed peaks, goodness, RMS error, whether the scope keeps it), and its coordinates on ``axes``
    (``step_coordinates``: null on an axis that needs a depth it does not have). ``step`` must be a frame of the
    dataset."""

    kept, patterns = step_patterns(dataset, scope, step)
    frame_id = dataset.frame_ids[step]
    position = dataset.sample_positions[step]
    return PointInfo(
        step=step,
        frame_id=frame_id if isinstance(frame_id, str) else int(frame_id),
        sample_position=(_finite_or_none(position[0]), _finite_or_none(position[1]), _finite_or_none(position[2])),
        depth=_finite_or_none(dataset.depths[step]),
        n_peaks=int(dataset.frame_n_peaks[step]),
        in_scope=kept,
        patterns=patterns,
        coordinates=step_coordinates(dataset, step, axes),
        crystal=crystal_cell(dataset),
    )


def _rows(matrix: np.ndarray) -> tuple[tuple[float, float, float], ...]:
    """A 3x3 basis as three rows of floats."""

    return tuple((float(row[0]), float(row[1]), float(row[2])) for row in matrix)


def crystal_cell(dataset: VisualizationDataset) -> CrystalCell | None:
    """The results' nominal cell, its reference reciprocal basis and its equivalent settings (the space groups lauelab
    gives symmetry operations for: cubic 195-230, hexagonal 168-194); None without a crystal."""

    crystal = dataset.crystal
    if crystal is None:
        return None
    cell = crystal.cell.in_angstrom
    reference = np.asarray(
        lattice_params_to_reciprocal(
            cell.a / 10.0,
            cell.b / 10.0,
            cell.c / 10.0,
            cell.alpha,
            cell.beta,
            cell.gamma,
            space_group=crystal.space_group,
        ),
        dtype=float,
    )
    space_group = int(crystal.space_group)
    symmetry: Literal["cubic", "hexagonal", "none"] = (
        "cubic" if space_group >= 195 else "hexagonal" if space_group >= 168 else "none"
    )
    operations = symmetry_operations(space_group) if symmetry != "none" else np.eye(3)[np.newaxis]
    inverse = np.linalg.inv(reference)
    settings = tuple(
        tuple((int(row[0]), int(row[1]), int(row[2])) for row in np.rint(reference @ operation.T @ inverse))
        for operation in operations
    )
    return CrystalCell(
        name=crystal.name or None,
        space_group=space_group,
        lengths_nm=(cell.a / 10.0, cell.b / 10.0, cell.c / 10.0),
        angles_deg=(float(cell.alpha), float(cell.beta), float(cell.gamma)),
        reference_reciprocal=_rows(reference),
        symmetry=symmetry,
        equivalent_settings=settings,
    )


# ---------------------------------------------------------------------------
# A selection's misorientation


@dataclass(frozen=True)
class SelectionInfo:
    n_patterns: int
    n_oriented: int
    """Of ``n_patterns``, those with a finite orientation (the ones compared)."""
    n_pairs: int
    mean: float | None
    min: float | None
    max: float | None
    symmetry: Literal["cubic", "hexagonal", "none"]
    """The crystal's system when it is cubic or hexagonal (its proper rotations reduce the angles), else none."""


def unknown_patterns(dataset: VisualizationDataset, patterns: Sequence[tuple[int, int]]) -> list[int]:
    """The positions in ``patterns`` of the ``(step, pattern_index)`` pairs that are not patterns of the dataset."""

    known = set(zip(dataset.pattern_frame_indices.tolist(), dataset.pattern_indices.tolist(), strict=True))
    return [index for index, pattern in enumerate(patterns) if tuple(pattern) not in known]


class SelectionTooLarge(ValueError):
    """More than ``MAX_SELECTION_PATTERNS`` distinct selected patterns have an orientation."""

    def __init__(self, n_oriented: int) -> None:
        super().__init__(
            f"the selection has {n_oriented} patterns with an orientation; the misorientation is computed for at "
            f"most {MAX_SELECTION_PATTERNS}"
        )
        self.n_oriented = n_oriented


def pairwise_angles(rotations: np.ndarray, operations: np.ndarray | None) -> np.ndarray:
    """lauelab's ``pairwise_misorientation`` angles (degrees; pairs ``(i, j)``, ``i < j``, in its order), made by
    lauelab one reference at a time (``misorientation_from_reference``): the same ``misorientation_matrix(R_i,
    R_j)`` as its pair loop, batched, so the angles are identical (700 patterns: 0.4 s instead of 12 s)."""

    count = len(rotations)
    angles = np.empty((count, count))
    for j in range(1, count):
        angles[:j, j] = misorientation_from_reference(rotations[: j + 1], j, operations=operations)[1][:j]
    return angles[np.triu_indices(count, 1)]


def selection_stats(dataset: VisualizationDataset, patterns: Sequence[tuple[int, int]]) -> SelectionInfo:
    """The pairwise misorientation (degrees) of the selected patterns' orientations, reduced by the crystal's proper
    rotations when it is cubic or hexagonal. Every pair must be a pattern of the dataset (``unknown_patterns``);
    duplicates count once. ``SelectionTooLarge`` when more than ``MAX_SELECTION_PATTERNS`` of the distinct patterns have
    an orientation."""

    wanted = set(map(tuple, patterns))
    keys = zip(dataset.pattern_frame_indices.tolist(), dataset.pattern_indices.tolist(), strict=True)
    rows = np.asarray(sorted(row for row, key in enumerate(keys) if key in wanted), dtype=int)
    rotations = dataset.pattern_rotations[rows]
    rotations = rotations[np.isfinite(rotations).all(axis=(1, 2))]
    if len(rotations) > MAX_SELECTION_PATTERNS:
        raise SelectionTooLarge(len(rotations))
    system = dataset.crystal.crystal_system if dataset.crystal is not None else None
    symmetry = system if system == "cubic" or system == "hexagonal" else "none"
    operations = None if symmetry == "none" else symmetry_operations(symmetry)
    angles = pairwise_angles(rotations, operations) if len(rotations) >= 2 else np.empty(0)
    found = len(angles) > 0
    return SelectionInfo(
        n_patterns=len(rows),
        n_oriented=len(rotations),
        n_pairs=len(angles),
        mean=float(np.mean(angles)) if found else None,
        min=float(np.min(angles)) if found else None,
        max=float(np.max(angles)) if found else None,
        symmetry=symmetry,
    )
