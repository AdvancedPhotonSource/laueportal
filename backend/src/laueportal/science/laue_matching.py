"""The crystal and detector values LaueMatching reads, from the geometry and crystal files, through lauelab."""

from __future__ import annotations

from dataclasses import dataclass

DETECTOR_INDEX = 0
"""The detector slot every run uses (as ``indexing.lauego.compute``); the form has no detector control."""
_MICROMETRES_TO_METRES = 1e-6
_METRE_DECIMALS = 12
"""A length in metres is rounded to this many places, so a value in micrometres becomes the same double on every run:
a forward cache is keyed on the raw doubles."""

_F_CENTRED = {22, 42, 43, 69, 70, 196, 202, 203, 209, 210, 216, 219, 225, 226, 227, 228}
_I_CENTRED = {
    23, 24, 44, 45, 46, 71, 72, 73, 74, 79, 80, 82, 87, 88, 97, 98, 107, 108, 109, 110,
    119, 120, 121, 122, 139, 140, 141, 142, 197, 199, 204, 206, 211, 214, 217, 220, 229, 230,
}  # fmt: skip
_R_CENTRED = {146, 148, 155, 160, 161, 166, 167}
_A_CENTRED = {38, 39, 40, 41}
_C_CENTRED = {5, 8, 9, 12, 15, 20, 21, 35, 36, 37, 63, 64, 65, 66, 67, 68}


def centring_letter(space_group: int) -> str:
    """The lattice centring of space group number ``space_group`` (the first letter of its Hermann-Mauguin symbol)."""
    for letter, groups in (
        ("F", _F_CENTRED),
        ("I", _I_CENTRED),
        ("R", _R_CENTRED),
        ("A", _A_CENTRED),
        ("C", _C_CENTRED),
    ):
        if space_group in groups:
            return letter
    return "P"


@dataclass(frozen=True)
class Instrument:
    """What LaueMatching reads of a run's crystal and detector, in its units (m, nm, degrees, radians)."""

    space_group: int
    symmetry: str
    lattice_parameters: tuple[float, ...]
    p_array: tuple[float, ...]
    r_array: tuple[float, ...]
    px_x: float
    px_y: float
    nr_px_x: int
    nr_px_y: int
    detector_id: str


def _metres(micrometres: float) -> float:
    return round(float(micrometres) * _MICROMETRES_TO_METRES, _METRE_DECIMALS)


def read_instrument(geometry_file: str, crystal_file: str) -> Instrument:
    """The values for ``geometry_file``'s first detector and ``crystal_file``. Raises what lauelab raises for a file it
    cannot read (``OSError``, ``ValueError``)."""
    from lauelab.indexing import load_crystal, load_geometry

    detector = load_geometry(geometry_file).detector(DETECTOR_INDEX)
    crystal = load_crystal(crystal_file)
    cell = crystal.cell
    nanometres = {"nm": 1.0, "angstrom": 0.1, "A": 0.1, "micron": 1000.0}[cell.unit]
    return Instrument(
        space_group=crystal.space_group,
        symmetry=centring_letter(crystal.space_group),
        lattice_parameters=(
            float(cell.a) * nanometres,
            float(cell.b) * nanometres,
            float(cell.c) * nanometres,
            float(cell.alpha),
            float(cell.beta),
            float(cell.gamma),
        ),
        p_array=tuple(_metres(value) for value in detector.translation),
        r_array=tuple(float(value) for value in detector.rotation_vector),
        px_x=_metres(detector.size_x / detector.nx),
        px_y=_metres(detector.size_y / detector.ny),
        nr_px_x=int(detector.nx),
        nr_px_y=int(detector.ny),
        detector_id=detector.detector_id,
    )
