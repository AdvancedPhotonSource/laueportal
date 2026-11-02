"""Where a reconstruction's output is: the run's scan catalog, or the older per-depth files.

New runs keep ``reconstruction/scan.h5`` in the run directory (``jobs.rundir``). Earlier runs have
per-depth files ``<point_id>_<depth_index>.h5`` and ``<point_id>_summary.txt`` in the run directory.
Reading the located output (point table, frames, traces) is ``laueportal.science.recon``.

A run directory that is absent or cannot be listed (production's ``/net/...`` on another host, an unmounted or
stale mount, a folder the server may not open) is not on this host (``Found.on_host``), which the viewer tells
apart from a directory that is there without a reconstruction. These look at the file system unbounded: the
viewer calls them through ``files.probe`` (``reconstruction.viewer.locate``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from laueportal.jobs.rundir import reconstruction_catalog_path
from laueportal.science.recon import KIND_PER_DEPTH, KIND_SCAN, SUMMARY_SUFFIX, Artifact


@dataclass(frozen=True)
class Found:
    """What a run directory holds (``find_artifact``)."""

    artifact: Artifact | None
    on_host: bool
    """Whether the recorded run directory is there and can be listed (true when none is recorded: nothing to look
    for)."""


def find_artifact(run_directory: str | None) -> Found:
    """The scan catalog or per-depth output (including catalogs of running jobs), and whether the run directory is
    on this host."""

    if not run_directory:
        return Found(None, on_host=True)
    scan = reconstruction_catalog_path(run_directory)
    if os.path.isfile(scan):
        return Found(Artifact(KIND_SCAN, scan), on_host=True)
    try:
        with os.scandir(run_directory) as entries:
            if any(entry.name.endswith(SUMMARY_SUFFIX) for entry in entries):
                return Found(Artifact(KIND_PER_DEPTH, run_directory), on_host=True)
    except (OSError, ValueError):  # absent, not a folder, unreadable, a stale mount; a NUL in the path
        return Found(None, on_host=False)
    return Found(None, on_host=True)


def locate_artifact(run_directory: str | None) -> Artifact | None:
    """Find the scan catalog or per-depth output, including catalogs of running jobs."""

    return find_artifact(run_directory).artifact
