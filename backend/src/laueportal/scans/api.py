"""Scans endpoints: the scans grid, a scan's detail, its catalog and its notes; and the scan-log
import: index a log (an upload or a file on the server), then import its selected scans."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Path, Query, Request, Response, UploadFile
from fastapi.routing import APIRoute

from laueportal.core.api import EngineDep, SessionDep, SettingsDep, get_app_settings, refuse_cross_site_writes
from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.core.paging import GridParams
from laueportal.core.problems import ProblemError
from laueportal.scans import service
from laueportal.scans.schemas import (
    ScanCatalog,
    ScanCatalogUpdate,
    ScanDetail,
    ScanImportReport,
    ScanImportRequest,
    ScanLogIndex,
    ScanNote,
    ScanPage,
)

router = APIRouter(tags=["scans"])

ScanId = Annotated[int, Path(ge=0, le=SQLITE_INTEGER_MAX, description="The scan ID.")]


@router.get(
    "/scans",
    name="listScans",
    summary="List scans",
    response_model=ScanPage,
    openapi_extra=service.SCAN_GRID.openapi_extra(),
)
def list_scans(
    params: Annotated[GridParams, Query()], session: SessionDep, settings: SettingsDep, response: Response
) -> ScanPage:
    """The scans grid (the grid query protocol). Every field sorts and filters: `scan_number`,
    `n_dimensions` (number), `sample_name`, `aperture`, `user_name`, `technique`, `notes` (text) and `time` (date).
    Default sort `-scan_number`."""
    response.headers["Cache-Control"] = "no-store"
    return service.list_scans(session, params, motor_groups=settings.beamline.motor_groups)


@router.get("/scans/{scan_id}", name="getScan", summary="Get a scan", response_model=ScanDetail)
def get_scan(scan_id: ScanId, session: SessionDep, settings: SettingsDep, response: Response) -> ScanDetail:
    """The scan's metadata, its scan rows and its catalog (null until filled in). 404 for an unknown scan."""
    response.headers["Cache-Control"] = "no-store"
    return service.get_scan(session, scan_id, motor_groups=settings.beamline.motor_groups)


@router.put(
    "/scans/{scan_id}/catalog",
    name="updateScanCatalog",
    summary="Create or update a scan's catalog",
    response_model=ScanCatalog,
)
def update_scan_catalog(
    scan_id: ScanId, catalog: ScanCatalogUpdate, session: SessionDep, settings: SettingsDep
) -> ScanCatalog:
    """Create the catalog entry or replace it whole. The body's `version` is the stored catalog's `version` the edit
    started from (null for a scan without one): 409 (`urn:laueportal:problem:catalog-changed`) when the catalog
    changed since, a save or a note appended in between (reload it, then apply the changes again). 400 when the
    data folder is outside the allowed roots (`errors[].loc` `["filefolder"]`); 422 when a field is invalid; 404
    for an unknown scan; 503 `urn:laueportal:problem:not-responding` (with `Retry-After`) when the data folder's
    file system does not answer."""
    return service.update_catalog(session, scan_id, catalog, settings=settings)


@router.post(
    "/scans/{scan_id}/notes",
    name="appendScanNote",
    summary="Append a note to a scan's catalog",
    response_model=ScanCatalog,
)
def append_scan_note(scan_id: ScanId, note: ScanNote, session: SessionDep) -> ScanCatalog:
    """Append the text to the catalog's notes, on a new line. 409 when the scan has no catalog entry yet; 404 for
    an unknown scan."""
    return service.append_note(session, scan_id, note.text)


# ---------------------------------------------------------------------------
# The scan-log import

FORM_OVERHEAD = 64 * 1024
"""Bytes an upload's multipart form may hold beyond the scan log itself (boundaries, part headers, the other
fields)."""


class ScanLogUploadRoute(APIRoute):
    """The index route's class: it checks a request before FastAPI receives the body. FastAPI reads a whole form
    (spooling a file to disk) before any dependency runs, the request guards included, so this runs the write guard
    first (a cross-site post is refused before its body is read) and then refuses a body longer than the size limit
    by its ``Content-Length`` (a 413; a 411 without one)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            await refuse_cross_site_writes(request)
            service.check_upload_length(
                request.headers.get("content-length"), get_app_settings(request), overhead=FORM_OVERHEAD
            )
            return await handler(request)

        return bounded


def _one_source(message: str) -> ProblemError:
    return ProblemError(
        422, "Validation failed", message, errors=[{"loc": ["file"], "message": message, "code": "one_source"}]
    )


def index_scan_log(
    settings: SettingsDep,
    engine: EngineDep,
    response: Response,
    file: Annotated[UploadFile | None, File(description="The scan log, uploaded from the user's computer.")] = None,
    root: Annotated[
        str | None, Form(description="The allowed root the server's log is in (`GET /api/v1/files/roots`).")
    ] = None,
    path: Annotated[
        str | None, Form(description="The server's log: an absolute path inside the allowed roots (and `root`).")
    ] = None,
) -> ScanLogIndex:
    """Index a scan log and stage it for the import: every scan of the log, in log order,
    with its status (`new`, `exists`, `duplicate` or `invalid`) and the token of the staged copy
    (`POST /api/v1/scans/import`; it expires 24 hours after its last use).

    The log is either uploaded (`file`) or a file on the server (`path`, with `root` optionally; the path picker's
    browse source): one of the two. Its size is checked against `files.max_scan_log_mb` before it is read: an
    upload over it is a 413 (a 411 without a Content-Length), a server file over it a 422. The XML namespace is
    `beamline.scan_log_namespace`. 400 for a path outside the roots (or outside `root`), 404 for a missing file, 422
    for no source or both, a file of another type or extension (`files.scan_log_extensions`), a log with a DTD
    (`<!DOCTYPE`: refused, so no entity is expanded or fetched), one that is not well-formed XML, or one with no
    scan; 503 `urn:laueportal:problem:not-responding` when the server file's file system does not answer."""
    response.headers["Cache-Control"] = "no-store"
    if file is not None:
        if root is not None or path is not None:
            raise _one_source("Give an uploaded file or a server path, not both.")
        xml_bytes = service.read_upload(file.file, settings)
    elif path is not None:
        xml_bytes = service.read_server_log(settings, root=root, path=path)
    else:
        raise _one_source("Give the scan log: an uploaded file, or a path on the server.")
    return service.index_log(xml_bytes, settings=settings, engine=engine)


router.add_api_route(
    "/scans/import/index",
    index_scan_log,
    methods=["POST"],
    name="indexScanLog",
    summary="Index a scan log for import",
    response_model=ScanLogIndex,
    route_class_override=ScanLogUploadRoute,
)


@router.post(
    "/scans/import", name="importScans", summary="Import scans of a staged scan log", response_model=ScanImportReport
)
def import_scans(request: ScanImportRequest, settings: SettingsDep, engine: EngineDep) -> ScanImportReport:
    """Import the selected scans of an indexed log (its `token`), each committed on its own, with the catalog
    defaults: one result per scan in log order (`imported`, `exists` when the scan is already in the database, or
    `failed` with the error) and the counts. A scan ID names its first scan in the log. 400 for a data folder
    outside the roots (`errors[].loc` `["catalog_defaults", "filefolder"]`), 404 when the staged log expired, was
    dropped to make room or is unknown, 422 for a scan ID the log does not hold (nothing is imported), 503
    `urn:laueportal:problem:not-responding` (with `Retry-After`) when the data folder's file system does not answer
    (nothing is imported)."""
    return service.import_scans(request, settings=settings, engine=engine)
