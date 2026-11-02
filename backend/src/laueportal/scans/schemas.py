"""Typed results of the scans domain: the scan-log index and the per-scan import results, and the API schemas of the
scans grid, the scan detail and its catalog, and of the scan-log import. The API names are part of the contract (Orval
type names).

Values are raw (numbers as numbers, missing values as None, text as the scan log wrote it); scans of an
uploaded log are identified by their position in the log (``scan_index``), never by a stringified key.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.core.paging import GridPage
from laueportal.core.timeutil import UTCTimestamp


class ScanLogStatus(StrEnum):
    """Whether a scan of an uploaded log can be imported."""

    NEW = "new"
    """Not in the database yet: selectable for import."""
    EXISTS = "exists"
    """Its scan number is already in the database."""
    DUPLICATE = "duplicate"
    """An earlier scan in the same log has the same scan number."""
    INVALID = "invalid"
    """The scan number is missing or not an integer."""


class ScanLogEntry(BaseModel):
    """One scan of a scan log, as the import index lists it (read without parsing the whole scan)."""

    model_config = ConfigDict(frozen=True)

    scan_index: int
    """Position of the scan element in the log: the selection key for the import."""
    scan_number: int | None
    """None when the log's ``scanNumber`` is missing or not an integer (status ``invalid``)."""
    status: ScanLogStatus
    time: str | None
    """As written in the log (local time, no zone); ``time_epoch`` is the same instant in seconds."""
    time_epoch: int | None
    user_name: str | None
    energy: float | None
    energy_unit: str | None
    sample_xyz: str | None
    """Sample position as written in the log (``sample/XYZ``)."""
    num_dims: int
    """Scan dimensions."""
    importable: bool = True
    """False when the import could not store the scan: its completed points (``scanEnd/cpt``, a whole number per
    dimension) are missing or not numbers (``status`` stays as it is; the index lists such a ``new`` scan
    ``invalid``)."""


class ScanImportOutcome(StrEnum):
    """What importing one selected scan did."""

    IMPORTED = "imported"
    """Its log record, scan rows and catalog entry were saved."""
    EXISTS = "exists"
    """Its scan number was already in the database; nothing was written."""
    FAILED = "failed"
    """Parsing or saving it failed; nothing of it was written."""


class ScanImportResult(BaseModel):
    """What happened to one selected scan of a bulk import."""

    model_config = ConfigDict(frozen=True)

    scan_index: int
    scan_number: int | None
    outcome: ScanImportOutcome
    error: str | None = None
    """For ``failed``: the error (parsing or writing the scan); None otherwise."""


# ---------------------------------------------------------------------------
# API: the scans grid, the scan detail and the catalog


class _ApiModel(BaseModel):
    """API schemas: attribute docstrings become the OpenAPI descriptions (and Orval's comments)."""

    model_config = ConfigDict(use_attribute_docstrings=True)


Aperture = Literal["none", "wire", "mask"]
"""The apertures the catalog form offers, as stored: None is ``none``, Wire ``wire`` and Coded Aperture ``mask``."""


class ScanSummary(_ApiModel):
    """One row of the scans grid (``GET /api/v1/scans``)."""

    scan_number: int
    """The scan ID."""
    sample_name: str | None
    aperture: str | None
    """As the catalog stores it (``none``, ``wire`` or ``mask``; an older catalog may hold ``None``); null when
    the scan has no catalog entry."""
    user_name: str | None
    n_dimensions: int
    """The scan's dimensions: its number of scan rows (shown as ``2D``)."""
    technique: str
    """The motor groups the scan moves (``beamline.motor_groups``), in the order of its scan rows: ``line`` (one
    sample motor) or ``area`` (more), ``depth``, ``energy``, ``other``, joined with `` + ``, e.g. ``area + depth``;
    ``none`` without positioners."""
    time: UTCTimestamp
    """The scan's start."""
    notes: str | None
    """The catalog's notes; null without a catalog entry."""


class ScanPage(GridPage[ScanSummary]):
    """A page of the scans grid."""


class ScanMetadata(_ApiModel):
    """The scan log's record of a scan (the ``metadata`` table), as stored: the scan detail's Metadata tab."""

    scan_number: int
    time_epoch: int | None
    time: UTCTimestamp
    """The scan's start (the log's local time, converted to UTC)."""
    user_name: str | None
    source_beam_bad: str | None
    source_ccd_shutter: str | None
    source_mono_trans_status: str | None
    source_energy_unit: str | None
    source_energy: float | None
    source_id_gap_unit: str | None
    source_id_gap: float | None
    source_id_taper_unit: str | None
    source_id_taper: float | None
    source_ring_current_unit: str | None
    source_ring_current: float | None
    sample_xyz_unit: str | None
    sample_xyz_desc: str | None
    sample_xyz: str | None
    knife_edge_xyz_unit: str | None
    knife_edge_xyz_desc: str | None
    knife_edge_xyz: str | None
    knife_edge_knife_scan_unit: str | None
    knife_edge_knife_scan: float | None
    mda_file: str | None
    scan_end_abort: str | None
    scan_end_time_epoch: int | None
    scan_end_time: str | None
    """The log's text, as written (local time)."""
    scan_end_scan_duration_unit: str | None
    scan_end_scan_duration: float | None
    scan_end_source_beam_bad: str | None
    scan_end_source_ring_current_unit: str | None
    scan_end_source_ring_current: float | None
    motor_group_sample_npts_total: int | None
    motor_group_sample_cpt_total: int | None
    motor_group_energy_npts_total: int | None
    motor_group_energy_cpt_total: int | None
    motor_group_depth_npts_total: int | None
    motor_group_depth_cpt_total: int | None
    motor_group_other_npts_total: int | None
    motor_group_other_cpt_total: int | None


class ScanPositioner(_ApiModel):
    """A positioner a scan row moves (one with a PV): a row of the Scan Info tab's per-dimension table."""

    index: int
    """Its slot, 1 to 4 (``scan_positioner<index>_pv``)."""
    pv: str
    motor_group: str
    """The motor group of ``beamline.motor_groups`` the PV belongs to, or ``other``."""
    start: str | None
    """The first of the three parts of the positioner's text (``-10 10 1``), as written; null (as ``end`` and
    ``step``) unless the text has exactly three parts."""
    end: str | None
    """The text's second part."""
    step: str | None
    """The text's third part."""


class ScanDimension(_ApiModel):
    """One scan row (the ``scan`` table): a dimension of the scan, its four positioner and detector-trigger
    slots as stored (empty text for an unused slot), and the positioners it moves."""

    id: int
    scan_dim: int | None
    """The dimension's number in the scan log."""
    scan_npts: int | None
    """Points programmed."""
    scan_cpt: int
    """Points completed."""
    scan_after: str
    scan_positioner1_pv: str
    scan_positioner1_ar: str
    scan_positioner1_mode: str
    scan_positioner1: str
    scan_positioner2_pv: str
    scan_positioner2_ar: str
    scan_positioner2_mode: str
    scan_positioner2: str
    scan_positioner3_pv: str
    scan_positioner3_ar: str
    scan_positioner3_mode: str
    scan_positioner3: str
    scan_positioner4_pv: str
    scan_positioner4_ar: str
    scan_positioner4_mode: str
    scan_positioner4: str
    scan_detector_trig1_pv: str
    scan_detector_trig1_val: str
    scan_detector_trig2_pv: str
    scan_detector_trig2_val: str
    scan_detector_trig3_pv: str
    scan_detector_trig3_val: str
    scan_detector_trig4_pv: str
    scan_detector_trig4_val: str
    positioners: list[ScanPositioner]
    """The slots with a PV, in slot order, with their motor group and range."""


class ScanCatalog(_ApiModel):
    """What the scan log does not record (the ``catalog`` table): data folder, file prefixes, aperture, sample
    and notes."""

    filefolder: str
    """The data folder."""
    filename_prefix: list[str]
    aperture: str
    """As stored: ``none``, ``wire`` or ``mask`` (Coded Aperture) as the catalog form saves them; older rows
    may hold other text (an older catalog may hold ``None`` for None)."""
    sample_name: str | None
    notes: str | None
    version: str
    """The stored values' version (an entity tag of them, ``core.etag.value_tag``): any change to them, a note
    appended too, makes another. A catalog save sends the version it edited (``ScanCatalogUpdate.version``)."""

    @field_validator("filename_prefix", mode="before")
    @classmethod
    def _prefix_list(cls, value: Any) -> Any:
        if value is None:
            return []
        return [value] if isinstance(value, str) else value


class ScanCatalogUpdate(_ApiModel):
    """A scan's catalog, created or replaced whole by ``PUT /api/v1/scans/{scan_id}/catalog``."""

    model_config = ConfigDict(extra="forbid")

    filefolder: str
    """The data folder: an absolute path inside the allowed roots (``files.roots``; outside them, a 400); it need
    not exist here."""
    filename_prefix: list[str]
    """Filename prefixes; surrounding whitespace is removed, and empty entries are dropped."""
    aperture: Aperture
    """``none``, ``wire`` or ``mask`` (Coded Aperture)."""
    sample_name: str | None
    notes: str | None
    """Replaces the notes (``POST .../notes`` appends instead)."""
    version: str | None
    """The ``version`` of the stored catalog this edit started from, null for a scan without one. A catalog that
    changed since (another save, a note appended, an import) is not replaced: a 409."""


class ScanNote(_ApiModel):
    """Text to append to a scan's catalog notes (``POST /api/v1/scans/{scan_id}/notes``)."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    """Appended on a new line (the whole notes when they were empty), as written; not only whitespace."""

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the note is empty")
        return value


class ScanDetail(_ApiModel):
    """A scan: its log record, its scan rows and its catalog (``GET /api/v1/scans/{scan_id}``)."""

    scan_number: int
    technique: str
    """As in the scans grid (``ScanSummary.technique``); the scan detail calls it Scan Type."""
    metadata: ScanMetadata
    dimensions: list[ScanDimension]
    """The scan rows in the log's order."""
    catalog: ScanCatalog | None
    """Null until the catalog is filled in."""


# ---------------------------------------------------------------------------
# API: the scan-log import

MAX_IMPORT_SCANS = 100_000
"""The most scan IDs one import takes (a log of 100 MiB holds about 42,000 scans; ``files.max_scan_log_mb`` is 25 by
default)."""

ScanLogToken = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
"""A staged scan log's token: 32 lower-case hex digits (a random UUID's 122 bits)."""

ScanIdValue = Annotated[int, Field(ge=0, le=SQLITE_INTEGER_MAX)]


class ScanLogRow(_ApiModel):
    """One scan of an indexed scan log: a row of the import page's table."""

    scan_index: int
    """The scan's position in the log: the row's ID (a scan ID can repeat in a log)."""
    scan_number: int | None
    """The scan ID; null unless the log's is ASCII digits within SQLite's INTEGER
    (``invalid``)."""
    status: ScanLogStatus
    """The rules, in this order: ``invalid`` (no valid scan ID), ``duplicate`` (an earlier scan of the log has its
    ID), ``exists`` (the ID is in the database), else ``new``; and a ``new`` scan without a start time the import can
    store (``time`` null) is ``invalid`` too. Only ``new`` rows are imported, and each of them can be."""
    time: UTCTimestamp | None
    """The scan's start: the log's local time (``database.legacy_timezone``) in UTC, as the import stores it; null
    when the log has no time that can be read and stored (the log's text is not shown)."""
    user_name: str | None
    energy: float | None
    """Null when the log has none, or text that is not a number (the text is not shown)."""
    energy_unit: str | None
    sample_xyz: str | None
    """The sample position as the log writes it (``sample/XYZ``, e.g. ``7411.3 -492.2 -917``)."""
    n_dimensions: int
    """The scan's dimensions (its ``scan`` elements)."""


class ScanLogIndex(_ApiModel):
    """An indexed and staged scan log (``POST /api/v1/scans/import/index``)."""

    token: ScanLogToken
    """The staged log, for ``POST /api/v1/scans/import``: it expires 24 hours after its last use."""
    rows: list[ScanLogRow]
    """Every scan of the log, in log order."""


class ScanImportDefaults(_ApiModel):
    """The catalog entry every imported scan gets (the import defaults; the scan page edits each one later)."""

    model_config = ConfigDict(extra="forbid")

    aperture: Aperture = "wire"
    """``none``, ``wire`` (the default) or ``mask`` (Coded Aperture)."""
    sample_name: str = ""
    filefolder: str = ""
    """The data folder: empty (none yet) or an absolute path inside the allowed
    roots (``files.roots``; outside them, a 400); it need not exist here."""
    filename_prefix: list[str] = Field(default_factory=list)
    """Filename prefixes; surrounding whitespace is removed, and empty entries are dropped (as the catalog form)."""
    notes: str = ""


class ScanImportRequest(_ApiModel):
    """The scans of a staged log to import (``POST /api/v1/scans/import``)."""

    model_config = ConfigDict(extra="forbid")

    token: ScanLogToken
    """From the index (``ScanLogIndex.token``)."""
    scan_ids: list[ScanIdValue] = Field(min_length=1, max_length=MAX_IMPORT_SCANS)
    """The scan IDs of the ``new`` rows to import (a repeated ID counts once). Each names its first scan in the
    log; an ID the log does not hold is a 422 and nothing is imported."""
    catalog_defaults: ScanImportDefaults = Field(default_factory=ScanImportDefaults)


class ScanImportItem(_ApiModel):
    """What happened to one selected scan."""

    scan_number: int
    outcome: ScanImportOutcome
    error: str | None
    """For ``failed``: why (shortened); null otherwise."""


class ScanImportSummary(_ApiModel):
    """How many selected scans had each outcome."""

    imported: int
    exists: int
    failed: int


class ScanImportReport(_ApiModel):
    """The result of an import: one item per selected scan, in log order, and the counts."""

    results: list[ScanImportItem]
    summary: ScanImportSummary
