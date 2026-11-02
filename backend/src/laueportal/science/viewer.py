"""The indexing results viewer's computations on a loaded dataset: the data scope, and what
the summary reports. lauelab computes; ``indexing.viewer`` serves the results.

**Scope**: ``pattern0_only`` keeps pattern rank 0 of each frame (the primary pattern), otherwise every pattern;
``min_peaks`` is the minimum number of detected peaks a frame needs (0 keeps every frame; default 4). The scope bar's
"Skip steps with peaks ≤ N" (N default 3) is ``min_peaks = N + 1``, and N = 0 is 0 (off); no minimum of indexed
assignments applies; and frames left without a selected pattern stay as frame-only records, so no location disappears
from the maps. A **step** is a frame's position in the dataset (the manifest index of a native run, the XML step order
of a converted one).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from lauelab.visualization import AXIS_OPTIONS, COLOR_MODES, DataScope, VisualizationDataset

__all__ = [
    "DataScope",
    "ColorMode",
    "DatasetCounts",
    "available_axes",
    "color_modes",
    "dataset_counts",
    "default_step",
    "scope_steps",
    "to_data_scope",
]

DEFAULT_PATTERN0_ONLY = True
DEFAULT_MIN_PEAKS = 4

SCALAR_MODES = frozenset({"n_indexed", "goodness", "rms_error", "n_patterns"})
"""Colour modes that colour by a number (a palette and a range); the others give each point an RGB colour."""
CUBIC_MODES = frozenset({"cubic_ipf", "pole_hsv"})
"""Colour modes that need a cubic crystal (lauelab refuses them otherwise)."""


def to_data_scope(*, pattern0_only: bool = DEFAULT_PATTERN0_ONLY, min_peaks: int = DEFAULT_MIN_PEAKS) -> DataScope:
    """The lauelab selection behind the viewer's scope."""

    return DataScope(
        patterns=(0,) if pattern0_only else "all",
        min_indexed=0,
        min_detected=min_peaks or None,
        unindexed_frames=True,
    )


def scope_steps(dataset: VisualizationDataset, scope: DataScope) -> np.ndarray:
    """The steps the scope keeps, in order: frames with a selected pattern, and the frame-only records."""

    mask = np.zeros(dataset.n_frames, dtype=bool)
    rows = np.flatnonzero(scope.pattern_mask(dataset))
    mask[dataset.pattern_frame_indices[rows]] = True
    if scope.includes_unindexed_frames:
        mask |= scope.unindexed_frame_mask(dataset)
    return np.flatnonzero(mask)


def default_step(dataset: VisualizationDataset, steps: np.ndarray) -> int | None:
    """The first step with a pattern, else the first step; None when the scope keeps none."""

    if not len(steps):
        return None
    with_patterns = np.isin(steps, dataset.pattern_frame_indices)
    return int(steps[np.argmax(with_patterns)] if with_patterns.any() else steps[0])


@dataclass(frozen=True)
class DatasetCounts:
    frames: int
    patterns: int
    peaks: int


def dataset_counts(dataset: VisualizationDataset) -> DatasetCounts:
    return DatasetCounts(frames=dataset.n_frames, patterns=dataset.n_patterns, peaks=len(dataset.peaks))


AXIS_INPUTS: dict[str, tuple[str, ...]] = {
    "X": ("X",),
    "Y": ("Y",),
    "Z": ("Z",),
    "H": ("Y", "Z"),
    "F": ("Y", "Z"),
    "depth": ("depth",),
    "Xlab": ("X",),
    "Ylab": ("Y",),
    "Zlab": ("Z", "depth"),
    "Hlab": ("Y", "Z", "depth"),
    "Flab": ("Y", "Z", "depth"),
}
"""What each of lauelab's map axes (``AXIS_OPTIONS``) is computed from: the sample motors and the depth (H and F
mix Y and Z; the lab frame adds the depth to Z)."""


def available_axes(dataset: VisualizationDataset) -> list[tuple[str, str]]:
    """``(value, label)`` of lauelab's map axes that have a finite value for some frame, in lauelab's order (a scan
    without depth-resolved frames has no depth, so no depth, Zlab, Hlab or Flab)."""

    positions = np.asarray(dataset.sample_positions, dtype=float)
    finite = {
        "X": np.isfinite(positions[:, 0]),
        "Y": np.isfinite(positions[:, 1]),
        "Z": np.isfinite(positions[:, 2]),
        "depth": np.isfinite(np.asarray(dataset.depths, dtype=float)),
    }
    axes = []
    for choice in AXIS_OPTIONS:
        if np.logical_and.reduce([finite[name] for name in AXIS_INPUTS[choice.value]]).any():
            axes.append((choice.value, choice.label))
    return axes


@dataclass(frozen=True)
class ColorMode:
    value: str
    label: str
    scalar: bool
    unavailable_reason: str | None


def color_modes(dataset: VisualizationDataset) -> list[ColorMode]:
    """lauelab's map colour modes, in its order, each with why this dataset cannot use it (None when it can)."""

    crystal = dataset.crystal
    modes = []
    for choice in COLOR_MODES:
        reason = None
        if choice.value in CUBIC_MODES:
            if crystal is None:
                reason = "The results hold no crystal structure."
            elif crystal.crystal_system != "cubic":
                reason = f"Needs a cubic crystal; {crystal.name or 'the structure'} is {crystal.crystal_system}."
        elif choice.value == "misorientation" and dataset.n_patterns == 0:
            reason = "Needs a reference pattern; the results hold none."
        modes.append(ColorMode(choice.value, choice.label, choice.value in SCALAR_MODES, reason))
    return modes
