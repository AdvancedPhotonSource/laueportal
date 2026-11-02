"""Scan-log import: index an uploaded XML scan log, stage it, and import the selected scans.

Beamline specifics come from settings: the XML namespace is ``settings.beamline.scan_log_namespace`` and the motor
groups are ``settings.beamline.motor_groups``; callers pass them (``xmlns=``, ``motor_groups=``), nothing here defaults
to constants of its own.

Uploaded logs are staged under ``settings.files.uploads_dir`` (``<runs_root>/.uploads`` when unset),
in ``scan-logs/``, never in ``/tmp``. Staged logs expire 24 hours after their last use. Cleanup
needs no background job: every new upload removes expired logs (and temporary files a crashed upload
left behind), and loading an expired log deletes it. At most ``STAGED_SCAN_LOGS_LIMIT`` bytes are kept: a new upload
drops the logs used least recently until it fits.

Logs are parsed by ``parse_scan_log``, which refuses a document with a DTD (``<!DOCTYPE``) before ElementTree
reads it: a scan log has none, so no entity is ever declared, expanded or fetched. The caller bounds
the size first (``files.max_scan_log_mb``, ``scans.service``).

Results are typed (``scans.schemas``): the index lists ``ScanLogEntry`` records with a status, and an
import reports one ``ScanImportResult`` per selected scan, both keyed by the scan's position in the
log. The parsed values are keyed by their path in the log (``scanNumber``, ``source_ringCurrent``,
``knife-edge_XYZ``, ``scan_positioner1_PV``: the log's own names); ``import_metadata_row`` and
``import_scan_row`` map them to the snake_case columns.

The log's ``time`` is the beamline's local time without a zone: ``import_metadata_row`` reads it in the
zone the caller passes (``ZoneInfo(settings.database.legacy_timezone)``) and stores UTC, with the rule of
``core.timeutil.local_to_utc`` (the rule revision 0005 applied to v1's rows). ``scan_end_time`` stays the
log's text.
"""

import logging
import os
import re
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any
from xml.parsers import expat

from sqlalchemy import Engine
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from laueportal.core.db import SQLITE_INTEGER_MAX, begin_write
from laueportal.core.settings import FilesSettings
from laueportal.core.timeutil import local_to_utc
from laueportal.scans.models import Catalog, Metadata, Scan
from laueportal.scans.schemas import (
    ScanImportOutcome,
    ScanImportResult,
    ScanLogEntry,
    ScanLogStatus,
)

logger = logging.getLogger(__name__)

SCAN_LOG_CACHE_TTL_SECONDS = 24 * 60 * 60
SCAN_LOG_STAGING_SUBDIRECTORY = "scan-logs"
STAGED_SCAN_LOGS_LIMIT = 256 * 1024 * 1024
"""The most bytes of staged logs kept at once: staging a new log first drops the logs used
least recently until the new one fits (an import of a dropped log asks for the log again)."""

EXISTING_BATCH = 500
"""Scan numbers per database query when the index looks for existing scans (SQLite bounds a query's variables)."""

MotorGroups = Mapping[str, Sequence[str]]
"""Motor group name -> motor PVs (``settings.beamline.motor_groups``)."""


class UnsafeScanLog(ValueError):
    """A scan log with a document type declaration (``<!DOCTYPE``), refused before it is parsed."""


class UnknownScanNumbers(ValueError):
    """Scan numbers asked for that the staged log does not hold (nothing is imported)."""

    def __init__(self, numbers: Sequence[int]) -> None:
        self.numbers = list(numbers)
        super().__init__(f"The scan log holds no scan {', '.join(str(number) for number in self.numbers[:10])}")


class _RootReached(Exception):
    """The root element started: the prolog, where a DTD would be, had none."""


def _refuse_dtd(*_: object) -> None:
    raise UnsafeScanLog(
        "The scan log declares a document type (<!DOCTYPE ...>). A scan log has none, and its entities would be "
        "expanded, so it is refused."
    )


def _stop_at_root(*_: object) -> None:
    raise _RootReached


def parse_scan_log(xml_bytes: bytes) -> ET.Element:
    """The root element of a scan log, parsed safely.

    A DTD can only come before the root element, so a first expat pass reads the prolog alone (it stops at the
    root's start tag) and refuses a ``<!DOCTYPE`` (``UnsafeScanLog``): without one, no entity can be declared, so
    none is expanded (a billion-laughs document) or fetched (an external entity). Only then does ElementTree parse
    the document; malformed XML raises ``ET.ParseError``.
    """
    prolog = expat.ParserCreate()
    prolog.StartDoctypeDeclHandler = _refuse_dtd
    prolog.EntityDeclHandler = _refuse_dtd
    prolog.StartElementHandler = _stop_at_root
    try:
        prolog.Parse(xml_bytes, True)
    except _RootReached:
        pass
    except expat.ExpatError:
        pass  # malformed: ElementTree reports it
    return ET.fromstring(xml_bytes)


def parse_metadata(xml, *, xmlns: str, scan_no=2, empty="\n\t\t"):
    """Parse one scan by its child index in an XML scan log."""
    root = parse_scan_log(xml)
    return _parse_scan_element(root[scan_no], xmlns=xmlns, empty=empty)


def _parse_scan_element(scan, *, xmlns: str, empty="\n\t\t"):
    """Convert an already-parsed ``fullScan`` element into import dictionaries."""

    def name(s, xmlns=xmlns):
        return s.replace(f"{{{xmlns}}}", "")

    def traverse_tree(fields, tree_dict=None, parent_name=""):
        if tree_dict is None:
            tree_dict = {}
        if not len(fields):
            pass
        else:
            for field in list(fields):
                field_name = name(field.tag)
                if not any([field_name == f for f in ["scan", "cpt"]]):
                    path_name = f"{parent_name}{field_name}"
                    field_dict = dict([(f"{path_name}_{k}", v) for k, v in field.attrib.items()])
                    if field.text and empty not in field.text:
                        field_dict[path_name] = field.text
                    tree_dict.update(field_dict)
                    traverse_tree(field, tree_dict, path_name + "_")
        return tree_dict

    # Define numeric fields that should be None instead of empty string
    numeric_fields = {
        "time_epoch",
        "source_energy",
        "source_IDgap",
        "source_IDtaper",
        "source_ringCurrent",
        "knife-edge_knifeScan",
        "scanEnd_time_epoch",
        "scanEnd_scanDuration",
        "scanEnd_source_ringCurrent",
    }

    scanNumber = scan.get("scanNumber")
    log_dict = {
        "scanNumber": scanNumber,
        "time_epoch": None,
        "time": "",
        "user_name": "",
        "source_beamBad": "",
        "source_CCDshutter": "",
        "source_monoTransStatus": "",
        "source_energy_unit": "",
        "source_energy": None,
        "source_IDgap_unit": "",
        "source_IDgap": None,
        "source_IDtaper_unit": "",
        "source_IDtaper": None,
        "source_ringCurrent_unit": "",
        "source_ringCurrent": None,
        "sample_XYZ_unit": "",
        "sample_XYZ_desc": "",
        "sample_XYZ": "",
        "knife-edge_XYZ_unit": "",
        "knife-edge_XYZ_desc": "",
        "knife-edge_XYZ": "",
        "knife-edge_knifeScan_unit": "",
        "knife-edge_knifeScan": None,
        "mda_file": "",
        "scanEnd_abort": "",
        "scanEnd_time_epoch": None,
        "scanEnd_time": "",
        "scanEnd_scanDuration_unit": "",
        "scanEnd_scanDuration": None,
        "scanEnd_source_beamBad": "",
        "scanEnd_source_ringCurrent_unit": "",
        "scanEnd_source_ringCurrent": None,
    }

    log_dict = traverse_tree(scan, log_dict)

    # Convert empty strings to None for numeric fields
    for field in numeric_fields:
        if field in log_dict and log_dict[field] == "":
            log_dict[field] = None

    scan_label = "scan"
    scan_dims = list(scan.iter(f"{{{xmlns}}}{scan_label}"))
    # scan_dims_num = str(len(scan_dims))

    # *****#
    PV_label1 = "positioner"
    PV_label2 = "detectorTrig"
    scanEnd_cpt_list = scan.find(f"{{{xmlns}}}scanEnd").find(f"{{{xmlns}}}cpt").text.split()[::-1]
    # Define numeric fields for scan dimensions
    scan_numeric_fields = {"dim", "npts", "cpt"}

    dims_dict_list = []
    for ii, dim in enumerate(scan_dims):
        dim_dict = {
            "scanNumber": scanNumber,
            "dim": None,
            "npts": None,
            "after": "",
            "positioner1_PV": "",
            "positioner1_ar": "",
            "positioner1_mode": "",
            "positioner1": "",
            "positioner2_PV": "",
            "positioner2_ar": "",
            "positioner2_mode": "",
            "positioner2": "",
            "positioner3_PV": "",
            "positioner3_ar": "",
            "positioner3_mode": "",
            "positioner3": "",
            "positioner4_PV": "",
            "positioner4_ar": "",
            "positioner4_mode": "",
            "positioner4": "",
            "detectorTrig1_PV": "",
            "detectorTrig1_VAL": "",
            "detectorTrig2_PV": "",
            "detectorTrig2_VAL": "",
            "detectorTrig3_PV": "",
            "detectorTrig3_VAL": "",
            "detectorTrig4_PV": "",
            "detectorTrig4_VAL": "",
            "cpt": None,
        }
        dim_dict.update(dim.attrib)
        PV_count_dict = {PV_label1: 0, PV_label2: 0}
        for record in dim:
            # record_name = name(record.tag)
            if "PV" in record.attrib.keys():
                record_name = name(record.tag)
                for PV_label in PV_count_dict.keys():
                    if PV_label in record_name:
                        PV_count_dict[PV_label] += 1
                        record_label = f"{PV_label}{PV_count_dict[PV_label]}"
                        record_dict = dict([("_".join([record_label, k]), v) for k, v in record.attrib.items()])
                        if record.text:
                            record_dict[f"{record_label}"] = record.text
                        dim_dict.update(record_dict)
        dim_dict["cpt"] = scanEnd_cpt_list[ii]

        # Convert empty strings to None for numeric fields
        for field in scan_numeric_fields:
            if field in dim_dict and dim_dict[field] == "":
                dim_dict[field] = None

        dim_dict = {f"{scan_label}_{k}" if k != "scanNumber" else k: v for k, v in dim_dict.items()}
        dims_dict_list.append(dim_dict)
    # *****#

    return log_dict, dims_dict_list


def _find_text(element, path) -> str | None:
    """Stripped child text, or None when the optional element is missing or empty."""
    child = element.find(path)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def _integer(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _number(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _stored_integer(value: object) -> int | None:
    """``value`` as an integer column stores it: a whole number SQLite's INTEGER holds, else None."""
    number = _integer(value)
    return number if number is not None and -SQLITE_INTEGER_MAX - 1 <= number <= SQLITE_INTEGER_MAX else None


def _completed_points_stored(scan, *, namespace: str, dimensions: int) -> bool:
    """Whether the import can store a scan's completed points: ``scanEnd/cpt`` holds a whole number for each of its
    dimensions (``scan_cpt`` is NOT NULL, so a scan without them cannot be imported)."""
    end = scan.find(f"{namespace}scanEnd")
    completed = None if end is None else end.find(f"{namespace}cpt")
    if completed is None or completed.text is None:
        return False
    values = completed.text.split()[::-1]  # as ``_parse_scan_element`` reads them
    return len(values) >= dimensions and all(_stored_integer(value) is not None for value in values[:dimensions])


_ASCII_DIGITS = re.compile(r"[0-9]+")
_MAX_DIGITS = len(str(SQLITE_INTEGER_MAX))


def parse_scan_number(value: object) -> int | None:
    """A log's ``scanNumber`` as the database stores it: ASCII digits (white space around them allowed) within
    0..``SQLITE_INTEGER_MAX``; anything else is None, and the scan is ``invalid``. The index, ``first_positions``
    and the import all read it here, so a ``new`` row's number is one the import can store (``int`` alone also
    takes ``-5``, ``276_990`` and other scripts' digits, and numbers past SQLite's INTEGER)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _ASCII_DIGITS.fullmatch(text) or len(text.lstrip("0")) > _MAX_DIGITS:
        return None
    number = int(text)
    return number if number <= SQLITE_INTEGER_MAX else None


def _scan_index_entry(scan, scan_index: int, *, xmlns: str) -> ScanLogEntry:
    """The lightweight record of the first-stage scan index (status refined by ``_classify``)."""
    namespace = f"{{{xmlns}}}"
    time_element = scan.find(f"{namespace}time")
    energy_element = scan.find(f"{namespace}source/{namespace}energy")
    scan_number = parse_scan_number(scan.get("scanNumber"))
    dimensions = sum(1 for _ in scan.iter(f"{namespace}scan"))

    return ScanLogEntry(
        scan_index=scan_index,
        scan_number=scan_number,
        status=ScanLogStatus.NEW if scan_number is not None else ScanLogStatus.INVALID,
        time=_find_text(scan, f"{namespace}time"),
        time_epoch=_integer(time_element.get("epoch")) if time_element is not None else None,
        user_name=_find_text(scan, f"{namespace}user/{namespace}name"),
        energy=_number(_find_text(scan, f"{namespace}source/{namespace}energy")),
        energy_unit=(energy_element.get("unit") or None) if energy_element is not None else None,
        sample_xyz=_find_text(scan, f"{namespace}sample/{namespace}XYZ"),
        num_dims=dimensions,
        importable=_completed_points_stored(scan, namespace=namespace, dimensions=dimensions),
    )


def _classify(entries: Iterable[ScanLogEntry], existing: Iterable[int] = ()) -> list[ScanLogEntry]:
    """The rule: invalid number, else a repeat of an earlier scan in the log, else in the database, else new."""
    in_database = set(existing)
    seen: set[int] = set()
    classified = []
    for entry in entries:
        number = entry.scan_number
        if number is None:
            status = ScanLogStatus.INVALID
        elif number in seen:
            status = ScanLogStatus.DUPLICATE
        elif number in in_database:
            status = ScanLogStatus.EXISTS
        else:
            status = ScanLogStatus.NEW
        if number is not None:
            seen.add(number)
        classified.append(entry if entry.status == status else entry.model_copy(update={"status": status}))
    return classified


def index_scans_from_xml(xml_bytes, *, xmlns: str) -> list[ScanLogEntry]:
    """Parse a log once and list its scans for selection (``new``, ``duplicate`` or ``invalid``).

    The database is not consulted; ``index_scan_log`` also marks scans that already exist.
    """
    root = parse_scan_log(xml_bytes)
    return _classify(
        _scan_index_entry(element, scan_index, xmlns=xmlns)
        for scan_index, element in enumerate(root)
        if element.tag.endswith("Scan")
    )


def index_scan_log(xml_bytes, *, xmlns: str, engine: Engine) -> list[ScanLogEntry]:
    """The import index of a log: every scan with its status, including ``exists`` from the database."""
    entries = index_scans_from_xml(xml_bytes, xmlns=xmlns)
    numbers = [entry.scan_number for entry in entries if entry.scan_number is not None]
    return _classify(entries, check_existing_scan_numbers(numbers, engine=engine) if numbers else ())


@dataclass(frozen=True)
class ParsedScan:
    """One fully parsed scan element: the values to import, keyed by their path in the log."""

    scan_index: int
    scan_number: int | None
    log: dict[str, Any]
    """``metadata`` row values (``import_metadata_row`` maps them to columns)."""
    scans: list[dict[str, Any]]
    """One ``scan`` row per scan dimension (``import_scan_row`` maps them to columns)."""


def _parse_scans_from_root(root, selected_indices=None, *, xmlns: str) -> tuple[list[ParsedScan], dict[int, str]]:
    """Parsed scans in log order, plus ``{scan_index: error}`` for scan elements that failed to parse."""
    selected = None if selected_indices is None else {int(index) for index in selected_indices}
    found = set()
    results = []
    failures: dict[int, str] = {}

    for scan_index, element in enumerate(root):
        if not element.tag.endswith("Scan") or (selected is not None and scan_index not in selected):
            continue

        found.add(scan_index)
        try:
            log, scans = _parse_scan_element(element, xmlns=xmlns)
        except Exception as error:
            logger.exception("Skipping scan at XML index %s after parse failure", scan_index)
            failures[scan_index] = f"{type(error).__name__}: {error}"
            continue
        results.append(
            ParsedScan(
                scan_index=scan_index, scan_number=parse_scan_number(log.get("scanNumber")), log=log, scans=scans
            )
        )

    if selected is not None:
        missing = selected - found
        if missing:
            missing_display = ", ".join(str(index) for index in sorted(missing))
            raise ValueError(f"Selected scan indices are not present in the staged log: {missing_display}")

    return results, failures


def parse_selected_scans_from_xml(xml_bytes, scan_indices, *, xmlns: str) -> list[ParsedScan]:
    """Fully parse only the selected top-level scan elements in one document pass (failures are logged)."""
    root = parse_scan_log(xml_bytes)
    return _parse_scans_from_root(root, selected_indices=scan_indices, xmlns=xmlns)[0]


def staging_directory(files: FilesSettings) -> Path:
    """Where uploaded scan logs are staged: ``<uploads_dir>/scan-logs`` (``uploads_dir`` defaults to
    ``<runs_root>/.uploads``)."""
    uploads = files.uploads_dir if files.uploads_dir is not None else files.runs_root / ".uploads"
    return uploads / SCAN_LOG_STAGING_SUBDIRECTORY


def _ensure_scan_log_cache_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        logger.warning("Could not restrict permissions on scan-log cache %s", directory)


def _scan_log_cache_path(directory: Path, token: object) -> Path:
    if not isinstance(token, str):
        raise ValueError("Invalid staged scan-log token")
    try:
        canonical_token = uuid.UUID(token).hex
    except (ValueError, AttributeError) as error:
        raise ValueError("Invalid staged scan-log token") from error
    if token != canonical_token:
        raise ValueError("Invalid staged scan-log token")
    return directory / f"{canonical_token}.xml"


def cleanup_expired_scan_logs(files: FilesSettings, now: float | None = None) -> int:
    """Remove staged logs not used within the TTL, and stale temporary files of interrupted uploads.

    Returns how many files were removed.
    """
    directory = staging_directory(files)
    try:
        directory.stat()
    except FileNotFoundError:
        return 0
    except OSError:  # e.g. under a folder the server may not open (Path.exists raises then, since Python 3.12)
        logger.warning("Could not open the staged scan-log folder %s", directory, exc_info=True)
        return 0

    cutoff = (time.time() if now is None else now) - SCAN_LOG_CACHE_TTL_SECONDS
    removed = 0
    for path in [*directory.glob("*.xml"), *directory.glob(".*.tmp")]:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Could not clean up staged scan log %s", path, exc_info=True)
    return removed


def _make_room(directory: Path, size: int) -> None:
    """Drop the staged logs used least recently (oldest modification: a load touches its log) until ``size`` more
    bytes fit in ``STAGED_SCAN_LOGS_LIMIT``."""
    kept: list[tuple[float, int, Path]] = []
    for path in directory.glob("*.xml"):
        try:
            status = path.stat()
        except OSError:
            continue
        kept.append((status.st_mtime, status.st_size, path))
    total = sum(entry[1] for entry in kept)
    for _, kept_size, path in sorted(kept):
        if total + size <= STAGED_SCAN_LOGS_LIMIT:
            break
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not drop staged scan log %s", path, exc_info=True)
            continue
        total -= kept_size


def stage_scan_log(xml_bytes: bytes, *, files: FilesSettings) -> str:
    """Stage an already-validated uploaded scan log and return an opaque token for it (the least recently used
    logs dropped first while the staged logs would hold more than ``STAGED_SCAN_LOGS_LIMIT``)."""
    directory = staging_directory(files)
    _ensure_scan_log_cache_dir(directory)
    cleanup_expired_scan_logs(files)
    _make_room(directory, len(xml_bytes))

    token = uuid.uuid4().hex
    final_path = _scan_log_cache_path(directory, token)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{token}-", suffix=".tmp", dir=directory)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(xml_bytes)
        temporary_path.chmod(0o600)
        temporary_path.replace(final_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return token


def load_staged_scan_log(token: object, *, files: FilesSettings) -> bytes:
    """Load a staged scan log, refreshing its expiry window on access."""
    path = _scan_log_cache_path(staging_directory(files), token)
    try:
        if path.stat().st_mtime < time.time() - SCAN_LOG_CACHE_TTL_SECONDS:
            path.unlink()
            raise FileNotFoundError
        xml_bytes = path.read_bytes()
        path.touch()
    except FileNotFoundError as error:
        raise FileNotFoundError("The staged scan log expired or is unavailable; upload it again.") from error
    return xml_bytes


def build_technique_strings(scans, none="none", *, motor_groups: MotorGroups):
    """Build the filtered and complete motor-group descriptions for scans."""
    found_groups = []
    for scan in scans:
        for attr_name in dir(scan):
            if attr_name.startswith("scan_positioner") and attr_name.endswith("_pv"):
                pv_value = getattr(scan, attr_name, None)
                if pv_value:
                    found_groups.append(find_motor_group(pv_value, motor_groups=motor_groups))

    all_motors_str = "; ".join(found_groups) if found_groups else none
    if not found_groups:
        return none, all_motors_str

    motor_groups_lower = [group.lower() for group in found_groups]
    sample_count = motor_groups_lower.count("sample")
    seen_groups = {none}
    final_groups = []

    for group in motor_groups_lower:
        if group in seen_groups:
            continue
        seen_groups.add(group)
        if group == "sample":
            final_groups.append("line" if sample_count == 1 else "area")
        else:
            final_groups.append(group)

    return " + ".join(final_groups) if final_groups else none, all_motors_str


def find_motor_group(pv_value, *, motor_groups: MotorGroups):
    """
    Find which motor group contains the given motor string.

    Args:
        pv_value: The motor PV string to search for
        motor_groups: Motor group name -> motor PVs (``settings.beamline.motor_groups``)

    Returns:
        The motor group key if found, "none" if pv_value is None,
        or "other" if pv_value is not None but not found
    """
    if pv_value is None:
        return "none"

    motor_string = pv_value.split(".VAL")[0]
    for group_key, motor_list in motor_groups.items():
        if motor_string in motor_list:
            return group_key

    return "other"


def update_motor_group_totals(motor_group_totals, scan, *, motor_groups: MotorGroups):
    """
    Updates the motor group total programmed points and completed points
    with the data for each motor group from a single scan.
    """
    for PV_i in range(1, 5):
        pv_attr = f"scan_positioner{PV_i}_pv"
        if getattr(scan, pv_attr, None):
            motor_group = find_motor_group(getattr(scan, pv_attr), motor_groups=motor_groups)
            if motor_group not in motor_group_totals:
                motor_group_totals[motor_group] = {"points": 1, "completed": 1}

            totals = motor_group_totals[motor_group]
            totals["points"] = _product(totals["points"], scan.scan_npts)
            totals["completed"] = _product(totals["completed"], scan.scan_cpt)
    return motor_group_totals


def _product(total: int | None, factor: int | None) -> int | None:
    """A motor group's total times a dimension's count: None (unknown) when either is, or past SQLite's INTEGER."""
    return None if total is None or factor is None else _stored_integer(total * factor)


def convert_time_string_to_datetime(time_string):
    """
    Convert time string to datetime object.
    Handles various time formats commonly found in the XML data.
    The result is naive: the log's local time, without a zone.
    """
    if not time_string or time_string == "":
        return None

    # Common time formats in the XML data
    time_formats = [
        "%Y-%m-%dT%H:%M:%S",  # ISO format: 2023-02-01T18:46:06
        "%Y-%m-%d %H:%M:%S",  # Alternative format
        "%Y-%m-%d, %H:%M:%S",  # Format with comma: 2023-02-25, 04:00:38
        "%Y-%m-%dT%H:%M:%S.%f",  # With microseconds
    ]

    for fmt in time_formats:
        try:
            return datetime.strptime(time_string, fmt)
        except ValueError:
            continue

    # If none of the formats work, raise an error
    raise ValueError(f"Unable to parse time string: {time_string}")


def import_metadata_row(metadata_object, *, timezone: tzinfo):
    """A ``Metadata`` row from parsed log values (keyed by their path in the log). A value that is not the number
    its column holds is stored as null (the index shows it so), so a ``new`` scan never fails on one.

    ``timezone`` is the zone of the log's local clock (the beamline's): ``time`` is read in it and
    stored in UTC.
    """
    local_time = convert_time_string_to_datetime(metadata_object["time"])
    metadata_row = Metadata(
        scan_number=metadata_object["scanNumber"],
        time_epoch=_stored_integer(metadata_object["time_epoch"]),
        time=local_to_utc(local_time, timezone) if local_time is not None else None,
        user_name=metadata_object["user_name"],
        source_beam_bad=metadata_object["source_beamBad"],
        source_ccd_shutter=metadata_object["source_CCDshutter"],
        source_mono_trans_status=metadata_object["source_monoTransStatus"],
        source_energy_unit=metadata_object["source_energy_unit"],
        source_energy=_number(metadata_object["source_energy"]),
        source_id_gap_unit=metadata_object["source_IDgap_unit"],
        source_id_gap=_number(metadata_object["source_IDgap"]),
        source_id_taper_unit=metadata_object["source_IDtaper_unit"],
        source_id_taper=_number(metadata_object["source_IDtaper"]),
        source_ring_current_unit=metadata_object["source_ringCurrent_unit"],
        source_ring_current=_number(metadata_object["source_ringCurrent"]),
        sample_xyz_unit=metadata_object["sample_XYZ_unit"],
        sample_xyz_desc=metadata_object["sample_XYZ_desc"],
        sample_xyz=metadata_object["sample_XYZ"],
        knife_edge_xyz_unit=metadata_object["knife-edge_XYZ_unit"],
        knife_edge_xyz_desc=metadata_object["knife-edge_XYZ_desc"],
        knife_edge_xyz=metadata_object["knife-edge_XYZ"],
        knife_edge_knife_scan_unit=metadata_object["knife-edge_knifeScan_unit"],
        knife_edge_knife_scan=_number(metadata_object["knife-edge_knifeScan"]),
        mda_file=metadata_object["mda_file"],
        scan_end_abort=metadata_object["scanEnd_abort"],
        scan_end_time_epoch=_stored_integer(metadata_object["scanEnd_time_epoch"]),
        scan_end_time=metadata_object["scanEnd_time"],
        scan_end_scan_duration_unit=metadata_object["scanEnd_scanDuration_unit"],
        scan_end_scan_duration=_number(metadata_object["scanEnd_scanDuration"]),
        scan_end_source_beam_bad=metadata_object["scanEnd_source_beamBad"],
        scan_end_source_ring_current_unit=metadata_object["scanEnd_source_ringCurrent_unit"],
        scan_end_source_ring_current=_number(metadata_object["scanEnd_source_ringCurrent"]),
    )
    return metadata_row


def import_scan_row(scan_object):
    """Create a Scan ORM object from a scan dictionary (keyed by the log's names); its numbers as
    ``import_metadata_row`` stores them."""
    scan_row = Scan(
        scan_number=scan_object["scanNumber"],
        scan_dim=_stored_integer(scan_object["scan_dim"]),
        scan_npts=_stored_integer(scan_object["scan_npts"]),
        scan_after=scan_object["scan_after"],
        scan_positioner1_pv=scan_object["scan_positioner1_PV"],
        scan_positioner1_ar=scan_object["scan_positioner1_ar"],
        scan_positioner1_mode=scan_object["scan_positioner1_mode"],
        scan_positioner1=scan_object["scan_positioner1"],
        scan_positioner2_pv=scan_object["scan_positioner2_PV"],
        scan_positioner2_ar=scan_object["scan_positioner2_ar"],
        scan_positioner2_mode=scan_object["scan_positioner2_mode"],
        scan_positioner2=scan_object["scan_positioner2"],
        scan_positioner3_pv=scan_object["scan_positioner3_PV"],
        scan_positioner3_ar=scan_object["scan_positioner3_ar"],
        scan_positioner3_mode=scan_object["scan_positioner3_mode"],
        scan_positioner3=scan_object["scan_positioner3"],
        scan_positioner4_pv=scan_object["scan_positioner4_PV"],
        scan_positioner4_ar=scan_object["scan_positioner4_ar"],
        scan_positioner4_mode=scan_object["scan_positioner4_mode"],
        scan_positioner4=scan_object["scan_positioner4"],
        scan_detector_trig1_pv=scan_object["scan_detectorTrig1_PV"],
        scan_detector_trig1_val=scan_object["scan_detectorTrig1_VAL"],
        scan_detector_trig2_pv=scan_object["scan_detectorTrig2_PV"],
        scan_detector_trig2_val=scan_object["scan_detectorTrig2_VAL"],
        scan_detector_trig3_pv=scan_object["scan_detectorTrig3_PV"],
        scan_detector_trig3_val=scan_object["scan_detectorTrig3_VAL"],
        scan_detector_trig4_pv=scan_object["scan_detectorTrig4_PV"],
        scan_detector_trig4_val=scan_object["scan_detectorTrig4_VAL"],
        scan_cpt=_stored_integer(scan_object["scan_cpt"]),  # NOT NULL: the index marks a scan without it invalid
    )
    return scan_row


def parse_all_scans_from_xml(xml_bytes, *, xmlns: str) -> list[ParsedScan]:
    """Parse every scan element of an XML log (failures are logged and left out)."""
    root = parse_scan_log(xml_bytes)
    return _parse_scans_from_root(root, xmlns=xmlns)[0]


def check_existing_scan_numbers(scan_numbers, *, engine: Engine) -> set[int]:
    """The scan numbers (strings or ints) that already exist in the database, as ints (queried in batches of
    ``EXISTING_BATCH``: a log may hold more scans than SQLite takes variables in one query)."""
    int_scan_numbers = sorted({int(sn) for sn in scan_numbers})

    found: set[int] = set()
    with Session(engine) as session:
        for start in range(0, len(int_scan_numbers), EXISTING_BATCH):
            batch = int_scan_numbers[start : start + EXISTING_BATCH]
            found.update(row[0] for row in session.query(Metadata.scan_number).filter(Metadata.scan_number.in_(batch)))
    return found


def _import_one(
    parsed: ParsedScan,
    catalog_defaults: Mapping[str, Any],
    *,
    engine: Engine,
    motor_groups: MotorGroups,
    timezone: tzinfo,
):
    scan_number = parsed.scan_number
    if scan_number is None:
        return ScanImportOutcome.FAILED, f"invalid scan number {parsed.log.get('scanNumber')!r}"
    with Session(engine) as session:
        try:
            begin_write(session)  # the check and the insert are one step: a concurrent import of it is ``exists``
            if session.query(Metadata).filter(Metadata.scan_number == scan_number).first():
                return ScanImportOutcome.EXISTS, None

            metadata = import_metadata_row(parsed.log, timezone=timezone)
            metadata.scan_number = scan_number  # the parsed number, not the log's text

            # Create scan dimension ORM objects and compute motor groups
            motor_group_totals = {}
            for scan_dict in parsed.scans:
                scan_row = import_scan_row(scan_dict)
                scan_row.scan_number = scan_number
                session.add(scan_row)
                motor_group_totals = update_motor_group_totals(motor_group_totals, scan_row, motor_groups=motor_groups)

            # Apply motor group totals with fallback logic
            if motor_group_totals:
                for specific_motor_group in ["sample", "depth"]:
                    if specific_motor_group not in motor_group_totals:
                        if any(group.get("completed", 0) for group in motor_group_totals.values()):
                            motor_group_totals[specific_motor_group] = {"points": 0, "completed": 1}

                for motor_group, totals in motor_group_totals.items():
                    setattr(metadata, f"motor_group_{motor_group}_npts_total", totals["points"])
                    setattr(metadata, f"motor_group_{motor_group}_cpt_total", totals["completed"])

            session.add(metadata)
            session.add(
                Catalog(
                    scan_number=scan_number,
                    filefolder=catalog_defaults.get("filefolder", ""),
                    filename_prefix=catalog_defaults.get("filename_prefix", []),
                    aperture=catalog_defaults.get("aperture") or "none",
                    sample_name=catalog_defaults.get("sample_name", ""),
                    notes=catalog_defaults.get("notes", ""),
                )
            )
            session.commit()
            return ScanImportOutcome.IMPORTED, None
        except Exception as error:
            session.rollback()
            logger.warning("Importing scan %s failed", scan_number, exc_info=True)
            return ScanImportOutcome.FAILED, _failure(error)


def _failure(error: Exception) -> str:
    """A failed scan's error for its result: an error of a statement (the driver's, or one raised while SQLAlchemy
    prepared its parameters) by its cause's first line (SQLAlchemy's text adds the statement and its parameters;
    the log keeps them), anything else as raised."""
    if isinstance(error, StatementError) and error.orig is not None:
        cause = error.orig
        lines = str(cause).splitlines()
        return f"{type(cause).__name__}: {lines[0]}" if lines else type(cause).__name__
    return f"{type(error).__name__}: {error}"


def bulk_import_scans(
    parsed_scans: Iterable[ParsedScan],
    catalog_defaults: Mapping[str, Any],
    *,
    engine: Engine,
    motor_groups: MotorGroups,
    timezone: tzinfo,
) -> list[ScanImportResult]:
    """Import scans, committing each one on its own so that a failure does not roll back the others.

    ``catalog_defaults`` holds catalog columns: ``filefolder``, ``filename_prefix``, ``aperture``,
    ``sample_name``, ``notes``. ``timezone`` is the zone of the log's local times. Returns one result per
    scan, in the given order.
    """
    results = []
    for parsed in parsed_scans:
        outcome, error = _import_one(
            parsed, catalog_defaults, engine=engine, motor_groups=motor_groups, timezone=timezone
        )
        results.append(
            ScanImportResult(scan_index=parsed.scan_index, scan_number=parsed.scan_number, outcome=outcome, error=error)
        )
    return results


def import_selected_scans(
    xml_bytes,
    scan_indices: Iterable[int],
    catalog_defaults: Mapping[str, Any],
    *,
    xmlns: str,
    engine: Engine,
    motor_groups: MotorGroups,
    timezone: tzinfo,
) -> list[ScanImportResult]:
    """Parse the selected scans of a (staged) log and import them: one result per selected scan, in log order.

    A selected scan element that fails to parse is ``failed`` with the parse error. Indices that are
    not scan elements of the log raise ValueError before anything is imported.
    """
    return _import_from_root(
        parse_scan_log(xml_bytes),
        scan_indices,
        catalog_defaults,
        xmlns=xmlns,
        engine=engine,
        motor_groups=motor_groups,
        timezone=timezone,
    )


def first_positions(root: ET.Element) -> dict[int, int]:
    """Each valid scan number of a parsed log and the position of its first scan element: the scan the index
    lists as ``new`` or ``exists`` (a later one with the number is a ``duplicate``)."""
    positions: dict[int, int] = {}
    for scan_index, element in enumerate(root):
        if element.tag.endswith("Scan"):
            number = parse_scan_number(element.get("scanNumber"))
            if number is not None:
                positions.setdefault(number, scan_index)
    return positions


def import_scan_numbers(
    xml_bytes,
    scan_numbers: Iterable[int],
    catalog_defaults: Mapping[str, Any],
    *,
    xmlns: str,
    engine: Engine,
    motor_groups: MotorGroups,
    timezone: tzinfo,
) -> list[ScanImportResult]:
    """Import the scans of a (staged) log by scan number (``POST /api/v1/scans/import``): each number's first scan
    in the log, the one its index row names. One result per number, in log order; a number the log does not hold
    raises ``UnknownScanNumbers`` before anything is imported."""
    root = parse_scan_log(xml_bytes)
    positions = first_positions(root)
    numbers = list(dict.fromkeys(scan_numbers))
    missing = [number for number in numbers if number not in positions]
    if missing:
        raise UnknownScanNumbers(missing)
    return _import_from_root(
        root,
        [positions[number] for number in numbers],
        catalog_defaults,
        xmlns=xmlns,
        engine=engine,
        motor_groups=motor_groups,
        timezone=timezone,
    )


def _import_from_root(
    root: ET.Element,
    scan_indices: Iterable[int],
    catalog_defaults: Mapping[str, Any],
    *,
    xmlns: str,
    engine: Engine,
    motor_groups: MotorGroups,
    timezone: tzinfo,
) -> list[ScanImportResult]:
    parsed, failures = _parse_scans_from_root(root, selected_indices=list(scan_indices), xmlns=xmlns)
    results = bulk_import_scans(parsed, catalog_defaults, engine=engine, motor_groups=motor_groups, timezone=timezone)
    numbers = {index: parse_scan_number(root[index].get("scanNumber")) for index in failures}
    results.extend(
        ScanImportResult(scan_index=index, scan_number=numbers[index], outcome=ScanImportOutcome.FAILED, error=error)
        for index, error in failures.items()
    )
    return sorted(results, key=lambda result: result.scan_index)
