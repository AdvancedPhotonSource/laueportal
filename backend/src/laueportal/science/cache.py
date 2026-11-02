"""Server-side cache of normalized lauelab visualization datasets.

Requests name an indexing run; the results viewer resolves its artifact (``indexing.viewer``) and the normalized
dataset lives here, keyed by the file's identity (real path, size, modification and change times) and the
geometry file it was loaded with (its path and identity), so a republished file or a geometry file edited in place
(also one rewritten with its size and modification time kept) is reloaded and an unchanged one is reused across
requests. The cache is bounded (``indexing.dataset_cache_size``, 6 datasets by default) and evicts the least recently
used dataset; the app keeps one (``app.state.datasets``). A file loads once however many requests ask for it, a load
holds up only the requests for that file (per-key loading), and at most ``MAX_LOADS`` files load at once, at most
``MAX_XML_LOADS`` of them Output XMLs (an XML's parse peaks at about 7.5 times the file's size). What the viewer
derives from a dataset (its peaks and patterns tables, ``DatasetCache.derived``) is kept with it and evicted with it.

**Geometry.** lauelab would resolve a geometry path the file names (``output.h5``'s ``/geometry@path``, an Output XML's
``geoFile``) by itself, and that path may be another host's. ``load_dataset`` never lets it: a dataset is loaded with
the geometry file the caller mapped and confined (``geometry=``), with the geometry text a results file embeds, or
without geometry.
"""

from __future__ import annotations

import dataclasses
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, TypeVar

import anyio.to_thread
import lauelab
from lauelab.indexing import Geometry
from lauelab.visualization import VisualizationDataset, load_results, load_visualization_xml

from laueportal.science.once import Pending
from laueportal.science.results import named_geometry

DEFAULT_CAPACITY = 6
"""The default capacity; the app's comes from ``indexing.dataset_cache_size``."""

MAX_LOADS = 2
"""Files loading at once; the others wait for a turn. It bounds the transients the cache's capacity does not: an
``output.h5`` of 627,535 peaks loads in 0.4 s at a peak of about 270 MB, into a dataset of about 207 MB
(measured in a process of its own)."""

MAX_XML_LOADS = 1
"""Output XMLs loading at once (each also takes one of ``MAX_LOADS``). lauelab parses the whole tree: a 344 MB
XML loads in 24 s at a peak of 2.6 GB RSS, so two at once would need more than 5 GB.
Every run migrated from v1 is XML-only until converted (``indexing.conversion``), so this is a migrated run's
first open."""

T = TypeVar("T")

KIND_RESULTS = "results"
KIND_XML = "xml"


@dataclass(frozen=True)
class FileIdentity:
    path: str
    """The real path (links resolved)."""
    size: int
    mtime_ns: int
    ctime_ns: int
    """The change time: a file rewritten in place can keep its size and modification time (``cp -p``,
    ``rsync --inplace -t``), never its change time (``science.results.validate_results``)."""


def file_identity(path: str | os.PathLike[str]) -> FileIdentity:
    real = os.path.realpath(path)
    status = os.stat(real)
    return FileIdentity(real, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


@dataclass(frozen=True)
class SourceKey:
    """What a loaded dataset depends on: the results file or Output XML, and the geometry file it loads with."""

    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    geometry: str | None
    """The geometry file given to lauelab, as given."""
    geometry_file: FileIdentity | None
    """Its identity, so that one edited in place is reloaded; None without one, or when it cannot be read (lauelab
    skips it)."""


def source_key(path: str | os.PathLike[str], geometry: str | None = None) -> SourceKey:
    source = file_identity(path)
    try:
        geometry_file = file_identity(geometry) if geometry is not None else None
    except OSError:
        geometry_file = None
    return SourceKey(source.path, source.size, source.mtime_ns, source.ctime_ns, geometry, geometry_file)


def source_kind(path: str | os.PathLike[str]) -> str:
    return KIND_RESULTS if lauelab.is_results_file(path) else KIND_XML


def _unresolved() -> Geometry:
    """A stand-in lauelab keeps as the dataset's geometry without using it, so that it resolves no path the file
    names (``load_results`` and ``load_visualization_xml`` only store the geometry they are given)."""
    return Geometry.__new__(Geometry)


def load_dataset(path: str | os.PathLike[str], *, geometry: str | None = None) -> VisualizationDataset:
    """Load a results file or a LaueGo Output XML into the shared model (uncached).

    ``geometry`` is the geometry file to use: a path the caller mapped and confined to the file roots (one lauelab
    cannot read is skipped, as lauelab skips the file's own). Without it, a results file's embedded geometry text
    is used when it has one, and otherwise the dataset has no geometry: no geometry path the file names is ever
    opened."""

    given = _geometry(geometry) if geometry is not None else None
    if source_kind(path) == KIND_RESULTS:
        if given is not None:
            return load_results(path, geometry=given)
        if geometry is None and named_geometry(path).embedded:
            return load_results(path)
        return dataclasses.replace(load_results(path, geometry=_unresolved()), geometry=None)
    if given is not None:
        return load_visualization_xml(path, geometry=given)
    return dataclasses.replace(load_visualization_xml(path, geometry=_unresolved()), geometry=None)


def _geometry(path: str) -> Geometry | None:
    try:
        return Geometry(path)
    except (ImportError, OSError, ValueError):
        return None


class _Entry:
    def __init__(self, dataset: VisualizationDataset) -> None:
        self.dataset = dataset
        self.derived: dict[Hashable, Any] = {}
        self.pending: dict[Hashable, Pending] = {}


class DatasetCache:
    """Bounded least-recently-used cache of datasets by source identity (``SourceKey``), and of what is derived
    from each (``derived``: the results viewer's tables), which lives and goes with its dataset.

    One load at a time per key (``science.once``): a request for a dataset another request is loading waits for that
    load (and gets its dataset, or a copy of its error), while hits and loads of other files go ahead, ``max_loads``
    at once, of which ``max_xml_loads`` Output XMLs; the cache's lock is never held during a load, and a load that
    fails, or whose bookkeeping does, leaves no key loading. Deriving works the same way per dataset and name.

    ``get`` waits in its thread; ``load`` is ``get`` for an ``async`` route (the results viewer's): a request waiting
    for another's load of the same file waits in the event loop, holding no thread (``Pending.wait_async``), and a
    request that loads runs the load (and its wait for one of the ``max_loads`` turns) in a worker thread."""

    def __init__(
        self, capacity: int = DEFAULT_CAPACITY, *, max_loads: int = MAX_LOADS, max_xml_loads: int = MAX_XML_LOADS
    ):
        if capacity < 1 or max_loads < 1 or max_xml_loads < 1:
            raise ValueError("capacity, max_loads and max_xml_loads must be positive")
        self.capacity = capacity
        self._loads = threading.BoundedSemaphore(max_loads)
        self._xml_loads = threading.BoundedSemaphore(max_xml_loads)
        self._entries: OrderedDict[SourceKey, _Entry] = OrderedDict()
        self._loading: dict[SourceKey, Pending] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, path: str | os.PathLike[str], *, geometry: str | None = None) -> VisualizationDataset:
        key = source_key(path, geometry)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                self.hits += 1
                return entry.dataset
            pending = self._loading.get(key)
            loads = pending is None
            if pending is None:
                pending = self._loading[key] = Pending()
                self.misses += 1
            else:
                self.hits += 1
        if not loads:
            return pending.wait()
        return pending.settle(
            lambda: self._load(key, geometry),
            keep=lambda dataset: self._keep(key, dataset),
            lock=self._lock,
            table=self._loading,
            keys=(key,),
        )

    async def load(self, key: SourceKey) -> VisualizationDataset:
        """``get`` of the file ``key`` names (its identity, ``source_key(path, geometry)``, read by the caller), for
        a coroutine: a hit at once, a load another request makes waited for in the event loop, a load of its own in
        a worker thread."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                self.hits += 1
                return entry.dataset
            pending = self._loading.get(key)
            loads = pending is None
            if pending is None:
                pending = self._loading[key] = Pending()
                self.misses += 1
            else:
                self.hits += 1
        if not loads:
            return await pending.wait_async()
        claimed = pending

        def settle() -> VisualizationDataset:
            return claimed.settle(
                lambda: self._load(key, key.geometry),
                keep=lambda dataset: self._keep(key, dataset),
                lock=self._lock,
                table=self._loading,
                keys=(key,),
            )

        return await anyio.to_thread.run_sync(settle)  # not abandoned when cancelled: the key is always released

    def _load(self, key: SourceKey, geometry: str | None) -> VisualizationDataset:
        with ExitStack() as turn:
            if source_kind(key.path) == KIND_XML:
                turn.enter_context(self._xml_loads)  # first, so an XML waiting for its turn holds no load's
            turn.enter_context(self._loads)
            return load_dataset(key.path, geometry=geometry)

    def _keep(self, key: SourceKey, dataset: VisualizationDataset) -> None:
        with self._lock:
            # Any older entry for the same path is stale: the file changed.
            for stale in [entry for entry in self._entries if entry.path == key.path]:
                del self._entries[stale]
            self._entries[key] = _Entry(dataset)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
                self.evictions += 1

    def derived(self, dataset: VisualizationDataset, name: Hashable, make: Callable[[VisualizationDataset], T]) -> T:
        """``make(dataset)``, made once and kept with ``dataset`` while the cache holds it (made each time for a
        dataset it no longer holds)."""

        with self._lock:
            key = next((key for key, entry in self._entries.items() if entry.dataset is dataset), None)
            entry = self._entries[key] if key is not None else None
            if key is not None:
                self._entries.move_to_end(key)  # a use of the dataset
            if entry is None:
                pending, makes = None, True
            elif name in entry.derived:
                return entry.derived[name]
            else:
                pending = entry.pending.get(name)
                makes = pending is None
                if pending is None:
                    pending = entry.pending[name] = Pending()
        if not makes:
            assert pending is not None
            return pending.wait()
        if entry is None or pending is None:
            return make(dataset)
        kept = entry

        def keep(value: T) -> None:
            with self._lock:
                kept.derived[name] = value

        return pending.settle(lambda: make(dataset), keep=keep, lock=self._lock, table=entry.pending, keys=(name,))

    def holds(self, dataset: VisualizationDataset, name: Hashable) -> bool:
        """Whether ``derived(dataset, name, ...)`` has its value kept (not being made, nor to make)."""

        with self._lock:
            entry = next((entry for entry in self._entries.values() if entry.dataset is dataset), None)
            return entry is not None and name in entry.derived

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def keys(self) -> tuple[SourceKey, ...]:
        return tuple(self._entries)
