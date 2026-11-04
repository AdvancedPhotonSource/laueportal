"""The results viewer's products: the colour map, the pole figure, the colour keys, a point's
details and a selection's misorientation, computed by lauelab (``science.products``) on the dataset
``indexing.viewer.viewer_dataset`` loads.

- **Parameters** come as query text (``axes=X,H``, ``surface=normal`` or nine numbers, ``pole_hkl=1,0,0``, ...);
  ``parse_*`` turn them into ``science.products``' requests, and a malformed one is a 422 that locates it.
- **A failing product** (lauelab cannot make it for these results and settings: a non-cubic crystal for a cubic
  mode, a reference without an orientation, a singular G_ref, ...) is a 422 problem of its own type,
  ``urn:laueportal:problem:product-failed``, with lauelab's (or the portal's) message: the page keeps its last good plot
  and shows the message as a "stale" warning.
- **Caching:** ``tag`` gives each product's entity tag (``core.etag``): the dataset's identity
  (``indexing.viewer.viewer_source``, before the load), the scope and the product's parameters.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from laueportal.core import etag
from laueportal.core.problems import ProblemError, ProblemItem, problems_detail
from laueportal.core.text import quoted, shortened
from laueportal.indexing.schemas import (
    AxisCoordinate,
    CrystalCellDetails,
    PatternDetails,
    PointDetails,
    SelectionStats,
)
from laueportal.indexing.viewer import ViewerDataset, ViewerSource
from laueportal.science import products as science
from laueportal.science.products import MapRequest, PoleFigureRequest, ProductError, RodriguesReference, Surface
from laueportal.science.viewer import DataScope

PRODUCT_FAILED = "urn:laueportal:problem:product-failed"
"""``ProductFailed``'s problem type."""


class ProductFailed(ProblemError):
    """lauelab cannot make the product for these results and settings (a 422 with its message)."""

    def __init__(self, product: str, error: ProductError) -> None:
        super().__init__(
            422, "Product failed", f"No {product}: {shortened(str(error), 600)}", problem_type=PRODUCT_FAILED
        )


class StepNotFound(ProblemError):
    def __init__(self, loaded: ViewerDataset, step: int) -> None:
        super().__init__(
            404,
            "Step not found",
            f"I{loaded.indexing_id}'s results have steps 0 to {loaded.dataset.n_frames - 1}, not {step}",
        )


def invalid(name: str, message: str, code: str = "invalid_value") -> ProblemError:
    """A 422 locating the query parameter ``name``."""

    return ProblemError(
        422,
        "Validation failed",
        problems_detail(1),
        errors=[ProblemItem(loc=["query", name], message=message, code=code)],
    )


# ---------------------------------------------------------------------------
# Parameters


def _parts(text: str) -> list[str]:
    return [part.strip() for part in text.split(",")]


def _floats(name: str, text: str, count: int, what: str) -> tuple[float, ...]:
    parts = _parts(text)
    try:
        values = tuple(float(part) for part in parts)
    except ValueError:
        values = ()
    if len(parts) != count or len(values) != count or not all(math.isfinite(value) for value in values):
        raise invalid(name, f"Expected {what}, separated by commas; got {quoted(text)}")
    return values


def _integers(name: str, text: str, count: int, what: str, *, minimum: int | None = None) -> tuple[int, ...]:
    parts = _parts(text)
    values = tuple(int(part) for part in parts if _is_integer(part))
    if len(parts) != count or len(values) != count or (minimum is not None and min(values) < minimum):
        raise invalid(name, f"Expected {what}, separated by commas; got {quoted(text)}")
    return values


def _is_integer(text: str) -> bool:
    digits = text[1:] if text[:1] in "+-" else text
    return 0 < len(digits) <= 18 and digits.isascii() and digits.isdigit()


def parse_axes(text: str, name: str = "axes") -> tuple[str, ...]:
    """Two or three of lauelab's axis names (``AXIS_OPTIONS``), e.g. ``X,H`` or ``X,Y,Z``."""

    axes = tuple(_parts(text))
    if len(axes) not in (2, 3) or not set(axes) <= set(science.AXES):
        raise invalid(
            name, f"Expected two or three of {', '.join(science.AXES)}, separated by commas; got {quoted(text)}"
        )
    return axes


def parse_surface(text: str, name: str = "surface") -> Surface:
    """A preset (``normal``, ``X``, ``H``, ``Y``, ``Z``, ``F``) or nine numbers: a custom frame's tilt, roll and
    normal vectors (the Custom surface)."""

    if text in science.SURFACES:
        return text
    return _floats(name, text, 9, f"one of {', '.join(science.SURFACES)}, or nine numbers (tilt, roll, normal)")


def parse_hkl(text: str, name: str) -> tuple[int, int, int]:
    """Three integers, not all zero."""

    h, k, l = _integers(name, text, 3, "three integers h, k, l")  # noqa: E741 (Miller indices)
    if (h, k, l) == (0, 0, 0):
        raise invalid(name, "HKL cannot be 0,0,0")
    return h, k, l


def parse_point(text: str, name: str) -> tuple[float, float]:
    """Two finite numbers: a position on the pole figure."""

    x, y = _floats(name, text, 2, "two numbers x, y")
    return x, y


def parse_rodrigues_reference(text: str, name: str = "rodrigues_ref") -> RodriguesReference:
    """``lab``, a step (its pattern 0 is the reference), or nine numbers: a custom G_ref, rows a*, b*,
    c* in 1/nm including 2π."""

    if text == "lab":
        return "lab"
    if _is_integer(text) and int(text) >= 0:
        return int(text)
    return _floats(name, text, 9, "lab, a step, or nine numbers (G_ref rows a*, b*, c* in 1/nm)")


def parse_pattern(text: str, name: str = "ref_pattern") -> tuple[int, int]:
    """A pattern as ``step,pattern_index``."""

    step, pattern_index = _integers(name, text, 2, "a pattern as step, pattern_index", minimum=0)
    return step, pattern_index


def parse_limit(value: float | None, name: str) -> float | None:
    if value is not None and not math.isfinite(value):
        raise invalid(name, f"Expected a finite number; got {quoted(value)}")
    return value


def parse_radius(value: float, name: str) -> float:
    if not (math.isfinite(value) and 0 < value <= 180):
        raise invalid(name, f"Expected an angle in degrees above 0 and at most 180; got {quoted(value)}")
    return value


def parse_energy_range(text: str | None, window: tuple[float, float], name: str = "kev") -> tuple[float, float]:
    """``low,high`` in keV (lauelab's simulation window), increasing and within ``window`` (the beamline's, 6 to 30 keV
    by default), which a request may narrow but not widen: the simulation's cost grows steeply with the window (28 s a
    pattern at 6 to 60 keV against 4.5 s). None: ``window``."""

    if text is None:
        return window
    low, high = _floats(name, text, 2, "two energies in keV")
    if not (window[0] <= low < high <= window[1]):
        raise invalid(
            name,
            f"Expected {window[0]:g} <= low < high <= {window[1]:g} (keV, the simulation's window); got {quoted(text)}",
        )
    return low, high


# ---------------------------------------------------------------------------
# Products


def tag(source: ViewerSource, product: str, *parts: object) -> str:
    """The entity tag of ``product`` made from ``source`` with ``parts`` (the scope and the parameters), before
    its dataset is loaded."""

    return etag.entity_tag(product, source.identity, *parts)


def map_product(loaded: ViewerDataset, scope: DataScope, request: MapRequest) -> bytes:
    try:
        return science.map_stream(loaded.dataset, scope, request)
    except ProductError as error:
        raise ProductFailed("map", error) from error


def pole_figure_product(loaded: ViewerDataset, scope: DataScope, request: PoleFigureRequest) -> bytes:
    try:
        return science.pole_figure_stream(loaded.dataset, scope, request)
    except ProductError as error:
        raise ProductFailed("pole figure", error) from error


def legend_product(color: str) -> bytes:
    return science.legend_png(color)


def point_product(loaded: ViewerDataset, scope: DataScope, step: int, axes: tuple[str, ...]) -> PointDetails:
    if step >= loaded.dataset.n_frames:
        raise StepNotFound(loaded, step)
    try:
        point = science.point_details(loaded.dataset, scope, step, axes)
    except ProductError as error:
        raise ProductFailed("point details", error) from error
    return PointDetails(
        step=point.step,
        frame_id=point.frame_id,
        sample_position=list(point.sample_position),
        depth=point.depth,
        n_peaks=point.n_peaks,
        in_scope=point.in_scope,
        patterns=[
            PatternDetails(
                pattern_index=pattern.pattern_index,
                n_indexed=pattern.n_indexed,
                goodness=pattern.goodness,
                rms_error_deg=pattern.rms_error_deg,
                indexed=pattern.indexed,
                in_scope=pattern.in_scope,
                reciprocal=[list(row) for row in pattern.reciprocal] if pattern.reciprocal else None,
            )
            for pattern in point.patterns
        ],
        coordinates=[
            AxisCoordinate(axis=value.axis, label=value.label, value=value.value) for value in point.coordinates
        ],
        crystal=(
            CrystalCellDetails(
                name=point.crystal.name,
                space_group=point.crystal.space_group,
                lengths_nm=list(point.crystal.lengths_nm),
                angles_deg=list(point.crystal.angles_deg),
                reference_reciprocal=[list(row) for row in point.crystal.reference_reciprocal],
                symmetry=point.crystal.symmetry,
                equivalent_settings=[[list(row) for row in setting] for setting in point.crystal.equivalent_settings],
            )
            if point.crystal
            else None
        ),
    )


def selection_product(loaded: ViewerDataset, patterns: Sequence[Sequence[int]]) -> SelectionStats:
    """422 when a pair is not a pattern of the results (each located), or when more than 700 distinct patterns
    with an orientation are selected (``science.products.MAX_SELECTION_PATTERNS``)."""

    pairs = [(pattern[0], pattern[1]) for pattern in patterns]
    unknown = science.unknown_patterns(loaded.dataset, pairs)
    if unknown:
        raise ProblemError(
            422,
            "Validation failed",
            problems_detail(len(unknown)),
            errors=[
                ProblemItem(
                    loc=["patterns", index],
                    message=f"I{loaded.indexing_id}'s results have no pattern {pairs[index][1]} at step "
                    f"{pairs[index][0]}",
                    code="unknown_pattern",
                )
                for index in unknown
            ],
        )
    try:
        stats = science.selection_stats(loaded.dataset, pairs)
    except science.SelectionTooLarge as error:
        raise ProblemError(
            422,
            "Validation failed",
            problems_detail(1),
            errors=[ProblemItem(loc=["patterns"], message=f"I{loaded.indexing_id}: {error}", code="too_many_patterns")],
        ) from error
    return SelectionStats(
        n_patterns=stats.n_patterns,
        n_oriented=stats.n_oriented,
        n_pairs=stats.n_pairs,
        mean=stats.mean,
        min=stats.min,
        max=stats.max,
        symmetry=stats.symmetry,
    )
