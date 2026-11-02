"""Detector image loading for the pixel-space detector view.

The indexing XML stores the source detector file in ``<inputImage>``.  In
practice that value may be absolute, relative to the indexing output folder,
or relative to the original data folder stored on the indexing run.  This
module resolves those common locations and loads a 2-D HDF5 dataset for use as
an image background. For reconstructed data, the ``ScanFrame`` reference
selects one depth frame from a point file. The reader checks the point ID
before loading that frame.

The HDF5 datasets to try come from ``settings.beamline.detector_datasets`` (callers pass them);
when none of them exists, the first dataset with two or more dimensions is used.

Nothing is looked at or opened that the caller's ``admit`` function (required) has not allowed: it maps each
candidate path (a remote run's, ``remote.paths.portal_path``) and confines it to the file roots
(``files.roots.resolve_inside_roots``), as the results viewer's does (``indexing.detector``). The viewer locates a
step's image with ``locate_detector_image``, reads the frame in its own type (``read_detector_frame``) and sends it
as ``frame_bytes`` encodes it. ``load_detector_image`` locates it the same way and reads it as
float values with a contrast range (no route uses it).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np


@dataclass
class DetectorImage:
    """Loaded detector image and provenance metadata."""

    data: np.ndarray
    path: str
    dataset: str
    vmin: float
    vmax: float


@dataclass
class DetectorImageResult:
    """Result object that preserves warnings for the detector summary."""

    image: DetectorImage | None = None
    warning: str | None = None
    attempted_paths: tuple[str, ...] = ()


def load_detector_image(
    input_image: str | None,
    *,
    source=None,
    xml_path: str | None = None,
    data_folder: str | None = None,
    root_path: str | None = None,
    dataset_paths: Iterable[str],
    admit: Admit,
) -> DetectorImageResult:
    """Resolve and load the detector image referenced by an indexing step (``locate_detector_image``).

    Parameters
    ----------
    input_image:
        Value from the XML ``<inputImage>`` element.
    source:
        The result's ``lauelab.indexing.ScanFrame``, or None for a frame file.
        When given, its point and depth index select one stored frame of the
        point file found at ``input_image``.
    xml_path:
        Current indexing XML path.  Used to resolve paths relative to the
        output folder.
    data_folder:
        Original indexing-run data folder from the DB.  Used when the XML stores
        only a detector filename.
    root_path:
        Portal workspace root from configuration.  Used for DB-relative paths.
    dataset_paths:
        Candidate HDF5 datasets to try in order (``settings.beamline.detector_datasets``; a leading
        ``/`` is optional). When none exists, the first dataset of two or more dimensions is used.
        Not used for a ``source`` frame.
    admit:
        What may be opened (``Admit``): each candidate path mapped and confined to the file roots.
    """
    located = locate_detector_image(
        input_image, xml_path=xml_path, data_folder=data_folder, root_path=root_path, admit=admit
    )
    if located.path is None:
        return DetectorImageResult(warning=located.warning, attempted_paths=located.attempted)
    image_path = Path(located.path)
    attempted = located.attempted
    try:
        data, dataset = read_detector_frame(image_path, source=source, dataset_paths=dataset_paths)
        data = data.astype(float, copy=False)
    except Exception as exc:
        return DetectorImageResult(
            warning=f"Could not load detector image {image_path}: {exc}",
            attempted_paths=attempted,
        )

    if data.ndim != 2:
        return DetectorImageResult(
            warning=f"Detector image dataset {dataset} in {image_path} is {data.ndim}D, expected 2D.",
            attempted_paths=attempted,
        )

    finite = data[np.isfinite(data)]
    if finite.size:
        vmin = float(np.nanpercentile(finite, 1.0))
        vmax = float(np.nanpercentile(finite, 99.9))
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin == vmax:
            vmin = float(np.nanmin(finite))
            vmax = float(np.nanmax(finite))
    else:
        vmin, vmax = 0.0, 1.0

    return DetectorImageResult(
        image=DetectorImage(data=data, path=str(image_path), dataset=dataset, vmin=vmin, vmax=vmax),
        attempted_paths=attempted,
    )


Admit = Callable[[Path], Path | str]
"""What a caller allows to be opened: for a candidate path, the path to open in its place (mapped and confined to the
file roots), or why it may not be opened."""


@dataclass(frozen=True)
class LocatedImage:
    """Where a step's detector image is (``locate_detector_image``)."""

    path: str | None
    """The file to open (as ``admit`` gave it); None when there is none."""
    warning: str | None
    """Why there is none: the paths tried and why each was not opened."""
    attempted: tuple[str, ...] = ()


def locate_detector_image(
    input_image: str | None,
    *,
    xml_path: str | None = None,
    data_folder: str | None = None,
    root_path: str | None = None,
    admit: Admit,
) -> LocatedImage:
    """The detector image a step's ``inputImage`` names: the first candidate (the value itself when absolute, else
    beside the output, in the data folder and under the root, ``load_detector_image``'s rules) that ``admit``
    allows and that is a file. Nothing is opened; ``admit`` sees each candidate before it is looked at. When none
    is, the warning lists each candidate (with the path ``admit`` mapped it to, when another) and why it was not
    used."""

    if not input_image:
        return LocatedImage(None, "No inputImage is recorded for this step.")
    candidates = _candidate_paths(input_image, xml_path=xml_path, data_folder=data_folder, root_path=root_path)
    if not candidates:
        return LocatedImage(None, f"Could not resolve detector image path: {input_image!r}.")

    attempted = tuple(str(path) for path in candidates)
    tried = []
    for candidate in candidates:
        path = admit(candidate)
        if isinstance(path, str):
            tried.append(f"{candidate} (not opened: {path})")
            continue
        looked_at = str(candidate) if path == candidate else f"{candidate} → {path}"
        try:
            if path.is_file():
                return LocatedImage(str(path), None, attempted)
        except OSError as error:  # a location the portal cannot read (for example another user's tree)
            tried.append(f"{looked_at} (not accessible: {error.strerror or type(error).__name__})")
            continue
        tried.append(f"{looked_at} (no such file)")
    return LocatedImage(None, "Detector image file was not found. Tried: " + "; ".join(tried), attempted)


def read_detector_frame(path: str | Path, *, source=None, dataset_paths: Iterable[str]) -> tuple[np.ndarray, str]:
    """The 2-D frame in ``path`` in its stored type, and its dataset: for a ``ScanFrame`` ``source``, its depth frame
    of the point file; else the first of ``dataset_paths`` the HDF5 file holds, or its first dataset of two or more
    dimensions (the first 2-D slice of a stack)."""

    if source is None:
        return _read_hdf5_image(Path(path), dataset_paths)
    return _read_scan_frame(Path(path), source)


def detector_dataset_name(path: str | Path, *, source=None, dataset_paths: Iterable[str]) -> str:
    """The dataset ``read_detector_frame`` reads, found without reading it."""

    if source is not None:
        return _scan_frame_name(source)
    with h5py.File(path, "r") as h5:
        return _image_dataset(h5, dataset_paths)[0]


FRAME_UINT16 = "uint16"
FRAME_FLOAT32 = "float32"


def frame_bytes(data: np.ndarray) -> tuple[bytes, str]:
    """A frame as the API sends it, row-major and little-endian, with its type's name:
    ``uint16`` when every value is an integer from 0 to 65535 (a detector's counts), else ``float32`` (a float
    source, or a reconstructed frame's signed values)."""

    data = np.asarray(data)
    integral = data.dtype.kind in "biu"
    if integral and (data.size == 0 or (int(data.min()) >= 0 and int(data.max()) <= 0xFFFF)):
        return np.ascontiguousarray(data, dtype="<u2").tobytes(), FRAME_UINT16
    return np.ascontiguousarray(data, dtype="<f4").tobytes(), FRAME_FLOAT32


def _candidate_paths(
    input_image: str,
    *,
    xml_path: str | None,
    data_folder: str | None,
    root_path: str | None,
) -> list[Path]:
    raw = Path(str(input_image).strip())
    candidates: list[Path] = []

    def add(path: Path) -> None:
        expanded = path.expanduser()
        if expanded not in candidates:
            candidates.append(expanded)

    if raw.is_absolute():
        add(raw)
    else:
        if xml_path:
            add(Path(xml_path).expanduser().resolve().parent / raw)
        if data_folder:
            data_path = Path(data_folder).expanduser()
            add(data_path / raw)
            if root_path and not data_path.is_absolute():
                add(Path(root_path).expanduser() / data_path / raw)
        if root_path:
            add(Path(root_path).expanduser() / raw)
        add(raw)

    return candidates


def _image_dataset(h5, dataset_paths: Iterable[str]) -> tuple[str, h5py.Dataset]:
    for dataset_path in dataset_paths:
        key = str(dataset_path).strip()
        if not key:
            continue
        lookup = key[1:] if key.startswith("/") else key
        if lookup in h5:
            item = h5[lookup]
            if not isinstance(item, h5py.Dataset):  # reading a group fails, as it always did
                raise TypeError(f"{key} is not a dataset")
            return key, item
    found = _first_2d_dataset(h5)
    if found is not None:
        return found
    raise KeyError("no 2-D detector image dataset found")


def _read_hdf5_image(path: Path, dataset_paths: Iterable[str]) -> tuple[np.ndarray, str]:
    with h5py.File(path, "r") as h5:
        dataset, item = _image_dataset(h5, dataset_paths)
        first = (0,) * max(item.ndim - 2, 0)  # a stack's first frame, read alone
        return _coerce_image_2d(np.asarray(item[first])), dataset


def _scan_frame_name(source) -> str:
    return f"point {source.point_id}, depth index {source.depth_index}"


def _read_scan_frame(path: Path, source) -> tuple[np.ndarray, str]:
    from lauelab.reconstruct import PointReader

    with PointReader(path) as point:
        if point.point_id != source.point_id:
            raise ValueError(f"{path} holds point {point.point_id!r}, not {source.point_id!r}")
        data = point.frame(source.depth_index)
    return np.asarray(data), _scan_frame_name(source)


def _first_2d_dataset(group, prefix: str = "") -> tuple[str, h5py.Dataset] | None:
    for name, item in group.items():
        path = f"{prefix}/{name}"
        if isinstance(item, h5py.Dataset):
            if item.ndim >= 2:
                return path, item
        elif isinstance(item, h5py.Group):
            found = _first_2d_dataset(item, path)
            if found is not None:
                return found
    return None


def _coerce_image_2d(data: np.ndarray) -> np.ndarray:
    data = np.asarray(data)
    while data.ndim > 2:
        data = data[0]
    if not np.issubdtype(data.dtype, np.number):
        raise TypeError(f"detector dataset has non-numeric dtype {data.dtype}")
    return data
