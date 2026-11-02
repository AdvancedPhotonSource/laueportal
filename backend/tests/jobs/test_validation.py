"""The validation both programs share (``jobs.validation``) and the validate endpoints:
``POST /api/v1/indexing-runs/validate`` and ``POST /api/v1/reconstruction-runs/validate``."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy import text

from laueportal.app import create_app
from laueportal.core.problems import MAX_LOC_PART_LENGTH, MAX_MESSAGE_LENGTH
from laueportal.core.settings import Settings
from laueportal.indexing import validation as indexing_validation
from laueportal.indexing.validation import validate_indexing
from laueportal.jobs import programs
from laueportal.jobs.validation import MAX_ISSUES, Validated
from laueportal.reconstruction import validation as reconstruction_validation
from laueportal.reconstruction.validation import validate_reconstruction
from laueportal.util.srange import srange
from tests.api_support import PORTAL_HEADERS
from tests.validation_support import FRAMES, Place, found, make_place, offering

VALIDATE = {"lauego": validate_indexing, "wire": validate_reconstruction}
PATHS = {"lauego": "/api/v1/indexing-runs/validate", "wire": "/api/v1/reconstruction-runs/validate"}
UNLINKED = (("scan_number",), "warning", "unlinked")
UNLINKED_MESSAGES = {"lauego": indexing_validation.UNLINKED, "wire": reconstruction_validation.UNLINKED}


@pytest.fixture
def place(tmp_path) -> Iterator[Place]:
    yield (place := make_place(tmp_path))
    place.engine.dispose()


def check(
    place: Place, kind: programs.ProgramKind, form: dict[str, Any], settings: Settings | None = None
) -> Validated:
    return VALIDATE[kind](form, settings=settings or place.settings, engine=place.engine)


@pytest.mark.parametrize(("kind", "config"), [("lauego", "beamline"), ("wire", "num_threads")])
def test_the_portals_values_and_unknown_keys_are_errors_and_every_other_field_is_still_checked(place, kind, config):
    form = place.form(kind, geometry_file=os.fspath(place.root / "no.xml"), **{config: 1, "crop": [0, 1]})
    validated = check(place, kind, form)
    assert found(validated) == {
        UNLINKED,
        ((config,), "error", "set_by_portal"),
        (("crop",), "error", "extra_forbidden"),
        (("geometry_file",), "error", "not_found"),
    }
    assert validated.params is None and not validated.ok


def test_a_rule_of_the_whole_form_is_a_general_error_so_a_refused_form_always_says_why(place, monkeypatch):
    """A model rule without a field (a model validator: loc ``[]``, which the form shows as a general error) is
    reported too, here once the field that failed its own rule has its stand-in: a ``Validated`` without parameters
    always holds an error."""
    calls: list[dict[str, Any]] = []

    def build_params(kind, form, settings):
        calls.append(dict(form))
        whole = InitErrorDetails(type=PydanticCustomError("whole_form", "The form disagrees"), loc=(), input=form)
        field = InitErrorDetails(type=PydanticCustomError("box", "Bad box"), loc=("box_size",), input=0)
        raise ValidationError.from_exception_data("LaueGoParamsV1", [field] if len(calls) == 1 else [whole])

    monkeypatch.setattr(programs, "build_params", build_params)
    validated = check(place, "lauego", place.form("lauego"))
    assert [(issue.loc, issue.code, issue.message) for issue in validated.issues] == [
        (["box_size"], "box", "Bad box"),  # the label is the form's (its summary adds it)
        ([], "whole_form", "The form disagrees"),
    ]
    assert len(calls) == 2 and validated.params is None and not validated.ok


def test_the_answer_is_bounded_whatever_the_request(place):
    """As a problem's errors are: at most ``MAX_ISSUES``, each message and loc bounded."""
    form = place.form("lauego", scan_points="1-" + "x" * 5000) | {
        f"name{index}" + "x" * 100: 1 for index in range(2000)
    }
    issues = check(place, "lauego", form).issues
    assert len(issues) == MAX_ISSUES
    assert (issues[-1].loc, issues[-1].code, issues[-1].severity) == ([], "more_issues", "error")
    assert issues[-1].message == f"{2002 - (MAX_ISSUES - 1)} more issues"  # 2000 names, the scan points, unlinked
    assert max(len(issue.message) for issue in issues) <= MAX_MESSAGE_LENGTH
    assert max(len(str(part)) for issue in issues for part in issue.loc) <= MAX_LOC_PART_LENGTH


@pytest.mark.parametrize(
    ("kind", "values", "expected"),
    [
        ("wire", {"output_path_template": "{root}/runs/rec_%d"}, set()),
        ("wire", {"output_path_template": "{root}/runs/new/rec_%d/data"}, set()),  # run creation makes the folder
        ("wire", {"output_path_template": "/tmp/rec_%d"}, {("error", "path_not_allowed")}),
        ("wire", {"output_path_template": "{root}/runs/a%s/rec_%d"}, {("error", "output_template")}),
        ("wire", {"output_path_template": "{root}/a-file/rec_%d"}, {("error", "not_a_folder")}),
        ("wire", {"backend": "aps", "output_path_template": "{root}/analysis/rec_%d/data"}, set()),
        (
            "lauego",
            {"backend": "crux", "output_path_template": "{root}/runs/rec_%d"},
            {("error", "remote_output_folder")},
        ),
        (
            "wire",
            {"backend": "aps", "output_path_template": "{root}/analysis/away_%d"},
            {("error", "remote_output_folder")},
        ),
    ],
)
def test_the_output_folder_is_inside_the_roots_and_a_remote_runs_under_analysis(place, kind, values, expected):
    """``remote.plan.build_plan`` refuses a remote run directory outside ``<files.runs_root>/analysis/`` at submission
    (here the root: ``offering``), also one that a link leads out of the roots once formatted (``away_1``); a template
    the run ID cannot be put in fails the run's creation, and so does a file where its folder is made (``a-file``:
    ``Not a directory``)."""
    (place.root / "a-file").write_text("")
    (place.root / "analysis").mkdir()
    (place.root / "analysis" / "away_1").symlink_to(place.root.parent / "elsewhere")
    form = place.form(kind, **{name: value.format(root=place.root) for name, value in values.items()})
    settings = offering(place, values["backend"]) if "backend" in values else None
    issues = {
        (issue.severity, issue.code) for issue in check(place, kind, form, settings).issues if issue.code != "unlinked"
    }
    assert issues == expected


@pytest.mark.parametrize(
    ("backend", "xml", "code"),
    [
        ("local", "{root}/runs/out.xml", None),
        ("local", "/tmp/out.xml", "path_not_allowed"),
        ("aps", "{root}/runs/analysis/out.xml", "remote_output_xml"),
        ("crux", "out/results.xml", None),
    ],
)
def test_an_absolute_output_xml_is_inside_the_roots_and_refused_on_a_remote_target(place, backend, xml, code):
    """A remote run writes its outputs inside its run directory (M0b question 11), which has no ID yet."""
    output = os.fspath(place.root / "analysis" / "index_%d")
    form = place.form("lauego", backend=backend, output_path_template=output, output_xml=xml.format(root=place.root))
    assert found(check(place, "lauego", form, offering(place, backend))) - {UNLINKED} == (
        set() if code is None else {(("output_xml",), "error", code)}
    )


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_a_run_reading_more_inputs_than_allowed_is_an_error_found_without_expanding_its_ranges(
    place, kind, monkeypatch
):
    """``execution.max_run_inputs`` (1,000,000 by default), counted from the index ranges before input resolution:
    a typed range cannot make validation expand millions of indices (scan points × depths, for each file name)."""
    monkeypatch.setattr(srange, "list", lambda self: pytest.fail("a range was expanded"))
    many = {"scan_points": "1-2000", "depth_range": "1-2000", "filename_prefixes": ["img_%d_%d.tif"]}
    validated = check(place, kind, place.form(kind, **(many if kind == "lauego" else {"scan_points": "1-1000001"})))
    too_many = {(("scan_points",), "error", "too_many_inputs")}
    depths = {(("depth_range",), "error", "too_many_inputs")} if kind == "lauego" else set()
    assert found(validated) == {UNLINKED, *too_many, *depths}

    execution = place.settings.execution.model_copy(update={"max_run_inputs": 1})
    settings = place.settings.model_copy(update={"execution": execution})
    two = VALIDATE[kind](place.form(kind), settings=settings, engine=place.engine)  # the valid form's two frames
    assert found(two) == {UNLINKED, *too_many}


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_a_path_under_a_folder_the_server_cannot_open_is_not_accessible(place, kind):
    """Not a 500: ``Path.is_file`` and ``is_dir`` raise there (Python 3.12), as on a shared folder whose group the
    server is not in."""
    locked = place.root / "locked"
    (locked / "data").mkdir(parents=True)
    (locked / "geometry.xml").write_text("geometry")
    locked.chmod(0)
    try:
        validated = check(
            place, kind, place.form(kind, input_path=f"{locked}/data", geometry_file=f"{locked}/geometry.xml")
        )
    finally:
        locked.chmod(0o700)
    assert {(("input_path",), "error", "unreadable"), (("geometry_file",), "error", "unreadable")} <= found(validated)
    assert "Geometry File is not accessible (Permission denied)" in [issue.message for issue in validated.issues]


@pytest.mark.parametrize(
    ("targets", "backend", "code"),
    [
        ((), "local", None),  # remote execution off: APS is this portal's worker
        ((), "crux", "backend_not_offered"),
        (("aps",), "local", "backend_not_offered"),  # the APS choice is the DM target then
        (("aps",), "aps", None),
        (("crux",), "crux", None),
        (None, "local", "backend_unavailable"),  # remote execution on, but its settings cannot be read
    ],
)
def test_the_target_is_one_this_deployment_offers(place, targets, backend, code):
    """Creation refuses any other (``require_backend``), as submission does: the form's "Run on" choices."""
    if targets is None:
        remote = place.settings.remote.model_copy(update={"enabled": True, "settings_file": place.root / "no.yaml"})
        settings = place.settings.model_copy(update={"remote": remote})
    else:
        settings = offering(place, *targets) if targets else place.settings
    output = os.fspath(place.root / "analysis" / "rec_%d")
    issues = found(check(place, "wire", place.form("wire", backend=backend, output_path_template=output), settings))
    assert issues - {UNLINKED} == (set() if code is None else {(("backend",), "error", code)})


@pytest.mark.parametrize(("backend", "refused"), [("aps", set()), ("crux", {"input_path", "geometry_file"})])
def test_an_alcf_run_reads_only_files_inside_the_experiment_folder(tmp_path, backend, refused):
    """``remote.plan.build_plan`` sends a ``crux`` run's inputs to ALCF from inside the experiment folder only, and
    refuses any other at submission; an ``aps`` run reads them where they are."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "data").mkdir(parents=True)
    for name in FRAMES:
        (elsewhere / "data" / name).write_text("frame")
    (elsewhere / "geometry.xml").write_text("geometry")
    place = make_place(tmp_path, roots={"data": tmp_path / "root", "elsewhere": elsewhere})
    try:
        form = place.form(
            "lauego",
            backend=backend,
            input_path=os.fspath(elsewhere / "data"),
            geometry_file=os.fspath(elsewhere / "geometry.xml"),
            output_path_template=os.fspath(place.root / "analysis" / "index_%d"),
        )
        issues = found(check(place, "lauego", form, offering(place, "aps", "crux")))
    finally:
        place.engine.dispose()
    assert issues - {UNLINKED} == {((name,), "error", "remote_input") for name in refused}


def _tree(place: Place) -> list[str]:
    return sorted(os.fspath(path.relative_to(place.root)) for path in place.root.rglob("*"))


def _runs(place: Place) -> tuple[int, ...]:
    """How many jobs, reconstruction runs and indexing runs there are."""
    with place.engine.connect() as connection:
        return tuple(
            connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("job", "reconstruction_run", "indexing_run")
        )


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_the_length_limits_are_issues_on_their_fields_in_plain_words(place, kind):
    """At most 100 file-name templates, a 200-character Author and 10,000 characters of Notes; live validation places
    each on its field, worded without the field's label."""
    form = place.form(kind, filename_prefixes=["x_%d.h5"] * 101, author="a" * 201, notes="n" * 10_001)
    with TestClient(create_app(place.settings), headers=PORTAL_HEADERS) as client:
        issues = client.post(PATHS[kind], json=form).json()["issues"]

    assert {(tuple(issue["loc"]), issue["message"]) for issue in issues if issue["severity"] == "error"} == {
        (("filename_prefixes",), "Must have at most 100 entries"),
        (("author",), "Must have at most 200 characters"),
        (("notes",), "Must have at most 10,000 characters"),
    }


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_a_filename_template_over_255_characters_is_an_issue_on_the_field(place, kind):
    """Each template has at most 255 characters (a file name's maximum); live validation places the issue on Filename,
    at the template's position, and a 255-character template passes."""
    longest, longer = "x" * 249 + "_%d.h5", "y" * 250 + "_%d.h5"
    form = place.form(kind, filename_prefixes=[longest, longer])
    with TestClient(create_app(place.settings), headers=PORTAL_HEADERS) as client:
        issues = client.post(PATHS[kind], json=form).json()["issues"]

    assert (len(longest), len(longer)) == (255, 256)
    assert [(issue["loc"], issue["message"]) for issue in issues if issue["severity"] == "error"] == [
        (["filename_prefixes", 1], "Must have at most 255 characters")
    ]


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_validate_answers_the_issues_of_one_run_and_creates_nothing(place, kind):
    """A 200 with ``{issues}`` whatever they are (only create endpoints answer 422); no side effects; through the write
    guard like every POST (a 403 without the portal's header); a list of runs is refused."""
    before = (_tree(place), _runs(place))
    with TestClient(create_app(place.settings), headers=PORTAL_HEADERS) as client:
        valid = client.post(PATHS[kind], json=place.form(kind))
        invalid = client.post(PATHS[kind], json=place.form(kind, scan_points="1-3", author=""))
        several = client.post(PATHS[kind], json=[place.form(kind), place.form(kind)])
    with TestClient(create_app(place.settings)) as foreign:
        unguarded = foreign.post(PATHS[kind], json=place.form(kind))

    assert valid.status_code == 200, valid.text
    assert valid.json()["issues"] == [
        {"loc": ["scan_number"], "message": UNLINKED_MESSAGES[kind], "code": "unlinked", "severity": "warning"},
    ]
    assert invalid.status_code == 200
    assert {(tuple(issue["loc"]), issue["severity"], issue["code"]) for issue in invalid.json()["issues"]} == {
        UNLINKED,
        (("author",), "error", "missing"),
        (("input_path",), "error", "no_inputs"),
    }
    assert several.status_code == 422 and several.headers["content-type"] == "application/problem+json"
    assert unguarded.status_code == 403
    assert (_tree(place), _runs(place)) == before
