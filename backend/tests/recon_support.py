"""Reconstructions for the wire viewer's tests, made from the synthetic reconstruction (``synthetic_reconstruction()``:
lauelab's synthetic wire scan reconstructed by lauelab, ``science.synthetic``, once per test process; a finished
one-point scan catalog, 51 float64 depths of 128 x 128):

- ``copy_synthetic``: the synthetic ``reconstruction/`` in a run directory, as it is;
- ``write_snapshot``: a catalog snapshot of several points in any status, as lauelab publishes them while a run
  writes (lauelab's own catalog writer, which validates what it writes); a complete point is a copy of the
  synthetic point file;
- ``write_per_depth``: the older per-depth format, ``<point>_<depth index>.h5`` and ``<point>_summary.txt``,
  exported from the synthetic point by lauelab (``export_per_depth``, LaueGo's per-depth layout).
"""

from __future__ import annotations

import atexit
import functools
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import h5py
import numpy as np
from lauelab.reconstruct import PointReader, export_per_depth
from lauelab.reconstruct import _scan_layout as scan_layout
from lauelab.reconstruct._scan_writer import parameter_values, write_catalog

from laueportal.jobs.rundir import reconstruction_catalog_path
from laueportal.science import recon, synthetic
from tests.wire_support import GEOMETRY

SYNTHETIC_DEPTHS = 51
SYNTHETIC_FRAME = (128, 128)


@functools.cache
def synthetic_reconstruction() -> Path:
    """The synthetic scan's ``reconstruction/`` (``scan.h5``, ``points/synthetic_wire_scan.h5``), made once per
    process in a temporary folder removed at exit."""
    directory = Path(tempfile.mkdtemp(prefix="synthetic-reconstruction-"))
    atexit.register(shutil.rmtree, directory, True)
    scan = synthetic.write_wire_scan(directory / "synthetic_wire_scan.h5")
    synthetic.reconstruct_wire_scan(scan, GEOMETRY, directory / "reconstruction")
    return directory / "reconstruction"


def synthetic_point() -> Path:
    """The synthetic reconstruction's point file."""
    return synthetic_reconstruction() / "points" / "synthetic_wire_scan.h5"


def copy_synthetic(run_directory: Path) -> Path:
    """The synthetic ``reconstruction/`` copied into ``run_directory``; returns its catalog."""
    shutil.copytree(synthetic_reconstruction(), run_directory / "reconstruction")
    return Path(reconstruction_catalog_path(run_directory))


@dataclass(frozen=True)
class CatalogPoint:
    point_id: str
    status: str
    """lauelab's point status: ``pending``, ``writing``, ``complete``, ``failed``, ``interrupted``,
    ``unattempted``."""
    source: str
    error: str = ""


def write_snapshot(run_directory: Path, points: list[CatalogPoint], *, run_status: str = "running") -> Path:
    """Publish a catalog snapshot of ``points`` in ``run_directory/reconstruction`` (replacing the last one), with
    the synthetic point's settings, geometry and shape; returns the catalog."""
    record = recon.read_point_record(synthetic_point())
    with PointReader(synthetic_point()) as reader:
        geometry_xml = reader.geometry_xml()
    directory = run_directory / "reconstruction"
    for index, point in enumerate(points):
        target = directory / scan_layout.point_path(point.point_id)
        if point.status == "complete" and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(synthetic_point(), target)
            with h5py.File(target, "r+") as file:  # the catalog's ID and position, which ScanReader.point checks
                _replace(file, "point_id", np.array([point.point_id.encode()]))
                _replace(file, "manifest_index", np.array([index], dtype=np.int32))
    status_type = scan_layout.POINT_COLUMNS["/points/statuses"].dtype
    columns = {
        "/points/point_ids": [p.point_id for p in points],
        "/points/paths": [scan_layout.point_path(p.point_id) for p in points],
        "/points/statuses": np.array([scan_layout.PointStatus[p.status.upper()] for p in points], dtype=status_type),
        "/points/errors": [p.error for p in points],
        "/points/source_paths": [p.source for p in points],
        "/points/scan_numbers": [1 for _ in points],
        "/points/sample_positions": [(float(i), 2.0 * i, -1.5) for i in range(len(points))],
        "/points/energies_kev": [20.0 for _ in points],
        "/points/shapes": [record.shape for _ in points],
        "/points/depth_bounds": [record.depth_bounds_um for _ in points],
        "/points/dtypes": [record.settings["output_pixel_type"] for _ in points],
    }
    catalog = Path(reconstruction_catalog_path(run_directory))
    catalog.parent.mkdir(parents=True, exist_ok=True)
    write_catalog(
        catalog,
        created=datetime(2026, 10, 22, tzinfo=UTC).isoformat(),
        run_status=scan_layout.RunStatus[run_status.upper()],
        native_version=record.native_version or "",
        parameters=parameter_values(int(record.settings["detector"]), record.settings),
        geometry_path=record.geometry_path or "",
        geometry_xml=geometry_xml,
        points=columns,
        replace=catalog.exists(),
    )
    return catalog


def _replace(file: h5py.File, name: str, data: np.ndarray) -> None:
    path = scan_layout.RECONSTRUCTION + name
    if path in file:
        del file[path]
    file.create_dataset(path, data=data)


def write_per_depth(run_directory: Path, point_id: str, *, frames: bool = True) -> list[Path]:
    """The per-depth files of point ``point_id`` in ``run_directory`` (the synthetic point's depths); without
    ``frames``, only its summary. Returns the files written."""
    run_directory.mkdir(parents=True, exist_ok=True)
    written = [Path(path) for path in export_per_depth(synthetic_point(), run_directory / f"{point_id}_")]
    if not frames:
        for path in written[:-1]:
            path.unlink()
        written = written[-1:]
    return written
