"""The validation of a new LaueGo or LaueMatching indexing run (``POST /api/v1/indexing-runs/validate``,
``POST /api/v1/indexing-runs/laue-matching/validate``): the model's rules, then the cross-field and context checks (``jobs.validation`` has the shared ones, and the issue format).

The checks (the source is ``scan_number`` and ``reconstruction_id``):

- the source: a scan that exists (its runs refer to it), a reconstruction that exists and belongs to that scan
  (errors), and a reconstruction sent without its scan gives the run its scan; neither is a warning
  (an unlinked run); an input that differs from the source's path is a warning (the
  reconstruction's ``scan.h5``, or its run directory for a folder input; otherwise the scan's catalog folder);
- the input: a folder holding files, or a readable reconstruction catalog (``scan.h5``);
- the file-name templates against the indices: for a folder, one ``%d`` takes the scan index and a second the depth
  index, so one ``%d`` refuses a Depth Range and two need one, and three are refused; for a catalog, each template
  has exactly one ``%d`` (the point's scan index) and Depth Range selects the depths;
- the geometry, crystal and mask files exist; the output folder (``jobs.validation.check_output``), the target
  and an ALCF run's inputs (``check_backend``, ``check_transferred_inputs``); on a remote target, the Output XML is
  a name inside the run directory (an absolute one is refused);
- at most ``execution.max_run_inputs`` inputs, counted before anything is expanded; then input resolution
  (``files.inputs.resolve_request_inputs``, as ``create_indexing``) finds every requested file or frame;
- for LaueMatching: the energy range, the data folder and the forward cache (``indexing.laue_matching.data``), and a
  warning when a reconstruction's depths reach beyond ``DEEP_DEPTH_UM``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.core.settings import Settings
from laueportal.core.text import quoted
from laueportal.files.inputs import resolve_request_inputs
from laueportal.indexing.laue_matching.data import DEEP_DEPTH_UM, check_laue_matching
from laueportal.indexing.laue_matching.params import LaueMatchingParamsV1
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.jobs.rundir import reconstruction_catalog_path
from laueportal.jobs.validation import (
    Checked,
    InputKind,
    Issues,
    Validated,
    check_author,
    check_backend,
    check_file,
    check_form,
    check_input_count,
    check_input_folder,
    check_inputs,
    check_output,
    check_source_path,
    check_transferred_inputs,
    finish,
    inside_roots,
    remote_target,
    title,
)
from laueportal.reconstruction.models import ReconstructionRun
from laueportal.scans.models import Metadata
from laueportal.scans.service import get_catalog_by_scan_number

UNLINKED = "No source scan or reconstruction: this creates an unlinked peak indexing"


def validate_indexing(form: Mapping[str, Any], *, settings: Settings, engine: Engine) -> Validated:
    """Every issue of one LaueGo run's parameters (``form``: the create form's values). Reads only."""

    return _validate("lauego", form, settings=settings, engine=engine)


def validate_laue_matching(form: Mapping[str, Any], *, settings: Settings, engine: Engine) -> Validated:
    """Every issue of one LaueMatching run's parameters (``form``: the create form's values). Reads only."""

    return _validate("laue_matching", form, settings=settings, engine=engine)


def _validate(
    program: Literal["lauego", "laue_matching"], form: Mapping[str, Any], *, settings: Settings, engine: Engine
) -> Validated:
    issues = Issues()
    checked = check_form(program, form, settings, issues)
    params = checked.values
    if not isinstance(params, (LaueGoParamsV1, LaueMatchingParamsV1)):
        return Validated(None, finish(issues))
    check_author(issues, checked)
    source = _check_source(issues, checked, params, engine)
    run_params = checked.params
    if source is not None and source.scan_number is not None and params.scan_number is None:
        # a reconstruction sent without its scan: the run takes the reconstruction's
        params = params.with_values(scan_number=source.scan_number)
        run_params = params if run_params is not None else None
    kind = check_input_folder(issues, settings, checked, is_catalog=_is_catalog)
    if kind is not None and source is not None:
        check_source_path(
            issues,
            source.label,
            source.catalog if kind == "catalog" and source.catalog else source.path,
            params.input_path,
        )
    if kind is not None:
        _check_placeholders(issues, checked, params, kind)
    for name in ("geometry_file", "crystal_file", "mask_file"):
        check_file(issues, settings, checked, name)
    if isinstance(params, LaueMatchingParamsV1):
        check_file(issues, settings, checked, "background_file")
        check_laue_matching(issues, settings, checked, params)
        _check_depths(issues, checked, params, engine)
    check_backend(issues, settings, checked)  # first: the remote rules apply to an offered remote target only
    check_output(issues, settings, checked)
    check_transferred_inputs(issues, settings, checked)
    if isinstance(params, LaueGoParamsV1):
        _check_output_xml(issues, settings, checked, params)
    names = ("filename_prefixes", "scan_points", "depth_range")
    counted = checked.usable(*names) and not issues.has_error(*names) and check_input_count(issues, settings, params)
    if counted and kind is not None and checked.usable("input_path") and not issues.has_error("input_path"):
        check_inputs(
            issues,
            lambda: resolve_request_inputs(
                params.input_path, params.filename_prefixes, params.scan_point_values, depth_values=params.depth_values
            ),
        )
    return Validated(run_params, finish(issues))


def _check_depths(issues: Issues, checked: Checked, params: LaueMatchingParamsV1, engine: Engine) -> None:
    """A warning when the source reconstruction's depths reach far enough for the depth term LaueMatching lacks to
    matter. Frames read from files are checked as they are read (``indexing.laue_matching.compute``)."""

    if not checked.usable("reconstruction_id") or params.reconstruction_id is None:
        return
    with Session(engine) as session:
        reconstruction = session.get(ReconstructionRun, params.reconstruction_id)
        parameters = reconstruction.wire_parameters if reconstruction is not None else None
        if parameters is None or max(abs(parameters.depth_start), abs(parameters.depth_end)) <= DEEP_DEPTH_UM:
            return
    issues.warning(
        ["reconstruction_id"],
        "deep_frames",
        f"This reconstruction reaches beyond {DEEP_DEPTH_UM:g} um: LaueMatching uses the geometry at depth 0, so spots "
        "of frames that deep are displaced by about half a pixel or more",
    )


def _is_catalog(path: os.PathLike[str]) -> bool:
    from laueportal.science.recon import is_reconstruction_scan  # lauelab, loaded only for a file input

    return is_reconstruction_scan(path)


@dataclass(frozen=True)
class _Source:
    """The run's source, for the source-path warning."""

    label: str
    path: str
    catalog: str | None = None
    """A reconstruction's catalog (``scan.h5``)."""
    scan_number: int | None = None
    """A reconstruction's scan."""


def _check_source(
    issues: Issues, checked: Checked, params: LaueGoParamsV1 | LaueMatchingParamsV1, engine: Engine
) -> _Source | None:
    """The source scan and reconstruction exist and agree (errors); without either, a warning (unlinked). Returns
    the source's label and path (and a reconstruction's catalog and scan)."""

    if not checked.usable("scan_number", "reconstruction_id"):
        return None
    scan_number, reconstruction_id = params.scan_number, params.reconstruction_id
    if scan_number is None and reconstruction_id is None:
        issues.warning(["scan_number"], "unlinked", UNLINKED)
        return None
    with Session(engine) as session:
        if scan_number is not None and session.get(Metadata, scan_number) is None:
            issues.error(["scan_number"], "source_not_found", f"Scan {scan_number} was not found")
            return None
        if reconstruction_id is not None:
            reconstruction = session.get(ReconstructionRun, reconstruction_id)
            if reconstruction is None:
                issues.error(
                    ["reconstruction_id"], "source_not_found", f"Reconstruction R{reconstruction_id} was not found"
                )
                return None
            if scan_number is not None and reconstruction.scan_number not in (None, scan_number):
                issues.error(
                    ["reconstruction_id"],
                    "source_mismatch",
                    f"Reconstruction R{reconstruction_id} belongs to scan {reconstruction.scan_number}, "
                    f"not {scan_number}",
                )
                return None
            output = reconstruction.output_path
            catalog = reconstruction_catalog_path(output) if output else None
            return _Source(f"R{reconstruction_id}", output or "", catalog, reconstruction.scan_number)
        catalog_row = get_catalog_by_scan_number(session, scan_number) if scan_number is not None else None
        return _Source(f"SN{scan_number}", catalog_row.filefolder) if catalog_row is not None else None


def _check_placeholders(
    issues: Issues, checked: Checked, params: LaueGoParamsV1 | LaueMatchingParamsV1, kind: InputKind
) -> None:
    """The ``%d`` placeholders of each file-name template against Scan Indices and Depth Indices (errors)."""

    if not checked.usable("filename_prefixes", "depth_range"):
        return
    has_depths = params.depth_range is not None
    depth_title = title(params, "depth_range")
    for index, template in enumerate(params.filename_prefixes):
        count = template.count("%d")
        shown = quoted(template)
        if kind == "catalog":
            if count != 1:
                problem = "has more than one %d" if count > 1 else "has no %d"
                issues.error(
                    ["filename_prefixes", index],
                    "placeholders",
                    f"Filename {shown} {problem}: in a reconstruction's scan.h5 one %d takes the scan index, and "
                    f"{depth_title} selects the depths",
                )
        elif count > 2:
            issues.error(["filename_prefixes", index], "placeholders", f"Filename {shown} has more than two %d")
        elif count == 1 and has_depths:
            issues.error(
                ["filename_prefixes", index],
                "placeholders",
                f"Filename {shown} has one %d, the scan index, but {depth_title} is given: add a second %d for the "
                f"depth index, or clear {depth_title}",
            )
        elif count == 2 and not has_depths:
            issues.error(
                ["filename_prefixes", index],
                "placeholders",
                f"Filename {shown} has two %d (the scan and depth indices): give {depth_title}",
            )


def _check_output_xml(issues: Issues, settings: Settings, checked: Checked, params: LaueGoParamsV1) -> None:
    """An absolute Output XML is inside the roots; on a remote target it is refused, since a remote run writes its
    outputs inside its run directory, which has no ID yet (``remote.plan.build_plan``)."""

    xml = params.output_xml
    if not checked.usable("output_xml") or xml is None or not os.path.isabs(xml):
        return
    label = title(params, "output_xml")
    if remote_target(issues, checked):
        issues.error(
            ["output_xml"],
            "remote_output_xml",
            f"A remote run writes its {label} inside its run directory: enter a name relative to it",
        )
        return
    inside_roots(issues, settings, ["output_xml"], label, xml)
