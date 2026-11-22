"""LaueMatching indexing of one run: each frame is preprocessed, matched by the ``LaueMatchingCPU`` binary against the
orientation database, and filtered, one frame at a time.

The authoritative output is ``output.h5`` in the run directory, in lauelab's results format, so the results viewer
reads it as it reads a LaueGo run's. Written to a private ``.partial`` name, validated against the manifest identities
and published by rename. ``laue_matching.h5`` beside it keeps the indexer's own tables, with the columns the results
format has no place for.

What the format holds, from what LaueMatching gives:

- A **peak** is a connected component of the thresholded image: its centre of mass, and its peak and summed intensity.
  LaueMatching fits no peak profile, so the fit's widths, tilt, chi-square and background are NaN.
- A **pattern** is a kept orientation, its goodness LaueMatching's quality, ``NMatches * sqrt(Intensity)``. The indexer
  writes the reciprocal matrix with the columns a*, b*, c*; the format holds them as rows.
- An **assignment** is a predicted spot within 5 px of a peak. A predicted spot with none has no observed peak to name,
  so it is only in ``laue_matching.h5``; ``unmatched_spots`` in the run summary counts them. Its angular error is NaN:
  the indexer records only predicted positions, so the only error it could report is the rounding of its own pixel.

Registers run kind ``laue_matching_indexing``. Like every compute module, this one never imports the database or the
settings (import-linter rule 3): everything comes from the request and the hooks. ``jobs.contract`` imports it only
for this kind, since a remote compute node has no ``laue_index``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import scipy.ndimage as ndimg
from laue_index import build_info
from laue_index import indexer as laue_indexer
from laue_index.pipeline import PIPELINE_DIR
from laue_index.pipeline.laue_indexfile import energy_from_recip_and_hkl, orient_matrix_to_euler_deg
from laue_index.postprocess import PostProcessor, PostProcessResult
from laue_index.preprocess import (
    HAS_DIPLIB,
    HAS_SKIMAGE,
    apply_exclusion,
    calculate_gaussian_sigma,
    compute_background,
    filter_small_components,
    find_connected_components,
    load_background,
    save_background,
)
from laue_index.records import SOLUTION_FORMATS
from laue_index.thresholds import apply_threshold
from lauelab.indexing import (
    FrameResult,
    InputError,
    InvalidResultsFile,
    Pattern,
    ResultsWriter,
    ScanFrame,
    load_crystal,
    load_geometry,
    load_mask,
    merge_results_files,
    read_frame,
    validate_results_file,
)
from lauelab.indexing.indexer import PEAK_DTYPE
from skimage.segmentation import watershed

from laueportal.core.provenance import library_provenance
from laueportal.indexing.laue_matching.cache import (
    DATABASE_NAME,
    FORWARD_NAME,
    HKL_NAME,
    CacheKey,
    cache_folder,
    key_from_parameters,
    read_record,
    reuse_problem,
)
from laueportal.indexing.laue_matching.output import (
    CHECKPOINT_FORMAT,
    CHECKPOINT_RECORD,
    NATIVE_FILENAME,
    SEGMENTS_DIRNAME,
    SOLUTION_COLUMNS,
    SPOT_COLUMNS,
    CheckpointError,
    Segments,
    SingleOutput,
    checkpoint_identity,
    committed_segments,
    merge_native_tables,
    read_segment_failures,
    skip_committed,
)
from laueportal.indexing.laue_matching.stream import (
    DaemonFailed,
    StopRequested,
    StreamMatcher,
    gpu_evidence,
    stream_binary,
)
from laueportal.jobs.contract import RunHooks, RunOutcome, RunRequest, register_compute_function
from laueportal.jobs.rundir import (
    CATEGORY_CANCELLED,
    CATEGORY_NOT_RUN,
    RESULTS_FILENAME,
    FailureRecord,
    FailureReportWriter,
    ManifestEntry,
    discard_partial,
    partial_path,
    publish_file,
    write_json_file,
)

LAUE_MATCHING_KIND = "laue_matching_indexing"
PROGRAM = "laue_matching"
PARAMS_FILENAME = "params.txt"
BACKGROUND_FILENAME = "background.bin"
BACKGROUND_RECORD = "background.json"
"""A continued run's record of its computed background (:class:`_Background`)."""
BACKGROUND_FORMAT = "laue-matching-background"
DETECTOR_INDEX = 0
BACKGROUND_PASSES = 1
"""Median passes of a background computed from a raw frame (LaueMatching's ``NMeadianPasses``)."""
GAUSSIAN_FACTOR = 0.25
"""The matching blur's sigma as a fraction of the spot spacing (LaueMatching's ``GaussianFactor`` default)."""
MAX_PEAK_DISTANCE_PX = 5.0
"""How near a peak must be to a predicted spot to be assigned to it (as LaueMatching's own index files)."""
DEPTH_WARNING_UM = 100.0
"""Depth beyond which the geometry's missing depth term moves a spot by about half a pixel."""
STOP_POLL_SECONDS = 0.5
TERMINATE_SECONDS = 5.0
_SOLUTION = SOLUTION_FORMATS["runimage"]
_SPOT_HKL = (2, 3, 4)
_SPOT_X, _SPOT_Y = 5, 6


class _IndexerFailed(RuntimeError):
    """The binary exited with an error on one frame."""


class _StopRequested(Exception):
    """A stop was requested while the binary ran: it was ended, and its frame did not finish."""


@dataclass(frozen=True)
class _SpotParameters:
    """What ``output.h5``'s ``run`` group records of the spot finding."""

    threshold_method: str
    threshold_percentile: float
    threshold_value: float
    min_area: int
    watershed_image: bool
    background_file: str | None
    mask_file: str | None
    filter_radius: int
    gauss_sigma_max: float


@dataclass(frozen=True)
class _MatchParameters:
    """What ``output.h5``'s ``run`` group records of the matching."""

    elo: float
    ehi: float
    max_nr_laue_spots: int
    min_nr_spots: int
    min_good_spots: int
    min_intensity: float
    max_angle: float
    robust_filter: bool
    orientation_spacing: float
    forward_cache: str


@dataclass(frozen=True)
class _Files:
    """The data files a run reads, and the scratch folder of the frame in progress."""

    database: Path
    cache: Path
    forward: Path
    hkls: Path
    params: Path
    scratch: Path


@dataclass(frozen=True)
class _Prepared:
    """One frame after preprocessing. ``blurred`` is ``None`` when no spot remains, so there is nothing to match."""

    blurred: np.ndarray | None
    labels: np.ndarray
    filtered: np.ndarray
    centers: list[Any]
    threshold: float


@dataclass(frozen=True)
class _FrameOutput:
    result: FrameResult
    solutions: np.ndarray
    kept: np.ndarray
    spots: np.ndarray
    unique_labels: np.ndarray
    unmatched_spots: int


def _floats(values: Iterable[float]) -> str:
    """Values as text that reads back as the same doubles: the forward cache is keyed on them."""
    return " ".join(repr(float(value)) for value in values)


def params_text(parameters: Mapping[str, Any], files: _Files) -> str:
    """The indexer's parameter file for this run, in LaueMatching's ``Key value`` format."""
    background = parameters.get("background_file") or ""
    return "\n".join(
        (
            f"SpaceGroup {parameters['space_group']}",
            f"Symmetry {parameters['symmetry']}",
            f"LatticeParameter {_floats(parameters['lattice_parameters'])}",
            f"P_Array {_floats(parameters['p_array'])}",
            f"R_Array {_floats(parameters['r_array'])}",
            f"PxX {float(parameters['px_x'])!r}",
            f"PxY {float(parameters['px_y'])!r}",
            f"NrPxX {parameters['nr_px_x']}",
            f"NrPxY {parameters['nr_px_y']}",
            f"Elo {float(parameters['elo'])!r}",
            f"Ehi {float(parameters['ehi'])!r}",
            f"MaxNrLaueSpots {parameters['max_nr_laue_spots']}",
            f"MinNrSpots {parameters['min_nr_spots']}",
            f"MinGoodSpots {parameters['min_good_spots']}",
            f"RobustFilter {int(bool(parameters['robust_filter']))}",
            f"MinIntensity {float(parameters['min_intensity'])!r}",
            f"MinArea {parameters['min_area']}",
            f"MaxAngle {float(parameters['max_angle'])!r}",
            f"ThresholdMethod {parameters['threshold_method']}",
            f"ThresholdPercentile {float(parameters['threshold_percentile'])!r}",
            f"Threshold {float(parameters['threshold_value'])!r}",
            f"FilterRadius {parameters['filter_radius']}",
            f"NMeadianPasses {BACKGROUND_PASSES}",
            f"GaussSigmaMax {float(parameters['gauss_sigma_max'])!r}",
            f"WatershedImage {int(bool(parameters['watershed_image']))}",
            f"OrientationSpacing {float(parameters['orientation_spacing'])!r}",
            "DoFwd 0",
            f"OrientationFile {files.database}",
            f"HKLFile {files.hkls}",
            f"ForwardFile {files.forward}",
            f"BackgroundFile {background}",
            "",
        )
    )


def _resolve_files(parameters: Mapping[str, Any], run_directory: str, scratch: Path) -> _Files:
    root = parameters.get("data_root")
    if not root:
        raise RuntimeError("[laue_matching] data_root is not set")
    cache = cache_folder(root, str(parameters["forward_cache"]))
    return _Files(
        database=Path(root) / DATABASE_NAME,
        cache=cache,
        forward=cache / FORWARD_NAME,
        hkls=cache / HKL_NAME,
        params=Path(run_directory) / PARAMS_FILENAME,
        scratch=scratch,
    )


def _generate_hkls(parameters: Mapping[str, Any], files: _Files) -> None:
    """Write a new cache's reflection list with LaueMatching's generator."""
    command = [
        sys.executable,
        str(PIPELINE_DIR / "GenerateHKLs.py"),
        "-resultFileName",
        str(files.hkls),
        "-sgnum",
        str(parameters["space_group"]),
        "-sym",
        str(parameters["symmetry"]),
        "-latticeParameter",
        *_floats(parameters["lattice_parameters"]).split(),
        "-RArray",
        *_floats(parameters["r_array"]).split(),
        "-PArray",
        *_floats(parameters["p_array"]).split(),
        "-NumPxX",
        str(parameters["nr_px_x"]),
        "-NumPxY",
        str(parameters["nr_px_y"]),
        "-dx",
        repr(float(parameters["px_x"])),
        "-dy",
        repr(float(parameters["px_y"])),
        "-Elo",
        repr(float(parameters["elo"])),
        "-Ehi",
        repr(float(parameters["ehi"])),
    ]
    done = subprocess.run(command, cwd=files.scratch, capture_output=True, text=True, check=False)
    if done.returncode != 0 or not files.hkls.is_file() or files.hkls.stat().st_size == 0:
        raise RuntimeError(f"The reflection list was not generated: {done.stderr[-500:] or done.stdout[-500:]}")


def _prepare_cache(parameters: Mapping[str, Any], files: _Files, key: CacheKey, hooks: RunHooks) -> None:
    """A reused cache must exactly fit the run: a cache of another size would be rebuilt over by the indexer. A new
    cache's name must be free; a folder holding no forward file is a build that never finished, and is used again."""
    if not parameters["create_forward_cache"]:
        problem = reuse_problem(files.cache, key)
        if problem is not None:
            raise RuntimeError(f"Forward cache {files.cache.name} cannot be used: {problem}")
        return
    if files.forward.exists():
        raise RuntimeError(f"Forward cache {files.cache.name} already exists; a new cache needs a new name")
    files.cache.mkdir(parents=True, exist_ok=True)
    if not files.hkls.is_file():
        _generate_hkls(parameters, files)
    hooks.log(f"Building forward cache {files.cache.name} with the first frame")


def _frame_input(entry: ManifestEntry) -> str | ScanFrame:
    if entry.point_id is None:
        return entry.source
    if entry.depth_index is None:
        raise InputError(f"manifest entry {entry.input_id!r} names point {entry.point_id!r} but no depth")
    return ScanFrame(entry.source, entry.point_id, entry.depth_index)


def _check_full_detector(
    image: np.ndarray, processing: Mapping[str, Any], metadata: Mapping[str, Any], parameters: Mapping[str, Any]
) -> None:
    """LaueMatching's forward simulation covers the whole detector, so a frame must be the whole detector, unbinned."""
    shape = (int(parameters["nr_px_y"]), int(parameters["nr_px_x"]))
    start, group = tuple(processing.get("start", (0, 0))), tuple(processing.get("group", (1, 1)))
    if image.shape != shape or start != (0, 0) or group != (1, 1):
        raise InputError(
            f"LaueMatching needs the full detector, {shape[1]}x{shape[0]} unbinned: this frame is "
            f"{image.shape[1]}x{image.shape[0]} from {start} with binning {group}"
        )
    detector_id = metadata.get("detector_id")
    if detector_id is not None and detector_id != parameters["detector_id"]:
        raise InputError(f"frame detector {detector_id!r} is not the geometry's {parameters['detector_id']!r}")


def _prepare_frame(
    raw: np.ndarray,
    parameters: Mapping[str, Any],
    background: np.ndarray | None,
    exclusion: np.ndarray | None,
) -> _Prepared:
    """Threshold, segment and blur one frame, the steps and order of LaueMatching's ``RunImage``."""
    corrected = raw if background is None else np.maximum(raw - background, 0.0)
    thresholded, threshold = apply_threshold(
        corrected,
        method=str(parameters["threshold_method"]),
        fixed_value=float(parameters["threshold_value"]),
        percentile=float(parameters["threshold_percentile"]),
    )
    thresholded = thresholded.astype(np.float32)
    labels, boxes, areas, n_labels = find_connected_components(thresholded)
    if n_labels <= 1:
        return _Prepared(None, labels, thresholded, [], float(threshold))
    filtered, filtered_labels, centers = filter_small_components(
        thresholded, labels, boxes, areas, n_labels, min_area=int(parameters["min_area"])
    )
    if exclusion is not None:
        filtered, filtered_labels, centers, _ = apply_exclusion(filtered, filtered_labels, centers, exclusion)
    if not centers:
        return _Prepared(None, filtered_labels, filtered, [], float(threshold))
    sigma = calculate_gaussian_sigma(
        centers,
        pixel_size=float(parameters["px_x"]),
        distance=float(parameters["p_array"][2]),
        orient_spacing=float(parameters["orientation_spacing"]),
        factor=GAUSSIAN_FACTOR,
    )
    limit = float(parameters["gauss_sigma_max"])
    if limit > 0:
        sigma = min(sigma, limit)
    blurred = ndimg.gaussian_filter(filtered.astype(np.float64), sigma)
    final_labels = filtered_labels
    mask = filtered > 0
    if parameters["watershed_image"] and mask.any():
        final_labels = watershed(-blurred, markers=filtered_labels, mask=mask, connectivity=2)
    return _Prepared(blurred, final_labels, filtered, centers, float(threshold))


def _run_binary(binary: Path, files: _Files, threads: int, hooks: RunHooks) -> Path:
    """Match the frame in the scratch folder and return that folder's image path. The binary stays in this job
    process's group, so stopping the job reaches it. Raises ``_StopRequested`` when a stop ended it."""
    image = files.scratch / "frame.bin"
    log = files.scratch / "indexer.log"
    command = [str(binary), str(files.params), str(files.database), str(files.hkls), str(image), str(threads)]
    with open(log, "wb") as output:
        process = subprocess.Popen(command, cwd=files.scratch, stdout=output, stderr=subprocess.STDOUT)
    try:
        while True:
            try:
                code = process.wait(timeout=STOP_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if hooks.should_stop():
                    process.terminate()
                    try:
                        process.wait(timeout=TERMINATE_SECONDS)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise _StopRequested from None
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    if code != 0:
        tail = log.read_text(errors="replace")[-500:]
        raise _IndexerFailed(f"LaueMatchingCPU exited with code {code}: {tail}")
    return image


class _CpuMatcher:
    """``LaueMatchingCPU``: one process per frame, matching the image written to the scratch folder."""

    def __init__(self, binary: Path, files: _Files, threads: int, hooks: RunHooks) -> None:
        self.binary, self.files, self.threads, self.hooks = binary, files, threads, hooks
        self.name = "LaueMatchingCPU"
        self._columns: tuple[str, str] | None = None

    def start(self) -> None:
        pass

    def match(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        image.astype(np.float64).tofile(self.files.scratch / "frame.bin")
        indexed = _run_binary(self.binary, self.files, self.threads, self.hooks)
        solutions_path, spots_path = Path(f"{indexed}.solutions.txt"), Path(f"{indexed}.spots.txt")
        if self._columns is None and solutions_path.is_file() and spots_path.is_file():
            self._columns = (
                solutions_path.read_text().split("\n", 1)[0],
                spots_path.read_text().split("\n", 1)[0],
            )
        return _table(solutions_path, SOLUTION_COLUMNS), _table(spots_path, SPOT_COLUMNS)

    def columns(self) -> tuple[str, str] | None:
        return self._columns

    def pid(self) -> int | None:
        return None

    def clean(self) -> None:
        for name in ("frame.bin", "frame.bin.solutions.txt", "frame.bin.spots.txt"):
            (self.files.scratch / name).unlink(missing_ok=True)

    def close(self) -> None:
        pass


class _StreamAdapter:
    """``stream.StreamMatcher`` with the compute's exceptions: a stop is ``_StopRequested``."""

    def __init__(self, matcher: StreamMatcher) -> None:
        self.matcher = matcher
        self.name = matcher.name
        self.binary = matcher.binary

    def start(self) -> None:
        try:
            self.matcher.start()
        except StopRequested:
            raise _StopRequested from None

    def match(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        try:
            return self.matcher.match(image)
        except StopRequested:
            raise _StopRequested from None

    def columns(self) -> tuple[str, str] | None:
        return self.matcher.columns()

    def pid(self) -> int | None:
        return self.matcher.pid()

    def clean(self) -> None:
        pass

    def close(self) -> None:
        self.matcher.close()


def _table(path: Path, columns: int) -> np.ndarray:
    """A table the indexer wrote: its rows after the header line (none when it found nothing)."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="loadtxt: input contained no data")
        data = np.loadtxt(path, skiprows=1, ndmin=2)
    return data if data.size else np.empty((0, columns))


def _peaks(prepared: _Prepared, raw: np.ndarray, geometry: Any, depth: float | None) -> np.ndarray:
    """One ``PEAK_DTYPE`` row per connected component, in the order of ``prepared.centers``."""
    peaks = np.zeros(len(prepared.centers), dtype=PEAK_DTYPE)
    for name in ("hwhm_x", "hwhm_y", "tilt", "chisq", "background"):
        peaks[name] = np.nan
    if not prepared.centers:
        return peaks
    labels = [center[0] for center in prepared.centers]
    positions = np.array([center[1] for center in prepared.centers], dtype=np.float64)
    peaks["fit_x"] = positions[:, 0]
    peaks["fit_y"] = positions[:, 1]
    component = prepared.labels if prepared.blurred is None else _component_labels(prepared)
    peaks["intens"] = ndimg.maximum(raw, component, labels)
    peaks["integral"] = ndimg.sum_labels(raw, component, labels)
    peaks["qhat"] = geometry.pixels_to_q(positions, detector_index=DETECTOR_INDEX, depth=depth)
    return peaks


def _component_labels(prepared: _Prepared) -> np.ndarray:
    return np.where(prepared.filtered > 0, prepared.labels, 0)


def _nearest_peaks(spots: np.ndarray, peaks: np.ndarray) -> np.ndarray:
    """Each spot's nearest peak (a row of ``peaks``), or -1 when none is within ``MAX_PEAK_DISTANCE_PX``."""
    if len(peaks) == 0:
        return np.full(len(spots), -1, dtype=np.int32)
    dx = spots[:, _SPOT_X, None] - peaks["fit_x"][None, :]
    dy = spots[:, _SPOT_Y, None] - peaks["fit_y"][None, :]
    squared = dx * dx + dy * dy
    nearest = squared.argmin(axis=1).astype(np.int32)
    nearest[squared[np.arange(len(spots)), nearest] > MAX_PEAK_DISTANCE_PX**2] = -1
    return nearest


def _patterns(processed: PostProcessResult, peaks: np.ndarray) -> tuple[tuple[Pattern, ...], int]:
    """The kept orientations as results-file patterns, best first, and how many predicted spots had no peak."""
    patterns = []
    unmatched = 0
    spots = processed.filtered_spots
    orientations = np.atleast_2d(processed.filtered_orientations) if processed.filtered_orientations.size else ()
    for row in orientations:
        own = spots[spots[:, _SOLUTION.spot_grain].astype(int) == int(row[_SOLUTION.grain])]
        nearest = _nearest_peaks(own, peaks)
        found = nearest >= 0
        unmatched += int((~found).sum())
        # The indexer's columns are a*, b*, c*: Q = recip @ hkl there; the results format's rows are.
        columns = row[_SOLUTION.recip_start : _SOLUTION.recip_start + 9].reshape(3, 3)
        hkl = own[found][:, _SPOT_HKL].astype(np.int32).reshape(-1, 3)
        energies = np.array([energy_from_recip_and_hkl(columns, indices.tolist()) for indices in hkl], dtype=np.float64)
        rotation = row[_SOLUTION.om_start : _SOLUTION.om_start + 9].reshape(3, 3)
        n_indexed = len(hkl)
        patterns.append(
            Pattern(
                euler_deg=np.array(orient_matrix_to_euler_deg(rotation), dtype=np.float64),
                rotation=rotation,
                reciprocal=columns.T,
                goodness=float(row[_SOLUTION.quality]),
                rms_error_deg=float("nan"),
                hkl=hkl,
                pk_index=nearest[found],
                err_deg=np.full(n_indexed, np.nan),
                energy_kev=energies,
                pred_intens=np.full(n_indexed, np.nan),
            )
        )
    return tuple(patterns), unmatched


class _Background:
    """A run's background image: none for reconstructed frames, the user's file, or the first raw frame's median.

    ``kept`` (a run continued over allocations): the computed background is the run's, not an allocation's. The
    allocation that computes it records it in ``background.json`` (the frame it came from and the checksum of
    ``background.bin``), and every later allocation reads that one back (:meth:`restore`, checked) instead of
    computing another from its own first frame."""

    def __init__(self, parameters: Mapping[str, Any], run_directory: str, *, kept: bool = False) -> None:
        self.parameters = parameters
        self.computed_path = Path(run_directory) / BACKGROUND_FILENAME
        self.record_path = Path(run_directory) / BACKGROUND_RECORD
        self.kept = kept
        self.image: np.ndarray | None = None
        self.source: str | None = None
        self.sha256: str | None = None

    def restore(self) -> None:
        """Read the run's computed background back, when an earlier allocation recorded one: ``CheckpointError`` when
        ``background.bin`` is not the recorded one."""
        if not self.kept or self.parameters.get("background_file") or not self.record_path.exists():
            return
        try:
            record = json.loads(self.record_path.read_text(encoding="utf-8"))
            digest = _file_sha256(self.computed_path)
        except (FileNotFoundError, ValueError) as error:
            raise CheckpointError(f"the run's background cannot be read: {error}") from None
        if not isinstance(record, dict) or record.get("format") != BACKGROUND_FORMAT or record.get("sha256") != digest:
            raise CheckpointError(f"{BACKGROUND_FILENAME} is not the background {BACKGROUND_RECORD} records")
        nx, ny = int(self.parameters["nr_px_x"]), int(self.parameters["nr_px_y"])
        if self.computed_path.stat().st_size != nx * ny * 8:
            raise CheckpointError(f"{BACKGROUND_FILENAME} is not {nx}x{ny} float64 pixels")
        self.image = load_background(str(self.computed_path), nx, ny)
        self.source, self.sha256 = record.get("source"), digest

    def for_frame(self, entry: ManifestEntry, raw: np.ndarray) -> np.ndarray | None:
        if entry.point_id is not None:
            return None
        if self.image is not None:
            return self.image
        given = self.parameters.get("background_file")
        nx, ny = int(self.parameters["nr_px_x"]), int(self.parameters["nr_px_y"])
        if given:
            image = load_background(str(given), nx, ny)
            if np.count_nonzero(image) == 0:
                raise RuntimeError(f"Background file {given} is empty or is not {nx}x{ny} float64 pixels")
            self.source = str(given)
        else:
            radius = int(self.parameters["filter_radius"])
            image = compute_background(raw, filter_radius=radius, median_passes=BACKGROUND_PASSES)
            save_background(
                image,
                str(self.computed_path),
                source=entry.source,
                frame_index=0,
                filter_radius=radius,
                median_passes=BACKGROUND_PASSES,
            )
            self.source = entry.source
            if self.kept:
                self.sha256 = _file_sha256(self.computed_path)
                write_json_file(
                    self.record_path,
                    {
                        "format": BACKGROUND_FORMAT,
                        "version": 1,
                        "source": entry.source,
                        "input_id": entry.input_id,
                        "filter_radius": radius,
                        "median_passes": BACKGROUND_PASSES,
                        "sha256": self.sha256,
                    },
                    overwrite=True,  # one an allocation wrote before it ended without committing a frame with it
                )
        self.image = image
        return image


def _process_frame(
    entry: ManifestEntry,
    parameters: Mapping[str, Any],
    matcher: _CpuMatcher | _StreamAdapter,
    geometry: Any,
    background: _Background,
    exclusion: np.ndarray | None,
    processor: PostProcessor,
) -> _FrameOutput:
    started = time.perf_counter()
    frame = _frame_input(entry)
    source_image, metadata, processing = read_frame(frame)
    image = np.asarray(source_image)
    _check_full_detector(image, processing, metadata, parameters)
    depth = entry.depth if entry.depth is not None else processing.get("depth")
    raw = np.asarray(image, dtype=np.float64)
    prepared = _prepare_frame(raw, parameters, background.for_frame(entry, raw), exclusion)
    prepared_at = time.perf_counter()
    peaks = _peaks(prepared, raw, geometry, depth)
    solutions, spots = np.empty((0, SOLUTION_COLUMNS)), np.empty((0, SPOT_COLUMNS))
    processed = None
    if prepared.blurred is not None:
        solutions, spots = matcher.match(prepared.blurred)
        processed = processor(solutions, spots, prepared.labels)
    patterns: tuple[Pattern, ...] = ()
    unmatched = 0
    sorted_solutions = solutions
    kept = np.zeros(len(solutions), dtype=bool)
    unique_labels = np.zeros(len(solutions), dtype=np.int32)
    if processed is not None:
        patterns, unmatched = _patterns(processed, peaks)
        if processed.orientations_sorted.size:
            sorted_solutions = np.atleast_2d(processed.orientations_sorted)
        grains = sorted_solutions[:, _SOLUTION.grain].astype(int)
        kept = np.isin(grains, list(processed.kept_grain_nrs))
        unique_labels = np.array(
            [processed.unique_spot_info.get(grain, {}).get("unique_label_count", 0) for grain in grains], dtype=np.int32
        )
    finished = time.perf_counter()
    result = FrameResult(
        peaks=peaks,
        patterns=patterns,
        threshold_used=prepared.threshold,
        total_sum=float(raw.sum()),
        sum_above_threshold=float(prepared.filtered.sum()),
        num_above_threshold=int(np.count_nonzero(prepared.filtered)),
        peaksearch_seconds=prepared_at - started,
        indexing_seconds=finished - prepared_at,
        threshold_ratio=float("nan"),
        metadata=dict(metadata),
        input_image=entry.source,
        image_shape=(int(image.shape[0]), int(image.shape[1])),
        depth=None if depth is None else float(depth),
        source=frame if isinstance(frame, ScanFrame) else None,
    )
    return _FrameOutput(result, sorted_solutions, kept, spots, unique_labels, unmatched)


def _file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


_binary_sha256 = _file_sha256
IDENTITY_FILES = ("geometry_file", "crystal_file", "mask_file", "background_file")


def _identity(
    request: RunRequest, parameters: Mapping[str, Any], files: _Files, name: str, binary: Path, params: str
) -> dict[str, Any]:
    """What a continued run's results depend on (``output.checkpoint_identity``): the request's parameters (its paths
    as this allocation reads them), the manifest, the files read and their checksums, the orientation database's size,
    the forward cache's record, size and reflections, the parameter file and the matcher. Every allocation must find
    the same, so that no committed frame was computed with anything else. The database and the forward file (tens of
    gigabytes) are named by size and by the cache's record, not read."""
    info = build_info()
    return {
        "format": CHECKPOINT_FORMAT,
        "version": 1,
        "job_id": request.job_id,
        "n_inputs": request.n_inputs,
        "manifest_sha256": _file_sha256(request.manifest_path),
        "parameters": dict(parameters),
        "params_sha256": hashlib.sha256(params.encode("utf-8")).hexdigest(),
        "files": {
            key: {"path": str(parameters[key]), "sha256": _file_sha256(parameters[key])}
            for key in IDENTITY_FILES
            if parameters.get(key)
        },
        "orientation_database": {"path": str(files.database), "size": files.database.stat().st_size},
        "forward_cache": {
            "name": files.cache.name,
            "record_sha256": _file_sha256(files.cache / f"{FORWARD_NAME}.meta.json"),
            "forward_size": files.forward.stat().st_size,
            "hkls_sha256": _file_sha256(files.hkls),
        },
        "matcher": {
            "name": name,
            "binary_sha256": _file_sha256(binary),
            "laue_index_version": info.get("version"),
            "laue_index_c_src_sha256": info.get("c_src_sha256"),
        },
        "software": _software(),
    }


IDENTITY_PACKAGES = ("lauelab", "laue-index", "numpy", "scipy", "h5py", "scikit-image", "diplib")
"""The packages whose versions are part of a continued run's identity (with Python's and this portal code's): an
allocation run with other software ends the run checked (``CheckpointError``), never resumes under changed science."""


def _software() -> dict[str, str | None]:
    """The versions of ``IDENTITY_PACKAGES``, Python, and the checksum of this module's source (the code that computes
    every frame)."""
    from importlib import metadata

    versions: dict[str, str | None] = {"python": sys.version.split()[0]}
    for name in IDENTITY_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    versions["laue_matching_compute_sha256"] = _file_sha256(__file__)
    return versions


def _exclusion_mask(parameters: Mapping[str, Any]) -> np.ndarray | None:
    """The mask file as LaueMatching's exclusion mask; in both, ``True`` excludes a pixel's spot."""
    mask_file = parameters.get("mask_file")
    if not mask_file:
        return None
    mask = load_mask(str(mask_file))
    shape = (int(parameters["nr_px_y"]), int(parameters["nr_px_x"]))
    if mask.shape != shape:
        raise RuntimeError(f"The mask is {mask.shape[1]}x{mask.shape[0]}, and the detector {shape[1]}x{shape[0]}")
    return mask


def _writer(path: Path, parameters: Mapping[str, Any], geometry: Any, crystal: Any) -> ResultsWriter:
    return ResultsWriter(
        path,
        crystal=crystal,
        geometry=geometry,
        peak_params=_SpotParameters(  # pyright: ignore[reportArgumentType]
            threshold_method=str(parameters["threshold_method"]),
            threshold_percentile=float(parameters["threshold_percentile"]),
            threshold_value=float(parameters["threshold_value"]),
            min_area=int(parameters["min_area"]),
            watershed_image=bool(parameters["watershed_image"]),
            background_file=parameters.get("background_file"),
            mask_file=parameters.get("mask_file"),
            filter_radius=int(parameters["filter_radius"]),
            gauss_sigma_max=float(parameters["gauss_sigma_max"]),
        ),
        index_params=_MatchParameters(  # pyright: ignore[reportArgumentType]
            elo=float(parameters["elo"]),
            ehi=float(parameters["ehi"]),
            max_nr_laue_spots=int(parameters["max_nr_laue_spots"]),
            min_nr_spots=int(parameters["min_nr_spots"]),
            min_good_spots=int(parameters["min_good_spots"]),
            min_intensity=float(parameters["min_intensity"]),
            max_angle=float(parameters["max_angle"]),
            robust_filter=bool(parameters["robust_filter"]),
            orientation_spacing=float(parameters["orientation_spacing"]),
            forward_cache=str(parameters["forward_cache"]),
        ),
        detector_index=DETECTOR_INDEX,
        detector_id=str(parameters["detector_id"]),
        cosmic_filter=False,
        overwrite=True,
        program=PROGRAM,
    )


def _provenance(
    parameters: Mapping[str, Any],
    files: _Files,
    matcher: _CpuMatcher | _StreamAdapter,
    background: _Background,
    unmatched: int,
) -> dict[str, Any]:
    info = build_info()
    provenance = {
        **library_provenance(f"laue_index {info.get('version')} {matcher.name}"),
        "laue_index_version": info.get("version"),
        "laue_index_c_src_sha256": info.get("c_src_sha256"),
        "binary": str(matcher.binary),
        "binary_sha256": _binary_sha256(matcher.binary),
        "orientation_database": {"path": str(files.database), "size": files.database.stat().st_size},
        "forward_cache": {
            "name": files.cache.name,
            "created_by_this_run": bool(parameters["create_forward_cache"]),
            "record": read_record(files.cache) if (files.cache / f"{FORWARD_NAME}.meta.json").is_file() else None,
        },
        "background": background.source,
        "params_file": PARAMS_FILENAME,
        "threads": int(parameters["num_threads"]),
        "unmatched_spots": unmatched,
    }
    if isinstance(matcher, _StreamAdapter):
        provenance["laue_index_build"] = info
    return provenance


_clock = time.monotonic
"""The clock ``RunRequest.deadline`` is read on (the runner sets it from the same clock)."""
STREAM = "stream"
"""``execution.laue_matching_indexer`` of a target that matches with the GPU stream daemon."""


def _stream_matcher(
    binary: Path, files: _Files, parameters: Mapping[str, Any], scratch: Path, should_stop: Any
) -> _StreamAdapter:
    return _StreamAdapter(
        StreamMatcher(
            binary,
            params=files.params,
            database=files.database,
            hkls=files.hkls,
            threads=int(parameters["num_threads"]),
            folder=scratch / "daemon",
            solution_columns=SOLUTION_COLUMNS,
            spot_columns=SPOT_COLUMNS,
            should_stop=should_stop,
        )
    )


def compute_laue_matching(request: RunRequest, entries: Iterable[ManifestEntry], hooks: RunHooks) -> RunOutcome:
    """Index the run's frames in manifest order. On the portal's worker (no ``request.execution``): the CPU binary
    per frame, into the run's two files. On a remote target, as its execution policy says: the GPU stream daemon
    (``laue_matching_indexer: stream``), committed segments every ``segment_seconds``, and no frame started after
    ``request.deadline`` (the allocation's soft time limit).

    A run continued over PBS allocations (``request.continuation``, ``pioneer``) is one allocation's share of it: the
    run's identity is checked (``output.checkpoint_identity``), its committed segments are resumed and checked
    (``Segments.resume``, ``output.skip_committed``) and its computed background read back, and it goes on from the
    first input no segment attempted. An allocation that reaches its time limit or its frame limit
    (``max_frames_per_allocation``) with inputs left hands off (``RunOutcome.handoff``: nothing merged, failed or
    published; the launcher submits the next allocation, or fails the run when nothing was committed). A signal
    commits the open segment and ends the allocation (``RunOutcome.interrupted``) without ending the run. A stop, a
    daemon that fails, the end of the manifest and a finalizing allocation (``request.finalize``, which the launcher
    starts only after a stop: it attempts no input) end the run: every committed segment's failures are reported, the
    inputs left are not run, and the segments are merged into the run's two files once. Any error raised instead ends
    the allocation with the committed segments kept unmerged (``jobs.runner``). Without a continuation, the frames left
    at the time limit are not run."""
    if not (HAS_DIPLIB and HAS_SKIMAGE):
        raise RuntimeError("LaueMatching needs diplib and scikit-image: without them it computes different numbers")
    parameters = request.parameters
    execution = request.execution or {}
    stream = execution.get("laue_matching_indexer") == STREAM
    segment_seconds = execution.get("segment_seconds")
    continuation = request.continuation
    if continuation is not None and segment_seconds is None:
        raise RuntimeError("A run continued over allocations commits segments: its policy needs segment_seconds")
    frame_limit = None if continuation is None else int(continuation["max_frames_per_allocation"])
    finalize = continuation is not None and request.finalize
    deadline = request.deadline
    os.makedirs(request.run_directory, exist_ok=True)
    results_final = os.path.join(request.run_directory, RESULTS_FILENAME)
    if continuation is None and os.path.exists(results_final):
        raise FileExistsError(f"{results_final} already exists; a run never overwrites a previous run's results")
    native_final = os.path.join(request.run_directory, NATIVE_FILENAME)
    results_partial = partial_path(results_final)
    native_partial = partial_path(native_final)
    key = key_from_parameters(parameters)
    if stream and parameters["create_forward_cache"]:
        raise RuntimeError("A GPU run reads an installed forward cache; it never builds one")
    binary = stream_binary(laue_indexer.require_binary) if stream else laue_indexer.require_binary("CPU")
    geometry = load_geometry(str(parameters["geometry_file"]))
    crystal = load_crystal(str(parameters["crystal_file"]))
    exclusion = _exclusion_mask(parameters)
    processor = PostProcessor(
        robust=bool(parameters["robust_filter"]),
        min_unique=int(parameters["min_good_spots"]),
        min_total_spots=int(parameters["min_nr_spots"]),
        max_angle_deg=float(parameters["max_angle"]),
        space_group=int(parameters["space_group"]),
        csl_sigmas=(3,),
        csl_tol_deg=3.0,
    )

    def past_deadline() -> bool:
        return deadline is not None and _clock() >= deadline

    scratch = Path(tempfile.mkdtemp(prefix=f"laue_matching_{request.job_id}_"))
    run_warnings: list[str] = []
    succeeded = failed = n_indexed = unmatched = attempts = committed_before = 0
    stopped = time_limited = deep = False
    limit: str | None = None
    daemon_error: str | None = None
    remaining: Iterator[ManifestEntry] = iter(entries)
    background = _Background(parameters, request.run_directory, kept=continuation is not None)
    matcher: _CpuMatcher | _StreamAdapter | None = None
    sink: Segments | SingleOutput | None = None
    gpu: dict[str, Any] | None = None
    daemon_seconds: float | None = None
    identity: str | None = None
    ending = "final"
    leftover: list[ManifestEntry] = []  # a continued run's inputs not run, reported as it ends
    first_reason = rest_reason = (CATEGORY_NOT_RUN, "")

    def not_run_reason(*, during: bool = False) -> tuple[str, str]:
        """The category and message of an input not run, as the run ends."""
        if stopped:
            message = "run stopped while this input was indexed" if during else "run stopped before this input started"
            return CATEGORY_CANCELLED, message
        if time_limited:
            return CATEGORY_NOT_RUN, "the allocation's time limit came " + (
                "while this input was indexed" if during else "first"
            )
        return CATEGORY_NOT_RUN, f"not run: {daemon_error}"

    def not_run(entry: ManifestEntry, *, during: bool = False) -> FailureRecord:
        category, message = not_run_reason(during=during)
        return FailureRecord.not_run(entry, category=category, message=message)

    def report() -> None:
        if isinstance(sink, Segments):
            hooks.report_progress(succeeded=len(sink.frame_ids), failed=sink.n_failed)
        else:
            hooks.report_progress(succeeded=succeeded, failed=failed)

    def limit_reached() -> str | None:
        """Why no further input starts now: a ``stop`` (a ``signal`` on a continued run), the ``time`` limit, the
        allocation's ``frames`` limit; None when none applies."""
        if hooks.should_stop():
            return "signal" if continuation is not None and hooks.shutting_down() else "stop"
        if past_deadline():
            return "time"
        if frame_limit is not None and attempts >= frame_limit:
            return "frames"
        return None

    try:
        files = _resolve_files(parameters, request.run_directory, scratch)
        _prepare_cache(parameters, files, key, hooks)
        text = params_text(parameters, files)
        if continuation is not None and files.params.exists():
            if files.params.read_text(encoding="utf-8") != text:
                raise CheckpointError(f"{PARAMS_FILENAME} differs from the one this run's first allocation wrote")
        else:
            with open(files.params, "x", encoding="utf-8") as stream_file:
                stream_file.write(text)
        threads = int(parameters["num_threads"])
        matcher = (
            _stream_matcher(binary, files, parameters, scratch, lambda: hooks.should_stop() or past_deadline())
            if stream
            else _CpuMatcher(binary, files, threads, hooks)
        )

        def writer(path: Path) -> Any:
            return _writer(path, parameters, geometry, crystal)

        if segment_seconds is not None:
            if continuation is not None:
                identity = checkpoint_identity(
                    Path(request.run_directory), _identity(request, parameters, files, matcher.name, binary, text)
                )
            sink = Segments(
                Path(request.run_directory),
                writer,
                hooks,
                seconds=float(segment_seconds),
                identity=identity,
                allocation=request.allocation,
                report_at_commit=continuation is None,
            )
            if continuation is not None:
                for path in (results_final, native_final):
                    if os.path.lexists(path):  # an earlier end of this run was killed: never trusted or replaced
                        raise CheckpointError(f"{os.path.basename(path)} is in the output folder before the run ended")
                committed_before = sink.resume()
                remaining = skip_committed(remaining, sink.attempted_ids)
                background.restore()
                report()
        else:
            sink = SingleOutput(Path(results_partial), Path(native_partial), writer, hooks)
        try:
            interrupted: ManifestEntry | None = None
            during = False
            limit = "stop" if finalize else limit_reached()
            if limit is None:
                try:
                    started = time.monotonic()
                    matcher.start()
                    if stream:
                        daemon_seconds = time.monotonic() - started
                        gpu = gpu_evidence(matcher.pid())
                        if execution.get("require_gpu", True) and not (gpu and gpu["daemon_on_gpu"]):
                            raise RuntimeError(
                                "The GPU stream daemon runs on no GPU that nvidia-smi lists; the run does not go on"
                            )
                except _StopRequested:
                    limit = limit_reached() or "time"
            for entry in [] if limit is not None else remaining:
                limit = limit_reached()
                if limit is not None:
                    interrupted = entry
                    break
                attempts += 1
                started_frame = time.perf_counter()
                try:
                    output = _process_frame(entry, parameters, matcher, geometry, background, exclusion, processor)
                except _StopRequested:
                    limit, interrupted, during = limit_reached() or "time", entry, True
                    break
                except DaemonFailed as error:
                    daemon_error = str(error)
                    failed += 1
                    sink.fail(FailureRecord.from_error(entry, error))
                    break
                except (InputError, _IndexerFailed, OSError, ValueError, KeyError) as error:
                    failed += 1
                    sink.fail(
                        FailureRecord.from_error(entry, error, context={"seconds": time.perf_counter() - started_frame})
                    )
                else:
                    depth = output.result.depth
                    frame_deep = depth is not None and abs(depth) > DEPTH_WARNING_UM
                    sink.append(
                        entry.input_id,
                        output.result,
                        output,
                        indexed=output.result.n_patterns > 0,
                        unmatched=output.unmatched_spots,
                        deep=frame_deep,
                    )
                    columns = matcher.columns()
                    if columns is not None:
                        sink.set_columns(columns)
                    succeeded += 1
                    unmatched += output.unmatched_spots
                    if output.result.n_patterns > 0:
                        n_indexed += 1
                    deep = deep or frame_deep
                finally:
                    matcher.clean()
                if sink.due():
                    sink.commit()
                    report()
                elif isinstance(sink, SingleOutput):
                    report()
            # The frames before come first in the failure report: the open segment is committed before.
            sink.commit()
            following = interrupted if interrupted is not None else next(remaining, None)
            more = following is not None
            if following is not None:
                remaining = itertools.chain([following], remaining)
            if continuation is not None and more and not daemon_error:
                if limit == "signal":
                    ending = "interrupted"
                elif limit in ("time", "frames"):
                    ending = "handoff"
            # Stopped only with inputs left: a run whose every input was attempted ends as they did.
            stopped = more and limit == "stop"
            time_limited = limit == "time" and continuation is None
            if ending == "final" and continuation is None:  # a continued run reports them as it ends
                if interrupted is not None:
                    hooks.record_failure(not_run(interrupted, during=during))
                    next(remaining)  # the interrupted input, reported
                if stopped or time_limited or daemon_error:
                    for entry in remaining:
                        hooks.record_failure(not_run(entry))
            elif ending == "final":
                leftover = list(remaining)  # not run: the run ends here
                if leftover:
                    first_reason = not_run_reason(during=interrupted is not None and during)
                    rest_reason = not_run_reason()
        finally:
            matcher.close()
            sink.close()
        if isinstance(sink, Segments):
            report()
            n_indexed, unmatched, deep = sink.n_indexed, sink.unmatched, sink.deep
            succeeded, failed = len(sink.frame_ids), sink.n_failed
        if deep:
            run_warnings.append(
                f"Frames deeper than {DEPTH_WARNING_UM:g} um were matched with the geometry at depth 0: their spots "
                "are displaced by about half a pixel or more"
            )
        if time_limited:
            run_warnings.append(
                f"Stopped at the allocation's time limit after {succeeded + failed} of {request.n_inputs} frames; "
                "the rest were not run"
            )
        if daemon_error:
            run_warnings.append(f"The GPU stream daemon ended after {succeeded + failed} frames: {daemon_error}")
        provenance = _provenance(parameters, files, matcher, background, unmatched)
        if stream:
            provenance["gpu"] = gpu
            provenance["daemon_start_seconds"] = daemon_seconds
        if identity is not None:
            provenance["checkpoint_identity"] = identity
            provenance["background_sha256"] = background.sha256
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    segments = sink if isinstance(sink, Segments) else None
    if ending != "final":
        assert segments is not None
        outcome = RunOutcome(
            n_succeeded=succeeded,
            n_failed=failed,
            warnings=run_warnings,
            provenance=provenance,
            n_indexed=n_indexed,
            handoff=None if ending == "interrupted" else ("time_limit" if limit == "time" else "frame_limit"),
            interrupted=ending == "interrupted",
            checkpoint=segments.handoff(),
        )
        outcome.summary = (
            f"allocation {request.allocation} committed {len(segments.attempted_ids) - committed_before} inputs; "
            f"{len(segments.attempted_ids)} of {request.n_inputs} committed"
        )
        return outcome
    outcome = RunOutcome(
        n_succeeded=succeeded,
        n_failed=failed,
        stopped=stopped,
        warnings=run_warnings,
        provenance=provenance,
        n_indexed=n_indexed,
    )
    if continuation is not None:
        assert segments is not None
        return _end_continued(
            request,
            outcome,
            segments,
            hooks,
            leftover,
            first_reason,
            rest_reason,
            results_final=results_final,
            native_final=native_final,
            key=key,
        )
    if succeeded == 0:
        discard_partial(results_final)
        discard_partial(native_final)
        if segments is not None:
            segments.remove()
        outcome.summary = "no frame was processed"
        return outcome
    frame_ids = sink.frame_ids
    try:
        if segments is not None:
            segments.merge(Path(results_partial), Path(native_partial), merge_results_files)
        summary = validate_results_file(results_partial, frame_ids=frame_ids)
    except (InvalidResultsFile, OSError, ValueError) as error:
        outcome.validation_error = f"{RESULTS_FILENAME}: {error}"
        outcome.summary = f"results file kept as {os.path.basename(results_partial)} for diagnosis" + (
            ", with its segments" if segments is not None else ""
        )
        return outcome
    publish_file(results_partial, results_final)
    publish_file(native_partial, native_final)
    outcome.artifacts["results"] = results_final
    outcome.artifacts["native_tables"] = native_final
    outcome.provenance["results_summary"] = {
        "n_frames": summary.n_frames,
        "n_peaks": summary.n_peaks,
        "n_patterns": summary.n_patterns,
        "n_assignments": summary.n_assignments,
    }
    outcome.provenance["cache_key"] = asdict(key)
    if segments is not None:
        outcome.provenance["segments"] = segments.segments()
        segments.remove()
    outcome.summary = f"{summary.n_frames} frames, {summary.n_patterns} orientations in {RESULTS_FILENAME}" + (
        f"; {n_indexed} frames indexed" if n_indexed else "; no frame indexed"
    )
    return outcome


def _end_continued(
    request: RunRequest,
    outcome: RunOutcome,
    segments: Segments,
    hooks: RunHooks,
    leftover: list[ManifestEntry],
    first_reason: tuple[str, str],
    rest_reason: tuple[str, str],
    *,
    results_final: str,
    native_final: str,
    key: Any,
) -> RunOutcome:
    """The end of a continued run (``output``'s docstring): merge the committed segments into partial files, validate
    and publish them, and report the failures (every committed segment's, then each input not run: the first with
    ``first_reason``, the rest with ``rest_reason``). The segments stay until the runner has written the summary
    (``RunOutcome.remove_after_summary``); a merge that does not validate keeps them, and its partial file."""

    def report_failures() -> None:
        for record in segments.committed_failures():
            hooks.record_failure(record)
        for position, entry in enumerate(leftover):
            category, message = first_reason if position == 0 else rest_reason
            hooks.record_failure(FailureRecord.not_run(entry, category=category, message=message))

    outcome.remove_after_summary = [str(segments.root)]
    if not segments.frame_ids:
        report_failures()
        outcome.summary = "no frame was processed"
        return outcome
    results_partial, native_partial = partial_path(results_final), partial_path(native_final)
    try:
        segments.merge(results_partial, native_partial, merge_results_files)
        summary = validate_results_file(str(results_partial), frame_ids=segments.frame_ids)
    except (InvalidResultsFile, ValueError) as error:
        report_failures()
        outcome.remove_after_summary = []  # the evidence stays
        outcome.validation_error = f"{RESULTS_FILENAME}: {error}"
        outcome.summary = f"results file kept as {results_partial.name} for diagnosis, with its segments"
        return outcome
    publish_file(results_partial, results_final)
    publish_file(native_partial, native_final)
    outcome.artifacts["results"] = results_final
    outcome.artifacts["native_tables"] = native_final
    outcome.provenance["results_summary"] = {
        "n_frames": summary.n_frames,
        "n_peaks": summary.n_peaks,
        "n_patterns": summary.n_patterns,
        "n_assignments": summary.n_assignments,
    }
    outcome.provenance["cache_key"] = asdict(key)
    outcome.provenance["segments"] = segments.segments()
    outcome.summary = f"{summary.n_frames} frames, {summary.n_patterns} orientations in {RESULTS_FILENAME}" + (
        f"; {outcome.n_indexed} frames indexed" if outcome.n_indexed else "; no frame indexed"
    )
    report_failures()
    return outcome


def salvage(output_directory: str | os.PathLike[str], destination: str | os.PathLike[str]) -> dict[str, Any]:
    """Merge the committed segments of a continued run that ended Failed into ``destination``, a folder that must not
    exist (the remote-execution runbook's "Salvaging a failed GPU run"): ``output.h5``, ``laue_matching.h5`` and the
    segments' ``failures.jsonl``. Every segment is checked against its record and the run's ``checkpoint.json``
    (``output.committed_segments``); nothing in ``output_directory`` is written, no input is computed. Returns what
    was salvaged."""
    output = Path(output_directory)
    identity = _file_sha256(output / CHECKPOINT_RECORD)
    records = committed_segments(output / SEGMENTS_DIRNAME, identity)
    frame_ids = [frame for record in records for frame in record["frame_ids"]]
    if not frame_ids:
        raise CheckpointError("no committed segment holds a frame")
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=False)
    results, native = target / RESULTS_FILENAME, target / NATIVE_FILENAME
    merge_results_files([r["folder"] / RESULTS_FILENAME for r in records], partial_path(results), frame_ids=frame_ids)
    merge_native_tables([r["folder"] / NATIVE_FILENAME for r in records], partial_path(native))
    summary = validate_results_file(str(partial_path(results)), frame_ids=frame_ids)
    publish_file(partial_path(results), results)
    publish_file(partial_path(native), native)
    failures = FailureReportWriter(target / "failures.jsonl")
    for record in records:
        for failure in read_segment_failures(record["folder"], record):
            failures.append(failure)
    failures.close()
    attempted = sum(len(record["attempted_ids"]) for record in records)
    return {"n_attempted": attempted, "n_succeeded": len(frame_ids), "n_frames": summary.n_frames}


register_compute_function(LAUE_MATCHING_KIND, compute_laue_matching)
