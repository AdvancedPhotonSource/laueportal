"""API schemas of the calibration domain: the Calibration page's setup, a frame's peaks, the indexing of its peaks, the
fit of its detector and the saved geometry file. The names are part of the contract (Orval type names).

Positions are frame pixels (zero-based ``(x, y)``, ``image[y, x]``), translations µm, rotation vectors rad (axis
times angle, as in the geometry file), energies keV and energy differences eV.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_PEAKS = 1000
"""The most peaks a request may send (the LaueGo form's peak search returns at most 300 by default)."""

Finite = Annotated[float, Field(allow_inf_nan=False)]
Vector = Annotated[list[Finite], Field(min_length=3, max_length=3)]
PathText = Annotated[str, Field(min_length=1, max_length=4096)]


class _ApiModel(BaseModel):
    """API schemas: attribute docstrings become the OpenAPI descriptions (and Orval's comments)."""

    model_config = ConfigDict(use_attribute_docstrings=True)


class CalibrationPeakSettings(_ApiModel):
    """The peak search's settings, with the LaueGo form's names, meanings and defaults (``[indexing.defaults]``)."""

    box_size: Annotated[int, Field(gt=0, le=200)]
    """Half-width of the fit box (pixels)."""
    max_rfactor: Annotated[float, Field(ge=0, le=1)]
    min_size: Annotated[float, Field(gt=0, le=1000)]
    """Minimum spot size (pixels)."""
    min_separation: Annotated[int, Field(ge=0, le=2000)]
    """Minimum distance between peaks (pixels)."""
    threshold: Annotated[float | None, Field(ge=0, allow_inf_nan=False)] = None
    """Absolute threshold (counts); null for automatic."""
    threshold_ratio: Annotated[float | None, Field(allow_inf_nan=False)] = None
    """Multiple of the image's standard deviation for the automatic threshold; null or not positive: lauelab's."""
    peak_shape: Literal["Lorentzian", "Gaussian"]
    max_number: Annotated[int | None, Field(le=MAX_PEAKS)] = None
    """The most peaks to find; null or not positive: no limit (then at most ``MAX_PEAKS`` are answered)."""
    smooth: bool = False


class CalibrationIndexSettings(_ApiModel):
    """The indexing's settings, with the LaueGo form's names, meanings and defaults."""

    index_kev_max_calc: Annotated[float, Field(gt=0, le=200)]
    index_kev_max_test: Annotated[float, Field(gt=0, le=200)]
    index_angle_tolerance: Annotated[float, Field(gt=0, le=10)]
    """Degrees."""
    index_cone: Annotated[float, Field(gt=0, le=180)]
    """Degrees."""
    index_hkl: Annotated[list[int], Field(min_length=3, max_length=3)]
    """The preferred hkl at the centre of the cone."""
    max_peaks: Annotated[int | None, Field(ge=2, le=MAX_PEAKS)] = None
    """The most peaks indexed, brightest first; null: 250."""


class CalibrationDetectorValues(_ApiModel):
    """A detector's rotation vector and translation, as in the geometry file (translation in µm here)."""

    rotation_vector: Vector
    """Axis times angle (rad)."""
    translation_um: Vector


class GeometryFolderFile(_ApiModel):
    name: str
    path: str


class GeometryFolder(_ApiModel):
    """The shared geometry folder (``[calibration] geometry_dir``)."""

    state: Literal["not_set", "ok", "outside_roots", "not_found", "not_writable", "not_responding"]
    """``ok``: the page saves here. Otherwise Save is unavailable, and the state says why."""
    path: str | None
    files: list[GeometryFolderFile]
    """Its ``geoN_*.xml`` files, newest (by name) first."""


class CalibrationSetup(_ApiModel):
    """What the Calibration page starts from: the geometry folder and the form defaults."""

    geometry_folder: GeometryFolder
    default_geometry: str | None
    """The newest geometry file of the folder; null when there is none."""
    peak_settings: CalibrationPeakSettings
    index_settings: CalibrationIndexSettings
    azimuth_deg: float
    """The detector-0 azimuth about the incident beam (deg) that the fit holds: LaueGo's 34-ID-E preference."""
    surface_warning_deg: float
    """The angle (deg) between the surface reflection and the expected surface normal above which the page warns."""


class CalibrationFrameRequest(_ApiModel):
    frame: PathText
    """The calibration frame (an HDF5 file inside the file roots)."""


class CalibrationInputs(CalibrationFrameRequest):
    geometry: PathText
    """The starting geometry file."""
    crystal: PathText
    """The calibration crystal's file."""


class CalibrationDetector(_ApiModel):
    """A detector of the geometry file."""

    slot: int
    """Its physical slot in the geometry file (LaueGo's detector number)."""
    detector_id: str
    nx: int
    ny: int
    size_x_um: float
    size_y_um: float
    rotation_vector: list[float]
    translation_um: list[float]


class CalibrationInputsInfo(_ApiModel):
    """What the inputs are: the frame's size and detector, the geometry's detector for it, and the crystal."""

    width: int
    height: int
    detector_id: str | None
    """The frame's detector ID; null when the file records none (detector 0 is then used)."""
    start: list[int]
    """The frame's full-detector origin ``(x, y)``."""
    group: list[int]
    """Its pixel grouping ``(x, y)``."""
    detector: CalibrationDetector
    fit_sample_rotation: bool
    """Whether the sample rotation is fitted with the detector (detector 0, as LaueGo)."""
    crystal_name: str
    space_group: int


class CalibrationPeakSearchRequest(CalibrationFrameRequest):
    settings: CalibrationPeakSettings


class CalibrationPeak(_ApiModel):
    x: float
    y: float
    intensity: float


class CalibrationPeaks(_ApiModel):
    """Peak search's peaks, brightest first."""

    peaks: list[CalibrationPeak]
    threshold: float
    """The threshold used (counts)."""


class CalibrationPeakFitRequest(CalibrationFrameRequest):
    x: Finite
    y: Finite


class CalibrationPeakFit(_ApiModel):
    """The peak fitted near a clicked pixel; ``peak`` null when there is none."""

    peak: CalibrationPeak | None


class CalibrationPosition(_ApiModel):
    x: Finite
    y: Finite


class CalibrationMeasurement(_ApiModel):
    """A user's value for one peak (by its index in ``peaks``)."""

    peak: Annotated[int, Field(ge=0, lt=MAX_PEAKS)]
    energy_kev: Annotated[float | None, Field(gt=0, le=200, allow_inf_nan=False)] = None
    """The measured energy; null for none."""
    source: Annotated[str, Field(max_length=200)] = ""
    """Where the energy was measured, such as a scan number."""
    order: Annotated[int | None, Field(ge=1, le=8)] = None
    """The harmonic: the reflection is this multiple of its first-order hkl; null keeps the indexing's."""


class CalibrationIndexRequest(CalibrationInputs):
    peaks: Annotated[list[CalibrationPosition], Field(max_length=MAX_PEAKS)]
    """The peaks to index, in order (found, then added)."""
    settings: CalibrationIndexSettings
    start: CalibrationDetectorValues | None = None
    """The starting detector instead of the geometry file's (Match Again, from a refined detector)."""


class CalibrationSurface(_ApiModel):
    """Where the crystal's surface reflection points (lauelab's ``surface_check``: the {001} member nearest the
    34-ID-E standard's surface normal, 45 degrees between the reversed incident beam and +y)."""

    h: int
    k: int
    l: int  # noqa: E741  (Miller index)
    angle_to_expected_deg: float
    angle_to_incident_deg: float
    """The angle to the reversed incident beam."""
    angle_to_exit_deg: float
    """The angle to the beam the expected surface reflects."""
    between: bool


class CalibrationHarmonic(_ApiModel):
    order: int
    h: int
    k: int
    l: int  # noqa: E741
    energy_kev: float | None


class CalibrationRow(_ApiModel):
    """One indexed peak."""

    peak: int
    """Its index in the request's ``peaks``."""
    h: int
    k: int
    l: int  # noqa: E741
    """The reflection: the indexing's hkl, or the request's harmonic of it."""
    order: int
    """Its harmonic order (the greatest common divisor of h, k and l)."""
    x: float
    y: float
    """The peak (frame pixels)."""
    predicted_x: float | None
    predicted_y: float | None
    """Where the reflection hits the starting detector (frame pixels); null off it."""
    calculated_energy_kev: float | None
    """Its energy from the indexed orientation (LaueGo's ``calculated_keV``)."""
    harmonics: list[CalibrationHarmonic]
    """Its harmonic orders 1 to 8 with their energies, to choose the one a measured energy belongs to."""


class CalibrationIndexing(_ApiModel):
    """The indexing of the peaks with the starting detector."""

    found: bool
    """Whether a pattern was found; the other values are empty otherwise."""
    n_patterns: int
    n_peaks: int
    n_indexed: int
    rms_error_deg: float | None
    goodness: float | None
    rows: list[CalibrationRow]
    surface: CalibrationSurface | None
    detector: CalibrationDetector
    """The starting detector."""
    fit_sample_rotation: bool


class CalibrationRefineRequest(CalibrationIndexRequest):
    measurements: Annotated[list[CalibrationMeasurement], Field(max_length=MAX_PEAKS)] = []
    """Measured energies and harmonic orders, by peak."""
    sample_reciprocal: Annotated[list[Vector] | None, Field(min_length=3, max_length=3)] = None
    """For a detector other than 0: the reciprocal lattice rows (1/nm, including 2π) of a detector-0 calibration of
    the same sample, whose sample rotation is kept; null: this frame's indexing's."""
    azimuth_deg: Annotated[float, Field(ge=-180, le=180)] = 90.0
    """The detector-0 azimuth the fit holds (deg)."""


class CalibrationRefinedRow(_ApiModel):
    peak: int
    h: int
    k: int
    l: int  # noqa: E741
    x: float
    y: float
    predicted_x: float | None
    predicted_y: float | None
    """Where the reflection hits the refined detector (frame pixels); null off it."""
    pixel_error: float | None
    """The distance between predicted and measured position (full-detector pixels)."""
    angle_error_deg: float | None
    measured_energy_kev: float | None
    energy_source: str
    calculated_energy_kev: float | None
    """From the refined sample rotation."""
    energy_error_ev: float | None
    """Measured minus calculated (eV)."""


class CalibrationResult(_ApiModel):
    """The fit of the detector (``lauelab.calibration.calibrate_detector``)."""

    status: Literal["ok", "insufficient_data", "energy_mismatch", "not_converged", "not_indexed"]
    """``ok``: refined values; ``not_indexed``: no pattern was found; else lauelab's reason (``message``)."""
    message: str
    initial: CalibrationDetectorValues
    refined: CalibrationDetectorValues
    rotation_vector_sigma: list[float | None]
    translation_sigma_um: list[float | None]
    """Standard errors from the fit's residuals under LaueGo's weights; not a calibrated uncertainty."""
    fit_sample_rotation: bool
    sample_rotation_from: Literal["fitted", "detector0", "indexing"]
    """``detector0``: a detector-0 calibration's sample rotation was kept; ``indexing``: this frame's."""
    sample_rotation_vector: list[float]
    """The (refined) sample rotation, axis times angle (rad), LaueGo's rho."""
    sample_rotation_change_deg: float
    sample_rotation_sigma: list[float | None]
    refined_reciprocal: list[list[float]]
    """The refined orientation's reciprocal rows (1/nm, including 2π), for the other detectors' fits."""
    error_start: float
    error_end: float
    n_evaluations: int
    rms_pixel_error: float | None
    rms_energy_error_ev: float | None
    n_rows: int
    n_energies: int
    rows: list[CalibrationRefinedRow]
    surface: CalibrationSurface | None
    """The surface check of the refined orientation."""
    note: str
    """The geometry file note Save writes by default."""
    inputs_version: str
    """The version of the frame, geometry and crystal files the fit read (their identities): Save sends it back and is
    refused when a file has changed since."""


class CalibrationSaveRequest(CalibrationRefineRequest):
    note: Annotated[str, Field(min_length=1, max_length=99)]
    """The calibrated detector's ``<note>``: printable ASCII without <, > or &."""
    inputs_version: Annotated[str, Field(min_length=1, max_length=200)]
    """The ``inputs_version`` of the fit shown: Save is a 409 when the files have changed since."""
    folder: PathText | None = None
    """The folder to write in (inside the file roots); null: the geometry folder (``[calibration] geometry_dir``)."""
    name: Annotated[str | None, Field(max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.xml$")] = None
    """The new file's name (letters, digits, ``.``, ``_``, ``-``, ending ``.xml``); null: ``geoN_<local time>.xml``.
    An existing file is never replaced."""


class CalibrationSaved(_ApiModel):
    """The saved geometry file."""

    name: str
    path: str
    result: CalibrationResult
    """The fit written (computed again from the request)."""
