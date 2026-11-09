"""The parameter models ``lauego/v1`` and ``wire/v1`` and ``GET /api/v1/programs``.

Their ``request.json`` keys are the committed goldens' (``tests/fixtures/remote_goldens/``); their form defaults,
normalizations, form rules and layout are pinned here, field by field.
"""

from __future__ import annotations

import json
import os
import re
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from lauelab.indexing import PeakParams
from pydantic import ValidationError

from laueportal.app import create_app
from laueportal.core.settings import Settings
from laueportal.indexing.laue_matching.params import LaueMatchingParamsV1
from laueportal.indexing.lauego import compute as lauego
from laueportal.indexing.lauego.params import LaueGoParamsV1
from laueportal.jobs import programs
from laueportal.jobs.backends import BACKENDS
from laueportal.jobs.contract import RunRequest
from laueportal.jobs.rundir import read_request, request_document, write_request
from laueportal.reconstruction.wire import compute as wire
from laueportal.reconstruction.wire.params import WireParamsV1
from tests.remote_support import GOLDENS, Layout, bdp_settings
from tests.settings_support import make_settings

SOURCES: dict[programs.ProgramKind, dict[str, Any]] = {
    "lauego": {
        "scan_number": 1,
        "reconstruction_id": None,
        "input_path": "/data/scan_1",
        "filename_prefixes": ["frame_%d.h5"],
        "scan_points": "3,1-2",
        "geometry_file": "/config/geometry.xml",
        "crystal_file": "/config/Ni.xml",
    },
    "wire": {
        "scan_number": 1,
        "input_path": "/data/scan_1",
        "filename_prefixes": ["wire_%d.h5"],
        "scan_points": "1-3",
        "geometry_file": "/config/geometry.xml",
    },
}
"""What a form takes from its source (prefill), besides the configured defaults."""


def _form(kind: programs.ProgramKind, settings: Settings, **values: Any) -> dict[str, Any]:
    """A create form as it starts: the configured defaults, the source's values, then the user's ``values``."""
    return {**programs.form_defaults(kind, settings), **SOURCES[kind], **values}


@pytest.mark.parametrize(("kind", "golden"), [("lauego", "aps-lauego"), ("wire", "aps-wire")])
def test_each_model_writes_exactly_the_goldens_request_keys_plus_schema_id(tmp_path, kind, golden):
    v1_keys = set(json.loads((GOLDENS / golden / "request.json").read_text(encoding="utf-8"))["request"])
    settings = make_settings(tmp_path)
    params = programs.build_params(kind, _form(kind, settings), settings)

    assert "backend" in v1_keys  # a golden key
    assert set(request_document(params)) == v1_keys | {"schema_id"}
    assert request_document(params)["schema_id"] == f"{kind}/v1"


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_a_blank_author_notes_or_depth_is_written_as_null(tmp_path, kind):
    """A blank value is written as null, and the shipped defaults of author, notes and depth are blank."""
    settings = make_settings(tmp_path)
    blank = ("author", "notes", "depth") if kind == "lauego" else ("author", "notes")

    for form in (_form(kind, settings), _form(kind, settings, **dict.fromkeys(blank, "  "))):
        written = request_document(programs.build_params(kind, form, settings))
        assert {name: written[name] for name in blank} == dict.fromkeys(blank)
    typed = {"author": " Ada ", "notes": "line 1\n line 2 "}
    written = request_document(programs.build_params(kind, _form(kind, settings, **typed), settings))
    assert {name: written[name] for name in typed} == typed  # other text as typed


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        (" ", None),
        ("-49.0", "-49.0"),  # as typed
        (" 12.5 ", "12.5"),
        ("1e2", "1e2"),
        (-49, "-49"),  # a JSON number, as its text
        (2.5, "2.5"),
        ("deep", ValueError),
        ("nan", ValueError),  # refused (a migrated run's: each frame's own depth)
        ("-inf", ValueError),
        (float("inf"), ValueError),
        (True, ValueError),
    ],
)
def test_depth_is_blank_or_a_finite_number_stored_as_typed(tmp_path, value, expected):
    """Text that is not a number is refused: it would create a run whose every frame fails."""
    settings = make_settings(tmp_path)
    form = _form("lauego", settings, depth=value)
    if expected is ValueError:
        with pytest.raises(ValidationError, match="Depth must be a number in µm, or empty for automatic"):
            programs.build_params("lauego", form, settings)
    else:
        assert programs.build_params("lauego", form, settings).depth == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "output.xml"), ("", "output.xml"), ("  ", "output.xml"), (" merged.xml ", "merged.xml")],
)
def test_a_blank_output_xml_is_written_as_output_xml(tmp_path, value, expected):
    """A blank Output XML is the default name, ``output.xml``."""
    settings = make_settings(tmp_path)
    params = programs.build_params("lauego", _form("lauego", settings, output_xml=value), settings)
    assert request_document(params)["output_xml"] == expected


def test_a_request_json_written_from_the_configured_defaults_reaches_lauelab_unchanged(tmp_path: Path) -> None:
    """Both models, from the settings' defaults, through a written and read ``request.json``, to the compute
    functions' settings (which read the keys by name and ignore ``schema_id``)."""
    settings = make_settings(
        tmp_path,
        indexing={"defaults": {"threshold_ratio": 4.5, "peak_shape": "Gaussian", "depth": -49.0}},
        reconstruction={"defaults": {"wire_edges": "both", "frame_skip": 2}},
    )

    def read_back(kind: programs.ProgramKind) -> RunRequest:
        params = programs.build_params(kind, _form(kind, settings), settings)
        path = tmp_path / f"{kind}.json"
        write_request(path, {"kind": kind, "request": request_document(params), "output": {}})
        document = read_request(path)
        assert document["request"]["schema_id"] == f"{kind}/v1"
        return RunRequest(1, kind, "R1", os.fspath(tmp_path), "", document, n_inputs=3, workers=1, max_in_flight=None)

    computed = lauego.settings_from_request(read_back("lauego").parameters)
    assert computed.peak_params == PeakParams(
        boxsize=18,
        max_rfactor=0.5,
        min_size=3.0,
        min_separation=20,
        threshold=None,
        threshold_ratio=4.5,  # not truncated
        peak_shape="Gaussian",
        max_peaks=300,
        smooth=False,
    )
    assert (computed.index_params.max_data, computed.index_params.hkl_prefer) == (200, (0, 0, 1))
    assert (computed.index_params.kev_max_calc, computed.index_params.kev_max_test) == (17.2, 35.0)
    assert (computed.index_params.angle_tolerance_deg, computed.index_params.cone_deg) == (0.1, 72.0)
    assert (computed.geometry_file, computed.crystal_file, computed.mask_file) == (
        "/config/geometry.xml",
        "/config/Ni.xml",
        None,
    )
    assert (computed.depth_override, computed.cosmic_filter) == (-49.0, False)

    wire_settings = wire.settings_from_request(read_back("wire").parameters)
    assert (wire_settings.wire_edge, wire_settings.depth_range, wire_settings.resolution) == (
        "both",
        (-50.0, 150.0),
        1.0,
    )
    assert (wire_settings.percent_brightest, wire_settings.frame_skip, wire_settings.wire_skip) == (100.0, 2, 1)
    assert (wire_settings.threads_per_worker, wire_settings.memory_limit_mb) == (1, 512)  # [execution]


class Refused(str):
    """The model refuses the value, with this message."""


NORMALIZED_VALUES: list[tuple[programs.ProgramKind, str, Any, Any]] = [
    # the paths: os.path.normpath (a path object as its text); an empty one, or ".", is refused
    ("lauego", "input_path", "/data//scan_1/./raw/../", "/data/scan_1"),
    ("wire", "input_path", Path("/data/scan_1/"), "/data/scan_1"),
    ("lauego", "input_path", "", Refused("Input path is required")),
    ("wire", "input_path", ".", Refused("Input path is required")),
    ("lauego", "output_path_template", "/runs//analysis/./index_%d/", "/runs/analysis/index_%d"),
    ("wire", "output_path_template", "/runs/analysis/rec", Refused("exactly one %d")),
    ("lauego", "geometry_file", Path("/config//geometry.xml"), "/config/geometry.xml"),
    ("wire", "geometry_file", "", Refused("Geometry file is required")),
    ("lauego", "crystal_file", "/config/./Ni.xml", "/config/Ni.xml"),
    ("lauego", "crystal_file", ".", Refused("Structures file is required")),
    # the index ranges: srange's text; at least one scan point; an empty depth range is none
    ("lauego", "scan_points", "3,1-2", "1-3"),
    ("wire", "scan_points", "5, 1-3", "1-3,5"),
    ("lauego", "scan_points", "", Refused("scan points must contain at least one value")),
    ("wire", "scan_points", "1-x", Refused("Invalid scan points")),
    ("lauego", "depth_range", "2,0-1", "0-2"),
    ("lauego", "depth_range", " ", None),
    # the file-name templates: stripped, blank ones dropped, at least one
    ("lauego", "filename_prefixes", [" Si_%d.h5 ", "", "  "], ("Si_%d.h5",)),
    ("wire", "filename_prefixes", ["", " "], Refused("At least one filename template is required")),
    # the host name: stripped, required
    ("lauego", "computer_name", " host-a\n", "host-a"),
    ("wire", "computer_name", "  ", Refused("Computer name is required")),
    # mask_file: stripped, blank is none
    ("lauego", "mask_file", " /config/mask.h5 ", "/config/mask.h5"),
    ("lauego", "mask_file", "  ", None),
]
"""The models' normalizations, value by value."""


@pytest.mark.parametrize(("kind", "name", "value", "expected"), NORMALIZED_VALUES)
def test_the_models_normalize_each_value(tmp_path, kind, name, value, expected):
    """Through ``with_values``, which checks and normalizes a copy again (``model_copy`` would not)."""
    settings = make_settings(tmp_path)
    params = programs.build_params(kind, _form(kind, settings), settings)
    if isinstance(expected, Refused):
        with pytest.raises(ValidationError, match=re.escape(expected)):
            params.with_values(**{name: value})
    else:
        assert getattr(params.with_values(**{name: value}), name) == expected


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_the_fixed_settings_come_from_config_and_a_client_cannot_set_them(tmp_path, kind):
    settings = make_settings(
        tmp_path,
        indexing={
            "fixed": {
                "detector_crop": [8, 2039, 16, 2031],
                "energy_unit": "eV",
                "exposure_unit": "ms",
                "reciprocal_lattice_unit": "1/Å",
                "lattice_parameters_unit": "Å",
                "beamline": "34ID-X",
            }
        },
        execution={"num_threads": 7, "memory_limit_mb": 4096, "verbose": 2},
    )
    started = datetime.now(UTC)
    params = programs.build_params(kind, _form(kind, settings), settings)

    expected = {
        "lauego": {
            "detector_crop_x1": 8,
            "detector_crop_x2": 2039,
            "detector_crop_y1": 16,
            "detector_crop_y2": 2031,
            "energy_unit": "eV",
            "exposure_unit": "ms",
            "reciprocal_lattice_unit": "1/Å",
            "lattice_parameters_unit": "Å",
            "beamline": "34ID-X",
        },
        "wire": {"num_threads": 7, "memory_limit_mb": 4096, "verbose": 2},
    }[kind]
    assert {name: getattr(params, name) for name in type(params).fields_from("config")} == expected
    assert (params.computer_name, params.priority, params.algorithm_version) == (socket.gethostname(), 0, None)
    assert started <= params.submitted_at <= datetime.now(UTC) + timedelta(seconds=1)
    if kind == "lauego":
        assert params.cosmic_filter is False  # always false; no setting

    portal = {"computer_name": "elsewhere", "priority": 9, "submitted_at": "2020-01-01T00:00:00Z"}
    portal |= {"schema_id": f"{kind}/v1", "algorithm_version": "x", **{name: 1 for name in expected}}
    portal |= {"cosmic_filter": True} if kind == "lauego" else {}
    with pytest.raises(ValidationError) as refused:
        programs.build_params(kind, _form(kind, settings, **portal), settings)
    assert {(error["loc"][0], error["type"]) for error in refused.value.errors()} == {
        (name, "set_by_portal") for name in portal
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        programs.build_params(kind, _form(kind, settings, crop="0 2047 0 2047"), settings)


FORM_DEFAULTS: dict[programs.ProgramKind, dict[str, tuple[Any, dict[str, float]]]] = {
    "lauego": {
        "threshold": (None, {"minimum": 0}),
        "threshold_ratio": (None, {"minimum": 0}),
        "max_rfactor": (0.5, {"minimum": 0, "maximum": 1}),
        "box_size": (18, {"exclusiveMinimum": 0}),
        "max_number": (300, {"exclusiveMinimum": 0}),
        "min_separation": (20, {"minimum": 0}),
        "peak_shape": ("Lorentzian", {}),
        "min_size": (3, {"exclusiveMinimum": 0}),
        "max_peaks": (200, {"exclusiveMinimum": 0}),
        "smooth": (False, {}),
        "mask_file": (None, {}),
        "index_kev_max_calc": (17.2, {"exclusiveMinimum": 0}),
        "index_kev_max_test": (35.0, {"exclusiveMinimum": 0}),
        "index_angle_tolerance": (0.1, {"minimum": 0}),
        "index_h": (0, {}),
        "index_k": (0, {}),
        "index_l": (1, {}),
        "index_cone": (72.0, {"minimum": 0, "maximum": 180}),
        "output_xml": ("output.xml", {}),
        "depth": (None, {}),  # automatic
        "scan_points": ("", {}),
        "depth_range": ("", {}),
        "author": ("", {}),
        "notes": ("", {}),
        "backend": ("local", {}),
    },
    "wire": {
        "percent_brightest": (100, {"exclusiveMinimum": 0, "maximum": 100}),
        "wire_edges": ("leading", {}),
        "depth_start": (-50, {}),
        "depth_end": (150, {}),
        "depth_resolution": (1, {"exclusiveMinimum": 0}),
        "frame_skip": (1, {"minimum": 0}),
        "wire_skip": (1, {"minimum": 0}),
        "author": ("", {}),
        "notes": ("", {}),
        "backend": ("local", {}),
    },
}
"""The form defaults and numeric limits, by field; fields not listed have none (the paths, which come from the source or
the deployment's config)."""
FORM_CHOICES = {"peak_shape": ["Lorentzian", "Gaussian"], "wire_edges": ["leading", "trailing", "both"]}
LIMIT_KEYS = ("minimum", "exclusiveMinimum", "maximum")


def _limits(schema: dict[str, Any]) -> dict[str, float]:
    found = {key: schema[key] for key in LIMIT_KEYS if key in schema}
    for option in schema.get("anyOf", ()):
        found |= {key: option[key] for key in LIMIT_KEYS if key in option}
    return found


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_the_defaults_and_limits_are_the_forms_and_the_model_enforces_them(tmp_path, kind):
    settings = make_settings(tmp_path)  # the shipped defaults
    properties = programs.parameters_schema(kind, settings)["properties"]
    form = _form(kind, settings)

    for name in programs.PROGRAMS[kind].fields_from("form"):
        default, limits = FORM_DEFAULTS[kind].get(name, (properties[name].get("default", "<none>"), {}))
        assert (name, properties[name].get("default", "<none>"), _limits(properties[name])) == (name, default, limits)
        if name in FORM_CHOICES:
            assert properties[name]["enum"] == FORM_CHOICES[name]
            with pytest.raises(ValidationError, match=name):
                programs.build_params(kind, {**form, name: FORM_CHOICES[name][0].upper()}, settings)
        for key, bound in limits.items():
            outside = {"minimum": bound - 1, "exclusiveMinimum": bound, "maximum": bound + 1}[key]
            with pytest.raises(ValidationError, match=name):
                programs.build_params(kind, {**form, name: outside}, settings)
            if key != "exclusiveMinimum":
                assert getattr(programs.build_params(kind, {**form, name: bound}, settings), name) == bound
        if limits:
            with pytest.raises(ValidationError, match=name):
                programs.build_params(kind, {**form, name: float("nan")}, settings)
    assert properties["backend"]["enum"] == list(BACKENDS)
    if kind == "wire":  # the wire form: at most one %d per file-name template
        with pytest.raises(ValidationError, match="more than one %d"):
            programs.build_params(kind, {**form, "filename_prefixes": ["Si_%d_%d.h5"]}, settings)


@pytest.mark.parametrize(
    ("kind", "scan", "rec", "expected"),
    [
        ("lauego", 7, 3, "analysis/scan_7/rec_3/index_%d"),
        ("lauego", 7, None, "analysis/scan_7/index_%d"),
        ("lauego", None, None, "analysis/index_%d"),
        ("wire", 7, None, "analysis/scan_7/rec_%d/data"),
        ("wire", None, None, "analysis/rec_%d/data"),
    ],
)
def test_the_output_folder_comes_from_output_templates(tmp_path, kind, scan, rec, expected):
    settings = make_settings(tmp_path)
    template = programs.output_path_template(kind, settings, scan_number=scan, reconstruction_id=rec)
    assert template == os.path.join(settings.files.runs_root, expected)


def test_programs_lists_both_programs_with_their_parameter_schemas(tmp_path):
    settings = make_settings(tmp_path, indexing={"defaults": {"box_size": 25}}, execution={"num_threads": 6})
    client = TestClient(create_app(settings))

    response = client.get("/api/v1/programs")

    assert response.status_code == 200
    found = {program["kind"]: program for program in response.json()["programs"]}
    assert {kind: (program["title"], program["schema_id"]) for kind, program in found.items()} == {
        "lauego": ("LaueGo indexing", "lauego/v1"),
        "laue_matching": ("LaueMatching indexing", "laue-matching/v1"),
        "wire": ("Wire reconstruction", "wire/v1"),
    }
    for kind, model in (("lauego", LaueGoParamsV1), ("laue_matching", LaueMatchingParamsV1), ("wire", WireParamsV1)):
        properties = found[kind]["json_schema"]["properties"]
        assert list(properties) == list(model.model_fields)
        assert all(
            schema["x-source"] in ("form", "config", "derived") and schema["title"] for schema in properties.values()
        )
        assert all(schema.get("readOnly", False) == (schema["x-source"] != "form") for schema in properties.values())
    assert found["lauego"]["json_schema"]["properties"]["box_size"]["default"] == 25  # the configured default
    assert found["wire"]["json_schema"]["properties"]["num_threads"]["default"] == 6  # a fixed value, read-only
    assert (
        client.get("/api/v1/openapi.json").json()["paths"]["/api/v1/programs"]["get"]["operationId"] == "listPrograms"
    )


FORM_LAYOUT: dict[programs.ProgramKind, list[tuple[str, list[str]]]] = {
    "lauego": [
        ("Identity", ["scan_number", "reconstruction_id", "author"]),
        (
            "File Paths",
            [
                "input_path",
                "filename_prefixes",
                "scan_points",
                "depth_range",
                "geometry_file",
                "crystal_file",
                "output_path_template",
                "output_xml",
            ],
        ),
        (
            "Peak Search Parameters",
            [
                "box_size",
                "max_rfactor",
                "threshold",
                "threshold_ratio",
                "min_size",
                "min_separation",
                "max_number",
                "peak_shape",
                "mask_file",
                "smooth",
            ],
        ),
        (
            "Indexing Parameters",
            [
                "index_kev_max_calc",
                "index_kev_max_test",
                "index_angle_tolerance",
                "index_h",  # one Central HKL field
                "index_k",
                "index_l",
                "index_cone",
                "max_peaks",
                "depth",
            ],
        ),
        ("Notes", ["notes"]),
        ("Execution", ["backend"]),  # "Run on", by Validate and Submit
    ],
    "wire": [
        ("Identity", ["scan_number", "author"]),
        ("File Paths", ["input_path", "filename_prefixes", "scan_points", "output_path_template", "geometry_file"]),
        (
            "Wire Reconstruction Parameters",
            [
                "depth_start",
                "depth_end",
                "depth_resolution",
                "wire_edges",
                "percent_brightest",
                "frame_skip",
                "wire_skip",
            ],
        ),
        ("Notes", ["notes"]),
        ("Execution", ["backend"]),
    ],
}
"""The form layout: its sections in order, each with its fields in order."""


@pytest.mark.parametrize("kind", ["lauego", "wire"])
def test_the_served_schema_has_the_form_layout_and_option_order_and_requires_only_the_forms_fields(tmp_path, kind):
    model = programs.PROGRAMS[kind]
    schema = programs.parameters_schema(kind, make_settings(tmp_path))
    properties = schema["properties"]

    assert [(section["group"], section["fields"]) for section in schema["x-layout"]] == FORM_LAYOUT[kind]
    assert sorted(name for _, fields in FORM_LAYOUT[kind] for name in fields) == sorted(model.fields_from("form"))
    assert all(properties[name]["x-group"] == group for group, fields in FORM_LAYOUT[kind] for name in fields)
    for name, values in FORM_CHOICES.items():
        if name in properties:
            assert [option["value"] for option in properties[name]["x-options"]] == values
    required = {name for name, field in model.model_fields.items() if field.is_required()}
    assert set(schema["required"]) == required & set(model.fields_from("form"))  # not the portal's values


FORM_FIXTURE = Path(__file__).resolve().parents[3] / "frontend" / "src" / "test" / "programs.json"
"""The form kit's tests read the programs from this capture of ``GET /api/v1/programs`` (the dev config's defaults,
paths made neutral)."""


def _without_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    kept = json.loads(json.dumps(schema))
    for item in kept["properties"].values():
        item.pop("default", None)
    return kept


def test_the_form_kits_programs_fixture_is_the_served_schema_but_for_its_defaults(tmp_path):
    """The frontend's fixture cannot drift from the models: every program (the batch three, and the live two the Live
    page reads, listed while live processing is on), every field (its type and nullability, title, hint, placeholder,
    widget, limits, options), the layout and the required list are the served ones; only the configured defaults are
    the dev config's. Refresh it from the API when the models change."""

    fixture = json.loads(FORM_FIXTURE.read_text(encoding="utf-8"))
    settings = make_settings(tmp_path, live={"enabled": True})
    assert [(program["kind"], program["title"], program["schema_id"]) for program in fixture["programs"]] == [
        (program.kind, program.title, program.schema_id) for program in programs.list_programs(settings).programs
    ]
    for program in fixture["programs"]:
        served = programs.parameters_schema(program["kind"], settings)
        assert _without_defaults(program["json_schema"]) == _without_defaults(served), program["kind"]


DEVELOPER_TEXT = re.compile(r"`|\bv1\b|lauelab|LaueGo|reconstructN|\[|settings|config|3ad1557", re.IGNORECASE)
"""What a user hint leaves to the developer ``description``: code, v1 history, lauelab names, config keys."""


@pytest.mark.parametrize("kind", ["lauego", "laue_matching", "wire"])
def test_every_form_field_has_a_user_hint_and_at_most_a_terse_value_pattern_as_placeholder(tmp_path, kind):
    """The form shows labels and values; a field's hint (``x-hint``) is the label's tooltip, in the user's words (the
    ``description`` stays the API's documentation), and a placeholder reads as a value (``Si_%d.h5``, ``Automatic``),
    never a sentence or an example list."""

    properties = programs.parameters_schema(kind, make_settings(tmp_path))["properties"]
    for name in programs.PROGRAMS[kind].fields_from("form"):
        hint = properties[name]["x-hint"]
        assert hint and len(hint) <= 120 and not DEVELOPER_TEXT.search(hint), (name, hint)
        placeholder = properties[name].get("x-placeholder")
        assert placeholder is None or (len(placeholder) <= 12 and " " not in placeholder), (name, placeholder)
    assert {name for name, item in properties.items() if "x-extensions" in item} == {
        name for name, item in properties.items() if item.get("x-widget") == "file"
    }  # a file picker lists only its kind of file


@pytest.mark.parametrize(
    ("targets", "offered"),
    [
        (None, [("local", "APS")]),  # remote execution off
        ({"aps", "crux"}, [("aps", "APS"), ("crux", "ALCF")]),
        ({"crux"}, [("local", "APS"), ("crux", "ALCF")]),
    ],
)
def test_programs_offers_the_deployments_execution_targets(tmp_path, targets, offered):
    """The "Run on" choices (``remote.settings.available_backends``): one APS choice, ``local`` until the DM ``aps``
    target is enabled; ALCF when ``crux`` is. The form starts at the first."""
    layout = Layout(tmp_path)
    remote: dict[str, Any] = {"enabled": False}
    if targets is not None:
        document = bdp_settings(layout)
        for name, target in document["targets"].items():
            target["enabled"] = name in targets
        (tmp_path / "bdp.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
        remote = {"enabled": True, "settings_file": tmp_path / "bdp.yaml"}
    settings = make_settings(tmp_path, files={"runs_root": layout.experiment}, remote=remote)

    response = TestClient(create_app(settings)).get("/api/v1/programs")

    assert response.status_code == 200
    for program in response.json()["programs"]:
        if program["kind"] == "laue_matching":  # computes on the portal's worker: no target to choose
            assert program["json_schema"]["properties"]["backend"]["readOnly"]
            continue
        backend = program["json_schema"]["properties"]["backend"]
        assert [(option["value"], option["label"]) for option in backend["x-options"]] == offered
        assert backend["default"] == offered[0][0]


def test_programs_is_a_503_when_the_remote_settings_cannot_be_used(tmp_path):
    missing = tmp_path / "private" / "bdp.yaml"
    settings = make_settings(tmp_path, remote={"enabled": True, "settings_file": missing})

    response = TestClient(create_app(settings)).get("/api/v1/programs")

    assert (response.status_code, response.json()["title"]) == (503, "Remote settings unusable")
    assert str(missing.parent) not in response.text
