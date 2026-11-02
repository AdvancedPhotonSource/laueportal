"""API schemas of the reconstruction domain: the reconstructions grid and the scan detail's reconstruction card
and the wire viewer's run, artifact, points and a point's images. The names are
part of the contract (Orval type names)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from laueportal.core.paging import GridPage
from laueportal.core.timeutil import UTCTimestamp
from laueportal.indexing.schemas import OutputPathState
from laueportal.jobs.schemas import RunStatus


class _ApiModel(BaseModel):
    """API schemas: attribute docstrings become the OpenAPI descriptions (and Orval's comments)."""

    model_config = ConfigDict(use_attribute_docstrings=True)


class ReconstructionRunSummary(_ApiModel):
    """One row of the reconstructions grid (``GET /api/v1/reconstruction-runs``) and of
    the scan detail's reconstruction card."""

    id: int
    """The reconstruction's ID (shown as ``R<id>``)."""
    method: str
    """The reconstruction method (``wire``)."""
    scan_number: int | None
    """Its source, the scan it reconstructs; null for an unlinked run ("Unlinked")."""
    sample_name: str | None
    """Its scan's sample (the scan's catalog); null without a scan or a sample."""
    n_points: int | None
    """The scan points it reconstructs; null without wire parameters."""
    percent_brightest: float | None
    """The percentage of brightest pixels used (the card's Pixels)."""
    depth_start: float | None
    depth_end: float | None
    author: str | None
    notes: str | None
    created_at: UTCTimestamp
    """When the run was submitted."""
    start_time: UTCTimestamp | None
    """When the run started; null until it does."""
    finish_time: UTCTimestamp | None
    """When the run ended; null until it does."""
    status: RunStatus
    n_inputs: int
    n_succeeded: int
    n_failed: int
    n_not_run: int
    n_processed: int
    """Inputs processed, with or without an error (``n_succeeded`` + ``n_failed``)."""


class ReconstructionRunPage(GridPage[ReconstructionRunSummary]):
    """A page of the reconstructions grid."""


# ---------------------------------------------------------------------------
# The wire viewer


class WireParameters(_ApiModel):
    """A wire run's stored parameters (``wire_reconstruction_parameters``), as recorded, for runs migrated from
    v1 as for runs submitted here. Paths are as stored (full paths)."""

    filename_prefixes: list[str]
    """The image file name patterns."""
    geometry_file: str
    percent_brightest: float
    wire_edges: str
    """``leading``, ``trailing`` or ``both``."""
    depth_start: float
    """µm."""
    depth_end: float
    """µm."""
    depth_resolution: float
    """µm."""
    frame_skip: int | None
    """Null for a run recorded before the skip options (it used lauelab's default)."""
    wire_skip: int | None
    """Null for a run recorded before the skip options (it used lauelab's default)."""
    num_threads: int
    memory_limit_mb: int
    scan_points: str
    """The scan point indices (a range text)."""
    scan_points_len: int
    verbose: int


class RootRelativePath(_ApiModel):
    """A recorded path inside one of the file roots (``files.roots``), relative to it: the path without
    its root (the innermost root whose folder is a whole-component prefix of the path as recorded; symbolic links
    are not followed)."""

    root: str
    """The root's name."""
    relative: str
    """The path relative to the root; empty for the root itself."""


class ReconstructionRelativePaths(_ApiModel):
    """A run's recorded paths relative to the file root that holds each; null for one not recorded or outside every
    root."""

    input_path: RootRelativePath | None
    output_path: RootRelativePath | None
    geometry_file: RootRelativePath | None
    """The parameters' ``geometry_file``."""


class ReconstructionRunDetail(_ApiModel):
    """One reconstruction run (``GET /api/v1/reconstruction-runs/{id}``): the wire viewer's header and Parameters
    tab."""

    id: int
    """The reconstruction's ID (shown as ``R<id>``)."""
    method: str
    """The reconstruction method (``wire``)."""
    scan_number: int | None
    """Its source scan; null for an unlinked run."""
    job_id: int
    """The run's job (the run monitor's page)."""
    input_path: str
    """The folder its inputs were read from."""
    output_path: str | None
    """Its run directory as recorded."""
    author: str | None
    notes: str | None
    created_at: UTCTimestamp
    """When the run was submitted."""
    parameters: WireParameters | None
    """The stored wire parameters; null for a run recorded without them."""
    relative_paths: ReconstructionRelativePaths
    """Its paths relative to the file root that holds them."""


class ReconstructionArtifactState(StrEnum):
    """What the wire viewer can show of a run (``reconstruction.views.locate_artifact``)."""

    READY = "ready"
    """A scan catalog or per-depth files, also while the run still writes them (its points so far)."""
    WRITING = "writing"
    """Nothing yet, and the run is queued or running: it has not published its scan catalog."""
    EMPTY = "empty"
    """Nothing, and the run has ended: the run directory is there (or none is recorded) without a reconstruction."""
    NOT_ON_HOST = "not_on_host"
    """The recorded run directory (or its mount) is absent or unreadable on this host: production's ``/net/...``
    paths need not exist here."""
    NOT_RESPONDING = "not_responding"
    """The run directory's file system did not answer in time (``files.probe``): nothing was looked at."""


class ReconstructionFormat(StrEnum):
    SCAN = "scan"
    """lauelab's scan catalog, ``<run>/reconstruction/scan.h5``, with a point file per complete point."""
    PER_DEPTH = "per_depth"
    """Older per-depth files in the run directory: ``<point>_<depth index>.h5`` and ``<point>_summary.txt``."""


class ReconstructionCatalogStatus(StrEnum):
    """The run status a scan catalog's snapshot records (lauelab's)."""

    RUNNING = "running"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ReconstructionArtifact(_ApiModel):
    """A run's reconstruction as the viewer finds it (``GET /api/v1/reconstruction-runs/{id}/artifact``); the page
    polls it while the run is active. Paths are shown, never requested by path."""

    reconstruction_id: int
    state: ReconstructionArtifactState
    run_status: RunStatus
    """The run's status (its job's): the page polls while it is ``queued`` or ``running``."""
    live_open: bool
    """The run is a live run still open (``queued`` or ``running``): its points arrive while it
    runs, in arrival order, and the page offers "Follow newest point"."""
    format: ReconstructionFormat | None
    """``ready``: the format found; null otherwise."""
    path: str | None
    """``ready``: the scan catalog, or the run directory holding the per-depth files."""
    catalog_status: ReconstructionCatalogStatus | None
    """``ready`` with a scan catalog: the run status its latest snapshot records; null otherwise."""
    points_complete: int | None
    """``ready``: the points with images so far ("x of y points complete")."""
    points_total: int | None
    """``ready``: every point of the catalog, or every per-depth summary."""


class ReconstructionPointStatus(StrEnum):
    """A point's status: lauelab's catalog statuses, and ``no_frames`` for the older per-depth files."""

    PENDING = "pending"
    WRITING = "writing"
    COMPLETE = "complete"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNATTEMPTED = "unattempted"
    NO_FRAMES = "no_frames"
    """A per-depth summary without depth files next to it."""


FrameShape = Annotated[list[int], Field(min_length=2, max_length=2)]


class ReconstructionPoint(_ApiModel):
    """One point of a reconstruction (a row of the Points tab), as stored: no formatting."""

    point_id: str
    """The point (lauelab's point ID: its input file's stem; per-depth: the summary's prefix)."""
    index: int
    """Its position in the catalog (per-depth: in point ID order)."""
    status: ReconstructionPointStatus
    explanation: str | None
    """Why a point that is not complete has no images (e.g. "Point wire_2 has not been
    reconstructed yet; press Refresh after it completes. It has no images."); null for a complete point."""
    n_depths: int | None
    """Its depth frames (Depths): known for a complete point."""
    depth_first_um: float | None
    """The first and last frame's depth (µm): known for a complete point of a scan catalog (per-depth: once the
    point is opened)."""
    depth_last_um: float | None
    frame_shape: FrameShape | None
    """One depth frame's ``[y, x]`` (rows, columns): known for a complete point of a scan catalog."""
    dtype: str | None
    """The stored pixel type (``uint16``, ``float32``): known for a complete point of a scan catalog."""
    sample_x_um: float | None
    """The sample position (µm) the catalog records; null when a component is missing, and for per-depth files."""
    sample_y_um: float | None
    sample_z_um: float | None
    source: str | None
    """The input file it was reconstructed from, as the catalog records it (a remote run's: as the remote host
    wrote it); null for per-depth files."""
    source_path: str | None
    """``source`` as the portal sees it (a remote run's mapped with its plan; a
    local run's as written); null when ``source_state`` is ``unavailable``: nothing is opened."""
    source_state: OutputPathState | None
    """``available`` (mapped, inside the file roots and a file here), ``unavailable``, ``outside_roots``,
    ``missing`` or ``not_responding`` (its file system did not answer); null without a source."""
    error: str | None
    """The error a failed point records; for a per-depth summary without frames, "no per-depth files next to
    the summary"."""


class ReconstructionPointList(_ApiModel):
    """Every point of a run's reconstruction (``GET /api/v1/reconstruction-runs/{id}/points``), held whole by the page
    (a few hundred at most: the DataGrid's ``rows``)."""

    reconstruction_id: int
    state: ReconstructionArtifactState
    """The artifact's state (``ReconstructionArtifact``): rows only when ``ready``."""
    rows: list[ReconstructionPoint]
    """Every point, in catalog order (per-depth: by point ID)."""


# ---------------------------------------------------------------------------
# A point's images (the Depth View and the ROI inspector)


class ReconstructionTrace(_ApiModel):
    """A point's full-frame intensity through depth (the Depth View's trace, lauelab's ``depth_trace``)."""

    reconstruction_id: int
    point_id: str
    depth_um: list[float]
    """Each depth frame's depth (µm), by depth index."""
    values: list[float | None]
    """The sum of each depth frame's stored pixels, by depth index (null for a value that is not finite)."""
    brightest_index: int
    """The depth index of the largest value (the first of equal ones): where the Depth View opens the point."""


class ReconstructionReferenceKind(StrEnum):
    """A reference image for the ROI inspector (lauelab's ``REFERENCE_OPTIONS``, in their order)."""

    SUM_RECONSTRUCTED = "sum_reconstructed"
    """The sum of the stored (reconstructed) depth frames."""
    FIRST_RAW = "first_raw"
    """The first raw detector frame (counts before filtering and normalization); a scan catalog's points only."""
    SUM_RAW = "sum_raw"
    """The sum of the raw detector frames; a scan catalog's points only."""


MAX_ROIS = 32
"""The ROIs one request may trace (four times lauelab's eight ROI colours): each reads its pixels at every depth."""
MAX_ROI_SIZE = 65535
"""An ROI's largest side in pixels, beyond any frame."""


class ReconstructionRoi(_ApiModel):
    """A square ROI as the page places it: its side, centred as near as it can be to a position."""

    x: Annotated[float, Field(allow_inf_nan=False)]
    """The position in stored-image pixels, zero-based (a whole number is a pixel's centre): across."""
    y: Annotated[float, Field(allow_inf_nan=False)]
    """Down."""
    size: Annotated[int, Field(ge=1, le=MAX_ROI_SIZE)]
    """The square's side in pixels."""


class ReconstructionRoiTracesRequest(_ApiModel):
    """The ROIs to trace through a point's depths."""

    rois: list[ReconstructionRoi] = Field(max_length=MAX_ROIS)
    """The ROIs, in the order of the answer's traces; each must lie inside the image (none is clipped or
    shifted)."""
    normalize: bool = False
    """Divide each trace by its own maximum (lauelab's ``plot_roi_traces(normalized=True)``)."""


class ReconstructionRoiTrace(_ApiModel):
    """One ROI's trace through depth."""

    bounds: Annotated[list[int], Field(min_length=4, max_length=4)]
    """The pixels it sums, ``[y0, y1, x0, x1]`` (half-open: rows ``y0`` to ``y1 - 1``)."""
    values: list[float | None] | None
    """The sum of its stored pixels at each depth, or that divided by its maximum with ``normalize`` (null for a
    value that is not finite); null when it cannot be normalized (``reason``)."""
    reason: str | None
    """Why a trace cannot be normalized (its maximum is not positive), as lauelab says it; null otherwise."""


class ReconstructionRoiTraces(_ApiModel):
    """One trace per ROI of a point, in the request's order, against the depth (µm) and the depth index."""

    reconstruction_id: int
    point_id: str
    normalize: bool
    depth_um: list[float]
    """Each depth frame's depth (µm), by depth index."""
    depth_index: list[int]
    """The depth indices (``0`` to ``n - 1``), the other axis."""
    traces: list[ReconstructionRoiTrace]
