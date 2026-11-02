"""Synthetic development data, made deterministically: the demo seed's inputs and registered runs, and the tests'.
No measured data is needed.

- ``write_wire_scan``: lauelab's synthetic wire scan, by the recipe of its
  ``tests/data/reconstruction/generate_reference.py`` (``write_input_file``): three Gaussian spots on a 34-ID-E
  detector, each switched off once the wire's leading edge passes the depth given to it (the geometry of
  ``geoN_2022-03-29_14-15-05.xml``'s detector 0 and wire), Poisson noise from a fixed seed, 81 scan images after the
  intensity map. Binned 16 (128 x 128, the default) it is that recipe's file exactly; binned finer, the same scan at
  a higher resolution (the spots keep their place and size on the detector).
- ``reconstruct_wire_scan``: lauelab's reconstruction of such a scan into a finished scan catalog
  (``scan.h5`` and ``points/<stem>.h5``), with one worker and one thread.
- ``write_indexing_map``: a LaueGo results file of a grid of map positions, made from the results of natively
  indexed frames: each position gets one of them (by a fixed grain map), rotated by a small orientation gradient and
  scaled by seeded noise. The numbers are valid input for the viewer and the tables, not a physical measurement.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import h5py
import numpy as np

SEED = 20260828
"""The recipe's seed (Poisson noise)."""
FULL_PIXELS = 2048
DETECTOR_SIZE_UM = 409.6e3
RECIPE_BINNING = 16
"""The recipe's binning (128 x 128); ``SPOTS`` are in its pixels."""
WIRE_Y_UM = 1000.0
WIRE_Z_START_UM = -80.0
WIRE_Z_STEP_UM = 2.0
N_SCAN_IMAGES = 81
WIRE_DIAMETER_UM = 101.5
SPOTS = (
    (64.0, 64.0, 2.5, 4000.0, 0.0),
    (52.0, 70.0, 2.0, 3000.0, -10.0),
    (68.0, 58.0, 3.0, 2500.0, 12.0),
)
"""(column, row, sigma in the recipe's binned pixels, amplitude, source depth in micrometres)."""
BACKGROUND = 20.0
DETECTOR_P_UM = np.array([28.720, 3.010, 513.097]) * 1e3
DETECTOR_R = np.array([-1.20127231, -1.21381742, -1.21879073])
WIRE_R = np.array([0.0045, -0.00684, -3.375e-05])
WIRE_AXIS = np.array([0.99999608, 0.0, 0.0028000002])
"""Detector 0 and the wire of ``geoN_2022-03-29_14-15-05.xml``."""

RECONSTRUCTION_SETTINGS = {
    "detector": 0,
    "resolution": 1.0,
    "workers": 1,
    "threads_per_worker": 1,
    "memory_limit_mb": 512,
    "wire_edge": "leading",
    "percent_brightest": 100.0,
}
"""``reconstruct_wire_scan``'s fixed settings (lauelab's ``reconstruct_scan``)."""


def _rodrigues(vector: np.ndarray) -> np.ndarray:
    angle = float(np.linalg.norm(vector))
    if angle == 0.0:
        return np.eye(3)
    nx, ny, nz = vector / angle
    c, s = np.cos(angle), np.sin(angle)
    c1 = 1.0 - c
    return np.array(
        [
            [c + nx * nx * c1, nx * ny * c1 - nz * s, nx * nz * c1 + ny * s],
            [nx * ny * c1 + nz * s, c + ny * ny * c1, ny * nz * c1 - nx * s],
            [nx * nz * c1 - ny * s, ny * nz * c1 + nx * s, c + nz * nz * c1],
        ]
    )


class _WireGeometry:
    """The pixel-to-depth geometry of lauelab's wire-scan program (``pixel_to_point_xyz``, ``pixel_xyz_to_depth``)."""

    def __init__(self, binning: int) -> None:
        self.binning = binning
        self.detector_rotation = _rodrigues(DETECTOR_R)
        self.wire_rotation = _rodrigues(WIRE_R)
        axis = self.wire_rotation @ WIRE_AXIS
        axis /= np.linalg.norm(axis)
        rvec = np.array([0.0, axis[2], -axis[1]])
        sin_theta = np.linalg.norm(rvec)
        rvec *= np.arcsin(sin_theta) / sin_theta
        self.rho = _rodrigues(rvec)
        self.ki = self.rho @ np.array([0.0, 0.0, 1.0])

    def pixel_xyz(self, column: np.ndarray, row: np.ndarray) -> np.ndarray:
        ci = column * self.binning + (self.binning - 1) / 2.0
        cj = row * self.binning + (self.binning - 1) / 2.0
        pitch = DETECTOR_SIZE_UM / FULL_PIXELS
        x = (ci - 0.5 * (FULL_PIXELS - 1)) * pitch + DETECTOR_P_UM[0]
        y = (cj - 0.5 * (FULL_PIXELS - 1)) * pitch + DETECTOR_P_UM[1]
        z = np.full_like(x, DETECTOR_P_UM[2])
        return np.stack([x, y, z], axis=-1) @ self.detector_rotation.T

    def wire_to_beamline(self, wire_raw: np.ndarray) -> np.ndarray:
        return (wire_raw @ self.wire_rotation.T) @ self.rho.T

    def depth(self, pixel_xyz: np.ndarray, wire_beamline: np.ndarray) -> np.ndarray:
        """The leading edge's depth for each pixel."""
        p = pixel_xyz @ self.rho.T
        dy = wire_beamline[1] - p[..., 1]
        dz = wire_beamline[2] - p[..., 2]
        radius = WIRE_DIAMETER_UM / 2.0
        tan_phi0 = dz / dy
        tan_dphi = radius / np.sqrt(dy * dy + dz * dz - radius * radius)
        tan_phi = (tan_phi0 - tan_dphi) / (1.0 + tan_phi0 * tan_dphi)
        b = p[..., 2] - p[..., 1] * tan_phi
        sz = b / (1.0 - tan_phi * self.ki[1] / self.ki[2])
        s = np.stack([self.ki[0] / self.ki[2] * sz, self.ki[1] / self.ki[2] * sz, sz], axis=-1)
        return s @ self.ki


def _wire_z(k: np.ndarray | float) -> np.ndarray:
    """The wire's z (raw positioner frame, micrometres) at which stored slice ``k`` was recorded."""
    return WIRE_Z_START_UM + WIRE_Z_STEP_UM * np.asarray(k, dtype=float)


def wire_scan_images(binning: int = RECIPE_BINNING) -> tuple[np.ndarray, np.ndarray]:
    """``(images, wire_raw)``: ``N_SCAN_IMAGES + 1`` uint16 images (the intensity map first) of
    ``2048 // binning`` square pixels, and the ``N_SCAN_IMAGES + 3`` raw wire positions (x, y, z)."""
    n = FULL_PIXELS // binning
    scale = RECIPE_BINNING / binning
    geometry = _WireGeometry(binning)
    rows, columns = np.meshgrid(np.arange(n, dtype=float), np.arange(n, dtype=float), indexing="ij")
    pixel_xyz = geometry.pixel_xyz(columns, rows)
    j = np.arange(N_SCAN_IMAGES + 3, dtype=float)
    z = _wire_z(j - 1.0)
    wire_raw = np.stack([np.zeros_like(z), np.full_like(z, WIRE_Y_UM), z], axis=-1)
    rng = np.random.default_rng(SEED)
    profiles = [
        amplitude * np.exp(-((columns - ci * scale) ** 2 + (rows - cj * scale) ** 2) / (2.0 * (sigma * scale) ** 2))
        for ci, cj, sigma, amplitude, _depth in SPOTS
    ]
    images = np.empty((N_SCAN_IMAGES + 1, n, n), dtype=np.uint16)
    for k in range(N_SCAN_IMAGES + 1):
        frame = np.full((n, n), BACKGROUND)
        if k == 0:  # the intensity map: every spot on
            for profile in profiles:
                frame += profile
        else:
            here = geometry.depth(pixel_xyz, geometry.wire_to_beamline(np.array([0.0, WIRE_Y_UM, float(_wire_z(k))])))
            for profile, (*_spot, source_depth) in zip(profiles, SPOTS, strict=True):
                frame += profile * (here < source_depth)
        frame = rng.poisson(frame).astype(np.float64)
        images[k] = np.clip(frame, 0, np.iinfo(np.uint16).max).astype(np.uint16)
    return images, wire_raw


def write_wire_scan(
    path: str | os.PathLike[str],
    *,
    binning: int = RECIPE_BINNING,
    sample_position: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> Path:
    """Write the synthetic wire scan (one scan point) to ``path``; returns it."""
    images, wire_raw = wire_scan_images(binning)
    target = Path(path)
    with h5py.File(target, "w") as f:
        f.attrs["file_name"] = np.bytes_(b"synthetic_wire_scan.h5")
        f.attrs["file_time"] = np.bytes_(b"2026-08-28T12:00:00-05:00")
        facility = f.create_group("Facility")
        facility.create_dataset("facility_name", data=np.array([b"APS"], dtype="S3"))
        facility.create_dataset("facility_beamline", data=np.array([b"34ID-E"], dtype="S6"))
        entry = f.create_group("entry1")
        data = entry.create_group("data").create_dataset("data", data=images)
        data.attrs["signal"] = np.int32(1)
        entry.create_dataset("depth", data=np.array([0.0]))
        entry.create_dataset("scanNum", data=np.array([1], dtype=np.int32))
        detector = entry.create_group("detector")
        for name, value in (
            ("Nx", FULL_PIXELS),
            ("Ny", FULL_PIXELS),
            ("startx", 0),
            ("starty", 0),
            ("endx", FULL_PIXELS - 1),
            ("endy", FULL_PIXELS - 1),
            ("binx", binning),
            ("biny", binning),
        ):
            detector.create_dataset(name, data=np.array([value], dtype=np.int32))
        detector.create_dataset("exposure", data=np.array([1.0]))
        detector.create_dataset("ID", data=np.array([b"PE1621 723-3335"], dtype="S15"))
        sample = entry.create_group("sample")
        sample.create_dataset("incident_energy", data=np.array([20.0]))
        for axis, value in zip("XYZ", sample_position, strict=True):
            sample.create_dataset(f"sample{axis}", data=np.array([value]))
        wire = entry.create_group("wire")
        for name, column in (("wireX", 0), ("wireY", 1), ("wireZ", 2), ("wirescan", 2)):
            wire.create_dataset(name, data=wire_raw[:, column])
    return target


def reconstruct_wire_scan(
    scan: str | os.PathLike[str],
    geometry: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    depth_range: tuple[float, float] = (-25.0, 25.0),
    output_pixel_type: int = 5,
) -> Path:
    """Reconstruct the wire scan ``scan`` into the new directory ``destination`` (lauelab's ``reconstruct_scan``,
    ``RECONSTRUCTION_SETTINGS``); returns the point file (``points/<scan's stem>.h5``). ``output_pixel_type`` is
    lauelab's (5: float64, 3: uint16). Raises ``RuntimeError`` when the point is not complete."""
    from lauelab.reconstruct import reconstruct_scan

    outcome = reconstruct_scan(
        [os.fspath(scan)],
        os.fspath(destination),
        geometry=os.fspath(geometry),
        depth_range=depth_range,
        output_pixel_type=output_pixel_type,
        **RECONSTRUCTION_SETTINGS,
    )
    if not outcome.complete:
        raise RuntimeError(f"the synthetic reconstruction of {scan} did not complete: {outcome.outcomes}")
    return Path(destination) / "points" / f"{Path(scan).stem}.h5"


def _rotation(axis: Sequence[float], degrees: float) -> np.ndarray:
    vector = np.asarray(axis, dtype=float)
    return _rodrigues(vector / np.linalg.norm(vector) * np.radians(degrees))


def write_indexing_map(
    destination: str | os.PathLike[str],
    frames: Sequence[str | os.PathLike[str]],
    geometry: str | os.PathLike[str],
    crystal: str | os.PathLike[str],
    *,
    indexing: Mapping[str, Any],
    peaks: Mapping[str, Any],
    grid: tuple[int, int, int] = (16, 16, 4),
    step_um: tuple[float, float, float] = (2.0, 2.0, 5.0),
) -> int:
    """Write a LaueGo results file (``destination``, lauelab's format) of a ``grid`` of map positions (sample X and
    Y, then depth), ``step_um`` apart, centred on 0; returns its number of frames.

    ``frames`` are indexed natively once (lauelab's ``Indexer`` with ``peaks``, ``PeakParams``' fields, and
    ``indexing``, ``IndexParams``' fields); a position takes the result of one of them by a fixed grain map (two
    grains split by a slanted boundary, the last frame, when it has no pattern, on a few positions), its orientation
    rotated about a fixed axis by up to 4 degrees along X (an orientation gradient) and its peaks' intensities and
    patterns' goodness scaled by seeded noise. Each frame names its source frame as its input image."""
    from lauelab.indexing import Indexer, IndexParams, PeakParams

    indexer = Indexer(
        os.fspath(geometry),
        os.fspath(crystal),
        peak_params=PeakParams(**peaks),
        index_params=IndexParams(**indexing),
    )
    sources = [indexer.index(os.fspath(frame), keep_image=False) for frame in frames]
    indexed = [result for result in sources if result.patterns]
    empty = [result for result in sources if not result.patterns]
    if len(indexed) < 2:
        raise ValueError("write_indexing_map needs two frames that index")
    rng = np.random.default_rng(SEED)
    nx, ny, nz = grid
    sx, sy, sz = step_um
    count = 0
    with indexer.results_writer(Path(destination), overwrite=True) as writer:
        for iz in range(nz):
            for iy in range(ny):
                for ix in range(nx):
                    x, y, depth = (ix - (nx - 1) / 2) * sx, (iy - (ny - 1) / 2) * sy, (iz - (nz - 1) / 2) * sz
                    if empty and (ix * 7 + iy * 3 + iz) % 23 == 0:
                        base = empty[0]
                    else:
                        base = indexed[0] if ix + 0.5 * iy < nx * 0.6 else indexed[1]
                    turn = _rotation((1.0, 2.0, 3.0), 4.0 * ix / max(nx - 1, 1) + rng.normal(0.0, 0.05))
                    result = _moved(base, turn, rng, position=(x, y, 0.0), depth=depth)
                    writer.append(result, frame_id=f"map_{count + 1}")
                    count += 1
    return count


def _moved(base, turn: np.ndarray, rng: np.random.Generator, *, position: tuple[float, float, float], depth: float):
    """``base`` (a lauelab ``FrameResult``) at ``position`` and ``depth``, its crystal turned by ``turn``."""
    peaks = base.peaks.copy()
    if len(peaks):
        peaks["intens"] = peaks["intens"] * rng.uniform(0.5, 1.5, len(peaks))
        peaks["integral"] = peaks["integral"] * rng.uniform(0.5, 1.5, len(peaks))
        peaks["qhat"] = peaks["qhat"] @ turn.T
    patterns = tuple(
        dataclasses.replace(
            pattern,
            rotation=turn @ pattern.rotation,
            reciprocal=pattern.reciprocal @ turn.T,
            goodness=float(pattern.goodness * rng.uniform(0.1, 0.6)),
        )
        for pattern in base.patterns
    )
    metadata = {**base.metadata, "sample_position": position}
    return dataclasses.replace(base, peaks=peaks, patterns=patterns, metadata=metadata, depth=depth)
