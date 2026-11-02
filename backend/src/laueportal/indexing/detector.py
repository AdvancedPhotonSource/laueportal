"""The results viewer's detector view: a step's detector image (``GET …/detector/{step}/image``) and
its overlays (``…/overlays``), on the dataset ``indexing.viewer`` loads.

- **The image** a step's ``inputImage`` names is located by these rules (``science.detector.locate_detector_image``: the
  value when absolute, else beside the output, in the run's data folder and under the runs root), but each candidate is
  first mapped with the run's plan (``remote.paths.portal_path``, as every path an output names) and confined to the
  file roots (``files.roots.resolve_inside_roots``): only a path that passes both is looked at, and only the file found
  is opened. The frame is read in its own type (the configured HDF5 datasets, ``beamline.detector_datasets``, then the
  first 2-D dataset; a reconstructed frame from its point file) and sent as ``science.detector.frame_bytes`` encodes it.
  An image not on this host is a 404 that says where it was looked for and why each path was not opened; each
  candidate's resolution and first stat run through ``files.probe``, keyed by the candidate, so a hung mount is a 503
  (``NotResponding``) within its timeout. The read and the copy take a turn (``IMAGES_AT_ONCE``), waited for outside the
  thread pool, as the wire viewer's depth frames do.
- **The overlays** are lauelab's (``science.overlays``), with the summary card's values and where the image is.
- **Simulations** take seconds a pattern (lauelab's, pure Python under the GIL): the route runs them one at a time
  (``SIMULATIONS_AT_ONCE``) and skips one whose client has gone while it waited, and each step's simulation is kept
  with its dataset (``prepared_view``), so a request for one being made waits for it and a revisited step reuses it.
- **Tags:** both read a file the dataset names, so their entity tag holds that file's identity (real path, size,
  modification and change times; ``image_tag``), the HDF5 datasets tried, and the dataset's identity, the step and
  the parameters. That file is known only once the dataset is loaded, so these routes load it (from the cache) before
  they revalidate; a 304 still skips reading and encoding the frame, and the simulation.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from laueportal.core import etag
from laueportal.core.problems import ProblemError
from laueportal.core.settings import Settings
from laueportal.files.probe import bounded
from laueportal.files.roots import PathNotAllowed, recorded_real_path, resolve_inside_roots, shown
from laueportal.indexing.products import ProductFailed, StepNotFound
from laueportal.indexing.schemas import (
    DetectedPeak,
    DetectorImageInfo,
    DetectorOverlays,
    DetectorPattern,
    DetectorSummary,
    IndexedPeak,
    SimulatedReflection,
)
from laueportal.indexing.viewer import ViewerDataset, ViewerSource
from laueportal.remote.paths import portal_path, run_path_map
from laueportal.science import detector, overlays
from laueportal.science.cache import DatasetCache, FileIdentity, VisualizationDataset, file_identity
from laueportal.science.products import ProductError
from laueportal.science.viewer import DataScope


class DetectorImageNotFound(ProblemError):
    def __init__(self, loaded: ViewerDataset, step: int, reason: str) -> None:
        super().__init__(404, "Detector image not found", f"I{loaded.indexing_id}, step {step}: {reason}")


@dataclass(frozen=True)
class StepImage:
    """Where a step's detector image is (``step_image``); nothing has been opened."""

    step: int
    input_image: str | None
    """The ``inputImage`` the results record."""
    path: str | None
    """The file to read: located, mapped and inside the roots; None when there is none."""
    identity: FileIdentity | None
    reason: str | None
    """Why there is no file."""
    frame: Any = None
    """A reconstructed frame's ``ScanFrame`` (its point and depth index), or None for a frame file."""


def check_step(loaded: ViewerDataset, step: int) -> None:
    if step >= loaded.dataset.n_frames:
        raise StepNotFound(loaded, step)


def step_image(source: ViewerSource, loaded: ViewerDataset, step: int, *, settings: Settings) -> StepImage:
    """Locate ``step``'s detector image (a step of the dataset), every candidate mapped and confined first."""

    dataset = loaded.dataset
    input_image = dataset.input_images[step]
    mappings = run_path_map(source.run_directory) if source.run_directory else None

    def admit(candidate: Path) -> Path | str:
        mapped = portal_path(str(candidate), mappings, resolve=recorded_real_path)  # through files.probe: a 503
        if mapped is None:
            return "not available for this run: its plan maps no such path, or it leads out of the mapped folder"
        try:
            _root, resolved = resolve_inside_roots(mapped, settings, recorded=True)  # through files.probe: a 503
        except PathNotAllowed as error:
            return error.problem.detail or error.problem.title
        bounded(resolved, lambda: _stat(resolved))  # its first stat, keyed by itself; the lookup's stats follow
        return resolved

    located = detector.locate_detector_image(
        input_image,
        xml_path=source.path,
        data_folder=source.data_folder,
        root_path=str(settings.files.runs_root),
        admit=admit,
    )
    identity = None
    reason = shown(located.warning) if located.warning is not None else None  # paths, maybe non-UTF-8 bytes
    if located.path is not None:
        try:
            identity = file_identity(located.path)
        except OSError as error:
            reason = f"{shown(located.path)} cannot be read: {error.strerror or type(error).__name__}"
    return StepImage(
        step=step,
        input_image=input_image or None,
        path=located.path if identity is not None else None,
        identity=identity,
        reason=reason,
        frame=dataset.sources[step] if dataset.sources is not None else None,
    )


def _stat(path: Path) -> None:
    """A candidate's first stat (``step_image``); what it finds, the lookup reports."""
    try:
        path.stat()
    except OSError:
        pass


def image_tag(source: ViewerSource, product: str, image: StepImage, settings: Settings, *parts: object) -> str:
    """The entity tag of a product that reads ``image``: the dataset's identity, the image file's (or why there is
    none), the HDF5 datasets tried, and ``parts`` (the step, the scope, the parameters)."""

    file = dataclasses.asdict(image.identity) if image.identity is not None else image.reason
    return etag.entity_tag(product, source.identity, file, list(settings.beamline.detector_datasets), *parts)


@dataclass(frozen=True)
class Frame:
    body: bytes
    dtype: str
    """``uint16`` or ``float32`` (``science.detector.frame_bytes``)."""
    height: int
    width: int


def image_product(loaded: ViewerDataset, image: StepImage, *, settings: Settings) -> Frame:
    """The frame of ``image``: a 404 when it is not on this host, a 422 when it cannot be read as a 2-D image."""

    if image.path is None:
        raise DetectorImageNotFound(loaded, image.step, image.reason or "no detector image")
    try:
        data, dataset = detector.read_detector_frame(
            image.path, source=image.frame, dataset_paths=settings.beamline.detector_datasets
        )
    except Exception as error:  # whatever h5py or lauelab make of the file: its message
        raise ProductFailed("detector image", ProductError(f"cannot read {shown(image.path)}: {error}")) from error
    if data.ndim != 2:
        raise ProductFailed(
            "detector image", ProductError(f"{dataset} in {shown(image.path)} is {data.ndim}-D, not 2-D")
        )
    body, dtype = detector.frame_bytes(data)
    return Frame(body=body, dtype=dtype, height=int(data.shape[0]), width=int(data.shape[1]))


def _image_info(image: StepImage, settings: Settings) -> DetectorImageInfo:
    if image.path is None:
        return DetectorImageInfo(available=False, path=None, dataset=None, reason=image.reason)
    try:
        name = detector.detector_dataset_name(
            image.path, source=image.frame, dataset_paths=settings.beamline.detector_datasets
        )
    except Exception as error:  # the image endpoint reports the same as a 422
        return DetectorImageInfo(
            available=False, path=shown(image.path), dataset=None, reason=f"cannot be read: {shown(str(error))}"
        )
    return DetectorImageInfo(available=True, path=shown(image.path), dataset=name, reason=None)


SIMULATIONS_AT_ONCE = 1
"""Simulations running at once (the overlays route's limiter, ``app.state.simulations``); the others wait outside
the thread pool. lauelab's simulation is pure Python, so simultaneous ones share one core under the GIL and each only
takes longer: four at once took 26 s each on a large run, against 4 s alone."""

SIMULATIONS_WAITING = 4
"""Simulations that may wait for a turn (``core.api.turn``); the next request is a 503 problem the detector tab shows. A
request leaves the queue when its client goes (the page aborts the previous step's on Next), so these are live viewers:
a few tabs or users, each waiting up to a simulation of a multi-pattern step (about 40 s on a large run) per request
ahead of it."""

IMAGES_AT_ONCE = 6
"""Detector images read and copied at once (the image route's limiter, ``app.state.detector_images``, as the
depth frames' ``reconstruction.viewer.FRAMES_AT_ONCE``). Each read holds the 8-16 MiB frame and
its copy as bytes; the body is gzipped and sent after the turn. Uncapped, 80 steps in flight took the thread pool's 40
threads and kept the runs list waiting 1.4 to 3.0 s on the dev stack. h5py reads one file at a time, so more at once
would only add memory, and six serve a browser's six connections to one host."""

IMAGES_WAITING = 34
"""Detector images that may wait for a turn: with ``IMAGES_AT_ONCE``, the 40 the uncapped route served at once. A
request whose client has gone (a step moved past: the page aborts it) leaves the queue unread; the next is a 503
problem with ``Retry-After``, which the page waits out."""


def _simulation(step: int, energy_range_kev: tuple[float, float]) -> tuple[str, int, tuple[float, float]]:
    return ("detector-simulation", step, energy_range_kev)


class _SimulationFailed(Exception):
    """A view whose simulation failed: returned, not kept (``DatasetCache.derived`` keeps no error)."""

    def __init__(self, prepared: overlays.PreparedView) -> None:
        super().__init__(prepared.simulation_error)
        self.prepared = prepared


def prepared_view(
    loaded: ViewerDataset, step: int, *, energy_range_kev: tuple[float, float] | None, cache: DatasetCache
) -> overlays.PreparedView:
    """``step``'s view (``science.overlays.prepare_view``), with a simulation in ``energy_range_kev`` (None: none).

    A simulation does not depend on the scope and takes seconds a pattern, so it is kept with the dataset
    (``DatasetCache.derived``, by step and window; the dataset's identity is the cache's key): a request for one being
    made waits for it, and a revisited step or another scope reuses it. One that failed is not kept, so the next
    request tries again. 422 ``product-failed`` when lauelab cannot prepare the view."""

    def simulate(dataset: VisualizationDataset) -> overlays.PreparedView:
        prepared = overlays.prepare_view(dataset, step, energy_range_kev=energy_range_kev)
        if prepared.simulation_error is not None:
            raise _SimulationFailed(prepared)
        return prepared

    try:
        if energy_range_kev is None:
            return overlays.prepare_view(loaded.dataset, step, energy_range_kev=None)
        return cache.derived(loaded.dataset, _simulation(step, energy_range_kev), simulate)
    except _SimulationFailed as failed:
        return failed.prepared
    except ProductError as error:
        raise ProductFailed("detector overlays", error) from error


def simulation_kept(
    loaded: ViewerDataset, step: int, energy_range_kev: tuple[float, float], *, cache: DatasetCache
) -> bool:
    """Whether ``step``'s simulation in ``energy_range_kev`` is kept (``prepared_view`` returns it at once)."""

    return cache.holds(loaded.dataset, _simulation(step, energy_range_kev))


def overlays_product(
    loaded: ViewerDataset, scope: DataScope, image: StepImage, prepared: overlays.PreparedView, *, settings: Settings
) -> DetectorOverlays:
    """``image``'s step's overlays (``science.overlays``) within ``scope`` with the summary card, from its
    ``prepared`` view (``prepared_view``). 422 ``product-failed`` when lauelab cannot make them."""

    try:
        view = overlays.detector_overlays(loaded.dataset, scope, prepared)
    except ProductError as error:
        raise ProductFailed("detector overlays", error) from error
    n_detected = len(view.detected)
    n_indexed = sum(peak.indexed for peak in view.detected)
    return DetectorOverlays(
        summary=DetectorSummary(
            step=view.step,
            frame_id=view.frame_id,
            in_scope=view.in_scope,
            sample_position=list(view.sample_position),
            detector_id=view.detector_id,
            n_detected=n_detected,
            n_indexed=n_indexed,
            indexed_fraction=n_indexed / n_detected if n_detected else 0.0,
            input_image=shown(image.input_image) if image.input_image else None,
            image=_image_info(image, settings),
        ),
        width=view.width,
        height=view.height,
        detected=[
            DetectedPeak(peak_index=peak.peak_index, x=peak.x, y=peak.y, intensity=peak.intensity, indexed=peak.indexed)
            for peak in view.detected
        ],
        patterns=[
            DetectorPattern(
                pattern_index=item.pattern.pattern_index,
                n_indexed=item.pattern.n_indexed,
                goodness=item.pattern.goodness,
                rms_error_deg=item.pattern.rms_error_deg,
                indexed=item.pattern.indexed,
                in_scope=item.pattern.in_scope,
                peaks=[
                    IndexedPeak(
                        peak_index=peak.peak_index, h=peak.hkl[0], k=peak.hkl[1], l=peak.hkl[2], x=peak.x, y=peak.y
                    )
                    for peak in item.indexed
                ],
                simulated=None
                if item.simulated is None
                else [
                    SimulatedReflection(
                        h=reflection.hkl[0],
                        k=reflection.hkl[1],
                        l=reflection.hkl[2],
                        x=reflection.x,
                        y=reflection.y,
                        energy_kev=reflection.energy_kev,
                        relative_intensity=reflection.relative_intensity,
                    )
                    for reflection in item.simulated
                ],
            )
            for item in view.patterns
        ],
        energy_range_kev=None if prepared.energy_range_kev is None else list(prepared.energy_range_kev),
        simulation_error=view.simulation_error,
    )
