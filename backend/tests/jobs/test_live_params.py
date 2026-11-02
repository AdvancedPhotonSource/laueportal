"""The live schemas ``wire-live/v1`` and ``lauego-live/v1``, checked against the batch schemas ``wire/v1`` and
``lauego/v1`` they share their fields with."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from laueportal.core.params import ProgramParams
from laueportal.indexing.lauego.live_params import LaueGoLiveParamsV1, check_live_output_xml_name
from laueportal.indexing.lauego.params import LaueGoParamsV1, check_output_xml_name
from laueportal.jobs import programs
from laueportal.jobs.rundir import read_request, request_document, write_request
from laueportal.reconstruction.wire.live_params import WireLiveParamsV1
from laueportal.reconstruction.wire.params import WireParamsV1
from tests.remote_support import GOLDENS
from tests.settings_support import make_settings

LIVE: dict[programs.ProgramKind, tuple[type[ProgramParams], type[ProgramParams]]] = {
    "wire": (WireLiveParamsV1, WireParamsV1),
    "lauego": (LaueGoLiveParamsV1, LaueGoParamsV1),
}
"""Each live schema, and the batch schema it shares its fields with."""

DIFFERENCES: dict[programs.ProgramKind, set[str]] = {
    "wire": {"scan_points", "filename_prefixes", "backend", "schema_id"},
    "lauego": {"scan_points", "filename_prefixes", "output_xml", "backend", "schema_id"},
}
"""The only keys whose JSON Schema differs from the batch schema's."""

SOURCES: dict[programs.ProgramKind, dict[str, Any]] = {
    "wire": {
        "scan_number": 5,
        "input_path": "/data/scan_5/",
        "filename_prefixes": [" Si_%d.h5 "],
        "geometry_file": "/config/./geometry.xml",
    },
    "lauego": {
        "scan_number": 5,
        "reconstruction_id": None,
        "input_path": "/data/scan_5/",
        "filename_prefixes": [" Si_%d.h5 "],
        "geometry_file": "/config/./geometry.xml",
        "crystal_file": "/config/Ni.xml",
        "depth": " 12.5 ",
        "author": "  ",
    },
}
"""What the Live page's Scan card and a program card give a live run, besides the configured defaults."""


def _form(kind: programs.ProgramKind, settings: Any, **values: Any) -> dict[str, Any]:
    defaults = {name: value for name, value in programs.form_defaults(kind, settings).items() if name != "scan_points"}
    return {**defaults, **SOURCES[kind], **values}


def _live(kind: programs.ProgramKind, settings: Any, **values: Any) -> ProgramParams:
    return LIVE[kind][0].from_form(_form(kind, settings, **values), fixed=programs.fixed_values(kind, settings))


@pytest.mark.parametrize("kind", ["wire", "lauego"])
def test_each_live_schema_has_its_batch_schemas_fields_and_differs_only_where_the_design_says(kind):
    live, batch = LIVE[kind]
    live_schema, batch_schema = live.model_json_schema(), batch.model_json_schema()
    live_properties, batch_properties = live_schema["properties"], batch_schema["properties"]

    assert list(live.model_fields) == list(batch.model_fields)
    assert list(live_properties) == list(batch_properties)
    assert {name for name in batch_properties if live_properties[name] != batch_properties[name]} == DIFFERENCES[kind]
    assert live_properties["scan_points"]["default"] == ""
    assert live_properties["scan_points"]["x-source"] == live_properties["backend"]["x-source"] == "derived"
    assert live_properties["backend"]["const"] == "local"
    assert live_properties["filename_prefixes"]["maxItems"] == 1
    assert live_properties["schema_id"]["const"] == f"{kind}-live/v1"
    assert set(live_schema["required"]) == set(batch_schema["required"]) - {"scan_points"}


@pytest.mark.parametrize("kind", ["wire", "lauego"])
def test_the_live_form_takes_the_batch_forms_fields_without_the_scan_indices_and_the_target(tmp_path, kind):
    live, batch = LIVE[kind]
    settings = make_settings(tmp_path)
    form = set(live.fields_from("form"))

    assert form == set(batch.fields_from("form")) - {"scan_points", "backend"}
    assert {name for _, fields in live.form_layout for name in fields} == form
    assert [group for group, _ in live.form_layout] == [group for group, _ in batch.form_layout if group != "Execution"]
    with pytest.raises(ValidationError) as refused:
        _live(kind, settings, scan_points="1-3", backend="aps")
    assert {(error["loc"][0], error["type"]) for error in refused.value.errors()} == {
        ("scan_points", "set_by_portal"),
        ("backend", "set_by_portal"),
    }


@pytest.mark.parametrize(("kind", "golden"), [("wire", "aps-wire"), ("lauego", "aps-lauego")])
def test_a_live_runs_request_json_has_exactly_the_batch_schemas_keys(tmp_path, kind, golden):
    v1_keys = list(json.loads((GOLDENS / golden / "request.json").read_text(encoding="utf-8"))["request"])
    settings = make_settings(tmp_path)
    live = _live(kind, settings)
    batch = programs.build_params(kind, {**_form(kind, settings), "scan_points": "1-3"}, settings)
    write_request(tmp_path / "request.json", {"request": request_document(live)})

    request = read_request(tmp_path / "request.json")["request"]
    assert list(request_document(live)) == list(request_document(batch))
    assert set(request) == set(v1_keys) | {"schema_id"}
    assert (request["scan_points"], request["backend"], request["schema_id"]) == ("", "local", f"{kind}-live/v1")


@pytest.mark.parametrize("kind", ["wire", "lauego"])
def test_the_live_models_normalize_each_shared_value_as_the_batch_models_do(tmp_path, kind):
    settings = make_settings(tmp_path)
    live = request_document(_live(kind, settings))
    batch = request_document(programs.build_params(kind, {**_form(kind, settings), "scan_points": "1-3"}, settings))

    assert {name for name in live if live[name] != batch[name]} == {"scan_points", "schema_id", "submitted_at"}
    assert live["input_path"] == "/data/scan_5"
    assert list(live["filename_prefixes"]) == ["Si_%d.h5"]


@pytest.mark.parametrize("kind", ["wire", "lauego"])
@pytest.mark.parametrize(
    ("templates", "accepted"),
    [
        (["Si_%d.h5"], True),
        (["Si_*%d.h5"], True),
        (["Si.h5"], False),  # no number
        (["Si_%d_%d.h5"], False),  # a raw frame's second %d (a depth index) is batch-only
        (["Si_%d.h5", "Ge_%d.h5"], False),  # one template
        (["Si_%d.h5", " "], True),  # a blank one dropped, as the batch schemas drop it
    ],
)
def test_a_live_run_takes_one_file_name_template_with_one_number(tmp_path, kind, templates, accepted):
    settings = make_settings(tmp_path)
    if accepted:
        assert list(request_document(_live(kind, settings, filename_prefixes=templates))["filename_prefixes"]) == [
            templates[0]
        ]
    else:
        with pytest.raises(ValidationError, match="filename_prefixes"):
            _live(kind, settings, filename_prefixes=templates)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", "output.xml"), (" results.xml ", "results.xml"), ("sub/out.xml", None), ("/abs/out.xml", None)]
    + [("run.json", None), ("out.xml.partial", None)],
)
def test_a_live_indexing_runs_output_xml_is_a_file_name_in_its_folder(tmp_path, value, expected):
    settings = make_settings(tmp_path)
    if expected is None:
        with pytest.raises(ValidationError, match="output_xml"):
            _live("lauego", settings, output_xml=value)
    else:
        assert _live("lauego", settings, output_xml=value).model_dump()["output_xml"] == expected


@pytest.mark.parametrize(
    "name", ["live.json", "arrivals.jsonl", "arrivals.lock", "arrival-status.json", "completed.jsonl"]
)
def test_a_live_run_s_files_are_reserved_names_for_every_run(name):
    """No Output XML, batch or live, may take one."""
    for check in (check_output_xml_name, check_live_output_xml_name):
        with pytest.raises(ValueError, match="reserved"):
            check(name)
