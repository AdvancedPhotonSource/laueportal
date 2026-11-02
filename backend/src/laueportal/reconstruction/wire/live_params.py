"""Live wire reconstruction parameters, ``wire-live/v1``: the jobs core creates and runs a live run with them
(``jobs.programs.LIVE_PROGRAMS``, ``jobs.creation.create_live_run``); the Live page's API takes its form fields
(``jobs.live_session``; ``GET /programs`` lists it as ``wire-live``).

``WireLiveParamsV1`` has exactly ``wire/v1``'s fields, in their order, so a live run's ``request.json`` holds exactly
``wire/v1``'s keys (``jobs.rundir.request_document``). Each field is ``wire/v1``'s (type, default, limits,
source and form metadata: ``core.params.shared_field``), normalized as ``wire/v1`` does, but for:

- ``scan_points``: derived, always empty: a live run's inputs are the files that arrive while it runs;
- ``filename_prefixes``: exactly one template with exactly one ``%d``;
- ``backend``: derived, always ``local``: a live run is computed on the portal host;
- ``schema_id``: ``wire-live/v1``.

``live.json`` (``jobs.live``) says where a live run's files come from.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import field_validator

from laueportal.core.params import (
    FilenameTemplate,
    ProgramParams,
    live_backend_param,
    live_form_layout,
    live_scan_points_param,
    live_template_items,
    live_templates,
    param,
    required_path,
    schema_id_param,
    shared_field,
)
from laueportal.files.inputs import (
    normalize_filename_templates,
    normalize_input_directory,
    normalize_output_path_template,
)
from laueportal.reconstruction.wire.params import FILES, WireParamsV1

SCHEMA_ID = "wire-live/v1"


def _wire(name: str) -> Any:
    return shared_field(WireParamsV1, name)


class WireLiveParamsV1(ProgramParams):
    """The parameters of one live wire reconstruction run (`wire-live/v1`): `wire/v1`'s, without the scan indices."""

    form_layout = live_form_layout(WireParamsV1)

    scan_number: int | None = _wire("scan_number")
    input_path: str = _wire("input_path")
    output_path_template: str = _wire("output_path_template")
    filename_prefixes: Sequence[FilenameTemplate] = param(
        title="Filename",
        source="form",
        group=FILES,
        widget="filename-templates",
        hint="The wire-scan files' name: %d is each file's number, * any text (e.g. Si_%d.h5).",
        placeholder="Si_%d.h5",
        min_length=1,
        max_length=1,
        description=(
            "The arriving wire-scan files' name template: exactly one, with exactly one `%d`, each file's number; "
            "wildcards are allowed; at most 255 characters."
        ),
    )
    geometry_file: str = _wire("geometry_file")
    percent_brightest: float = _wire("percent_brightest")
    wire_edges: Literal["leading", "trailing", "both"] = _wire("wire_edges")
    depth_start: float = _wire("depth_start")
    depth_end: float = _wire("depth_end")
    depth_resolution: float = _wire("depth_resolution")
    num_threads: int = _wire("num_threads")
    memory_limit_mb: int = _wire("memory_limit_mb")
    scan_points: Literal[""] = live_scan_points_param()
    verbose: int = _wire("verbose")
    frame_skip: int = _wire("frame_skip")
    wire_skip: int = _wire("wire_skip")
    author: str | None = _wire("author")
    notes: str | None = _wire("notes")
    algorithm_version: str | None = _wire("algorithm_version")
    backend: Literal["local"] = live_backend_param()
    computer_name: str = _wire("computer_name")
    priority: int = _wire("priority")
    submitted_at: datetime = _wire("submitted_at")
    schema_id: Literal["wire-live/v1"] = schema_id_param(SCHEMA_ID)

    @field_validator("input_path")
    @classmethod
    def _input_path(cls, value: str) -> str:
        return normalize_input_directory(value)

    @field_validator("output_path_template")
    @classmethod
    def _output_path_template(cls, value: str) -> str:
        return normalize_output_path_template(value)

    @field_validator("filename_prefixes", mode="before")
    @classmethod
    def _filename_prefix_items(cls, value: Any) -> Any:
        return live_template_items(value)

    @field_validator("filename_prefixes")
    @classmethod
    def _filename_prefixes(cls, value: Sequence[str]) -> tuple[str, ...]:
        return live_templates(normalize_filename_templates(value))

    @field_validator("geometry_file")
    @classmethod
    def _geometry_file(cls, value: str) -> str:
        return required_path(value, "Geometry file")
