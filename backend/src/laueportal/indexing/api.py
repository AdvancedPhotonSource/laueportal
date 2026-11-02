"""Indexing endpoints: the indexations grid, also a scan's peak indexing card, and the results
viewer's run (its job and stored parameters), artifact, XML chooser, conversion and products: the summary, map,
pole figure, legends, point details, selection statistics, detector image and overlays, and the peaks and patterns
tables."""

from __future__ import annotations

from functools import partial
from typing import Annotated, Any

from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response
from sqlalchemy.orm import Session, sessionmaker

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
from laueportal.core.paging import grid_request
from laueportal.core.settings import Settings
from laueportal.indexing import conversion, detector, products, service, tables, validation, viewer
from laueportal.indexing.schemas import (
    MAX_MIN_PEAKS,
    MIN_PEAKS_DESCRIPTION,
    PATTERN0_ONLY_DESCRIPTION,
    ConversionJob,
    DetectorOverlays,
    IndexingArtifact,
    IndexingResultsSummary,
    IndexingRunDetail,
    IndexingRunPage,
    LegendColor,
    MapColor,
    OrientationSymmetry,
    Palette,
    PatternPage,
    PeakPage,
    PointDetails,
    PoleScheme,
    ResultsGridParams,
    SelectionStats,
    SelectionStatsRequest,
    XmlChoice,
)
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
from laueportal.science.cache import DatasetCache
from laueportal.science.products import MapRequest, PoleFigureRequest
from laueportal.science.viewer import DEFAULT_MIN_PEAKS, DEFAULT_PATTERN0_ONLY, DataScope, to_data_scope

router = APIRouter(tags=["indexing"])

ARROW = "application/vnd.apache.arrow.stream"
PNG = "image/png"
OCTET = "application/octet-stream"

IndexingId = Annotated[int, Path(ge=0, le=SQLITE_INTEGER_MAX, description="The indexing run's ID (`I<id>`).")]
Step = Annotated[int, Path(ge=0, le=SQLITE_INTEGER_MAX, description="The step (a frame's position).")]


def get_dataset_cache(request: Request) -> DatasetCache:
    """The app's dataset cache (``app.state.datasets``, ``indexing.dataset_cache_size`` datasets)."""
    return request.app.state.datasets


DatasetCacheDep = Annotated[DatasetCache, Depends(get_dataset_cache)]


def simulations(request: Request) -> CapacityLimiter:
    """The app's turns for the detector overlays' simulations (``app.state.simulations``,
    ``indexing.detector.SIMULATIONS_AT_ONCE``)."""
    return request.app.state.simulations


def detector_images(request: Request) -> CapacityLimiter:
    """The app's turns for the detector images' reads (``app.state.detector_images``,
    ``indexing.detector.IMAGES_AT_ONCE``)."""
    return request.app.state.detector_images


def data_scope(
    pattern0_only: Annotated[bool, Query(description=PATTERN0_ONLY_DESCRIPTION)] = DEFAULT_PATTERN0_ONLY,
    min_peaks: Annotated[int, Query(ge=0, le=MAX_MIN_PEAKS, description=MIN_PEAKS_DESCRIPTION)] = DEFAULT_MIN_PEAKS,
) -> DataScope:
    """The scope every product takes, as lauelab's ``DataScope``:
    frames left without a selected pattern stay as frame-only records."""
    return to_data_scope(pattern0_only=pattern0_only, min_peaks=min_peaks)


ScopeDep = Annotated[DataScope, Depends(data_scope)]


# The results viewer's products are `async` routes (docs/contributing/backend.md, "Long work"): each reads its run
# and resolves its files in the thread pool (`viewer.viewer_source`: a session closed first, the run directory
# looked at through `files.probe`), waits for its dataset in the event loop when another request loads it, holding no
# thread (`viewer.load_viewer_dataset`), and computes its product in the thread pool.


async def _viewer_source(sessions: sessionmaker[Session], indexing_id: int, settings: Settings) -> viewer.ViewerSource:
    return await to_thread.run_sync(partial(viewer.viewer_source, sessions, indexing_id, settings=settings))


@router.get(
    "/indexing-runs",
    name="listIndexingRuns",
    summary="List indexing runs",
    response_model=IndexingRunPage,
    openapi_extra=service.INDEXING_RUN_GRID.openapi_extra(),
)
def list_indexing_runs(
    params: Annotated[RunGridParams, Query()], session: SessionDep, response: Response
) -> IndexingRunPage:
    """The indexations grid of LaueGo runs (the grid query protocol), live (`changed_since`:
    the active runs and those changed since). Every field but `method` sorts and filters: `id`, `scan_number`
    and `reconstruction_id` (the source; both blank for an unlinked run), `n_points`, `n_depths`, `box_size`, the
    counters `n_inputs`, `n_succeeded`, `n_failed`, `n_not_run`, `n_processed` (number), `crystal_file`,
    `author`, `notes`, `status` (text; it sorts in the lifecycle order: queued, running, finished, failed,
    cancelled) and `created_at` (date). Default sort `-id`. `scan_id` lists one scan's runs."""
    response.headers["Cache-Control"] = "no-store"
    return service.list_indexing_runs(session, params)


@router.post(
    "/indexing-runs/validate",
    name="validateIndexingRun",
    summary="Validate a LaueGo indexing run's parameters",
    response_model=ValidationReport,
)
async def validate_indexing_run(
    form: Annotated[dict[str, Any], Body(description=VALIDATE_BODY.format(schema="lauego/v1"))],
    settings: SettingsDep,
    engine: EngineDep,
    request: Request,
) -> ValidationReport | Response:
    """Check one LaueGo run's parameters, the create form's values: the model's rules, the cross-field
    rules (the `%d` placeholders against Scan Indices and Depth Indices) and the context checks (every path inside
    the roots and existing, the error `not_responding` when its file system does not answer; the input a folder of files or a reconstruction's `scan.h5`; the source scan and
    reconstruction; the output folder; input resolution). Always a 200 with the issues (`[]` when there are none):
    an `error` stops the run's creation, a `warning` does not. Reads only: nothing is created or written. A folder
    read (it lists the input folder): four at once with the file browser's listings, Find Matching Files and Find
    Indices; while 16 wait, a 503 with `Retry-After`."""
    validated = await read_folders(
        request, partial(validation.validate_indexing, form, settings=settings, engine=engine)
    )
    return validated if isinstance(validated, Response) else ValidationReport(issues=validated.issues)


@router.post(
    "/indexing-runs",
    name="createIndexingRun",
    summary="Create and submit a LaueGo indexing run",
    status_code=201,
    response_model=CreatedRun,
    responses=KEY_CONFLICT,
)
async def create_indexing_run(
    form: Annotated[dict[str, Any], Body(description=CREATE_BODY.format(schema="lauego/v1"))],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=IDEMPOTENCY_KEY_LENGTH, description=IDEMPOTENCY_KEY),
    ],
    settings: SettingsDep,
    engine: EngineDep,
    request: Request,
) -> CreatedRun | Response:
    """Create one LaueGo run from the create form's values and submit it: validated as
    `validateIndexingRun` validates them (a 422 problem with every issue when one is an error; warnings do not stop
    it), created (inputs resolved and published) and enqueued on its target (`local`: the portal's worker; `aps`,
    `crux`: DM). A 201 with the run and its job once the run exists, also when its publication or queueing failed
    (`failure` says why; the job is then Failed, or left for the remote poller when DM's reply was lost). The same
    `Idempotency-Key` with the same body answers the same 201 again (24 hours); with another body, or while the first
    request runs, a 409 (another body after the run was created: the 409, of type
    `urn:laueportal:problem:key-created`, names it and carries its answer, `created`). A 503
    `urn:laueportal:problem:not-responding` (with `Retry-After`) when the output folder's file system does not answer
    run creation's own check of it, before anything is created. A folder read (it lists the input folder): four at
    once with the file browser's listings, Find Matching Files, Find Indices and the validation; while 16 wait, a 503
    with `Retry-After` before the key is claimed (send it again)."""
    return await read_folders(
        request,
        partial(creation.submit, "lauego", form, key=idempotency_key, settings=settings, engine=engine),
    )


@router.get(
    "/indexing-runs/{indexing_id}",
    name="getIndexingRun",
    summary="Get an indexing run",
    response_model=IndexingRunDetail,
)
def get_indexing_run(indexing_id: IndexingId, session: SessionDep, response: Response) -> IndexingRunDetail:
    """The run (its source, paths, author and notes), its job's ID and its stored LaueGo parameters (`parameters`,
    null without them), read from the database, so a run migrated from v1 has them too.
    Any method. 404 for an unknown run."""
    response.headers["Cache-Control"] = "no-store"
    return viewer.run_detail(session, indexing_id)


@router.get(
    "/indexing-runs/{indexing_id}/artifact",
    name="getIndexingArtifact",
    summary="Get an indexing run's results artifact",
    response_model=IndexingArtifact,
)
def get_indexing_artifact(
    indexing_id: IndexingId, sessions: SessionsDep, request: Request, response: Response
) -> IndexingArtifact | Response:
    """What the results viewer can show, in order: the recorded results file, else a valid `output.h5` in the
    run directory (`ready`); else the recorded Output XML, the one chosen with `select-xml`, or the only XML in the
    run directory (`xml_only`, readable as it is and convertible; `xml_source` says which); else `ambiguous`
    (several XML files: `xml_candidates`); else `not_on_host` (the recorded run directory, read as recorded, is
    absent or unreadable on this host), `writing` (the run is queued or running) or `empty`. A run directory whose
    file system does not answer within a few seconds (a hung mount) is `not_responding`. An `xml_only` artifact
    reports its conversion job (`conversion`): poll this while it waits or runs, then load the results once the
    state is `ready` (`results_version` says which results file: another one is read again). `live_open`: a live run
    still open, whose results grow (poll this while it is). 404 for an unknown run. The run is read in a session closed
    before any file is touched. Revalidated by its `ETag` (`Cache-Control: private, no-cache`): with the tag in
    `If-None-Match`, a 304 while the answer is unchanged."""
    answer = viewer.get_artifact(
        sessions, indexing_id, find_conversion=lambda run_id: conversion.find_conversion(run_id, sessions=sessions)
    )
    if (unchanged := revalidated(request, response, viewer.artifact_tag(answer))) is not None:
        return unchanged
    return answer


@router.post(
    "/indexing-runs/{indexing_id}/artifact/select-xml",
    name="selectIndexingXml",
    summary="Choose the Output XML of an indexing run",
    response_model=IndexingArtifact,
)
def select_indexing_xml(indexing_id: IndexingId, choice: XmlChoice, sessions: SessionsDep) -> IndexingArtifact:
    """Record which of the run directory's XML files holds the results, when the artifact is `ambiguous` or its XML
    is the one chosen before (`xml_source` `chosen`: choose again); returns the artifact it then resolves to
    (`xml_only`). 409 when a results file, the recorded XML or the only XML decides, or while the run's conversion
    is queued or running (once it has converted, `output.h5` decides); 422 for a name that is not one of the
    `xml_candidates`; 503 when the run directory does not answer; 404 for an unknown run."""
    return viewer.select_xml(
        sessions,
        indexing_id,
        choice.name,
        find_conversion=lambda run_id: conversion.find_conversion(run_id, sessions=sessions),
    )


@router.post(
    "/indexing-runs/{indexing_id}/convert",
    name="convertIndexingResults",
    summary="Convert an indexing run's Output XML to output.h5",
    status_code=202,
    response_model=ConversionJob,
)
def convert_indexing_results(indexing_id: IndexingId, sessions: SessionsDep) -> ConversionJob:
    """Queue the conversion of an `xml_only` artifact on the worker (job kind `convert_results`), ahead of the
    queued runs, with a timeout of its own (one hour); the one already queued or running is returned. Without a
    worker it waits, recorded, until the worker starts. On success it writes `output.h5` into the run directory and
    records it: the artifact becomes `ready`. Poll the artifact (`conversion`). 409 unless the artifact
    is `xml_only` with a run directory; 503 when the run directory does not answer; 404 for an unknown run."""
    viewer.check_convertible(sessions, indexing_id)
    return conversion.enqueue_conversion(indexing_id, sessions=sessions)


def _binary(media_type: str, description: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {"description": description, "content": {media_type: {"schema": {"type": "string", "format": "binary"}}}}
    }


def _tagged(media_type: str, body: bytes, tag: str) -> Response:
    return Response(body, media_type=media_type, headers=etag.headers(tag))


@router.get(
    "/indexing-runs/{indexing_id}/summary",
    name="getIndexingSummary",
    summary="Get the summary of an indexing run's results",
    response_model=IndexingResultsSummary,
)
async def get_indexing_summary(
    indexing_id: IndexingId,
    scope: ScopeDep,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: DatasetCacheDep,
    request: Request,
    response: Response,
) -> IndexingResultsSummary | Response:
    """The results viewer's summary: counts, the map axes with values, the colour modes (each with
    why it is unavailable), the steps the scope keeps and the default step, the structure and space group, and the
    geometry the results load with. 409 while the artifact is `ambiguous` or cannot be read; 404 when it has no
    results (`writing`, `empty`, `not_on_host`), or for an unknown run; 503 when the run directory does not answer
    (`not_responding`). Revalidated by its `ETag` (`Cache-Control: private, no-cache`): with the tag in
    `If-None-Match`, a 304 while the results are unchanged."""
    source = await _viewer_source(sessions, indexing_id, settings)
    if (unchanged := revalidated(request, response, products.tag(source, "summary", scope))) is not None:
        return unchanged
    loaded = await viewer.load_viewer_dataset(source, cache=cache)
    return await to_thread.run_sync(viewer.summary, loaded, scope)


def map_request(
    color: Annotated[MapColor, Query(description="The colour mode (the summary's `color_modes`).")] = (
        MapColor.CUBIC_IPF
    ),
    axes: Annotated[
        str, Query(description="Two or three of the summary's `axes`, separated by commas: `X,H` (2D), `X,Y,Z` (3D).")
    ] = "X,H",
    surface: Annotated[
        str,
        Query(
            description="The sample surface of the IPF and pole colours: `normal`, `X`, `H`, `Y`, `Z`, `F`, or nine "
            "numbers separated by commas (a custom frame's tilt, roll and normal vectors). For `pole_hsv`, the "
            "pole figure's surface."
        ),
    ] = "normal",
    palette: Annotated[Palette, Query(description="A scalar mode's palette (echoed in the metadata).")] = (
        Palette.VIRIDIS
    ),
    reverse: Annotated[bool, Query(description="Reverse a scalar mode's palette (echoed in the metadata).")] = False,
    minimum: Annotated[
        float | None,
        Query(alias="min", description="A scalar mode's range; with `max`, else the data's range (`auto_range`)."),
    ] = None,
    maximum: Annotated[float | None, Query(alias="max", description="A scalar mode's range; with `min`.")] = None,
    rodrigues_symmetry: Annotated[
        OrientationSymmetry,
        Query(description="The symmetry reduction of the `rodrigues` and `misorientation` colours."),
    ] = OrientationSymmetry.AUTO,
    rodrigues_ref: Annotated[
        str,
        Query(
            description="The `rodrigues` reference: `lab`, a step (its pattern 0), or nine numbers separated by "
            "commas (a custom G_ref: rows a*, b*, c* in 1/nm including 2π)."
        ),
    ] = "lab",
    ref_pattern: Annotated[
        str | None,
        Query(description="The `misorientation` reference pattern as `step,pattern_index` (a clicked pole)."),
    ] = None,
    pole_hkl: Annotated[str, Query(description="`pole_hsv`: the pole figure's HKL, `h,k,l`.")] = "1,0,0",
    pole_center: Annotated[str, Query(description="`pole_hsv`: the colour centre, `x,y` (a clicked pole).")] = "0,0",
    pole_radius: Annotated[float, Query(description="`pole_hsv`: the colour radius in degrees.")] = 22.5,
) -> MapRequest:
    """The colour map's parameters."""
    return MapRequest(
        axes=products.parse_axes(axes),
        color=color.value,
        surface=products.parse_surface(surface),
        palette=palette.value,
        reverse=reverse,
        limits=(products.parse_limit(minimum, "min"), products.parse_limit(maximum, "max")),
        symmetry=rodrigues_symmetry.value,
        rodrigues_reference=products.parse_rodrigues_reference(rodrigues_ref),
        reference_pattern=products.parse_pattern(ref_pattern) if ref_pattern is not None else None,
        pole_hkl=products.parse_hkl(pole_hkl, "pole_hkl"),
        pole_center=products.parse_point(pole_center, "pole_center"),
        pole_radius=products.parse_radius(pole_radius, "pole_radius"),
    )


@router.get(
    "/indexing-runs/{indexing_id}/map",
    name="getIndexingMap",
    summary="Get an indexing run's colour map",
    response_class=Response,
    response_model=None,
    responses=_binary(ARROW, "The map's records (Arrow, one record batch)."),
)
async def get_indexing_map(
    indexing_id: IndexingId,
    scope: ScopeDep,
    params: Annotated[MapRequest, Depends(map_request)],
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: DatasetCacheDep,
    request: Request,
    response: Response,
) -> Response:
    """The colour map, computed by lauelab (`prepare_map`): one record per pattern the scope
    selects, then one per frame it leaves without a pattern. Arrow columns: `x`, `y` (and `z` with three axes;
    float64); `r`, `g`, `b` (uint8; 150,150,150 without a colour) for the orientation modes, or `value` (float64;
    NaN without a value: draw it as not indexed) for the scalar ones; `frame_id` (int64, or text); `pattern_index`
    (int32; -1 for a frame-only record); `step` (int32); `indexed` (bool: a pattern with a finite orientation; the
    orientation modes draw the others in the not-indexed style). The schema metadata's `product` is JSON:
    `color`, `color_kind` (`rgb` or `scalar`), `color_label`, `axes`, `axis_labels`, `spatial_axes` (the aspect
    may be locked), `symmetry` (applied by `rodrigues` and `misorientation`, else null) and, for the scalar modes,
    `palette`, `reverse`, `range` (`[min, max]`: `min` and `max` when both are set and differ, else the data's)
    and `auto_range` (the data's finite range; null without values).
    Revalidated by its `ETag` (`Cache-Control: private, no-cache`). 422 `urn:laueportal:problem:product-failed`
    with lauelab's message when it cannot make the product for these results and settings (the page keeps its
    last good plot and shows the message as stale); 422 for a malformed parameter; 409 while the artifact is
    `ambiguous` or cannot be read; 404 when it has no results, or for an unknown run; 503 when its run directory does
    not answer."""
    source = await _viewer_source(sessions, indexing_id, settings)
    tag = products.tag(source, "map", scope, params)
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged
    loaded = await viewer.load_viewer_dataset(source, cache=cache)
    return _tagged(ARROW, await to_thread.run_sync(products.map_product, loaded, scope, params), tag)


def pole_figure_request(
    hkl: Annotated[str, Query(description="The HKL family, `h,k,l` (a cubic crystal).")] = "1,0,0",
    surface: Annotated[
        str, Query(description="`normal`, `X`, `H`, `Y`, `Z`, `F`, or nine numbers (tilt, roll, normal).")
    ] = "normal",
    scheme: Annotated[PoleScheme, Query(description="The colours: by position (HSV), cubic IPF, or uniform.")] = (
        PoleScheme.HSV
    ),
    radius: Annotated[float, Query(description="`hsv`: the colour radius in degrees.")] = 22.5,
    center: Annotated[str, Query(description="`hsv`: the colour centre, `x,y` (a clicked pole).")] = "0,0",
) -> PoleFigureRequest:
    """The pole figure's parameters."""
    return PoleFigureRequest(
        hkl=products.parse_hkl(hkl, "hkl"),
        surface=products.parse_surface(surface),
        scheme=scheme.value,
        radius=products.parse_radius(radius, "radius"),
        center=products.parse_point(center, "center"),
    )


@router.get(
    "/indexing-runs/{indexing_id}/pole-figure",
    name="getIndexingPoleFigure",
    summary="Get an indexing run's pole figure",
    response_class=Response,
    response_model=None,
    responses=_binary(ARROW, "The poles (Arrow, one record batch)."),
)
async def get_indexing_pole_figure(
    indexing_id: IndexingId,
    scope: ScopeDep,
    params: Annotated[PoleFigureRequest, Depends(pole_figure_request)],
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: DatasetCacheDep,
    request: Request,
    response: Response,
) -> Response:
    """The pole figure, computed by lauelab (`prepare_pole_figure`): one record per pole of
    the HKL family of each pattern the scope selects; a cubic crystal. Arrow columns: `x`, `y` (float64,
    stereographic), `r`, `g`, `b` (uint8), `frame_id` (int64, or text), `pattern_index` (int32), `step` (int32).
    The schema metadata's `product` is JSON: `hkl`, `scheme`, `color_kind` (`rgb` or `uniform`), `color_radius`
    (the HSV scheme's, else null) and `center`.
    Revalidated by its `ETag` (`Cache-Control: private, no-cache`). 422 `urn:laueportal:problem:product-failed`
    with lauelab's message when it cannot make the product for these results and settings (the page keeps its
    last good plot and shows the message as stale); 422 for a malformed parameter; 409 while the artifact is
    `ambiguous` or cannot be read; 404 when it has no results, or for an unknown run; 503 when its run directory does
    not answer."""
    source = await _viewer_source(sessions, indexing_id, settings)
    tag = products.tag(source, "pole-figure", scope, params)
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged
    loaded = await viewer.load_viewer_dataset(source, cache=cache)
    return _tagged(ARROW, await to_thread.run_sync(products.pole_figure_product, loaded, scope, params), tag)


@router.get(
    "/indexing-runs/{indexing_id}/legend",
    name="getIndexingLegend",
    summary="Get a colour mode's key image",
    response_class=Response,
    response_model=None,
    responses=_binary(PNG, "The key (PNG, 256 × 256 RGBA)."),
)
def get_indexing_legend(
    indexing_id: IndexingId,
    color: Annotated[LegendColor, Query(description="The colour mode: the IPF triangle or the HSV wheel.")],
    session: SessionDep,
    request: Request,
    response: Response,
) -> Response:
    """The colour key of `cubic_ipf` (the cubic IPF triangle, 001 red, 101 green, 111 blue) or `pole_hsv` (the
    HSV wheel; also the pole figure's `hsv` scheme), made by lauelab (the page labels the
    corners and axes). Revalidated by its `ETag`. 404 for an unknown run."""
    viewer.load_run(session, indexing_id)
    tag = etag.entity_tag("legend", color.value)
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged
    return _tagged(PNG, products.legend_product(color.value), tag)


@router.get(
    "/indexing-runs/{indexing_id}/points/{step}",
    name="getIndexingPoint",
    summary="Get a step's details",
    response_model=PointDetails,
)
async def get_indexing_point(
    indexing_id: IndexingId,
    step: Step,
    scope: ScopeDep,
    axes: Annotated[
        str, Query(description="Two or three of the summary's `axes` for `coordinates`, separated by commas.")
    ] = "X,H",
    *,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: DatasetCacheDep,
    request: Request,
    response: Response,
) -> PointDetails | Response:
    """A step's details for the map's hover and click cards: frame, motor position,
    depth, detected peaks, each pattern (indexed peaks, goodness, RMS error, whether the scope keeps it), and the
    step's coordinates on `axes` (null on an axis that needs a depth the step does not have; another step's missing
    depth does not matter). Revalidated by its `ETag`. 404 for a step the results do not have; otherwise the map's
    errors."""
    point_axes = products.parse_axes(axes)
    source = await _viewer_source(sessions, indexing_id, settings)
    if (
        unchanged := revalidated(request, response, products.tag(source, "point", scope, step, point_axes))
    ) is not None:
        return unchanged
    loaded = await viewer.load_viewer_dataset(source, cache=cache)
    return await to_thread.run_sync(products.point_product, loaded, scope, step, point_axes)


@router.post(
    "/indexing-runs/{indexing_id}/selection-stats",
    name="computeIndexingSelectionStats",
    summary="Compute a selection's pairwise misorientation",
    response_model=SelectionStats,
)
async def compute_indexing_selection_stats(
    indexing_id: IndexingId,
    selection: SelectionStatsRequest,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: DatasetCacheDep,
) -> SelectionStats:
    """The pairwise misorientation of the selected patterns' orientations (mean, min and max in degrees), reduced by the
    crystal's proper rotations when it is cubic or hexagonal, by lauelab. At most 700 distinct patterns with an
    orientation (a pair named twice counts once; 422 above), in at most 33,600 pairs; 422 for a pair that is not a
    pattern of the results."""
    source = await _viewer_source(sessions, indexing_id, settings)
    loaded = await viewer.load_viewer_dataset(source, cache=cache)
    return await to_thread.run_sync(products.selection_product, loaded, selection.patterns)


@router.get(
    "/indexing-runs/{indexing_id}/detector/{step}/image",
    name="getIndexingDetectorImage",
    summary="Get a step's detector image",
    response_class=Response,
    response_model=None,
    responses=_binary(
        OCTET,
        "The frame: row-major, little-endian `uint16` (or `float32`), as `X-Frame-Shape` and `X-Frame-Dtype` say.",
    ),
)
async def get_indexing_detector_image(
    indexing_id: IndexingId,
    step: Step,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: DatasetCacheDep,
    request: Request,
    response: Response,
) -> Response:
    """A step's detector frame: the image its `inputImage` names, found by these
    rules (the path when absolute, else beside the output, in the run's data folder and under the runs root), each
    path mapped for a remote run and confined to the file roots before it is opened; the configured HDF5 datasets,
    then the first 2-D one; a reconstructed frame from its point file. The body is the frame, row-major and
    little-endian: `uint16` when every value is an integer from 0 to 65535, else `float32` (a float source, or a
    reconstructed frame's signed values); headers `X-Frame-Shape: H,W` and `X-Frame-Dtype`. It takes no scope (it
    selects no records). Revalidated by its `ETag`, which holds the image file's identity. 404 "Detector image not
    found" when the image is not on this host (the detail says where it was looked for and why each path was not
    opened), or for a step the results do not have; 422 `urn:laueportal:problem:product-failed` when the file is not
    a readable 2-D image; otherwise the map's errors. Six images are read at once: the others wait for a turn, a
    request whose client has gone stops waiting, and while 34 already wait the answer is a 503 with `Retry-After`; a
    503 `urn:laueportal:problem:not-responding` (with `Retry-After`) when an image path's file system does not
    answer."""
    # As the wire viewer's depth frames (docs/contributing/backend.md, "Long work"): the run is read in a session closed
    # before the wait (viewer.viewer_source), the dataset waited for outside the thread pool, and the frame is read and
    # copied in a turn of detector.IMAGES_AT_ONCE, waited for outside the thread pool; gzip and the send come after the
    # turn.
    source = await _viewer_source(sessions, indexing_id, settings)
    loaded = await viewer.load_viewer_dataset(source, cache=cache)

    def inputs() -> tuple[detector.StepImage, str]:
        detector.check_step(loaded, step)
        image = detector.step_image(source, loaded, step, settings=settings)
        return image, detector.image_tag(source, "detector-image", image, settings, step)

    image, tag = await to_thread.run_sync(inputs)
    if image.path is not None and (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged

    def work() -> Response:
        frame = detector.image_product(loaded, image, settings=settings)
        headers = {**etag.headers(tag), "X-Frame-Shape": f"{frame.height},{frame.width}", "X-Frame-Dtype": frame.dtype}
        return Response(frame.body, media_type=OCTET, headers=headers)

    limiter = detector_images(request)
    async with turn(request, limiter, waiting=detector.IMAGES_WAITING, what="detector images") as ready:
        if not ready:
            return Response(status_code=CLIENT_CLOSED_REQUEST)
        return await to_thread.run_sync(work)


@router.get(
    "/indexing-runs/{indexing_id}/detector/{step}/overlays",
    name="getIndexingDetectorOverlays",
    summary="Get a step's detector overlays",
    response_model=DetectorOverlays,
)
async def get_indexing_detector_overlays(
    indexing_id: IndexingId,
    step: Step,
    scope: ScopeDep,
    simulate: Annotated[
        bool, Query(description="Also simulate the reflections each pattern predicts that it did not index.")
    ] = False,
    kev: Annotated[
        str | None,
        Query(
            description="The simulation's energy window in keV, `low,high`, within the beamline's (6 to 30 by default)."
        ),
    ] = None,
    *,
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: DatasetCacheDep,
    request: Request,
    response: Response,
) -> DetectorOverlays | Response:
    """A step's detector overlays, prepared by lauelab: the detected peaks
    (pixels of the frame, x across and y down), every pattern of the step with its statistics, whether the scope keeps
    it, and its indexed peaks (hkl and predicted positions), and with `simulate` the reflections a simulation of each
    pattern predicts on the frame in `kev` that it did not index. A simulation that fails leaves the other layers
    complete and says why in `simulation_error`. The summary is a card's values (step, frame, motor position, detector,
    detected and indexed peaks) with where the image is (`image`: the file and dataset, or why it is not
    available). Revalidated by its `ETag`, which holds the image file's identity. 422
    `urn:laueportal:problem:product-failed` when lauelab cannot prepare the view (no geometry, a detector the
    geometry lacks); 422 for a malformed `kev` or one beyond the beamline's window (the simulation's cost grows
    steeply with it); 404 for a step the results do not have; 503 with `Retry-After` while four simulations already
    wait for their turn (one runs at a time; a request whose client has gone stops waiting), and a 503
    `urn:laueportal:problem:not-responding` when an image path's file system does not answer; otherwise the map's
    errors."""
    # An `async` route (docs/contributing/backend.md, "Long work"): simulations run one at a time
    # (detector.SIMULATIONS_AT_ONCE) and a request waits for its turn here, outside the thread pool, so a waiter holds
    # no thread, and with the run read in a session closed before (viewer.viewer_source) no database connection;
    # at most detector.SIMULATIONS_WAITING wait (then a 503), and one whose client goes is dropped unrun (core.api.turn).
    # The blocking work runs in the thread pool. A step's simulation is kept with its dataset (detector.prepared_view):
    # a request for a kept one takes no turn.
    energy_range = products.parse_energy_range(kev, settings.beamline.simulation_kev)
    window = energy_range if simulate else None

    source = await _viewer_source(sessions, indexing_id, settings)
    loaded = await viewer.load_viewer_dataset(source, cache=cache)

    def inputs() -> tuple[detector.StepImage, str]:
        detector.check_step(loaded, step)
        image = detector.step_image(source, loaded, step, settings=settings)
        return image, detector.image_tag(source, "detector-overlays", image, settings, step, scope, window)

    image, tag = await to_thread.run_sync(inputs)
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged

    def overlays() -> DetectorOverlays:
        prepared = detector.prepared_view(loaded, step, energy_range_kev=window, cache=cache)
        return detector.overlays_product(loaded, scope, image, prepared, settings=settings)

    if window is None or detector.simulation_kept(loaded, step, window, cache=cache):
        return await to_thread.run_sync(overlays)
    limiter = simulations(request)
    async with turn(request, limiter, waiting=detector.SIMULATIONS_WAITING, what="detector simulations") as ready:
        if not ready:
            return Response(status_code=CLIENT_CLOSED_REQUEST)
        return await to_thread.run_sync(overlays)


def _scope(params: ResultsGridParams) -> DataScope:
    return to_data_scope(pattern0_only=params.pattern0_only, min_peaks=params.min_peaks)


@router.get(
    "/indexing-runs/{indexing_id}/peaks",
    name="listIndexingPeaks",
    summary="List an indexing run's indexed peaks",
    response_model=PeakPage,
    openapi_extra=tables.PEAK_GRID.openapi_extra(),
)
async def list_indexing_peaks(
    indexing_id: IndexingId,
    params: Annotated[ResultsGridParams, Query()],
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: DatasetCacheDep,
    request: Request,
    response: Response,
) -> PeakPage | Response:
    """The indexed peaks within the scope (lauelab's `indexed_peak_table`): one row
    per pattern's assignment, with its detected peak and its pattern, on the grid query protocol (the
    scope's `pattern0_only` and `min_peaks` are among the grid's parameters). Every field sorts and filters
    (`frame_id` and `input_image` as text, `frame_id` sorting by step; the others as numbers); default order `id`
    (the results'). Revalidated by its `ETag` (`Cache-Control: private, no-cache`), not `no-store` like the live
    lists: the rows change only with the results. Otherwise the map's errors."""
    grid_request(tables.PEAK_GRID, params)  # a 422 before anything loads
    scope = _scope(params)
    source = await _viewer_source(sessions, indexing_id, settings)
    tag = products.tag(source, "peaks", scope, tables.page_parameters(params))
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged
    loaded = await viewer.load_viewer_dataset(source, cache=cache)
    return await to_thread.run_sync(partial(tables.peaks_page, loaded, scope, params, cache=cache))


@router.get(
    "/indexing-runs/{indexing_id}/patterns",
    name="listIndexingPatterns",
    summary="List an indexing run's patterns",
    response_model=PatternPage,
    openapi_extra=tables.PATTERN_GRID.openapi_extra(),
)
async def list_indexing_patterns(
    indexing_id: IndexingId,
    params: Annotated[ResultsGridParams, Query()],
    sessions: SessionsDep,
    settings: SettingsDep,
    cache: DatasetCacheDep,
    request: Request,
    response: Response,
) -> PatternPage | Response:
    """The patterns within the scope (lauelab's `pattern_table`), on the grid
    query protocol as `…/peaks`: every field sorts and filters (`frame_id`, `astar`, `bstar`, `cstar`, `structure`
    and `input_image` as text; the others as numbers); default order `id` (the results'). Revalidated by its `ETag`.
    Otherwise the map's errors."""
    grid_request(tables.PATTERN_GRID, params)
    scope = _scope(params)
    source = await _viewer_source(sessions, indexing_id, settings)
    tag = products.tag(source, "patterns", scope, tables.page_parameters(params))
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged
    loaded = await viewer.load_viewer_dataset(source, cache=cache)
    return await to_thread.run_sync(partial(tables.patterns_page, loaded, scope, params, cache=cache))
