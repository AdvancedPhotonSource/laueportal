"""Settings: one TOML file plus ``LAUEPORTAL_<SECTION>__<KEY>`` environment overrides.

- ``Settings(...)`` is a plain validated value. It reads neither files nor the environment, so
  tests build their own (``Settings(database=..., files=...)``) and never see a developer's config.
- ``load_settings(path)`` reads the TOML file, applies the environment overrides on top
  (``LAUEPORTAL_FILES__ROOTS__SAMPLES``, ...) and validates. A retired section (``RETIRED_SECTIONS``: ``[redis]``)
  and its overrides are ignored, with one warning line.
- ``get_settings()`` is ``load_settings($LAUEPORTAL_CONFIG)``, cached. Only entry points (the CLI,
  the app factory used by ``serve``, the worker) call it; everything else receives settings.

Every model forbids unknown keys, so a typo in the file or in an override fails at startup with the
key's location. Error messages never include setting values.
``~`` is expanded in every path setting. Nothing here does I/O at import time.
"""

from __future__ import annotations

import functools
import logging
import math
import os
import re
import string
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, EnvSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict

CONFIG_ENV = "LAUEPORTAL_CONFIG"
ENV_PREFIX = "LAUEPORTAL_"
ENV_NESTED_DELIMITER = "__"

RETIRED_SECTIONS = ("redis",)
"""Sections the settings no longer have (``redis``: Redis and RQ, which the job table replaced). A settings file's
retired section and its ``LAUEPORTAL_<SECTION>__*`` variables are ignored, with one warning line naming them (never
a value), so a config or an environment file from before still starts."""

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """The configuration is missing or invalid. The message names keys, never values."""


# ---------------------------------------------------------------------------
# Field types


def _empty_to_none(value: Any) -> Any:
    """TOML has no null: an empty string means "not set" for optional settings."""
    return None if isinstance(value, str) and not value.strip() else value


def _expand(path: Path) -> Path:
    return path.expanduser()


def _expand_optional(path: Path | None) -> Path | None:
    return None if path is None else path.expanduser()


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


ExpandedPath = Annotated[Path, AfterValidator(_expand)]
"""A path with ``~`` expanded."""

OptionalPath = Annotated[Path | None, BeforeValidator(_empty_to_none), AfterValidator(_expand_optional)]
"""An optional path; ``""`` means not set."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Sections (deploy/config/dev.toml.template documents every key)


_HOST_NAME = re.compile(r"(\*|(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*)")


class AppSettings(_Section):
    title: str = "3DMN Portal"
    host: str = "127.0.0.1"
    port: int = Field(default=2100, ge=1, le=65535)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"], min_length=1)
    """The host names a request's ``Host`` header may name (its port aside): the names the users' browsers reach
    the portal by (``localhost`` through an SSH tunnel; the host's name behind a proxy). Any other is a 400
    (``app.HostCheckMiddleware``, logged), so a page whose DNS name was pointed at this host (DNS rebinding)
    cannot read the API's answers. ``*.example.org`` admits its subdomains, ``*`` every name (no check)."""
    static_dir: OptionalPath = None
    """The built SPA (``frontend/dist``). Not set or missing: the API is served alone (``laueportal serve`` without
    ``--reload``, production's, refuses to start)."""
    log_level: Annotated[Literal["debug", "info", "warning", "error"], BeforeValidator(_lower)] = "info"

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _host_names(cls, names: Any) -> Any:
        if not isinstance(names, list):
            return names
        names = [name.strip().lower() if isinstance(name, str) else name for name in names]
        bad = [name for name in names if not isinstance(name, str) or not _HOST_NAME.fullmatch(name)]
        if bad:
            raise ValueError(f"allowed_hosts must be host names without a scheme or port (or *.domain, *); got {bad}")
        return names


class DatabaseSettings(_Section):
    path: ExpandedPath
    journal_mode: Annotated[Literal["wal", "delete"], BeforeValidator(_lower)] = "wal"
    legacy_timezone: str = "America/Chicago"
    """Time zone of v1's naive local timestamps (the UTC migration converts them)."""
    protected: list[ExpandedPath] = Field(default_factory=list)
    """Absolute paths that ``seed demo``, ``seed slow-runs`` and ``db backup`` never write, even with ``--force``:
    each a database (with its journal, WAL and shared-memory files and its ``.sha256`` and ``.migration.sha256``
    checksums) or a folder (everything under it). None by default; a development copy lists the originals it keeps."""

    @field_validator("legacy_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {value!r}") from None
        return value

    @field_validator("protected")
    @classmethod
    def _absolute_protected(cls, paths: list[Path]) -> list[Path]:
        if any(not path.is_absolute() for path in paths):
            raise ValueError("protected paths must be absolute")
        return paths


class ExecutionSettings(_Section):
    """Whole-run limits and compute knobs.

    The defaults suit a development machine; the production template sets the production values.
    """

    job_timeout_seconds: int = Field(default=86400, gt=0)
    heartbeat_seconds: float = Field(default=15.0, gt=0)
    progress_seconds: float = Field(default=5.0, gt=0)
    cancel_poll_seconds: float = Field(default=2.0, gt=0)
    stale_heartbeat_seconds: int = Field(default=300, gt=0)
    shutdown_grace_seconds: int = Field(default=600, ge=0)
    dispatch_seconds: float = Field(default=1.0, gt=0)
    """How often the worker's dispatcher looks for work (``jobs.dispatch``; also at once when a job process exits):
    a read of SQLite on local disk."""
    reconcile_seconds: int = Field(default=60, gt=0)
    """How often the worker's reconciler runs (``jobs.reconciler``; also ``laueportal jobs reconcile --once``)."""
    creation_timeout_seconds: int = Field(default=600, ge=60)
    """A run still ``created`` or ``published`` (never queued) this long after its last change did not finish its
    creation: the reconciler finalizes it Failed and keeps its run directory. A remote run's plan
    building (one check per input on ``crux``, slow on a slow mount) records progress every minute
    (``remote.submission.PLAN_PROGRESS_SECONDS``), so this counts from its last progress, not its publication; hence
    at least a minute."""
    workers: int = Field(default=1, gt=0)
    max_in_flight: Annotated[PositiveInt | None, BeforeValidator(_empty_to_none)] = None
    """Outstanding inputs per indexing run; not set means automatic."""
    max_run_inputs: int = Field(default=1_000_000, gt=0)
    """The most inputs a new run may read (scan points × depths for each file name, counted before anything is
    listed or expanded): validation and run creation refuse more (``jobs.validation``), so a typed range cannot make
    the API expand millions of indices. The portal's own limit; the largest migrated run read 99,825."""
    job_log: OptionalPath = None
    """The job processes' own log file (``jobs.dispatch.JobLog``: each job process's stdout and stderr, appended), so
    a job process that outlives its worker still logs. The worker rotates it, only when it starts
    a job and the file has reached 20 MB, 10 kept (supervisor does not): a long run or a live session writes on past
    20 MB until the next start. Not set: the worker's own output, lost for a job process once its worker is gone."""
    reconstruction_workers: int = Field(default=1, gt=0)
    num_threads: int = Field(default=1, gt=0)
    memory_limit_mb: int = Field(default=512, gt=0)
    verbose: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> ExecutionSettings:
        # The limits that depend on each other.
        if self.stale_heartbeat_seconds <= self.heartbeat_seconds:
            raise ValueError("stale_heartbeat_seconds must exceed heartbeat_seconds")
        if self.max_in_flight is not None and self.max_in_flight < self.workers:
            raise ValueError("max_in_flight must be at least workers")
        return self


_ROOT_NAME = re.compile(r"[A-Za-z0-9_-]+")


class FilesSettings(_Section):
    runs_root: ExpandedPath
    """Run outputs live here; output templates are relative to it."""
    uploads_dir: OptionalPath = None
    """Staged scan-log uploads. Not set: ``<runs_root>/.uploads``."""
    hdf_extensions: list[str] = Field(default_factory=lambda: [".hdf", ".h5", ".hdf5", ".he5"])
    scan_log_extensions: list[str] = Field(default_factory=lambda: [".xml", ".txt"])
    scan_log_dir: OptionalPath = None
    """Where the scan-log picker opens (inside a root). Not set: the first root."""
    max_scan_log_mb: int = Field(default=25, gt=0)
    """The largest scan log the import reads, in MiB (real logs are 3-10 MB). Indexing or importing a log
    holds its parsed tree, about 10 MiB of memory per MiB of log, while the request runs: a 10 MiB log (about 4,200
    scans) indexes in 0.75 s with 95 MiB, a 100 MiB one in 7 s with 1 GiB. Raise it only with the memory to match."""
    listing_cap: int = Field(default=1000, gt=0)
    probe_timeout_seconds: float = Field(default=5.0, gt=0)
    """How long the API waits for a check of a recorded or requested path (a stat, a listing, an open:
    ``files.probe``) before it answers that the file system does not respond; a stat on a healthy NFS mount takes
    milliseconds. The worker and the CLI wait as long as a check takes."""
    hung_mount_seconds: float = Field(default=30.0, ge=0)
    """How long, at least, a mount whose check timed out answers "not responding" at once (also while that check is
    stuck), and the 503's ``Retry-After``."""
    probe_threads: int = Field(default=8, ge=2, le=64)
    """The API's threads for those checks: the most that can be stuck in the kernel on hung mounts (one mount holds
    at most two)."""
    roots: dict[str, ExpandedPath] = Field(default_factory=dict)
    """Named allowed roots: every path from the browser must resolve inside one."""

    @model_validator(mode="before")
    @classmethod
    def _default_uploads_dir(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and _empty_to_none(data.get("uploads_dir")) is None and data.get("runs_root"):
            data = {**data, "uploads_dir": str(Path(str(data["runs_root"])) / ".uploads")}
        return data

    @field_validator("hdf_extensions", "scan_log_extensions")
    @classmethod
    def _dotted(cls, values: list[str]) -> list[str]:
        bad = [value for value in values if not value.startswith(".") or len(value) < 2]
        if bad:
            raise ValueError(f"extensions must look like '.h5'; got {bad}")
        return [value.lower() for value in values]

    @field_validator("roots")
    @classmethod
    def _absolute_roots(cls, roots: dict[str, Path]) -> dict[str, Path]:
        for name, path in roots.items():
            if not _ROOT_NAME.fullmatch(name):
                raise ValueError(f"root name {name!r} may only contain letters, digits, '_' and '-'")
            if not path.is_absolute():
                raise ValueError(f"root {name!r} must be an absolute path")
        return roots


_DEFAULT_MOTOR_GROUPS: dict[str, list[str]] = {
    "sample": ["34ide:mxv:c0:m1", "34ide:mxv:c0:m3", "34ide:mxv:c0:m2", "34ide:focusTHK:F", "34ide:focusTHK:H"],
    "energy": ["34ide:monoE:keV"],
    "depth": [
        "34ide:t80:c0:m1",
        "34ide:t80:c0:m3",
        "34ide:t80:c0:m2",
        "34ide:focusAlio:F",
        "34ide:focusAlio:H",
        "34ide:mxv:c0:m4",
    ],
    "other": [
        "34ide:m58:c0:m6",
        "34ide:m58:c0:m8",
        "34ide:m58:c1:m1",
        "34ide:m58:c1:m2",
        "34ide:m58:c1:m3",
        "34ide:m58:c1:m5",
        "34ide:m58:c1:m7",
        "34ide:540:c0:out2",
        "34ide:540:c0:out3",
        "34ide:float6",
    ],
}


class BeamlineSettings(_Section):
    """34-ID-E specifics."""

    name: str = "34ID-E"
    scan_log_namespace: str = "http://sector34.xray.aps.anl.gov/34ide/scanLog"
    detector_datasets: list[str] = Field(default_factory=lambda: ["/entry1/data/data", "/entry/data/data", "/data"])
    simulation_kev: tuple[float, float] = (6.0, 30.0)
    """The detector view's simulation window in keV: a request may narrow it,
    not widen it (the simulation's cost grows steeply with the window)."""
    motor_groups: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in _DEFAULT_MOTOR_GROUPS.items()}
    )

    @field_validator("simulation_kev")
    @classmethod
    def _window(cls, value: tuple[float, float]) -> tuple[float, float]:
        if not 0 < value[0] < value[1]:
            raise ValueError("simulation_kev must be [low, high] with 0 < low < high")
        return value


def _template_fields(template: str) -> set[str]:
    try:
        return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError as exc:
        raise ValueError(f"malformed template: {exc}") from None


class OutputTemplates(_Section):
    """Run output folders relative to ``files.runs_root``.

    ``{scan}`` and ``{rec}`` segments are omitted when the run has no scan or reconstruction.
    """

    indexing: str = "analysis/scan_{scan}/rec_{rec}/index_{run}"
    reconstruction: str = "analysis/scan_{scan}/rec_{run}/data"

    @field_validator("indexing", "reconstruction")
    @classmethod
    def _placeholders(cls, value: str, info: ValidationInfo) -> str:
        allowed = {"scan", "rec", "run"} if info.field_name == "indexing" else {"scan", "run"}
        names = _template_fields(value)
        if names - allowed:
            raise ValueError(f"unknown placeholders {sorted(names - allowed)}; allowed: {sorted(allowed)}")
        if "run" not in names:
            raise ValueError("the template must contain {run}")
        return value


def _depth_text(value: Any) -> Any:
    """The ``depth`` default: a number in µm as text (the request's type), or not set ("", NaN)."""
    value = _empty_to_none(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        text = str(value).strip()
        try:
            number = float(text)
        except ValueError:
            raise ValueError("depth must be a number in µm, or empty for automatic") from None
        if math.isinf(number):  # the parameter model refuses it
            raise ValueError("depth must be a finite number in µm, or empty for automatic")
        return None if math.isnan(number) else text
    return value


class IndexingDefaults(_Section):
    """LaueGo form defaults, by the request field names.

    The types follow ``LaueGoParamsV1`` (``indexing/lauego/params.py``, the fields' ``x-config``); the limits are the
    form's rules, which the model enforces (``indexing/validation.py`` reports them), and
    ``tests/core/test_settings.py`` checks both.
    """

    box_size: int = Field(default=18, gt=0)
    max_rfactor: float = Field(default=0.5, ge=0, le=1)
    threshold: Annotated[NonNegativeInt | None, BeforeValidator(_empty_to_none)] = None
    """Absolute intensity threshold (counts). Not set: automatic."""
    threshold_ratio: Annotated[NonNegativeFloat | None, BeforeValidator(_empty_to_none)] = None
    """Multiple of the image's standard deviation for the automatic threshold (lauelab
    ``PeakParams.threshold_ratio``, a float). Not set: lauelab's default (4.0)."""
    min_size: float = Field(default=3, gt=0)
    min_separation: int = Field(default=20, ge=0)
    max_number: Annotated[PositiveInt | None, BeforeValidator(_empty_to_none)] = 300
    peak_shape: Literal["Lorentzian", "Gaussian"] = "Lorentzian"
    """lauelab's peak models."""
    mask_file: OptionalPath = None
    smooth: bool = False
    index_kev_max_calc: float = Field(default=17.2, gt=0)
    """The form's indexing energy cutoffs (keV), this one and ``index_kev_max_test``. The scan's source energy does
    not replace them: a white-beam header's nominal energy is not an indexing cutoff. The prefill keeps them, or
    takes a source indexing run's (``jobs.prefill``)."""
    index_kev_max_test: float = Field(default=35.0, gt=0)
    index_angle_tolerance: float = Field(default=0.1, ge=0)
    index_hkl: tuple[int, int, int] = (0, 0, 1)
    index_cone: float = Field(default=72.0, ge=0, le=180)
    max_peaks: int = Field(default=200, gt=0)
    output_xml: str = "output.xml"
    geometry_file: OptionalPath = None
    crystal_file: OptionalPath = None
    scan_points: str = ""
    depth_range: str = ""
    depth: Annotated[str | None, BeforeValidator(_depth_text)] = None
    """The ``depth`` request field (text, µm): a finite number overrides every frame's depth. Not set, "" or
    NaN: automatic. A TOML number is accepted and stored as text."""
    author: str = ""
    notes: str = ""


class IndexingFixed(_Section):
    """Values written into every LaueGo request but never shown on forms."""

    detector_crop: tuple[int, int, int, int] = (0, 2047, 0, 2047)
    """x1, x2, y1, y2."""
    energy_unit: str = "keV"
    exposure_unit: str = "sec"
    reciprocal_lattice_unit: str = "1/nm"
    lattice_parameters_unit: str = "nm"
    beamline: str = "34ID-E"

    @model_validator(mode="before")
    @classmethod
    def _retired_cosmic_filter(cls, data: Any) -> Any:
        """``cosmic_filter`` was once a setting here, with no effect: run creation stores ``false`` for every
        run, now the parameter model's derived value. A config file that still sets it loads; the key is
        ignored, as its value always was."""
        if isinstance(data, dict) and "cosmic_filter" in data:
            return {key: value for key, value in data.items() if key != "cosmic_filter"}
        return data


class IndexingSettings(_Section):
    dataset_cache_size: int = Field(default=6, ge=1, le=16)
    """How many indexing results datasets the API keeps loaded for the results viewer (``science.cache``, least
    recently used first out). About 540 MB per dataset of 627,535 peaks once the peaks and
    patterns grids have read its tables, measured in a process of its own: the dataset 207 MB,
    the peaks table 308 MB of RSS (140 MB as Arrow), the patterns table 21 MB. The API process holds them all, on
    top of the loads' transients (``science.cache.MAX_LOADS``; loading a 344 MB Output XML peaks at 2.6 GB): after
    the viewer's products and simulations, the dev API held 1.07 GB RSS with one such dataset. So 6 such datasets
    hold about 3.2 GB and the dev template's 3 about 1.6 GB; size it to the host."""
    defaults: IndexingDefaults = Field(default_factory=IndexingDefaults)
    fixed: IndexingFixed = Field(default_factory=IndexingFixed)


class ReconstructionDefaults(_Section):
    """Wire form defaults."""

    geometry_file: OptionalPath = None
    percent_brightest: float = Field(default=100, gt=0, le=100)
    wire_edges: Literal["leading", "trailing", "both"] = "leading"
    depth_start: float = -50
    depth_end: float = 150
    depth_resolution: float = Field(default=1, gt=0)
    frame_skip: int = Field(default=1, ge=0)
    wire_skip: int = Field(default=1, ge=0)
    author: str = ""
    notes: str = ""

    @model_validator(mode="after")
    def _depths(self) -> ReconstructionDefaults:
        if self.depth_start >= self.depth_end:
            raise ValueError("depth_start must be below depth_end")
        return self


class ReconstructionSettings(_Section):
    product_cache_mib: int = Field(default=1024, ge=64, le=65536)
    """The wire viewer's product cache, in MiB (``science.recon.ProductCache``: catalog metadata, depth frames,
    traces and reference images, least recently used first out). The API process holds it on top of
    the indexing viewer's datasets: one 2048 x 2048 frame is 8 MiB as uint16 and 16 MiB as float32, so the
    measured sample's 101 depths are about 800 MiB once every frame was viewed; size it to the host. The API's
    resident memory under quick depth stepping is about this plus 250 MiB (40 frames at once on that
    sample: 780 MiB with 512), with the indexing viewer's datasets on top."""
    defaults: ReconstructionDefaults = Field(default_factory=ReconstructionDefaults)


class RemoteSection(_Section):
    """Remote execution through DM. Off unless enabled.

    The remote settings themselves (DM, storage, targets) are ``bdp.yaml``, a separate file the launcher
    reads too (``deploy/remote/bdp.yaml.template``; ``laueportal.remote.settings``); this section only names it.
    Its experiment directory must be ``files.runs_root`` (checked where the file is loaded).
    """

    enabled: bool = False
    settings_file: OptionalPath = None
    """The private ``bdp.yaml``; required when ``enabled``. Keep it outside the checkout."""

    @model_validator(mode="after")
    def _file_when_enabled(self) -> RemoteSection:
        if self.enabled and self.settings_file is None:
            raise ValueError("settings_file is required when remote execution is enabled")
        return self


class LiveSettings(_Section):
    """Live processing: the dispatcher's live rules, the arrivals' rates (each background rate a setting with a
    conservative default, running only while a live run needs it) and DM's operations for a DAQ's file catalog. The
    jobs core reads ``arrivals_seconds``; the dispatcher's live rules, the catalog thread, the DAQ list and the bind
    check (``jobs.catalog``, ``jobs.daq``) and the live computes read the rest."""

    enabled: bool = False
    """The Live page's API (``GET /live/daqs``, ``POST /live-runs/validate``, ``POST /live-runs``, and the
    live programs in ``GET /programs``): off, those routes answer 404 and ``/programs`` lists the batch programs
    only. The worker's live rules do not depend on it (a live run created while it was on still runs)."""
    batch_stop_seconds: float = Field(default=60.0, gt=0)
    """How long a running local batch run, stopped for a live run ("stopped for a live run"), has to drain before
    the dispatcher kills its job process's group and finalizes it."""
    cpu_share: float = Field(default=0.75, gt=0, le=1)
    """The live runs' share of the machine's cores: ``floor(cpu_count × cpu_share)``, at least 1."""
    wire_share: float = Field(default=0.67, gt=0, lt=1)
    """With a live wire run and its follower, the wire run's part of the live share (e.g. 16 of 24 cores); the
    LaueGo run takes the rest, each at least one core."""
    catalog_seconds: float = Field(default=5.0, gt=0)
    """The catalog thread's cadence while files arrive: one DM call per cycle, reading every page since its cursor."""
    catalog_idle_seconds: float = Field(default=30.0, gt=0)
    """The cadence it doubles up to while nothing arrives."""
    catalog_rescan_seconds: float = Field(default=300.0, gt=0)
    """How often it reads the folder's whole catalog again, besides its first cycle."""
    catalog_page_size: int = Field(default=1000, gt=0)
    """Rows per DM catalog request, a page (one at a time inside a cycle's call)."""
    catalog_max_rows: int = Field(default=5000, gt=0)
    """The most rows one cycle's call reads; a query it stops goes on at the next cycle, at once (one
    cycle's worth at 10 frames per second with the 120 s overlap is about 1,300)."""
    follow_seconds: float = Field(default=5.0, gt=0)
    """How often a follower's thread looks at its wire run's ``completed.jsonl`` (a stat, then a read when it grew)."""
    publish_seconds: float = Field(default=10.0, gt=0)
    """The most often a live job process republishes its snapshot (the wire run's catalog, the LaueGo preview), and
    a follower its ``arrival-status.json`` while its waiting reason stays."""
    arrivals_seconds: float = Field(default=1.0, gt=0)
    """The most often a live job process looks at its run's ``arrivals.jsonl`` on the runs root, however fast its
    live compute asks for arrivals: a stat, then a read when the file changed; its seal reads to the end at
    once."""
    dm_setup_file: OptionalPath = None
    """DM's setup file for the DAQ list, a DAQ's info and the catalog pages (``remote/dm.py``'s client)."""
    dm_ssh_host: str | None = None
    """The host the DM operations run on over SSH; not set: on the portal host."""
    dm_timeout_seconds: float = Field(default=15.0, gt=0)
    """The bound of one DM operation; a catalog call starts no request after half of it."""
    daq_list_seconds: float = Field(default=30.0, gt=0)
    """The api's DAQ list: one answer shared by every browser, at most one DM call this often, and only while one is
    asked for (``jobs.daq.DaqList``)."""
    daq_name_exclude: list[str] = Field(default_factory=lambda: ["logs", "current"])
    """The Live page's DAQ list (``GET /live/daqs``) leaves out a DAQ whose option shows one of these, ignoring case:
    its experiment name, or its data directory's last two names (``jobs.live_session.excluded``); Validate and Start
    still take it. Each entry is stripped, and a blank one is refused (it would match every DAQ, or one with a space).
    Empty: every DAQ."""

    _optional_text = field_validator("dm_ssh_host", mode="before")(_empty_to_none)

    @field_validator("daq_name_exclude")
    @classmethod
    def _daq_names(cls, values: list[str]) -> list[str]:
        stripped = [value.strip() for value in values]
        if not all(stripped):
            raise ValueError("an entry is blank: remove it, or give the text to leave out")
        return stripped

    @field_validator("dm_ssh_host")
    @classmethod
    def _ssh_destination(cls, value: str | None) -> str | None:
        if value is not None and value.startswith("-"):  # ssh would read it as an option (remote.settings' rule)
            raise ValueError("must not start with '-' (ssh would read it as an option)")
        return value

    @model_validator(mode="after")
    def _idle_after_active(self) -> LiveSettings:
        if self.catalog_idle_seconds < self.catalog_seconds:
            raise ValueError("catalog_idle_seconds must be at least catalog_seconds")
        return self


# ---------------------------------------------------------------------------
# Settings


class Settings(BaseSettings):
    """All settings. ``database.path`` and ``files.runs_root`` are required; everything else has a default."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        case_sensitive=False,
        extra="forbid",
        frozen=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    files: FilesSettings
    beamline: BeamlineSettings = Field(default_factory=BeamlineSettings)
    output_templates: OutputTemplates = Field(default_factory=OutputTemplates)
    indexing: IndexingSettings = Field(default_factory=IndexingSettings)
    reconstruction: ReconstructionSettings = Field(default_factory=ReconstructionSettings)
    remote: RemoteSection = Field(default_factory=RemoteSection)
    live: LiveSettings = Field(default_factory=LiveSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Constructor arguments only: load_settings() layers the file and the environment.
        return (init_settings,)

    def redacted(self) -> dict[str, Any]:
        """All settings as JSON-ready data (``laueportal config show``)."""
        return self.model_dump(mode="json")


def format_validation_error(error: ValidationError) -> str:
    """One line per problem, without the offending values (they may be secrets)."""
    lines = []
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        where = ".".join(str(part) for part in item["loc"]) or "(top level)"
        lines.append(f"  {where}: {item['msg']}")
    return "\n".join(lines)


def environment_overrides(environ: Mapping[str, str] | None = None) -> list[str]:
    """The ``LAUEPORTAL_*`` override variables present (names only), sorted.

    Raises ConfigError for a variable that cannot belong to any section, such as the easy typo
    ``LAUEPORTAL_FILES_RUNS_ROOT`` (one underscore), which pydantic-settings would silently ignore. A retired
    section's variables are not overrides (``retired_variables``).
    """
    environ = os.environ if environ is None else environ
    sections = set(Settings.model_fields)
    names = []
    for name in environ:
        upper = name.upper()
        if not upper.startswith(ENV_PREFIX) or upper == CONFIG_ENV:
            continue
        section = _section_of(upper)
        if section in RETIRED_SECTIONS:
            continue
        if section not in sections:
            raise ConfigError(
                f"unknown environment variable {name}: overrides are "
                f"{ENV_PREFIX}<SECTION>{ENV_NESTED_DELIMITER}<KEY> with SECTION one of {', '.join(sorted(sections))}"
            )
        names.append(name)
    return sorted(names)


def _section_of(upper: str) -> str:
    return upper[len(ENV_PREFIX) :].split(ENV_NESTED_DELIMITER, 1)[0].lower()


def retired_variables(environ: Mapping[str, str] | None = None) -> list[str]:
    """The ``LAUEPORTAL_<SECTION>__*`` variables of a retired section (``RETIRED_SECTIONS``) present: names only,
    sorted. ``load_settings`` ignores them."""
    environ = os.environ if environ is None else environ
    return sorted(
        name
        for name in environ
        if name.upper().startswith(ENV_PREFIX)
        and ENV_NESTED_DELIMITER in name
        and _section_of(name.upper()) in RETIRED_SECTIONS
    )


def _warn_retired(where: str, sections: list[str], variables: list[str]) -> None:
    """One warning line for whatever retired settings ``load_settings`` ignored: names, never values."""
    parts = [f"[{section}] in {where}" for section in sections] + variables
    if parts:
        logger.warning(
            "Ignored %s: Redis and RQ are no longer used; remove %s",
            ", ".join(parts),
            "it" if len(parts) == 1 else "them",
        )


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def load_settings(config_file: str | os.PathLike[str] | None) -> Settings:
    """Read ``config_file`` (TOML; ``None`` = no file) and apply the ``LAUEPORTAL_*`` environment overrides."""
    data: dict[str, Any] = {}
    where = "environment"
    if config_file is not None:
        path = Path(config_file).expanduser()
        where = str(path)
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError:
            raise ConfigError(f"settings file {path} does not exist") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"settings file {path} is not valid TOML: {exc}") from None
    environment_overrides()  # rejects unknown LAUEPORTAL_* names before pydantic-settings ignores them
    retired = [section for section in RETIRED_SECTIONS if section in data]
    data = {key: value for key, value in data.items() if key not in RETIRED_SECTIONS}
    try:
        env = EnvSettingsSource(Settings)()
        settings = Settings(**_deep_merge(data, env))
    except ValidationError as exc:
        raise ConfigError(
            f"invalid settings ({where} and {ENV_PREFIX}* overrides):\n{format_validation_error(exc)}"
        ) from None
    except ValueError as exc:  # pydantic-settings could not parse a complex override (expects JSON)
        raise ConfigError(f"invalid {ENV_PREFIX}* override: {exc}") from None
    _warn_retired(where, retired, retired_variables())
    return settings


def config_file_from_env() -> Path:
    value = os.environ.get(CONFIG_ENV, "").strip()
    if not value:
        raise ConfigError(f"{CONFIG_ENV} is not set: point it at a TOML settings file (deploy/config/*.toml.template)")
    return Path(value).expanduser()


@functools.cache
def get_settings() -> Settings:
    """The process's settings: ``$LAUEPORTAL_CONFIG`` plus environment overrides, loaded once."""
    return load_settings(config_file_from_env())
