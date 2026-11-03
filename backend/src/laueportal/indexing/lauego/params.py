"""LaueGo indexing parameters, ``lauego/v1``.

``LaueGoParamsV1`` has exactly the fields of a LaueGo indexing request, in their order, plus
``schema_id``; ``request.json`` stores them (``jobs.rundir.request_document``). It normalizes each value and
checks the per-field rules; where each value comes from (the form, config, or
the portal) is in ``core.params``.
"""

import os
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
    depth_text,
    notes_param,
    optional_text,
    param,
    priority_param,
    required_path,
    schema_id_param,
    submitted_at_param,
)
from laueportal.files.inputs import (
    WorkflowValidationError,
    count_integer_range,
    count_optional_integer_range,
    normalize_filename_templates,
    normalize_input_directory,
    normalize_integer_range,
    normalize_optional_integer_range,
    normalize_output_path_template,
)
from laueportal.jobs.rundir import PARTIAL_SUFFIX, RESERVED_RUN_FILENAMES, RESERVED_RUN_PREFIXES

SCHEMA_ID = "lauego/v1"
DEFAULT_OUTPUT_XML = "output.xml"

INDICES_PLACEHOLDER = "1-10,15"
AUTOMATIC = "Automatic"
TEMPLATE_PLACEHOLDER = "Si_%d.h5"
XML = (".xml",)
PEAKS = "Peak Search Parameters"
INDEXING = "Indexing Parameters"
FILES = "File Paths"


def check_output_xml_name(name: str) -> None:
    """Refuse an Output XML name the run must not create.

    - A relative name is joined to the run directory and stays inside it: no ``..`` component.
    - The last component is the file's name: not empty (a trailing ``/``), ``.`` or ``..``.
    - The file's name is not one of ``RESERVED_RUN_FILENAMES``, does not start with one of
      ``RESERVED_RUN_PREFIXES`` (the portal's ``.bdp-*`` and ``.cancel-*`` markers) and does not end in
      ``.partial`` (a file still being written). For a relative name the same holds for every
      directory on the way: ``lauego`` creates them in the output directory, whose entries are
      published into the run directory, so ``incoming/x.xml`` would collide with the return inbox.
      An absolute name is used as given; only its file name is checked.
    """

    components = name.split(os.sep)
    relative = not os.path.isabs(name)
    if relative and os.pardir in components:
        raise WorkflowValidationError(
            f"Output XML name {name!r} must stay inside the run directory: a relative name may not contain {os.pardir!r}"
        )
    if components[-1] in ("", os.curdir, os.pardir):
        raise WorkflowValidationError(
            f"Output XML name {name!r} must end in a file name, not {components[-1] or os.sep!r}"
        )
    checked = [part for part in components if part not in ("", os.curdir)] if relative else components[-1:]
    for part in checked:
        where = f"Output XML name {name!r}" + ("" if part == name else f": {part!r}")
        if part in RESERVED_RUN_FILENAMES or part.startswith(RESERVED_RUN_PREFIXES):
            raise WorkflowValidationError(f"{where} is reserved for run support files")
        if part.endswith(PARTIAL_SUFFIX):
            raise WorkflowValidationError(
                f"{where} is reserved: names ending in {PARTIAL_SUFFIX!r} are for files still being written"
            )


def _defaults(name: str) -> str:
    return f"indexing.defaults.{name}"


def _crop(name: str, index: int) -> Any:
    return param(
        title=f"Detector Crop {name}",
        source="config",
        unit="px",
        config=f"indexing.fixed.detector_crop[{index}]",
        description="The detector crop (x1, x2, y1, y2), stored for the record: compute does not use it.",
    )


def _unit(title: str, name: str) -> Any:
    return param(
        title=title,
        source="config",
        config=f"indexing.fixed.{name}",
        description="Stored for the record: compute does not use it.",
    )


def _hkl(axis: str, index: int) -> Any:
    return param(
        title=f"Central HKL: {axis}",
        source="form",
        config=f"indexing.defaults.index_hkl[{index}]",
        group=INDEXING,
        widget="hkl",
        hint="The reflection the indexer prefers at the centre of the cone.",
        description=(
            f"The {axis} of the Central HKL (one HKL widget of three integers): the reflection the indexer "
            "prefers at the centre of the cone (lauelab `hkl_prefer`)."
        ),
    )


class LaueGoParamsV1(ProgramParams):
    """The parameters of one LaueGo indexing run (`lauego/v1`)."""

    form_layout = (  # "Run on" sits by Validate and Submit
        ("Identity", ("scan_number", "reconstruction_id", "author")),
        (
            FILES,
            (
                "input_path",
                "filename_prefixes",
                "scan_points",
                "depth_range",
                "geometry_file",
                "crystal_file",
                "output_path_template",
                "output_xml",
            ),
        ),
        (
            PEAKS,
            (
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
            ),
        ),
        (
            INDEXING,
            (
                "index_kev_max_calc",
                "index_kev_max_test",
                "index_angle_tolerance",
                "index_h",
                "index_k",
                "index_l",
                "index_cone",
                "max_peaks",
                "depth",
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
        hint="The scan the run belongs to. Empty: the reconstruction's scan, or an unlinked run.",
        description=(
            "The scan the run belongs to; empty for an unlinked run, which then takes "
            "its reconstruction's scan, if any."
        ),
    )
    reconstruction_id: int | None = param(
        title="Reconstruction ID",
        source="form",
        group="Identity",
        widget="reconstruction",
        hint="The reconstruction whose depth-resolved frames are indexed. Empty: a folder of frame files.",
        description=(
            "The reconstruction whose depth-resolved frames the run indexes (its `scan.h5` is "
            "the input); empty for a folder of frame files."
        ),
    )
    input_path: PathText = param(
        title="Folder Path",
        source="form",
        group=FILES,
        widget="folder",
        hint="The folder of the frame files, or a reconstruction's scan.h5.",
        description="The folder of the frame files, or a reconstruction's catalog (its `scan.h5`).",
    )
    output_path_template: PathText = param(
        title="Output Path",
        source="form",
        config="output_templates.indexing",
        group=FILES,
        widget="output-folder",
        hint="The run's folder; %d becomes the run ID.",
        description=(
            "The run directory, with exactly one `%d`, the run ID. The default is "
            "`[output_templates] indexing` under `files.runs_root`, without the segments of a scan or reconstruction "
            "the run does not have."
        ),
    )
    filename_prefixes: Sequence[FilenameTemplate] = param(
        title="Filename",
        source="form",
        group=FILES,
        widget="filename-templates",
        hint="Frame file names, comma-separated: %d is the scan index, a second %d the depth index, * any text (e.g. Si_*%d.h5).",
        placeholder=TEMPLATE_PLACEHOLDER,
        min_length=1,
        max_length=TEMPLATES_MAX_ITEMS,
        description=(
            "The frame files' name templates: `%d` takes the scan index, a second "
            "`%d` the depth index; wildcards are allowed. At least one, at most 100, each at most 255 characters; "
            "blank ones are dropped."
        ),
    )
    threshold: int | None = param(
        title="Threshold",
        source="form",
        config=_defaults("threshold"),
        unit="counts",
        group=PEAKS,
        widget="integer",
        hint="The peak search's intensity threshold. Empty: automatic, from the Threshold Ratio.",
        placeholder=AUTOMATIC,
        ge=0,
        description="The peak search's absolute intensity threshold; empty: automatic (from the Threshold Ratio).",
    )
    threshold_ratio: float | None = param(
        title="Threshold Ratio",
        source="form",
        config=_defaults("threshold_ratio"),
        group=PEAKS,
        widget="number",
        hint="The automatic threshold, in standard deviations of the image. Empty or 0: 4.",
        placeholder=AUTOMATIC,
        ge=0,
        description=(
            "The automatic threshold as a multiple of the image's standard deviation (lauelab "
            "`PeakParams.threshold_ratio`, a float); empty or 0: lauelab's default (4.0)."
        ),
    )
    max_rfactor: float = param(
        title="Max R-factor",
        source="form",
        config=_defaults("max_rfactor"),
        group=PEAKS,
        widget="number",
        hint="The largest R-factor of a fitted peak that is kept.",
        ge=0,
        le=1,
        description="The largest R-factor of a fitted peak that is kept.",
    )
    box_size: int = param(
        title="Box Size",
        source="form",
        config=_defaults("box_size"),
        unit="px",
        group=PEAKS,
        widget="integer",
        hint="The side of the box each peak is fitted in.",
        gt=0,
        description="The side of the box each peak is fitted in.",
    )
    max_number: int | None = param(
        title="Max detected spots",
        source="form",
        config=_defaults("max_number"),
        group=PEAKS,
        widget="integer",
        hint="The most peaks kept per frame. Empty: all.",
        placeholder="All",
        gt=0,
        description="The most peaks the peak search keeps per frame; empty: all.",
    )
    min_separation: int = param(
        title="Min Spot Separation",
        source="form",
        config=_defaults("min_separation"),
        unit="px",
        group=PEAKS,
        widget="integer",
        hint="The smallest distance between two peaks.",
        ge=0,
        description="The smallest distance between two peaks.",
    )
    peak_shape: Literal["Lorentzian", "Gaussian"] = param(
        title="Peak Shape",
        source="form",
        config=_defaults("peak_shape"),
        group=PEAKS,
        widget="select",
        hint="The profile fitted to each peak.",
        options={"Lorentzian": "Lorentzian", "Gaussian": "Gaussian"},
        description="The profile fitted to each peak (lauelab's peak models).",
    )
    scan_points: str = param(
        title="Scan Indices",
        source="form",
        config=_defaults("scan_points"),
        group=FILES,
        widget="indices",
        hint="The scan points to index, e.g. 1-10, 1,5,8,9, 1-4,10-21 or 1-10:2 (every second).",
        placeholder=INDICES_PLACEHOLDER,
        description=(
            "The scan points to index: srange syntax, at least one; stored as srange writes it (`3,1-2` is `1-3`)."
        ),
    )
    depth_range: str | None = param(
        title="Depth Indices",
        source="form",
        config=_defaults("depth_range"),
        group=FILES,
        widget="indices",
        hint="The depth indices to index, e.g. 0-90:30. Empty: every depth of a reconstruction; none for names with one %d.",
        description=(
            "The depth indices to index (srange syntax), for a reconstruction's frames or file names with two `%d`; "
            "empty: every depth of a reconstruction's `scan.h5`, none for file names with one `%d`."
        ),
    )
    detector_crop_x1: int = _crop("X1", 0)
    detector_crop_x2: int = _crop("X2", 1)
    detector_crop_y1: int = _crop("Y1", 2)
    detector_crop_y2: int = _crop("Y2", 3)
    min_size: float = param(
        title="Min Spot Size",
        source="form",
        config=_defaults("min_size"),
        unit="px",
        group=PEAKS,
        widget="number",
        hint="The smallest peak kept.",
        gt=0,
        description="The smallest peak kept (LaueGo `peaksearch -m`), used as given: a fraction is not rounded.",
    )
    max_peaks: int = param(
        title="Max spots to index",
        source="form",
        config=_defaults("max_peaks"),
        group=INDEXING,
        widget="integer",
        hint="How many of a frame's peaks the indexer uses.",
        gt=0,
        description="How many of a frame's peaks the indexer uses (lauelab `IndexParams.max_data`).",
    )
    smooth: bool = param(
        title="Smooth peak before fitting",
        source="form",
        config=_defaults("smooth"),
        group=PEAKS,
        widget="checkbox",
        hint="Smooth each peak before it is fitted.",
        description="Smooth each peak before it is fitted.",
    )
    mask_file: PathText | None = param(
        title="Mask File",
        source="form",
        config=_defaults("mask_file"),
        group=PEAKS,
        widget="file",
        hint="The pixels the peak search ignores (an HDF5 image). Empty: none.",
        extensions=(".h5", ".hdf5"),
        description="A detector mask for the peak search, inside the roots; empty: none.",
    )
    index_kev_max_calc: float = param(
        title="Max Calc Energy",
        source="form",
        config=_defaults("index_kev_max_calc"),
        unit="keV",
        group=INDEXING,
        widget="number",
        hint="The highest energy of the reflections the indexer calculates.",
        gt=0,
        description=(
            "The highest energy of the reflections the indexer calculates. The scan's source energy does not "
            "replace it (a white-beam header's energy is not an indexing cutoff)."
        ),
    )
    index_kev_max_test: float = param(
        title="Max Test Energy",
        source="form",
        config=_defaults("index_kev_max_test"),
        unit="keV",
        group=INDEXING,
        widget="number",
        hint="The highest energy of the reflections the indexer tests.",
        gt=0,
        description="The highest energy of the reflections the indexer tests (not replaced by the scan's energy).",
    )
    index_angle_tolerance: float = param(
        title="Angle Tolerance",
        source="form",
        config=_defaults("index_angle_tolerance"),
        unit="deg",
        group=INDEXING,
        widget="number",
        hint="The largest angle between a measured and a calculated reflection that still match.",
        ge=0,
        description="The largest angle between a measured and a calculated reflection that still match.",
    )
    index_h: int = _hkl("H", 0)
    index_k: int = _hkl("K", 1)
    index_l: int = _hkl("L", 2)
    index_cone: float = param(
        title="Cone Angle",
        source="form",
        config=_defaults("index_cone"),
        unit="deg",
        group=INDEXING,
        widget="number",
        hint="The opening angle of the cone around the Central HKL in which reflections are matched.",
        ge=0,
        le=180,
        description="The opening angle of the cone around the Central HKL in which reflections are matched.",
    )
    energy_unit: str = _unit("Energy Unit", "energy_unit")
    exposure_unit: str = _unit("Exposure Unit", "exposure_unit")
    cosmic_filter: bool = param(
        False,
        title="Cosmic Filter",
        source="derived",
        description="Off: every new run stores `false`, and compute never filters.",
    )
    reciprocal_lattice_unit: str = _unit("Reciprocal Lattice Unit", "reciprocal_lattice_unit")
    lattice_parameters_unit: str = _unit("Lattice Parameters Unit", "lattice_parameters_unit")
    output_xml: str | None = param(
        title="Output XML",
        source="form",
        config=_defaults("output_xml"),
        group=FILES,
        widget="text",
        hint="The results file: a name inside the run's folder, or an absolute path. Empty: output.xml.",
        description=(
            "The Output XML file: a relative name is inside the run directory, an absolute one is used as given; "
            "not a name the run directory reserves. Empty: `output.xml`, which is stored."
        ),
    )
    geometry_file: PathText = param(
        title="Geometry File",
        source="form",
        config=_defaults("geometry_file"),
        group=FILES,
        widget="file",
        hint="The detector geometry (XML).",
        extensions=XML,
        description="The detector geometry (XML); it must exist inside the roots.",
    )
    crystal_file: PathText = param(
        title="Structures File",
        source="form",
        config=_defaults("crystal_file"),
        group=FILES,
        widget="file",
        hint="The crystal structure (XML).",
        extensions=XML,
        description="The crystal structure (XML); it must exist inside the roots.",
    )
    depth: str | None = param(
        title="Depth",
        source="form",
        config=_defaults("depth"),
        unit="µm",
        group=INDEXING,
        widget="text",
        hint="One depth for every frame. Empty: each frame's own.",
        placeholder=AUTOMATIC,
        description=(
            "A depth that replaces every frame's; empty: each frame's own. A finite number, stored as typed (text); "
            "other text, NaN and infinity are refused."
        ),
    )
    beamline: str = _unit("Beamline", "beamline")
    author: str | None = author_param(_defaults("author"))
    notes: str | None = notes_param(_defaults("notes"))
    algorithm_version: str | None = algorithm_version_param()
    backend: Backend = backend_param()
    computer_name: str = computer_name_param()
    priority: int = priority_param()
    submitted_at: datetime = submitted_at_param()
    schema_id: Literal["lauego/v1"] = schema_id_param(SCHEMA_ID)

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
        return normalize_filename_templates(value)

    @field_validator("scan_points")
    @classmethod
    def _scan_points(cls, value: str) -> str:
        return count_integer_range(value, "scan points")[0]  # counted, not expanded

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
        return required_path(value, "Structures file")

    @field_validator("mask_file")
    @classmethod
    def _mask_file(cls, value: str | None) -> str | None:
        return optional_text(value)

    @field_validator("output_xml")
    @classmethod
    def _output_xml(cls, value: str | None) -> str:
        value = optional_text(value) or DEFAULT_OUTPUT_XML  # blank is the default name
        check_output_xml_name(value)
        return value

    @field_validator("depth", mode="before")
    @classmethod
    def _depth(cls, value: Any) -> str | None:
        return depth_text(value)

    @property
    def scan_point_values(self) -> tuple[int, ...]:
        """The scan points, expanded (derived: not in ``request.json``)."""
        return normalize_integer_range(self.scan_points, "scan points")[1]

    @property
    def depth_values(self) -> tuple[int, ...] | None:
        """The depth indices, expanded; ``None`` without a depth range (derived: not in ``request.json``)."""
        return normalize_optional_integer_range(self.depth_range, "depth range")[1]

    @property
    def scan_point_count(self) -> int:
        """How many scan points there are, without expanding them (derived: not in ``request.json``)."""
        return count_integer_range(self.scan_points, "scan points")[1]

    @property
    def depth_count(self) -> int | None:
        """How many depth indices there are, without expanding them; ``None`` without a depth range."""
        return count_optional_integer_range(self.depth_range, "depth range")[1]
