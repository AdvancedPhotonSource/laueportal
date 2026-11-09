"""LaueMatching indexing parameters, ``laue-matching/v1``.

``LaueMatchingParamsV1`` has exactly the keys of a LaueMatching run's ``request.json`` ``request``, plus ``schema_id``.
It shares the source and file fields with ``lauego/v1``. The crystal and detector values the indexer reads are derived
at run creation from the geometry and crystal files, and frozen here, so a run's ``request.json`` gives them even if
those files later change. Where each value comes from (the form, config, or the portal) is in ``core.params``.
"""

import copy
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import field_validator

from laueportal.core.params import (
    FilenameTemplate,
    PathText,
    ProgramParams,
    algorithm_version_param,
    author_param,
    computer_name_param,
    notes_param,
    optional_text,
    param,
    priority_param,
    required_path,
    schema_id_param,
    shared_field,
    submitted_at_param,
)
from laueportal.files.inputs import (
    count_integer_range,
    count_optional_integer_range,
    normalize_filename_templates,
    normalize_input_directory,
    normalize_integer_range,
    normalize_optional_integer_range,
    normalize_output_path_template,
)
from laueportal.indexing.laue_matching.cache import name_problem
from laueportal.indexing.lauego.params import FILES, LaueGoParamsV1

SCHEMA_ID = "laue-matching/v1"
CACHE = "Forward Cache"
SPOTS = "Spot Finding"
MATCHING = "Indexing Parameters"
ThresholdMethod = Literal["percentile", "adaptive", "otsu", "fixed"]


def _defaults(name: str) -> str:
    return f"laue_matching.defaults.{name}"


def _shared(name: str, *, group: str | None = None) -> Any:
    """``lauego/v1``'s field ``name``; in the form section ``group`` when given."""
    field = shared_field(LaueGoParamsV1, name)
    if group is not None:
        field.json_schema_extra = {**copy.deepcopy(field.json_schema_extra), "x-group": group}
    return field


def _instrument(title: str, description: str) -> Any:
    return param(None, title=title, source="derived", description=description)


class LaueMatchingParamsV1(ProgramParams):
    """The parameters of one LaueMatching indexing run (`laue-matching/v1`)."""

    form_layout = (
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
                "mask_file",
                "output_path_template",
            ),
        ),
        (CACHE, ("create_forward_cache", "forward_cache")),
        (
            SPOTS,
            (
                "threshold_method",
                "threshold_percentile",
                "threshold_value",
                "min_area",
                "watershed_image",
                "background_file",
            ),
        ),
        (
            MATCHING,
            (
                "elo",
                "ehi",
                "max_nr_laue_spots",
                "min_nr_spots",
                "min_good_spots",
                "min_intensity",
                "max_angle",
                "robust_filter",
            ),
        ),
        ("Notes", ("notes",)),
    )

    scan_number: int | None = _shared("scan_number")
    reconstruction_id: int | None = _shared("reconstruction_id")
    input_path: str = _shared("input_path")
    output_path_template: str = _shared("output_path_template")
    filename_prefixes: Sequence[FilenameTemplate] = _shared("filename_prefixes")
    scan_points: str = _shared("scan_points")
    depth_range: str | None = _shared("depth_range")
    geometry_file: str = _shared("geometry_file")
    crystal_file: str = _shared("crystal_file")
    mask_file: str | None = _shared("mask_file", group=FILES)

    create_forward_cache: bool = param(
        False,
        title="New Cache",
        source="form",
        group=CACHE,
        widget="forward-cache-new",
        hint="Build a new forward cache. The first frame builds it, which can take many minutes.",
        description="Whether the run builds the named forward cache (its name must be new) or reuses it.",
    )
    forward_cache: str = param(
        title="Forward Cache",
        source="form",
        group=CACHE,
        widget="forward-cache",
        hint="The forward simulation this run reads. One cache fits one geometry, crystal, energy range and spot count.",
        description="The forward cache's name: a folder in `[laue_matching] data_root`.",
        max_length=128,
    )

    threshold_method: ThresholdMethod = param(
        title="Threshold Method",
        source="form",
        config=_defaults("threshold_method"),
        group=SPOTS,
        widget="select",
        options={"percentile": "Percentile", "adaptive": "Adaptive", "otsu": "Otsu", "fixed": "Fixed"},
        hint="How pixels are separated from the background before spots are found.",
        description="The image threshold method.",
    )
    threshold_percentile: float = param(
        title="Threshold Percentile",
        source="form",
        config=_defaults("threshold_percentile"),
        unit="%",
        group=SPOTS,
        widget="number",
        hint="Used by the Percentile method: pixels above this percentile of the image are kept.",
        gt=0,
        le=100,
        description="The percentile of the image used by the `percentile` method.",
    )
    threshold_value: float = param(
        title="Threshold",
        source="form",
        config=_defaults("threshold_value"),
        unit="counts",
        group=SPOTS,
        widget="number",
        hint="Used by the Fixed method: the intensity threshold.",
        ge=0,
        description="The intensity threshold of the `fixed` method.",
    )
    min_area: int = param(
        title="Min Spot Area",
        source="form",
        config=_defaults("min_area"),
        unit="px",
        group=SPOTS,
        widget="integer",
        hint="Connected pixels fewer than this are not a spot.",
        gt=0,
        description="The fewest connected pixels that make a spot.",
    )
    watershed_image: bool = param(
        title="Split Overlapping Spots",
        source="form",
        config=_defaults("watershed_image"),
        group=SPOTS,
        widget="checkbox",
        hint="Split touching spots with a watershed before counting them.",
        description="Whether a watershed segmentation splits touching spots (LaueMatching `WatershedImage`).",
    )
    background_file: str | None = param(
        title="Background File",
        source="form",
        config=_defaults("background_file"),
        group=SPOTS,
        widget="file",
        placeholder="Automatic",
        extensions=(".bin",),
        hint="A background image for raw frames. Empty: the first frame's median. Reconstructed frames use none.",
        description="A float64 background image for raw frames; empty: the median filter of the run's first frame.",
    )

    elo: float = param(
        title="Min Energy",
        source="form",
        config=_defaults("elo"),
        unit="keV",
        group=MATCHING,
        widget="number",
        hint="The lowest photon energy simulated. Part of what the cache is built for.",
        gt=0,
        description="The lowest energy of the simulated reflections (LaueMatching `Elo`).",
    )
    ehi: float = param(
        title="Max Energy",
        source="form",
        config=_defaults("ehi"),
        unit="keV",
        group=MATCHING,
        widget="number",
        hint="The highest photon energy simulated. Part of what the cache is built for.",
        gt=0,
        description="The highest energy of the simulated reflections (LaueMatching `Ehi`).",
    )
    max_nr_laue_spots: int = param(
        title="Max Spots per Orientation",
        source="form",
        config=_defaults("max_nr_laue_spots"),
        group=MATCHING,
        widget="integer",
        hint="How many spots are simulated per orientation. Part of what the cache is built for.",
        gt=0,
        description="The spots simulated per orientation (LaueMatching `MaxNrLaueSpots`); sets the forward cache's size.",
    )
    min_nr_spots: int = param(
        title="Min Matching Spots",
        source="form",
        config=_defaults("min_nr_spots"),
        group=MATCHING,
        widget="integer",
        hint="The fewest matching spots that qualify an orientation in the first pass.",
        gt=0,
        description="The fewest matching spots of an orientation (LaueMatching `MinNrSpots`).",
    )
    min_good_spots: int = param(
        title="Min Exclusive Spots",
        source="form",
        config=_defaults("min_good_spots"),
        group=MATCHING,
        widget="integer",
        hint="The fewest spots an orientation must claim that no better orientation in the frame claimed first.",
        ge=0,
        description="The fewest winner-take-all spots of a kept orientation (LaueMatching `MinGoodSpots`).",
    )
    min_intensity: float = param(
        title="Min Intensity",
        source="form",
        config=_defaults("min_intensity"),
        unit="counts",
        group=MATCHING,
        widget="number",
        hint="The least total intensity that qualifies an orientation.",
        ge=0,
        description="The least total intensity of an orientation (LaueMatching `MinIntensity`).",
    )
    max_angle: float = param(
        title="Min Misorientation",
        source="form",
        config=_defaults("max_angle"),
        unit="deg",
        group=MATCHING,
        widget="number",
        hint="Orientations closer than this are one grain.",
        gt=0,
        description="The misorientation below which two orientations are merged (LaueMatching `MaxAngle`).",
    )
    robust_filter: bool = param(
        title="Keep Twins",
        source="form",
        config=_defaults("robust_filter"),
        group=MATCHING,
        widget="checkbox",
        hint="Keep twin-related orientations that share spots with a stronger one.",
        description="Whether the twin-aware filter is used (LaueMatching `RobustFilter`).",
    )

    author: str | None = author_param("indexing.defaults.author")
    notes: str | None = notes_param("indexing.defaults.notes")

    data_root: PathText | None = param(
        title="Data Folder",
        source="config",
        config="laue_matching.data_root",
        description="The folder holding the orientation database and the forward caches.",
    )
    orientation_spacing: float = param(
        title="Orientation Spacing",
        source="config",
        unit="deg",
        config="laue_matching.orientation_spacing",
        description="The orientation database's grid spacing (LaueMatching `OrientationSpacing`).",
    )
    filter_radius: int = param(
        title="Background Filter Size",
        source="config",
        unit="px",
        config="laue_matching.filter_radius",
        description="The median filter's size for a background computed from a raw frame (LaueMatching `FilterRadius`).",
    )
    gauss_sigma_max: float = param(
        title="Max Blur",
        source="config",
        unit="px",
        config="laue_matching.gauss_sigma_max",
        description="The cap on the matching blur's sigma (LaueMatching `GaussSigmaMax`).",
    )
    num_threads: int = param(
        title="Threads",
        source="config",
        config="laue_matching.threads",
        description="The indexer's threads for each frame. Frames are indexed one at a time.",
    )

    space_group: int | None = _instrument("Space Group", "The crystal file's space group number.")
    symmetry: str | None = _instrument("Centring", "The lattice centring letter of the space group.")
    lattice_parameters: Sequence[float] | None = _instrument("Lattice", "a, b, c (nm), alpha, beta, gamma (degrees).")
    p_array: Sequence[float] | None = _instrument("Detector Position", "The geometry file's detector translation (m).")
    r_array: Sequence[float] | None = _instrument("Detector Rotation", "The geometry file's rotation vector (radians).")
    px_x: float | None = _instrument("Pixel Size X", "The detector's pixel size along x (m).")
    px_y: float | None = _instrument("Pixel Size Y", "The detector's pixel size along y (m).")
    nr_px_x: int | None = _instrument("Pixels X", "The detector's columns, unbinned.")
    nr_px_y: int | None = _instrument("Pixels Y", "The detector's rows, unbinned.")
    detector_id: str | None = _instrument("Detector", "The geometry file's detector identifier.")
    n_orientations: int | None = _instrument("Orientations", "The orientation database's size, in orientations.")

    backend: Literal["local"] = param("local", title="Run on", source="derived", description="Always `local`.")
    algorithm_version: str | None = algorithm_version_param()
    computer_name: str = computer_name_param()
    priority: int = priority_param()
    submitted_at: datetime = submitted_at_param()
    schema_id: Literal["laue-matching/v1"] = schema_id_param(SCHEMA_ID)

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
        return count_integer_range(value, "scan points")[0]

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

    @field_validator("mask_file", "background_file")
    @classmethod
    def _optional_path(cls, value: str | None) -> str | None:
        return optional_text(value)

    @field_validator("forward_cache")
    @classmethod
    def _forward_cache(cls, value: str) -> str:
        value = value.strip()
        problem = name_problem(value)
        if problem is not None:
            raise ValueError(f"The cache name {problem}")
        return value

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
