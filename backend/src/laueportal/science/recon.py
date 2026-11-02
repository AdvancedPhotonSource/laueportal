"""Read reconstructions through lauelab: scan catalogs, point and per-depth readers, images and traces.

``reconstruction.views`` locates a run's output (``locate_artifact``); this module reads it.

New runs use ``reconstruction/scan.h5`` to track progress and locate completed
files under ``reconstruction/points/``. Earlier runs have per-depth files
``<point_id>_<depth_index>.h5`` and ``<point_id>_summary.txt`` in the run directory.

Catalog metadata and computed products are cached on the server, in the app's ``ProductCache``
(``app.state.reconstruction_products``, ``reconstruction.product_cache_mib``; every reader takes it as
``cache=``), computed once per key however many requests ask for it. Point-file identity determines whether an
image or trace can be reused after a catalog update. Per-depth runs use directory identity instead (their
listing is kept too); their first reference image or full-frame trace reads every frame, one at a time (a scan
point's are stored in its file). A catalog's, a listing's, a reader's or a point's product's value kept under a new
identity replaces the one kept under the old (``ProductCache``'s slots), so a watched run's never pile up.

Every point file is checked by the caller's ``admit`` before anything opens it, or even looks at it
(``reconstruction.viewer``: a remote run's path mapped with its plan, then confined to the file roots).

The point table reads a fresh catalog snapshot on each explicit refresh.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import h5py
import numpy as np
from lauelab.indexing import InputError
from lauelab.reconstruct import PerDepthReader, PointReader, ScanReader
from lauelab.reconstruct import _scan_layout as scan_layout
from lauelab.reconstruct._scan_writer import parameter_values, write_catalog
from lauelab.reconstruct.inspection import (
    REFERENCE_LABELS,
    Bounds,
    DepthTrace,
    ReferenceImage,
    depth_trace,
    reference_image,
    square_bounds,
)

from laueportal.science.once import Pending

KIND_SCAN = "scan"
KIND_PER_DEPTH = "per_depth"
SUMMARY_SUFFIX = "_summary.txt"
MIB = 1024 * 1024
DEFAULT_CACHE_BYTES = 1024 * MIB
"""The default budget (1 GiB); the app's comes from ``reconstruction.product_cache_mib``."""
ROI_BLOCK_BYTES = 64 * MIB
"""The most pixel bytes an ROI pass reads at once (``roi_traces``; lauelab's ``depth_trace`` default): as many depths
as fit, at least one."""
ROI_READ_PIXELS = 2**18
"""What one read costs beyond its pixels, in pixels (``_roi_sums``): opening a per-depth file and reading a few pixels
of it took 0.6 ms on the measured sample, as long as reading about 2^18 pixels."""

T = TypeVar("T")

Admit = Callable[[str], str]
"""Checks a point file before anything opens it, or even looks at it (``reconstruction.viewer``: a remote run's path
mapped with its plan, then confined to the file roots) and returns the path to read; raises when the file may not
be read. The readers' default admits every path as it is."""


def _as_is(path: str) -> str:
    return path


def is_reconstruction_scan(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` is a readable lauelab reconstruction scan catalog (``scan.h5``)."""

    if not os.path.isfile(path):
        return False
    try:
        with ScanReader(path):
            return True
    except (OSError, ValueError):
        return False


def read_scan_catalog(path: str | os.PathLike[str]) -> ScanReader:
    """A snapshot of a scan catalog's metadata (the file is closed again). Raises OSError or ValueError."""

    return ScanReader(path)


class NotKept(Exception):
    """A product read only if kept (``kept_only``) that the cache no longer holds: evicted since a route found it
    kept, so the route takes a turn after all. Neither a ``ViewError`` nor a problem."""


class ViewError(RuntimeError):
    """An error message suitable for display on the reconstruction page."""


@dataclass(frozen=True)
class Artifact:
    """Location and format of a reconstruction."""

    kind: str  # KIND_SCAN or KIND_PER_DEPTH
    path: str  # the scan catalog, or the run directory holding per-depth files


def _file_identity(path: str | os.PathLike[str]) -> tuple:
    """Use the path, inode, size, and modification and change times to detect file changes (the change time: a
    file rewritten in place may keep its size and modification time, never its change time)."""

    status = os.stat(path)
    return (os.path.realpath(path), status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def identity(artifact: Artifact) -> tuple:
    """Identify the current catalog snapshot or per-depth directory (its kind, real path, inode, size, modification
    and change times): a new catalog snapshot, or a file added to or removed from a per-depth directory, changes
    it. Raises OSError."""

    return (artifact.kind, *_file_identity(artifact.path))


_DEPTH_INDEX = re.compile(r"\d+")


def _per_depth_files(directory: str) -> dict[str, list[str]]:
    """Group per-depth files by point ID, using summary filenames to identify points. One pass over the listing:
    ``<point>_<depth index>.h5`` belongs to the summary's ``<point>``, and since the index holds no ``_`` the last
    ``_`` of the name splits it (``a_1_2.h5`` is point ``a_1``'s, ``a_1.h5`` point ``a``'s)."""

    names = os.listdir(directory)
    points = sorted(name[: -len(SUMMARY_SUFFIX)] for name in names if name.endswith(SUMMARY_SUFFIX))
    files: dict[str, list[str]] = {point: [] for point in points}
    for name in names:
        if not name.endswith(".h5"):
            continue
        point, separator, index = name[: -len(".h5")].rpartition("_")
        if separator and point in files and _DEPTH_INDEX.fullmatch(index):
            files[point].append(os.path.join(directory, name))
    return files


def _finite(values) -> tuple[float, ...] | None:
    numbers = tuple(float(value) for value in values)
    return numbers if all(np.isfinite(numbers)) else None


STATUS_NO_FRAMES = "no_frames"
"""Point status of a per-depth point whose summary has no frame files next to it (lauelab's catalog
statuses are ``pending``, ``writing``, ``complete``, ``failed``, ``interrupted`` and ``unattempted``)."""
NO_FRAMES_ERROR = "no per-depth files next to the summary"
"""Such a point's ``error`` text."""


@dataclass(frozen=True)
class PointRow:
    """One point of a reconstruction, as stored: no formatting (the frontend writes "2048 x 2048")."""

    point_id: str
    index: int
    status: str
    """The catalog's point status, or ``complete``/``STATUS_NO_FRAMES`` for per-depth files."""
    source: str | None
    """The input file the point was reconstructed from (scan catalogs only)."""
    sample_position_um: tuple[float, ...] | None
    """Sample (x, y, z) in µm; None when a component is missing or for per-depth files."""
    n_depths: int | None
    depth_first_um: float | None
    """Depth of the first and last frame; known for complete points of a scan catalog only."""
    depth_last_um: float | None
    frame_shape: tuple[int, int] | None
    """(rows, columns) of one depth frame, for complete points of a scan catalog."""
    dtype: str | None
    error: str | None


@dataclass(frozen=True)
class PointTable:
    """Point metadata for the table, with the catalog status when available."""

    rows: list[PointRow]
    run_status: str | None  # the catalog's run status; None for per-depth files


def point_table(artifact: Artifact, *, cache: ProductCache | None = None) -> PointTable:
    """Read current point metadata for the table without loading images. With ``cache``, a scan catalog's metadata
    is the snapshot cached for its identity (shared with the frames and traces: a new snapshot is read once), and a
    per-depth folder's listing the one kept for the folder's identity; without, they are read again. Raises OSError or ValueError (lauelab's ``InvalidScanFile``)."""

    rows: list[PointRow] = []
    if artifact.kind == KIND_SCAN:
        with _scan_reader(artifact, cache) as scan:
            run_status = scan.run_status
            for entry in scan.points:
                complete = entry.status == "complete"
                rows.append(
                    PointRow(
                        point_id=entry.point_id,
                        index=entry.index,
                        status=entry.status,
                        source=entry.source_path or None,
                        sample_position_um=_finite(entry.sample_position),
                        n_depths=int(entry.shape[0]) if complete else None,
                        depth_first_um=float(entry.depth_bounds_um[0]) if complete else None,
                        depth_last_um=float(entry.depth_bounds_um[1]) if complete else None,
                        frame_shape=(int(entry.shape[1]), int(entry.shape[2])) if complete else None,
                        dtype=str(entry.dtype) if complete and entry.dtype is not None else None,
                        error=entry.error or None,
                    )
                )
        return PointTable(rows, run_status)
    listing = _per_depth_files(artifact.path) if cache is None else _listing(artifact, cache)
    for index, (point_id, files) in enumerate(listing.items()):
        rows.append(
            PointRow(
                point_id=point_id,
                index=index,
                status="complete" if files else STATUS_NO_FRAMES,
                source=None,
                sample_position_um=None,
                n_depths=len(files) or None,
                depth_first_um=None,  # known once the point is opened
                depth_last_um=None,
                frame_shape=None,
                dtype=None,
                error=None if files else NO_FRAMES_ERROR,
            )
        )
    return PointTable(rows, None)


def _scan_reader(artifact: Artifact, cache: ProductCache | None):
    """The catalog's reader for a ``with`` block: a new one, or the cached snapshot's (lent, not closed)."""

    return ScanReader(artifact.path) if cache is None else _Borrowed(_catalog(artifact, cache).scan)


class ProductCache:
    """Cache products with a byte limit; estimate memory use for metadata. The API holds one per app
    (``app.state.reconstruction_products``, ``reconstruction.api.ProductCacheDep``).

    One computation per key (``science.once``, as ``science.cache.DatasetCache`` loads): a request for a product
    another request is computing waits for it (and gets its value, or a copy of its error), while hits and other keys
    go ahead; the lock is never held while a product is computed, and a computation that fails, or whose bookkeeping
    does, leaves no key computing. Several products can be computed together (``get_all``: a point's ROI traces, in
    one pass over its frames). A value kept for a ``slot`` replaces the one kept for it before: a catalog's new
    snapshot evicts its old one, so a watched live run's snapshots never pile up, and a point's
    product under a per-depth folder's new identity its value under the old."""

    def __init__(self, max_bytes: int = DEFAULT_CACHE_BYTES):
        self.max_bytes = max_bytes
        self._entries: OrderedDict[Hashable, tuple[object, int]] = OrderedDict()
        self._computing: dict[Hashable, Pending] = {}
        self._bytes = 0
        self._slots: dict[Hashable, Hashable] = {}  # the key kept for each slot
        self._slot_of: dict[Hashable, Hashable] = {}  # and the slot of each such key
        self._lock = threading.Lock()

    @property
    def nbytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)

    def holds(self, key: Hashable) -> bool:
        """Whether ``key``'s value is kept (``get`` returns it without computing)."""
        with self._lock:
            return key in self._entries

    def get(
        self, key: Hashable, compute: Callable[[], T], *, kept_only: bool = False, slot: Hashable | None = None
    ) -> T:
        """``get_all`` of one key; with ``slot``, its value once kept replaces the one kept for ``slot`` before."""
        slots = None if slot is None else {key: slot}
        return self.get_all((key,), lambda missing: (compute(),), kept_only=kept_only, slots=slots)[0]

    def get_all(
        self,
        keys: Sequence[Hashable],
        compute: Callable[[tuple[Hashable, ...]], Sequence[T]],
        *,
        kept_only: bool = False,
        slots: Mapping[Hashable, Hashable] | None = None,
    ) -> list[T]:
        """The value of each of ``keys``, in order: a kept one at once; one another request is computing when that
        computation ends; the others computed together, by one call ``compute(missing)`` (their values, in order),
        then kept by key, each replacing the value kept before for its slot in ``slots``, if it has one. A request
        computes its own keys before it waits for another's, so two never wait for each other. With ``kept_only``,
        ``NotKept`` unless every key is kept: nothing is computed or waited for."""
        values: dict[Hashable, Any] = {}
        others: dict[Hashable, Pending] = {}
        missing: list[Hashable] = []
        with self._lock:
            if kept_only and not all(key in self._entries for key in keys):
                raise NotKept("evicted since it was found kept")
            for key in dict.fromkeys(keys):
                if key in self._entries:
                    self._entries.move_to_end(key)
                    values[key] = self._entries[key][0]
                elif key in self._computing:
                    others[key] = self._computing[key]
                else:
                    missing.append(key)
            mine = Pending()
            for key in missing:
                self._computing[key] = mine
        if missing:
            wanted = tuple(missing)
            values |= mine.settle(
                lambda: dict(zip(wanted, compute(wanted), strict=True)),  # outside the lock: a slow read stalls no one
                keep=lambda kept: self._keep(kept, slots or {}),
                lock=self._lock,
                table=self._computing,
                keys=wanted,
            )
        for key, pending in others.items():
            values[key] = pending.wait()[key]
        # A key always names the same kind of product, so a value has compute's type.
        return [cast(T, values[key]) for key in keys]

    def _keep(self, values: dict[Hashable, Any], slots: Mapping[Hashable, Hashable]) -> None:
        sizes = {key: _nbytes(value) for key, value in values.items()}
        with self._lock:
            for key, value in values.items():
                if sizes[key] <= self.max_bytes:
                    if (slot := slots.get(key)) is not None:
                        if (replaced := self._slots.get(slot)) is not None:
                            self._drop(replaced)
                        self._slots[slot], self._slot_of[key] = key, slot
                    self._entries[key] = (value, sizes[key])
                    self._bytes += sizes[key]
            while self._bytes > self.max_bytes:
                self._drop(next(iter(self._entries)))

    def _drop(self, key: Hashable) -> None:
        """Forget ``key``'s value and its slot (under the lock)."""
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry[1]
        slot = self._slot_of.pop(key, None)
        if slot is not None and self._slots.get(slot) == key:
            del self._slots[slot]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._slots.clear()
            self._slot_of.clear()
            self._bytes = 0


def _nbytes(value) -> int:
    if isinstance(value, np.ndarray):
        return value.nbytes
    if isinstance(value, ReferenceImage):
        return value.image.nbytes
    if isinstance(value, DepthTrace):
        return value.values.nbytes + value.depth_um.nbytes
    if isinstance(value, PerDepthReader):
        return 64 * len(value.paths)  # metadata only; pixels are read per operation
    if isinstance(value, _Catalog):
        return sum(1024 + len(p.source_path) + len(p.error) + len(p.path) for p in value.scan.points)
    if isinstance(value, dict):  # a per-depth listing (``_listing``)
        return sum(64 + len(point) + sum(64 + len(path) for path in files) for point, files in value.items())
    return 64


def _open_point(artifact: Artifact, point_id: str, cache: ProductCache, admit: Admit):
    """Open a point reader for use in a ``with`` block: a scan point's file, which ``_point_key`` admitted, or the
    per-depth point's reader, made once of the files ``admit`` let through."""

    if artifact.kind == KIND_SCAN:
        return _catalog(artifact, cache).point(point_id)
    current = identity(artifact)
    reader = cache.get(
        (current, point_id, "reader"),
        lambda: _per_depth_reader(artifact, point_id, cache, admit),
        slot=(current[:2], point_id, "reader"),
    )
    return _Borrowed(reader)


def _listing(artifact: Artifact, cache: ProductCache) -> dict[str, list[str]]:
    """A per-depth folder's files by point (``_per_depth_files``), listed once per folder identity (a new one
    replaces the folder's last, as a catalog's snapshots do)."""

    current = identity(artifact)
    return cache.get(
        (current, "per_depth_files"), lambda: _per_depth_files(artifact.path), slot=(current[:2], "per_depth_files")
    )


def _per_depth_reader(artifact: Artifact, point_id: str, cache: ProductCache, admit: Admit) -> PerDepthReader:
    files = _listing(artifact, cache).get(point_id)
    if not files:
        raise ViewError(f"point {point_id!r} has no per-depth files in {artifact.path}")
    return PerDepthReader([admit(path) for path in files], point_id=point_id)


class _Catalog:
    """Share catalog metadata across callbacks. Callbacks close their own point readers."""

    def __init__(self, path):
        self.scan = ScanReader(path)
        self.points = {entry.point_id: entry for entry in self.scan.points}
        self._lock = threading.Lock()

    def point(self, point_id):
        # ScanReader updates its list of open readers here. Only opening and
        # validation need this lock; callbacks can read their pixels independently.
        with self._lock:
            return self.scan.point(point_id)


class _Borrowed:
    """Lends a cached reader without closing it."""

    def __init__(self, reader):
        self._reader = reader

    def __enter__(self):
        return self._reader

    def __exit__(self, *exc):
        return None


def _catalog(artifact: Artifact, cache: ProductCache) -> _Catalog:
    """Reuse the catalog metadata until a new snapshot is published, which then replaces it in the cache (its slot
    is the catalog's kind and real path: a watched live run's old snapshots, never read again, do not pile up)."""

    current = identity(artifact)
    return cache.get((current, "catalog"), lambda: _Catalog(artifact.path), slot=(current[:2], "catalog"))


def _point_key(artifact: Artifact, point_id: str, cache: ProductCache, admit: Admit) -> tuple:
    """Build a cache key from the point file (admitted first) or per-depth directory."""

    if artifact.kind != KIND_SCAN:
        return (identity(artifact), point_id)
    catalog = _catalog(artifact, cache)
    entry = catalog.points.get(point_id)
    if entry is None:
        raise ViewError(f"the catalog has no point {point_id!r}")
    if entry.status != "complete":
        raise ViewError(f"point {point_id!r} is {entry.status}" + (f": {entry.error}" if entry.error else ""))
    return (KIND_SCAN, point_id, *_file_identity(admit(str(catalog.scan.point_path(point_id)))))


def _product_slot(artifact: Artifact, point_id: str, product: Hashable) -> Hashable:
    """A point's product's slot in the cache (``ProductCache.get``'s ``slot``): the catalog or per-depth folder (its
    kind and real path), the point and the product. The product's key holds the point file's identity, or for
    per-depth files the folder's, which a file added to or removed from the folder changes for every point: once a
    product is kept under its new key, its value under the old one is dropped, so a watched folder's products do not
    pile up (as the listing's and the readers' slots)."""
    return ((artifact.kind, os.path.realpath(artifact.path)), point_id, product)


def _viewed(read: Callable[[], T]) -> T:
    try:
        return read()
    except (InputError, KeyError, OSError, ValueError) as error:
        raise ViewError(str(error) or type(error).__name__) from error


def point_key(artifact: Artifact, point_id: str, *, cache: ProductCache, admit: Admit | None = None) -> tuple:
    """What a point's products are kept by: its point file's identity (real path, inode, size, modification and
    change times; the file admitted first), or for per-depth files the folder's identity and the point. A rewritten
    point file changes it. Raises ViewError."""

    return _viewed(lambda: _point_key(artifact, point_id, cache, admit or _as_is))


def _cached(
    artifact: Artifact,
    point_id: str,
    product: Hashable,
    compute,
    cache: ProductCache,
    admit: Admit | None,
    *,
    kept_only: bool = False,
):
    admit = admit or _as_is
    return _viewed(
        lambda: cache.get(
            (_point_key(artifact, point_id, cache, admit), product),
            lambda: _with_point(artifact, point_id, compute, cache, admit),
            kept_only=kept_only,
            slot=_product_slot(artifact, point_id, product),
        )
    )


def _with_point(artifact: Artifact, point_id: str, compute, cache: ProductCache, admit: Admit):
    with _open_point(artifact, point_id, cache, admit) as point:
        return compute(point)


@dataclass(frozen=True)
class PointSummary:
    point_id: str
    shape: tuple[int, int, int]
    dtype: str
    depth_um: np.ndarray


def point_summary(
    artifact: Artifact, point_id: str, *, cache: ProductCache, admit: Admit | None = None
) -> PointSummary:
    return _cached(
        artifact,
        point_id,
        "summary",
        lambda point: PointSummary(point_id, tuple(point.shape), str(point.dtype), np.array(point.depth_um)),
        cache,
        admit,
    )


def stored_frame(
    artifact: Artifact, point_id: str, depth_index: int, *, cache: ProductCache, admit: Admit | None = None
) -> np.ndarray:
    """Read and cache one depth frame."""

    index = int(depth_index)
    return _cached(artifact, point_id, ("frame", index), lambda point: point.frame(index), cache, admit)


FULL_TRACE = "full_trace"
"""The product name of a point's full trace (``full_trace``; ``kept``)."""


def reference_product(kind: str) -> tuple[str, str]:
    """The product name of a point's reference image ``kind`` (``reference``; ``kept``)."""
    return ("reference", kind)


def roi_product(bounds: Bounds) -> tuple[str, Bounds]:
    """The product name of a point's ROI trace (``roi_traces``; ``kept``): its bounds, whatever its name."""
    return ("roi", bounds)


def kept(point: tuple, products: Iterable[Hashable], *, cache: ProductCache) -> bool:
    """Whether every one of ``products`` of the point whose key is ``point`` (``point_key``) is kept, so that reading
    it computes nothing (the wire viewer's routes then take no turn)."""
    return all(cache.holds((point, product)) for product in products)


def full_trace(
    artifact: Artifact, point_id: str, *, cache: ProductCache, admit: Admit | None = None, kept_only: bool = False
) -> DepthTrace:
    """The full frame's intensity through depth: a scan point's stored reduction, or per-depth files summed one frame
    at a time. With ``kept_only``, ``NotKept`` unless it is kept (``ProductCache.get_all``)."""

    return _cached(artifact, point_id, FULL_TRACE, lambda point: depth_trace(point), cache, admit, kept_only=kept_only)


def reference(
    artifact: Artifact,
    point_id: str,
    kind: str,
    *,
    cache: ProductCache,
    admit: Admit | None = None,
    kept_only: bool = False,
) -> ReferenceImage:
    """A reference image (``REFERENCE_NAMES``): a scan point's stored image, or per-depth files' reconstructed sum,
    summed one frame at a time (they hold no raw images: a ViewError). With ``kept_only``, ``NotKept`` unless it is
    kept."""

    return _cached(
        artifact,
        point_id,
        reference_product(kind),
        lambda point: reference_image(point, kind),
        cache,
        admit,
        kept_only=kept_only,
    )


def roi_traces(
    artifact: Artifact,
    point_id: str,
    regions: Sequence[tuple[str, Sequence[float]]],
    *,
    cache: ProductCache,
    admit: Admit | None = None,
    kept_only: bool = False,
) -> list[DepthTrace]:
    """Each named region's trace, in order: the sum of its stored pixels at each depth, as lauelab's ``depth_trace``
    makes it, under its name. A region is ``(y0, y1, x0, x1)``, half-open, its values truncated to whole pixels. Kept
    by bounds (``roi_product``), so a renamed or revisited ROI reuses its trace; those not kept are summed together in
    one pass over the point's depths, which reads at most one box holding them per depth (``_roi_sums``). With
    ``kept_only``, ``NotKept`` unless every one is kept. Raises ViewError (lauelab's refusal of bounds that are not
    four integers inside the image too)."""

    admit = admit or _as_is
    # Any length goes through, so lauelab (not unpacking here) rejects bounds that are not four values.
    named = [(name, cast(Bounds, tuple(int(value) for value in bounds))) for name, bounds in regions]

    def read() -> list[DepthTrace]:
        point = _point_key(artifact, point_id, cache, admit)
        keys = [(point, roi_product(bounds)) for _, bounds in named]
        bounds_of: dict[Hashable, Bounds] = dict(zip(keys, (bounds for _, bounds in named), strict=True))
        slots = {key: _product_slot(artifact, point_id, roi_product(bounds)) for key, bounds in bounds_of.items()}

        def compute(missing: tuple[Hashable, ...]) -> list[DepthTrace]:
            wanted = [bounds_of[key] for key in missing]
            return _with_point(artifact, point_id, lambda reader: _roi_sums(reader, point_id, wanted), cache, admit)

        return cache.get_all(keys, compute, kept_only=kept_only, slots=slots)

    traces = _viewed(read)
    return [
        DepthTrace(trace.values, trace.depth_um, trace.point_id, trace.bounds, name)
        for (name, _), trace in zip(named, traces, strict=True)
    ]


def _roi_sums(point, point_id: str, regions: Sequence[Bounds]) -> list[DepthTrace]:
    """``regions``' traces in one pass over ``point``'s depths, reading for each block of depths whichever reads less
    (each read counted with ``ROI_READ_PIXELS`` more): the box that holds every region, once, or each region's own
    pixels (regions far apart). So no request reads more than one box per depth, however many regions it has.

    Each region is summed as lauelab's ``depth_trace`` sums one, so the values are lauelab's: over each depth, in
    lauelab's accumulator type (integers in ``int64``, exact in any order; floats in ``float64``, from the region's
    pixels in one contiguous array, as lauelab reads them, so in lauelab's order)."""

    for bounds in regions:
        point.region(bounds, slice(0, 0))  # lauelab's check (four integers, inside the image); reads no pixel
    if not regions:
        return []
    top, bottom = min(bounds[0] for bounds in regions), max(bounds[1] for bounds in regions)
    left, right = min(bounds[2] for bounds in regions), max(bounds[3] for bounds in regions)
    areas = [(y1 - y0) * (x1 - x0) for y0, y1, x0, x1 in regions]
    box = (bottom - top) * (right - left)
    in_one = box + ROI_READ_PIXELS <= sum(areas) + len(regions) * ROI_READ_PIXELS
    n_depths = int(point.shape[0])
    total = np.dtype(scan_layout.accumulator_dtype(point.dtype))
    step = max(1, ROI_BLOCK_BYTES // ((box if in_one else max(areas)) * point.dtype.itemsize))
    sums = [np.empty(n_depths, dtype=total) for _ in regions]
    for first in range(0, n_depths, step):
        depths = slice(first, min(first + step, n_depths))
        if in_one:
            pixels = point.region((top, bottom, left, right), depths)
            for values, (y0, y1, x0, x1) in zip(sums, regions, strict=True):
                region = pixels[:, y0 - top : y1 - top, x0 - left : x1 - left]
                if total.kind == "f":  # a float sum depends on its order: lauelab's is that of a contiguous region
                    region = np.ascontiguousarray(region)
                values[depths] = region.sum(axis=(1, 2), dtype=total)
            del pixels  # before the next block is read
        else:
            for values, bounds in zip(sums, regions, strict=True):
                values[depths] = point.region(bounds, depths).sum(axis=(1, 2), dtype=total)
    return [
        DepthTrace(values, point.depth_um, point_id, bounds, "ROI [{}:{}, {}:{}]".format(*bounds))
        for values, bounds in zip(sums, regions, strict=True)
    ]


REFERENCE_NAMES: dict[str, str] = dict(REFERENCE_LABELS)
"""lauelab's reference images (``REFERENCE_OPTIONS``' values) and their labels ("Sum of raw frames")."""


def roi_bounds(size: int, x: float, y: float, shape: tuple[int, int]) -> Bounds | None:
    """The square ROI of ``size`` pixels centred as near as it can be to ``(x, y)`` (lauelab's ``square_bounds``:
    never clipped or shifted), as ``(y0, y1, x0, x1)``; None when it would leave the
    ``(rows, columns)`` image (or ``size`` is not a positive whole number, or the position not finite)."""

    try:
        return square_bounds(size, x, y, shape)
    except InputError:
        return None


# ---------------------------------------------------------------------------
# A point reconstructed elsewhere, published as a finished one-point scan (``laueportal seed demo``)
#
# lauelab has no public way to catalog a point file it did not reconstruct in that scan, so this uses its
# catalog writer (``lauelab.reconstruct._scan_writer.write_catalog``, which validates what it writes) and its
# layout constants. lauelab is pinned; ``tests/science/test_recon.py`` reads the result back through
# the public ``ScanReader``, so an incompatible lauelab fails there.

_POINT_GROUP = scan_layout.RECONSTRUCTION  # "/entry1/reconstruction/"
_MANIFEST_INDEX = _POINT_GROUP + "manifest_index"


@dataclass(frozen=True)
class PointRecord:
    """What a point file records about its reconstruction (``read_point_record``)."""

    point_id: str
    source_path: str
    """The raw wire scan, as the reconstructing process named it."""
    geometry_path: str
    scan_number: int | None
    shape: tuple[int, int, int]
    """(depths, rows, columns)."""
    depth_bounds_um: tuple[float, float]
    settings: dict[str, Any]
    """``PointReader.settings``: the reconstruction settings used (depth range, resolution, wire edge, ...)."""
    num_threads: int
    elapsed_seconds: float
    created: datetime | None
    """When lauelab wrote the point file (its ``created`` root attribute), aware UTC; None if unreadable."""
    lauelab_version: str | None
    native_version: str


def _text_value(value: Any) -> str:
    if isinstance(value, np.ndarray):
        value = value.reshape(-1)[0]
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _created(text: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text) if text else None
    except ValueError:
        return None
    if moment is None or moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def read_point_record(path: str | os.PathLike[str]) -> PointRecord:
    """Validate a point file (lauelab's ``PointReader``) and read its record. Raises OSError or ValueError."""

    with PointReader(path) as point:
        settings = dict(point.settings)
        depths = np.asarray(point.depth_um, dtype=float)
        record = {
            "point_id": point.point_id,
            "source_path": point.source_path,
            "geometry_path": str(settings["geometry_path"]),
            "scan_number": point.scan_number,
            "shape": cast(tuple[int, int, int], tuple(int(value) for value in point.shape)),
            "depth_bounds_um": (float(depths[0]), float(depths[-1])),
            "settings": settings,
        }
    with h5py.File(path, "r") as file:
        group = file[_POINT_GROUP]
        assert isinstance(group, h5py.Group)
        num_threads = int(np.asarray(group["num_threads"]).reshape(-1)[0])
        elapsed = float(np.asarray(group["elapsed_seconds"]).reshape(-1)[0])
        native = _text_value(np.asarray(group["native_version"]))
        created = file.attrs.get("created")
        version = file.attrs.get("lauelab_version")
    return PointRecord(
        **record,
        num_threads=num_threads,
        elapsed_seconds=elapsed,
        created=_created(_text_value(created) if created is not None else None),
        lauelab_version=_text_value(version) if version is not None else None,
        native_version=native,
    )


def publish_point_scan(source: str | os.PathLike[str], scan_directory: str | os.PathLike[str]) -> PointRecord:
    """Make ``scan_directory`` a finished one-point lauelab scan holding a copy of the point file ``source``
    (``publish_scan``). Returns the point's record."""

    return publish_scan([source], scan_directory)[0]


def publish_scan(
    sources: Sequence[str | os.PathLike[str]], scan_directory: str | os.PathLike[str]
) -> list[PointRecord]:
    """Make ``scan_directory`` a finished lauelab scan holding a copy of each point file of ``sources``, in their
    order (point IDs distinct, the settings and geometry of the first).

    ``scan_directory`` (a run's ``reconstruction/``) must not exist or be empty. Each copy goes to
    ``points/<point_id>.h5``, with its manifest index set to its position: a point the catalog lists must say so
    (``ScanReader.point`` checks it), and a standalone point has none. The sources are only read. Then the catalog
    ``scan.h5`` is published, run status finished, with every point complete and the settings the first point
    records. Returns the points' records. Raises OSError or ValueError (lauelab's ``InvalidScanFile``).
    """

    directory = Path(scan_directory)
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError(f"{directory} is not empty")
    if not sources:
        raise ValueError("a scan needs at least one point")
    records: list[PointRecord] = []
    relatives: list[str] = []
    positions: list[tuple[float, ...]] = []
    energies: list[float] = []
    geometry_xml = ""
    for index, source in enumerate(sources):
        record = read_point_record(source)
        stem = record.point_id
        if (problem := scan_layout.unsafe_stem(stem)) is not None:
            raise ValueError(f"point ID {stem!r} cannot name a point file: {problem}")
        if any(earlier.point_id == stem for earlier in records):
            raise ValueError(f"point ID {stem!r} is given twice")
        relative = scan_layout.point_path(stem)
        point = directory / relative
        point.parent.mkdir(parents=True, exist_ok=True)
        partial = point.with_name(point.name + ".partial")
        shutil.copyfile(source, partial)
        try:
            with h5py.File(partial, "r+") as file:
                if _MANIFEST_INDEX in file:
                    del file[_MANIFEST_INDEX]
                file.create_dataset(_MANIFEST_INDEX, data=np.array([index], dtype=np.int32))
            read_point_record(partial)  # still a valid point file
            os.replace(partial, point)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        with PointReader(point) as reader:
            geometry_xml = geometry_xml or reader.geometry_xml()
            positions.append(tuple(float(value) for value in reader.sample_position))
            energies.append(np.nan if reader.energy_kev is None else reader.energy_kev)
        records.append(record)
        relatives.append(relative)

    first = records[0]
    settings = first.settings
    status_type = cast("np.typing.DTypeLike", scan_layout.POINT_COLUMNS["/points/statuses"].dtype)
    points = {
        "/points/point_ids": [record.point_id for record in records],
        "/points/paths": relatives,
        "/points/statuses": np.array([scan_layout.PointStatus.COMPLETE] * len(records), dtype=status_type),
        "/points/errors": ["" for _ in records],
        "/points/source_paths": [record.source_path for record in records],
        "/points/scan_numbers": [-1 if record.scan_number is None else record.scan_number for record in records],
        "/points/sample_positions": positions,
        "/points/energies_kev": energies,
        "/points/shapes": [record.shape for record in records],
        "/points/depth_bounds": [record.depth_bounds_um for record in records],
        "/points/dtypes": [record.settings["output_pixel_type"] for record in records],
    }
    catalog = directory / scan_layout.SCAN_FILENAME
    write_catalog(
        catalog,
        created=datetime.now(UTC).isoformat(),
        run_status=scan_layout.RunStatus.FINISHED,
        native_version=first.native_version,
        parameters=parameter_values(int(settings["detector"]), settings),
        geometry_path=first.geometry_path,
        geometry_xml=geometry_xml,
        points=points,
        replace=False,
    )
    with ScanReader(catalog) as scan:  # the catalog and its points agree
        for record in records:
            with scan.point(record.point_id):
                pass
    return records
