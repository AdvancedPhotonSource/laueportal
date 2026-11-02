"""Files endpoints: the allowed roots and the roots-restricted folder listing behind the path picker (the scan
detail's Files Path, the create forms and the scan-log import), and the create forms' file helpers (Find Matching
Files and Find Indices)."""

from __future__ import annotations

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from laueportal.core.api import SettingsDep, read_folders
from laueportal.files import browser, finder
from laueportal.files.schemas import (
    BrowseParams,
    DirectoryListing,
    FileIndices,
    FilePatterns,
    FileRoots,
    IndicesRequest,
    PatternsRequest,
)

router = APIRouter(tags=["files"])


@router.get("/files/roots", name="listFileRoots", summary="List the allowed file roots", response_model=FileRoots)
def list_file_roots(settings: SettingsDep, response: Response) -> FileRoots:
    """The allowed roots (`files.roots`) in the settings' order: where the path picker starts. Each says whether
    its folder is on this host (one whose file system does not answer is not)."""
    response.headers["Cache-Control"] = "no-store"
    return browser.list_roots(settings)


@router.get(
    "/files/browse",
    name="browseFiles",
    summary="List a folder inside the allowed roots",
    response_model=DirectoryListing,
)
async def browse_files(
    params: Annotated[BrowseParams, Query()], settings: SettingsDep, request: Request, response: Response
) -> DirectoryListing | Response:
    """One level of a folder inside the allowed roots: folders first, then files, each in natural name order;
    hidden (dot) entries and symlinks leading outside the roots are left out; at most `files.listing_cap` entries,
    with `truncated` set when there were more. Every subfolder is considered, but only ten times
    `files.listing_cap` of a folder's files are sorted (the first the file system gives): with more, the listing is
    truncated, and its files are the first, in natural order, of those. Give `root` (its folder), `path`, or both
    (`path` inside `root`).
    400 for an unknown root or a path outside the roots, 404 (with the reason) for a folder missing on this host,
    422 for a file or for neither `root` nor `path`, 503 `urn:laueportal:problem:not-responding` when the folder's
    file system does not answer. A folder read: four are made at once (with Find Matching Files, Find Indices and
    the run forms' validation and creation), the others wait for a turn, a request whose client has gone stops
    waiting, and while 16 already wait the answer is a 503 with `Retry-After`."""
    # A listing that waits its turn (docs/contributing/backend.md, "Long work") waits outside the thread
    # pool; the listing itself runs in it. It reads no database.
    response.headers["Cache-Control"] = "no-store"
    return await read_folders(
        request,
        partial(browser.browse, settings, root=params.root, path=params.path, extensions=params.extension_list()),
    )


@router.post(
    "/files/patterns",
    name="findFilePatterns",
    summary="Suggest a folder's file-name templates",
    response_model=FilePatterns,
)
async def find_file_patterns(
    body: PatternsRequest, settings: SettingsDep, request: Request, response: Response
) -> FilePatterns | Response:
    """Up to 10 file-name templates for the folder's HDF files (Find Matching Files): each number at a name's
    end made a `%d` (the last two for `lauego`, the last one for `wire`), similar templates merged with `*`, each
    labelled with its index ranges and giving them (`scan_points`, `depth_range`) for the form's index fields. At
    most 200,000 files are read (`truncated` beyond). 400 for a folder outside the roots, 404 for one missing on this
    host, 422 for a file or a folder that cannot be read, 503 `urn:laueportal:problem:not-responding` when its file
    system does not answer. A folder read: four at once with the file browser's listings and the run forms'
    validation and creation; while 16 wait, a 503 with `Retry-After`."""
    response.headers["Cache-Control"] = "no-store"
    return await read_folders(request, partial(finder.find_patterns, settings, body.folder, body.program))


@router.post(
    "/files/indices", name="findFileIndices", summary="Find the indices of a folder's files", response_model=FileIndices
)
async def find_file_indices(
    body: IndicesRequest, settings: SettingsDep, request: Request, response: Response
) -> FileIndices | Response:
    """The scan points and depths of the folder's files, as ranges for the form's index fields (Find Indices):
    for `lauego`, of the HDF files the template matches (its first `%d` the point, its second the depth; the
    template is required); for `wire`, every HDF file's last number, whatever the template. At most
    200,000 files are read (`truncated` beyond). 400 for a folder outside the roots, 404 for one missing on this
    host, 422 for a file or a folder that cannot be read, 503 `urn:laueportal:problem:not-responding` when its file
    system does not answer. A folder read: four at once with the file browser's listings and the run forms'
    validation and creation; while 16 wait, a 503 with `Retry-After`."""
    response.headers["Cache-Control"] = "no-store"
    return await read_folders(request, partial(finder.find_indices, settings, body.folder, body.pattern, body.program))
