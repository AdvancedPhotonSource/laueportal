"""The detector view's overlays (``GET …/detector/{step}/overlays``), computed by lauelab's ``prepare_detector_view``:
the step's detected peaks, each pattern's indexed peaks with their hkl and predicted positions, and, when asked, the
reflections a simulation of the pattern predicts on the detector that were not indexed (6 to 30 keV by default).

Every pattern of the step is drawn (the page toggles them), and a simulation that fails (lauelab's
``ValueError`` or ``RuntimeError``: no crystal, no atoms, the backend) does not fail the view: the other layers are
prepared without it and the failure is reported (``Overlays.simulation_error``). A view lauelab cannot prepare (no
geometry, a detector the geometry lacks) is a ``ProductError``. Positions are pixels of the frame (its region of
interest and binning applied), x across and y down.

lauelab's view of a step (``prepare_view``, the slow part when it simulates) does not depend on the scope, so a
caller may keep it (``indexing.detector.prepared_view``); the scope is applied after (``detector_overlays``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from lauelab.visualization import DataScope, DetectorViewData, VisualizationDataset, prepare_detector_view

from laueportal.science.products import PatternInfo, ProductError, step_patterns


@dataclass(frozen=True)
class DetectedPeak:
    peak_index: int
    x: float | None
    y: float | None
    intensity: float | None
    indexed: bool
    """A pattern of the step indexes it."""


@dataclass(frozen=True)
class IndexedPeak:
    peak_index: int
    """The detected peak it indexes."""
    hkl: tuple[int, int, int]
    x: float | None
    """Where the pattern predicts it (null where the detector cannot place it)."""
    y: float | None


@dataclass(frozen=True)
class SimulatedReflection:
    hkl: tuple[int, int, int]
    x: float
    y: float
    energy_kev: float
    relative_intensity: float


@dataclass(frozen=True)
class PatternOverlay:
    pattern: PatternInfo
    indexed: tuple[IndexedPeak, ...]
    simulated: tuple[SimulatedReflection, ...] | None
    """None without a simulation (not asked, or failed)."""


@dataclass(frozen=True)
class Overlays:
    step: int
    frame_id: Any
    in_scope: bool
    """The scope keeps the step."""
    detector_id: str | None
    width: int
    height: int
    """The frame's size in pixels."""
    sample_position: tuple[float | None, float | None, float | None]
    detected: tuple[DetectedPeak, ...]
    patterns: tuple[PatternOverlay, ...]
    simulation_error: str | None


def _number(value: Any) -> float | None:
    value = float(value)
    return value if np.isfinite(value) else None


def _hkl(row: np.ndarray) -> tuple[int, int, int]:
    return int(row[0]), int(row[1]), int(row[2])


@dataclass(frozen=True)
class PreparedView:
    """lauelab's view of a step, with its simulation when asked: what does not depend on the scope."""

    step: int
    view: DetectorViewData
    energy_range_kev: tuple[float, float] | None
    """The simulation's window; None: no simulation was asked for."""
    simulation_error: str | None
    """Why the simulation failed (the view is then prepared without it); None when none failed."""


def prepare_view(
    dataset: VisualizationDataset, step: int, *, energy_range_kev: tuple[float, float] | None
) -> PreparedView:
    """lauelab's view of ``step`` (a frame of the dataset), with a simulation in ``energy_range_kev`` (None: none)."""

    frame_id = dataset.frame_ids[step]
    simulation_error = None
    try:
        view = prepare_detector_view(
            dataset, frame_id=frame_id, patterns="all", simulation_energy_range_kev=energy_range_kev
        )
    except (ValueError, RuntimeError) as error:
        if energy_range_kev is None:
            raise ProductError(str(error)) from error
        simulation_error = str(error) or type(error).__name__
        try:
            view = prepare_detector_view(dataset, frame_id=frame_id, patterns="all")
        except (ValueError, KeyError) as second:
            raise ProductError(str(second)) from second
    except KeyError as error:
        raise ProductError(str(error)) from error
    return PreparedView(step, view, energy_range_kev, simulation_error)


def detector_overlays(dataset: VisualizationDataset, scope: DataScope, prepared: PreparedView) -> Overlays:
    """The overlays of ``prepared``'s step within ``scope``."""

    step, view = prepared.step, prepared.view
    frame_id = dataset.frame_ids[step]
    in_scope, infos = step_patterns(dataset, scope, step)
    by_index = {info.pattern_index: info for info in infos}
    simulated = {item.pattern_index: item for item in view.simulations}
    patterns = []
    for pattern in view.patterns:
        simulation = simulated.get(pattern.pattern_index)
        patterns.append(
            PatternOverlay(
                pattern=by_index[pattern.pattern_index],
                indexed=tuple(
                    IndexedPeak(int(peak), _hkl(hkl), _number(xy[0]), _number(xy[1]))
                    for peak, hkl, xy in zip(
                        pattern.measured_peak_indices, pattern.hkl, pattern.predicted_xy, strict=True
                    )
                ),
                simulated=None
                if simulation is None
                else tuple(
                    SimulatedReflection(_hkl(hkl), float(xy[0]), float(xy[1]), float(energy), float(intensity))
                    for hkl, xy, energy, intensity in zip(
                        simulation.hkl,
                        simulation.predicted_xy,
                        simulation.energy_kev,
                        simulation.relative_intensity,
                        strict=True,
                    )
                ),
            )
        )
    position = dataset.sample_positions[step]
    width, height = (int(value) for value in view.extent)
    return Overlays(
        step=step,
        frame_id=frame_id if isinstance(frame_id, str) else int(frame_id),
        in_scope=in_scope,
        detector_id=view.detector_id or None,
        width=width,
        height=height,
        sample_position=(_number(position[0]), _number(position[1]), _number(position[2])),
        detected=tuple(
            DetectedPeak(int(peak), _number(xy[0]), _number(xy[1]), _number(intensity), bool(indexed))
            for peak, xy, intensity, indexed in zip(
                view.measured_peak_indices,
                view.measured_xy,
                view.measured_intensity,
                view.measured_indexed,
                strict=True,
            )
        ),
        patterns=tuple(patterns),
        simulation_error=prepared.simulation_error,
    )
