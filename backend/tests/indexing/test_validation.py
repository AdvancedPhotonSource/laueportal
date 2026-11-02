"""The validation of a new LaueGo run (``indexing.validation``): each rule of the create form with its severity
(``RULES``), then the templates' placeholder counts, the number fields, a missing field and a reconstruction's catalog
as the input. Every path is absolute and inside the roots, with no base folder to resolve it against, and the source
is the scan and reconstruction pickers (``scan_number`` and ``reconstruction_id``), one value each."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from laueportal.indexing.validation import validate_indexing
from tests.validation_support import FRAMES, Place, found, make_place

UNLINKED = (("scan_number",), "warning", "unlinked")


@pytest.fixture
def place(tmp_path) -> Iterator[Place]:
    yield (place := make_place(tmp_path))
    place.engine.dispose()


def check(place: Place, **values: Any):
    return validate_indexing(place.form("lauego", **values), settings=place.settings, engine=place.engine)


def test_a_valid_form_only_warns_that_the_run_is_unlinked(place):
    validated = check(place)
    assert found(validated) == {UNLINKED}
    assert validated.ok and validated.params is not None
    assert validated.issues[0].message == "No source scan or reconstruction: this creates an unlinked peak indexing"
    assert found(check(place, scan_number=1)) == set()  # linked, and the folder is the scan's catalog folder
    # a source that does not parse is an error, not the unlinked warning
    assert found(check(place, scan_number="bad-id")) == {(("scan_number",), "error", "int_parsing")}


def test_a_reconstruction_sent_without_its_scan_gives_the_run_its_scan(place):
    """The reconstruction gives the run its scan, so the run is listed on its scan's page."""
    validated = check(place, reconstruction_id=1, scan_number=None)
    assert found(validated) == {(("input_path",), "warning", "source_path_differs")}  # R1's run directory
    assert validated.ok and validated.params is not None
    assert (validated.params.scan_number, validated.params.reconstruction_id) == (1, 1)


RULES = [
    # 1. every path: no white space at either end, absolute and inside the roots; the input folder exists (errors)
    ("relative path", {"input_path": "data"}, ("input_path",), "error", "path_not_allowed"),
    ("relative geometry", {"geometry_file": "geometry.xml"}, ("geometry_file",), "error", "path_not_allowed"),
    ("outside the roots", {"crystal_file": "/etc/hostname"}, ("crystal_file",), "error", "path_not_allowed"),
    ("missing folder", {"input_path": "{data}-x"}, ("input_path",), "error", "not_found"),
    ("padded geometry", {"geometry_file": " {root}/geometry.xml"}, ("geometry_file",), "error", "path_not_allowed"),
    ("padded crystal", {"crystal_file": "{root}/crystal.xml\n"}, ("crystal_file",), "error", "path_not_allowed"),
    ("padded output", {"output_path_template": " {root}/runs/index_%d"}, ("output_path_template",), "error", "path_not_allowed"),
    # 2. the source pickers: a source must parse, exist and agree (errors); none is a warning (unlinked)
    ("source not a number", {"scan_number": "SN1"}, ("scan_number",), "error", "int_parsing"),
    ("scan not found", {"scan_number": 9}, ("scan_number",), "error", "source_not_found"),
    ("R not found", {"reconstruction_id": 9}, ("reconstruction_id",), "error", "source_not_found"),
    ("source mismatch", {"scan_number": 2, "reconstruction_id": 1}, ("reconstruction_id",), "error", "source_mismatch"),
    ("no source", {}, ("scan_number",), "warning", "unlinked"),
    # 3. required fields (errors), Author included; the optional ones may be blank (the valid form's)
    ("required field", {"input_path": None}, ("input_path",), "error", "missing"),
    ("blank required field", {"geometry_file": " "}, ("geometry_file",), "error", "missing"),
    ("blank author", {"author": " "}, ("author",), "error", "missing"),
    # 4. the input: a folder holding files, or a readable reconstruction catalog, scan.h5 (errors); an input other than
    # the source's path (a warning)
    ("empty folder", {"input_path": "{root}/empty"}, ("input_path",), "error", "no_files"),
    ("a file", {"input_path": "{root}/geometry.xml"}, ("input_path",), "error", "not_a_folder"),
    ("scan's path", {"scan_number": 1, "input_path": "{root}/frames"}, ("input_path",), "warning", "source_path_differs"),
    ("R's path", {"reconstruction_id": 1}, ("input_path",), "warning", "source_path_differs"),
    # 5. the file name's %d against the indices, and valid ranges (errors)
    ("one %d and depths", {"depth_range": "1"}, ("filename_prefixes", 0), "error", "placeholders"),
    ("two %d, no depths", {"filename_prefixes": ["img_%d_%d.tif"]}, ("filename_prefixes", 0), "error", "placeholders"),
    ("three %d", {"filename_prefixes": ["a", "i%d_%d_%d"], "depth_range": "1"}, ("filename_prefixes", 1), "error", "placeholders"),
    ("scan range", {"scan_points": "1-abc"}, ("scan_points",), "error", "value_error"),
    ("blank scan range", {"scan_points": " "}, ("scan_points",), "error", "missing"),
    ("depth range", {"depth_range": "x"}, ("depth_range",), "error", "value_error"),
    ("scan range too large", {"scan_points": "1-20000000"}, ("scan_points",), "error", "value_error"),  # srange's limit
    ("depth range overflows", {"depth_range": "0-1" + "0" * 400}, ("depth_range",), "error", "value_error"),
    # 6. the geometry, crystal and mask files exist (errors)
    ("geometry", {"geometry_file": "{root}/no.xml"}, ("geometry_file",), "error", "not_found"),
    ("crystal", {"crystal_file": "{root}/no.xml"}, ("crystal_file",), "error", "not_found"),
    ("crystal folder", {"crystal_file": "{data}"}, ("crystal_file",), "error", "not_a_file"),
    ("mask None", {"mask_file": "None"}, ("mask_file",), "error", "path_not_allowed"),  # not "no mask"
    ("mask", {"mask_file": "{root}/mask.h5"}, ("mask_file",), "error", "not_found"),
    # 7. numeric limits (errors)
    ("threshold", {"threshold": -1}, ("threshold",), "error", "greater_than_equal"),
    ("threshold ratio", {"threshold_ratio": -0.5}, ("threshold_ratio",), "error", "greater_than_equal"),
    ("max R-factor", {"max_rfactor": 2}, ("max_rfactor",), "error", "less_than_equal"),
    ("box size", {"box_size": 0}, ("box_size",), "error", "greater_than"),
    ("max number", {"max_number": 0}, ("max_number",), "error", "greater_than"),
    ("min size", {"min_size": 0}, ("min_size",), "error", "greater_than"),
    ("max peaks", {"max_peaks": 0}, ("max_peaks",), "error", "greater_than"),
    ("min separation", {"min_separation": -1}, ("min_separation",), "error", "greater_than_equal"),
    ("keV calc", {"index_kev_max_calc": 0}, ("index_kev_max_calc",), "error", "greater_than"),
    ("keV test", {"index_kev_max_test": -1}, ("index_kev_max_test",), "error", "greater_than"),
    ("angle tolerance", {"index_angle_tolerance": -0.1}, ("index_angle_tolerance",), "error", "greater_than_equal"),
    ("cone", {"index_cone": 181}, ("index_cone",), "error", "less_than_equal"),
    # an integer SQLite's INTEGER cannot hold (a source lookup, the run's insert) is refused
    ("scan beyond SQLite", {"scan_number": 2**63}, ("scan_number",), "error", "less_than_equal"),
    ("R beyond SQLite", {"reconstruction_id": 10**30}, ("reconstruction_id",), "error", "less_than_equal"),
    ("box size beyond SQLite", {"box_size": 10**30}, ("box_size",), "error", "less_than_equal"),
    ("HKL beyond SQLite", {"index_h": -(2**63) - 1}, ("index_h",), "error", "greater_than_equal"),
    # 8. HKL: three integers (errors)
    ("HKL text", {"index_h": "not-hkl"}, ("index_h",), "error", "int_parsing"),
    ("HKL fraction", {"index_l": 1.5}, ("index_l",), "error", "int_from_float"),
    # 9. Output XML not a reserved name; the output template has exactly one %d (errors)
    ("reserved XML", {"output_xml": "request.json"}, ("output_xml",), "error", "value_error"),
    ("no %d", {"output_path_template": "{root}/runs/out"}, ("output_path_template",), "error", "value_error"),
    ("two %d", {"output_path_template": "{root}/runs/%d_%d"}, ("output_path_template",), "error", "value_error"),
    # 10. input resolution finds every requested file (an error)
    ("missing frame", {"scan_points": "1-3"}, ("input_path",), "error", "no_inputs"),
    # 11. the detector crop is fixed config: a form cannot set it
    ("crop", {"detector_crop_x1": 10}, ("detector_crop_x1",), "error", "set_by_portal"),
]  # fmt: skip
"""The create form's rules, numbered, each comment followed by its cases: ``(case, form values, loc, severity,
code)``; ``{root}`` and ``{data}`` in a value are the place's root and frame folder."""


def _placed(place: Place, value: Any) -> Any:
    return value.format(root=place.root, data=place.data) if isinstance(value, str) else value


@pytest.mark.parametrize(
    ("values", "loc", "severity", "code"), [case[1:] for case in RULES], ids=[case[0] for case in RULES]
)
def test_each_rule_has_its_severity(place, values, loc, severity, code):
    (place.root / "empty").mkdir()
    (place.root / "frames").mkdir()
    for name in FRAMES:
        (place.root / "frames" / name).write_text("frame")
    validated = check(place, **{name: _placed(place, value) for name, value in values.items()})
    assert (loc, severity, code) in found(validated), validated.issues
    assert validated.ok is (severity == "warning")


@pytest.mark.parametrize(
    ("templates", "depth_range"),
    [(["img_1.tif"], None), (["img_%d.tif"], None), (["img_%d_%d.tif"], "0-1"), (["ref.tif", "img_%d.tif"], "")],
)
def test_a_folders_templates_take_no_scan_index_one_or_the_scan_and_depth_indices(place, templates, depth_range):
    """The valid placeholder counts of a file-name template; a blank Depth Range is none."""
    for scan in (1, 2):
        for depth in (0, 1):
            (place.data / f"img_{scan}_{depth}.tif").write_text("frame")
    (place.data / "ref.tif").write_text("frame")
    assert found(check(place, filename_prefixes=templates, depth_range=depth_range)) == {UNLINKED}


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("box_size", "3", None),  # a number or its text
        ("box_size", 7, None),
        ("box_size", "", "missing"),  # required
        ("box_size", None, "missing"),
        ("box_size", "3.5", "int_parsing"),
        ("box_size", "abc", "int_parsing"),
        ("min_size", "3.5", None),
        ("min_size", 4, None),
        ("min_size", "1.13", None),
        ("min_size", "", "missing"),
        ("min_size", "abc", "float_parsing"),
        ("min_size", "-1", "greater_than"),
        ("min_size", "nan", "finite_number"),
        ("min_size", "inf", "finite_number"),
        ("max_number", None, None),  # both spot limits may be blank
        ("max_peaks", None, "missing"),  # but Max spots to index is required
        ("threshold", None, None),  # optional fields
        ("threshold_ratio", None, None),
        ("depth_range", "", None),
    ],
)
def test_a_number_is_a_number_or_its_text_and_only_the_optional_fields_may_be_blank(place, name, value, code):
    """Required, optional and converted values."""
    issues = found(check(place, **{name: value})) - {UNLINKED}
    assert issues == (set() if code is None else {((name,), "error", code)})


def test_a_missing_field_is_required_and_every_field_is_still_checked(place):
    """The form says "Required" under the field, whose label the form's summary adds, and every field is checked at
    once: a field that fails its own rule leaves the others checked (``jobs.validation.check_form``'s stand-ins)."""
    form = place.form("lauego", box_size=0, crystal_file=os.fspath(place.root / "no.xml"))
    del form["input_path"]
    validated = validate_indexing(form, settings=place.settings, engine=place.engine)
    assert found(validated) == {
        UNLINKED,
        (("input_path",), "error", "missing"),
        (("box_size",), "error", "greater_than"),
        (("crystal_file",), "error", "not_found"),
    }
    assert validated.params is None and not validated.ok
    assert {(tuple(issue.loc), issue.message) for issue in validated.issues} >= {(("input_path",), "Required")}
    for blank in ("", "  ", None):  # left blank: "Required", whatever the type's own complaint
        blanks = check(place, input_path=blank, box_size=blank, filename_prefixes=[blank] if blank else [])
        assert {(tuple(issue.loc), issue.code, issue.message) for issue in blanks.issues} >= {
            (("input_path",), "missing", "Required"),
            (("box_size",), "missing", "Required"),
            (("filename_prefixes",), "missing", "Required"),  # an empty Filename, or one of blanks
        }


@pytest.fixture(scope="module")
def scan_file(tmp_path_factory) -> Path:
    """A reconstruction's catalog: point ``wire_1`` complete (11 depths), ``wire_2`` failed."""
    from lauelab.reconstruct import reconstruct_scan

    from tests.wire_support import GEOMETRY, write_wire_scan

    directory = tmp_path_factory.mktemp("rec")
    inputs = [write_wire_scan(directory / "wire_1.h5"), directory / "wire_2.h5"]
    result = reconstruct_scan(
        inputs, directory / "reconstruction", geometry=GEOMETRY, detector=0, point_ids=["wire_1", "wire_2"],
        depth_range=(-25.0, 25.0), resolution=5.0, threads_per_worker=1,
    )  # fmt: skip
    return Path(result.path)


@pytest.fixture
def catalog_place(tmp_path, scan_file) -> Iterator[Place]:
    yield (place := make_place(tmp_path, roots={"data": tmp_path / "root", "rec": scan_file.parent.parent}))
    place.engine.dispose()


def check_catalog(place: Place, scan_file: Path, **values: Any):
    values = {"input_path": os.fspath(scan_file), "filename_prefixes": ["wire_%d"], "scan_points": "1"} | values
    return check(place, **{"depth_range": "0-3", **values})


@pytest.mark.slow
def test_a_reconstructions_scan_catalog_is_an_input_that_selects_depths(catalog_place, scan_file):
    """A scan catalog is a valid data path, and its selection problems are reported before submission: a failed point, a
    depth beyond the stack, and a template with two %d (or none) are errors."""
    assert found(check_catalog(catalog_place, scan_file)) == {UNLINKED}
    assert found(check_catalog(catalog_place, scan_file, depth_range=None)) == {UNLINKED}  # every depth

    failed = check_catalog(catalog_place, scan_file, scan_points="1-2")
    assert (("input_path",), "error", "no_inputs") in found(failed)
    assert "point 'wire_2' is failed" in str(failed.issues)
    beyond = check_catalog(catalog_place, scan_file, depth_range="0-11")
    assert "requested 11" in str(beyond.issues)
    for templates in (["wire_%d_%d"], ["wire_1"]):
        issues = found(check_catalog(catalog_place, scan_file, filename_prefixes=templates))
        assert issues == {UNLINKED, (("filename_prefixes", 0), "error", "placeholders")}


@pytest.mark.slow
def test_a_linked_reconstruction_is_compared_with_its_scan_catalog(catalog_place, scan_file):
    differs = (("input_path",), "warning", "source_path_differs")
    assert differs in found(check_catalog(catalog_place, scan_file, reconstruction_id=1))  # R1 is elsewhere
    with catalog_place.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE reconstruction_run SET output_path = ?", (os.fspath(scan_file.parent.parent),)
        )
    assert found(check_catalog(catalog_place, scan_file, reconstruction_id=1)) == set()
