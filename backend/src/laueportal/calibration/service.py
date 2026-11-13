"""The Calibration page's work: its setup (the shared geometry folder and the form defaults), a frame's image and
peaks, a peak fitted where the user clicked, the indexing of the peaks, the detector's fit, and the geometry file
saved with the refined detector. Nothing is stored: each request carries the page's inputs, and Save fits again from
them, so the file written is the fit of the inputs it names.

Every path from a request is confined to the file roots (``files.roots.resolve_inside_roots``) and checked through the
probe (``files.probe.bounded``, an open of the file) before it is read; the geometry folder is a configured path
(``files.roots.real_path``) and is listed through the probe too. lauelab's work is in ``science.detector_calibration``.
"""

from __future__ import annotations

import dataclasses
import math
import os
import stat
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TypeVar

import numpy as np
from scipy.spatial.transform import Rotation

from laueportal.calibration.schemas import (
    MAX_PEAKS,
    CalibrationDetector,
    CalibrationDetectorValues,
    CalibrationHarmonic,
    CalibrationIndexing,
    CalibrationIndexRequest,
    CalibrationIndexSettings,
    CalibrationInputs,
    CalibrationInputsInfo,
    CalibrationPeak,
    CalibrationPeakFit,
    CalibrationPeakFitRequest,
    CalibrationPeaks,
    CalibrationPeakSearchRequest,
    CalibrationPeakSettings,
    CalibrationRefinedRow,
    CalibrationRefineRequest,
    CalibrationResult,
    CalibrationRow,
    CalibrationSaved,
    CalibrationSaveRequest,
    CalibrationSetup,
    CalibrationSurface,
    GeometryFolder,
    GeometryFolderFile,
)
from laueportal.core import etag
from laueportal.core.problems import ProblemError
from laueportal.core.settings import Settings
from laueportal.files import probe
from laueportal.files.roots import is_inside_roots, real_path, resolve_inside_roots
from laueportal.science import detector_calibration as science
from laueportal.science.cache import file_identity
from laueportal.science.detector import frame_bytes

T = TypeVar("T")

CALIBRATIONS_AT_ONCE = 2
"""Calibration computations (peak search, a peak fit, indexing, a fit, a save) run at once in the API: each reads a
frame (8 MiB for 2048 x 2048) and runs lauelab's native peak search or a SciPy fit for up to a few seconds."""
CALIBRATIONS_WAITING = 4
"""Calibration computations that may wait for a turn; more get a 503 with ``Retry-After``."""

AZIMUTH_DEG = 90.0
"""LaueGo's 34-ID-E beamline preference: detector 0's azimuth about the incident beam (deg), which the fit holds."""
SURFACE_WARNING_DEG = 3.0
"""The page warns when the surface reflection is further than this (deg) from the expected surface normal."""

GEOMETRY_PREFIX = "geoN_"
GEOMETRY_SUFFIX = ".xml"
GEOMETRY_NAME = "geoN_%Y-%m-%d_%H-%M-%S.xml"
"""A saved file's name, as LaueGo names geometry files: the local time it was saved."""

REFUSED = "Calibration input refused"


def refused(field: str, message: str) -> ProblemError:
    """A 422 problem on one of the request's fields."""
    return ProblemError(422, REFUSED, message, errors=[{"loc": [field], "message": message, "code": "refused"}])


def _call(work: Callable[[], T]) -> T:
    """``work``, with lauelab's refusals of an input as 422 problems."""
    try:
        return work()
    except science.CalibrationInputError as error:
        raise refused(error.field, error.message) from error


# ---------------------------------------------------------------------------------------------------------------
# Paths


def requested_file(settings: Settings, value: str, field: str, label: str) -> Path:
    """A file a request names: inside the roots, opened through the probe (a hung mount is a 503), a regular file;
    its resolved path. A missing or unreadable file is a 422 on ``field``."""
    _, path = resolve_inside_roots(value, settings)
    try:
        status = probe.bounded(path, lambda: probe.open_check(path), requested=True)
    except FileNotFoundError as error:
        raise refused(field, f"{label} not found") from error
    except OSError as error:
        raise refused(field, f"{label} cannot be read: {error.strerror or error}") from error
    if not stat.S_ISREG(status.st_mode):
        raise refused(field, f"{label} is not a file")
    return path


def _inputs(settings: Settings, request: CalibrationInputs) -> tuple[Path, Path, Path]:
    return (
        requested_file(settings, request.frame, "frame", "Frame"),
        requested_file(settings, request.geometry, "geometry", "Geometry file"),
        requested_file(settings, request.crystal, "crystal", "Crystal file"),
    )


def inputs_version(paths: tuple[Path, Path, Path]) -> str:
    """The version of the frame, geometry and crystal files: their identities (path, size, modification and change
    times), each read through the probe."""
    identities = [probe.bounded(path, lambda path=path: file_identity(path), requested=True) for path in paths]
    return etag.value_tag(*(dataclasses.astuple(identity) for identity in identities))


def geometry_folder(settings: Settings) -> GeometryFolder:
    """The shared geometry folder's state and its ``geoN_*.xml`` files, newest first (the names hold the time)."""
    configured = settings.calibration.geometry_dir
    if configured is None:
        return GeometryFolder(state="not_set", path=None, files=[])
    shown = str(configured)
    try:
        real = real_path(configured)
        if not is_inside_roots(real, settings):
            return GeometryFolder(state="outside_roots", path=shown, files=[])

        def look() -> tuple[list[str], bool]:
            with os.scandir(real) as entries:
                names = [entry.name for entry in entries if _geometry_name(entry.name)]
            return sorted(names, reverse=True), os.access(real, os.W_OK)

        names, writable = probe.bounded(real, look)
    except probe.NotResponding:
        return GeometryFolder(state="not_responding", path=shown, files=[])
    except (FileNotFoundError, NotADirectoryError):
        return GeometryFolder(state="not_found", path=shown, files=[])
    except OSError:
        return GeometryFolder(state="not_writable", path=shown, files=[])
    files = [GeometryFolderFile(name=name, path=str(real / name)) for name in names]
    return GeometryFolder(state="ok" if writable else "not_writable", path=str(real), files=files)


def _geometry_name(name: str) -> bool:
    return name.startswith(GEOMETRY_PREFIX) and name.endswith(GEOMETRY_SUFFIX) and not name.startswith(".")


# ---------------------------------------------------------------------------------------------------------------
# Setup


def setup(settings: Settings) -> CalibrationSetup:
    """The page's starting point: the folder, its newest geometry file, and the LaueGo form's defaults."""
    folder = geometry_folder(settings)
    defaults = settings.indexing.defaults
    return CalibrationSetup(
        geometry_folder=folder,
        default_geometry=folder.files[0].path if folder.files else None,
        peak_settings=CalibrationPeakSettings(
            box_size=defaults.box_size,
            max_rfactor=defaults.max_rfactor,
            min_size=defaults.min_size,
            min_separation=defaults.min_separation,
            threshold=defaults.threshold,
            threshold_ratio=defaults.threshold_ratio,
            peak_shape=defaults.peak_shape,
            max_number=defaults.max_number,
            smooth=defaults.smooth,
        ),
        index_settings=CalibrationIndexSettings(
            index_kev_max_calc=defaults.index_kev_max_calc,
            index_kev_max_test=defaults.index_kev_max_test,
            index_angle_tolerance=defaults.index_angle_tolerance or 0.1,
            index_cone=defaults.index_cone,
            index_hkl=list(defaults.index_hkl),
            max_peaks=defaults.max_peaks,
        ),
        azimuth_deg=AZIMUTH_DEG,
        surface_warning_deg=SURFACE_WARNING_DEG,
    )


# ---------------------------------------------------------------------------------------------------------------
# The frame


def frame_image(settings: Settings, value: str) -> tuple[str, Callable[[], tuple[bytes, str, tuple[int, int]]]]:
    """The frame's tag (its identity, through the probe) and the work that reads it: its bytes (``frame_bytes``),
    their type and its shape ``(height, width)``."""
    path = requested_file(settings, value, "frame", "Frame")
    identity = probe.bounded(path, lambda: file_identity(path), requested=True)
    tag = etag.entity_tag("calibration-frame", identity.path, identity.size, identity.mtime_ns, identity.ctime_ns)

    def read() -> tuple[bytes, str, tuple[int, int]]:
        frame = _call(lambda: science.read_calibration_frame(path))
        body, dtype = frame_bytes(frame.image)
        return body, dtype, (int(frame.image.shape[0]), int(frame.image.shape[1]))

    return tag, read


def _detector(slot: int, detector) -> CalibrationDetector:
    return CalibrationDetector(
        slot=slot,
        detector_id=detector.detector_id,
        nx=int(detector.nx),
        ny=int(detector.ny),
        size_x_um=float(detector.size_x),
        size_y_um=float(detector.size_y),
        rotation_vector=[float(v) for v in detector.rotation_vector],
        translation_um=[float(v) for v in detector.translation],
    )


def inputs_info(settings: Settings, request: CalibrationInputs) -> CalibrationInputsInfo:
    """What the frame, geometry and crystal are, or a 422 on the one lauelab refuses."""
    frame_path, geometry_path, crystal_path = _inputs(settings, request)

    def work() -> CalibrationInputsInfo:
        frame = science.read_calibration_frame(frame_path)
        with science.calibration_files(geometry_path, crystal_path) as files:
            slot, detector = science.starting_detector(files, frame, None)
            return CalibrationInputsInfo(
                width=int(frame.image.shape[1]),
                height=int(frame.image.shape[0]),
                detector_id=frame.detector_id,
                start=list(frame.start),
                group=list(frame.group),
                detector=_detector(slot, detector),
                fit_sample_rotation=slot == 0,
                crystal_name=files.crystal.name,
                space_group=files.crystal.space_group,
            )

    return _call(work)


def peaks(settings: Settings, request: CalibrationPeakSearchRequest) -> CalibrationPeaks:
    """The frame's peaks (lauelab's peak search with the LaueGo form's settings), at most ``MAX_PEAKS``."""
    path = requested_file(settings, request.frame, "frame", "Frame")
    s = request.settings

    def work() -> CalibrationPeaks:
        frame = science.read_calibration_frame(path)
        found = science.search_peaks(
            frame,
            science.PeakSettings(
                box_size=s.box_size,
                max_rfactor=s.max_rfactor,
                min_size=s.min_size,
                min_separation=s.min_separation,
                threshold=s.threshold,
                threshold_ratio=s.threshold_ratio,
                peak_shape=s.peak_shape,
                max_number=s.max_number,
                smooth=s.smooth,
            ),
        )
        count = min(len(found.xy), MAX_PEAKS)
        return CalibrationPeaks(
            peaks=[
                CalibrationPeak(x=float(found.xy[i, 0]), y=float(found.xy[i, 1]), intensity=float(found.intensity[i]))
                for i in range(count)
            ],
            threshold=found.threshold if math.isfinite(found.threshold) else 0.0,
        )

    return _call(work)


def fit_peak(settings: Settings, request: CalibrationPeakFitRequest) -> CalibrationPeakFit:
    """The peak fitted near the clicked pixel, or none."""
    path = requested_file(settings, request.frame, "frame", "Frame")

    def work() -> CalibrationPeakFit:
        frame = science.read_calibration_frame(path)
        fitted = science.fit_clicked_peak(frame, request.x, request.y)
        if fitted is None:
            return CalibrationPeakFit(peak=None)
        return CalibrationPeakFit(peak=CalibrationPeak(x=fitted[0], y=fitted[1], intensity=fitted[2]))

    return _call(work)


# ---------------------------------------------------------------------------------------------------------------
# Indexing and the fit


def _index_settings(settings: CalibrationIndexSettings) -> science.IndexSettings:
    h, k, l = settings.index_hkl  # noqa: E741
    return science.IndexSettings(
        kev_max_calc=settings.index_kev_max_calc,
        kev_max_test=settings.index_kev_max_test,
        angle_tolerance=settings.index_angle_tolerance,
        cone=settings.index_cone,
        hkl=(h, k, l),
        max_peaks=settings.max_peaks,
    )


def _start(values: CalibrationDetectorValues | None) -> tuple[Sequence[float], Sequence[float]] | None:
    return None if values is None else (values.rotation_vector, values.translation_um)


def _peak_array(request: CalibrationIndexRequest) -> np.ndarray:
    return np.array([[p.x, p.y] for p in request.peaks], dtype=float).reshape((-1, 2))


def _optional(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def _surface(check) -> CalibrationSurface:
    h, k, l = (int(v) for v in check.hkl)  # noqa: E741
    return CalibrationSurface(
        h=h,
        k=k,
        l=l,
        angle_to_expected_deg=float(check.angle_to_expected_deg),
        angle_to_incident_deg=float(check.angle_to_incident_deg),
        angle_to_exit_deg=float(check.angle_to_exit_deg),
        between=bool(check.between),
    )


def _harmonics(hkl: np.ndarray, reciprocal: np.ndarray) -> list[CalibrationHarmonic]:
    first = np.asarray(science.primitive(hkl))
    orders = np.arange(1, science.MAX_HARMONIC + 1)
    energies = science.calculated_energies((orders[:, None] * first) @ reciprocal)
    return [
        CalibrationHarmonic(
            order=int(n), h=int(n * first[0]), k=int(n * first[1]), l=int(n * first[2]), energy_kev=_optional(e)
        )
        for n, e in zip(orders, energies, strict=True)
    ]


def _indexing(frame: science.CalibrationFrame, indexed: science.Indexed, files, n_peaks: int) -> CalibrationIndexing:
    detector = _detector(indexed.slot, indexed.detector)
    if indexed.pattern is None or indexed.table is None:
        return CalibrationIndexing(
            found=False,
            n_patterns=0,
            n_peaks=n_peaks,
            n_indexed=0,
            rms_error_deg=None,
            goodness=None,
            rows=[],
            surface=None,
            detector=detector,
            fit_sample_rotation=indexed.slot == 0,
        )
    table, reciprocal = indexed.table, indexed.pattern.reciprocal
    q = table.hkl @ reciprocal
    predicted = science.predicted_frame_pixels(indexed.detector, frame, q)
    energies = science.calculated_energies(q)
    shown = frame.to_frame(table.pixels)
    rows = [
        CalibrationRow(
            peak=int(table.peak_index[row]),
            h=int(table.hkl[row, 0]),
            k=int(table.hkl[row, 1]),
            l=int(table.hkl[row, 2]),
            order=math.gcd(*(int(v) for v in table.hkl[row])),
            x=float(shown[row, 0]),
            y=float(shown[row, 1]),
            predicted_x=_optional(predicted[row, 0]),
            predicted_y=_optional(predicted[row, 1]),
            calculated_energy_kev=_optional(energies[row]),
            harmonics=_harmonics(table.hkl[row], reciprocal),
        )
        for row in range(len(table))
    ]
    return CalibrationIndexing(
        found=True,
        n_patterns=indexed.n_patterns,
        n_peaks=n_peaks,
        n_indexed=len(table),
        rms_error_deg=_optional(indexed.pattern.rms_error_deg),
        goodness=_optional(indexed.pattern.goodness),
        rows=rows,
        surface=_surface(science.check_surface(files.crystal, reciprocal)),
        detector=detector,
        fit_sample_rotation=indexed.slot == 0,
    )


def index(settings: Settings, request: CalibrationIndexRequest) -> CalibrationIndexing:
    """The peaks indexed with the starting detector (the geometry file's, or ``request.start``)."""
    frame_path, geometry_path, crystal_path = _inputs(settings, request)

    def work() -> CalibrationIndexing:
        frame = science.read_calibration_frame(frame_path)
        with science.calibration_files(geometry_path, crystal_path) as files:
            indexed = science.index_peaks(
                files, frame, _peak_array(request), _index_settings(request.settings), _start(request.start)
            )
            return _indexing(frame, indexed, files, len(request.peaks))

    return _call(work)


def _measurements(request: CalibrationRefineRequest) -> dict[int, science.Measurement]:
    measured = {}
    for item in request.measurements:
        if item.peak >= len(request.peaks):
            raise refused("measurements", f"Measurement of peak {item.peak}, which the request does not have")
        measured[item.peak] = science.Measurement(item.energy_kev, item.source, item.order)
    return measured


def _values(rotation_vector, translation) -> CalibrationDetectorValues:
    return CalibrationDetectorValues(
        rotation_vector=[float(v) for v in rotation_vector], translation_um=[float(v) for v in translation]
    )


def _sigmas(values: np.ndarray) -> list[float | None]:
    return [_optional(v) for v in np.asarray(values, float)]


def _result(frame: science.CalibrationFrame, refined: science.Refined, files, version: str) -> CalibrationResult:
    indexed = refined.indexed
    initial = _values(indexed.detector.rotation_vector, indexed.detector.translation)
    calibration = refined.calibration
    if calibration is None:
        return CalibrationResult(
            status="not_indexed",
            message="No pattern was found",
            initial=initial,
            refined=initial,
            rotation_vector_sigma=[None] * 3,
            translation_sigma_um=[None] * 3,
            fit_sample_rotation=indexed.slot == 0,
            sample_rotation_from="indexing",
            sample_rotation_vector=[0.0] * 3,
            sample_rotation_change_deg=0.0,
            sample_rotation_sigma=[None] * 3,
            refined_reciprocal=[[0.0] * 3] * 3,
            error_start=0.0,
            error_end=0.0,
            n_evaluations=0,
            rms_pixel_error=None,
            rms_energy_error_ev=None,
            n_rows=0,
            n_energies=0,
            rows=[],
            surface=None,
            note="",
            inputs_version=version,
        )
    table = calibration.table
    q = table.hkl @ calibration.refined_reciprocal
    predicted = science.predicted_frame_pixels(calibration.refined, frame, q)
    shown = frame.to_frame(table.pixels)
    distance = np.linalg.norm(calibration.pixel_error, axis=1)
    rows = [
        CalibrationRefinedRow(
            peak=int(table.peak_index[row]),
            h=int(table.hkl[row, 0]),
            k=int(table.hkl[row, 1]),
            l=int(table.hkl[row, 2]),
            x=float(shown[row, 0]),
            y=float(shown[row, 1]),
            predicted_x=_optional(predicted[row, 0]),
            predicted_y=_optional(predicted[row, 1]),
            pixel_error=_optional(distance[row]),
            angle_error_deg=_optional(calibration.angle_error_deg[row]),
            measured_energy_kev=_optional(table.energy_kev[row]),
            energy_source=table.energy_source[row],
            calculated_energy_kev=_optional(calibration.calculated_energy_kev[row]),
            energy_error_ev=_optional(calibration.energy_error_ev[row]),
        )
        for row in range(len(table))
    ]
    change = Rotation.from_matrix(calibration.refined_sample_rotation @ calibration.initial_sample_rotation.T)
    fitted = calibration.fit_sample_rotation
    return CalibrationResult(
        status=calibration.status,  # pyright: ignore[reportArgumentType]
        message=calibration.message,
        initial=initial,
        refined=_values(calibration.refined.rotation_vector, calibration.refined.translation),
        rotation_vector_sigma=_sigmas(calibration.rotation_vector_sigma),
        translation_sigma_um=_sigmas(calibration.translation_sigma_um),
        fit_sample_rotation=fitted,
        sample_rotation_from="fitted" if fitted else refined.sample_rotation_from,  # pyright: ignore[reportArgumentType]
        sample_rotation_vector=[float(v) for v in calibration.sample_rotation_vector],
        sample_rotation_change_deg=float(math.degrees(change.magnitude())),
        sample_rotation_sigma=_sigmas(calibration.sample_rotation_sigma),
        refined_reciprocal=[[float(v) for v in row] for row in calibration.refined_reciprocal],
        error_start=float(calibration.error_start),
        error_end=float(calibration.error_end),
        n_evaluations=int(calibration.n_evaluations),
        rms_pixel_error=_optional(calibration.rms_pixel_error),
        rms_energy_error_ev=_optional(calibration.rms_energy_error_ev),
        n_rows=len(table),
        n_energies=table.n_energies,
        rows=rows,
        surface=_surface(science.check_surface(files.crystal, calibration.refined_reciprocal)),
        note=calibration.summary_note(),
        inputs_version=version,
    )


def _refine(frame_path: Path, files, request: CalibrationRefineRequest):
    frame = science.read_calibration_frame(frame_path)
    reciprocal = None if request.sample_reciprocal is None else np.asarray(request.sample_reciprocal, float)
    refined = science.refine_detector(
        files,
        frame,
        _peak_array(request),
        _index_settings(request.settings),
        _start(request.start),
        _measurements(request),
        sample_reciprocal=reciprocal,
        azimuth_deg=request.azimuth_deg,
    )
    return frame, refined


def refine(settings: Settings, request: CalibrationRefineRequest) -> CalibrationResult:
    """The fit of the frame's detector (and, for detector 0, of the sample rotation) to the indexed peaks and the
    measured energies."""
    paths = _inputs(settings, request)
    frame_path, geometry_path, crystal_path = paths
    version = inputs_version(paths)

    def work() -> CalibrationResult:
        with science.calibration_files(geometry_path, crystal_path) as files:
            frame, refined = _refine(frame_path, files, request)
            return _result(frame, refined, files, version)

    return _call(work)


def save_folder(settings: Settings, requested: str | None) -> Path:
    """Where Save writes: a requested folder (inside the roots, checked through the probe: an existing folder the API
    can write), or the geometry folder; a 422 when it cannot be used."""
    if requested is None:
        folder = geometry_folder(settings)
        if folder.state != "ok" or folder.path is None:
            raise ProblemError(
                422, "Geometry folder unavailable", f"The geometry folder is {folder.state.replace('_', ' ')}"
            )
        return Path(folder.path)
    _, path = resolve_inside_roots(requested, settings)

    def look() -> tuple[bool, bool]:
        return path.is_dir(), os.access(path, os.W_OK)

    try:
        is_folder, writable = probe.bounded(path, look, requested=True)
    except OSError as error:
        raise refused("folder", f"Folder cannot be read: {error.strerror or error}") from error
    if not is_folder:
        raise refused("folder", "Folder not found")
    if not writable:
        raise refused("folder", "Folder is not writable")
    return path


def save(settings: Settings, request: CalibrationSaveRequest, *, now: datetime | None = None) -> CalibrationSaved:
    """Fit again from the request and write the starting geometry file with the refined detector into the requested
    folder (or the geometry folder) as the requested name (or ``geoN_<local time>.xml``): 409 when that file exists or
    the inputs changed since the fit, 422 when the fit has no refined values or the folder cannot be written."""
    problem = science.note_problem(request.note)
    if problem is not None:
        raise refused("note", problem)
    folder_path = save_folder(settings, request.folder)
    paths = _inputs(settings, request)
    frame_path, geometry_path, crystal_path = paths
    version = inputs_version(paths)
    if version != request.inputs_version:
        raise ProblemError(
            409,
            "Inputs changed",
            "The frame, geometry or crystal file changed since the fit: refine again before saving",
        )
    name = request.name or (now or datetime.now()).strftime(GEOMETRY_NAME)
    destination = folder_path / name

    def work() -> CalibrationSaved:
        with science.calibration_files(geometry_path, crystal_path) as files:
            frame, refined = _refine(frame_path, files, request)
            result = _result(frame, refined, files, version)
            calibration = refined.calibration
            if calibration is None or not calibration.success:
                raise ProblemError(422, "Nothing to save", result.message)
            file_note = science.ascii_note(f"Calibrated from {frame_path.name}")
            try:
                # The folder is shared (often on /net): the write and its checks run in the probe, keyed on the folder,
                # so a hung mount answers 503 instead of holding this thread.
                probe.bounded(
                    folder_path,
                    lambda: science.save_geometry(
                        files, destination, calibration.refined, note=request.note, file_note=file_note
                    ),
                )
            except FileExistsError as error:
                raise ProblemError(409, "Geometry file exists", f"{name} exists; save again") from error
            except OSError as error:
                raise ProblemError(422, "Geometry file not written", f"{name}: {error.strerror or error}") from error
            return CalibrationSaved(name=name, path=str(destination), result=result)

    return _call(work)


__all__ = [
    "CALIBRATIONS_AT_ONCE",
    "CALIBRATIONS_WAITING",
    "frame_image",
    "geometry_folder",
    "index",
    "inputs_info",
    "fit_peak",
    "peaks",
    "refine",
    "requested_file",
    "save",
    "setup",
]
