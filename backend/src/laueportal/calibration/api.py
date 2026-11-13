"""Calibration endpoints: the Calibration page's setup, a frame's image and peaks, a peak fitted where the user
clicked, the indexing of the peaks, the detector's fit and the geometry file saved with it. Nothing is stored and no
database is read: each request carries the page's inputs (``calibration.service``)."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Annotated, Any, TypeVar

from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter, Query, Request, Response

from laueportal.calibration import service
from laueportal.calibration.schemas import (
    CalibrationIndexing,
    CalibrationIndexRequest,
    CalibrationInputs,
    CalibrationInputsInfo,
    CalibrationPeakFit,
    CalibrationPeakFitRequest,
    CalibrationPeaks,
    CalibrationPeakSearchRequest,
    CalibrationRefineRequest,
    CalibrationResult,
    CalibrationSaved,
    CalibrationSaveRequest,
    CalibrationSetup,
)
from laueportal.core import etag
from laueportal.core.api import CLIENT_CLOSED_REQUEST, SettingsDep, read_folders, revalidated, turn
from laueportal.indexing.detector import IMAGES_WAITING

router = APIRouter(tags=["calibration"])

T = TypeVar("T")
OCTET = "application/octet-stream"


def calibrations(request: Request) -> CapacityLimiter:
    """The app's turns for calibration computations (``app.state.calibrations``)."""
    return request.app.state.calibrations


async def _computed(request: Request, work: Callable[[], T]) -> T | Response:
    """``work`` (blocking) in the thread pool, in a turn of the calibration computations waited for outside it."""
    async with turn(
        request, calibrations(request), waiting=service.CALIBRATIONS_WAITING, what="calibration computations"
    ) as ready:
        if not ready:
            return Response(status_code=CLIENT_CLOSED_REQUEST)
        return await to_thread.run_sync(work)


@router.get(
    "/calibration/setup",
    name="getCalibrationSetup",
    summary="Get the Calibration page's setup",
    response_model=CalibrationSetup,
)
async def get_calibration_setup(settings: SettingsDep, request: Request, response: Response) -> Any:
    """The shared geometry folder (`[calibration] geometry_dir`: its state and `geoN_*.xml` files, newest first),
    the newest file as the default starting geometry, and the LaueGo form's peak search and indexing defaults. The
    folder is listed through the probe, in the folder reads' turn."""
    response.headers["Cache-Control"] = "no-store"
    return await read_folders(request, partial(service.setup, settings))


@router.get(
    "/calibration/frame",
    name="getCalibrationFrame",
    summary="Get a calibration frame's image",
    response_class=Response,
    response_model=None,
    responses={
        200: {
            "description": "The frame: row-major, little-endian `uint16` (or `float32`), as `X-Frame-Shape` and "
            "`X-Frame-Dtype` say.",
            "content": {OCTET: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def get_calibration_frame(
    path: Annotated[str, Query(min_length=1, max_length=4096, description="The frame (an HDF5 file in the roots).")],
    settings: SettingsDep,
    request: Request,
    response: Response,
) -> Response:
    """A frame's image as lauelab reads it, revalidated by its `ETag` (the file's identity); headers
    `X-Frame-Shape: H,W` and `X-Frame-Dtype`. Read in a turn of the detector images (six at once, 34 waiting). Each path must be a file inside the file roots: 400 outside them, 422 (on the field) for one that is missing,
    unreadable or refused by lauelab, 503 `urn:laueportal:problem:not-responding` when its file system does not answer."""
    tag, read = await to_thread.run_sync(partial(service.frame_image, settings, path))
    if (unchanged := revalidated(request, response, tag)) is not None:
        return unchanged

    def work() -> Response:
        body, dtype, (height, width) = read()
        headers = {**etag.headers(tag), "X-Frame-Shape": f"{height},{width}", "X-Frame-Dtype": dtype}
        return Response(body, media_type=OCTET, headers=headers)

    limiter: CapacityLimiter = request.app.state.detector_images
    async with turn(request, limiter, waiting=IMAGES_WAITING, what="detector images") as ready:
        if not ready:
            return Response(status_code=CLIENT_CLOSED_REQUEST)
        return await to_thread.run_sync(work)


@router.post(
    "/calibration/inputs",
    name="inspectCalibrationInputs",
    summary="Check the calibration's inputs",
    response_model=CalibrationInputsInfo,
)
async def inspect_calibration_inputs(body: CalibrationInputs, settings: SettingsDep, request: Request) -> Any:
    """The frame's size and detector ID, the geometry's detector for it (by ID; detector 0 for a frame without one)
    and the crystal. Each path must be a file inside the file roots: 400 outside them, 422 (on the field) for one that is missing,
    unreadable or refused by lauelab, 503 `urn:laueportal:problem:not-responding` when its file system does not answer. Two calibration computations run at once; the others wait for a turn, and while four wait the answer is a 503
    with `Retry-After`."""
    return await _computed(request, partial(service.inputs_info, settings, body))


@router.post(
    "/calibration/peaks",
    name="findCalibrationPeaks",
    summary="Find a calibration frame's peaks",
    response_model=CalibrationPeaks,
)
async def find_calibration_peaks(body: CalibrationPeakSearchRequest, settings: SettingsDep, request: Request) -> Any:
    """lauelab's peak search on the frame with the LaueGo form's settings, brightest first (at most 1,000). It
    changes nothing. Each path must be a file inside the file roots: 400 outside them, 422 (on the field) for one that is missing,
    unreadable or refused by lauelab, 503 `urn:laueportal:problem:not-responding` when its file system does not answer. Two calibration computations run at once; the others wait for a turn, and while four wait the answer is a 503
    with `Retry-After`."""
    return await _computed(request, partial(service.peaks, settings, body))


@router.post(
    "/calibration/peak",
    name="fitCalibrationPeak",
    summary="Fit a peak where the user clicked",
    response_model=CalibrationPeakFit,
)
async def fit_calibration_peak(body: CalibrationPeakFitRequest, settings: SettingsDep, request: Request) -> Any:
    """A two-dimensional Gaussian fitted around the brightest pixel within 10 pixels of `(x, y)` (lauelab's
    `fit_peak`, LaueGo's "Add Gaussian to pkList"); `peak` null when no peak fits. It changes nothing. Each path must be a file inside the file roots: 400 outside them, 422 (on the field) for one that is missing,
    unreadable or refused by lauelab, 503 `urn:laueportal:problem:not-responding` when its file system does not answer. Two calibration computations run at once; the others wait for a turn, and while four wait the answer is a 503
    with `Retry-After`."""
    return await _computed(request, partial(service.fit_peak, settings, body))


@router.post(
    "/calibration/index",
    name="indexCalibrationPeaks",
    summary="Index a calibration frame's peaks",
    response_model=CalibrationIndexing,
)
async def index_calibration_peaks(body: CalibrationIndexRequest, settings: SettingsDep, request: Request) -> Any:
    """The peaks indexed with the starting detector (the geometry file's detector of the frame, or `start`): the best
    pattern's reflections with their energies and harmonics, and where its {001} reflection points. It changes
    nothing. Each path must be a file inside the file roots: 400 outside them, 422 (on the field) for one that is missing,
    unreadable or refused by lauelab, 503 `urn:laueportal:problem:not-responding` when its file system does not answer. Two calibration computations run at once; the others wait for a turn, and while four wait the answer is a 503
    with `Retry-After`."""
    return await _computed(request, partial(service.index, settings, body))


@router.post(
    "/calibration/refine",
    name="refineCalibration",
    summary="Fit the frame's detector",
    response_model=CalibrationResult,
)
async def refine_calibration(body: CalibrationRefineRequest, settings: SettingsDep, request: Request) -> Any:
    """The peaks indexed as `index` does, the measured energies and harmonics added, and the detector's rotation and
    translation fitted with lauelab's port of LaueGo's calibration (detector 0: with the sample rotation, its azimuth
    held at `azimuth_deg`). A fit that cannot run or does not converge is a 200 whose `status` says why. It changes
    nothing. Each path must be a file inside the file roots: 400 outside them, 422 (on the field) for one that is missing,
    unreadable or refused by lauelab, 503 `urn:laueportal:problem:not-responding` when its file system does not answer. Two calibration computations run at once; the others wait for a turn, and while four wait the answer is a 503
    with `Retry-After`."""
    return await _computed(request, partial(service.refine, settings, body))


@router.post(
    "/calibration/geometry-files",
    name="saveCalibration",
    summary="Save the refined detector in a geometry file",
    response_model=CalibrationSaved,
)
async def save_calibration(body: CalibrationSaveRequest, settings: SettingsDep, request: Request) -> Any:
    """Fit again as `refine` does and write the starting geometry file, with the refined detector's `<R>`, `<P>`,
    `<timeMeasured>` and `<note>` and the write times, into `folder` (inside the roots; default the geometry folder) as
    `name` (default `geoN_<local time>.xml`); the file is read back before it takes its name. 409 when that file
    exists or when the frame, geometry or crystal changed since the fit (`inputs_version`); 422 when the fit has no
    refined values, the note is not printable ASCII without <, > or &, or the folder is not set, missing or not
    writable. Each path must be inside the file roots: 400 outside them, 422 (on the field) for a file that is
    missing, unreadable or refused by lauelab, 503 `urn:laueportal:problem:not-responding` when its file system does
    not answer. Two calibration computations run at once; the others wait for a turn, and while four wait the answer
    is a 503 with `Retry-After`."""
    return await _computed(request, partial(service.save, settings, body))
