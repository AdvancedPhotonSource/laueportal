"""Reconstruction endpoints: the reconstructions grid, also a scan's reconstruction card; the validation and the
creation of a new wire run; and the wire viewer's run, artifact, points and a point's images: its depth frames,
trace, reference images and ROI traces."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Annotated, Any, TypeVar

from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response

from laueportal.core import etag
from laueportal.core.api import (
    CLIENT_CLOSED_REQUEST,
    EngineDep,
    SessionDep,
    SessionsDep,
    SettingsDep,
    read_folders,
    revalidated,
    turn,
)
from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.jobs import creation
from laueportal.jobs.schemas import (
    CREATE_BODY,
    IDEMPOTENCY_KEY,
    IDEMPOTENCY_KEY_LENGTH,
    KEY_CONFLICT,
    VALIDATE_BODY,
    CreatedRun,
    RunGridParams,
    ValidationReport,
)
from laueportal.reconstruction import service, validation, viewer
from laueportal.reconstruction.schemas import (
    ReconstructionArtifact,
    ReconstructionPointList,
    ReconstructionReferenceKind,
    ReconstructionRoiTraces,
    ReconstructionRoiTracesRequest,
    ReconstructionRunDetail,
    ReconstructionRunPage,
    ReconstructionTrace,
)
from laueportal.science.recon import NotKept, ProductCache

router = APIRouter(tags=["reconstruction"])

ReconstructionId = Annotated[
    int, Path(ge=0, le=SQLITE_INTEGER_MAX, description="The reconstruction run's ID (`R<id>`).")
]
PointId = Annotated[
    str,
    Path(description="The point's ID (`point_id` of the points: lauelab's; per-depth files: the summary's prefix)."),
]
DepthIndex = Annotated[int, Path(ge=0, le=SQLITE_INTEGER_MAX, description="The depth frame's zero-based index.")]

OCTET = "application/octet-stream"
FRAME_DESCRIPTION = (
    "The image: row-major, little-endian `uint16` (or `float32`), as `X-Frame-Shape` (`H,W`) and `X-Frame-Dtype` say."
)


def get_product_cache(request: Request) -> ProductCache:
    """The app's wire viewer cache (``app.state.reconstruction_products``, ``reconstruction.product_cache_mib``)."""
    return request.app.state.reconstruction_products


ProductCacheDep = Annotated[ProductCache, Depends(get_product_cache)]

T = TypeVar("T")


def point_computations(request: Request) -> CapacityLimiter:
    """The app's turns for a point's computed images (``app.state.point_computations``,
    ``viewer.COMPUTATIONS_AT_ONCE``)."""
    return request.app.state.point_computations


def frame_reads(request: Request) -> CapacityLimiter:
    """The app's turns for a point's depth frames (``app.state.frame_reads``, ``viewer.FRAMES_AT_ONCE``)."""
    return request.app.state.frame_reads


async def _computed(request: Request, work: Callable[[bool], T], *, kept: bool) -> T | Response:
    """``work(kept_only)`` in the thread pool: at once when what it reads is ``kept`` (``work(True)``, which reads it
    only if it still is), else, or when it was evicted since (``recon.NotKept``), in a turn
    (``core.api.turn``) waited for outside the pool. At most ``viewer.COMPUTATIONS_WAITING`` wait (the next is a 503
    with ``Retry-After``), and one whose client has gone is dropped unrun (499)."""
    if kept:
        try:
            return await to_thread.run_sync(work, True)
        except NotKept:
            pass  # evicted since it was found kept: computed in a turn like any other
    waiting = viewer.COMPUTATIONS_WAITING
    async with turn(request, point_computations(request), waiting=waiting, what="point computations") as ready:
        if not ready:
            return Response(status_code=CLIENT_CLOSED_REQUEST)
        return await to_thread.run_sync(work, False)


def _binary(media_type: str, description: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {"description": description, "content": {media_type: {"schema": {"type": "string", "format": "binary"}}}}
    }


@router.get(
    "/reconstruction-runs",
    name="listReconstructionRuns",
    summary="List reconstruction runs",
    response_model=ReconstructionRunPage,
    openapi_extra=service.RECONSTRUCTION_RUN_GRID.openapi_extra(),
)
def list_reconstruction_runs(
    params: Annotated[RunGridParams, Query()], session: SessionDep, response: Response
) -> ReconstructionRunPage:
    """The reconstructions grid (the grid query protocol), live (`changed_since`: the active
    runs and those changed since). Every field sorts and filters: `id`, `scan_number` (the source; blank for an
    unlinked run), `n_points`, `percent_brightest`, `depth_start`, `depth_end`, the counters `n_inputs`,
    `n_succeeded`, `n_failed`, `n_not_run`, `n_processed` (number), `method`, `author`, `notes`, `status` (text;
    it sorts in the lifecycle order: queued, running, finished, failed, cancelled) and `created_at`, `start_time`,
    `finish_time` (date). Default sort `-id`. `scan_id` lists one scan's runs."""
    response.headers["Cache-Control"] = "no-store"
    return service.list_reconstruction_runs(session, params)


@router.post(
    "/reconstruction-runs/validate",
    name="validateReconstructionRun",
    summary="Validate a wire reconstruction run's parameters",
    response_model=ValidationReport,
)
async def validate_reconstruction_run(
    form: Annotated[dict[str, Any], Body(description=VALIDATE_BODY.format(schema="wire/v1"))],
    settings: SettingsDep,
    engine: EngineDep,
    request: Request,
) -> ValidationReport | Response:
    """Check one wire run's parameters, the create form's values: the model's rules, the cross-field
    rules (depth start < end, the resolution within the span) and the context checks (every path inside the roots
    and existing, the error `not_responding` when its file system does not answer; the input a folder of files; the source scan and its catalog folder; the output folder; input
    resolution). Always a 200 with the issues (`[]` when there are none): an `error` stops the run's creation, a
    `warning` does not. Reads only: nothing is created or written. A folder read (it lists the input folder): four
    at once with the file browser's listings, Find Matching Files and Find Indices; while 16 wait, a 503 with
    `Retry-After`."""
    validated = await read_folders(
        request, partial(validation.validate_reconstruction, form, settings=settings, engine=engine)
    )
    return validated if isinstance(validated, Response) else ValidationReport(issues=validated.issues)


@router.post(
    "/reconstruction-runs",
    name="createReconstructionRun",
    summary="Create and submit a wire reconstruction run",
    status_code=201,
    response_model=CreatedRun,
    responses=KEY_CONFLICT,
)
async def create_reconstruction_run(
    form: Annotated[dict[str, Any], Body(description=CREATE_BODY.format(schema="wire/v1"))],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=IDEMPOTENCY_KEY_LENGTH, description=IDEMPOTENCY_KEY),
    ],
    settings: SettingsDep,
    engine: EngineDep,
    request: Request,
) -> CreatedRun | Response:
    """Create one wire run from the create form's values and submit it: validated as
    `validateReconstructionRun` validates them (a 422 problem with every issue when one is an error; warnings do not
    stop it), created (inputs resolved and published) and enqueued on its target (`local`: the portal's worker;
    `aps`, `crux`: DM). A 201 with the run and its job once the run exists, also when its publication or queueing
    failed (`failure` says why; the job is then Failed, or left for the remote poller when DM's reply was lost). The
    same `Idempotency-Key` with the same body answers the same 201 again (24 hours); with another body, or while the
    first request runs, a 409 (another body after the run was created: the 409, of type
    `urn:laueportal:problem:key-created`, names it and carries its answer, `created`). A 503
    `urn:laueportal:problem:not-responding` (with `Retry-After`) when the output folder's file system does not
    answer run creation's own check of it, before anything is created. A folder read (it lists the input folder):
    four at once with the file browser's listings, Find Matching Files, Find Indices and the validation; while 16
    wait, a 503 with `Retry-After` before the key is claimed (send it again)."""
    return await read_folders(
        request,
        partial(creation.submit, "wire", form, key=idempotency_key, settings=settings, engine=engine),
    )


@router.get(
    "/reconstruction-runs/{reconstruction_id}",
    name="getReconstructionRun",
    summary="Get a reconstruction run",
    response_model=ReconstructionRunDetail,
)
def get_reconstruction_run(
    reconstruction_id: ReconstructionId, session: SessionDep, settings: SettingsDep, response: Response
) -> ReconstructionRunDetail:
    """The run (its source, paths, author and notes), its job's ID and its stored wire parameters (`parameters`,
    null without them), read from the database, so a run migrated from v1 has them too;
    its paths also relative to the file root that holds them (`relative_paths`). 404 for an unknown run."""
    response.headers["Cache-Control"] = "no-store"
    return viewer.run_detail(session, reconstruction_id, settings=settings)


@router.get(
    "/reconstruction-runs/{reconstruction_id}/artifact",
    name="getReconstructionArtifact",
    summary="Get a reconstruction run's artifact",
    response_model=ReconstructionArtifact,
)
def get_reconstruction_artifact(
    reconstruction_id: ReconstructionId,
    sessions: SessionsDep,
    cache: ProductCacheDep,
    request: Request,
    response: Response,
) -> ReconstructionArtifact | Response:
    """What the wire viewer can show: the run directory's scan catalog (`reconstruction/scan.h5`, format `scan`)
    or the older per-depth files (`per_depth`), `ready` with the points complete so far of all its points (also while
    the run still writes it: poll this while `run_status` is `queued` or `running`); else `writing` (the run is
    active and has not published its catalog yet), `empty` (the run directory is there, or none is recorded, with
    nothing to read), `not_on_host` (the recorded run directory is absent or unreadable here) or `not_responding`
    (its file system did not answer; ask again after a while). 409 when the catalog or the per-depth folder cannot be
    read; 404 for an unknown run. Revalidated by its `ETag` (`Cache-Control: private, no-cache`): with
    the tag in `If-None-Match`, a 304 while the run's status and the artifact's files are unchanged."""
    located = viewer.locate(viewer.run_files(sessions, reconstruction_id))
    if (unchanged := revalidated(request, response, viewer.artifact_tag(located))) is not None:
        return unchanged
    return viewer.artifact(located, cache=cache)


@router.get(
    "/reconstruction-runs/{reconstruction_id}/points",
    name="listReconstructionPoints",
    summary="List a reconstruction run's points",
    response_model=ReconstructionPointList,
)
def list_reconstruction_points(
    reconstruction_id: ReconstructionId,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: ProductCacheDep,
    request: Request,
    response: Response,
) -> ReconstructionPointList | Response:
    """Every point of the artifact, whole (the page sorts and filters them): its status, depths, depth range
    (µm), frame shape, stored type, sample position, source and error, and for a point that is not complete the
    explanation. A remote run's sources are mapped with its plan; one that cannot
    be is `unavailable` and is never opened. No rows unless the artifact is `ready` (`state`); while the run
    writes, the points so far. 409 when the artifact cannot be read; 404 for an unknown run. Revalidated by its
    `ETag` (`Cache-Control: private, no-cache`): a 304 while the rows are unchanged."""
    located = viewer.locate(viewer.run_files(sessions, reconstruction_id))
    answer = viewer.points(located, settings=settings, cache=cache)
    if (unchanged := revalidated(request, response, viewer.points_tag(answer))) is not None:
        return unchanged
    return answer


# A point's images. Each reads the run in a session closed before it touches a file (``viewer.run_files``), finds
# the point and admits its file (``viewer.point_images``: mapped with a remote run's plan and confined to the roots
# before anything opens it), then reads through the app's ``ProductCache``, which computes each product once however
# many requests ask for it. A frame, the trace and a reference are tagged by the point file's identity
# (``viewer.images_tag``) before anything is read. They run as docs/contributing/backend.md's "Long work" says:
# `async` routes that do their reads in the thread pool and wait for a turn outside it, holding no thread and no
# database connection. A frame (one read, but 8-16 MiB and its copy) always takes a turn of the frame reads
# (``viewer.FRAMES_AT_ONCE``: the read and the copy; gzip and the send come after it); the trace, a reference and the
# ROI traces read every frame of the point, so they take a turn of the point computations unless what they read is
# kept (``_computed``).


@router.get(
    "/reconstruction-runs/{reconstruction_id}/points/{point_id}/frames/{depth_index}",
    name="getReconstructionFrame",
    summary="Get a point's depth frame",
    response_class=Response,
    response_model=None,
    responses=_binary(OCTET, FRAME_DESCRIPTION),
)
async def get_reconstruction_frame(
    reconstruction_id: ReconstructionId,
    point_id: PointId,
    depth_index: DepthIndex,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: ProductCacheDep,
    request: Request,
    response: Response,
) -> Response:
    """A complete point's depth frame (the Depth View) as stored, as the API sends frames:
    `uint16`, or `float32` for a float type or values outside 0 to 65535; headers `X-Frame-Shape: H,W`,
    `X-Frame-Dtype` and `X-Depth-Um` (its depth in µm). Revalidated by its `ETag`, which holds the point file's
    identity: a finished point's never changes, and a point rewritten while the run writes gets another. 404 for a
    depth index past the point's last, an unknown run or point, a run without a reconstruction, or a point file not
    on this host (a remote run's without a portal path, or one outside the file roots: the detail says why, and
    nothing is opened); 409 for a point without images (its explanation) or one that cannot be read. Six frames are
    read at once: the others wait for a turn, a request whose client has gone stops waiting, and while 34 already
    wait the answer is a 503 with `Retry-After`; a 503 `urn:laueportal:problem:not-responding` (with `Retry-After`)
    when the run directory's or the point file's file system does not answer."""

    def inputs() -> tuple[viewer.PointImages, str]:
        images = viewer.point_images(
            viewer.run_files(sessions, reconstruction_id), point_id, settings=settings, cache=cache
        )
        viewer.check_depth(images, depth_index)
        return images, viewer.images_tag(images, "frame", depth_index)

    images, tag = await to_thread.run_sync(inputs)
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged

    def work() -> Response:
        frame = viewer.depth_frame(images, depth_index, cache=cache)
        headers = {
            **etag.headers(tag),
            "X-Frame-Shape": f"{frame.height},{frame.width}",
            "X-Frame-Dtype": frame.dtype,
            "X-Depth-Um": repr(frame.depth_um),
        }
        return Response(frame.body, media_type=OCTET, headers=headers)

    async with turn(request, frame_reads(request), waiting=viewer.FRAMES_WAITING, what="frame reads") as ready:
        if not ready:
            return Response(status_code=CLIENT_CLOSED_REQUEST)
        return await to_thread.run_sync(work)


@router.get(
    "/reconstruction-runs/{reconstruction_id}/points/{point_id}/trace",
    name="getReconstructionTrace",
    summary="Get a point's intensity trace",
    response_model=ReconstructionTrace,
)
async def get_reconstruction_trace(
    reconstruction_id: ReconstructionId,
    point_id: PointId,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: ProductCacheDep,
    request: Request,
    response: Response,
) -> ReconstructionTrace | Response:
    """A complete point's full-frame intensity at every depth, with the depths in µm and its brightest depth (the
    Depth View opens there): a scan point's stored reduction, or per-depth files summed one frame at a time.
    Revalidated by its `ETag`, as the frames. One not computed yet waits for a turn (two at a time): 503 with
    `Retry-After` while four already wait, and a request whose client has gone stops waiting. Otherwise the frames'
    errors."""

    def inputs() -> tuple[viewer.PointImages, str]:
        images = viewer.point_images(
            viewer.run_files(sessions, reconstruction_id), point_id, settings=settings, cache=cache
        )
        return images, viewer.images_tag(images, "trace")

    images, tag = await to_thread.run_sync(inputs)
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged

    def work(kept_only: bool) -> ReconstructionTrace:
        return viewer.trace(images, cache=cache, kept_only=kept_only)

    return await _computed(request, work, kept=viewer.trace_kept(images, cache=cache))


@router.get(
    "/reconstruction-runs/{reconstruction_id}/points/{point_id}/reference",
    name="getReconstructionReference",
    summary="Get a point's reference image",
    response_class=Response,
    response_model=None,
    responses=_binary(OCTET, FRAME_DESCRIPTION),
)
async def get_reconstruction_reference(
    reconstruction_id: ReconstructionId,
    point_id: PointId,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: ProductCacheDep,
    request: Request,
    response: Response,
    kind: Annotated[
        ReconstructionReferenceKind, Query(description="The image (lauelab's reference options).")
    ] = ReconstructionReferenceKind.SUM_RECONSTRUCTED,
) -> Response:
    """A complete point's reference image for the ROI inspector, sent as the frames are: `sum_reconstructed` (the
    stored frames' sum: per-depth files are summed one frame at a time), `first_raw` or `sum_raw` (the raw detector
    frames', which a scan point stores). A sum is sent as `float32`, which holds every whole number up to 2^24
    (16,777,216) exactly: a larger pixel sum (a sum of many raw frames) is rounded to the nearest `float32`, at most
    one part in 2^24 off. Revalidated by its `ETag`, as the frames. One not computed yet waits for a turn, as the
    trace. 422 for another `kind`, and `urn:laueportal:problem:product-failed` with a message for a raw image of
    per-depth files, which hold none. Otherwise the frames' errors."""

    def inputs() -> tuple[viewer.PointImages, str]:
        images = viewer.point_images(
            viewer.run_files(sessions, reconstruction_id), point_id, settings=settings, cache=cache
        )
        viewer.check_reference(images, kind)
        return images, viewer.images_tag(images, "reference", kind.value)

    images, tag = await to_thread.run_sync(inputs)
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged

    def work(kept_only: bool) -> Response:
        image = viewer.reference_image(images, kind, cache=cache, kept_only=kept_only)
        shape = {"X-Frame-Shape": f"{image.height},{image.width}", "X-Frame-Dtype": image.dtype}
        return Response(image.body, media_type=OCTET, headers={**etag.headers(tag), **shape})

    return await _computed(request, work, kept=viewer.reference_kept(images, kind, cache=cache))


@router.post(
    "/reconstruction-runs/{reconstruction_id}/points/{point_id}/roi-traces",
    name="computeReconstructionRoiTraces",
    summary="Compute a point's ROI traces",
    response_model=ReconstructionRoiTraces,
)
async def compute_reconstruction_roi_traces(
    reconstruction_id: ReconstructionId,
    point_id: PointId,
    body: ReconstructionRoiTracesRequest,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: ProductCacheDep,
    request: Request,
) -> ReconstructionRoiTraces | Response:
    """One trace per square ROI of a complete point, in the request's order, against the depth (µm) and the depth
    index, as lauelab's `plot_roi_traces` plots them: the sum of the ROI's stored pixels at each depth, or with
    `normalize` that divided by its own maximum (a trace whose maximum is not positive has no values, and the
    reason). Each square is centred as near as it can be to `x`, `y`: never clipped or
    shifted, so one that would extend outside the image is a 422 naming it (`["rois", i]`), and nothing is read. At
    most 32 ROIs. Each ROI's trace is kept: those not computed yet are computed together, in one pass over the
    point's frames, which waits for a turn, as the trace. Reads only. Otherwise the frames' errors."""

    def inputs() -> tuple[viewer.PointImages, viewer.PlacedRois]:
        images = viewer.point_images(
            viewer.run_files(sessions, reconstruction_id), point_id, settings=settings, cache=cache
        )
        return images, viewer.place_rois(images, body, cache=cache)

    images, placed = await to_thread.run_sync(inputs)

    def work(kept_only: bool) -> ReconstructionRoiTraces:
        return viewer.roi_traces(images, placed, normalize=body.normalize, cache=cache, kept_only=kept_only)

    return await _computed(request, work, kept=viewer.rois_kept(images, placed, cache=cache))
