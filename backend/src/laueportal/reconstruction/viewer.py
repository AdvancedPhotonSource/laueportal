"""The wire viewer's service: a run's detail (its job and stored parameters), its artifact, its points and
a point's images.

- **Detail:** the run's database rows only; its paths are also shown relative to the file root that holds them
  (``root_relative``: lexical, so nothing on disk is touched).
- **Artifact:** ``reconstruction.views.find_artifact`` in the run directory (the run's ``output_path``,
  read as recorded), through ``files.probe`` (a hung mount is ``not_responding`` within the probe's timeout; the
  catalog is opened there too, which on NFS asks the server): the scan catalog (``reconstruction/scan.h5``) or the older
  per-depth files, read through ``science.recon``. A run that
  still writes it shows its points so far; without one, a run directory that is absent or unreadable is
  ``not_on_host``, else nothing yet is ``writing`` while the run is queued or running, and ``empty`` after. A
  located file lauelab cannot read is a 409 (``ReconstructionUnreadable``).
- **Points:** every point of the artifact, with the explanation for a point without images and its source
  mapped (``indexing.viewer.mapped_path``: a remote run's with its plan,
  read once; an unavailable one is shown, never opened).
- **A point's images** (``point_images``, then ``depth_frame``, ``trace``, ``reference_image``, ``roi_traces``): a
  complete point's depth frames, full trace, reference images and ROI traces, read by ``science.recon`` through the
  app's ``ProductCache`` (one computation per product, however many requests ask). Every point file is admitted
  first (``_admission``: mapped with a remote run's plan, inside the file roots and a file here, checked through
  ``files.probe``), so one that is not on this host is a 404 with the reason and is never opened, and one whose
  file system does not answer a 503 (``files.probe.NotResponding``).
- **No database connection across file I/O:** every route reads the run in a session of its own and closes it
  (``run_files``) before it touches the run directory, which may be on a slow or hung mount, or waits for a product
  another request is computing.
- **Revalidation** (``core.etag``): the artifact is tagged before it is read, from the run's status and the
  catalog's (or per-depth folder's) identity, so a poll of an unchanged run is a 304 without opening HDF5; the
  points by their content, which also depends on the source files (their state) and the run's plan; a point's
  frames, trace and references by its point file's identity (``images_tag``), so a finished point's never change
  and a point the running run rewrites gets new ones.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from laueportal.core import etag
from laueportal.core.db import session_scope
from laueportal.core.problems import ProblemError, ProblemItem, problems_detail
from laueportal.core.settings import Settings
from laueportal.core.text import quoted, shortened
from laueportal.files.probe import NotResponding, bounded, default_probe, open_check
from laueportal.files.roots import RealRoots, is_utf8, real_roots, shown
from laueportal.indexing.products import PRODUCT_FAILED
from laueportal.indexing.schemas import OutputPathState
from laueportal.indexing.viewer import mapped_path
from laueportal.jobs.live import live_marker
from laueportal.jobs.models import Job
from laueportal.jobs.schemas import RunStatus
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.reconstruction.schemas import (
    ReconstructionArtifact,
    ReconstructionArtifactState,
    ReconstructionCatalogStatus,
    ReconstructionFormat,
    ReconstructionPoint,
    ReconstructionPointList,
    ReconstructionPointStatus,
    ReconstructionReferenceKind,
    ReconstructionRelativePaths,
    ReconstructionRoi,
    ReconstructionRoiTrace,
    ReconstructionRoiTraces,
    ReconstructionRoiTracesRequest,
    ReconstructionRunDetail,
    ReconstructionTrace,
    RootRelativePath,
    WireParameters,
)
from laueportal.reconstruction.views import find_artifact
from laueportal.remote.paths import run_path_map
from laueportal.science import recon
from laueportal.science.detector import frame_bytes

T = TypeVar("T")

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)

POINT_STATUS_MESSAGES = {
    "pending": "has not been reconstructed yet; press Refresh after it completes",
    "writing": "is being reconstructed; press Refresh after it completes",
    "unattempted": "was not reconstructed because the run stopped first",
    "interrupted": "was interrupted before its file was recorded",
    recon.STATUS_NO_FRAMES: "has no per-depth files next to its summary",
}
"""Why a point has no images, by status, and for a per-depth summary without frames its error text (its status was
``no frames``). A failed point gives its error."""


class ReconstructionRunNotFound(ProblemError):
    def __init__(self, reconstruction_id: int) -> None:
        super().__init__(404, "Reconstruction run not found", f"No reconstruction run R{reconstruction_id}")


RECONSTRUCTION_UNREADABLE = "urn:laueportal:problem:reconstruction-unreadable"
"""``ReconstructionUnreadable``'s problem type."""


class ReconstructionUnreadable(ProblemError):
    """The run's scan catalog or per-depth folder exists but cannot be read (a 409, as the indexing viewer's unreadable
    results: the run's state, not the request's fault)."""

    def __init__(self, path: str, error: BaseException) -> None:
        super().__init__(
            409,
            "Reconstruction unreadable",
            f"{shown(path)} cannot be read: {quoted(f'{type(error).__name__}: {error}')}",
            problem_type=RECONSTRUCTION_UNREADABLE,
        )


# ---------------------------------------------------------------------------
# The run


def load_run(session: Session, reconstruction_id: int) -> ReconstructionRun:
    """The run with its parameters and job (readable once the session closes); 404 for an unknown run."""
    statement = (
        select(ReconstructionRun)
        .where(ReconstructionRun.id == reconstruction_id)
        .options(joinedload(ReconstructionRun.wire_parameters), joinedload(ReconstructionRun.job))
    )
    run = session.scalars(statement).one_or_none()
    if run is None:
        raise ReconstructionRunNotFound(reconstruction_id)
    return run


def root_relative(path: str | None, roots: Mapping[str, Path]) -> RootRelativePath | None:
    """``path`` relative to the innermost file root whose folder is a whole-component prefix of it, compared as text
    (normalized, symbolic links not followed: nothing is read); None for no path or one outside every root."""

    if not path or not os.path.isabs(path):
        return None
    candidate = PurePosixPath(os.path.normpath(path))
    match: tuple[str, PurePosixPath] | None = None
    for name, root in roots.items():
        folder = PurePosixPath(os.path.normpath(root))
        if candidate.is_relative_to(folder) and (match is None or len(folder.parts) > len(match[1].parts)):
            match = (name, folder)
    if match is None:
        return None
    relative = candidate.relative_to(match[1]).as_posix()
    return RootRelativePath(root=match[0], relative="" if relative == "." else relative)


def run_detail(session: Session, reconstruction_id: int, *, settings: Settings) -> ReconstructionRunDetail:
    """The run, its job and its stored parameters (the viewer's header and Parameters tab), with its paths relative
    to their roots; reads the database only. 404 for an unknown run."""
    run = load_run(session, reconstruction_id)
    stored = run.wire_parameters
    roots = settings.files.roots
    relative = ReconstructionRelativePaths(
        input_path=root_relative(run.input_path, roots),
        output_path=root_relative(run.output_path, roots),
        geometry_file=root_relative(stored.geometry_file if stored is not None else None, roots),
    )
    return ReconstructionRunDetail(
        id=run.id,
        method=run.method,
        scan_number=run.scan_number,
        job_id=run.job_id,
        input_path=run.input_path,
        output_path=run.output_path,
        author=run.author,
        notes=run.notes,
        created_at=run.created_at,
        parameters=None if stored is None else WireParameters.model_validate(stored, from_attributes=True),
        relative_paths=relative,
    )


@dataclass(frozen=True)
class RunFiles:
    """What the artifact and the points need of the run's rows."""

    reconstruction_id: int
    run_directory: str | None
    """The run's ``output_path``."""
    run_status: RunStatus
    live_open: bool = False
    """A live run still open (Queued or Running: its queue identity, ``jobs.live.live_marker``)."""


def run_files(sessions: sessionmaker[Session], reconstruction_id: int) -> RunFiles:
    """The run's directory, status and whether it is an open live run, read in a session of its own that is closed
    before the caller touches any file (a hung mount must not hold a database connection). 404 for an unknown
    run."""
    with session_scope(sessions) as session:
        row = session.execute(
            select(ReconstructionRun.output_path, Job.status, live_marker().label("live"))
            .join(Job, Job.job_id == ReconstructionRun.job_id)
            .where(ReconstructionRun.id == reconstruction_id)
        ).one_or_none()
    if row is None:
        raise ReconstructionRunNotFound(reconstruction_id)
    status = RunStatus.of(row.status)
    return RunFiles(reconstruction_id, row.output_path, status, live_open=bool(row.live) and status in ACTIVE)


# ---------------------------------------------------------------------------
# The artifact


@dataclass(frozen=True)
class Located:
    """A run's artifact as found (``locate``), not read yet."""

    files: RunFiles
    artifact: recon.Artifact | None
    identity: tuple[object, ...] | None
    """The catalog's (or per-depth folder's) identity (``science.recon.identity``); None without an artifact."""
    on_host: bool = True
    """Whether the run directory is there and can be listed (``views.Found.on_host``)."""
    responding: bool = True
    """Whether its file system answered (``files.probe``); nothing was looked at otherwise."""

    @property
    def state(self) -> ReconstructionArtifactState:
        if self.artifact is not None:
            return ReconstructionArtifactState.READY
        if not self.responding:
            return ReconstructionArtifactState.NOT_RESPONDING
        if not self.on_host:
            return ReconstructionArtifactState.NOT_ON_HOST
        if self.files.run_status in ACTIVE:
            return ReconstructionArtifactState.WRITING
        return ReconstructionArtifactState.EMPTY


def _located(files: RunFiles) -> Located:
    found = find_artifact(files.run_directory)
    if found.artifact is None:
        return Located(files, None, None, on_host=found.on_host)
    try:
        identity = recon.identity(found.artifact)
        if found.artifact.kind == recon.KIND_SCAN:
            open_check(found.artifact.path)  # what the request reads next: on NFS, an open asks the server
    except OSError as error:
        raise ReconstructionUnreadable(found.artifact.path, error) from error
    return Located(files, found.artifact, identity)


def locate(files: RunFiles) -> Located:
    """Find the run's scan catalog or per-depth files and their identity, through ``files.probe`` (a run directory
    whose file system does not answer is not responding); reads no HDF5."""
    if not files.run_directory:
        return _located(files)
    try:
        return bounded(files.run_directory, lambda: _located(files))
    except NotResponding:
        return Located(files, None, None, responding=False)


def artifact_tag(located: Located) -> str:
    """The artifact's entity tag: everything ``artifact`` answers from (the run's directory, status and whether it is
    an open live run, the artifact's identity, the state) and the code."""
    files = located.files
    return etag.entity_tag(
        "reconstruction-artifact",
        files.reconstruction_id,
        files.run_directory,
        files.run_status,
        files.live_open,
        located.identity,
        located.state,
    )


def _table(artifact: recon.Artifact, cache: recon.ProductCache) -> recon.PointTable:
    """The artifact's points (``science.recon.point_table``). A per-depth point whose name is not UTF-8 (a listed
    name ``os`` keeps as lone surrogates) is left out, of the rows and the counts: the API can neither send its ID
    nor be asked for it. A catalog's IDs are UTF-8 (lauelab writes them so)."""
    try:
        table = recon.point_table(artifact, cache=cache)
    except (OSError, ValueError) as error:
        raise ReconstructionUnreadable(artifact.path, error) from error
    if artifact.kind == recon.KIND_PER_DEPTH:
        table = recon.PointTable([row for row in table.rows if is_utf8(row.point_id)], table.run_status)
    return table


def artifact(located: Located, *, cache: recon.ProductCache) -> ReconstructionArtifact:
    """The artifact's state, format and points complete so far (reads the catalog's metadata, or lists the
    per-depth folder)."""
    files = located.files
    found = located.artifact
    if found is None:
        return ReconstructionArtifact(
            reconstruction_id=files.reconstruction_id,
            state=located.state,
            run_status=files.run_status,
            live_open=files.live_open,
            format=None,
            path=None,
            catalog_status=None,
            points_complete=None,
            points_total=None,
        )
    table = _table(found, cache)
    return ReconstructionArtifact(
        reconstruction_id=files.reconstruction_id,
        state=located.state,
        run_status=files.run_status,
        live_open=files.live_open,
        format=ReconstructionFormat(found.kind),
        path=shown(found.path),
        catalog_status=None if table.run_status is None else ReconstructionCatalogStatus(table.run_status),
        points_complete=sum(row.status == "complete" for row in table.rows),
        points_total=len(table.rows),
    )


# ---------------------------------------------------------------------------
# The points


def explanation(row: recon.PointRow) -> str | None:
    """Why a point that is not complete has no images; None for a complete point."""
    if row.status == "complete":
        return None
    if row.status == "failed":
        detail = f"failed: {row.error}" if row.error else "failed"
    else:
        detail = POINT_STATUS_MESSAGES.get(row.status, f"is {row.status}")
    return f"Point {row.point_id} {detail}. It has no images."


def _point(
    row: recon.PointRow, *, mappings: Mapping[str, str] | None, base: str, settings: Settings, roots: RealRoots
) -> ReconstructionPoint:
    source = None
    if row.source is not None:
        source = mapped_path(row.source, mappings=mappings, base=base, settings=settings, roots=roots).shown
    x, y, z = row.sample_position_um if row.sample_position_um is not None else (None, None, None)
    return ReconstructionPoint(
        point_id=row.point_id,
        index=row.index,
        status=ReconstructionPointStatus(row.status),
        explanation=explanation(row),
        n_depths=row.n_depths,
        depth_first_um=row.depth_first_um,
        depth_last_um=row.depth_last_um,
        frame_shape=None if row.frame_shape is None else list(row.frame_shape),
        dtype=row.dtype,
        sample_x_um=x,
        sample_y_um=y,
        sample_z_um=z,
        source=None if source is None else source.value,
        source_path=None if source is None else source.path,
        source_state=None if source is None else source.state,
        error=row.error,
    )


def points(located: Located, *, settings: Settings, cache: recon.ProductCache) -> ReconstructionPointList:
    """Every point of the artifact (none unless it is ``ready``), its source mapped with the run's plan (read
    once: ``run_path_map``; a local run has none) and confined to the file roots (resolved once for them all)."""
    files = located.files
    found = located.artifact
    if found is None:
        return ReconstructionPointList(reconstruction_id=files.reconstruction_id, state=located.state, rows=[])
    table = _table(found, cache)
    run_directory = files.run_directory or ""  # an artifact is found only in a run directory
    mappings = run_path_map(run_directory) if run_directory else None
    roots = real_roots(settings, requested=False)
    rows = [_point(row, mappings=mappings, base=run_directory, settings=settings, roots=roots) for row in table.rows]
    return ReconstructionPointList(reconstruction_id=files.reconstruction_id, state=located.state, rows=rows)


def points_tag(answer: ReconstructionPointList) -> str:
    """The points' entity tag: their content (which depends on the catalog, the run's plan and the source files'
    states) and the code."""
    return etag.entity_tag("reconstruction-points", answer.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# A point's images: its depth frames, its full trace, its reference images and its ROI traces

COMPUTATIONS_AT_ONCE = 2
"""A point's computed images made at once (the trace, reference and ROI-trace routes' limiter,
``app.state.point_computations``; docs/contributing/backend.md, "Long work"); the others wait for a turn outside the
thread pool. Each reads a point's frames: on the measured sample (101 frames of 2048 x 2048) a per-depth point's trace
reads 808 MiB in 1.1 s at about +10 MiB, a reference sum takes 1.3 to 2.5 s at about +95 MiB, and an ROI pass reads at
most ``science.recon.ROI_BLOCK_BYTES`` (64 MiB) at a time (32 ROIs of 1900 pixels: 7.8 s at +122 MiB). h5py reads one
file at a time in a process, so more at once would mostly add memory; two let a short one go beside a long one. A depth
frame (one frame, about 40 ms) takes a turn of its own (``FRAMES_AT_ONCE``)."""

COMPUTATIONS_WAITING = 4
"""Computations that may wait for a turn (``core.api.turn``); the next request is a 503 problem with
``Retry-After`` that the page shows. A request leaves the queue when its client goes (the page aborts a point's
requests when another is chosen), so these are live viewers."""

FRAMES_AT_ONCE = 6
"""Depth frames read and copied at once (the frame route's limiter, ``app.state.frame_reads``).
Each read holds its 8-16 MiB frame and a copy of it as bytes: uncapped, 40 at once (the thread pool) took the API to
1.4 GiB on the measured sample and every other plain route waited behind them. Six read about as fast as more (h5py
reads one file at a time), and a browser asks for at most six at once over one host's connections. The turn ends when
the route returns its response: the body is then gzipped (Starlette's own limiter of 40 threads) and sent, so more
bodies than six can be held at once (R2, 512 MiB cache: 780 MiB resident at the peak with 40 frames in flight, 922
MiB with 80)."""

FRAMES_WAITING = 34
"""Depth frames that may wait for a turn: with ``FRAMES_AT_ONCE``, the 40 requests the uncapped route could serve at
once. A frame whose client has gone (a depth stepped past: the page aborts it) leaves the queue unread, so these are
frames still wanted; the next is a 503 problem with ``Retry-After``."""


class ReconstructionPointNotFound(ProblemError):
    def __init__(self, reconstruction_id: int, point_id: str, reason: str | None = None) -> None:
        detail = f"R{reconstruction_id} has no point {quoted(point_id)}" + (f": {reason}" if reason else "")
        super().__init__(404, "Point not found", detail)


class PointWithoutImages(ProblemError):
    """A point that is not complete, with its explanation (a 409: the run's state, not the request's fault)."""

    def __init__(self, reconstruction_id: int, row: recon.PointRow) -> None:
        super().__init__(409, "Point without images", f"R{reconstruction_id}: {explanation(row)}")


POINT_FILE_REASONS = {
    OutputPathState.UNAVAILABLE: (
        "is not available for this run: its plan maps no such path, or it leads out of the run directory"
    ),
    OutputPathState.OUTSIDE_ROOTS: "is outside the file roots",
    OutputPathState.MISSING: "is not a file this host can reach",
}
"""Why a point file is not read, by its state (``indexing.viewer.mapped_path``)."""


class PointFileUnavailable(ProblemError):
    """A point file that is not read: a remote run's without a portal path (its plan maps none, or the file leads out of
    the run directory), or one outside the roots or missing (a 404 with the reason, as the indexing viewer's detector
    image not on this host)."""

    def __init__(self, reconstruction_id: int, point_id: str, path: str, state: OutputPathState) -> None:
        reason = POINT_FILE_REASONS.get(state, state.value)
        super().__init__(
            404, "Point file not available", f"R{reconstruction_id}, point {quoted(point_id)}: {shown(path)} {reason}"
        )


class PointUnreadable(ProblemError):
    """A point's files exist but lauelab cannot read them (a 409, as the unreadable catalog)."""

    def __init__(self, reconstruction_id: int, point_id: str, error: BaseException) -> None:
        super().__init__(
            409,
            "Reconstruction unreadable",
            f"R{reconstruction_id}, point {quoted(point_id)} cannot be read: {quoted(str(error))}",
            problem_type=RECONSTRUCTION_UNREADABLE,
        )


class DepthNotFound(ProblemError):
    """A depth index past the point's last (a 404, as the indexing viewer's step the results do not have)."""

    def __init__(self, reconstruction_id: int, point_id: str, depth_index: int, n_depths: int) -> None:
        super().__init__(
            404,
            "Depth not found",
            f"R{reconstruction_id}, point {quoted(point_id)} has depth indices 0 to {n_depths - 1}: no {depth_index}",
        )


class ReferenceUnavailable(ProblemError):
    """A reference image the point does not hold (per-depth files hold no raw images): lauelab's kind of message,
    as for a product lauelab cannot make (the indexing viewer's 422 ``product-failed``)."""

    def __init__(self, kind: str, error: BaseException) -> None:
        label = recon.REFERENCE_NAMES.get(kind, kind)
        super().__init__(
            422, "Product failed", f"{label} is unavailable: {shortened(str(error), 600)}", problem_type=PRODUCT_FAILED
        )


@dataclass(frozen=True)
class PointImages:
    """A complete point of a run, whose files may be read (``point_images``)."""

    files: RunFiles
    artifact: recon.Artifact
    row: recon.PointRow
    admit: recon.Admit
    """Maps and confines each point file before anything opens it."""
    key: tuple[object, ...]
    """What its products are kept and tagged by (``science.recon.point_key``)."""

    @property
    def point_id(self) -> str:
        return self.row.point_id

    @property
    def n_depths(self) -> int:
        return self.row.n_depths or 0


def _admission(files: RunFiles, point_id: str, settings: Settings) -> recon.Admit:
    """The check every point file passes before anything opens it: mapped with the
    run's plan (read once; a local run has none, and its paths are the portal's), inside the file roots and a file
    here (``indexing.viewer.mapped_path``); the path to read is the one that passed (links resolved)."""
    run_directory = files.run_directory or ""  # a point is found only in a run directory
    mappings = run_path_map(run_directory) if run_directory else None

    def admit(path: str) -> str:
        mapped = mapped_path(path, mappings=mappings, base=run_directory, settings=settings)
        if mapped.shown.state is OutputPathState.NOT_RESPONDING:
            raise default_probe().not_responding(mapped.shown.path or path)
        if mapped.file is None:
            raise PointFileUnavailable(files.reconstruction_id, point_id, path, mapped.shown.state)
        return mapped.file

    return admit


def _read(images: PointImages, read: Callable[[], T]) -> T:
    try:
        return read()
    except recon.ViewError as error:
        raise PointUnreadable(images.files.reconstruction_id, images.point_id, error) from error


def point_images(files: RunFiles, point_id: str, *, settings: Settings, cache: recon.ProductCache) -> PointImages:
    """The run's complete point ``point_id``, its file admitted and its identity read (no pixels): 404 without a
    reconstruction, for an unknown point, or for a point file not on this host (``PointFileUnavailable``); 409 for a
    point without images (its explanation) or one lauelab cannot read."""
    located = locate(files)
    found = located.artifact
    if found is None:
        state = located.state
        if state is ReconstructionArtifactState.NOT_RESPONDING:
            raise default_probe().not_responding(files.run_directory or "")
        reason = {
            ReconstructionArtifactState.WRITING: "the run has not written its reconstruction yet",
            ReconstructionArtifactState.NOT_ON_HOST: "its run directory is not on this host",
        }.get(state, "the run has no reconstruction")
        raise ReconstructionPointNotFound(files.reconstruction_id, point_id, reason)
    row = next((row for row in _table(found, cache).rows if row.point_id == point_id), None)
    if row is None:
        raise ReconstructionPointNotFound(files.reconstruction_id, point_id)
    if row.status != "complete":
        raise PointWithoutImages(files.reconstruction_id, row)
    admit = _admission(files, point_id, settings)
    try:
        key = recon.point_key(found, point_id, cache=cache, admit=admit)
    except recon.ViewError as error:
        raise PointUnreadable(files.reconstruction_id, point_id, error) from error
    return PointImages(files, found, row, admit, key)


def images_tag(images: PointImages, product: str, *parts: object) -> str:
    """The entity tag of a point's ``product``: the point file's identity (``PointImages.key``: a rewritten point
    changes it, so a finished point's never does) and ``parts`` (its parameters), and the code."""
    return etag.entity_tag(f"reconstruction-{product}", images.key, *parts)


def check_depth(images: PointImages, depth_index: int) -> None:
    if depth_index >= images.n_depths:
        raise DepthNotFound(images.files.reconstruction_id, images.point_id, depth_index, images.n_depths)


@dataclass(frozen=True)
class DepthFrame:
    body: bytes
    dtype: str
    """``uint16`` or ``float32`` (``science.detector.frame_bytes``)."""
    height: int
    width: int
    depth_um: float


def depth_frame(images: PointImages, depth_index: int, *, cache: recon.ProductCache) -> DepthFrame:
    """One depth frame, as stored, as the API sends a frame, and its depth (µm)."""
    check_depth(images, depth_index)
    found, point_id, admit = images.artifact, images.point_id, images.admit
    summary = _read(images, lambda: recon.point_summary(found, point_id, cache=cache, admit=admit))
    frame = _read(images, lambda: recon.stored_frame(found, point_id, depth_index, cache=cache, admit=admit))
    body, dtype = frame_bytes(frame)
    height, width = (int(size) for size in frame.shape)
    return DepthFrame(body, dtype, height, width, float(summary.depth_um[depth_index]))


def _numbers(values: Iterable[float]) -> list[float | None]:
    """Values as JSON numbers: null for one that is not finite (JSON has no NaN)."""
    return [float(value) if math.isfinite(value) else None for value in values]


def trace_kept(images: PointImages, *, cache: recon.ProductCache) -> bool:
    """Whether the point's trace is kept (``trace`` computes nothing, and the route takes no turn)."""
    return recon.kept(images.key, [recon.FULL_TRACE], cache=cache)


def trace(images: PointImages, *, cache: recon.ProductCache, kept_only: bool = False) -> ReconstructionTrace:
    """The point's full-frame trace (a scan point's stored reduction; per-depth files summed one frame at a time)
    and its brightest depth, where the Depth View opens it (``argmax``). With ``kept_only``,
    ``recon.NotKept`` unless it is kept (the route then takes a turn)."""
    found, point_id, admit = images.artifact, images.point_id, images.admit
    full = _read(images, lambda: recon.full_trace(found, point_id, cache=cache, admit=admit, kept_only=kept_only))
    return ReconstructionTrace(
        reconstruction_id=images.files.reconstruction_id,
        point_id=point_id,
        depth_um=full.depth_um.tolist(),  # finite: lauelab checks them
        values=_numbers(full.values.tolist()),
        brightest_index=int(full.values.argmax()),
    )


@dataclass(frozen=True)
class ReferenceFrame:
    body: bytes
    dtype: str
    height: int
    width: int


def check_reference(images: PointImages, kind: ReconstructionReferenceKind) -> None:
    """Per-depth files hold only the reconstructed sum: a raw image of theirs is a 422 (``ReferenceUnavailable``),
    before anything is read."""
    if images.artifact.kind == recon.KIND_PER_DEPTH and kind is not ReconstructionReferenceKind.SUM_RECONSTRUCTED:
        error = ValueError(f"reference image {kind.value!r} is not available for per-depth files")  # lauelab's words
        raise ReferenceUnavailable(kind.value, error)


def reference_kept(images: PointImages, kind: ReconstructionReferenceKind, *, cache: recon.ProductCache) -> bool:
    """Whether the point's reference image ``kind`` is kept (the route takes no turn)."""
    return recon.kept(images.key, [recon.reference_product(kind.value)], cache=cache)


def reference_image(
    images: PointImages, kind: ReconstructionReferenceKind, *, cache: recon.ProductCache, kept_only: bool = False
) -> ReferenceFrame:
    """A reference image as the API sends a frame: ``uint16`` when every value fits (a raw frame), else ``float32``
    (a sum, so exact up to 2^24). A scan point's are stored; per-depth files hold only the reconstructed sum, made
    one frame at a time, and the raw ones are a 422 (``check_reference``). With ``kept_only``, ``recon.NotKept``
    unless it is kept."""
    found, point_id, admit = images.artifact, images.point_id, images.admit
    check_reference(images, kind)
    image = _read(
        images, lambda: recon.reference(found, point_id, kind.value, cache=cache, admit=admit, kept_only=kept_only)
    ).image
    body, dtype = frame_bytes(image)
    height, width = (int(size) for size in image.shape)
    return ReferenceFrame(body, dtype, height, width)


def _roi_message(roi: ReconstructionRoi, shape: tuple[int, int]) -> str:
    rows, columns = shape
    return f"A {roi.size} x {roi.size} ROI at ({roi.x:g}, {roi.y:g}) would extend outside the {columns} x {rows} image"


@dataclass(frozen=True)
class PlacedRois:
    """A request's ROIs placed on a point's image (``place_rois``)."""

    bounds: list[recon.Bounds]
    """Each square's ``(y0, y1, x0, x1)``, half-open, in the request's order."""
    depth_um: list[float]


def place_rois(
    images: PointImages, request: ReconstructionRoiTracesRequest, *, cache: recon.ProductCache
) -> PlacedRois:
    """The request's squares placed by ``science.recon.roi_bounds`` on the point's image (its shape
    from its summary: no pixel is read); one that would leave the image is a 422 naming it (``rois[i]``)."""
    found, point_id, admit = images.artifact, images.point_id, images.admit
    summary = _read(images, lambda: recon.point_summary(found, point_id, cache=cache, admit=admit))
    shape = (int(summary.shape[1]), int(summary.shape[2]))
    placed = [recon.roi_bounds(roi.size, roi.x, roi.y, shape) for roi in request.rois]
    outside = [
        ProblemItem(loc=["rois", index], message=_roi_message(roi, shape), code="roi_outside")
        for index, (roi, bounds) in enumerate(zip(request.rois, placed, strict=True))
        if bounds is None
    ]
    if outside:
        raise ProblemError(422, "Validation failed", problems_detail(len(outside)), errors=outside)
    depth_um = [float(depth) for depth in summary.depth_um]  # finite: lauelab checks them
    return PlacedRois([bounds for bounds in placed if bounds is not None], depth_um)


def rois_kept(images: PointImages, placed: PlacedRois, *, cache: recon.ProductCache) -> bool:
    """Whether every placed ROI's trace is kept (the route takes no turn)."""
    return recon.kept(images.key, [recon.roi_product(bounds) for bounds in placed.bounds], cache=cache)


def roi_traces(
    images: PointImages, placed: PlacedRois, *, normalize: bool, cache: recon.ProductCache, kept_only: bool = False
) -> ReconstructionRoiTraces:
    """One trace per placed ROI, in order, as lauelab's ``plot_roi_traces`` plots them: the sum of the ROI's stored
    pixels at each depth (those not kept summed together in one pass over the frames, ``science.recon.roi_traces``),
    or with ``normalize`` that divided by its own maximum (a trace whose maximum is not positive has no values, and
    lauelab's reason). With ``kept_only``, ``recon.NotKept`` unless every one is kept."""
    found, point_id, admit = images.artifact, images.point_id, images.admit
    # lauelab's default name, which its normalization reason gives
    regions = [("ROI [{}:{}, {}:{}]".format(*bounds), bounds) for bounds in placed.bounds]
    traces = []
    for roi in _read(
        images, lambda: recon.roi_traces(found, point_id, regions, cache=cache, admit=admit, kept_only=kept_only)
    ):
        values, reason = roi.values, None
        if normalize:
            normalized = roi.normalized()
            values, reason = normalized.values, normalized.reason or None
        traces.append(
            ReconstructionRoiTrace(
                bounds=list(roi.bounds or ()),
                values=None if values is None else _numbers(values.tolist()),
                reason=reason,
            )
        )
    return ReconstructionRoiTraces(
        reconstruction_id=images.files.reconstruction_id,
        point_id=point_id,
        normalize=normalize,
        depth_um=placed.depth_um,
        depth_index=list(range(len(placed.depth_um))),
        traces=traces,
    )
