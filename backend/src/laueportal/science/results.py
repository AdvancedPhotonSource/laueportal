"""Indexing-results files through lauelab: format check, structural validation, XML conversion.

``indexing.results`` resolves and converts a run's results through this module; the lauelab names it
needs (``InvalidResultsFile``, ``ResultsFileSummary``, ``convert_xml``) are re-exported here, so only
the science boundary imports lauelab (import-linter rule 2).
"""

from __future__ import annotations

import functools
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import h5py
import lauelab
import numpy as np
from lauelab.indexing import InvalidResultsFile, ResultsFileSummary, validate_results_file
from lauelab.visualization import convert_xml

__all__ = [
    "InvalidResultsFile",
    "NamedGeometry",
    "ResultsFileSummary",
    "convert_xml",
    "count_indexed_frames",
    "is_results_file",
    "named_geometry",
    "validate_results",
]


def is_results_file(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` carries the lauelab indexing-results format marker."""

    return lauelab.is_results_file(path)


def validate_results(path: str | os.PathLike[str]) -> tuple[ResultsFileSummary | None, str | None]:
    """Return (summary, None) for a valid results file, else (None, reason). A valid file's summary is kept by the
    file's identity (real path, inode, size, modification and change times; the last ``VALID_KEPT``; the change time
    because a file rewritten in place can keep its size and modification time, as ``cp -p`` or ``rsync --inplace -t``
    leave it, while every write, rename or ``utime`` sets the change time, which user space cannot set): every
    results-viewer request resolves its run's results (``indexing.viewer.viewer_source``), and validating I1's
    ``output.h5`` takes about 30 ms of h5py under the GIL, which 80 detector images in flight turned into a starved
    thread pool (re-measured). A file found invalid is checked again each time."""

    path = Path(path)
    if not path.is_file():
        return None, f"{path} does not exist"
    try:
        status = os.stat(path)
        identity = (os.path.realpath(path), status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
        return _valid_summary(str(path), *identity), None
    except _NotResults:
        return None, f"{path} is not a lauelab indexing-results file"
    except (InvalidResultsFile, OSError) as error:
        return None, f"{path} failed validation: {error}"


VALID_KEPT = 8
"""Valid results files whose summary ``validate_results`` keeps (each holds the file's frame IDs); the results
viewer keeps ``indexing.dataset_cache_size`` (6) datasets loaded."""


class _NotResults(Exception):
    """The file has no lauelab results marker (``_valid_summary``)."""


@functools.lru_cache(maxsize=VALID_KEPT)
def _valid_summary(
    path: str, _real: str, _inode: int, _size: int, _mtime_ns: int, _ctime_ns: int
) -> ResultsFileSummary:
    """``path``'s summary, kept by its identity; raises (and keeps nothing) when it is not valid."""
    if not lauelab.is_results_file(path):
        raise _NotResults(path)
    return validate_results_file(path)


def count_indexed_frames(path: str | os.PathLike[str]) -> int:
    """Frames with at least one pattern in a validated results file (``indexing_run.n_frames_indexed``, as the
    ``lauego`` compute counts them). Reads only the frames' pattern offsets."""

    with h5py.File(path, "r") as file:
        offsets = np.asarray(file["frames/pattern_offsets"])
    return int(np.count_nonzero(np.diff(offsets) > 0))


@dataclass(frozen=True)
class NamedGeometry:
    """The detector geometry an indexing output names: what lauelab would load by itself."""

    embedded: bool
    """A results file holds the geometry's text (``/geometry/xml``); lauelab loads that and opens no path."""
    paths: tuple[str, ...]
    """The geometry paths as written, in order: ``output.h5``'s ``/geometry@path``, or an Output XML's distinct
    ``geoFile`` values (LaueGo writes the same one in every step; lauelab tries each until one loads). Not
    rewritten at publication: readers map them."""


def named_geometry(path: str | os.PathLike[str]) -> NamedGeometry:
    """The geometry ``path`` (a results file or an Output XML) names. Reads only the geometry group of a results
    file; an XML is parsed once per file version (real path, size, modification and change times) and
    remembered."""

    real = os.path.realpath(path)
    status = os.stat(real)
    return _named_geometry(real, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


@functools.lru_cache(maxsize=64)
def _named_geometry(path: str, _size: int, _mtime_ns: int, _ctime_ns: int) -> NamedGeometry:
    if lauelab.is_results_file(path):
        with h5py.File(path, "r") as file:
            group = file.get("geometry")
            if not isinstance(group, h5py.Group):
                return NamedGeometry(embedded=False, paths=())
            value = group.attrs.get("path", "")
            text = (value.decode("utf-8", "surrogateescape") if isinstance(value, bytes) else str(value)).strip()
            return NamedGeometry(embedded="xml" in group, paths=(text,) if text else ())
    values: list[str] = []
    for _event, element in ET.iterparse(path):
        if element.tag == "geoFile":
            text = (element.text or "").strip()
            if text and text not in values:
                values.append(text)
        elif element.tag == "step":
            element.clear()  # keep memory flat on a long document
    return NamedGeometry(embedded=False, paths=tuple(values))
