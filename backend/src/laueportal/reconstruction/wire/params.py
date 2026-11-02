"""Wire reconstruction parameters, ``wire/v1``.

``WireParamsV1`` has exactly the fields of a wire reconstruction request, in their order, plus
``schema_id``; ``request.json`` stores them (``jobs.rundir.request_document``). It normalizes each value and
checks the per-field rules; where each value comes from (the form, config, or
the portal) is in ``core.params``. The compute function never imports it (a compute node has no pydantic).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import field_validator

from laueportal.core.params import (
    TEMPLATES_MAX_ITEMS,
    Backend,
    FilenameTemplate,
    PathText,
    ProgramParams,
    algorithm_version_param,
    author_param,
    backend_param,
    computer_name_param,
    notes_param,
    param,
    priority_param,
    required_path,
    schema_id_param,
    submitted_at_param,
)
from laueportal.files.inputs import (
    count_integer_range,
    normalize_filename_templates,
    normalize_input_directory,
    normalize_integer_range,
    normalize_output_path_template,
)
from laueportal.reconstruction.wire.constants import DEFAULT_SKIP

SCHEMA_ID = "wire/v1"

FILES = "File Paths"
WIRE = "Wire Reconstruction Parameters"


def _defaults(name: str) -> str:
    return f"reconstruction.defaults.{name}"


def _depth(title: str, name: str, description: str, hint: str, **limits: Any) -> Any:
    return param(
        title=title,
        source="form",
        config=_defaults(name),
        unit="µm",
        group=WIRE,
        widget="number",
        hint=hint,
        description=description,
        **limits,
    )


def _skip(title: str, name: str, unit: str, description: str, hint: str) -> Any:
    return param(
        DEFAULT_SKIP,
        title=title,
        source="form",
        config=_defaults(name),
        unit=unit,
        group=WIRE,
        widget="integer",
        ge=0,
        hint=hint,
        description=description,
    )


class WireParamsV1(ProgramParams):
    """The parameters of one wire reconstruction run (`wire/v1`)."""

    form_layout = (  # "Run on" sits by Validate and Submit
        ("Identity", ("scan_number", "author")),
        (FILES, ("input_path", "filename_prefixes", "scan_points", "output_path_template", "geometry_file")),
        (
            WIRE,
            (
                "depth_start",
                "depth_end",
                "depth_resolution",
                "wire_edges",
                "percent_brightest",
                "frame_skip",
                "wire_skip",
            ),
        ),
        ("Notes", ("notes",)),
        ("Execution", ("backend",)),
    )

    scan_number: int | None = param(
        title="Scan Number",
        source="form",
        group="Identity",
        widget="scan",
        hint="The scan the run reconstructs. Empty: an unlinked run.",
        description="The scan the run reconstructs; empty for an unlinked run.",
    )
    input_path: PathText = param(
        title="Folder Path",
        source="form",
        group=FILES,
        widget="folder",
        hint="The folder of the wire-scan files.",
        description="The folder of the wire-scan files (from the scan's catalog or the source run).",
    )
    output_path_template: PathText = param(
        title="Output Path",
        source="form",
        config="output_templates.reconstruction",
        group=FILES,
        widget="output-folder",
        hint="The run's folder; %d becomes the run ID.",
        description=(
            "The run directory, with exactly one `%d`, the run ID. The default is "
            "`[output_templates] reconstruction` under `files.runs_root`, without the scan segment when the run has "
            "no scan."
        ),
    )
    filename_prefixes: Sequence[FilenameTemplate] = param(
        title="Filename",
        source="form",
        group=FILES,
        widget="filename-templates",
        hint="The wire-scan files' names, comma-separated: %d is the scan index, * any text (e.g. Si_%d.h5).",
        placeholder="Si_%d.h5",
        min_length=1,
        max_length=TEMPLATES_MAX_ITEMS,
        description=(
            "The wire-scan files' name templates (the catalog's prefixes): at most one "
            "`%d` each, the scan index; wildcards are allowed. At least one, at most 100, each at most 255 characters; "
            "blank ones are dropped."
        ),
    )
    geometry_file: PathText = param(
        title="Geometry File",
        source="form",
        config=_defaults("geometry_file"),
        group=FILES,
        widget="file",
        hint="The detector geometry (XML).",
        extensions=(".xml",),
        description="The detector geometry (XML); it must exist inside the roots.",
    )
    percent_brightest: float = param(
        title="Intensity Percentile",
        source="form",
        config=_defaults("percent_brightest"),
        unit="%",
        group=WIRE,
        widget="number",
        hint="The share of the brightest pixels that are reconstructed.",
        gt=0,
        le=100,
        description="The percentage of the brightest pixels that are reconstructed, in (0, 100].",
    )
    wire_edges: Literal["leading", "trailing", "both"] = param(
        title="Wire Edges",
        source="form",
        config=_defaults("wire_edges"),
        group=WIRE,
        widget="select",
        hint="The edge of the wire's shadow that is used.",
        options={"leading": "Leading Edge", "trailing": "Trailing Edge", "both": "Both Edges"},
        description="The edge of the wire's shadow that is used.",
    )
    depth_start: float = _depth(
        "Depth Start",
        "depth_start",
        "The start of the reconstructed depth range.",
        "The start of the reconstructed depth range.",
    )
    depth_end: float = _depth(
        "Depth End",
        "depth_end",
        "The end of the reconstructed depth range.",
        "The end of the reconstructed depth range.",
    )
    depth_resolution: float = _depth(
        "Depth Resolution",
        "depth_resolution",
        "The depth step of the reconstruction.",
        "The depth step of the reconstruction.",
        gt=0,
    )
    num_threads: int = param(
        title="Threads",
        source="config",
        config="execution.num_threads",
        gt=0,
        description="The OpenMP threads of each reconstruction worker.",
    )
    memory_limit_mb: int = param(
        title="Memory Limit",
        source="config",
        config="execution.memory_limit_mb",
        unit="MB",
        gt=0,
        description="The stripe-buffer budget of each reconstruction worker.",
    )
    scan_points: str = param(
        title="Scan Indices",
        source="form",
        group=FILES,
        widget="indices",
        hint="The scan points to reconstruct, e.g. 1-10, 1,5,8,9, 1-4,10-21 or 1-10:2 (every second).",
        placeholder="1-10,15",
        description=(
            "The scan points to reconstruct (found from the files): srange syntax, at least one; stored as srange "
            "writes it."
        ),
    )
    verbose: int = param(
        title="Verbose",
        source="config",
        config="execution.verbose",
        ge=0,
        description="Stored for the record: compute does not use it.",
    )
    frame_skip: int = _skip(
        "Frame Skip",
        "frame_skip",
        "slices",
        "The leading stored slices the reconstruction skips (reconstructN_cpu's `MULTI_IMAGE_SKIP`, 1); another "
        "value changes which wire positions are paired with each frame.",
        "The leading frames the reconstruction skips; another value changes which wire positions match each frame.",
    )
    wire_skip: int = _skip(
        "Wire Skip",
        "wire_skip",
        "entries",
        "The leading wire-vector entries the reconstruction skips (reconstructN_cpu's `MULTI_IMAGE_SKIPV`, 1).",
        "The leading wire positions the reconstruction skips.",
    )
    author: str | None = author_param(_defaults("author"))
    notes: str | None = notes_param(_defaults("notes"))
    algorithm_version: str | None = algorithm_version_param()
    backend: Backend = backend_param()
    computer_name: str = computer_name_param()
    priority: int = priority_param()
    submitted_at: datetime = submitted_at_param()
    schema_id: Literal["wire/v1"] = schema_id_param(SCHEMA_ID)

    @field_validator("input_path")
    @classmethod
    def _input_path(cls, value: str) -> str:
        return normalize_input_directory(value)

    @field_validator("output_path_template")
    @classmethod
    def _output_path_template(cls, value: str) -> str:
        return normalize_output_path_template(value)

    @field_validator("filename_prefixes")
    @classmethod
    def _filename_prefixes(cls, value: Sequence[str]) -> tuple[str, ...]:
        templates = normalize_filename_templates(value)
        for template in templates:
            if template.count("%d") > 1:
                raise ValueError(f"Filename template {template!r} has more than one %d placeholder")
        return templates

    @field_validator("scan_points")
    @classmethod
    def _scan_points(cls, value: str) -> str:
        return count_integer_range(value, "scan points")[0]  # counted, not expanded

    @field_validator("geometry_file")
    @classmethod
    def _geometry_file(cls, value: str) -> str:
        return required_path(value, "Geometry file")

    @property
    def scan_point_values(self) -> tuple[int, ...]:
        """The scan points, expanded (derived: not in ``request.json``)."""
        return normalize_integer_range(self.scan_points, "scan points")[1]

    @property
    def scan_point_count(self) -> int:
        """How many scan points there are, without expanding them (derived: not in ``request.json``)."""
        return count_integer_range(self.scan_points, "scan points")[1]
