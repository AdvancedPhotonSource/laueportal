"""The Calibration page's computations with lauelab (``lauelab.calibration``, a port of LaueGo's ``Calibration.ipf``):
a frame's peak search, a peak fitted where the user clicked, the indexing of the peaks with a starting detector,
the fit of the detector (and of the sample rotation for detector 0), the surface check of the crystal's orientation,
and the geometry file written with the refined detector.

The geometry and crystal files are loaded from private copies of at most ``MAX_CALIBRATION_BYTES`` read through one
descriptor (``science.calibration``), and the geometry file the page saves is written from the same copy, so the
bytes the fit started from are the bytes the new file is made of. Positions in this module's inputs and outputs are
frame pixels, as the page draws them; lauelab's calibration takes full-detector pixels, converted here with the
frame's ``start`` and ``group``. Translations are in µm and rotation vectors in rad (axis times angle).
"""

from __future__ import annotations

import math
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from lauelab.calibration import (
    HC_KEV_NM,
    CalibrationTable,
    DetectorCalibration,
    SurfaceCheck,
    calibrate_detector,
    calibration_table,
    fit_peak,
    peak_directions,
    rotation_matrix,
    surface_check,
    write_geometry,
)
from lauelab.indexing import (
    Crystal,
    DetectorGeometry,
    Geometry,
    InputError,
    LaueError,
    Pattern,
    index_orientations,
    load_crystal,
    peak_search,
    read_frame,
)

from laueportal.science.calibration import CalibrationProblem, _private_copy, _TooLarge, size_problem

MAX_HARMONIC = 8
"""The highest harmonic order the page offers for a reflection with a measured energy (008 for 001)."""

DEFAULT_MAX_DATA = 250
"""lauelab's ``IndexParams.max_data`` when the form's Max Peaks to Index is blank or not positive."""


class CalibrationInputError(ValueError):
    """A request's file or value that lauelab refused: ``field`` names the request's field, ``message`` says why."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class CalibrationFrame:
    """A frame read by lauelab: the image (``image[y, x]``), its detector ID (None when the file records none) and
    the full-detector origin and grouping of its pixels, as ``(x, y)``."""

    image: np.ndarray
    detector_id: str | None
    start: tuple[int, int]
    group: tuple[int, int]

    def to_detector(self, xy: np.ndarray) -> np.ndarray:
        """Frame pixels ``(n, 2)`` as full-detector pixels (the centre of each group)."""
        start, group = np.asarray(self.start, float), np.asarray(self.group, float)
        return start + np.asarray(xy, float) * group + (group - 1) / 2.0

    def to_frame(self, xy: np.ndarray) -> np.ndarray:
        """Full-detector pixels ``(n, 2)`` as frame pixels."""
        start, group = np.asarray(self.start, float), np.asarray(self.group, float)
        return (np.asarray(xy, float) - start - (group - 1) / 2.0) / group


def read_calibration_frame(path: str | Path) -> CalibrationFrame:
    """The frame at ``path`` (a 34-ID-E HDF5 frame), or ``CalibrationInputError`` on ``frame``."""
    try:
        stored, metadata, processing = read_frame(path)
        image = np.asarray(stored)
    except (InputError, OSError) as error:
        raise CalibrationInputError("frame", f"Does not read as a frame: {error}") from error
    if image.ndim != 2:
        raise CalibrationInputError("frame", f"Is not a two-dimensional image (shape {image.shape})")
    detector_id = metadata.get("detector_id")
    start = tuple(int(v) for v in processing.get("start", (0, 0)))
    group = tuple(int(v) for v in processing.get("group", (1, 1)))
    return CalibrationFrame(
        image, str(detector_id) if detector_id else None, (start[0], start[1]), (group[0], group[1])
    )


@dataclass(frozen=True)
class CalibrationFiles:
    """The loaded geometry and crystal, and the path of the geometry's private copy (the source of a saved file)."""

    geometry: Geometry
    crystal: Crystal
    geometry_copy: Path


@contextmanager
def calibration_files(geometry: str | Path, crystal: str | Path) -> Iterator[CalibrationFiles]:
    """The geometry and crystal loaded from private copies of their bytes (removed on exit), or
    ``CalibrationInputError`` on ``geometry`` or ``crystal``."""
    with tempfile.TemporaryDirectory(prefix="laueportal-detector-calibration-") as directory:
        geometry_copy = _copied(Path(geometry), Path(directory) / "geometry", "geometry")
        crystal_copy = _copied(Path(crystal), Path(directory) / "crystal", "crystal")
        try:
            loaded_geometry = Geometry(geometry_copy)
        except (ValueError, LaueError, OSError) as error:
            raise CalibrationInputError("geometry", _message("geometry", error, geometry_copy, geometry)) from error
        try:
            loaded_crystal = load_crystal(crystal_copy)
        except Exception as error:  # a file the user names: any failure to read it is its refusal, never a 500
            raise CalibrationInputError("crystal", _message("crystal", error, crystal_copy, crystal)) from error
        yield CalibrationFiles(loaded_geometry, replace(loaded_crystal, source=str(crystal)), geometry_copy)


def _copied(path: Path, directory: Path, field: str) -> Path:
    directory.mkdir()
    try:
        return Path(_private_copy(str(path), str(directory)))
    except _TooLarge as large:
        raise CalibrationInputError(field, size_problem(large.size) or "") from large
    except OSError as error:
        raise CalibrationInputError(field, f"Does not load as a {field} file: {error}") from error


def _message(what: str, error: BaseException, copy: Path, shown: str | Path) -> str:
    text = (str(error) or type(error).__name__).replace(str(copy), Path(shown).name)
    return f"Does not load as a {what} file: {text}"


def detector_slot(geometry: Geometry, frame: CalibrationFrame) -> int:
    """The geometry's slot of the frame's detector (by its ID; slot 0 for a frame that records none), or
    ``CalibrationInputError`` on ``geometry`` when the geometry has no detector with that ID."""
    if frame.detector_id is None:
        return 0
    slot = geometry.find_detector(frame.detector_id)
    if slot < 0:
        raise CalibrationInputError("geometry", f"Has no detector with the frame's ID {frame.detector_id!r}")
    return slot


@dataclass(frozen=True)
class PeakSettings:
    """The peak search's settings, by the LaueGo form's names and rules (``indexing.lauego.compute``)."""

    box_size: int
    max_rfactor: float
    min_size: float
    min_separation: int
    threshold: float | None
    threshold_ratio: float | None
    peak_shape: str
    max_number: int | None
    smooth: bool


@dataclass(frozen=True)
class IndexSettings:
    """The indexing's settings, by the LaueGo form's names and rules."""

    kev_max_calc: float
    kev_max_test: float
    angle_tolerance: float
    cone: float
    hkl: tuple[int, int, int]
    max_peaks: int | None


@dataclass(frozen=True)
class FoundPeaks:
    """Peak search's peaks (frame pixels, intensity, half widths in pixels) and its threshold (counts)."""

    xy: np.ndarray
    intensity: np.ndarray
    hwhm: np.ndarray
    threshold: float


def search_peaks(frame: CalibrationFrame, settings: PeakSettings) -> FoundPeaks:
    """lauelab's peak search on the frame, brightest first."""
    ratio = settings.threshold_ratio
    try:
        found = peak_search(
            frame.image,
            boxsize=settings.box_size,
            max_rfactor=settings.max_rfactor,
            min_size=settings.min_size,
            min_separation=settings.min_separation,
            threshold=settings.threshold,
            threshold_ratio=None if ratio is None or ratio <= 0 else ratio,
            peak_shape=settings.peak_shape,  # pyright: ignore[reportArgumentType]
            max_peaks=None if settings.max_number is None or settings.max_number <= 0 else settings.max_number,
            smooth=settings.smooth,
        )
    except InputError as error:
        raise CalibrationInputError("peaks", str(error)) from error
    peaks = found.peaks
    return FoundPeaks(
        np.column_stack([peaks["fit_x"], peaks["fit_y"]]),
        np.asarray(peaks["intens"], float),
        np.column_stack([peaks["hwhm_x"], peaks["hwhm_y"]]),
        float(found.threshold_used),
    )


def fit_clicked_peak(frame: CalibrationFrame, x: float, y: float) -> tuple[float, float, float] | None:
    """The peak lauelab fits near frame pixel ``(x, y)`` (a two-dimensional Gaussian): its centre and amplitude, or
    None when there is none."""
    try:
        peak = fit_peak(frame.image, x, y)
    except InputError as error:
        raise CalibrationInputError("peak", str(error)) from error
    if peak is None:
        return None
    return float(peak["fit_x"][0]), float(peak["fit_y"][0]), float(peak["intens"][0])


@dataclass(frozen=True)
class Indexed:
    """The indexing of the peaks: the starting detector, its slot, the best pattern and the calibration table of the
    peaks it indexed (``table.peak_index`` into the peaks), or no pattern."""

    slot: int
    detector: DetectorGeometry
    n_patterns: int
    pattern: Pattern | None
    table: CalibrationTable | None


def starting_detector(
    files: CalibrationFiles, frame: CalibrationFrame, start: tuple[Sequence[float], Sequence[float]] | None
) -> tuple[int, DetectorGeometry]:
    """The frame's slot and its detector: the geometry file's, or with ``start``'s rotation vector (rad) and
    translation (µm) instead (the page's Match Again, from a refined detector)."""
    slot = detector_slot(files.geometry, frame)
    detector = files.geometry.detector(slot)
    if start is not None:
        vector = np.asarray(start[0], float)
        detector = replace(
            detector, rotation_vector=vector, rotation=rotation_matrix(vector), translation=np.asarray(start[1], float)
        )
    return slot, detector


def index_peaks(
    files: CalibrationFiles,
    frame: CalibrationFrame,
    peaks: np.ndarray,
    settings: IndexSettings,
    start: tuple[Sequence[float], Sequence[float]] | None,
) -> Indexed:
    """Index the frame's ``peaks`` (frame pixels, in order) with the starting detector (``starting_detector``)."""
    slot, detector = starting_detector(files, frame, start)
    full = frame.to_detector(peaks)
    outside = (full[:, 0] < 0) | (full[:, 0] > detector.nx - 1) | (full[:, 1] < 0) | (full[:, 1] > detector.ny - 1)
    if len(full) and outside.any():
        raise CalibrationInputError("peaks", f"Peak {int(np.flatnonzero(outside)[0])} is outside the detector")
    if len(peaks) < 2:
        return Indexed(slot, detector, 0, None, None)
    max_data = settings.max_peaks if settings.max_peaks and settings.max_peaks > 0 else DEFAULT_MAX_DATA
    try:
        patterns = index_orientations(
            peak_directions(detector, full),
            files.crystal,
            kev_max_calc=settings.kev_max_calc,
            kev_max_test=settings.kev_max_test,
            angle_tolerance_deg=settings.angle_tolerance,
            cone_deg=settings.cone,
            hkl_prefer=settings.hkl,
            max_data=max_data,
        )
    except InputError as error:
        raise CalibrationInputError("index", str(error)) from error
    if not patterns:
        return Indexed(slot, detector, 0, None, None)
    pattern = patterns[0]
    table = calibration_table(peaks, pattern, start=frame.start, group=frame.group)
    return Indexed(slot, detector, len(patterns), pattern, table)


def predicted_frame_pixels(detector: DetectorGeometry, frame: CalibrationFrame, q: np.ndarray) -> np.ndarray:
    """Where reciprocal vectors ``q`` (1/nm, rows) hit the detector, as frame pixels; NaN off the detector."""
    if len(q) == 0:
        return np.zeros((0, 2))
    return frame.to_frame(detector.q_to_pixel(q, on_detector=True))


def calculated_energies(q: np.ndarray) -> np.ndarray:
    """The energy (keV) that diffracts into each reciprocal vector (1/nm, rows), as LaueGo's ``calculated_keV``."""
    q = np.asarray(q, float).reshape((-1, 3))
    length = np.linalg.norm(q, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sin_theta = -q[:, 2] / length
        energy = HC_KEV_NM * length / (4.0 * math.pi * sin_theta)
    return np.where(sin_theta > 0, energy, np.nan)


def primitive(hkl: Sequence[int] | np.ndarray) -> tuple[int, int, int]:
    """``hkl`` divided by the greatest common divisor of its indices (its first-order reflection)."""
    divisor = math.gcd(*(int(v) for v in hkl)) or 1
    return (int(hkl[0]) // divisor, int(hkl[1]) // divisor, int(hkl[2]) // divisor)


@dataclass(frozen=True)
class Measurement:
    """A user's value for one peak: its measured energy (keV, with where it was measured) and the harmonic order of
    its reflection (``order`` times its first-order hkl; None keeps indexing's)."""

    energy_kev: float | None
    source: str
    order: int | None


def measured_table(table: CalibrationTable, measurements: Mapping[int, Measurement]) -> CalibrationTable:
    """``table`` with the measurements of its rows' peaks (by peak index): energies and harmonic orders."""
    for row, peak in enumerate(table.peak_index):
        measurement = measurements.get(int(peak))
        if measurement is None:
            continue
        if measurement.order is not None:
            table = table.with_hkl(row, np.asarray(primitive(table.hkl[row])) * measurement.order)
        if measurement.energy_kev is not None:
            table = table.with_energy(row, measurement.energy_kev, measurement.source)
    return table


@dataclass(frozen=True)
class Refined:
    """The fit of a detector: the indexing it used, the calibration (``lauelab.calibration.DetectorCalibration``),
    whether the sample rotation was fitted, and where a fixed one came from (``"detector0"``: the page's detector-0
    calibration; ``"indexing"``: this frame's indexing)."""

    indexed: Indexed
    calibration: DetectorCalibration | None
    sample_rotation_from: str


def refine_detector(
    files: CalibrationFiles,
    frame: CalibrationFrame,
    peaks: np.ndarray,
    settings: IndexSettings,
    start: tuple[Sequence[float], Sequence[float]] | None,
    measurements: Mapping[int, Measurement],
    *,
    sample_reciprocal: np.ndarray | None,
    azimuth_deg: float,
) -> Refined:
    """Index the peaks (``index_peaks``), add the measurements, and fit the detector: with the sample rotation for
    detector 0, else with the sample rotation of ``sample_reciprocal`` (rows in 1/nm, a detector-0 calibration's) or,
    without one, of this frame's indexing."""
    indexed = index_peaks(files, frame, peaks, settings, start)
    if indexed.pattern is None or indexed.table is None:
        return Refined(indexed, None, "indexing")
    table = indexed.table
    fit_sample = indexed.slot == 0
    reciprocal = indexed.pattern.reciprocal
    source = "indexing"
    if not fit_sample and sample_reciprocal is not None:
        # The side frame's indexing may name the reflections in another symmetry-equivalent setting than detector 0's
        # orientation: its hkl are carried into that setting before the fit keeps that orientation.
        setting = equivalent_setting(indexed.pattern.reciprocal, sample_reciprocal)
        if setting is None:
            raise CalibrationInputError(
                "sample_reciprocal",
                "The frame's orientation is not detector 0's calibrated orientation: calibrate detector 0 with a frame "
                "of the same sample position",
            )
        for row in range(len(table)):
            table = table.with_hkl(row, table.hkl[row] @ setting)
        reciprocal, source = sample_reciprocal, "detector0"
    table = measured_table(table, measurements)
    try:
        calibration = calibrate_detector(
            table,
            files.crystal,
            reciprocal,
            indexed.detector,
            fit_sample_rotation=fit_sample,
            azimuth_deg=azimuth_deg,
        )
    except InputError as error:
        raise CalibrationInputError("sample_reciprocal" if source == "detector0" else "peaks", str(error)) from error
    return Refined(indexed, calibration, source)


SETTING_TOLERANCE = 0.05
"""The largest difference between ``measured @ inv(reference)`` and an integer matrix for two orientations to be the same
up to the crystal's setting (a 0.05 entry: about 3 degrees)."""


def equivalent_setting(measured: np.ndarray, reference: np.ndarray) -> np.ndarray | None:
    """The integer matrix ``M`` with ``measured ≈ M @ reference`` (reciprocal rows, 1/nm) when the two orientations are
    the same crystal orientation named in two settings (``M`` unimodular), so that ``hkl @ M`` names in ``reference``'s
    setting a reflection ``hkl`` of ``measured``; None when they are different orientations."""
    matrix = np.asarray(measured, float) @ np.linalg.inv(np.asarray(reference, float))
    rounded = np.rint(matrix)
    if np.max(np.abs(matrix - rounded)) > SETTING_TOLERANCE or abs(abs(np.linalg.det(rounded)) - 1) > 1e-9:
        return None
    return rounded.astype(np.int64)


def check_surface(crystal: Crystal, reciprocal: np.ndarray) -> SurfaceCheck:
    """Where the crystal's {001} reflection points (lauelab's ``surface_check``, the 34-ID-E standard's normal)."""
    return surface_check(crystal, reciprocal)


def note_problem(text: str) -> str | None:
    """Why ``text`` cannot be a detector's ``<note>`` (the C programs and Igor search the file's text for tags, and
    Igor keeps 99 characters), or None."""
    if not text.isascii() or not text.isprintable() or any(c in text for c in "<>&"):
        return "Must be printable ASCII without <, > or &"
    if len(text) > 99:
        return f"Must be at most 99 characters; it has {len(text)}"
    return None


def ascii_note(text: str) -> str:
    """``text`` as a note the geometry readers accept: printable ASCII, ``<``, ``>`` and ``&`` dropped."""
    kept = "".join(c if c.isascii() and c.isprintable() else "?" for c in text)
    return "".join(c for c in kept if c not in "<>&")


def save_geometry(
    files: CalibrationFiles, destination: Path, refined: DetectorGeometry, *, note: str, file_note: str
) -> Path:
    """Write the geometry file's copy with the refined detector into ``destination`` (which must not exist):
    ``lauelab.calibration.write_geometry`` changes the detector's ``<R>``, ``<P>``, ``<timeMeasured>`` and
    ``<note>``, the write times and ``<fileNote>``, and reads the file back before it takes its name."""
    try:
        return write_geometry(files.geometry_copy, destination, [refined], note=note, file_note=file_note)
    except InputError as error:
        raise CalibrationInputError("geometry", str(error)) from error


__all__ = [
    "CalibrationFiles",
    "CalibrationFrame",
    "CalibrationInputError",
    "CalibrationProblem",
    "FoundPeaks",
    "IndexSettings",
    "Indexed",
    "MAX_HARMONIC",
    "Measurement",
    "PeakSettings",
    "Refined",
    "ascii_note",
    "calculated_energies",
    "calibration_files",
    "check_surface",
    "detector_slot",
    "equivalent_setting",
    "fit_clicked_peak",
    "index_peaks",
    "measured_table",
    "note_problem",
    "predicted_frame_pixels",
    "primitive",
    "read_calibration_frame",
    "refine_detector",
    "save_geometry",
    "search_peaks",
    "starting_detector",
]
