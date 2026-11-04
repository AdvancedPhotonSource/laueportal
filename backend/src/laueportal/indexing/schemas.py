"""API schemas of the indexing domain: the indexations grid and the scan detail's peak indexing card, and
the results viewer's artifact, conversion, summary and products. The names are part of
the contract (Orval type names)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.core.paging import GridPage, GridParams
from laueportal.core.timeutil import UTCTimestamp
from laueportal.jobs.schemas import RunStatus
from laueportal.science.viewer import DEFAULT_MIN_PEAKS, DEFAULT_PATTERN0_ONLY


class _ApiModel(BaseModel):
    """API schemas: attribute docstrings become the OpenAPI descriptions (and Orval's comments)."""

    model_config = ConfigDict(use_attribute_docstrings=True)


class IndexingRunSummary(_ApiModel):
    """One row of the indexations grid (``GET /api/v1/indexing-runs``: LaueGo runs) and
    of the scan detail's peak indexing card."""

    id: int
    """The indexing run's ID (shown as ``I<id>``)."""
    method: str
    """The indexing method: ``lauego`` (the list holds LaueGo runs only)."""
    scan_number: int | None
    """Its source scan; null when it has none (with no ``reconstruction_id`` either: "Unlinked")."""
    sample_name: str | None
    """Its scan's sample (the scan's catalog); null without a scan or a sample."""
    reconstruction_id: int | None
    """The reconstruction it indexes (its other source); null when it indexes the scan's images."""
    n_points: int | None
    """The scan points it indexes; null without LaueGo parameters."""
    n_depths: int | None
    """The depths it indexes per point (the card's Depth Points); null when it indexes no depths."""
    box_size: int | None
    """The peak search box."""
    crystal_file: str | None
    """The structure file (the card's Structures File)."""
    author: str | None
    notes: str | None
    created_at: UTCTimestamp
    """When the run was submitted."""
    status: RunStatus
    n_inputs: int
    n_succeeded: int
    n_failed: int
    n_not_run: int
    n_processed: int
    """Inputs processed, with or without an error (``n_succeeded`` + ``n_failed``)."""


class IndexingRunPage(GridPage[IndexingRunSummary]):
    """A page of the indexations grid."""


# ---------------------------------------------------------------------------
# The results viewer


class LaueGoParameters(_ApiModel):
    """A LaueGo run's stored parameters (``lauego_indexing_parameters``), as recorded, for runs migrated from
    v1 as for runs submitted here. Paths are as stored (full paths)."""

    filename_prefixes: list[str]
    """The image file name patterns."""
    threshold: int | None
    """Null: automatic."""
    threshold_ratio: float | None
    """Null: automatic. A float (lauelab's type), such as 4.5."""
    max_rfactor: float
    box_size: int
    max_number: int | None
    """The most peaks detected in an image; null: all."""
    min_separation: int
    peak_shape: str
    scan_points: str
    """The scan point indices (a range text)."""
    scan_points_len: int
    depth_range: str | None
    """The depth indices (a range text); null when the run indexes no depths."""
    depth_range_len: int | None
    detector_crop_x1: int
    detector_crop_x2: int
    detector_crop_y1: int
    detector_crop_y2: int
    min_size: float
    max_peaks: int
    """The most peaks indexed in an image."""
    smooth: bool
    mask_file: str | None
    index_kev_max_calc: float
    index_kev_max_test: float
    index_angle_tolerance: float
    index_h: int
    index_k: int
    index_l: int
    index_cone: float
    energy_unit: str
    exposure_unit: str
    cosmic_filter: bool
    reciprocal_lattice_unit: str
    lattice_parameters_unit: str
    output_xml: str | None
    geometry_file: str
    crystal_file: str
    depth: str | None
    """Null: automatic."""
    beamline: str


class IndexingRunDetail(_ApiModel):
    """One indexing run (``GET /api/v1/indexing-runs/{id}``): the results viewer's header and Parameters tab."""

    id: int
    """The indexing run's ID (shown as ``I<id>``)."""
    method: str
    """The indexing method (``lauego`` or ``laue_matching``)."""
    scan_number: int | None
    """Its source scan; null when it has none."""
    reconstruction_id: int | None
    """The reconstruction it indexes; null when it indexes the scan's images."""
    job_id: int
    """The run's job (the run monitor's page)."""
    input_path: str
    """The folder its inputs were read from."""
    output_path: str | None
    """Its output folder as recorded."""
    author: str | None
    notes: str | None
    created_at: UTCTimestamp
    """When the run was submitted."""
    parameters: LaueGoParameters | None
    """The stored LaueGo parameters; null for another method or a run recorded without them."""


class ArtifactState(StrEnum):
    """What the viewer can show of a run, found in order (``indexing.results``)."""

    READY = "ready"
    """A validated results file (``output.h5``): the recorded one, else the run directory's."""
    XML_ONLY = "xml_only"
    """Only an Output XML: the viewer reads it, and converting it (``POST …/convert``) writes ``output.h5``."""
    AMBIGUOUS = "ambiguous"
    """Several XML files in the run directory and none recorded: a person chooses one (``POST …/select-xml``)."""
    WRITING = "writing"
    """Neither a results file nor an XML yet, and the run is queued or running."""
    EMPTY = "empty"
    """Neither a results file nor an XML, and the run has ended: the run directory is there (or none is recorded)
    with nothing the viewer reads."""
    NOT_ON_HOST = "not_on_host"
    """Neither a results file nor an XML, and the recorded run directory (or its mount) is absent or unreadable on
    this host: production's ``/net/...`` paths need not exist here."""
    NOT_RESPONDING = "not_responding"
    """The run directory's file system did not answer in time (``files.probe``): nothing was resolved."""


class XmlSource(StrEnum):
    """Where an ``xml_only`` artifact's Output XML came from (step 3 of the resolution order)."""

    RECORDED = "recorded"
    """The run's recorded Output XML."""
    CHOSEN = "chosen"
    """The one a person chose among the run directory's XML files (``POST …/select-xml``); it can be chosen again."""
    ONLY = "only"
    """The only XML in the run directory."""


class ConversionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    """``output.h5`` is written and recorded (the artifact is then ``ready``)."""
    FAILED = "failed"


class ConversionJob(_ApiModel):
    """A conversion of a run's Output XML to ``output.h5`` on the worker (job kind ``convert_results``): the run's
    ``conversion`` row, its own queue item, not a run, so it has no job page and is not in the run monitor."""

    id: str
    """Its ID: ``convert_results_<indexing ID>`` (a run has at most one)."""
    kind: Literal["convert_results"]
    indexing_id: int
    status: ConversionStatus
    message: str | None
    """What the conversion reported (e.g. ``converted: 4 frames, 5 patterns``) or why it failed; null
    while it waits or runs."""
    enqueued_at: UTCTimestamp | None
    started_at: UTCTimestamp | None
    ended_at: UTCTimestamp | None
    held: bool
    """Queued while a live session is open: no conversion starts until it has ended."""


class IndexingArtifact(_ApiModel):
    """A run's results as the viewer finds them (``GET /api/v1/indexing-runs/{id}/artifact``). Paths are shown,
    never requested by path."""

    indexing_id: int
    state: ArtifactState
    results_path: str | None
    """``ready``: the results file."""
    xml_path: str | None
    """``xml_only``: the Output XML the viewer reads."""
    xml_source: XmlSource | None
    """``xml_only``: where that XML came from; null in the other states."""
    xml_candidates: list[str]
    """``ambiguous``, or ``xml_only`` with a ``chosen`` XML: the XML files in the run directory, by name, to choose
    from (again); empty otherwise."""
    conversion_job_id: str | None
    """``xml_only``: the run's conversion job (``conversion``) when it has one."""
    conversion: ConversionJob | None
    """``xml_only``: the run's conversion while it waits or runs, or how it ended (kept until the run is converted
    again); the page polls this endpoint, then loads the results once the state is ``ready``."""
    problems: list[str]
    """Why the earlier steps of the resolution order were passed over."""
    results_version: str | None
    """``ready``: the results file's version (a digest of its identity: real path, size, modification and change
    times); another version is another file, as when a live run replaces its preview ``output.h5``, and the page
    reads the results again. Null in the other states."""
    live_open: bool
    """The run is a live run still open (``queued`` or ``running``): its results grow while it
    runs, and the page polls this every 5 s."""


class XmlChoice(BaseModel):
    """The Output XML that holds a run's results (``POST /api/v1/indexing-runs/{id}/artifact/select-xml``)."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    """One of the artifact's ``xml_candidates``."""


class OutputPathState(StrEnum):
    """A path an output names, as the portal reads it."""

    AVAILABLE = "available"
    """Mapped, inside the file roots, and a file on this host."""
    UNAVAILABLE = "unavailable"
    """No portal path: a remote run's value outside its run directory and the APS root, or one no prefix covers.
    Nothing is opened."""
    OUTSIDE_ROOTS = "outside_roots"
    """Mapped, but outside the file roots: not opened."""
    MISSING = "missing"
    """Mapped and allowed, but not a file this host can reach (missing, or under a folder the server may not
    open)."""
    NOT_RESPONDING = "not_responding"
    """Mapped, but its file system did not answer in time (``files.probe``): not opened."""


class OutputPath(_ApiModel):
    value: str
    """As the output wrote it."""
    path: str | None
    """As the portal sees it (``remote.paths.portal_path`` with the run's plan; a local run's path as written);
    null when unavailable."""
    state: OutputPathState


class GeometrySource(StrEnum):
    EMBEDDED = "embedded"
    """The geometry text the results file holds."""
    FILE = "file"
    """The geometry file the output names (``named``), mapped."""
    RECORDED = "recorded"
    """The run's recorded geometry file (``recorded``, its parameters' ``geometry_file``), mapped: an Output XML
    loads with it when it is available, before the one the XML names."""
    NONE = "none"
    """No geometry: the output names none, or none that can be opened here (the detector view then simulates no
    reflections)."""


class ResultsGeometry(_ApiModel):
    source: GeometrySource
    named: OutputPath | None
    """The geometry path the output names (``output.h5``'s ``/geometry@path``, the XML's ``geoFile``): the one
    used, else the first; null when it names none."""
    recorded: OutputPath | None
    """An Output XML's: the run's recorded geometry file (``geometry_file``); null for a results file."""


class ResultsCounts(_ApiModel):
    frames: int
    patterns: int
    peaks: int
    """Detected peaks."""


class AxisChoice(_ApiModel):
    value: str
    """lauelab's axis name (the map's ``axes``)."""
    label: str


class ColorModeChoice(_ApiModel):
    value: str
    """lauelab's colour mode (the map's ``color``)."""
    label: str
    scalar: bool
    """Colours by a number (a palette and a range) rather than an RGB colour per point."""
    available: bool
    reason: str | None
    """Why this run's results cannot use it; null when available."""


class IndexingResultsSummary(_ApiModel):
    """The results viewer's summary of a run (``GET /api/v1/indexing-runs/{id}/summary``), within the
    scope (``pattern0_only``, ``min_peaks``)."""

    indexing_id: int
    source: Literal["results", "xml"]
    """What was loaded: the results file, or the Output XML of an ``xml_only`` artifact."""
    path: str
    counts: ResultsCounts
    """The whole results, whatever the scope."""
    axes: list[AxisChoice]
    """The map axes with values in these results, in lauelab's order."""
    color_modes: list[ColorModeChoice]
    """lauelab's map colour modes, in its order."""
    steps: list[int]
    """The steps (frame positions) the scope keeps, in order."""
    default_step: int | None
    """The first of ``steps`` with a pattern, else the first; null when the scope keeps none."""
    structure: str | None
    """The crystal structure's name; null when the results hold no structure."""
    space_group: int | None
    geometry: ResultsGeometry


# ---------------------------------------------------------------------------
# The products' parameters and JSON answers


class MapColor(StrEnum):
    """lauelab's map colour modes (``COLOR_MODES``): four orientation colourings, then the scalars."""

    CUBIC_IPF = "cubic_ipf"
    RODRIGUES = "rodrigues"
    MISORIENTATION = "misorientation"
    POLE_HSV = "pole_hsv"
    N_INDEXED = "n_indexed"
    GOODNESS = "goodness"
    RMS_ERROR = "rms_error"
    N_PATTERNS = "n_patterns"


class Palette(StrEnum):
    """A scalar mode's palette (``terrain`` is Plotly's Earth). The browser maps the values."""

    VIRIDIS = "viridis"
    PLASMA = "plasma"
    INFERNO = "inferno"
    MAGMA = "magma"
    JET = "jet"
    RAINBOW = "rainbow"
    TERRAIN = "terrain"


class OrientationSymmetry(StrEnum):
    """The symmetry reduction of the Rodrigues and misorientation colours: ``auto`` is the crystal's when it is
    cubic or hexagonal, else none."""

    AUTO = "auto"
    CUBIC = "cubic"
    HEXAGONAL = "hexagonal"
    NONE = "none"


class PoleScheme(StrEnum):
    """The pole figure's colours: by position (HSV around the centre), cubic IPF, or one colour."""

    HSV = "hsv"
    IPF = "ipf"
    UNIFORM = "uniform"


class LegendColor(StrEnum):
    """The colour modes with a key image: the cubic IPF triangle and the HSV wheel (the pole figure's ``ipf`` and
    ``hsv`` schemes use the same two)."""

    CUBIC_IPF = "cubic_ipf"
    POLE_HSV = "pole_hsv"


class PatternDetails(_ApiModel):
    pattern_index: int
    """The pattern's rank in its frame (0: the primary pattern)."""
    n_indexed: int
    """Indexed peaks."""
    goodness: float | None
    rms_error_deg: float | None
    """RMS error in degrees."""
    indexed: bool
    """The pattern has a finite orientation."""
    in_scope: bool
    """The scope (``pattern0_only``, ``min_peaks``) keeps the pattern."""
    reciprocal: list[list[float]] | None = None
    """Rows a*, b*, c* in beamline coordinates (1/nm, with 2π); null without a finite orientation."""


class CrystalCellDetails(_ApiModel):
    """The results' nominal cell."""

    name: str | None
    space_group: int | None
    lengths_nm: list[float]
    """a, b, c (nm)."""
    angles_deg: list[float]
    """α, β, γ (degrees)."""
    reference_reciprocal: list[list[float]]
    """The unrotated cell's rows a*, b*, c* (1/nm, with 2π): lauelab's reference basis."""
    symmetry: Literal["cubic", "hexagonal", "none"]
    """The proper rotations of ``equivalent_settings``: lauelab's, which knows the cubic and hexagonal groups."""
    equivalent_settings: list[list[list[int]]]
    """Each proper rotation of the crystal as an integer matrix P: P·R is an equivalent setting of a pattern's rows
    R (a, b, c relabelled, the same lattice). The identity alone for ``none``."""


class AxisCoordinate(_ApiModel):
    axis: str
    """lauelab's axis name."""
    label: str
    value: float | None


class PointDetails(_ApiModel):
    """A step's details for the map's hover and click cards (``GET …/points/{step}``)."""

    step: int
    """The frame's position."""
    frame_id: int | str
    """The frame's identity in the results (the manifest index of a native run, the step order of an XML)."""
    sample_position: list[float | None]
    """X, Y and Z motor positions (µm)."""
    depth: float | None
    """µm; null for a frame that is not depth-resolved."""
    n_peaks: int
    """Detected peaks."""
    in_scope: bool
    """The scope keeps the step (a selected pattern, or a frame-only record)."""
    patterns: list[PatternDetails]
    """Every pattern of the frame, in the results' order."""
    coordinates: list[AxisCoordinate]
    """The step's coordinates on the requested ``axes``."""
    crystal: CrystalCellDetails | None = None
    """The results' nominal cell; null without a crystal."""


MAX_SELECTION_ENTRIES = 48 * 700
"""The most pairs a request may name: 700 patterns (the bound on the distinct ones with an orientation,
``science.products.MAX_SELECTION_PATTERNS``), each once per pole of a cubic family (at most 48), as a pole-figure
lasso may send them."""


class SelectionStatsRequest(_ApiModel):
    patterns: list[
        Annotated[list[Annotated[int, Field(ge=0, le=SQLITE_INTEGER_MAX)]], Field(min_length=2, max_length=2)]
    ] = Field(max_length=MAX_SELECTION_ENTRIES)
    """The selected patterns as ``[step, pattern_index]`` pairs (the map's and the pole figure's ``step`` and
    ``pattern_index``). A pair named twice counts once: at most 700 distinct patterns with an orientation
    (a 422 above), in at most 33,600 entries."""


class SelectionStats(_ApiModel):
    """The pairwise misorientation of a selection's orientations."""

    n_patterns: int
    """The distinct patterns selected."""
    n_oriented: int
    """Of those, the patterns with a finite orientation: the ones compared."""
    n_pairs: int
    mean: float | None
    """Degrees; null with fewer than two oriented patterns (as are ``min`` and ``max``)."""
    min: float | None
    max: float | None
    symmetry: Literal["cubic", "hexagonal", "none"]
    """The reduction applied: the crystal's proper rotations when it is cubic or hexagonal, else none."""


# ---------------------------------------------------------------------------
# The detector view


class DetectedPeak(_ApiModel):
    peak_index: int
    x: float | None
    """Pixel of the frame, across (null when not finite)."""
    y: float | None
    """Pixel of the frame, down."""
    intensity: float | None
    indexed: bool
    """A pattern of the step indexes it."""


class IndexedPeak(_ApiModel):
    peak_index: int
    """The detected peak it indexes."""
    h: int
    k: int
    l: int  # noqa: E741 (Miller index)
    x: float | None
    """Where the pattern predicts it, in pixels of the frame (null where the detector cannot place it)."""
    y: float | None


class SimulatedReflection(_ApiModel):
    h: int
    k: int
    l: int  # noqa: E741 (Miller index)
    x: float
    y: float
    energy_kev: float
    relative_intensity: float
    """Uncalibrated."""


class DetectorPattern(_ApiModel):
    pattern_index: int
    n_indexed: int
    goodness: float | None
    rms_error_deg: float | None
    indexed: bool
    """The pattern has a finite orientation."""
    in_scope: bool
    """The scope keeps the pattern."""
    peaks: list[IndexedPeak]
    """Its indexed peaks, with their hkl and predicted positions."""
    simulated: list[SimulatedReflection] | None
    """The reflections a simulation of the pattern predicts on the frame that it did not index; null without a
    simulation (not asked, or failed: ``simulation_error``)."""


class DetectorImageInfo(_ApiModel):
    available: bool
    """The image is on this host (``GET …/detector/{step}/image`` serves it)."""
    path: str | None
    """The file read: the ``inputImage`` located, mapped and inside the file roots."""
    dataset: str | None
    """The HDF5 dataset read, or the point and depth index of a reconstructed frame."""
    reason: str | None
    """Why the image is not available: the paths tried and why each was not opened."""


class DetectorSummary(_ApiModel):
    step: int
    frame_id: int | str
    in_scope: bool
    """The scope keeps the step."""
    sample_position: list[float | None]
    """X, Y and Z motor positions (µm)."""
    detector_id: str | None
    n_detected: int
    """Detected peaks."""
    n_indexed: int
    """Detected peaks a pattern of the step indexes."""
    indexed_fraction: float
    """``n_indexed / n_detected`` (0 without peaks)."""
    input_image: str | None
    """The ``inputImage`` the results record for the step."""
    image: DetectorImageInfo


class DetectorOverlays(_ApiModel):
    """A step's detector overlays (``GET …/detector/{step}/overlays``)."""

    summary: DetectorSummary
    width: int
    """The frame's size in pixels (x from 0 to width - 1)."""
    height: int
    detected: list[DetectedPeak]
    patterns: list[DetectorPattern]
    """Every pattern of the step, in the results' order."""
    energy_range_kev: list[float] | None
    """The simulation's window, as asked; null without a simulation."""
    simulation_error: str | None
    """Why the simulation failed (lauelab's message); the other layers are complete."""


# ---------------------------------------------------------------------------
# The peaks and patterns tables (the grid protocol)

MAX_MIN_PEAKS = 100_000
"""``min_peaks`` above this is refused: no frame holds that many peaks."""

PATTERN0_ONLY_DESCRIPTION = "Only pattern rank 0 of each frame (the primary pattern); false: every pattern."
MIN_PEAKS_DESCRIPTION = (
    "The fewest detected peaks a step needs to be kept (default 4); 0 keeps every step. The scope bar's "
    "'Skip steps with peaks ≤ N' sends N + 1, and 0 for N = 0."
)


class ResultsGridParams(GridParams):
    """The grid query parameters and the viewer's scope: a grid's parameters admit no others, so the scope
    is part of them."""

    pattern0_only: bool = Field(default=DEFAULT_PATTERN0_ONLY, description=PATTERN0_ONLY_DESCRIPTION)
    min_peaks: int = Field(default=DEFAULT_MIN_PEAKS, ge=0, le=MAX_MIN_PEAKS, description=MIN_PEAKS_DESCRIPTION)


class PeakRow(_ApiModel):
    """One indexed peak: a pattern's assignment, with its detected peak and its pattern."""

    id: int
    """The row's position in the run's whole table: unique, the same in every scope (the grid's row ID)."""
    step: int
    frame_id: str
    """The frame's identity, as text (it sorts by step)."""
    pattern_index: int
    h: int
    k: int
    l: int  # noqa: E741 (Miller index)
    peak_index: int
    x_pixel: float | None
    y_pixel: float | None
    intensity: float | None
    integral: float | None
    qx: float | None
    qy: float | None
    qz: float | None
    rms_error_deg: float | None
    goodness: float | None
    n_peaks: int
    """The step's detected peaks."""
    energy_kev: float | None
    error_deg: float | None
    predicted_intensity: float | None
    input_image: str | None
    hwhm_x: float | None
    hwhm_y: float | None
    tilt: float | None
    chisq: float | None
    background: float | None
    pattern_n_indexed: int
    pattern_indexed_fraction: float | None
    """``pattern_n_indexed / n_peaks``; null without peaks."""


class PeakPage(GridPage[PeakRow]):
    """A page of a run's indexed peaks."""


class PatternRow(_ApiModel):
    """One pattern."""

    id: int
    """The row's position in the run's whole table: unique, the same in every scope (the grid's row ID)."""
    step: int
    frame_id: str
    """The frame's identity, as text (it sorts by step)."""
    scan_number: float | None
    pattern_index: int
    n_indexed: int
    n_peaks: int
    """The step's detected peaks."""
    indexed_fraction: float | None
    rms_error_deg: float | None
    goodness: float | None
    n_patterns: int
    """The step's patterns."""
    astar: str
    """The reciprocal vector a* (1/nm), three numbers of six significant digits."""
    bstar: str
    cstar: str
    structure: str | None
    space_group: int | None
    x_um: float | None
    y_um: float | None
    z_um: float | None
    depth_um: float | None
    frame_energy_kev: float | None
    input_image: str | None


class PatternPage(GridPage[PatternRow]):
    """A page of a run's patterns."""
