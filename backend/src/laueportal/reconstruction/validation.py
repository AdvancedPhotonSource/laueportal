"""The validation of a new wire reconstruction run (``POST /api/v1/reconstruction-runs/validate``): the
model's rules, then the cross-field and context checks (``jobs.validation`` has the shared ones, and the issue
format).

The checks (the source is ``scan_number``):

- the source: a scan that exists (an error: its runs refer to it); one without a catalog entry, no scan (unlinked)
  and an input folder that differs from the catalog's are warnings;
- the input: a folder holding files (an error); the geometry file exists; the output folder
  (``jobs.validation.check_output``), the target and an ALCF run's inputs (``check_backend``,
  ``check_transferred_inputs``);
- the depths: start < end (an error on both), a span over 500 µm (a warning on both), a resolution below 0.1 µm (a
  warning) or larger than the span (an error on all three); a frame or wire skip other than 1 (a warning);
- at most ``execution.max_run_inputs`` inputs, counted before anything is expanded; then input resolution
  (``files.inputs.resolve_inputs``, as ``create_reconstruction``) finds every requested file.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from laueportal.core.settings import Settings
from laueportal.files.inputs import resolve_inputs
from laueportal.jobs.validation import (
    Checked,
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
    title,
)
from laueportal.reconstruction.wire.constants import DEFAULT_SKIP
from laueportal.reconstruction.wire.live_params import WireLiveParamsV1
from laueportal.reconstruction.wire.params import WireParamsV1
from laueportal.scans.models import Metadata
from laueportal.scans.service import get_catalog_by_scan_number

UNLINKED = "No source scan: this creates an unlinked wire reconstruction"
LARGE_SPAN_UM = 500.0
"""A depth span above this is a warning."""
SMALL_RESOLUTION_UM = 0.1
"""A depth resolution below this is a warning."""


def validate_reconstruction(form: Mapping[str, Any], *, settings: Settings, engine: Engine) -> Validated:
    """Every issue of one wire run's parameters (``form``: the create form's values). Reads only."""

    issues = Issues()
    checked = check_form("wire", form, settings, issues)
    params = checked.values
    if not isinstance(params, WireParamsV1):
        return Validated(None, finish(issues))
    check_author(issues, checked)
    catalog_folder = _check_source(issues, checked, params, engine)
    kind = check_input_folder(issues, settings, checked)
    if kind is not None and catalog_folder is not None:
        check_source_path(
            issues, f"Catalog entry for Scan Number {params.scan_number}", catalog_folder, params.input_path
        )
    check_file(issues, settings, checked, "geometry_file")
    check_backend(issues, settings, checked)  # first: the remote rules apply to an offered remote target only
    check_output(issues, settings, checked)
    check_transferred_inputs(issues, settings, checked)
    check_depths(issues, checked, params)
    check_skips(issues, checked, params)
    names = ("filename_prefixes", "scan_points")
    counted = checked.usable(*names) and not issues.has_error(*names) and check_input_count(issues, settings, params)
    if counted and kind is not None and checked.usable("input_path") and not issues.has_error("input_path"):
        check_inputs(
            issues,
            lambda: resolve_inputs(
                params.input_path, params.filename_prefixes, params.scan_point_values, append_suffix_wildcard=True
            ),
        )
    return Validated(checked.params, finish(issues))


def _check_source(issues: Issues, checked: Checked, params: WireParamsV1, engine: Engine) -> str | None:
    """The source scan exists (an error); no scan, or no catalog entry, is a warning. Returns the catalog's folder."""

    if not checked.usable("scan_number"):
        return None
    scan_number = params.scan_number
    if scan_number is None:
        issues.warning(["scan_number"], "unlinked", UNLINKED)
        return None
    with Session(engine) as session:
        if session.get(Metadata, scan_number) is None:
            issues.error(["scan_number"], "source_not_found", f"Scan {scan_number} was not found")
            return None
        catalog = get_catalog_by_scan_number(session, scan_number)
        if catalog is None:
            issues.warning(["scan_number"], "no_catalog", f"Catalog entry not found for Scan Number {scan_number}")
            return None
        return catalog.filefolder


def check_skips(issues: Issues, checked: Checked, params: WireParamsV1 | WireLiveParamsV1) -> None:
    """A frame or wire skip other than 1: a warning."""

    for name in ("frame_skip", "wire_skip"):
        if checked.usable(name) and getattr(params, name) != DEFAULT_SKIP:
            issues.warning(
                [name],
                "skip_not_default",
                f"{title(params, name)} differs from the reconstructN_cpu value ({DEFAULT_SKIP}); it changes which "
                "wire positions are paired with each frame",
            )


def check_depths(issues: Issues, checked: Checked, params: WireParamsV1 | WireLiveParamsV1) -> None:
    """The depth rules (µm): start < end, the span, the resolution against the span (a live run's too)."""

    span = None
    if checked.usable("depth_start", "depth_end"):
        span = params.depth_end - params.depth_start
        if span <= 0:
            for name in ("depth_start", "depth_end"):
                issues.error([name], "depth_order", "Depth Start must be less than Depth End")
        elif span > LARGE_SPAN_UM:
            for name in ("depth_start", "depth_end"):
                issues.warning(
                    [name], "depth_span_large", f"Total depth range ({span:g} µm) is large (> {LARGE_SPAN_UM:g} µm)"
                )
    if not checked.usable("depth_resolution"):
        return
    resolution = params.depth_resolution
    if resolution < SMALL_RESOLUTION_UM:
        issues.warning(
            ["depth_resolution"],
            "resolution_small",
            f"Depth Resolution ({resolution:g} µm) is very small (< {SMALL_RESOLUTION_UM:g} µm)",
        )
    if span is not None and resolution > abs(span):
        for name in ("depth_start", "depth_end", "depth_resolution"):
            issues.error(
                [name],
                "resolution_exceeds_span",
                f"Depth Resolution ({resolution:g} µm) must be ≤ the depth range ({abs(span):g} µm)",
            )
