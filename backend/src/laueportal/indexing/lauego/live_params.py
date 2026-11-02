"""Live LaueGo indexing parameters, ``lauego-live/v1``: the jobs core creates and runs a live run with them
(``jobs.programs.LIVE_PROGRAMS``, ``jobs.creation.create_live_run``); the Live page's API takes its form fields
(``jobs.live_session``; ``GET /programs`` lists it as ``lauego-live``).

``LaueGoLiveParamsV1`` has exactly ``lauego/v1``'s fields, in their order, so a live run's ``request.json`` holds
exactly ``lauego/v1``'s keys (``jobs.rundir.request_document``). Each field is ``lauego/v1``'s (type, default,
limits, source and form metadata: ``core.params.shared_field``), normalized as ``lauego/v1`` does, but for:

- ``scan_points``: derived, always empty: a live run's inputs are the files (or, following a live wire run, the
  points) that arrive while it runs;
- ``filename_prefixes``: exactly one template with exactly one ``%d``;
- ``output_xml``: a file name in the run's folder, not a path;
- ``backend``: derived, always ``local``: a live run is computed on the portal host;
- ``schema_id``: ``lauego-live/v1``.

Raw frames or a live wire run's points (the Reconstruction source: ``reconstruction_id``) are the run's two kinds of
input; ``live.json`` (``jobs.live``) says which.
"""

import os
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import field_validator

from laueportal.core.params import (
    FilenameTemplate,
    PathText,
    ProgramParams,
    depth_text,
    live_backend_param,
    live_form_layout,
    live_scan_points_param,
    live_template_items,
    live_templates,
    optional_text,
    param,
    required_path,
    schema_id_param,
    shared_field,
)
from laueportal.files.inputs import (
    WorkflowValidationError,
    count_optional_integer_range,
    normalize_filename_templates,
    normalize_input_directory,
    normalize_optional_integer_range,
    normalize_output_path_template,
)
from laueportal.indexing.lauego.params import (
    DEFAULT_OUTPUT_XML,
    FILES,
    TEMPLATE_PLACEHOLDER,
    LaueGoParamsV1,
    check_output_xml_name,
)

SCHEMA_ID = "lauego-live/v1"


def _lauego(name: str) -> Any:
    return shared_field(LaueGoParamsV1, name)


def check_live_output_xml_name(name: str) -> None:
    """A live run's Output XML is a file name in the run's folder, and one the run may create
    (``check_output_xml_name``)."""
    if os.path.isabs(name) or os.path.basename(name) != name:
        raise WorkflowValidationError(f"Output XML name {name!r} must be a file name in the run's folder, not a path")
    check_output_xml_name(name)


class LaueGoLiveParamsV1(ProgramParams):
    """The parameters of one live LaueGo indexing run (`lauego-live/v1`): `lauego/v1`'s, without the scan indices."""

    form_layout = live_form_layout(LaueGoParamsV1)

    scan_number: int | None = _lauego("scan_number")
    reconstruction_id: int | None = _lauego("reconstruction_id")
    input_path: str = _lauego("input_path")
    output_path_template: str = _lauego("output_path_template")
    filename_prefixes: Sequence[FilenameTemplate] = param(
        title="Filename",
        source="form",
        group=FILES,
        widget="filename-templates",
        hint="The frame files' name: %d is each file's number, * any text (e.g. Si_%d.h5).",
        placeholder=TEMPLATE_PLACEHOLDER,
        min_length=1,
        max_length=1,
        description=(
            "The arriving frame files' name template, or the followed wire run's: exactly one, with exactly one "
            "`%d`, each file's number; wildcards are allowed; at most 255 characters."
        ),
    )
    threshold: int | None = _lauego("threshold")
    threshold_ratio: float | None = _lauego("threshold_ratio")
    max_rfactor: float = _lauego("max_rfactor")
    box_size: int = _lauego("box_size")
    max_number: int | None = _lauego("max_number")
    min_separation: int = _lauego("min_separation")
    peak_shape: Literal["Lorentzian", "Gaussian"] = _lauego("peak_shape")
    scan_points: Literal[""] = live_scan_points_param()
    depth_range: str | None = _lauego("depth_range")
    detector_crop_x1: int = _lauego("detector_crop_x1")
    detector_crop_x2: int = _lauego("detector_crop_x2")
    detector_crop_y1: int = _lauego("detector_crop_y1")
    detector_crop_y2: int = _lauego("detector_crop_y2")
    min_size: float = _lauego("min_size")
    max_peaks: int = _lauego("max_peaks")
    smooth: bool = _lauego("smooth")
    mask_file: PathText | None = _lauego("mask_file")
    index_kev_max_calc: float = _lauego("index_kev_max_calc")
    index_kev_max_test: float = _lauego("index_kev_max_test")
    index_angle_tolerance: float = _lauego("index_angle_tolerance")
    index_h: int = _lauego("index_h")
    index_k: int = _lauego("index_k")
    index_l: int = _lauego("index_l")
    index_cone: float = _lauego("index_cone")
    energy_unit: str = _lauego("energy_unit")
    exposure_unit: str = _lauego("exposure_unit")
    cosmic_filter: bool = _lauego("cosmic_filter")
    reciprocal_lattice_unit: str = _lauego("reciprocal_lattice_unit")
    lattice_parameters_unit: str = _lauego("lattice_parameters_unit")
    output_xml: str | None = param(
        title="Output XML",
        source="form",
        config="indexing.defaults.output_xml",
        group=FILES,
        widget="text",
        hint="The results file's name in the run's folder. Empty: output.xml.",
        description=(
            "The Output XML file's name in the run directory, written when the run ends; not a name the run "
            "directory reserves. Empty: `output.xml`, which is stored."
        ),
    )
    geometry_file: str = _lauego("geometry_file")
    crystal_file: str = _lauego("crystal_file")
    depth: str | None = _lauego("depth")
    beamline: str = _lauego("beamline")
    author: str | None = _lauego("author")
    notes: str | None = _lauego("notes")
    algorithm_version: str | None = _lauego("algorithm_version")
    backend: Literal["local"] = live_backend_param()
    computer_name: str = _lauego("computer_name")
    priority: int = _lauego("priority")
    submitted_at: datetime = _lauego("submitted_at")
    schema_id: Literal["lauego-live/v1"] = schema_id_param(SCHEMA_ID)

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

    @field_validator("depth_range")
    @classmethod
    def _depth_range(cls, value: str | None) -> str | None:
        return count_optional_integer_range(value, "depth range")[0]

    @field_validator("geometry_file")
    @classmethod
    def _geometry_file(cls, value: str) -> str:
        return required_path(value, "Geometry file")

    @field_validator("crystal_file")
    @classmethod
    def _crystal_file(cls, value: str) -> str:
        return required_path(value, "Crystal file")

    @field_validator("mask_file")
    @classmethod
    def _mask_file(cls, value: str | None) -> str | None:
        return optional_text(value)

    @field_validator("output_xml")
    @classmethod
    def _output_xml(cls, value: str | None) -> str:
        value = optional_text(value) or DEFAULT_OUTPUT_XML  # as lauego/v1: blank is the default name
        check_live_output_xml_name(value)
        return value

    @field_validator("depth", mode="before")
    @classmethod
    def _depth(cls, value: Any) -> str | None:
        return depth_text(value)

    @property
    def depth_values(self) -> tuple[int, ...] | None:
        """The depth indices, expanded; ``None`` without a depth range (derived: not in ``request.json``)."""
        return normalize_optional_integer_range(self.depth_range, "depth range")[1]

    @property
    def depth_count(self) -> int | None:
        """How many depth indices there are, without expanding them; ``None`` without a depth range."""
        return count_optional_integer_range(self.depth_range, "depth range")[1]
