"""A create form's values from one source context: the URL's prefill, and the clone of a run.

``GET /api/v1/programs/{kind}/prefill?scan=&reconstruction=&indexing=`` serves :func:`prefill`. Each parameter is
one ID (blank is none; anything else is a 422). An unknown source is a 404; sources that disagree (a reconstruction
of another scan) a 422.

The values start from the configured defaults (``jobs.programs.form_defaults``; the energy cutoffs stay the
configured ones), with the source's over them:

- **a scan** (``scan``): its catalog's folder and file-name templates;
- **a reconstruction on the LaueGo form** (``reconstruction``): its catalog (``<run directory>/reconstruction/
  scan.h5``) as the input, or its run directory when that file is not there (per-depth runs), and its
  file-name templates, scan points and geometry file; the scan is the reconstruction's;
- **a clone** (an indexing run on the LaueGo form, a reconstruction on the wire form): the run's saved parameters, its
  input folder, scan and reconstruction, with the author and notes reset to the configured defaults. Only form fields
  are copied: no config or derived value. A live run with no file name template (``filename_prefixes`` ``[]``) gives
  ``*%d.h5`` (``jobs.arrivals.NO_TEMPLATE``: the files it took, with the same numbers, but for a name whose last
  number does not end it), here and as a reconstruction's templates on the LaueGo form, never a blank Filename. A mapping applies to values the models refuse: an empty ``frame_skip`` or
  ``wire_skip`` becomes 1, a ``threshold_ratio`` of 0 or less becomes null; and a ``depth`` of ``nan`` (a migrated
  run's: 49 of 75 LaueGo runs) becomes null (automatic, as compute read it). The target is the run's when this
  deployment still offers it.

The output folder is the configured template for the source's scan and reconstruction (``output_path_template``).
Nothing is validated here: the form validates the values (a copied value a model refuses is an
issue there). Reads only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.core.db import SQLITE_INTEGER_MAX
from laueportal.core.problems import ProblemError
from laueportal.core.settings import Settings
from laueportal.core.text import quoted
from laueportal.files.probe import NotResponding, bounded
from laueportal.files.roots import is_file_here
from laueportal.indexing.models import IndexingRun
from laueportal.jobs import programs
from laueportal.jobs.arrivals import NO_TEMPLATE
from laueportal.jobs.models import Job
from laueportal.jobs.rundir import reconstruction_catalog_path
from laueportal.jobs.schemas import ProgramPrefill, RunKind, RunReference
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.reconstruction.wire.constants import DEFAULT_SKIP
from laueportal.scans.models import Metadata
from laueportal.scans.service import ScanNotFound, get_catalog_by_scan_number

RESET = ("author", "notes")
"""The fields a clone resets to their configured defaults."""
LABELS = {"scan": "scan", "reconstruction": "reconstruction", "indexing": "indexing run"}

_ID = re.compile(r"[0-9]{1,19}")
"""An ID's text: at most 19 digits (SQLite's largest INTEGER has 19), checked before it is converted (Python
refuses to convert more than 4,300 digits)."""


def source_id(name: str, value: str | None) -> int | None:
    """The ID ``value`` names (the query parameter ``name``'s text): ``None`` without one (absent or blank); a value
    that is not an ID is a 422."""

    entry = (value or "").strip()
    if not entry:
        return None
    if not _ID.fullmatch(entry) or int(entry) > SQLITE_INTEGER_MAX:
        message = (
            f"The {LABELS[name]} must be an ID (a whole number, at most {SQLITE_INTEGER_MAX}); got {quoted(entry)}"
        )
        raise ProblemError(
            422,
            "Invalid source",
            message,
            errors=[{"loc": ["query", name], "code": "int_parsing", "message": message}],
        )
    return int(entry)


def _not_found(title: str, detail: str) -> ProblemError:
    return ProblemError(404, title, detail)


def _mismatch(name: str, detail: str) -> ProblemError:
    return ProblemError(
        422, "Sources disagree", detail, errors=[{"loc": ["query", name], "code": "source_mismatch", "message": detail}]
    )


def _agree(name: str, given: int | None, saved: int | None, detail: str) -> int | None:
    """The source's ``saved`` scan or reconstruction, which a ``given`` one must match; either when the
    other is unknown."""
    if given is not None and saved is not None and given != saved:
        raise _mismatch(name, detail)
    return saved if saved is not None else given


def _templates(value: Any) -> list[str]:
    """A catalog's or a run's file-name templates as the form takes them (a list; a catalog may hold text)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(item) for item in value]


def _run_templates(value: Any) -> list[str]:
    """A run's file-name templates for a form (``_templates``); none, a live run's with no template, is
    ``NO_TEMPLATE``, which takes the files that run took with the same numbers (but a name whose last number does not
    end it)."""
    return _templates(value) or [NO_TEMPLATE]


def _target(job: Job | None, settings: Settings) -> str:
    """The clone's target: its run's when this deployment still offers it, else the default (the first offered)."""
    offered = [option["value"] for option in programs.execution_targets(settings)]
    return job.backend if job is not None and job.backend in offered else offered[0]


def _blank(kind: programs.ProgramKind, settings: Settings) -> dict[str, Any]:
    """Every form field: its configured default, else null (no file-name template: an empty list)."""
    values: dict[str, Any] = dict.fromkeys(programs.PROGRAMS[kind].fields_from("form"))
    values["filename_prefixes"] = []
    values |= programs.form_defaults(kind, settings)
    if "backend" in values:
        values["backend"] = _target(None, settings)
    return values


def _copied(saved: object, values: Mapping[str, Any]) -> dict[str, Any]:
    """The form fields ``saved`` (a run's parameter row) holds, but those a clone resets or rebuilds."""
    skipped = {*RESET, programs.OUTPUT_PATH_TEMPLATE, "backend"}
    return {name: getattr(saved, name) for name in values if name not in skipped and hasattr(saved, name)}


def _clone_lauego(run: IndexingRun, values: dict[str, Any]) -> dict[str, Any]:
    copied = _copied(run.lauego_parameters, values)
    copied["filename_prefixes"] = _run_templates(copied.get("filename_prefixes"))
    ratio = copied.get("threshold_ratio")
    if ratio is not None and ratio <= 0:  # a migrated run's -1: "automatic", as compute read it
        copied["threshold_ratio"] = None
    depth = copied.get("depth")
    if isinstance(depth, str) and depth.strip().lower() == "nan":  # automatic
        copied["depth"] = None
    return copied | {"input_path": run.input_path}


def _clone_laue_matching(run: IndexingRun, values: dict[str, Any]) -> dict[str, Any]:
    """A LaueMatching run's values for the form: it reuses the run's cache, whichever run built it."""
    copied = _copied(run.laue_matching_parameters, values)
    copied["filename_prefixes"] = _run_templates(copied.get("filename_prefixes"))
    return copied | {"input_path": run.input_path, "create_forward_cache": False}


def _clone_wire(run: ReconstructionRun, values: dict[str, Any]) -> dict[str, Any]:
    copied = _copied(run.wire_parameters, values)
    copied["filename_prefixes"] = _run_templates(copied.get("filename_prefixes"))
    for name in ("frame_skip", "wire_skip"):
        if copied.get(name) is None:  # an empty skip: the default
            copied[name] = DEFAULT_SKIP
    return copied | {"input_path": run.input_path}


def _reconstruction_input(run: ReconstructionRun) -> dict[str, Any]:
    """A reconstruction as an indexing run's source, as its rows give it: its run directory as the
    input (``_catalog_or_folder`` picks its catalog after the session), its templates, scan points and geometry."""
    values: dict[str, Any] = {"input_path": run.output_path}
    parameters = run.wire_parameters
    if parameters is not None:
        values |= {
            "filename_prefixes": _run_templates(parameters.filename_prefixes),
            "scan_points": parameters.scan_points,
            "geometry_file": parameters.geometry_file,
        }
    return values


def _catalog_or_folder(output: str | None) -> str | None:
    """A reconstruction's input for indexing: its catalog (``<run directory>/reconstruction/scan.h5``), or its run
    directory when that file is not there (older per-depth runs) or its file system does not answer. A
    file-system check, through ``files.probe`` (a recorded path): run it with no session open, since a stat of a
    hanging ``/net`` path would hold a pooled connection."""
    catalog = reconstruction_catalog_path(output) if output else None
    if catalog is None:
        return output
    try:
        return catalog if bounded(catalog, lambda: is_file_here(catalog)) else output
    except NotResponding:
        return output


def prefill(
    kind: programs.ProgramKind,
    *,
    scan: str | None,
    reconstruction: str | None,
    indexing: str | None,
    settings: Settings,
    engine: Engine,
) -> ProgramPrefill:
    """The ``kind`` form's values from one source context (``scan``, ``reconstruction``, ``indexing``: the query's
    text, each one ID): see the module."""

    scan_number = source_id("scan", scan)
    reconstruction_id = source_id("reconstruction", reconstruction)
    indexing_id = source_id("indexing", indexing)
    if kind == "wire" and indexing_id is not None:
        message = "A wire reconstruction has no indexing source: give a scan or a reconstruction"
        raise ProblemError(
            422,
            "Invalid source",
            message,
            errors=[{"loc": ["query", "indexing"], "code": "no_source", "message": message}],
        )
    values = _blank(kind, settings)
    clone_of: RunReference | None = None
    from_reconstruction = False  # its input is chosen after the session (``_catalog_or_folder``)
    with Session(engine) as session:
        if scan_number is not None and session.get(Metadata, scan_number) is None:
            raise ScanNotFound(scan_number)
        source: dict[str, Any] = {}
        if indexing_id is not None:
            run = session.get(IndexingRun, indexing_id)
            method_parameters = None
            if run is not None and run.method == kind:
                method_parameters = run.lauego_parameters if kind == "lauego" else run.laue_matching_parameters
            if run is None or method_parameters is None:
                raise _not_found("Indexing run not found", f"No {programs.TITLES[kind]} run I{indexing_id}")
            scan_number = _agree(
                "scan",
                scan_number,
                run.scan_number,
                f"Indexing run I{indexing_id} belongs to scan {run.scan_number}, not {scan_number}",
            )
            reconstruction_id = _agree(
                "reconstruction",
                reconstruction_id,
                run.reconstruction_id,
                f"Indexing run I{indexing_id} indexes reconstruction R{run.reconstruction_id}, not R{reconstruction_id}",
            )
            if kind == "lauego":
                source = _clone_lauego(run, values)
                values["backend"] = _target(run.job, settings)
            else:
                source = _clone_laue_matching(run, values)
            clone_of = RunReference(kind=RunKind.INDEXING, id=indexing_id, label=f"I{indexing_id}")
        if reconstruction_id is not None:
            parent = session.get(ReconstructionRun, reconstruction_id)
            if parent is None or (kind == "wire" and (parent.method != "wire" or parent.wire_parameters is None)):
                raise _not_found("Reconstruction run not found", f"No wire reconstruction R{reconstruction_id}")
            scan_number = _agree(
                "scan",
                scan_number,
                parent.scan_number,
                f"Reconstruction R{reconstruction_id} belongs to scan {parent.scan_number}, not {scan_number}",
            )
            if kind == "wire":
                source = _clone_wire(parent, values)
                values["backend"] = _target(parent.job, settings)
                clone_of = RunReference(
                    kind=RunKind.RECONSTRUCTION, id=reconstruction_id, label=f"R{reconstruction_id}"
                )
            elif clone_of is None:
                source = _reconstruction_input(parent)
                from_reconstruction = True
        if clone_of is None and reconstruction_id is None and scan_number is not None:
            catalog = get_catalog_by_scan_number(session, scan_number)
            if catalog is not None:
                source = {"input_path": catalog.filefolder, "filename_prefixes": _templates(catalog.filename_prefix)}
    if from_reconstruction:  # the rows are read and the session closed: now the file system
        source["input_path"] = _catalog_or_folder(source["input_path"])
    values |= source
    values["scan_number"] = scan_number
    indexes = kind in ("lauego", "laue_matching")
    if indexes:
        values["reconstruction_id"] = reconstruction_id
    values[programs.OUTPUT_PATH_TEMPLATE] = programs.output_path_template(
        kind, settings, scan_number=scan_number, reconstruction_id=reconstruction_id if indexes else None
    )
    return ProgramPrefill(
        kind=kind, schema_id=programs.PROGRAMS[kind].model_fields["schema_id"].default, clone_of=clone_of, values=values
    )
