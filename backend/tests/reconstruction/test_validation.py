"""The validation of a new wire reconstruction run (``reconstruction.validation``): each rule of the create form with
its severity (``RULES``), then the depth rules' messages. Every path is absolute and inside the roots, with no base
folder to resolve it against, and the source is the scan picker (``scan_number``), one value."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from laueportal.reconstruction.validation import validate_reconstruction
from tests.validation_support import Place, found, make_place

UNLINKED = (("scan_number",), "warning", "unlinked")


@pytest.fixture
def place(tmp_path) -> Iterator[Place]:
    yield (place := make_place(tmp_path))
    place.engine.dispose()


def check(place: Place, **values: Any):
    return validate_reconstruction(place.form("wire", **values), settings=place.settings, engine=place.engine)


def test_a_valid_form_only_warns_that_the_run_is_unlinked(place):
    validated = check(place)
    assert found(validated) == {UNLINKED} and validated.ok
    assert validated.issues[0].message == "No source scan: this creates an unlinked wire reconstruction"
    assert found(check(place, scan_number=1)) == set()  # linked, and the folder is the scan's catalog folder


RULES = [
    # 1. every path: no white space at either end, absolute and inside the roots (errors)
    ("relative path", {"input_path": "data"}, ("input_path",), "error", "path_not_allowed"),
    ("outside the roots", {"geometry_file": "/etc/hostname"}, ("geometry_file",), "error", "path_not_allowed"),
    ("padded geometry", {"geometry_file": "{root}/geometry.xml "}, ("geometry_file",), "error", "path_not_allowed"),
    ("padded output", {"output_path_template": " {root}/runs/rec_%d"}, ("output_path_template",), "error", "path_not_allowed"),
    # 2. the scan picker: a scan must parse and exist (errors); none, or one without a catalog entry, is a warning
    ("source not a number", {"scan_number": "SN1"}, ("scan_number",), "error", "int_parsing"),
    ("no source", {}, ("scan_number",), "warning", "unlinked"),
    ("scan not found", {"scan_number": 9}, ("scan_number",), "error", "source_not_found"),  # its runs refer to it
    ("no catalog entry", {"scan_number": 2}, ("scan_number",), "warning", "no_catalog"),
    # 3. required fields (errors), Author included
    ("required field", {"geometry_file": None}, ("geometry_file",), "error", "missing"),
    ("blank required field", {"depth_resolution": ""}, ("depth_resolution",), "error", "missing"),
    ("blank author", {"author": ""}, ("author",), "error", "missing"),
    # 4. the data folder exists and has files (errors); a different catalog folder (a warning)
    ("missing folder", {"input_path": "{data}-x"}, ("input_path",), "error", "not_found"),
    ("empty folder", {"input_path": "{root}/empty"}, ("input_path",), "error", "no_files"),
    ("a file", {"input_path": "{root}/geometry.xml"}, ("input_path",), "error", "not_a_folder"),
    ("catalog folder", {"scan_number": 1, "input_path": "{root}/frames"}, ("input_path",), "warning", "source_path_differs"),
    # 5. at most one %d per file name (an error)
    ("two %d", {"filename_prefixes": ["img_%d_%d.tif"]}, ("filename_prefixes",), "error", "value_error"),
    # 6. the scan indices are a valid range (an error)
    ("scan range", {"scan_points": "1-x"}, ("scan_points",), "error", "value_error"),
    ("scan range too large", {"scan_points": "1-20000000"}, ("scan_points",), "error", "value_error"),  # srange's limit
    # 7. the output folder: inside the roots and taking the run ID (errors), its cases in tests/jobs/test_validation.py;
    # whether it exists is not checked, since run creation makes it (jobs.validation.check_output)
    # 8. the geometry file exists (an error)
    ("geometry", {"geometry_file": "{root}/no.xml"}, ("geometry_file",), "error", "not_found"),
    # 9. depths: start < end (errors on both), the span (warnings), the resolution (> 0, small, ≤ span)
    ("start = end", {"depth_start": 5, "depth_end": 5}, ("depth_end",), "error", "depth_order"),
    ("start > end", {"depth_start": 6, "depth_end": 5}, ("depth_start",), "error", "depth_order"),
    ("large span", {"depth_start": -300, "depth_end": 300}, ("depth_end",), "warning", "depth_span_large"),
    ("resolution 0", {"depth_resolution": 0}, ("depth_resolution",), "error", "greater_than"),
    ("small resolution", {"depth_resolution": 0.05}, ("depth_resolution",), "warning", "resolution_small"),
    ("resolution > span", {"depth_resolution": 201}, ("depth_start",), "error", "resolution_exceeds_span"),
    # 10. the intensity percentile is in (0, 100] (errors)
    ("percentile 0", {"percent_brightest": 0}, ("percent_brightest",), "error", "greater_than"),
    ("percentile 101", {"percent_brightest": 101}, ("percent_brightest",), "error", "less_than_equal"),
    # 11. the skips are ≥ 0 (errors), and a warning when not 1
    ("negative skip", {"frame_skip": -1}, ("frame_skip",), "error", "greater_than_equal"),
    ("skip not 1", {"wire_skip": 2}, ("wire_skip",), "warning", "skip_not_default"),
    ("skip 0", {"frame_skip": 0}, ("frame_skip",), "warning", "skip_not_default"),
    ("skip beyond SQLite", {"wire_skip": 10**30}, ("wire_skip",), "error", "less_than_equal"),  # its INTEGER
    ("scan beyond SQLite", {"scan_number": 2**63}, ("scan_number",), "error", "less_than_equal"),
    # 12. input resolution finds every requested file (an error)
    ("missing file", {"scan_points": "1-3"}, ("input_path",), "error", "no_inputs"),
]  # fmt: skip
"""The create form's wire rules, numbered, each comment followed by its cases: ``(case, form values, loc, severity,
code)``; ``{root}`` and ``{data}`` in a value are the place's root and frame folder."""


@pytest.mark.parametrize(
    ("values", "loc", "severity", "code"), [case[1:] for case in RULES], ids=[case[0] for case in RULES]
)
def test_each_rule_has_its_severity(place, values, loc, severity, code):
    (place.root / "empty").mkdir()
    (place.root / "frames").mkdir()
    for index in (1, 2):
        (place.root / "frames" / f"img_{index}.tif").write_text("frame")
    placed = {
        name: value.format(root=place.root, data=place.data) if isinstance(value, str) else value
        for name, value in values.items()
    }
    validated = check(place, **placed)
    assert (loc, severity, code) in found(validated), validated.issues
    assert validated.ok is (severity == "warning")


def test_the_depth_rules_mark_each_field_they_concern_and_say_the_values(place):
    """A depth problem is put on every field it concerns, and says the values."""
    order = check(place, depth_start=6, depth_end=5, depth_resolution=0.05)
    assert {(tuple(issue.loc), issue.code, issue.message) for issue in order.issues} - {
        (UNLINKED[0], "unlinked", order.issues[0].message)
    } == {
        (("depth_start",), "depth_order", "Depth Start must be less than Depth End"),
        (("depth_end",), "depth_order", "Depth Start must be less than Depth End"),
        (("depth_resolution",), "resolution_small", "Depth Resolution (0.05 µm) is very small (< 0.1 µm)"),
    }
    span = check(place, depth_start=-300, depth_end=300, depth_resolution=700)
    assert {(issue.loc[0], issue.severity, issue.message) for issue in span.issues if issue.loc != ["scan_number"]} == {
        ("depth_start", "warning", "Total depth range (600 µm) is large (> 500 µm)"),
        ("depth_end", "warning", "Total depth range (600 µm) is large (> 500 µm)"),
        *(
            (name, "error", "Depth Resolution (700 µm) must be ≤ the depth range (600 µm)")
            for name in ("depth_start", "depth_end", "depth_resolution")
        ),
    }
