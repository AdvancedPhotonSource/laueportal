"""Scans: the grid list, the scan detail, the catalog and its notes, the scan-log import's
endpoints (index, import, and the page's quick select), and the lookups the other domains share
(catalog entries by scan number).

The technique of every scan in a list is computed in SQL, in the list's own query (``technique_subquery``), so it sorts
and filters like any other column. The scan detail computes it from the rows it loads
(``importer.build_technique_strings``); ``tests/scans/test_scans_api.py`` keeps the two in step.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from datetime import datetime, tzinfo
from typing import Any, BinaryIO, Unpack
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, Engine, Select, Subquery, case, func, inspect, literal, select, union_all
from sqlalchemy.orm import Session

from laueportal.core.db import begin_write
from laueportal.core.etag import value_tag
from laueportal.core.paging import GridField, GridParams, GridSpec, fetch_page
from laueportal.core.problems import ProblemError, ProblemItem
from laueportal.core.settings import Settings
from laueportal.core.text import shortened
from laueportal.core.timeutil import local_to_utc
from laueportal.files.browser import read_selected_file
from laueportal.files.roots import PathNotAllowed, resolve_inside_roots
from laueportal.scans import importer
from laueportal.scans.importer import MotorGroups, build_technique_strings, find_motor_group
from laueportal.scans.models import Catalog, Metadata, Scan
from laueportal.scans.schemas import (
    ScanCatalog,
    ScanCatalogUpdate,
    ScanDetail,
    ScanDimension,
    ScanImportDefaults,
    ScanImportItem,
    ScanImportOutcome,
    ScanImportReport,
    ScanImportRequest,
    ScanImportSummary,
    ScanLogEntry,
    ScanLogIndex,
    ScanLogRow,
    ScanLogStatus,
    ScanMetadata,
    ScanPage,
    ScanPositioner,
    ScanSummary,
)

POSITIONER_SLOTS = (1, 2, 3, 4)

SCAN_GRID = GridSpec(
    fields={
        "scan_number": GridField("number"),
        "sample_name": GridField("text"),
        "aperture": GridField("text"),
        "user_name": GridField("text"),
        "n_dimensions": GridField("number"),
        "technique": GridField("text"),
        "time": GridField("date"),
        "notes": GridField("text"),
    },
    key="scan_number",
    default_sort="-scan_number",
)
"""The scans grid: every column sorts and filters; newest scan first."""


class ScanNotFound(ProblemError):
    def __init__(self, scan_number: int) -> None:
        super().__init__(404, "Scan not found", f"No scan {scan_number}")


def get_catalog_by_scan_number(session: Session, scan_number: int) -> Catalog | None:
    """Return the catalog row identified by its unique scan number."""
    return session.query(Catalog).filter(Catalog.scan_number == scan_number).one_or_none()


# ---------------------------------------------------------------------------
# Technique in SQL


def _motor_group(pv: ColumnElement[Any], motor_groups: MotorGroups) -> ColumnElement[str]:
    """``find_motor_group`` in SQL: the PV up to its first ``.VAL``, looked up in the groups in their order."""

    motor = func.substr(pv, 1, func.instr(pv + ".VAL", ".VAL") - 1)
    whens = [(motor.in_(list(pvs)), name.lower()) for name, pvs in motor_groups.items() if pvs]
    if not whens:
        return literal("other")
    return case(*whens, else_="other")


def technique_subquery(motor_groups: MotorGroups) -> Subquery:
    """``(scan_number, technique)`` for every scan with a positioner, as ``build_technique_strings`` makes it.

    The positioners with a PV, in scan-row order (``scan.id``) and slot order, each named by its motor group
    (lower case); each group once, at its first place, except ``none``; ``sample`` is ``line`` when one
    positioner moves a sample motor and ``area`` when more do; joined with `` + ``. A scan without positioners
    has no row: its technique is ``none``. The ordered ``group_concat`` needs SQLite 3.44 or later (the
    environment pins libsqlite 3.54).
    """

    slots = [
        select(
            Scan.scan_number.label("scan_number"),
            (Scan.id * len(POSITIONER_SLOTS) + slot).label("position"),
            getattr(Scan, f"scan_positioner{slot}_pv").label("pv"),
        ).where(
            getattr(Scan, f"scan_positioner{slot}_pv").is_not(None),
            getattr(Scan, f"scan_positioner{slot}_pv") != "",
        )
        for slot in POSITIONER_SLOTS
    ]
    positioners = union_all(*slots).subquery("positioners")
    named = select(
        positioners.c.scan_number,
        positioners.c.position,
        _motor_group(positioners.c.pv, motor_groups).label("motor_group"),
    ).subquery("named")
    groups = (
        select(
            named.c.scan_number,
            named.c.motor_group,
            func.min(named.c.position).label("first"),
            func.count().label("n"),
        )
        .where(named.c.motor_group != "none")
        .group_by(named.c.scan_number, named.c.motor_group)
        .subquery("groups")
    )
    label = case(
        (groups.c.motor_group == "sample", case((groups.c.n == 1, "line"), else_="area")),
        else_=groups.c.motor_group,
    )
    return (
        select(
            groups.c.scan_number,
            func.group_concat(label, " + ").aggregate_order_by(groups.c.first).label("technique"),
        )
        .group_by(groups.c.scan_number)
        .subquery("technique")
    )


# ---------------------------------------------------------------------------
# The scans grid


def scan_list_query(motor_groups: MotorGroups) -> Select[Unpack[tuple[Any, ...]]]:
    """The scans grid's select: one row per scan, its catalog's columns, its dimensions and its technique."""

    dimensions = (
        select(Scan.scan_number, func.count().label("n_dimensions")).group_by(Scan.scan_number).subquery("dimensions")
    )
    technique = technique_subquery(motor_groups)
    return (
        select(
            Metadata.scan_number.label("scan_number"),
            Catalog.sample_name.label("sample_name"),
            Catalog.aperture.label("aperture"),
            Metadata.user_name.label("user_name"),
            func.coalesce(dimensions.c.n_dimensions, 0).label("n_dimensions"),
            func.coalesce(technique.c.technique, "none").label("technique"),
            Metadata.time.label("time"),
            Catalog.notes.label("notes"),
        )
        .select_from(Metadata)
        .outerjoin(Catalog, Catalog.scan_number == Metadata.scan_number)
        .outerjoin(dimensions, dimensions.c.scan_number == Metadata.scan_number)
        .outerjoin(technique, technique.c.scan_number == Metadata.scan_number)
    )


def list_scans(session: Session, params: GridParams, *, motor_groups: MotorGroups) -> ScanPage:
    """One page of the scans grid: two queries, whatever the number of scans."""

    page = fetch_page(session, scan_list_query(motor_groups), SCAN_GRID, params)
    rows = [ScanSummary.model_validate(dict(row)) for row in page.rows]
    return ScanPage(rows=rows, total=page.total, as_of=page.as_of)


# ---------------------------------------------------------------------------
# The scan detail


def _range(text: str | None) -> tuple[str | None, str | None, str | None]:
    parts = (text or "").split()
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else (None, None, None)


def _dimension(row: Scan, motor_groups: MotorGroups) -> ScanDimension:
    positioners = []
    for slot in POSITIONER_SLOTS:
        pv = getattr(row, f"scan_positioner{slot}_pv")
        if not pv:
            continue
        start, end, step = _range(getattr(row, f"scan_positioner{slot}"))
        positioners.append(
            ScanPositioner(
                index=slot,
                pv=pv,
                motor_group=find_motor_group(pv, motor_groups=motor_groups),
                start=start,
                end=end,
                step=step,
            )
        )
    values = {attribute.key: getattr(row, attribute.key) for attribute in inspect(Scan).column_attrs}
    return ScanDimension.model_validate({**values, "positioners": positioners})


CATALOG_CHANGED = "urn:laueportal:problem:catalog-changed"
"""The problem type of a catalog save over a catalog that changed since it was loaded (a 409)."""

CATALOG_VALUES = ("filefolder", "filename_prefix", "aperture", "sample_name", "notes")
"""The catalog's stored values: what a save replaces and its version covers."""


def catalog_version(catalog: Catalog) -> str:
    """The version of the catalog's stored values (``ScanCatalog.version``): an entity tag of them as stored."""

    return value_tag(*(getattr(catalog, name) for name in CATALOG_VALUES))


def _catalog(catalog: Catalog) -> ScanCatalog:
    values = {name: getattr(catalog, name) for name in CATALOG_VALUES}
    return ScanCatalog.model_validate({**values, "version": catalog_version(catalog)})


def get_scan(session: Session, scan_number: int, *, motor_groups: MotorGroups) -> ScanDetail:
    """The scan's metadata, its scan rows (in the log's order) and its catalog; ``ScanNotFound`` (404)."""

    metadata = session.get(Metadata, scan_number)
    if metadata is None:
        raise ScanNotFound(scan_number)
    rows: Sequence[Scan] = session.scalars(select(Scan).where(Scan.scan_number == scan_number).order_by(Scan.id)).all()
    catalog = get_catalog_by_scan_number(session, scan_number)
    technique, _ = build_technique_strings(rows, motor_groups=motor_groups)
    return ScanDetail(
        scan_number=scan_number,
        technique=technique,
        metadata=ScanMetadata.model_validate(metadata, from_attributes=True),
        dimensions=[_dimension(row, motor_groups) for row in rows],
        catalog=None if catalog is None else _catalog(catalog),
    )


# ---------------------------------------------------------------------------
# The catalog and its notes


def _check_folder(folder: str, settings: Settings, loc: Sequence[str] = ("filefolder",)) -> None:
    """The data folder must resolve inside the allowed roots: otherwise ``files.roots.PathNotAllowed``'s 400
    with an ``errors`` item located at ``loc`` (the form's ``filefolder``)."""

    try:
        resolve_inside_roots(folder, settings)
    except PathNotAllowed as error:
        problem = error.problem
        item = ProblemItem(loc=list(loc), message=problem.detail or problem.title, code="path_not_allowed")
        raise ProblemError(problem.status, problem.title, problem.detail, errors=[item]) from None


def _locked_scan(session: Session, scan_number: int) -> None:
    """Take the write lock (the read and the write it guards are one step), then require the scan."""

    begin_write(session)
    if session.get(Metadata, scan_number) is None:
        raise ScanNotFound(scan_number)


def update_catalog(session: Session, scan_number: int, update: ScanCatalogUpdate, *, settings: Settings) -> ScanCatalog:
    """Create or replace the scan's catalog (the catalog form, with the data folder checked against the roots).

    The folder is checked first, outside the write lock (resolving a ``/net`` path may be slow): a folder
    outside the roots is a 400 even for an unknown scan, which is otherwise a 404. Under the lock, the stored
    catalog must still be the version the edit started from (``update.version``; none for a new entry): one that
    changed since is a 409 and stays as it is.
    """

    folder = update.filefolder.strip()
    _check_folder(folder, settings)
    _locked_scan(session, scan_number)
    catalog = get_catalog_by_scan_number(session, scan_number)
    if update.version != (None if catalog is None else catalog_version(catalog)):
        raise ProblemError(
            409,
            "Catalog changed",
            f"Scan {scan_number}'s catalog changed since it was loaded; reload it and apply the changes again.",
            problem_type=CATALOG_CHANGED,
        )
    if catalog is None:
        catalog = Catalog(scan_number=scan_number)
        session.add(catalog)
    catalog.filefolder = folder
    catalog.filename_prefix = [prefix.strip() for prefix in update.filename_prefix if prefix.strip()]
    catalog.aperture = update.aperture
    catalog.sample_name = update.sample_name
    catalog.notes = update.notes
    session.flush()
    return _catalog(catalog)


def append_note(session: Session, scan_number: int, text: str) -> ScanCatalog:
    """Append ``text`` to the catalog's notes: on a new line, or as the notes when they are empty.

    A scan without a catalog entry has nowhere to keep notes: a 409 (fill in the catalog first).
    """

    _locked_scan(session, scan_number)
    catalog = get_catalog_by_scan_number(session, scan_number)
    if catalog is None:
        raise ProblemError(
            409, "No catalog entry", f"Scan {scan_number} has no catalog entry for notes; save its catalog first."
        )
    catalog.notes = f"{catalog.notes}\n{text}" if catalog.notes else text
    session.flush()
    return _catalog(catalog)


# ---------------------------------------------------------------------------
# The scan-log import

MEBIBYTE = 1024 * 1024
ERROR_LIMIT = 500
"""The longest error text an import result gives, in characters."""


def scan_log_limit(settings: Settings) -> int:
    """The largest scan log the import reads, in bytes (``files.max_scan_log_mb``, in MiB)."""

    return settings.files.max_scan_log_mb * MEBIBYTE


def _too_large(settings: Settings) -> ProblemError:
    return ProblemError(
        413,
        "Scan log too large",
        f"The scan log is larger than the {settings.files.max_scan_log_mb} MB limit (files.max_scan_log_mb).",
    )


def check_upload_length(content_length: str | None, settings: Settings, *, overhead: int) -> None:
    """Before an upload is received: a 411 without a ``Content-Length`` (a chunked body cannot be bounded before it
    is read), and a 413 when it is longer than the size limit plus ``overhead`` (the multipart form's own bytes)."""

    if content_length is None or not content_length.strip().isdigit():
        raise ProblemError(411, "Length required", "Send the scan log with a Content-Length header.")
    if int(content_length) > scan_log_limit(settings) + overhead:
        raise _too_large(settings)


def read_upload(stream: BinaryIO, settings: Settings) -> bytes:
    """An uploaded scan log's bytes, read only up to the size limit (a 413 beyond it), before anything parses it."""

    limit = scan_log_limit(settings)
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise _too_large(settings)
    return data


def read_server_log(settings: Settings, *, root: str | None, path: str) -> bytes:
    """A scan log on the server (the import page's "Browse server logs"): ``path`` inside the allowed roots (and
    inside ``root`` when given; else a 400), a file with a scan-log extension (``files.scan_log_extensions``) and
    within the size limit (else a 422), checked before it is read and read only up to the limit."""

    return read_selected_file(
        path,
        settings,
        root=root,
        extensions=settings.files.scan_log_extensions,
        max_file_size_mb=settings.files.max_scan_log_mb,
    )


def _unreadable(detail: str) -> ProblemError:
    return ProblemError(422, "Unreadable scan log", detail)


def _row_time(text: str | None, zone: tzinfo) -> datetime | None:
    """The log's local time in UTC, as the import stores it; None when the log has none it can read or the import
    could not store (out of range: year 9999 local is past year 9999 in UTC)."""

    try:
        local = importer.convert_time_string_to_datetime(text)
        return None if local is None else local_to_utc(local, zone)
    except (ValueError, OverflowError):
        return None


def _row(entry: ScanLogEntry, zone: tzinfo) -> ScanLogRow:
    """An index row. A ``new`` scan without a start time or completed points the import can store is ``invalid``:
    the tables need them (NOT NULL), so the import would fail it. Any other value that is not
    a number where one belongs is stored as null, as the row shows it (``importer.import_metadata_row``)."""

    time = _row_time(entry.time, zone)
    storable = time is not None and entry.importable
    status = ScanLogStatus.INVALID if entry.status == ScanLogStatus.NEW and not storable else entry.status
    return ScanLogRow(
        scan_index=entry.scan_index,
        scan_number=entry.scan_number,
        status=status,
        time=time,
        user_name=entry.user_name,
        energy=entry.energy,
        energy_unit=entry.energy_unit,
        sample_xyz=entry.sample_xyz,
        n_dimensions=entry.num_dims,
    )


def index_log(xml_bytes: bytes, *, settings: Settings, engine: Engine) -> ScanLogIndex:
    """Index a scan log (the first stage) and stage it for the import: its rows with their statuses, and the token
    of the staged copy (``files.uploads_dir``; 24 h after its last use, ``importer.stage_scan_log``).

    The caller has bounded the size. A log with a DTD, one that is not well-formed XML and one with no scan are a
    422; nothing is staged then."""

    try:
        entries = importer.index_scan_log(xml_bytes, xmlns=settings.beamline.scan_log_namespace, engine=engine)
    except importer.UnsafeScanLog as error:
        raise _unreadable(str(error)) from None
    except ET.ParseError as error:
        raise _unreadable(f"The file is not well-formed XML ({error}).") from None
    if not entries:
        raise _unreadable("The file holds no scans: it is not a scan log.")
    token = importer.stage_scan_log(xml_bytes, files=settings.files)
    zone = ZoneInfo(settings.database.legacy_timezone)
    return ScanLogIndex(token=token, rows=[_row(entry, zone) for entry in entries])


def _catalog_values(defaults: ScanImportDefaults, settings: Settings) -> dict[str, Any]:
    """The catalog columns every imported scan gets; a data folder outside the roots is a 400 (empty: none yet)."""

    folder = defaults.filefolder.strip()
    if folder:
        _check_folder(folder, settings, loc=("catalog_defaults", "filefolder"))
    return {
        "filefolder": folder,
        "filename_prefix": [prefix.strip() for prefix in defaults.filename_prefix if prefix.strip()],
        "aperture": defaults.aperture,
        "sample_name": defaults.sample_name,
        "notes": defaults.notes,
    }


def import_scans(request: ScanImportRequest, *, settings: Settings, engine: Engine) -> ScanImportReport:
    """Import the selected scans of a staged log (the second stage), each committed on its own: one result per
    scan and the counts. The catalog defaults are checked first (a 400), then the token (a 404 when the staged log
    expired, was dropped to make room or is unknown), then the scan IDs (a 422 naming those the log does not hold;
    nothing is imported)."""

    defaults = _catalog_values(request.catalog_defaults, settings)
    try:
        xml_bytes = importer.load_staged_scan_log(request.token, files=settings.files)
    except FileNotFoundError:
        raise ProblemError(
            404,
            "Scan log not found",
            (
                "The staged scan log expired (24 hours after its last use), was dropped to make room for newer logs, "
                "or is unknown: index the log again."
            ),
        ) from None
    try:
        results = importer.import_scan_numbers(
            xml_bytes,
            request.scan_ids,
            defaults,
            xmlns=settings.beamline.scan_log_namespace,
            engine=engine,
            motor_groups=settings.beamline.motor_groups,
            timezone=ZoneInfo(settings.database.legacy_timezone),
        )
    except importer.UnknownScanNumbers as error:
        unknown = set(error.numbers)
        items = [
            ProblemItem(
                loc=["scan_ids", position], message=f"The scan log holds no scan {number}.", code="unknown_scan"
            )
            for position, number in enumerate(request.scan_ids)
            if number in unknown
        ]
        raise ProblemError(
            422, "Validation failed", f"{len(unknown)} of the scan IDs are not in the scan log.", errors=items
        ) from None
    items = [
        ScanImportItem(
            scan_number=result.scan_number,
            outcome=result.outcome,
            error=None if result.error is None else shortened(result.error, ERROR_LIMIT),
        )
        for result in results
        if result.scan_number is not None
    ]
    counts = {outcome: sum(item.outcome == outcome for item in items) for outcome in ScanImportOutcome}
    return ScanImportReport(
        results=items,
        summary=ScanImportSummary(
            imported=counts[ScanImportOutcome.IMPORTED],
            exists=counts[ScanImportOutcome.EXISTS],
            failed=counts[ScanImportOutcome.FAILED],
        ),
    )


def quick_select(
    rows: Iterable[ScanLogRow],
    *,
    scan_id_greater_than: int | None = None,
    created_at_or_after: datetime | None = None,
) -> list[int]:
    """The scan IDs the import page's quick select picks (its two Select buttons): the ``new`` rows,
    in the given order, whose scan ID is greater than ``scan_id_greater_than`` and whose start (``time``) is at or
    after ``created_at_or_after`` (an aware datetime), each criterion when given (a ``new`` row always has a time).
    The page applies this rule itself, on the rows it holds; ``tests/scan_logs/quick_select.json`` holds the cases
    both test."""

    if created_at_or_after is not None and created_at_or_after.utcoffset() is None:
        raise ValueError("created_at_or_after needs a time zone")
    return [
        row.scan_number
        for row in rows
        if row.status == ScanLogStatus.NEW
        and row.scan_number is not None
        and (scan_id_greater_than is None or row.scan_number > scan_id_greater_than)
        and (created_at_or_after is None or (row.time is not None and row.time >= created_at_or_after))
    ]
